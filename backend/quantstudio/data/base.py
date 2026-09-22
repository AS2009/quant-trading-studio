# -*- coding: utf-8 -*-
"""HTTP Provider 基类：只用标准库 ``urllib.request`` 完成 GET / JSON / 解码 / 重试 / 自检。

职责
----
- ``_get_text`` / ``_get_json``：带 UA、Referer、gzip、超时、重试的 GET；
  自动探测 GBK / UTF-8（新浪是 GBK、东方财富是 UTF-8）；失败**统一抛**
  :class:`~quantstudio.core.errors.DataSourceError`。
- ``health()``：访问一个轻量接口并计时，返回 ``{'ok', 'detail', 'latency_ms'}``。
- ``_code_to_secid`` / ``_code_to_sina``：调用 :mod:`quantstudio.data.symbols` 做代码转换。
- 缓存读写包装（``_cache_get`` / ``_cache_set`` / ``_cache_get_stale``）与数值清洗
  （``_num`` / ``_num_or_none``，用于处理数据源返回的 ``"-"`` 占位）。

离线模式
--------
``settings.offline=True``（``QUANTSTUDIO_OFFLINE=1``）时，任何真实网络请求都会直接抛
:class:`ProviderUnavailable`，**不会发起连接**。
"""

import gzip
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import CacheTTL, Settings, get_settings
from ..core.errors import DataSourceError, ProviderUnavailable, SymbolNotFound
from . import symbols
from .cache import DiskCache

__all__ = ["BaseHTTPProvider"]

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: 触发重试的异常（网络层问题）
_RETRYABLE = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    http.client.HTTPException,
    socket.timeout,
    socket.gaierror,
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
    OSError,
)


class BaseHTTPProvider:
    """所有真实行情源（新浪 / 东方财富）的公共基类。"""

    name = "base"
    #: 该数据源要求的 Referer（缺失会被拒答）
    referer = ""
    user_agent = _DEFAULT_UA
    #: 子类实现：返回一行自检描述，失败时抛异常（供 health 使用）
    health_url = ""
    health_referer = ""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings if settings is not None else get_settings()
        self.timeout = float(getattr(self.settings, "http_timeout", 8) or 8)
        self.retries = max(0, int(getattr(self.settings, "http_retries", 2) or 0))
        self.offline = bool(getattr(self.settings, "offline", False))
        self.cache = DiskCache(getattr(self.settings, "cache_dir", ""))
        self.ttl = getattr(self.settings, "cache_ttl", None) or CacheTTL()
        self.last_latency_ms = 0
        #: 代码 → 名称的进程内快照（不落盘，避免名称过期后仍被长期使用）
        self._name_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ HTTP
    def _build_headers(self, referer: str = "", headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        out = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        ref = referer or self.referer
        if ref:
            out["Referer"] = ref
        for key, value in (headers or {}).items():
            out[str(key)] = str(value)
        return out

    def _fetch(
        self,
        url: str,
        referer: str = "",
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[bytes, str]:
        """GET 一次（含重试），返回 ``(原始字节, 响应头声明的字符集)``。"""
        if self.offline:
            raise ProviderUnavailable(
                "%s 处于离线模式（QUANTSTUDIO_OFFLINE=1），未发起网络请求" % self.name
            )
        target = url
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if query:
                target = url + ("&" if "?" in url else "?") + query

        attempts = self.retries + 1
        last_error = None
        for attempt in range(attempts):
            started = time.time()
            try:
                request = urllib.request.Request(target, headers=self._build_headers(referer, headers))
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 200) or 200)
                    if status >= 400:
                        raise DataSourceError("HTTP %s" % status)
                    raw = response.read()
                    encoding = ""
                    try:
                        encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
                        charset = response.headers.get_content_charset() or ""
                    except Exception:  # pragma: no cover - 极端响应对象
                        charset = ""
                if encoding == "gzip":
                    raw = gzip.decompress(raw)
                elif encoding == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                self.last_latency_ms = int((time.time() - started) * 1000)
                return raw, (charset or "")
            except DataSourceError as exc:      # 4xx/5xx：重试意义不大，但仍给一次机会
                last_error = exc
            except _RETRYABLE as exc:
                last_error = exc
            except Exception as exc:            # 解析等其它异常：直接抛出，不重试
                raise DataSourceError("GET %s 失败：%s: %s" % (target, type(exc).__name__, exc))
            if attempt < attempts - 1:
                time.sleep(min(0.4 * (attempt + 1), 2.0))

        raise DataSourceError(
            "GET %s 失败（已重试 %d 次）：%s: %s"
            % (target, attempts, type(last_error).__name__, last_error)
        )

    @staticmethod
    def _decode(raw: bytes, charset: str = "") -> str:
        """GBK / UTF-8 解码探测：响应头声明的字符集 → UTF-8 → GBK → 兜底 replace。"""
        if not raw:
            return ""
        order: List[str] = []
        declared = (charset or "").strip().lower()
        if declared:
            order.append("gbk" if declared in ("gb2312", "gbk", "gb18030") else declared)
        order.extend(["utf-8", "gbk"])
        seen = set()
        for codec in order:
            if codec in seen:
                continue
            seen.add(codec)
            try:
                return raw.decode(codec)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def _get_text(
        self,
        url: str,
        referer: str = "",
        params: Optional[Dict[str, Any]] = None,
        encoding: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        raw, charset = self._fetch(url, referer=referer, params=params, headers=headers)
        return self._decode(raw, encoding or charset)

    def _get_json(
        self,
        url: str,
        referer: str = "",
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """GET 并解析 JSON；失败抛 :class:`DataSourceError`。"""
        text = self._get_text(url, referer=referer, params=params, headers=headers)
        text = (text or "").strip()
        if not text:
            raise DataSourceError("GET %s 返回空响应" % url)
        try:
            return json.loads(text)
        except ValueError:
            pass
        # 容错：JSONP / var xxx = {...};
        body = text
        start = body.find("{")
        if start == -1:
            start = body.find("[")
        end = body.rfind("}")
        alt_end = body.rfind("]")
        end = max(end, alt_end)
        if start >= 0 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                pass
        raise DataSourceError("响应不是合法 JSON：%s" % text[:120].replace("\n", " "))

    # ------------------------------------------------------------------ 自检
    def _probe(self) -> str:
        """自检探测（子类实现）：成功返回一行描述，失败抛异常。"""
        if not self.health_url:
            raise ProviderUnavailable("%s 未实现 health 探测" % self.name)
        text = self._get_text(self.health_url, referer=self.health_referer)
        return "%s 可用（响应 %d 字节）" % (self.name, len(text or ""))

    def health(self) -> Dict[str, Any]:
        """自检：``{'ok', 'detail', 'latency_ms'}``（离线模式不发起请求）。"""
        started = time.time()
        if self.offline:
            return {
                "ok": False,
                "detail": "离线模式（QUANTSTUDIO_OFFLINE=1）：%s 不会发起网络请求" % self.name,
                "latency_ms": 0,
                "source": self.name,
            }
        try:
            detail = self._probe()
            ok = True
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
            ok = False
        return {
            "ok": ok,
            "detail": detail,
            "latency_ms": int((time.time() - started) * 1000),
            "source": self.name,
        }

    # ------------------------------------------------------------------ 代码转换
    def _code_to_secid(self, code: str) -> str:
        """项目代码 → 东方财富 secid（``600519.SH`` → ``1.600519``）。"""
        return symbols.to_eastmoney_secid(code)

    def _code_to_sina(self, code: str) -> str:
        """项目代码 → 新浪代码（``600519.SH`` → ``sh600519``）。"""
        return symbols.to_sina_symbol(code)

    @staticmethod
    def _normalize_all(codes: Sequence[str], on_error: str = "skip") -> List[str]:
        """批量规范化（默认跳过非法代码，``on_error='raise'`` 时抛 SymbolNotFound）。"""
        out: List[str] = []
        for item in codes or []:
            try:
                norm = symbols.normalize(item)
            except SymbolNotFound:
                if on_error == "raise":
                    raise
                continue
            if norm not in out:
                out.append(norm)
        return out

    # ------------------------------------------------------------------ 数值清洗
    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        """数据源的 ``"-"`` / ``None`` / ``""`` → default。"""
        if value is None:
            return default
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().replace(",", "").replace("%", "")
        if not text or text in ("-", "--", "null", "None"):
            return default
        try:
            return float(text)
        except ValueError:
            return default

    @classmethod
    def _num_or_none(cls, value: Any) -> Optional[float]:
        """同上，但无效时返回 None（用于 PE / PB 这类可为空的字段）。"""
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip().replace(",", "").replace("%", "")
        if not text or text in ("-", "--", "null", "None"):
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ 缓存包装
    def _cache_get(self, namespace: str, args: Sequence[Any]) -> Any:
        return self.cache.get(self.cache.make_key(namespace, list(args)))

    def _cache_set(self, namespace: str, args: Sequence[Any], value: Any, ttl: float) -> bool:
        return self.cache.set(self.cache.make_key(namespace, list(args)), value, ttl)

    def _cache_get_stale(self, namespace: str, args: Sequence[Any]) -> Any:
        return self.cache.get_stale(self.cache.make_key(namespace, list(args)))

    # ------------------------------------------------------------------ 名称缓存
    def remember_name(self, code: str, name: str) -> None:
        if name:
            try:
                self._name_cache[symbols.normalize(code)] = str(name)
            except SymbolNotFound:
                pass

    def known_name(self, code: str) -> Optional[str]:
        try:
            norm = symbols.normalize(code)
        except SymbolNotFound:
            return None
        return self._name_cache.get(norm) or symbols.INDEX_NAMES.get(norm)
