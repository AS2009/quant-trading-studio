# -*- coding: utf-8 -*-
"""模拟账户与持仓（回测 / 模拟盘共用的记账内核）。

口径（务必与 README 一致）
--------------------------
- 金额单位：元；价格单位：元/股；``return_pct`` 一律为**百分比**（5.0 表示 +5%）。
- 成本价（``cost``）：**摊薄成本价（含买入费用）**。买入后
  ``cost = (原成本金额 + 成交额 + 费用) / 新持仓数量``。
- 卖出实现盈亏：``pnl = 成交额 - 费用 - 加权平均成本 × 卖出数量``，
  ``ret_pct = pnl / (加权平均成本 × 卖出数量) × 100``。
- **T+1**：买入当日数量计入 ``qty`` 但不计入 ``available_qty``（不可卖）；
  每个交易日开盘前调用 :meth:`SimAccount.unlock_all` 解禁。
- ``frozen_cash`` 为冻结资金（回测中通常恒为 0，为模拟盘挂单预留）；
  ``equity() = cash + frozen_cash + 持仓市值``。
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List

from ..core.costs import EPS as _EPS          # 与 costs 的资金校验同一容差（单一来源）
from ..core.errors import OrderRejected, ValidationError
from ..core.models import Trade

# 资金校验容差见 quantstudio.core.costs.EPS（避免两侧各写一个常量而漂移）

SIDE_BUY = "buy"
SIDE_SELL = "sell"


def _finite(value: float) -> float:
    """把 NaN/Infinity 归一为 0.0，保证对外序列化安全。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(num) or math.isinf(num):
        return 0.0
    return num


def _round2(value: float) -> float:
    return round(_finite(value), 2)


@dataclass
class SimPosition:
    """回测用持仓（与 ``core.models.Position`` 字段对齐，数值口径相同）。"""

    code: str
    name: str = ""
    qty: int = 0
    available_qty: int = 0
    cost: float = 0.0            # 摊薄成本价（含费用）
    price: float = 0.0           # 最新价
    market_value: float = 0.0    # 最新市值
    total_pnl: float = 0.0       # 浮动盈亏（元）
    return_pct: float = 0.0      # 浮动收益率（%）

    @property
    def cost_value(self) -> float:
        """持仓成本金额（元）。"""
        return _finite(self.cost) * int(self.qty)

    def mark(self, price: float) -> None:
        """按最新价刷新市值 / 浮盈 / 收益率。"""
        self.price = _finite(price)
        self.market_value = self.price * int(self.qty)
        cost_value = self.cost_value
        self.total_pnl = self.market_value - cost_value
        self.return_pct = (self.total_pnl / cost_value * 100.0) if cost_value > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "qty": int(self.qty),
            "available_qty": int(self.available_qty),
            "cost": _round2(self.cost),
            "price": _round2(self.price),
            "market_value": _round2(self.market_value),
            "cost_value": _round2(self.cost_value),
            "total_pnl": _round2(self.total_pnl),
            "return_pct": round(_finite(self.return_pct), 2),
        }


class SimAccount:
    """模拟账户：现金、持仓、成交流水、费用与换手统计。"""

    def __init__(self, initial_cash: float = 1_000_000.0, lot_size: int = 100):
        initial_cash = _finite(initial_cash)
        if initial_cash <= 0:
            raise ValidationError("初始资金必须为正数", field="initial_cash")
        lot_size = int(lot_size or 0)
        if lot_size <= 0:
            raise ValidationError("最小交易单位必须为正整数", field="lot_size")
        self.initial_cash = initial_cash
        self.cash = initial_cash                  # 可用资金
        self.frozen_cash = 0.0                    # 冻结资金（模拟盘挂单）
        self.lot_size = lot_size
        self.positions: Dict[str, SimPosition] = {}
        self.trades: List[Trade] = []
        self.fees_total = 0.0                     # 累计费用
        self.turnover = 0.0                       # 累计成交额（买 + 卖）

    # ------------------------------------------------------------------ 行情
    def mark_to_market(self, prices: Dict[str, float]) -> None:
        """按最新价更新所有持仓的市值与浮盈（缺失价格的标的保持原价）。"""
        if not prices:
            return
        for code in sorted(self.positions.keys()):
            price = prices.get(code)
            if price is None or _finite(price) <= 0:
                continue
            self.positions[code].mark(price)

    def unlock_all(self) -> None:
        """每日开盘前调用：T+1 解禁，可用数量 = 持仓数量。"""
        for pos in self.positions.values():
            pos.available_qty = int(pos.qty)

    # ------------------------------------------------------------------ 资金
    def freeze_cash(self, amount: float) -> None:
        """冻结资金（模拟盘挂单用；回测不调用）。"""
        amount = _finite(amount)
        if amount < 0:
            raise ValidationError("冻结金额不能为负", field="amount")
        if amount > self.cash + _EPS:
            raise OrderRejected("可用资金不足，无法冻结 %.2f 元（可用 %.2f 元）" % (amount, self.cash))
        self.cash -= amount
        self.frozen_cash += amount

    def release_cash(self, amount: float) -> None:
        """解冻资金。"""
        amount = _finite(amount)
        if amount < 0:
            raise ValidationError("解冻金额不能为负", field="amount")
        amount = min(amount, self.frozen_cash)
        self.frozen_cash -= amount
        self.cash += amount

    # ------------------------------------------------------------------ 计价
    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    def equity(self) -> float:
        """总资产 = 可用资金 + 冻结资金 + 持仓市值。"""
        return self.cash + self.frozen_cash + self.market_value

    def total_pnl(self) -> float:
        return self.equity() - self.initial_cash

    def snapshot(self, date: str = "") -> Dict[str, Any]:
        """单日快照：``{date, equity, cash, market_value}``（金额保留 2 位小数）。"""
        return {
            "date": date,
            "equity": _round2(self.equity()),
            "cash": _round2(self.cash),
            "market_value": _round2(self.market_value),
        }

    # ------------------------------------------------------------------ 成交
    def apply_fill(
        self,
        code: str,
        name: str,
        side: str,
        price: float,
        qty: int,
        fee: float = 0.0,
        date: str = "",
        reason: str = "",
    ) -> Trade:
        """成交记账（唯一的持仓/资金变更入口）。

        买入：占资金（成交额 + 费用），摊薄成本含费用，当日买入**不计入**可卖数量（T+1）。
        卖出：仅能卖出 ``available_qty`` 以内数量，按加权平均成本计算实现盈亏。
        资金/数量校验失败抛 :class:`~quantstudio.core.errors.OrderRejected`。
        """
        if side not in (SIDE_BUY, SIDE_SELL):
            raise ValidationError("委托方向只能是 buy / sell，当前为 %r" % (side,), field="side")
        price = _finite(price)
        fee = _finite(fee)
        if price <= 0:
            raise OrderRejected("成交价非法：%r（%s）" % (price, code))
        if fee < 0:
            raise ValidationError("费用不能为负：%.4f" % fee, field="fee")
        qty = int(qty or 0)
        if qty <= 0:
            raise OrderRejected("成交数量必须为正数：%r（%s）" % (qty, code))
        amount = price * qty

        if side == SIDE_BUY:
            total = amount + fee
            if total > self.cash + _EPS:
                raise OrderRejected(
                    "资金不足：需 %.2f 元（成交额 %.2f + 费用 %.2f），可用 %.2f 元"
                    % (total, amount, fee, self.cash)
                )
            pos = self.positions.get(code)
            if pos is None:
                pos = SimPosition(code=code, name=name or code)
                self.positions[code] = pos
            old_cost_value = pos.cost_value
            pos.qty = int(pos.qty) + qty
            # 摊薄成本：把买入费用一并摊入成本价
            pos.cost = (old_cost_value + amount + fee) / pos.qty if pos.qty else 0.0
            if name:
                pos.name = name
            pos.mark(self._fill_price_or_last(code, price))
            self.cash -= total
            if -_EPS < self.cash < 0.0:      # 容差内的浮点噪声（如 -8.9e-16）→ 归零，保证现金非负
                self.cash = 0.0
            trade = Trade(
                date=date, code=code, name=pos.name or code, side=SIDE_BUY,
                price=_round2(price), qty=qty, amount=_round2(amount), fee=_round2(fee),
                pnl=None, ret_pct=None, reason=reason,
            )
        else:
            pos = self.positions.get(code)
            if pos is None or pos.qty <= 0:
                raise OrderRejected("无持仓可卖：%s" % code)
            if qty > pos.available_qty:
                raise OrderRejected(
                    "可卖数量不足（T+1）：%s 可卖 %d 股，试图卖出 %d 股"
                    % (code, pos.available_qty, qty)
                )
            cost_value = pos.cost * qty
            net = amount - fee
            pnl = net - cost_value
            ret_pct = (pnl / cost_value * 100.0) if cost_value > 0 else 0.0
            pos.qty = int(pos.qty) - qty
            pos.available_qty = int(pos.available_qty) - qty
            self.cash += net
            if pos.qty <= 0:
                self.positions.pop(code, None)
            else:
                pos.mark(self._fill_price_or_last(code, price))
            trade = Trade(
                date=date, code=code, name=pos.name or code, side=SIDE_SELL,
                price=_round2(price), qty=qty, amount=_round2(amount), fee=_round2(fee),
                pnl=_round2(pnl), ret_pct=round(ret_pct, 2), reason=reason,
            )

        self.trades.append(trade)
        self.fees_total += fee
        self.turnover += amount
        return trade

    # ------------------------------------------------------------------ 内部
    def _fill_price_or_last(self, code: str, fallback: float) -> float:
        pos = self.positions.get(code)
        if pos is not None and pos.price > 0:
            return pos.price
        return fallback

    def to_dict(self, date: str = "") -> Dict[str, Any]:
        """账户快照（持仓明细 + 汇总），金额保留 2 位小数。"""
        data = self.snapshot(date)
        data.update({
            "initial_cash": _round2(self.initial_cash),
            "frozen_cash": _round2(self.frozen_cash),
            "fees_total": _round2(self.fees_total),
            "turnover": _round2(self.turnover),
            "total_pnl": _round2(self.total_pnl()),
            "positions": [p.to_dict() for p in self.positions.values()],
        })
        return data
