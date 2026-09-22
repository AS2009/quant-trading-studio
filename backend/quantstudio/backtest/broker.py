# -*- coding: utf-8 -*-
"""模拟撮合（SimulatedBroker）——A 股真实约束下的日线回测成交模型。

撮合规则（README 照抄）
----------------------
1. **成交价**：``fill_mode="close"``（默认）按**当日收盘价**成交；
   ``fill_mode="open"`` 按**当日开盘价**成交（更保守，但注意它是「当日开盘」而非「次日开盘」，
   真正的次日开盘撮合需要挂单队列，见模块末「已知简化」）。
   成交价再按滑点调整：**买入上浮、卖出下浮** ``slippage_bps``，
   ``成交价 = round(基准价 × (1 + 滑点), 2)``（买入）/ ``round(基准价 × (1 - 滑点), 2)``（卖出），
   保留 2 位小数（A 股报价精度 0.01 元）。
2. **数量**：向下取整到 ``lot_size`` 整数倍；取整后为 0 → **不成交**（返回 None）。
3. **资金**：买入需 ``可用现金 ≥ 成交额 + 费用``；不足时按「可用资金能买的最大整手数」缩量，
   缩到 0 则放弃（返回 None）。费用本身计入资金占用（含最低佣金预留）。
4. **T+1**：卖出数量受 ``available_qty`` 限制（当日买入不可卖），超出部分缩量，缩到 0 则放弃。
5. **涨跌停不成交**：用成交基准价相对**前收盘**的涨跌幅判断
   （前收盘优先取撮合器跟踪的上一根 bar 收盘价，缺失时用 ``bar.change_pct``）：
   - 涨幅 ≥ ``limit_pct``（默认 9.8%，10% 留容错）→ 视同**涨停**：**买入不成交**（卖出可以成交）；
   - 跌幅 ≤ -9.8% → 视同**跌停**：**卖出不成交**（买入可以成交）。
   停牌（当日无 bar）由引擎拦截，不进入撮合。
6. 任何被放弃的委托都会记入 :attr:`SimulatedBroker.rejects`，便于回测结果给出提示。

费用（``calc_fee``）
-------------------
- 佣金：``max(成交额 × commission_rate, commission_min)``，双边；
- 印花税：``成交额 × stamp_duty_rate``，**仅卖出**；
- 过户费：``成交额 × transfer_fee_rate``，双边（沪深已统一）；
- 各分项与合计均四舍五入到分，且合计 ``max(..., 0.0)`` 保证非负。

已知简化
--------
- 日内不模拟：无盘中价格路径，涨跌停按日线收盘/开盘价判定，无法识别「盘中触板后打开」；
- 无成交量约束（不校验 bar 成交量是否够成交）、无排队与部分成交；
- ``fill_mode="open"`` 用的是**当日**开盘价；若需要「次日开盘」成交，应改用挂单队列（本项目未实现）。
"""

import math
from typing import Any, Dict, List, Optional

from ..core.calendar import TradingCalendar
from ..core.errors import ValidationError
from ..core.models import Bar, FeeConfig, OrderRequest, Trade
from .portfolio import SimAccount, SIDE_BUY, SIDE_SELL, _EPS

__all__ = ["SimulatedBroker", "DEFAULT_LIMIT_PCT"]

# 涨跌停判定阈值（%）：主板 10%，留 0.2 个百分点的容错（四舍五入 / 复权误差）
DEFAULT_LIMIT_PCT = 9.8

_FILL_MODES = ("close", "open")


class SimulatedBroker:
    """日线级模拟撮合器（无第三方依赖、结果完全确定）。"""

    name = "simulated"
    is_live = False

    def __init__(
        self,
        fee: Optional[FeeConfig] = None,
        calendar: Optional[TradingCalendar] = None,
        fill_mode: str = "close",
        limit_pct: float = DEFAULT_LIMIT_PCT,
    ):
        if fill_mode not in _FILL_MODES:
            raise ValidationError(
                "fill_mode 仅支持 close / open，当前为 %r" % (fill_mode,), field="fill_mode"
            )
        self.fee = fee or FeeConfig()
        self.calendar = calendar or TradingCalendar()
        self.fill_mode = fill_mode
        self.limit_pct = float(limit_pct or DEFAULT_LIMIT_PCT)
        self.lot_size = int(self.fee.lot_size or 100)
        # 跟踪上一根 bar 的收盘价，用于涨跌停判定（见 track_bars）
        self._last_bar: Dict[str, Bar] = {}
        self._prev_close: Dict[str, float] = {}
        self.rejects: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ 行情跟踪
    def track_bars(self, date: str, bars: Dict[str, Bar]) -> None:
        """引擎在每个交易日**撮合前**调用：记录上一根 bar 收盘价（涨跌停基准）。"""
        for code in sorted(bars.keys()):
            bar = bars.get(code)
            if bar is None:
                continue
            last = self._last_bar.get(code)
            if last is not None and last.date < bar.date and last.close > 0:
                self._prev_close[code] = float(last.close)
            self._last_bar[code] = bar

    def prev_close(self, code: str, bar: Optional[Bar] = None) -> float:
        """取得涨跌停判定用的前收盘价（无历史返回 0.0）。"""
        value = self._prev_close.get(code)
        if value is None and bar is not None and bar.change_pct:
            # 无上一根 bar 时，用数据源给出的涨跌幅反推前收盘
            pct = float(bar.change_pct)
            if abs(pct) > 1e-9 and abs(pct) < 1000:
                base = float(bar.close) / (1.0 + pct / 100.0)
                return base if base > 0 else 0.0
        return float(value or 0.0)

    def reset(self) -> None:
        """清空行情记忆与拒绝记录（复用同一个 broker 跑多次回测时调用）。"""
        self._last_bar = {}
        self._prev_close = {}
        self.rejects = []

    # ------------------------------------------------------------------ 费用
    def calc_fee(self, side: str, amount: float) -> float:
        """按 A 股主流费率计算单笔费用（元，保留 2 位小数）。

        佣金 ``max(成交额×万分之2.5, 5)`` + 印花税 ``成交额×0.05%``（仅卖出）
        + 过户费 ``成交额×0.001%``（双边）；``amount <= 0`` 返回 0.0，结果非负。
        """
        if side not in (SIDE_BUY, SIDE_SELL):
            raise ValidationError("委托方向只能是 buy / sell，当前为 %r" % (side,), field="side")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ValidationError("成交额必须是数字", field="amount")
        if math.isnan(amount) or math.isinf(amount) or amount <= 0:
            return 0.0
        cfg = self.fee
        commission = round(max(amount * float(cfg.commission_rate), float(cfg.commission_min)), 2)
        stamp = round(amount * float(cfg.stamp_duty_rate), 2) if side == SIDE_SELL else 0.0
        transfer = round(amount * float(cfg.transfer_fee_rate), 2)
        total = round(commission + stamp + transfer, 2)
        return total if total > 0 else 0.0

    def describe_fee(self) -> str:
        """人类可读的费用说明（供前端 / README 展示）。"""
        cfg = self.fee
        return (
            "佣金：成交额 × %.5f%%（单笔最低 %.2f 元，双边）；印花税：成交额 × %.4f%%（仅卖出）；"
            "过户费：成交额 × %.4f%%（双边）；滑点：%.1f bp（买入上浮 / 卖出下浮，成交价保留 2 位小数）；"
            "最小交易单位：%d 股"
            % (
                float(cfg.commission_rate) * 100.0,
                float(cfg.commission_min),
                float(cfg.stamp_duty_rate) * 100.0,
                float(cfg.transfer_fee_rate) * 100.0,
                float(cfg.slippage_bps),
                self.lot_size,
            )
        )

    # ------------------------------------------------------------------ 价格/限制
    def base_price(self, bar: Bar) -> float:
        """未计滑点的基准成交价。"""
        price = float(bar.open if self.fill_mode == "open" else bar.close)
        return price

    def fill_price(self, side: str, bar: Bar) -> float:
        """计入滑点后的成交价（买入上浮、卖出下浮，保留 2 位小数）。"""
        base = self.base_price(bar)
        if base <= 0:
            return 0.0
        ratio = 1.0 + (float(self.fee.slippage_bps) / 10000.0)
        price = base * (ratio if side == SIDE_BUY else (2.0 - ratio))
        return round(price, 2)

    def limit_reason(self, code: str, bar: Bar, side: str) -> str:
        """返回涨跌停拒绝原因；可成交返回空字符串。"""
        base = self.base_price(bar)
        if base <= 0:
            return ""
        prev = self.prev_close(code, bar)
        if prev > 0:
            pct = (base / prev - 1.0) * 100.0
        else:
            pct = float(bar.change_pct or 0.0)
        if pct >= self.limit_pct and side == SIDE_BUY:
            return "涨停（%s 相对前收盘 %+.2f%%）不予买入" % (bar.date, pct)
        if pct <= -self.limit_pct and side == SIDE_SELL:
            return "跌停（%s 相对前收盘 %+.2f%%）不予卖出" % (bar.date, pct)
        return ""

    def is_limit_blocked(self, code: str, bar: Bar, side: str) -> bool:
        return bool(self.limit_reason(code, bar, side))

    # ------------------------------------------------------------------ 撮合
    def execute(
        self,
        order: OrderRequest,
        bar: Bar,
        account: SimAccount,
        name: str = "",
        date: str = "",
    ) -> Optional[Trade]:
        """撮合一笔委托；成交返回 :class:`Trade`，未成交（含被规则拒绝）返回 ``None``。

        规则见模块 docstring：收盘价成交 + 滑点、整手取整、资金缩量、T+1 可卖限制、涨跌停不成交。
        """
        if order is None:
            return None
        if order.side not in (SIDE_BUY, SIDE_SELL):
            raise ValidationError("委托方向只能是 buy / sell，当前为 %r" % (order.side,), field="side")
        if bar is None:
            self._reject(date, order, "无当日行情（停牌/数据缺失）")
            return None
        price = self.fill_price(order.side, bar)
        if price <= 0:
            self._reject(date, order, "成交价非法（基准价 <= 0）")
            return None
        reason = self.limit_reason(order.code, bar, order.side)
        if reason:
            self._reject(date, order, reason)
            return None

        qty = self._round_lot(order.qty)
        if qty <= 0:
            self._reject(date, order, "委托数量不足 1 手（%d 股）" % self.lot_size)
            return None

        if order.side == SIDE_BUY:
            qty = min(qty, self.max_affordable_qty(price, account.cash))
            if qty <= 0:
                self._reject(date, order, "可用资金不足（现金 %.2f 元，价 %.2f 元）" % (account.cash, price))
                return None
        else:
            pos = account.positions.get(order.code)
            available = int(pos.available_qty) if pos is not None else 0
            qty = min(qty, self._round_lot(available))
            if qty <= 0:
                self._reject(date, order, "可卖数量不足（T+1 或空仓，可卖 %d 股）" % available)
                return None

        amount = price * qty
        fee = self.calc_fee(order.side, amount)
        return account.apply_fill(
            code=order.code,
            name=name or order.code,
            side=order.side,
            price=price,
            qty=qty,
            fee=fee,
            date=date or bar.date,
            reason=order.reason,
        )

    # ------------------------------------------------------------------ 内部
    def _round_lot(self, qty: Any) -> int:
        try:
            value = int(qty or 0)
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return 0
        return (value // self.lot_size) * self.lot_size

    def max_affordable_qty(self, price: float, cash: float) -> int:
        """按可用资金能买的最大整手数（含费用，预留最低佣金）。"""
        price = float(price or 0.0)
        cash = float(cash or 0.0)
        if price <= 0 or cash <= 0:
            return 0
        cfg = self.fee
        variable = float(cfg.commission_rate) + float(cfg.transfer_fee_rate)
        budget = cash - float(cfg.commission_min)
        if budget <= 0:
            return 0
        qty = self._round_lot(int(budget / (price * (1.0 + variable))))
        guard = 0
        while qty > 0 and guard < 1000000:
            amount = price * qty
            fee = self.calc_fee(SIDE_BUY, amount)
            if amount + fee <= cash + _EPS:
                break
            qty -= self.lot_size
            guard += 1
        return max(qty, 0)

    def _reject(self, date: str, order: OrderRequest, reason: str) -> None:
        self.rejects.append({
            "date": date or "",
            "code": order.code,
            "side": order.side,
            "qty": int(order.qty or 0),
            "reason": reason,
        })

    # ------------------------------------------------------------------ 统计
    def reject_summary(self) -> Dict[str, int]:
        """按原因归类统计未成交笔数（用于回测结果提示）。"""
        summary: Dict[str, int] = {}
        for item in self.rejects:
            key = item["reason"]
            summary[key] = summary.get(key, 0) + 1
        return summary
