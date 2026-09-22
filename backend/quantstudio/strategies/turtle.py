# -*- coding: utf-8 -*-
"""海龟突破策略（st_turtle，单标的）。

信号逻辑
--------
- **入场**：今日收盘价 > 之前 ``entry_high``（默认 20）个交易日的最高价（不含今日）→ 用 position_pct 买入；
- **出场**：
  - 跌破之前 ``exit_low``（默认 10）个交易日的最低价（不含今日），或
  - 触发 ATR 跟踪止损 ``持仓成本 - atr_mult × ATR(atr_period)``；
- 卖出全部可卖数量（受 T+1 限制）；ATR 用 Wilder 平滑，参数 ``atr_period`` 默认 14；
- 经典趋势跟踪框架：小亏多次、赚趋势的大波段；横盘期容易被反复止损。
"""

from typing import Dict, List

from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class TurtleStrategy(BaseStrategy):
    id = "st_turtle"
    name = "海龟突破策略"
    category = "趋势跟踪"
    desc = "价格突破 20 日高点开仓，跌破 10 日低点或触发 2 倍 ATR 跟踪止损离场，经典趋势跟踪框架。"
    universe = "趋势品种（股票 / 商品，单标的）"
    universe_type = "single"
    freq = "日线"
    min_bars = 22
    version = "1.0"
    default_params = {"entry_high": 20, "exit_low": 10, "atr_mult": 2.0, "atr_period": 14, "position_pct": 0.95}
    default_symbols = ["600519.SH", "300750.SZ", "601318.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "entry_high": {
                "label": "入场突破周期", "type": "int", "default": 20, "min": 2, "max": 250, "step": 1,
                "help": "突破过去多少个交易日的最高价入场",
            },
            "exit_low": {
                "label": "出场突破周期", "type": "int", "default": 10, "min": 2, "max": 250, "step": 1,
                "help": "跌破过去多少个交易日的最低价离场",
            },
            "atr_mult": {
                "label": "ATR 止损倍数", "type": "float", "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.5,
                "help": "跟踪止损 = 持仓成本 - atr_mult × ATR",
            },
            "atr_period": {
                "label": "ATR 周期", "type": "int", "default": 14, "min": 2, "max": 120, "step": 1,
                "help": "ATR（真实波幅）计算周期",
            },
            "position_pct": {
                "label": "买入仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                "help": "突破入场时使用的可用资金比例",
            },
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        entry_n = int(self.params["entry_high"])
        exit_n = int(self.params["exit_low"])
        atr_mult = float(self.params["atr_mult"])
        atr_n = int(self.params["atr_period"])
        pct = float(self.params["position_pct"])
        orders: List[OrderRequest] = []

        for code in self.symbols:
            bar = bars.get(code)
            if bar is None or bar.close <= 0:
                continue
            price = float(bar.close)

            highs = ctx.history(code, "high", entry_n + 1)[:-1]     # 不含今日
            lows = ctx.history(code, "low", exit_n + 1)[:-1]        # 不含今日
            atr_value = self.atr(ctx.bars_since(code, atr_n + 1), atr_n)
            pos = ctx.position(code)

            if pos is None:
                if len(highs) < entry_n:
                    continue
                prior_high = max(highs)
                if price > prior_high:
                    order = self.buy_order(
                        ctx, code, price, pct,
                        "突破 %d 日高点 %.2f（现价 %.2f）" % (entry_n, prior_high, price),
                    )
                    if order is not None:
                        orders.append(order)
                continue

            reasons = []
            if len(lows) >= exit_n:
                prior_low = min(lows)
                if price < prior_low:
                    reasons.append("跌破 %d 日低点 %.2f" % (exit_n, prior_low))
            if atr_value is not None and atr_value > 0 and pos.cost > 0:
                stop = pos.cost - atr_mult * atr_value
                if price <= stop:
                    reasons.append("触发 %.1f×ATR 止损（成本 %.2f - %.2f = %.2f）"
                                   % (atr_mult, pos.cost, atr_mult * atr_value, stop))
            if reasons:
                order = self.sell_order(ctx, code, reason="；".join(reasons))
                if order is not None:
                    orders.append(order)
        return orders
