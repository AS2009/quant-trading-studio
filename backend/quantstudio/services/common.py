# -*- coding: utf-8 -*-
"""业务服务层公共设施。

- 统一响应信封：``{"data": ..., "as_of": "...", "meta": {...}}``
- 数据源元信息（DataMeta）读取与兜底
- 进程内 TTL 缓存（带命中统计，供行情与回测复用）
- 参数规范化 / 校验、JSON 文件原子读写

本模块**不含 HTTP 细节**：只抛 ``core.errors`` 中的异常（或本模块的
``NotFound`` / ``Forbidden``），由接口层统一映射为状态码。
"""

import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core.errors import QuantStudioError, ValidationError
from ..core.models import DataMeta

TIME_FMT = "%Y-%m-%d %H:%M:%S"


class NotFound(QuantStudioError):
    """资源不存在（接口层映射为 404）。"""


class Forbidden(QuantStudioError):
    """操作被拒绝（接口层映射为 403）。"""


# --------------------------------------------------------------------------- 时间 / 序列化


def now_str() -> str:
    """当前本地时间（响应 as_of 的统一格式）。"""
    return datetime.now().strftime(TIME_FMT)


def to_dict(value: Any) -> Any:
    """模型 / 字典 → 可 JSON 序列化的 dict。"""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError("无法序列化为 dict：%r" % (type(value),))


def to_dict_list(values: Any) -> List[Dict[str, Any]]:
    return [to_dict(item) for item in (values or [])]


# --------------------------------------------------------------------------- 数据源元信息


def get_data_provider(settings: Any = None):
    """惰性获取数据源（不缓存到服务层，由 data 层维护单例）。

    优先把服务的 settings 传给 ``data.get_provider(settings)``，保证自定义 data_dir /
    offline 生效；若实现只接受无参签名则回退到 ``get_provider()``。
    """
    from ..data import get_provider

    if settings is None:
        return get_provider()
    try:
        return get_provider(settings)
    except TypeError:
        return get_provider()


def provider_meta(provider: Any = None) -> DataMeta:
    """读取 provider.last_meta；缺失 / provider 不可用时返回 DataMeta(source="unknown")。"""
    if provider is None:
        try:
            from ..data import get_provider  # 惰性导入：并行模块未就绪时不阻塞导入

            provider = get_provider()
        except Exception:
            return DataMeta(source="unknown")
    meta = getattr(provider, "last_meta", None)
    if isinstance(meta, DataMeta):
        return meta
    if isinstance(meta, dict):
        return _meta_from_dict(meta)
    name = getattr(provider, "name", "") or "unknown"
    return DataMeta(source=name)


def _meta_from_dict(raw: Dict[str, Any]) -> DataMeta:
    notes = raw.get("notes")
    if isinstance(notes, str):
        notes = [notes]
    return DataMeta(
        source=str(raw.get("source") or "unknown"),
        stale=bool(raw.get("stale", False)),
        offline=bool(raw.get("offline", False)),
        as_of=str(raw.get("as_of") or ""),
        latency_ms=int(raw.get("latency_ms") or 0),
        notes=list(notes or []),
    )


def envelope(
    data: Any,
    settings: Any = None,
    meta: Any = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一响应信封。

    - ``meta`` 可为 DataMeta / dict / None（None 时尝试读取 provider.last_meta）
    - ``as_of`` 优先使用显式值，其次 meta.as_of，最后当前时间
    """
    if meta is None:
        meta_obj = provider_meta()
    elif isinstance(meta, DataMeta):
        meta_obj = meta
    elif isinstance(meta, dict):
        meta_obj = _meta_from_dict(meta)
    else:
        meta_obj = DataMeta(source=str(meta))

    payload = meta_obj.to_dict()
    payload["notes"] = list(payload.get("notes") or [])
    if settings is not None and getattr(settings, "offline", False):
        payload["offline"] = True
    stamp = as_of or payload.get("as_of") or now_str()
    payload["as_of"] = stamp
    return {"data": data, "as_of": stamp, "meta": payload}


def trading_calendar(settings: Any = None):
    """交易日历：读取 data/holidays.json（缺省仅剔除周末）。"""
    from ..core.calendar import TradingCalendar

    if settings is None:
        return TradingCalendar()
    return TradingCalendar.from_file(settings.path("holidays.json"))


# --------------------------------------------------------------------------- 进程内 TTL 缓存

_CACHE_LOCK = threading.RLock()
_CACHE: "Dict[str, Tuple[float, Any]]" = {}
_CACHE_STATS = {"hits": 0, "misses": 0, "expired": 0, "entries": 0}
_MAX_ENTRIES = 256


def cached(key: str, ttl: float, fn, force: bool = False) -> Any:
    """轻量进程内缓存：key 命中且未过期则返回缓存值，否则调用 fn 并写入。

    ttl <= 0 时视为不缓存（每次调用 fn）。
    """
    if ttl is None or ttl <= 0:
        return fn()
    now = time.time()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is not None:
            if item[0] > now and not force:
                _CACHE_STATS["hits"] += 1
                return item[1]
            _CACHE_STATS["expired"] += 1
        _CACHE_STATS["misses"] += 1
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now + float(ttl), value)
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE_STATS["entries"] = len(_CACHE)
    return value


def cache_clear(key: Optional[str] = None) -> Dict[str, Any]:
    """清空（或删除单个 key）并返回统计。"""
    with _CACHE_LOCK:
        if key is None:
            _CACHE.clear()
        else:
            _CACHE.pop(key, None)
        _CACHE_STATS["entries"] = len(_CACHE)
        return dict(_CACHE_STATS)


def cache_stats() -> Dict[str, Any]:
    with _CACHE_LOCK:
        stats = dict(_CACHE_STATS)
        stats["size"] = len(_CACHE)
        stats["max_size"] = _MAX_ENTRIES
        return stats


# --------------------------------------------------------------------------- 参数校验


_ARGS_CODE_SUFFIX = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_ARGS_CODE_PREFIX = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_ARGS_CODE_PLAIN = re.compile(r"^\d{6}$")


def _guess_market(digits: str) -> str:
    if digits[0] in ("0", "3"):
        return "SZ"
    if digits[0] in ("4", "8"):
        return "BJ"
    return "SH"


def normalize_code(value: Any, field: str = "code") -> str:
    """把 600519 / sh600519 / 600519.SH 统一成 600519.SH；非法格式抛 ValidationError。"""
    text = str(value if value is not None else "").strip().upper().replace(" ", "")
    if not text:
        raise ValidationError("标的代码不能为空", field=field)
    match = _ARGS_CODE_SUFFIX.match(text)
    if match:
        return "%s.%s" % (match.group(1), match.group(2))
    match = _ARGS_CODE_PREFIX.match(text)
    if match:
        return "%s.%s" % (match.group(2), match.group(1))
    if _ARGS_CODE_PLAIN.match(text):
        return "%s.%s" % (text, _guess_market(text))
    raise ValidationError(
        "标的代码格式非法：%s（示例：600519.SH 或 600519）" % value, field=field
    )


def normalize_codes(raw: Any, field: str = "codes") -> List[str]:
    """逗号分隔 / 列表 → 规范化代码列表（去空、去重、保持顺序）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = str(raw).replace("，", ",").split(",")
    out: List[str] = []
    seen = set()
    for part in parts:
        text = str(part if part is not None else "").strip()
        if not text:
            continue
        code = normalize_code(text, field=field)
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def parse_int(
    value: Any,
    field: str,
    default: Optional[int] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    clamp: bool = False,
) -> int:
    """整数字段解析；clamp=True 时超界夹取，否则超界报 400。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValidationError("%s 需为整数" % field, field=field)
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(
            "%s 需为整数，当前为 %r" % (field, value), field=field
        )
    if minimum is not None and number < minimum:
        if clamp:
            number = minimum
        else:
            raise ValidationError("%s 不能小于 %d" % (field, minimum), field=field)
    if maximum is not None and number > maximum:
        if clamp:
            number = maximum
        else:
            raise ValidationError("%s 不能大于 %d" % (field, maximum), field=field)
    return number


def parse_float(
    value: Any,
    field: str,
    default: Optional[float] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    allow_none: bool = False,
) -> Optional[float]:
    """数字字段解析；非法 / NaN / Infinity 一律 400。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        if default is None:
            raise ValidationError("%s 需为数字" % field, field=field)
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(
            "%s 需为数字，当前为 %r" % (field, value), field=field
        )
    if math.isnan(number) or math.isinf(number):
        raise ValidationError("%s 需为有限数字" % field, field=field)
    if minimum is not None and number < minimum:
        raise ValidationError("%s 不能小于 %s" % (field, minimum), field=field)
    if maximum is not None and number > maximum:
        raise ValidationError("%s 不能大于 %s" % (field, maximum), field=field)
    return number


_ALLOWED_FREQ = ("day", "week", "month")
_ALLOWED_ADJUST = ("qfq", "hfq", "none")


def parse_freq(value: Any, default: str = "day") -> str:
    text = str(value or default).strip().lower()
    if text not in _ALLOWED_FREQ:
        raise ValidationError(
            "freq 仅支持 day/week/month，当前为 %r" % value, field="freq"
        )
    return text


def parse_adjust(value: Any, default: str = "qfq") -> str:
    text = str(value or default).strip().lower()
    if text not in _ALLOWED_ADJUST:
        raise ValidationError(
            "adjust 仅支持 qfq/hfq/none，当前为 %r" % value, field="adjust"
        )
    return text


# --------------------------------------------------------------------------- JSON 文件读写

_FILE_LOCK = threading.RLock()


def read_json(path: str, default: Any = None) -> Any:
    """读取 JSON；文件不存在 / 损坏时返回 default。"""
    with _FILE_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default


def write_json_atomic(path: str, payload: Any) -> None:
    """原子写 JSON：同目录临时文件 + fsync + os.replace。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    with _FILE_LOCK:
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
