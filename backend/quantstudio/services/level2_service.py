# -*- coding: utf-8 -*-
"""盘口 / 逐笔 / 资金流业务服务（``Level2Service``）。

把数据层的 ``orderbook()`` / ``ticks()`` / ``capital_flow()``（duck-typing）包装成
Web / 桌面 / MCP 共用的 JSON 契约（形状见 ``docs/level2.md`` §3.2）：

- **短 TTL 缓存**：盘口 2 秒、逐笔 10 秒（行情快照，缓存太久会误导）；构造时可传
  ``ttl_orderbook`` / ``ttl_ticks``（<= 0 表示不缓存，便于测试）；命中缓存时在
  ``meta.notes`` 里注明。
- **口径**：档位数量单位为**手**；逐笔方向 ``B/S/M`` 是第三方「盘口方向标记」，
  不是交易所 Level-2 的主动买卖判定；逐笔接口约覆盖最近 4000 笔。
- **失败不冒泡成 500**：数据源不支持 / 离线无数据 / 解析为空一律抛
  ``core.errors`` 业务异常（``DataSourceError`` / ``ProviderUnavailable``），
  由接口层映射 502、由 MCP 工具转成 ``isError`` 结果。
"""

import math
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.errors import DataSourceError, ProviderUnavailable
from ..data.level2 import (
    capabilities,
    compute_capital_flow,
    flow_to_dict,
    orderbook_summary,
    summarize_ticks,
)
from .common import (
    cached,
    get_data_provider,
    normalize_code,
    now_str,
    parse_int,
    provider_meta,
    to_dict,
)

#: 免费逐笔源（腾讯）单页约 600 笔、整段约覆盖最近 4000 笔
TICK_PAGE_SIZE = 600
#: 一次最多拉取的逐笔条数（源侧的覆盖上限）
MAX_TICKS = 4000
#: 逐笔接口默认样本条数
DEFAULT_TICKS_LIMIT = 120
#: 资金流默认样本条数
DEFAULT_FLOW_LIMIT = 2000

#: 盘口 / 逐笔的默认短 TTL（秒）
DEFAULT_ORDERBOOK_TTL = 2.0
DEFAULT_TICKS_TTL = 10.0

#: 逐笔契约里的固定口径说明（前端 / MCP 文案共用）
TICKS_NOTE = ("该接口约覆盖最近 4000 笔（分页拉取）；方向 B/S/M 是第三方「盘口方向标记」，"
              "不是交易所 Level-2 的主动买卖判定，请与外盘 / 内盘交叉参考。")


def _append_note(notes: List[str], text: Any) -> None:
    """去重追加一条 meta note（空值忽略）。"""
    value = str(text or "").strip()
    if value and value not in notes:
        notes.append(value)


class Level2Service:
    """盘口 / 逐笔 / 资金流业务（只做取数、缓存与结构化成 JSON）。"""

    def __init__(
        self,
        provider: Any = None,
        settings: Optional[Settings] = None,
        ttl_orderbook: float = DEFAULT_ORDERBOOK_TTL,
        ttl_ticks: float = DEFAULT_TICKS_TTL,
    ):
        self.settings = settings or get_settings()
        self._provider = provider
        self.ttl_orderbook = float(ttl_orderbook)
        self.ttl_ticks = float(ttl_ticks)

    # ------------------------------------------------------------------ 基础设施

    @property
    def provider(self):
        """惰性构造数据源（避免导入期访问网络；测试可注入替身）。"""
        if self._provider is None:
            self._provider = get_data_provider(self.settings)
        return self._provider

    def capabilities(self) -> Dict[str, Any]:
        """数据源「盘口级」能力协商。

        复合 provider（``CompositeProvider``）与本地文件源都有自己的 ``capabilities()``
        （前者是各源并集，后者声明十档 + 导入），优先用它；否则退回
        :func:`quantstudio.data.level2.capabilities` 的 duck-typing 推断。
        """
        own = getattr(self.provider, "capabilities", None)
        if callable(own):
            try:
                return dict(own())
            except Exception:                         # noqa: BLE001 - 自检失败退回推断
                pass
        return capabilities(self.provider)

    def is_offline(self) -> bool:
        """离线判定 = ``settings.offline`` 或数据源自报 ``offline``。"""
        if bool(getattr(self.settings, "offline", False)):
            return True
        try:
            return bool(provider_meta(self.provider).offline)
        except Exception:                             # noqa: BLE001 - 状态类判断不应抛错
            return False

    def meta(
        self,
        cache_hit: bool = False,
        ttl: Optional[float] = None,
        notes: Optional[List[str]] = None,
        source: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构造契约里的 ``meta``（沿用 ``DataMeta`` 的口径与字段）。

        固定包含 ``source / stale / offline / as_of / notes``（另带既有的 ``latency_ms``）；
        缓存命中、离线模式、调用方补充说明都写进 ``notes``。
        """
        raw = provider_meta(self.provider).to_dict()
        payload: Dict[str, Any] = {
            "source": str(source or raw.get("source") or "unknown"),
            "stale": bool(raw.get("stale")),
            "offline": bool(raw.get("offline") or getattr(self.settings, "offline", False)),
            "as_of": str(as_of or raw.get("as_of") or now_str()),
            "latency_ms": int(raw.get("latency_ms") or 0),
            "notes": [],
        }
        for item in list(raw.get("notes") or []):
            _append_note(payload["notes"], item)
        if getattr(self.settings, "offline", False) and not raw.get("offline"):
            _append_note(payload["notes"], "离线模式（QUANTSTUDIO_OFFLINE=1）：未发起网络请求")
        if cache_hit:
            _append_note(payload["notes"], "命中 %.0f 秒短缓存（快照可能滞后）" % float(ttl or 0.0))
        for item in list(notes or []):
            _append_note(payload["notes"], item)
        return payload

    # ------------------------------------------------------------------ 盘口

    def orderbook(self, code: str) -> Dict[str, Any]:
        """五档盘口快照 + 派生指标 + 能力协商（契约 JSON）。"""
        code = normalize_code(code)
        caps = self.capabilities()
        if not caps.get("orderbook"):
            raise self._unsupported("五档盘口", caps)

        payload, hit = self._cached(
            "level2:orderbook:%s" % code,
            self.ttl_orderbook,
            lambda: self._load_orderbook(code),
        )
        if not payload:
            raise self._no_data("五档盘口", code)

        result = dict(payload)
        result["code"] = str(result.get("code") or code)
        result["name"] = str(result.get("name") or self._name(code) or "")
        result["bids"] = list(result.get("bids") or [])
        result["asks"] = list(result.get("asks") or [])
        result["capabilities"] = caps
        result["meta"] = self.meta(cache_hit=hit, ttl=self.ttl_orderbook)
        return result

    # ------------------------------------------------------------------ 逐笔

    def ticks(self, code: str, limit: int = DEFAULT_TICKS_LIMIT) -> Dict[str, Any]:
        """逐笔成交（升序）+ 多空统计（契约 JSON）。"""
        code = normalize_code(code)
        limit = parse_int(
            limit, "limit", default=DEFAULT_TICKS_LIMIT, minimum=1, maximum=MAX_TICKS, clamp=True
        )
        caps = self.capabilities()
        if not caps.get("ticks"):
            raise self._unsupported("逐笔成交", caps)

        payload, hit = self._cached(
            "level2:ticks:%s:%d" % (code, limit),
            self.ttl_ticks,
            lambda: self._load_ticks(code, limit),
        )
        items = list(payload.get("items") or [])
        stats = dict(payload.get("stats") or summarize_ticks([]))
        note = TICKS_NOTE
        if not items:
            note = TICKS_NOTE + " 本次没有取到逐笔（可能尚未开盘、停牌或数据源限流）。"
        pages = int(math.ceil(len(items) / float(TICK_PAGE_SIZE))) if items else 0
        return {
            "code": code,
            "name": str(self._name(code) or ""),
            "count": len(items),
            "shown": len(items),
            "items": items,
            "stats": stats,
            "pages": pages,
            "note": note,
            "meta": self.meta(cache_hit=hit, ttl=self.ttl_ticks),
        }

    # ------------------------------------------------------------------ 资金流

    def capital_flow(self, code: str, limit: int = DEFAULT_FLOW_LIMIT) -> Dict[str, Any]:
        """由逐笔样本自算的资金流（分档买卖额、主力净额，契约 JSON）。"""
        code = normalize_code(code)
        limit = parse_int(
            limit, "limit", default=DEFAULT_FLOW_LIMIT, minimum=1, maximum=MAX_TICKS, clamp=True
        )
        caps = self.capabilities()
        has_flow = callable(getattr(self.provider, "capital_flow", None))
        if not has_flow and not caps.get("ticks"):
            raise self._unsupported("逐笔成交（资金流依赖逐笔）", caps)

        payload, hit = self._cached(
            "level2:flow:%s:%d" % (code, limit),
            self.ttl_ticks,
            lambda: self._load_flow(code, limit),
        )
        if not payload or not payload.get("tick_count"):
            raise self._no_data("逐笔成交（资金流样本）", code)

        meta = self.meta(cache_hit=hit, ttl=self.ttl_ticks)
        result = dict(payload)
        result["code"] = str(result.get("code") or code)
        result["name"] = str(result.get("name") or self._name(code) or "")
        result["ts"] = str(result.get("ts") or meta.get("as_of") or "")
        result["source"] = str(result.get("source") or meta.get("source") or "")
        result["meta"] = meta
        return result

    # ------------------------------------------------------------------ 内部：取数

    def _load_orderbook(self, code: str) -> Optional[Dict[str, Any]]:
        book = self.provider.orderbook(code)
        if book is None:
            return None
        payload = to_dict(book)
        if not (payload.get("bids") or payload.get("asks")):
            return None
        payload["summary"] = orderbook_summary(book)
        return payload

    def _load_ticks(self, code: str, limit: int) -> Dict[str, Any]:
        rows = list(self.provider.ticks(code, limit=limit) or [])
        return {
            "items": [to_dict(tick) for tick in rows],
            "stats": summarize_ticks(rows),
        }

    def _load_flow(self, code: str, limit: int) -> Optional[Dict[str, Any]]:
        provider = self.provider
        if callable(getattr(provider, "capital_flow", None)):
            flow = provider.capital_flow(code, limit=limit)
            return flow_to_dict(flow) if flow is not None else None
        # 降级：源只有逐笔能力时，服务层按同一口径自算
        rows = list(provider.ticks(code, limit=limit) or [])
        if not rows:
            return None
        flow = compute_capital_flow(
            rows, code=code, name=self._name(code) or "", ts="", source=self._source()
        )
        return flow_to_dict(flow)

    def _cached(self, key: str, ttl: float, loader) -> Any:
        """进程内短 TTL 缓存，返回 ``(值, 是否命中缓存)``（复用 ``services.common.cached``）。"""
        touched = []

        def wrapped():
            value = loader()
            touched.append(1)
            return value

        return cached(key, ttl, wrapped), not touched

    # ------------------------------------------------------------------ 内部：异常与杂项

    def _source(self) -> str:
        try:
            name = provider_meta(self.provider).source
        except Exception:                             # noqa: BLE001
            name = ""
        return str(name or getattr(self.provider, "name", "") or "unknown")

    def _name(self, code: str) -> str:
        resolver = getattr(self.provider, "resolve_name", None)
        if not callable(resolver):
            return ""
        try:
            return str(resolver(code) or "")
        except Exception:                             # noqa: BLE001 - 名称拿不到不影响数据
            return ""

    def _unsupported(self, what: str, caps: Dict[str, Any]) -> DataSourceError:
        message = "当前数据源（%s）不支持%s：%s。" % (
            self._source(), what, caps.get("detail") or "无盘口级能力",
        )
        if self.is_offline():
            message += "（当前为离线模式，不会发起网络请求）"
        return DataSourceError(message)

    def _no_data(self, what: str, code: str) -> DataSourceError:
        if self.is_offline():
            return ProviderUnavailable(
                "离线模式下没有取到 %s 的%s数据：未发起网络请求，本地缓存 / 导入通道也没有命中。"
                % (code, what)
            )
        return DataSourceError(
            "%s 没有取到%s数据：数据源可能限流、标的停牌或接口变更，请稍后重试。"
            % (code, what)
        )


__all__ = [
    "Level2Service",
    "MAX_TICKS",
    "TICK_PAGE_SIZE",
    "DEFAULT_TICKS_LIMIT",
    "DEFAULT_FLOW_LIMIT",
    "DEFAULT_ORDERBOOK_TTL",
    "DEFAULT_TICKS_TTL",
    "TICKS_NOTE",
]
