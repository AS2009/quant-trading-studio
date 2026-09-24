# -*- coding: utf-8 -*-
"""A 股成交价与交易费用（**单一事实来源**）。

本模块只做纯函数、不持有状态，且只依赖 :mod:`quantstudio.core.models`
（``FeeConfig`` 与 ``SIDE_BUY`` / ``SIDE_SELL``）与 :mod:`quantstudio.core.errors`，
不反向依赖 backtest / trading / strategies 包，回测与模拟盘都可以复用同一口径。

口径
----
1. **成交价**（:func:`exec_price`）——买入上浮、卖出下浮，两种滑点**叠加**：

   ``买方 = 基准价 × (1 + slippage_bps/10000) + slippage_ticks × tick_size``
   ``卖方 = 基准价 × (1 - slippage_bps/10000) - slippage_ticks × tick_size``

   随后按 ``tick_size`` 取整到最小变动价位，**统一取对买方不利的方向**
   （买入向上取整、卖出向下取整），最后保留 2 位小数。
   当 ``slippage_ticks <= 0``（默认）时**跳过**跳数与取整逻辑，退化为
   ``round(基准价 × (1 ± slippage_bps/10000), 2)``——与改造前的历史口径逐分一致。

2. **费用**（:func:`order_cost` / :func:`fee_items`）——各分项与合计均四舍五入到分：

   ================ ==================================================== 方向
   佣金             ``max(成交额 × commission_rate, commission_min)``     双边
   印花税           ``成交额 × stamp_duty_rate``                         仅卖出
   过户费           ``成交额 × transfer_fee_rate``                        双边
   流量费           ``flow_fee``（每笔固定）                              买卖各收一次
   ================ ==================================================== 方向

   费率/固定费为负时按 0 处理；成交额非有限值或 <= 0 时各分项为 0
   （不抛异常、不产生负数成本）。

3. **资金变动**（``cash_delta``）——买入 ``-(成交额 + 费用合计)``，卖出 ``+(成交额 - 费用合计)``。

4. **买入量反解**（:func:`max_buy_qty`）——给定**基准价**与可用资金，返回「含滑点与费用」能买入的
   最大整手数（先按成交价估上界，再以手数二分收敛）。回测撮合的资金缩量与策略
   ``max_buy_qty`` 共用这一实现，避免「策略算一套、撮合算另一套」。
"""

import math
from typing import Any, Dict, Optional

from .errors import ValidationError
from .models import SIDE_BUY, SIDE_SELL, FeeConfig

__all__ = ["EPS", "exec_price", "fee_items", "fee_total", "max_buy_qty", "normalize_fee", "order_cost",
           "zero_cost"]

_SIDES = (SIDE_BUY, SIDE_SELL)
EPS = 1e-6      # 金额比较容差（回测撮合的资金校验共用，避免两侧各写一个常量而漂移）


# --------------------------------------------------------------------------- 内部工具


def _to_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """把入参转成有限 float；无法转换或非有限值时返回 ``default``（``None`` 表示不兜底）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _nonneg(value: Any, default: float = 0.0) -> float:
    """取非负费率/费用：None / 非数值 / 非有限值 / 负数一律按 ``default``（默认 0）。"""
    num = _to_float(value, None)
    if num is None or num < 0.0:
        return default
    return num


def _check_side(side: str) -> str:
    """校验委托方向，非法值抛 :class:`ValidationError`。"""
    if side not in _SIDES:
        raise ValidationError("委托方向只能是 buy / sell，当前为 %r" % (side,), field="side")
    return side


def normalize_fee(fee: Optional[FeeConfig]) -> FeeConfig:
    """None / dict / FeeConfig 统一成 FeeConfig（dict 的未知键会被忽略）。"""
    if fee is None:
        return FeeConfig()
    if isinstance(fee, FeeConfig):
        return fee
    if isinstance(fee, dict):
        known = set(FeeConfig().__dict__.keys())
        return FeeConfig(**{key: value for key, value in fee.items() if key in known})
    raise ValidationError("fee 必须是 FeeConfig 或 dict，当前为 %r" % (type(fee).__name__,), field="fee")


def zero_cost() -> Dict[str, float]:
    """零成本结果（价格/数量非法、成交额 <= 0 时返回，字段与 :func:`order_cost` 一致）。"""
    return {
        "exec_price": 0.0,
        "amount": 0.0,
        "commission": 0.0,
        "stamp_duty": 0.0,
        "transfer_fee": 0.0,
        "flow_fee": 0.0,
        "fee_total": 0.0,
        "cash_delta": 0.0,
    }


# --------------------------------------------------------------------------- 成交价


def exec_price(side: str, price: float, fee: Optional[FeeConfig] = None) -> float:
    """计入滑点后的成交价（元，保留 2 位小数）。

    - ``price <= 0`` 或非有限值 → 返回 ``0.0``（由调用方判定「不成交」）；
    - 比例滑点 ``slippage_bps``：买入 ``price × (1 + bps/10000)``、卖出反向；
    - 跳数滑点 ``slippage_ticks × tick_size``：在比例滑点基础上**叠加**；
    - 有跳数滑点时按 ``tick_size`` 取整，方向**统一对买方不利**：
      买入向上取整（买方多付）、卖出向下取整（卖方少收）；
    - ``slippage_ticks <= 0``（默认）时不取整，结果与改造前 ``round(price × (1 ± bps/10000), 2)``
      **逐分一致**（回归锁见 backend/tests/test_costs.py）。
    """
    _check_side(side)
    cfg = normalize_fee(fee)
    base = _to_float(price, None)
    if base is None or base <= 0.0:
        return 0.0

    rate = _nonneg(cfg.slippage_bps) / 10000.0
    ratio = 1.0 + rate
    # 与改造前完全相同的写法：卖出用 (2 - ratio) 而非 (1 - rate)，保证浮点结果逐位一致
    value = base * (ratio if side == SIDE_BUY else (2.0 - ratio))

    ticks = _nonneg(cfg.slippage_ticks)
    tick = _nonneg(cfg.tick_size)
    if ticks > 0.0 and tick > 0.0:
        shift = ticks * tick
        value = value + shift if side == SIDE_BUY else value - shift
        if value <= 0.0:
            return 0.0
        units = value / tick
        # 取整到最小变动价位：买入向上、卖出向下（对买方不利），1e-9 抵消浮点噪声
        if side == SIDE_BUY:
            units = math.ceil(units - 1e-9)
        else:
            units = math.floor(units + 1e-9)
        value = units * tick

    value = round(value, 2)
    return value if value > 0.0 else 0.0


# --------------------------------------------------------------------------- 费用


def fee_items(side: str, amount: float, fee: Optional[FeeConfig] = None) -> Dict[str, float]:
    """按成交额计算各费用分项（元，四舍五入到分）。

    这是佣金的**唯一实现**：:func:`order_cost` 与 ``SimulatedBroker.calc_fee`` 都走这里。
    ``amount`` 非数值时抛 :class:`ValidationError`；``amount`` 非有限值或 ``<= 0`` 时全为 0。
    """
    _check_side(side)
    cfg = normalize_fee(fee)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValidationError("成交额必须是数字", field="amount")
    if math.isnan(value) or math.isinf(value):
        value = 0.0
    if value <= 0.0:
        return {"commission": 0.0, "stamp_duty": 0.0, "transfer_fee": 0.0,
                "flow_fee": 0.0, "fee_total": 0.0}

    commission = round(max(value * _nonneg(cfg.commission_rate), _nonneg(cfg.commission_min)), 2)
    stamp = round(value * _nonneg(cfg.stamp_duty_rate), 2) if side == SIDE_SELL else 0.0
    transfer = round(value * _nonneg(cfg.transfer_fee_rate), 2)
    flow = round(_nonneg(cfg.flow_fee), 2)          # 每笔固定：买卖各收一次
    total = round(commission + stamp + transfer + flow, 2)
    return {
        "commission": commission,
        "stamp_duty": stamp,
        "transfer_fee": transfer,
        "flow_fee": flow,
        "fee_total": total if total > 0.0 else 0.0,
    }


def fee_total(side: str, amount: float, fee: Optional[FeeConfig] = None) -> float:
    """单笔费用合计（元）——只有成交额、没有价格的调用点（如 ``calc_fee``）用本函数。

    与 ``order_cost(...)["fee_total"]`` 是同一实现（都调用 :func:`fee_items`）。
    """
    return fee_items(side, amount, fee)["fee_total"]


# --------------------------------------------------------------------------- 单笔成本


def order_cost(side: str, price: float, qty: float, fee: Optional[FeeConfig] = None) -> Dict[str, float]:
    """一笔委托的成交价、成交额、费用分项与资金变动（单一入口，回测/模拟盘共用）。

    返回字段：

    ================== ==========================================================
    ``exec_price``     滑点后成交价（元，2 位小数）
    ``amount``         成交额 = 成交价 × 数量（元，2 位小数）
    ``commission``     佣金（含最低佣金，双边）
    ``stamp_duty``     印花税（仅卖出）
    ``transfer_fee``   过户费（双边）
    ``flow_fee``       每笔固定流量费（买卖各收一次）
    ``fee_total``      合计费用（分项之和，非负）
    ``cash_delta``     资金变动：买入 ``-(amount + fee_total)``、卖出 ``+(amount - fee_total)``
    ================== ==========================================================

    ``price <= 0`` 或 ``qty <= 0``（含非数值 / 非有限值）时返回 :func:`zero_cost` 全零结果，
    **不抛异常也不产生负数成本**；只有 ``side`` 非法时抛 :class:`ValidationError`。
    """
    _check_side(side)
    cfg = normalize_fee(fee)
    base = _to_float(price, None)
    qty_value = _to_float(qty, None)
    if base is None or base <= 0.0 or qty_value is None or qty_value <= 0.0:
        return zero_cost()

    price_value = exec_price(side, base, cfg)
    if price_value <= 0.0:
        return zero_cost()
    amount = price_value * qty_value
    items = fee_items(side, amount, cfg)
    total = items["fee_total"]
    cash_delta = -(amount + total) if side == SIDE_BUY else (amount - total)
    return {
        "exec_price": price_value,
        "amount": round(amount, 2),
        "commission": items["commission"],
        "stamp_duty": items["stamp_duty"],
        "transfer_fee": items["transfer_fee"],
        "flow_fee": items["flow_fee"],
        "fee_total": total,
        "cash_delta": round(cash_delta, 2),
    }


# --------------------------------------------------------------------------- 买入量反解


def _buy_fits(price: float, qty: int, fee: FeeConfig, budget: float) -> bool:
    """买入 ``qty`` 股（含滑点与费用）是否在 ``budget`` 资金以内。"""
    if qty <= 0:
        return True
    cost = order_cost(SIDE_BUY, price, qty, fee)
    return (-cost["cash_delta"]) <= budget + EPS


def max_buy_qty(price: float, cash: float, fee: Optional[FeeConfig] = None, lot_size: int = 100) -> int:
    """按可用资金能买入的最大整手数（**已计入滑点与全部费用**，精确反解）。

    ``price`` 是**未计滑点的基准价**（与 :func:`order_cost` / :func:`exec_price` 同口径），
    函数内部自行算成交价；返回满足 ``成交额 + 费用合计 <= cash`` 的最大 ``lot_size`` 整数倍
    （买不满 1 手返回 0）。入参非法（价格/资金非正或非有限值、``lot_size <= 0``）返回 0，
    不抛异常。

    实现：先用成交价估一个不含费用的上界（必然不小于真值），若上界本身就买得起直接返回；
    否则在 ``[0, 上界]`` 上按「手数」二分——单笔费用对数量单调不减，二分安全。
    """
    cfg = normalize_fee(fee)
    base = _to_float(price, None)
    budget = _to_float(cash, None)
    lot = int(_to_float(lot_size, 0.0) or 0.0)
    if base is None or base <= 0.0 or budget is None or budget <= 0.0 or lot <= 0:
        return 0
    unit = exec_price(SIDE_BUY, base, cfg)
    if unit <= 0.0:
        return 0
    upper = int((budget + EPS) // (unit * lot)) * lot  # 硬上界：连费用都不算也买不满（含与 _buy_fits 同源的容差）
    if upper <= 0:
        return 0
    if _buy_fits(base, upper, cfg, budget):
        return upper
    low, high = 0, upper // lot                        # low 可负担、high 不可负担
    while high - low > 1:
        mid = (low + high) // 2
        if _buy_fits(base, mid * lot, cfg, budget):
            low = mid
        else:
            high = mid
    return low * lot
