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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import costs
from ..core.errors import ValidationError
from ..core.models import Bar, FeeConfig, OrderRequest, Position, StrategySpec

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# 买入时的费用/滑点预留系数（0.1%）：**只在拿不到费率信息时**兜底。
# 上下文暴露 fee_config() 时（回测上下文都有）改走 core.costs.max_buy_qty 精确反解。
_FEE_BUFFER = 0.999


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _numbers(values: Any) -> List[float]:
    """把序列转成浮点列表；含无法解析的值（None / NaN / 非数字）时返回空列表。

    指标函数据此返回 ``None``/``[]``：宁可说「数据不够」，也不要算出被污染的结果
    （典型场景：停牌日的高开低收是 NaN、财报字段缺失）。
    """
    out: List[float] = []
    for value in values or []:
        number = _to_float(value)
        if number is None:
            return []
        out.append(number)
    return out

def _fee_config(ctx) -> Any:
    """取上下文的费率配置（``fee_config()``）；拿不到或类型不符时返回 ``None``。"""
    getter = getattr(ctx, "fee_config", None)
    if not callable(getter):
        return None
    try:
        fee = getter()
    except Exception:                                  # 第三方上下文实现异常 → 退化估算
        return None
    return fee if isinstance(fee, (FeeConfig, dict)) else None

def _lot_size(ctx, fallback: int) -> int:
    """当前账户的最小交易单位：优先用上下文（账户真实口径），否则退回策略自身的 ``lot_size``。

    买卖两侧必须用**同一个**来源，否则「买入按 10 股、卖出按 100 股」会让小仓位永远卖不掉。
    """
    for value in (getattr(ctx, "lot_size", None), fallback):
        try:
            lot = int(value)
        except (TypeError, ValueError):
            continue
        if lot > 0:
            return lot
    return 100


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
        """按可用资金 × pct 能买入的最大整手数（**含滑点与全部费用**，与 broker 同口径）。

        ``ctx`` 暴露 :meth:`fee_config`（回测上下文都有）时按费率精确反解；拿不到费率信息
        的第三方上下文退化为预留 ``_FEE_BUFFER``（0.1%）的估算（broker 仍会兜底缩量）。
        """
        price = _to_float(price) or 0.0
        pct = _to_float(pct) or 0.0
        if price <= 0 or pct <= 0:
            return 0
        budget = float(ctx.cash()) * min(max(pct, 0.0), 1.0)
        if budget <= 0:
            return 0
        lot = _lot_size(ctx, self.lot_size)
        fee = _fee_config(ctx)
        if fee is None:
            qty = int(budget * _FEE_BUFFER / price // lot) * lot
            return max(qty, 0)
        return costs.max_buy_qty(price, budget, fee, lot)
        return costs.max_buy_qty(price, budget, fee, lot)

    def buy_order(self, ctx, code: str, price: float, pct: float = 1.0, reason: str = "") -> Optional[OrderRequest]:
        qty = self.max_buy_qty(ctx, price, pct)
        if qty <= 0:
            return None
        return OrderRequest(code=code, side=SIDE_BUY, qty=qty, price=None, reason=reason)

    def sell_order(self, ctx, code: str, qty: Optional[int] = None, reason: str = "") -> Optional[OrderRequest]:
        pos = ctx.position(code)
        if pos is None:
            return None
        lot = _lot_size(ctx, self.lot_size)
        available = int(pos.available_qty)
        if qty is not None:
            available = min(available, int(qty))
        available = available // lot * lot
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

    # ------------------------------------------------------------------ 指标助手（二）：序列与常用复合指标
    # 全部按公开定义独立实现（与通达信/MyTT 同口径），约定与上面一致：**取最后一根的值**；
    # 需要「前一根」做金叉判断时，用 self.cross(...) 或自己传 values[:-1]。

    @staticmethod
    def ref(values: Sequence[float], n: int = 1) -> Optional[float]:
        """``n`` 根之前的值（``ref(values, 1)`` = 上一根）；数据不足返回 None。"""
        n = int(n or 0)
        if n < 0 or len(values) < n + 1:
            return None
        return _to_float(values[-1 - n])

    @staticmethod
    def cross(a: Sequence[float], b: Sequence[float]) -> bool:
        """``a`` 上穿 ``b``（上一根 a ≤ b，最新一根 a > b）。"""
        a0, a1 = BaseStrategy.ref(a, 1), (_to_float(a[-1]) if a else None)
        b0, b1 = BaseStrategy.ref(b, 1), (_to_float(b[-1]) if b else None)
        if None in (a0, a1, b0, b1):
            return False
        return bool(a0 <= b0 and a1 > b1)

    @staticmethod
    def cross_under(a: Sequence[float], b: Sequence[float]) -> bool:
        """``a`` 下穿 ``b``（上一根 a ≥ b，最新一根 a < b）。"""
        a0, a1 = BaseStrategy.ref(a, 1), (_to_float(a[-1]) if a else None)
        b0, b1 = BaseStrategy.ref(b, 1), (_to_float(b[-1]) if b else None)
        if None in (a0, a1, b0, b1):
            return False
        return bool(a0 >= b0 and a1 < b1)

    @staticmethod
    def ema_series(values: Sequence[float], n: int) -> List[float]:
        """整条 EMA 序列（种子 = 第一个值，``alpha = 2/(n+1)``）；数据不足返回 ``[]``。

        自己组合指标（例如「EMA 之差再做 EMA」）时用这个，比反复调用 ``ema`` 方便。
        """
        n = int(n or 0)
        nums = _numbers(values)
        if n <= 0 or len(nums) < n:
            return []
        alpha = 2.0 / (n + 1.0)
        out = [nums[0]]
        for value in nums[1:]:
            out.append(out[-1] + alpha * (value - out[-1]))
        return out

    @staticmethod
    def sma_series(values: Sequence[float], n: int, m: int = 1) -> List[float]:
        """整条「中国式 SMA」（``Y = (M·X + (N−M)·Y') / N``，即 ``alpha = M/N``）。

        KDJ、RSI 等递推型指标都基于它；种子 = 第一个值。数据不足返回 ``[]``。
        """
        n = int(n or 0)
        m = int(m or 0)
        nums = _numbers(values)
        if n <= 0 or m <= 0 or m > n or len(nums) < n:
            return []
        alpha = float(m) / float(n)
        out = [nums[0]]
        for value in nums[1:]:
            out.append(out[-1] + alpha * (value - out[-1]))
        return out

    @staticmethod
    def sma(values: Sequence[float], n: int = 14, m: int = 1) -> Optional[float]:
        """「中国式 SMA」的最新值（见 :meth:`sma_series`）。"""
        series = BaseStrategy.sma_series(values, n, m)
        return series[-1] if series else None

    @staticmethod
    def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
             signal: int = 9) -> Optional[Tuple[float, float, float]]:
        """MACD → ``(DIF, DEA, HIST)``；``HIST = 2 × (DIF − DEA)``（与国内软件口径一致）。"""
        fast, slow, signal = int(fast or 0), int(slow or 0), int(signal or 0)
        nums = _numbers(values)
        if min(fast, slow, signal) <= 0 or fast >= slow or len(nums) < slow + signal:
            return None
        ema_fast = BaseStrategy.ema_series(nums, fast)
        ema_slow = BaseStrategy.ema_series(nums, slow)
        if not ema_fast or not ema_slow:
            return None
        dif_series = [a - b for a, b in zip(ema_fast[-len(ema_slow):], ema_slow)]
        dea_series = BaseStrategy.ema_series(dif_series, signal)
        if not dea_series:
            return None
        dif, dea = dif_series[-1], dea_series[-1]
        return round(dif, 6), round(dea, 6), round(2.0 * (dif - dea), 6)

    @staticmethod
    def kdj(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            n: int = 9, k: int = 3, d: int = 3) -> Optional[Tuple[float, float, float]]:
        """KDJ → ``(K, D, J)``。

        ``RSV = (C − LLV(L,n)) / (HHV(H,n) − LLV(L,n)) × 100``（区间为 0 时取中值 50），
        ``K = SMA(RSV, k, 1)``、``D = SMA(K, d, 1)``、``J = 3K − 2D``。
        """
        n, k, d = int(n or 0), int(k or 0), int(d or 0)
        hs, ls, cs = _numbers(highs), _numbers(lows), _numbers(closes)
        if min(n, k, d) <= 0 or min(len(hs), len(ls), len(cs)) < n:
            return None
        rsv: List[float] = []
        for i in range(n - 1, len(cs)):
            hh = max(hs[i - n + 1:i + 1])
            ll = min(ls[i - n + 1:i + 1])
            rsv.append(50.0 if hh <= ll else (cs[i] - ll) / (hh - ll) * 100.0)
        k_series = BaseStrategy.sma_series(rsv, k, 1)
        if not k_series:
            return None
        d_series = BaseStrategy.sma_series(k_series, d, 1)
        if not d_series:
            return None
        kk, dd = k_series[-1], d_series[-1]
        return round(kk, 4), round(dd, 4), round(3.0 * kk - 2.0 * dd, 4)

    @staticmethod
    def boll(values: Sequence[float], n: int = 20,
             k: float = 2.0) -> Optional[Tuple[float, float, float]]:
        """布林带 → ``(上轨, 中轨, 下轨)``；标准差用**总体**口径（与通达信一致）。"""
        n = int(n or 0)
        k = float(k or 0.0)
        nums = _numbers(values)
        if n <= 1 or len(nums) < n:
            return None
        window = nums[-n:]
        mid = sum(window) / n
        var = sum((v - mid) ** 2 for v in window) / n
        std = var ** 0.5
        return round(mid + k * std, 6), round(mid, 6), round(mid - k * std, 6)

    @staticmethod
    def cci(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            n: int = 14) -> Optional[float]:
        """CCI（顺势指标）；``TP = (H+L+C)/3``，平均绝对偏差为 0 时返回 0。"""
        n = int(n or 0)
        hs, ls, cs = _numbers(highs), _numbers(lows), _numbers(closes)
        if n <= 0 or min(len(hs), len(ls), len(cs)) < n:
            return None
        tp = [(h + l + c) / 3.0 for h, l, c in zip(hs[-n:], ls[-n:], cs[-n:])]
        ma = sum(tp) / n
        md = sum(abs(v - ma) for v in tp) / n
        if md <= 0:
            return 0.0
        return round((tp[-1] - ma) / (0.015 * md), 4)

    @staticmethod
    def wr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
           n: int = 14) -> Optional[float]:
        """威廉指标 W%R（取负值，范围 −100~0）；区间为 0 时返回 None。"""
        n = int(n or 0)
        hs, ls, cs = _numbers(highs), _numbers(lows), _numbers(closes)
        if n <= 0 or min(len(hs), len(ls), len(cs)) < n:
            return None
        hh = max(hs[-n:])
        ll = min(ls[-n:])
        if hh <= ll:
            return None
        return round((hh - cs[-1]) / (hh - ll) * -100.0, 4)

    @staticmethod
    def bias(values: Sequence[float], n: int = 6) -> Optional[float]:
        """乖离率 BIAS（%）：``(C − MA(C,n)) / MA(C,n) × 100``。"""
        n = int(n or 0)
        nums = _numbers(values)
        if n <= 0 or len(nums) < n:
            return None
        ma = sum(nums[-n:]) / n
        if ma == 0:
            return None
        return round((nums[-1] - ma) / ma * 100.0, 4)

    @staticmethod
    def obv(closes: Sequence[float], volumes: Sequence[float]) -> Optional[float]:
        """能量潮 OBV 的最新值：上涨累加成交量、下跌累减、平盘不变。"""
        cs, vs = _numbers(closes), _numbers(volumes)
        n = min(len(cs), len(vs))
        if n < 2:
            return None
        total = 0.0
        for i in range(1, n):
            if cs[i] > cs[i - 1]:
                total += vs[i]
            elif cs[i] < cs[i - 1]:
                total -= vs[i]
        return round(total, 4)

    @staticmethod
    def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            n: int = 14) -> Optional[Tuple[float, float, float]]:
        """DMI 的 ``(ADX, +DI, −DI)``：TR / +DM / −DM 先做 Wilder 平滑，再算 DX 与 ADX。"""
        n = int(n or 0)
        hs, ls, cs = _numbers(highs), _numbers(lows), _numbers(closes)
        size = min(len(hs), len(ls), len(cs))
        if n <= 0 or size < 2 * n + 1:
            return None
        trs: List[float] = []
        plus_dm: List[float] = []
        minus_dm: List[float] = []
        for i in range(1, size):
            up = hs[i] - hs[i - 1]
            down = ls[i - 1] - ls[i]
            trs.append(max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])))
            plus_dm.append(up if (up > down and up > 0) else 0.0)
            minus_dm.append(down if (down > up and down > 0) else 0.0)

        def _wilder(values: List[float]) -> List[float]:
            smooth = sum(values[:n])
            out = [smooth]
            for value in values[n:]:
                smooth = smooth - smooth / n + value
                out.append(smooth)
            return out

        tr_s, plus_s, minus_s = _wilder(trs), _wilder(plus_dm), _wilder(minus_dm)
        dx: List[float] = []
        for tr_v, p_v, m_v in zip(tr_s, plus_s, minus_s):
            if tr_v <= 0:
                dx.append(0.0)
                continue
            plus_di = 100.0 * p_v / tr_v
            minus_di = 100.0 * m_v / tr_v
            total = plus_di + minus_di
            dx.append(0.0 if total <= 0 else 100.0 * abs(plus_di - minus_di) / total)
        if len(dx) < n:
            return None
        adx_value = sum(dx[:n]) / n
        for value in dx[n:]:
            adx_value = (adx_value * (n - 1) + value) / n
        tr_last = tr_s[-1]
        plus_di_last = 0.0 if tr_last <= 0 else 100.0 * plus_s[-1] / tr_last
        minus_di_last = 0.0 if tr_last <= 0 else 100.0 * minus_s[-1] / tr_last
        return round(adx_value, 4), round(plus_di_last, 4), round(minus_di_last, 4)

    @staticmethod
    def trix(values: Sequence[float], n: int = 12,
             m: int = 9) -> Optional[Tuple[float, float]]:
        """TRIX → ``(TRIX, MATRIX)``：三重 EMA 的变化率（%）与其 ``m`` 日均线。"""
        n, m = int(n or 0), int(m or 0)
        if n <= 0 or m <= 0:
            return None
        series = BaseStrategy.ema_series(values, n)
        for _ in range(2):
            series = BaseStrategy.ema_series(series, n)
            if not series:
                return None
        if len(series) < 2:
            return None
        trix_series: List[float] = []
        for i in range(1, len(series)):
            prev = series[i - 1]
            trix_series.append(0.0 if prev == 0 else (series[i] - prev) / prev * 100.0)
        if len(trix_series) < m:
            return None
        return round(trix_series[-1], 4), round(sum(trix_series[-m:]) / m, 4)

    @staticmethod
    def psy(closes: Sequence[float], n: int = 12) -> Optional[float]:
        """心理线 PSY：最近 ``n`` 根里上涨根数占比 × 100（%）。"""
        n = int(n or 0)
        nums = _numbers(closes)
        if n <= 0 or len(nums) < n + 1:
            return None
        window = nums[-(n + 1):]
        ups = sum(1 for i in range(1, len(window)) if window[i] > window[i - 1])
        return round(ups * 100.0 / n, 4)

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
