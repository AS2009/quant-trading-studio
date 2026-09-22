# -*- coding: utf-8 -*-
"""回测业务服务：参数解析 → 组装 BacktestRequest → 调用回测引擎 → 进程内 LRU 缓存。

- 缓存容量 32，key 为「策略 + 规范化参数」的 SHA-256 摘要（跨进程稳定）
- 缓存中的 BacktestResult 视为**只读**：调用方不得修改返回结构
- 数据不足（``InsufficientData``）转换为可读的 ``ValidationError``（400）
"""

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings, get_settings
from ..core.errors import InsufficientData, ValidationError
from ..core.models import BacktestRequest, FeeConfig
from .common import (
    NotFound,
    get_data_provider,
    normalize_codes,
    parse_adjust,
    parse_float,
    trading_calendar,
)

# 请求级参数（其余键透传给策略作为参数覆盖）
REQUEST_KEYS = frozenset(
    [
        "start",
        "end",
        "cash",
        "initial_cash",
        "benchmark",
        "symbols",
        "adjust",
        "freq",
        "slippage_bps",
        "commission_rate",
        "commission_min",
        "stamp_duty_rate",
        "transfer_fee_rate",
        "lot_size",
        "params",
    ]
)


def _coerce(value: Any) -> Any:
    """query string 中的数字 / 布尔还原类型，便于策略参数覆盖。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return value


class BacktestService:
    """回测编排与结果缓存。"""

    CACHE_SIZE = 32

    def __init__(
        self,
        provider: Any = None,
        settings: Optional[Settings] = None,
        engine: Any = None,
        strategies_module: Any = None,
        user_module: Any = None,
        calendar: Any = None,
    ):
        self.settings = settings or get_settings()
        self._provider = provider
        self._engine = engine
        self._strategies = strategies_module
        self._user = user_module
        self._calendar = calendar
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # ------------------------------------------------------------------ 惰性依赖

    @property
    def provider(self):
        if self._provider is None:
            self._provider = get_data_provider(self.settings)
        return self._provider

    @property
    def strategies(self):
        if self._strategies is None:
            from .. import strategies as module

            self._strategies = module
        return self._strategies

    @property
    def user(self):
        if self._user is None:
            from ..strategies import user as module

            self._user = module
        return self._user

    @property
    def calendar(self):
        if self._calendar is None:
            self._calendar = trading_calendar(self.settings)
        return self._calendar

    @property
    def engine(self):
        if self._engine is None:
            from ..backtest.engine import BacktestEngine

            self._engine = BacktestEngine(self.provider, calendar=self.calendar)
        return self._engine

    # ------------------------------------------------------------------ 主流程

    def run(self, strategy_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行回测并返回 ``BacktestResult.to_dict()``。"""
        options = dict(params or {})
        strategy_params = self._extract_strategy_params(options)
        key = self._cache_key(strategy_id, options, strategy_params)
        hit = self._cache_get(key)
        if hit is not None:
            return hit

        request = self._build_request(strategy_id, options, strategy_params)
        strategy = self._create_strategy(strategy_id, strategy_params, request.symbols)
        try:
            result = self.engine.run(request, strategy)
        except InsufficientData as exc:
            raise ValidationError("历史数据不足，无法回测：%s" % exc, field="symbols")
        data = result.to_dict() if hasattr(result, "to_dict") else result
        adjust = parse_adjust(options.get("adjust"), "qfq")
        if adjust != "qfq" and isinstance(data, dict):
            data.setdefault("warnings", [])
            data["warnings"].append(
                "回测引擎固定使用前复权（qfq）日线，adjust=%s 已忽略" % adjust
            )
        self._cache_put(key, data)
        return data

    # ------------------------------------------------------------------ 参数解析

    @staticmethod
    def _extract_strategy_params(options: Dict[str, Any]) -> Dict[str, Any]:
        """显式 ``params`` + 未知键 → 策略参数覆盖（query 字符串按类型还原）。"""
        raw = options.pop("params", None)
        params: Dict[str, Any] = {}
        if isinstance(raw, dict):
            params.update(raw)
        elif raw not in (None, "", {}):
            raise ValidationError("params 需为 JSON 对象", field="params")
        for key in list(options.keys()):
            if key not in REQUEST_KEYS:
                params[key] = _coerce(options.pop(key))
        return params

    def _build_request(
        self, strategy_id: str, options: Dict[str, Any], strategy_params: Dict[str, Any]
    ) -> BacktestRequest:
        settings = self.settings
        default_fee = FeeConfig()

        cash = parse_float(
            options.get("cash", options.get("initial_cash")),
            "cash",
            default=settings.initial_cash,
            minimum=0.0,
        )
        benchmark = str(options.get("benchmark") or settings.benchmark).strip() or settings.benchmark
        raw_symbols = options.get("symbols")
        symbols = normalize_codes(raw_symbols, field="symbols") if raw_symbols else []
        adjust = parse_adjust(options.get("adjust"), "qfq")

        fee = FeeConfig(
            commission_rate=parse_float(
                options.get("commission_rate"),
                "commission_rate",
                default=default_fee.commission_rate,
                minimum=0.0,
                maximum=0.05,
            ),
            commission_min=parse_float(
                options.get("commission_min"),
                "commission_min",
                default=default_fee.commission_min,
                minimum=0.0,
            ),
            stamp_duty_rate=parse_float(
                options.get("stamp_duty_rate"),
                "stamp_duty_rate",
                default=default_fee.stamp_duty_rate,
                minimum=0.0,
                maximum=0.05,
            ),
            transfer_fee_rate=parse_float(
                options.get("transfer_fee_rate"),
                "transfer_fee_rate",
                default=default_fee.transfer_fee_rate,
                minimum=0.0,
                maximum=0.05,
            ),
            slippage_bps=parse_float(
                options.get("slippage_bps"),
                "slippage_bps",
                default=default_fee.slippage_bps,
                minimum=0.0,
                maximum=500.0,
            ),
            lot_size=int(
                parse_float(
                    options.get("lot_size"),
                    "lot_size",
                    default=float(default_fee.lot_size),
                    minimum=1.0,
                )
            ),
        )

        # 回测引擎固定使用前复权日线：adjust 仅做参数校验，不混入策略参数（详见 run 的 warnings）

        return BacktestRequest(
            strategy_id=strategy_id,
            symbols=symbols,
            start=str(options.get("start") or "").strip(),
            end=str(options.get("end") or "").strip(),
            initial_cash=float(cash),
            benchmark=benchmark,
            fee=fee,
            params_override=dict(strategy_params),
        )

    def _spec_id(self, spec: Any) -> str:
        if isinstance(spec, dict):
            return str(spec.get("id") or "")
        return str(getattr(spec, "id", "") or "")

    def _create_strategy(self, strategy_id: str, params: Dict[str, Any], symbols: List[str]):
        """内置 id 直接构造；用户策略按 template 构造并保留用户元数据。"""
        strategies = self.strategies
        builtin_ids = set()
        try:
            builtin_ids = {str(spec.id) for spec in (strategies.list_specs() or [])}
        except Exception:
            builtin_ids = set()

        if strategy_id in builtin_ids:
            try:
                return strategies.create(
                    strategy_id, params=params or None, symbols=symbols or None
                )
            except NotFound:
                raise
            except KeyError:
                raise NotFound("策略不存在：%s" % strategy_id)
            except ValidationError:
                raise
            except ValueError as exc:
                raise ValidationError(str(exc) or "策略参数非法")

        spec, template = self._find_user_strategy(strategy_id)
        if spec is None:
            raise NotFound("策略不存在：%s" % strategy_id)
        spec_params = spec.get("params") if isinstance(spec, dict) else getattr(spec, "params", {})
        merged = dict(spec_params or {})
        merged.update(params or {})
        try:
            return strategies.create(
                template, params=merged or None, symbols=symbols or None, spec=spec
            )
        except ValidationError:
            raise
        except ValueError as exc:
            raise ValidationError(str(exc) or "用户策略参数非法")

    def _find_user_strategy(self, strategy_id: str) -> Tuple[Any, str]:
        """在用户策略文件中查找 id；返回 (spec, template)。未找到返回 (None, "")。"""
        path = self.settings.user_strategies_path
        user = self.user
        template = ""
        loader = getattr(user, "load_user_items", None)
        if callable(loader):
            try:
                for raw in loader(path) or []:
                    if isinstance(raw, dict) and str(raw.get("id") or "") == strategy_id:
                        template = str(raw.get("template") or "").strip()
                        break
            except Exception:
                template = ""
        for item in self.user.load_user_specs(path) or []:
            if self._spec_id(item) == strategy_id:
                return item, template or "st_ma_cross"
        return None, ""

    # ------------------------------------------------------------------ LRU 缓存

    @staticmethod
    def _cache_key(
        strategy_id: str, options: Dict[str, Any], strategy_params: Dict[str, Any]
    ) -> str:
        canonical = json.dumps(
            {
                "strategy": strategy_id,
                "options": options,
                "params": strategy_params,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return "%s:%s" % (strategy_id, digest)

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def _cache_put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)

    def clear_cache(self) -> Dict[str, Any]:
        with self._lock:
            size = len(self._cache)
            self._cache.clear()
        return {"cleared": True, "size": size}

    def cache_info(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self.CACHE_SIZE,
                "keys": list(self._cache.keys()),
            }
