# -*- coding: utf-8 -*-
"""行情业务服务：指数/广度/板块/个股快照/K 线/自选池/系统状态。

只做「取数 + 组装 + 缓存 + 校验」，不涉及任何 HTTP 细节；
数据源异常按 ``core.errors`` 抛出，由接口层映射状态码。
"""

from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.errors import SymbolNotFound
from ..core.models import DataMeta
from .common import (
    cached,
    get_data_provider,
    normalize_code,
    normalize_codes,
    provider_meta,
    read_json,
    to_dict,
    trading_calendar,
    write_json_atomic,
)


class MarketService:
    """行情看板相关业务。"""

    def __init__(self, provider: Any = None, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._provider = provider

    # ------------------------------------------------------------------ 基础设施

    @property
    def provider(self):
        """惰性构造数据源（避免导入期访问网络；测试可注入替身）。"""
        if self._provider is None:
            self._provider = get_data_provider(self.settings)
        return self._provider

    def meta(self) -> DataMeta:
        return provider_meta(self.provider)

    @property
    def calendar(self):
        return trading_calendar(self.settings)

    # ------------------------------------------------------------------ 行情

    def overview(self) -> Dict[str, Any]:
        ttl = self.settings.cache_ttl
        breadth = cached(
            "market:breadth",
            ttl.breadth,
            lambda: to_dict(self.provider.breadth()),
        )
        indices = cached(
            "market:indices",
            ttl.quotes,
            lambda: [to_dict(x) for x in self.provider.index_quotes(self.settings.index_symbols)],
        )
        return {
            "indices": indices,
            "breadth": breadth,
            "total_amount_yi": breadth.get("total_amount_yi", 0.0) or 0.0,
            "main_net_inflow_yi": breadth.get("main_net_inflow_yi"),
            "north_net_inflow_yi": breadth.get("north_net_inflow_yi"),
        }

    def sectors(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if limit is None:
            limit = self.settings.sector_limit
        rows = cached(
            "market:sectors:%d" % limit,
            self.settings.cache_ttl.sectors,
            lambda: [to_dict(x) for x in self.provider.sectors(limit)],
        )
        ordered = sorted(rows, key=lambda item: item.get("change_pct") or 0.0, reverse=True)
        return ordered[:limit]

    def quotes(
        self, codes: Optional[List[str]] = None, strict: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """codes=None 表示自选池（缺失标的静默跳过）；显式 codes 缺失默认 404。"""
        if strict is None:
            strict = codes is not None
        if codes is None:
            codes = self.watchlist()
        codes = normalize_codes(codes)
        if not codes:
            return []

        def load():
            try:
                return [to_dict(x) for x in self.provider.latest_quotes(codes)]
            except SymbolNotFound:
                if strict:
                    raise
                # 自选池降级：逐个取数，跳过数据源不认识的标的
                rows = []
                for code in codes:
                    try:
                        rows.extend(to_dict(x) for x in self.provider.latest_quotes([code]))
                    except Exception:
                        continue
                return rows

        rows = cached(
            "market:quotes:" + ",".join(codes),
            self.settings.cache_ttl.quotes,
            load,
        )
        by_code = {}
        for row in rows:
            by_code[row.get("code")] = row
        missing = [code for code in codes if code not in by_code]
        if missing and strict:
            raise SymbolNotFound("未找到标的：%s" % ", ".join(missing))
        return [by_code[code] for code in codes if code in by_code]

    def kline(
        self,
        code: str,
        days: Optional[int] = None,
        freq: str = "day",
        adjust: str = "qfq",
    ) -> List[Dict[str, Any]]:
        code = normalize_code(code)
        if days is None:
            days = self.settings.default_kline_days
        key = "market:kline:%s:%d:%s:%s" % (code, days, freq, adjust)
        ttl = (
            self.settings.cache_ttl.kline_closed
            if self.calendar.is_closed()
            else self.settings.cache_ttl.kline_today
        )
        rows = cached(
            key,
            ttl,
            lambda: [to_dict(x) for x in self.provider.kline(code, days=days, freq=freq, adjust=adjust)],
        )
        if not rows:
            raise SymbolNotFound("未找到标的 %s 的 K 线数据" % code)
        return rows

    # ------------------------------------------------------------------ 自选池

    def watchlist(self) -> List[str]:
        """读取自选池；文件不存在时用 settings.default_watchlist 初始化（原子写）。"""
        path = self.settings.watchlist_path
        raw = read_json(path, default=None)
        if raw is None:
            codes = normalize_codes(list(self.settings.default_watchlist), field="watchlist")
            self._save_watchlist(codes)
            return codes
        items = raw.get("codes") if isinstance(raw, dict) else raw
        codes: List[str] = []
        for item in items or []:
            try:
                code = normalize_code(item, field="watchlist")
            except Exception:
                continue  # 手工编辑损坏的条目直接忽略，不影响服务启动
            if code not in codes:
                codes.append(code)
        return codes

    def _save_watchlist(self, codes: List[str]) -> None:
        write_json_atomic(self.settings.watchlist_path, {"codes": list(codes)})

    def add_to_watchlist(self, code: str) -> List[str]:
        code = normalize_code(code)
        codes = self.watchlist()
        if code not in codes:
            codes.append(code)
            self._save_watchlist(codes)
        return codes

    def remove_from_watchlist(self, code: str) -> List[str]:
        code = normalize_code(code)
        codes = self.watchlist()
        if code in codes:
            codes = [item for item in codes if item != code]
            self._save_watchlist(codes)
        return codes

    def watchlist_quotes(self) -> Dict[str, Any]:
        codes = self.watchlist()
        return {"codes": codes, "items": self.quotes(codes, strict=False) if codes else []}

    # ------------------------------------------------------------------ 系统状态

    def _provider_info(self) -> Dict[str, Any]:
        try:
            provider = self.provider
        except Exception as exc:  # 数据源构造失败也要能返回状态
            return {"active": "unavailable", "sources": [], "available": [], "error": str(exc)}
        active = getattr(provider, "name", "") or "unknown"
        sources: List[Any] = []
        try:
            sources = list(provider.sources())
        except Exception:
            sources = [active]
        available: List[Any] = []
        try:
            desc = provider.describe()
            if isinstance(desc, dict):
                raw = desc.get("available")
                if isinstance(raw, (list, tuple)):
                    available = list(raw)
                elif isinstance(desc.get("sources"), (list, tuple)):
                    available = list(desc["sources"])
        except Exception:
            available = []
        if not available:
            available = list(sources)
        return {"active": active, "sources": sources, "available": available}

    def _local_info(self) -> Dict[str, Any]:
        """本地策略目录状态（strategies/local/ 加载了什么、哪些文件失败）。"""
        try:
            from ..strategies import local_status

            status = local_status()
        except Exception as exc:  # noqa: BLE001 - 状态接口不应因为策略目录异常而失败
            return {"dir": "", "loaded": [], "errors": [{"file": "-", "error": str(exc)}]}
        return {
            "dir": status.get("dir") or "",
            "loaded": list(status.get("loaded") or []),
            "errors": list(status.get("errors") or []),
        }

    def _strategies_info(self) -> Dict[str, Any]:
        """策略数量统计（按 origin 区分：代码策略 / 界面新建的自定义实例）。"""
        try:
            from ..strategies import list_specs

            specs = list_specs() or []
        except Exception as exc:  # noqa: BLE001
            return {"total": 0, "builtin": 0, "local": 0, "error": str(exc)}
        counts = {"total": len(specs), "builtin": 0, "local": 0}
        for spec in specs:
            origin = getattr(spec, "origin", "builtin")
            if origin in counts:
                counts[origin] += 1
        return counts

    def _trade_info(self) -> Dict[str, Any]:
        try:
            from ..trading import available_brokers

            brokers = available_brokers()
        except Exception as exc:
            return {"mode": "paper", "brokers": {}, "error": str(exc)}
        if isinstance(brokers, dict):
            normalized = dict(brokers)
        else:
            normalized = {}
            for item in brokers or []:
                if isinstance(item, dict):
                    key = item.get("id") or item.get("name") or "unknown"
                    normalized[key] = item
                else:
                    name = str(item)
                    normalized[name] = {"name": name, "is_live": False}
        return {"mode": "paper", "brokers": normalized}

    def mode(self) -> str:
        """real / cache / offline（前端据此提示数据可信度）。"""
        try:
            meta = provider_meta(self.provider)
        except Exception:
            return "offline"
        if self.settings.offline or meta.offline:
            return "offline"
        if meta.stale or meta.source == "cache":
            return "cache"
        return "real"

    def is_offline(self) -> bool:
        try:
            meta = provider_meta(self.provider)
        except Exception:
            return True
        return bool(self.settings.offline or meta.offline)

    def system_status(self) -> Dict[str, Any]:
        from .. import __version__
        from ..compat import backend_info

        try:
            calendar = self.calendar
            market = {
                "is_closed": bool(calendar.is_closed()),
                "last_trading_day": calendar.last_trading_day(),
            }
        except Exception as exc:
            market = {"is_closed": True, "last_trading_day": "", "error": str(exc)}

        try:
            watchlist_count = len(self.watchlist())
        except Exception:
            watchlist_count = 0

        from .common import cache_stats

        return {
            "version": __version__,
            "mode": self.mode(),
            "provider": self._provider_info(),
            "offline": self.is_offline(),
            "as_of": provider_meta(self.provider).as_of or "",
            "market": market,
            "cache": {
                "ttl": self.settings.cache_ttl.to_dict(),
                "stats": cache_stats(),
            },
            "backend": backend_info(),
            "data_dir": self.settings.data_dir,
            "benchmark": self.settings.benchmark,
            "initial_cash": self.settings.initial_cash,
            "strategies": self._strategies_info(),
            "local": self._local_info(),
            "trade": self._trade_info(),
            "watchlist_count": watchlist_count,
        }
