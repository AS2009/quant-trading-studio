# -*- coding: utf-8 -*-
"""Composite Provider：方法级降级链，保证**任何单一数据源故障都不会让接口 500**。

降级链（方法级，四条链路各自独立）
--------------------------------
- 实时类（``latest_quotes`` / ``index_quotes`` / ``resolve_name``）::

      sina → tencent → eastmoney → 磁盘缓存（过期也返回） → CSV → 示例数据

- 历史类（``kline``）::

      eastmoney → tencent → sina → 磁盘缓存（过期也返回） → CSV → 示例数据

- 板块（``sectors``）::

      eastmoney → tencent → 磁盘缓存 → CSV → 示例数据

- 广度（``breadth``）::

      eastmoney → tencent → 磁盘缓存 → CSV → 示例数据

- 盘口 / 逐笔（``orderbook`` / ``ticks``）::

      tencent → sina → 磁盘缓存（过期也返回） → CSV → 示例数据

  这两条链**显式腾讯优先**（``CHAIN_PRIORITY["realtime"]`` 是新浪优先）：只有腾讯快照带
  外盘 / 内盘，也只有腾讯（免费源里）提供逐笔成交；``capital_flow`` 不做本地降级，
  直接由 ``ticks`` 按单笔成交额分档自算。

``sina`` 不提供 K 线 / 板块 / 广度（抛 ``ProviderUnavailable``），放在历史链里只是「按序尝试」；
``ProviderUnavailable`` 与 ``DataSourceError`` 一样会被视为该源不可用并继续往下走。

每次调用后都会刷新 :attr:`last_meta`（:class:`~quantstudio.core.models.DataMeta`），
服务层直接读它写进响应信封：``source`` / ``stale`` / ``offline`` / ``as_of`` / ``latency_ms`` / ``notes``。
标的质量口径（例如「腾讯行业板块口径（含新三板，合计家数偏大）」）由各 Provider 的
``last_notes`` 透传到 ``meta.notes``。

约定
----
- 只有**真实源**（sina / tencent / eastmoney）的成功结果会写入磁盘缓存；CSV / 示例数据不写，
  避免把本地数据伪装成「缓存下来的真实行情」。
- 命中缓存 / CSV / 示例数据时 ``meta.offline=True``（已降级为本地数据）；
  未过期的缓存 ``stale=False``，过期缓存 ``stale=True``。
- ``cache`` 是**内部兜底层**而非 Provider：``describe()`` 用 ``kind="internal"`` +
  ``enabled/entries/hits/stale_hits`` 如实报告，不会显示成「构造失败的数据源」。
- ``ValidationError``（入参非法，如 freq 写错）不参与降级，直接上抛给接口层返回 400。
"""

import dataclasses
import datetime
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import CacheTTL, Settings, get_settings
from ..core.errors import (
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
    ValidationError,
)
from ..core.models import (
    Bar, CapitalFlow, DataMeta, IndexQuote, MarketBreadth, OrderBook, OrderBookLevel,
    Quote, SectorQuote, Tick,
)
from . import level2
from . import symbols as sym
from .cache import DiskCache, make_key
from .csv_provider import CsvProvider
from .eastmoney import EastmoneyProvider
from .level2_import import Level2FileProvider
from .sample import SampleProvider
from .sina import SinaProvider
from .tencent import TencentProvider
from .ths import ThsProvider

__all__ = ["CompositeProvider"]

#: 本地数据来源（命中即视为离线降级）
LOCAL_SOURCES = ("cache", "csv", "sample")
#: 真实数据源名称
PROVIDER_LABELS = ("sina", "tencent", "eastmoney", "ths", "csv", "sample")
#: 降级链里出现的环节名（含内部兜底 ``cache`` 与本地导入 ``file``，供 sources() / describe() 展示）
CHAIN_LABELS = ("sina", "tencent", "eastmoney", "ths", "file", "cache", "csv", "sample")
#: 各类调用的默认优先级（mode 只把首选源提到最前，其余顺序不变）
CHAIN_PRIORITY = {
    "realtime": ["sina", "tencent", "eastmoney", "ths"],
    "history": ["eastmoney", "tencent", "sina", "ths"],
    "sectors": ["eastmoney", "tencent"],
    "breadth": ["eastmoney", "tencent"],
}

#: 盘口 / 逐笔专用链：**显式腾讯优先**（``CHAIN_PRIORITY["realtime"]`` 是新浪优先，
#: 但只有腾讯快照带外盘 / 内盘，也只有腾讯提供逐笔成交）；本地导入文件（``file``）只在
#: 导入目录里确实有文件时才插到链尾，避免给没有付费数据的用户刷 ``meta.notes``。
ORDERBOOK_CHAIN = ("tencent", "sina")
TICKS_CHAIN = ("tencent",)
#: 缓存反序列化时要还原的嵌套档位（字段名 → 元素 dataclass）
NESTED_MODELS = {"bids": OrderBookLevel, "asks": OrderBookLevel}

_MAX_NOTES = 8


def _brief(exc: Any, limit: int = 120) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    if isinstance(value, MarketBreadth):
        return value.total == 0 and value.total_amount_yi == 0
    return False


class CompositeProvider:
    """主源 + 缓存 + CSV + 示例数据的方法级降级组合。"""

    name = "composite"

    def __init__(
        self,
        settings: Optional[Settings] = None,
        providers: Optional[Dict[str, Any]] = None,
        mode: str = "auto",
    ):
        self.settings = settings if settings is not None else get_settings()
        self.offline = bool(getattr(self.settings, "offline", False))
        self.mode = mode if mode in ("auto", "sina", "tencent", "eastmoney") else "auto"
        if self.mode == "sina":
            self.name = "sina"
        elif self.mode == "eastmoney":
            self.name = "eastmoney"
        self.cache = DiskCache(getattr(self.settings, "cache_dir", ""))
        self.ttl = getattr(self.settings, "cache_ttl", None) or CacheTTL()
        self.last_meta = DataMeta()
        self.last_notes: List[str] = []
        #: 各源最近一次调用结果（供 describe / health 用）
        self._state: Dict[str, Dict[str, Any]] = {
            label: {"ok": None, "detail": "尚未调用"} for label in CHAIN_LABELS
        }
        self.providers: Dict[str, Any] = self._build_providers(providers)

    # ================================================================== 构造
    def _build_providers(self, overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """构造各数据源实例；单个源构造失败不影响其它源。"""
        overrides = overrides or {}
        out: Dict[str, Any] = {}
        factories = {
            "sina": SinaProvider,
            "tencent": TencentProvider,
            "eastmoney": EastmoneyProvider,
            "csv": CsvProvider,
            "sample": SampleProvider,
            "ths": ThsProvider,
            "file": lambda settings: Level2FileProvider(settings=settings),
        }
        for label, factory in factories.items():
            if label in overrides:
                out[label] = overrides[label]
                continue
            try:
                out[label] = factory(self.settings)
            except Exception as exc:      # pragma: no cover - 取决于运行环境
                out[label] = None
                self._state[label] = {"ok": False, "detail": "构造失败：%s" % _brief(exc)}
        return out

    # ================================================================== 降级链
    def _chain(self, kind: str = "realtime") -> List[str]:
        """按调用类型给出真实源优先级；``mode`` 只把首选源提到最前。"""
        base = list(CHAIN_PRIORITY.get(kind) or CHAIN_PRIORITY["realtime"])
        preferred = self.mode if self.mode in base else None
        if preferred:
            return [preferred] + [label for label in base if label != preferred]
        return base

    def _human_chain(self, kind: str = "realtime") -> List[str]:
        return self._chain(kind) + ["cache", "csv", "sample"]

    def _call(self, label: str, method: str, args: Tuple[Any, ...], kwargs: Dict[str, Any],
              notes: List[str]) -> Tuple[bool, Any]:
        provider = self.providers.get(label)
        if provider is None:
            return False, None
        func = getattr(provider, method, None)
        if func is None:
            return False, None
        try:
            value = func(*args, **kwargs)
        except ValidationError:
            raise
        except DataSourceError as exc:
            self._set_state(label, False, "%s: %s" % (type(exc).__name__, _brief(exc)))
            notes.append("%s.%s 失败：%s: %s" % (label, method, type(exc).__name__, _brief(exc)))
            return False, None
        except Exception as exc:          # 兜住第三方/未知异常，绝不让接口 500
            self._set_state(label, False, "%s: %s" % (type(exc).__name__, _brief(exc)))
            notes.append("%s.%s 异常：%s: %s" % (label, method, type(exc).__name__, _brief(exc)))
            return False, None
        if _is_empty(value):
            self._set_state(label, True, "返回空结果")
            notes.append("%s.%s 返回空结果" % (label, method))
            return False, None
        self._set_state(label, True, "正常")
        return True, value

    def _set_state(self, label: str, ok: bool, detail: str) -> None:
        self._state[label] = {"ok": bool(ok), "detail": detail,
                              "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def _through_primary(self, kind: str, method: str, args: Tuple[Any, ...],
                         notes: List[str]) -> Optional[Tuple[str, Any]]:
        """按调用类型走默认降级链（见 :data:`CHAIN_PRIORITY`）。"""
        return self._through_labels(self._chain(kind), method, args, notes)

    def _through_labels(self, labels: Sequence[str], method: str, args: Tuple[Any, ...],
                        notes: List[str]) -> Optional[Tuple[str, Any]]:
        """按给定顺序尝试真实源；全失败返回 ``None``（失败细节写入 notes）。"""
        for label in labels:
            ok, value = self._call(label, method, args, {}, notes)
            if ok:
                return label, value
        return None

    def _local_sources(self) -> List[str]:
        return [label for label in ("csv", "sample")]

    def _level2_labels(self, base: Sequence[str]) -> List[str]:
        """盘口/逐笔的候选源：仅在**导入目录里确实有文件**时把本地文件源插到链尾。

        这样没有付费数据的用户不会因为「文件不存在」在 ``meta.notes`` 里看到噪音，
        而有自己 Level-2 导出的用户在断网/离线时也能用起来（详见 docs/level2.md §4.2）。
        """
        labels = list(base)
        provider = self.providers.get("file")
        if provider is None:
            return labels
        try:
            if provider.health().get("ok"):
                labels.append("file")
        except Exception:                     # noqa: BLE001 - 自检失败就当没有导入文件
            pass
        return labels

    def capabilities(self) -> Dict[str, Any]:
        """本复合 provider 的盘口级能力 = 各源能力的并集（档数取最大，其余取任一）。

        服务层优先调用本方法，因此界面/MCP 看到的 ``orderbook_levels`` / ``import``
        会如实反映「链里有没有十档源、有没有本地导入通道」。
        """
        merged: Dict[str, Any] = {
            "orderbook": False, "orderbook_levels": 0, "ticks": False,
            "orders": False, "queue": False, "import": False, "detail": "",
        }
        parts: List[str] = []
        for label, provider in self.providers.items():
            if provider is None:
                continue
            caps = level2.capabilities(provider)
            if not caps.get("orderbook") and not caps.get("ticks"):
                continue
            merged["orderbook"] = merged["orderbook"] or bool(caps.get("orderbook"))
            merged["orderbook_levels"] = max(merged["orderbook_levels"],
                                             int(caps.get("orderbook_levels") or 0))
            merged["ticks"] = merged["ticks"] or bool(caps.get("ticks"))
            merged["orders"] = merged["orders"] or bool(caps.get("orders"))
            merged["queue"] = merged["queue"] or bool(caps.get("queue"))
            merged["import"] = merged["import"] or bool(caps.get("import"))
            parts.append("%s(%s)" % (label, caps.get("detail")))
        merged["detail"] = "；".join(parts) or "无盘口级能力"
        return merged

    # ================================================================== 缓存
    def _cache_write(self, key: str, value: Any, ttl: float) -> None:
        if isinstance(value, list):
            payload: Any = [item.to_dict() if hasattr(item, "to_dict") else item for item in value]
        elif hasattr(value, "to_dict"):
            payload = value.to_dict()
        else:
            payload = value
        self.cache.set(key, payload, ttl)

    @staticmethod
    def _rebuild_many(payload: Any, cls: Any) -> List[Any]:
        if payload is None:
            return []
        items = payload if isinstance(payload, list) else [payload]
        allowed = {field.name for field in dataclasses.fields(cls)}
        out = []
        for item in items:
            if isinstance(item, cls):
                out.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kwargs = {key: val for key, val in item.items() if key in allowed}
            for field_name, nested_cls in NESTED_MODELS.items():
                values = kwargs.get(field_name)
                if not isinstance(values, list):
                    continue
                nested = []
                for row in values:
                    if isinstance(row, nested_cls):
                        nested.append(row)
                    elif isinstance(row, dict):
                        try:
                            nested.append(nested_cls(**row))
                        except (TypeError, ValueError):
                            continue
                kwargs[field_name] = nested
            try:
                out.append(cls(**kwargs))
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    def _rebuild_one(cls, payload: Any, model: Any) -> Any:
        items = cls._rebuild_many(payload, model)
        return items[0] if items else None

    def _cache_read(self, key: str, cls: Any, notes: List[str]) -> Tuple[Optional[Any], bool]:
        """返回 ``(反序列化后的数据, 是否过期)``；未命中返回 ``(None, False)``。"""
        for stale in (False, True):
            raw = self.cache.get_stale(key) if stale else self.cache.get(key)
            if raw is None:
                continue
            items = self._rebuild_many(raw, cls)
            if items:
                notes.append("命中%s磁盘缓存" % ("过期" if stale else "未过期"))
                return items, stale
        return None, False

    # ================================================================== meta
    def _finish(self, source: str, started: float, notes: List[str], stale: bool = False) -> None:
        offline = self.offline or source in LOCAL_SOURCES
        self.last_notes = notes[-_MAX_NOTES:]
        self.last_meta = DataMeta(
            source=source,
            stale=bool(stale),
            offline=bool(offline),
            as_of=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            latency_ms=int((time.time() - started) * 1000),
            notes=list(self.last_notes),
        )

    def _no_data(self, what: str, notes: List[str], started: float) -> None:
        self._finish("unavailable", started, notes)
        raise ProviderUnavailable(
            "%s 数据不可用：%s" % (what, "；".join(self.last_notes) or "无可用数据源")
        )

    # ================================================================== 快照
    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照（sina → eastmoney → 缓存 → CSV → 示例）。"""
        started = time.time()
        notes: List[str] = []
        codes = self._normalize(symbols)
        if not codes:
            self._finish("mixed", started, ["未提供有效标的代码"])
            return []
        hit = self._through_primary("realtime", "latest_quotes", (codes,), notes)
        if hit is not None:
            label, value = hit
            # 用腾讯补齐主源缺失的估值字段（PE/PB/总市值/量比/换手率），主源已给全则不发请求
            value = self._enrich_quotes(value, notes)
            self._cache_write(self._key("quotes", codes), value, self.ttl.quotes)
            self._finish(label, started, notes)
            return value
        cached, stale = self._cache_read(self._key("quotes", codes), Quote, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached
        for label in self._local_sources():
            ok, value = self._call(label, "latest_quotes", (codes,), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("实时行情", notes, started)

    def _enrich_quotes(self, quotes: List[Quote], notes: List[str]) -> List[Quote]:
        """补齐快照中缺失的估值字段。

        新浪实时快照不提供 PE / PB / 总市值 / 量比（换手率也常为 0），而腾讯同一时刻的
        ``qt.gtimg.cn`` 快照提供这些字段；这里只在**确有缺失**时向腾讯发一次补充请求，
        避免主源已给全时产生多余流量。补充成功会在 ``meta.notes`` 中标注。
        """
        missing = [q.code for q in quotes
                   if q.pe_ttm is None or q.pb is None or q.market_cap_yi is None
                   or q.volume_ratio is None or not q.turnover_pct]
        if not missing:
            return quotes
        ok, extra = self._call("tencent", "latest_quotes", (missing,), {}, notes)
        if not ok or not extra:
            return quotes
        patch = {q.code: q for q in extra}
        filled = 0
        for quote in quotes:
            source = patch.get(quote.code)
            if source is None:
                continue
            if quote.pe_ttm is None and source.pe_ttm is not None:
                quote.pe_ttm = source.pe_ttm
                filled += 1
            if quote.pb is None and source.pb is not None:
                quote.pb = source.pb
            if quote.market_cap_yi is None and source.market_cap_yi is not None:
                quote.market_cap_yi = source.market_cap_yi
            if quote.volume_ratio is None and source.volume_ratio is not None:
                quote.volume_ratio = source.volume_ratio
            if not quote.turnover_pct and source.turnover_pct:
                quote.turnover_pct = source.turnover_pct
        if filled:
            notes.append("估值字段（PE/PB/总市值/量比/换手率）由腾讯快照补充")
        return quotes

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照（sina → eastmoney → 缓存 → CSV → 示例）。"""
        started = time.time()
        notes: List[str] = []
        codes = self._normalize(symbols)
        if not codes:
            self._finish("mixed", started, ["未提供有效标的代码"])
            return []
        hit = self._through_primary("realtime", "index_quotes", (codes,), notes)
        if hit is not None:
            label, value = hit
            self._cache_write(self._key("indices", codes), value, self.ttl.quotes)
            self._finish(label, started, notes)
            return value
        cached, stale = self._cache_read(self._key("indices", codes), IndexQuote, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached
        for label in self._local_sources():
            ok, value = self._call(label, "index_quotes", (codes,), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("指数行情", notes, started)

    # ================================================================== K 线
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        """历史 K 线（eastmoney → 缓存 → CSV → 示例）。"""
        started = time.time()
        notes: List[str] = []
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound as exc:
            self._finish("unavailable", started, [str(exc)])
            raise SymbolNotFound("无法识别的标的代码：%r" % (symbol,))
        try:
            limit = max(1, int(days))
        except (TypeError, ValueError):
            raise ValidationError("days 需为整数，当前为 %r" % (days,), field="days")
        freq_key = str(freq or "day").strip().lower()
        adjust_key = str(adjust or "qfq").strip().lower()
        if freq_key not in ("day", "week", "month"):
            raise ValidationError("K 线周期仅支持 day/week/month，当前为 %r" % (freq,), field="freq")
        if adjust_key not in ("qfq", "hfq", "none"):
            raise ValidationError("复权方式仅支持 qfq/hfq/none，当前为 %r" % (adjust,), field="adjust")

        key = self._key("kline", [code, limit, freq_key, adjust_key])
        hit = self._through_primary("history", "kline", (code, limit, freq_key, adjust_key), notes)
        if hit is not None:
            label, bars = hit
            self._cache_write(key, bars, self._kline_ttl(bars))
            self._finish(label, started, notes)
            return bars
        cached, stale = self._cache_read(key, Bar, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached
        for label in self._local_sources():
            ok, bars = self._call(label, "kline", (code, limit, freq_key, adjust_key), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return bars
        self._no_data("K 线", notes, started)

    def _kline_ttl(self, bars: List[Bar]) -> float:
        """最后一根是当日（可能还在变）→ 短缓存；否则长缓存（TTL 取自 settings.cache_ttl）。"""
        today = datetime.date.today().isoformat()
        if bars and str(bars[-1].date)[:10] == today:
            return self.ttl.kline_today
        return self.ttl.kline_closed

    # ============================================================== 盘口 / 逐笔
    def orderbook(self, code: str) -> OrderBook:
        """五档盘口（tencent → sina → 缓存 → CSV → 示例；腾讯带外盘 / 内盘）。"""
        started = time.time()
        notes: List[str] = []
        try:
            norm = sym.normalize(code)
        except SymbolNotFound:
            self._finish("unavailable", started, ["无法识别的标的代码：%r" % (code,)])
            raise SymbolNotFound("无法识别的标的代码：%r" % (code,))
        key = self._key("orderbook", [norm])
        hit = self._through_labels(self._level2_labels(ORDERBOOK_CHAIN), "orderbook", (norm,), notes)
        if hit is not None:
            label, book = hit
            self._cache_write(key, book, self.ttl.quotes)
            self._finish(label, started, notes)
            return book
        cached, stale = self._cache_read(key, OrderBook, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached[0]
        for label in self._local_sources():
            ok, value = self._call(label, "orderbook", (norm,), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("盘口", notes, started)

    def ticks(self, code: str, limit: int = 600) -> List[Tick]:
        """逐笔成交（tencent → sina → 缓存 → CSV → 示例），按时间升序、最多 ``limit`` 条。"""
        started = time.time()
        notes: List[str] = []
        try:
            norm = sym.normalize(code)
        except SymbolNotFound:
            self._finish("unavailable", started, ["无法识别的标的代码：%r" % (code,)])
            raise SymbolNotFound("无法识别的标的代码：%r" % (code,))
        try:
            top = int(limit)
        except (TypeError, ValueError):
            raise ValidationError("limit 需为整数，当前为 %r" % (limit,), field="limit")
        top = max(1, top)
        key = self._key("ticks", [norm, top])
        hit = self._through_labels(self._level2_labels(TICKS_CHAIN), "ticks", (norm, top), notes)
        if hit is not None:
            label, ticks = hit
            self._cache_write(key, ticks, self.ttl.quotes)
            self._finish(label, started, notes)
            return ticks
        cached, stale = self._cache_read(key, Tick, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached
        for label in self._local_sources():
            ok, value = self._call(label, "ticks", (norm, top), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("逐笔成交", notes, started)

    def capital_flow(self, code: str, limit: int = 2000) -> CapitalFlow:
        """资金流：由逐笔成交按**单笔成交额分档**自算（口径见 ``data/level2.py``）。

        名称取 :meth:`resolve_name`，时间戳取最新一条逐笔；逐笔不可用时抛
        :class:`DataSourceError`（``ProviderUnavailable`` 是其子类）。
        """
        started = time.time()
        notes: List[str] = ["按逐笔单笔成交额分档自算资金流（口径见 data/level2.py）"]
        try:
            norm = sym.normalize(code)
        except SymbolNotFound:
            self._finish("unavailable", started, ["无法识别的标的代码：%r" % (code,)])
            raise SymbolNotFound("无法识别的标的代码：%r" % (code,))
        ticks = self.ticks(norm, limit)
        if not ticks:
            self._no_data("资金流", notes, started)
        source = self.last_meta.source
        name = self.resolve_name(norm) or ""
        flow = level2.compute_capital_flow(
            ticks, code=norm, name=name, ts=ticks[-1].time, source=source,
        )
        self._finish(source or "mixed", started, notes)
        return flow

    # ================================================================== 板块 / 广度
    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """行业板块（sina→eastmoney→缓存→CSV→示例，sina 不提供板块会自动跳过）。"""
        started = time.time()
        notes: List[str] = []
        try:
            top = max(1, int(limit))
        except (TypeError, ValueError):
            top = 20
        hit = self._through_primary("sectors", "sectors", (top,), notes)
        if hit is not None:
            label, value = hit
            self._cache_write(self._key("sectors", [top]), value, self.ttl.sectors)
            self._finish(label, started, notes)
            return value
        cached, stale = self._cache_read(self._key("sectors", [top]), SectorQuote, notes)
        if cached:
            self._finish("cache", started, notes, stale=stale)
            return cached
        for label in self._local_sources():
            ok, value = self._call(label, "sectors", (top,), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("板块", notes, started)

    def breadth(self) -> MarketBreadth:
        """市场广度（sina→eastmoney→缓存→CSV→示例）。"""
        started = time.time()
        notes: List[str] = []
        key = self._key("breadth", [])
        hit = self._through_primary("breadth", "breadth", (), notes)
        if hit is not None:
            label, value = hit
            self._cache_write(key, value, self.ttl.breadth)
            provider = self.providers.get(label)
            for extra in getattr(provider, "last_notes", None) or []:
                notes.append(extra)
            self._finish(label, started, notes)
            return value
        raw = self.cache.get(key)
        stale = False
        if raw is None:
            raw = self.cache.get_stale(key)
            stale = raw is not None
        if isinstance(raw, dict):
            value = self._rebuild_one(raw, MarketBreadth)
            if value is not None and not _is_empty(value):
                notes.append("命中%s磁盘缓存" % ("过期" if stale else "未过期"))
                self._finish("cache", started, notes, stale=stale)
                return value
        for label in self._local_sources():
            ok, value = self._call(label, "breadth", (), {}, notes)
            if ok:
                self._finish(label, started, notes)
                return value
        self._no_data("市场广度", notes, started)

    # ================================================================== 名称
    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（指数表 → 缓存 → sina → eastmoney → CSV → 示例；失败返回 None）。"""
        started = time.time()
        notes: List[str] = []
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            self._finish("mixed", started, ["无法识别的标的代码：%r" % (symbol,)])
            return None
        known = sym.INDEX_NAMES.get(code)
        if known:
            self._finish("symbols", started, ["命中内置指数表"])
            return known
        key = self._key("name", [code])
        cached = self.cache.get(key)
        if cached:
            notes.append("命中未过期名称缓存")
            self._finish("cache", started, notes)
            return str(cached)
        for label in self._chain("realtime"):
            ok, value = self._call(label, "resolve_name", (code,), {}, notes)
            if ok and value:
                self.cache.set(key, str(value), self.ttl.name)
                self._finish(label, started, notes)
                return str(value)
        for label in self._local_sources():
            ok, value = self._call(label, "resolve_name", (code,), {}, notes)
            if ok and value:
                self._finish(label, started, notes)
                return str(value)
        self._finish("mixed", started, notes)
        return None

    # ================================================================== 可用性
    def _cache_report(self) -> Dict[str, Any]:
        """磁盘缓存的如实描述（它是**内部兜底层**，不是 Provider）。"""
        stats = self.cache.stats()
        return {
            "name": "cache",
            "kind": "internal",
            "role": "磁盘缓存（内部兜底层：网络失败时返回最近一次成功的数据）",
            "enabled": bool(stats.get("enabled")),
            "entries": int(stats.get("entries") or 0),
            "hits": int(stats.get("hits") or 0),
            "stale_hits": int(stats.get("stale_hits") or 0),
            "misses": int(stats.get("misses") or 0),
            "writes": int(stats.get("writes") or 0),
            "dir": stats.get("dir", ""),
        }

    def describe(self, probe: bool = False) -> Dict[str, Any]:
        """各源可用性描述（``probe=True`` 时对每个真实源做一次轻量自检）。

        ``cache`` 是内部兜底层，单独用 ``kind="internal"`` + entries/hits 描述，
        不会（也不应）被报告成「构造失败的 Provider」。
        """
        sources: Dict[str, Any] = {}
        for label in CHAIN_LABELS:
            if label == "cache":
                sources[label] = self._cache_report()
                continue
            provider = self.providers.get(label)
            state = dict(self._state.get(label) or {})
            entry = {
                "name": label,
                "kind": "provider",
                "available": provider is not None,
                "last_ok": state.get("ok"),
                "detail": state.get("detail", ""),
                "checked_at": state.get("ts", ""),
            }
            if probe:
                if provider is None:
                    entry["health"] = {"ok": False, "detail": "该源不可用（构造失败）", "latency_ms": 0}
                else:
                    try:
                        entry["health"] = provider.health()
                    except Exception as exc:       # pragma: no cover
                        entry["health"] = {"ok": False, "detail": str(exc), "latency_ms": 0}
            sources[label] = entry
        return {
            "provider": self.name,
            "mode": self.mode,
            "data_source": getattr(self.settings, "data_source", ""),
            "offline": self.offline,
            "realtime_chain": self._human_chain("realtime"),
            "history_chain": self._human_chain("history"),
            "sectors_chain": self._human_chain("sectors"),
            "breadth_chain": self._human_chain("breadth"),
            "sources": sources,
            "cache": self.cache.stats(),
            "last_meta": self.last_meta.to_dict(),
        }


    def sources(self) -> List[str]:
        """降级链上的数据源名称（按优先级）。"""
        return list(CHAIN_LABELS)

    def health(self) -> Dict[str, Any]:
        """自检：汇总各源可用性（示例数据源永远可用，因此降级链末端始终有兜底）。"""
        started = time.time()
        described = self.describe(probe=True)
        sources = described["sources"]
        primary_ok = [name for name in ("sina", "tencent", "eastmoney")
                      if (sources.get(name) or {}).get("health", {}).get("ok")]
        local_ok = [name for name in ("csv", "sample")
                    if (sources.get(name) or {}).get("health", {}).get("ok")]
        cache = described["sources"].get("cache") or {}
        cache_bits = "磁盘缓存 enabled=%s entries=%s hits=%s stale_hits=%s" % (
            cache.get("enabled"), cache.get("entries"), cache.get("hits"), cache.get("stale_hits"),
        )
        if self.offline:
            detail = ("离线模式（QUANTSTUDIO_OFFLINE=1）：仅使用缓存 / CSV / 示例数据；"
                      "本地可用源：%s；%s") % (", ".join(local_ok) or "无", cache_bits)
            ok = bool(local_ok)
        elif primary_ok:
            detail = "真实行情源可用：%s；%s" % (", ".join(primary_ok), cache_bits)
            ok = True
        else:
            detail = "真实行情源不可用，将降级为本地数据（%s）；%s" % (
                ", ".join(local_ok) or "无本地数据", cache_bits)
            ok = bool(local_ok)
        return {
            "ok": ok,
            "detail": detail,
            "latency_ms": int((time.time() - started) * 1000),
            "source": self.name,
            "mode": self.mode,
            "offline": self.offline,
            "sources": sources,
            "cache": described["cache"],
            "cache_report": cache,
        }

    # ================================================================== 内部
    @staticmethod
    def _key(namespace: str, args: Sequence[Any]) -> str:
        return make_key(namespace, list(args))

    @staticmethod
    def _normalize(symbols_in: Sequence[str]) -> List[str]:
        out: List[str] = []
        for item in symbols_in or []:
            try:
                code = sym.normalize(item)
            except SymbolNotFound:
                continue
            if code not in out:
                out.append(code)
        return out

