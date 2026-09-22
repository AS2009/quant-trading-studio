# -*- coding: utf-8 -*-
"""策略基类：指标助手 + 参数校验 / Schema + 元数据（内置策略与用户自定义策略共用）。

约定
----
- 子类只需要实现 ``on_bar(ctx, bars) -> List[OrderRequest]``；``on_start`` / ``on_finish`` 有默认空实现。
- 所有指标助手都是**纯标准库**实现，输入为「升序、最后一根是今日」的序列（来自 ``ctx.history``），
  数据不足时返回 ``None``（策略自行跳过），**绝不使用未来数据**。
- ``validate_params`` 做类型 / 范围校验并补齐默认值：缺失用默认值、未知参数与非法取值抛
  :class:`~quantstudio.core.errors.ValidationError`。
- ``param_schema()`` 是给前端表单用的参数描述：``{参数名: {label, type, default, min, max, step, help}}``。
"""

import math
from typing import Any, Dict, List, Optional, Sequence

from ..core.errors import ValidationError
from ..core.models import Bar, OrderRequest, Position, StrategySpec

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# 买入时的费用/滑点预留系数（0.1%）：避免策略给出的整手数量因费用被砍掉一手
_FEE_BUFFER = 0.999


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _coerce_param(strategy_id: str, key: str, value: Any, meta: Dict[str, Any]) -> Any:
    ptype = str(meta.get("type") or "float").lower()
    label = str(meta.get("label") or key)
    if value is None:
        value = meta.get("default")
    if value is None:
        raise ValidationError("策略 %s 参数 %s 不能为空" % (strategy_id, key), field=key)

    if ptype in ("int", "float"):
        if isinstance(value, bool):
            raise ValidationError("策略 %s 参数 %s（%s）需为数字，收到布尔值" % (strategy_id, key, label), field=key)
        if isinstance(value, str):
            num = _to_float(value.strip())
            if num is None:
                raise ValidationError(
                    "策略 %s 参数 %s（%s）需为数字，收到 %r" % (strategy_id, key, label, value), field=key
                )
            value = num
        if not isinstance(value, (int, float)):
            raise ValidationError(
                "策略 %s 参数 %s（%s）需为数字，收到 %s" % (strategy_id, key, label, type(value).__name__), field=key
            )
        num = _to_float(value)
        if num is None:
            raise ValidationError("策略 %s 参数 %s（%s）不能为 NaN/Infinity" % (strategy_id, key, label), field=key)
        if ptype == "int":
            if num != int(num):
                raise ValidationError("策略 %s 参数 %s（%s）需为整数，收到 %r" % (strategy_id, key, label, value), field=key)
            num = int(num)
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None and num < lo:
            raise ValidationError(
                "策略 %s 参数 %s（%s）不能小于 %s，当前 %s" % (strategy_id, key, label, lo, num), field=key
            )
        if hi is not None and num > hi:
            raise ValidationError(
                "策略 %s 参数 %s（%s）不能大于 %s，当前 %s" % (strategy_id, key, label, hi, num), field=key
            )
        return num

    if ptype == "bool":
        if not isinstance(value, bool):
            raise ValidationError("策略 %s 参数 %s（%s）需为布尔值" % (strategy_id, key, label), field=key)
        return value

    if ptype == "str":
        if not isinstance(value, str):
            raise ValidationError("策略 %s 参数 %s（%s）需为字符串" % (strategy_id, key, label), field=key)
        text = value.strip()
        choices = meta.get("choices")
        if choices and text not in choices:
            raise ValidationError(
                "策略 %s 参数 %s（%s）只能取 %s，当前 %r"
                % (strategy_id, key, label, "/".join(str(c) for c in choices), text), field=key
            )
        return text

    raise ValidationError("策略 %s 参数 %s 的类型 %r 不受支持" % (strategy_id, key, ptype), field=key)


class BaseStrategy:
    """策略基类（满足 ``core.interfaces.Strategy`` 协议）。"""

    id: str = ""
    name: str = ""
    category: str = "自定义"
    desc: str = ""
    universe: str = ""
    universe_type: str = "single"      # single / multi / index
    freq: str = "日线"
    min_bars: int = 60
    version: str = "1.0"
    default_params: Dict[str, Any] = {}
    default_symbols: List[str] = []
    lot_size: int = 100                # 一手股数（A 股股票为 100）

    def __init__(self, params: Optional[Dict[str, Any]] = None,
                 symbols: Optional[Sequence[str]] = None,
                 spec: Optional[StrategySpec] = None):
        cls = type(self)
        if not cls.id:
            raise ValidationError("策略类 %s 未定义 id" % cls.__name__)
        self.spec: StrategySpec = spec if isinstance(spec, StrategySpec) else cls.meta()
        merged: Dict[str, Any] = dict(self.spec.params or {})
        merged.update(params or {})
        self.params = cls.validate_params(merged)
        self.spec.params = dict(self.params)
        if not self.spec.param_schema:
            self.spec.param_schema = cls.param_schema()
        if not self.spec.min_bars:
            self.spec.min_bars = cls.min_bars
        self.symbols: List[str] = [str(code).strip() for code in (symbols if symbols is not None else cls.default_symbols) if str(code).strip()]
        self.ctx = None

    # ------------------------------------------------------------------ 元数据
    @classmethod
    def meta(cls) -> StrategySpec:
        """策略元数据（registry.list_specs / 前端策略卡片使用）。"""
        return StrategySpec(
            id=cls.id,
            name=cls.name or cls.id,
            category=cls.category,
            desc=cls.desc,
            params=cls.validate_params({}),
            status="paused",
            universe=cls.universe,
            universe_type=cls.universe_type,
            freq=cls.freq,
            builtin=True,
            param_schema=cls.param_schema(),
            min_bars=int(cls.min_bars),
            version=cls.version,
        )

    @classmethod
    def param_schema(cls) -> Dict[str, Any]:
        """参数描述：``{参数名: {label, type, default, min, max, step, help}}``。"""
        return {}

    @classmethod
    def validate_params(cls, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """校验并补齐参数（缺失取默认值；未知参数 / 类型 / 范围非法抛 ValidationError）。"""
        schema = cls.param_schema() or {}
        raw = params if params is not None else {}
        if not isinstance(raw, dict):
            raise ValidationError("策略 %s 的参数必须是对象（dict）" % cls.id, field="params")
        unknown = sorted(str(key) for key in raw.keys() if key not in schema)
        if unknown:
            raise ValidationError(
                "策略 %s 不支持参数 %s，可用参数：%s"
                % (cls.id, ", ".join(unknown), ", ".join(schema.keys()) or "（无）"),
                field="params",
            )
        out: Dict[str, Any] = {}
        for key in schema:                        # 按 schema 顺序输出，保证结果稳定
            out[key] = _coerce_param(cls.id, key, raw.get(key, schema[key].get("default")), schema[key])
        cls.check_params(out)
        return out

    @classmethod
    def check_params(cls, params: Dict[str, Any]) -> None:
        """跨参数一致性校验钩子（子类可覆盖；默认无约束）。"""

    # ------------------------------------------------------------------ 运行期便捷方法
    def set_params(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """运行时改参（回测引擎用于 params_override），返回生效后的参数。"""
        self.params = type(self).validate_params(params or {})
        self.spec.params = dict(self.params)
        return dict(self.params)

    def set_symbols(self, symbols: Sequence[str]) -> List[str]:
        """设置标的池（回测引擎在解析出 symbols 后调用）。"""
        self.symbols = [str(code).strip() for code in (symbols or []) if str(code).strip()]
        return list(self.symbols)

    # ------------------------------------------------------------------ 生命周期
    def on_start(self, ctx) -> None:
        """回测/模拟盘启动（默认空实现）。"""
        self.ctx = ctx

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        """收到当日各标的 bar，返回委托列表（子类必须实现）。"""
        raise NotImplementedError("%s 必须实现 on_bar" % type(self).__name__)

    def on_finish(self, ctx) -> None:
        """回测/模拟盘结束（默认空实现）。"""

    # ------------------------------------------------------------------ 下单助手
    def max_buy_qty(self, ctx, price: float, pct: float = 1.0) -> int:
        """按可用资金 × pct 能买入的最大整手数（预留 0.1% 费用/滑点，broker 会再校验）。"""
        price = _to_float(price) or 0.0
        pct = _to_float(pct) or 0.0
        if price <= 0 or pct <= 0:
            return 0
        budget = float(ctx.cash()) * min(max(pct, 0.0), 1.0) * _FEE_BUFFER
        qty = int(budget / price // self.lot_size) * self.lot_size
        return max(qty, 0)

    def buy_order(self, ctx, code: str, price: float, pct: float = 1.0, reason: str = "") -> Optional[OrderRequest]:
        qty = self.max_buy_qty(ctx, price, pct)
        if qty <= 0:
            return None
        return OrderRequest(code=code, side=SIDE_BUY, qty=qty, price=None, reason=reason)

    def sell_order(self, ctx, code: str, qty: Optional[int] = None, reason: str = "") -> Optional[OrderRequest]:
        pos = ctx.position(code)
        if pos is None:
            return None
        available = int(pos.available_qty)
        if qty is not None:
            available = min(available, int(qty))
        available = available // self.lot_size * self.lot_size
        if available <= 0:
            return None
        return OrderRequest(code=code, side=SIDE_SELL, qty=available, price=None, reason=reason)

    # ------------------------------------------------------------------ 指标助手
    @staticmethod
    def ma(values: Sequence[float], n: int) -> Optional[float]:
        """最近 n 个值的算术平均（数据不足返回 None）。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n:
            return None
        window = [_to_float(v) for v in values[-n:]]
        if any(v is None for v in window):
            return None
        return sum(window) / n

    @staticmethod
    def ema(values: Sequence[float], n: int) -> Optional[float]:
        """EMA(n)：以最初 n 个值的均值作为种子，再递推（数据不足返回 None）。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n:
            return None
        nums = [_to_float(v) for v in values]
        if any(v is None for v in nums):
            return None
        alpha = 2.0 / (n + 1.0)
        value = sum(nums[:n]) / n
        for item in nums[n:]:
            value = alpha * item + (1.0 - alpha) * value
        return value

    @staticmethod
    def std(values: Sequence[float], n: int) -> Optional[float]:
        """最近 n 个值的**样本**标准差（n < 2 返回 None）。"""
        n = int(n or 0)
        if n < 2 or len(values) < n:
            return None
        window = [_to_float(v) for v in values[-n:]]
        if any(v is None for v in window):
            return None
        avg = sum(window) / n
        var = sum((v - avg) ** 2 for v in window) / (n - 1)
        return math.sqrt(var) if var > 0 else 0.0

    @staticmethod
    def pct_change(values: Sequence[float], n: int = 1) -> Optional[float]:
        """最近 n 期的涨跌幅小数：``values[-1] / values[-1-n] - 1``。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n + 1:
            return None
        last, base = _to_float(values[-1]), _to_float(values[-1 - n])
        if last is None or base is None or base == 0:
            return None
        return last / base - 1.0

    @staticmethod
    def highest(values: Sequence[float], n: int) -> Optional[float]:
        """最近 n 个值的最大值。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n:
            return None
        window = [_to_float(v) for v in values[-n:]]
        if any(v is None for v in window):
            return None
        return max(window)

    @staticmethod
    def lowest(values: Sequence[float], n: int) -> Optional[float]:
        """最近 n 个值的最小值。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n:
            return None
        window = [_to_float(v) for v in values[-n:]]
        if any(v is None for v in window):
            return None
        return min(window)

    @staticmethod
    def rsi(values: Sequence[float], n: int = 14) -> Optional[float]:
        """RSI（Wilder 平滑），0~100；数据不足返回 None。"""
        n = int(n or 0)
        if n <= 0 or len(values) < n + 1:
            return None
        nums = [_to_float(v) for v in values]
        if any(v is None for v in nums):
            return None
        gains: List[float] = []
        losses: List[float] = []
        for i in range(1, n + 1):
            delta = nums[i] - nums[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        avg_gain = sum(gains) / n
        avg_loss = sum(losses) / n
        for i in range(n + 1, len(nums)):
            delta = nums[i] - nums[i - 1]
            avg_gain = (avg_gain * (n - 1) + max(delta, 0.0)) / n
            avg_loss = (avg_loss * (n - 1) + max(-delta, 0.0)) / n
        if avg_loss <= 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @staticmethod
    def atr(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
        """ATR（Wilder 平滑的真实波幅）；数据不足返回 None。"""
        n = int(n or 0)
        if n <= 0 or len(bars) < n + 1:
            return None
        trs: List[float] = []
        for i in range(1, len(bars)):
            high = _to_float(getattr(bars[i], "high", None))
            low = _to_float(getattr(bars[i], "low", None))
            prev_close = _to_float(getattr(bars[i - 1], "close", None))
            if high is None or low is None or prev_close is None:
                return None
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        value = sum(trs[:n]) / n
        for tr in trs[n:]:
            value = (value * (n - 1) + tr) / n
        return value

    # ------------------------------------------------------------------ 展示
    def describe(self) -> Dict[str, Any]:
        """策略实例的可读描述（调试 / 日志用）。"""
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "params": dict(self.params),
            "symbols": list(self.symbols),
            "category": self.spec.category,
        }

    # 便捷别名：策略里读持仓
    @staticmethod
    def position_of(ctx, code: str) -> Optional[Position]:
        return ctx.position(code)
