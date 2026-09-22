# -*- coding: utf-8 -*-
"""RSI 均值回归策略（st_rsi）。

信号逻辑
--------
- ``RSI(rsi_period)`` 使用 Wilder 平滑，基于**截至今日**的收盘价（严格无未来函数）；
- ``RSI < buy_th``（默认 30，超卖）且当前空仓 → 用 ``position_pct`` 的可用资金**全仓买入**；
- ``RSI > sell_th``（默认 70，超买）且有持仓 → **清仓**（卖出全部可卖数量，受 T+1 限制）；
- 超卖区不重复加仓（保持单一买点），适合震荡行情；单边趋势中可能长期空仓或错过趋势。
"""

from typing import Dict, List

from ..core.errors import ValidationError
from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class RsiReversionStrategy(BaseStrategy):
    id = "st_rsi"
    name = "RSI 均值回归策略"
    category = "均值回归"
    desc = "RSI(14) 低于 30 超卖买入（全仓），高于 70 超买清仓，适用于震荡行情。"
    universe = "自选股票池（可单标的或多标的）"
    universe_type = "single"
    freq = "日线"
    min_bars = 15
    version = "1.0"
    default_params = {"rsi_period": 14, "buy_th": 30, "sell_th": 70, "position_pct": 0.95}
    default_symbols = ["600519.SH", "000858.SZ", "600036.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "rsi_period": {
                "label": "RSI 周期", "type": "int", "default": 14, "min": 2, "max": 120, "step": 1,
                "help": "RSI 计算周期（交易日）",
            },
            "buy_th": {
                "label": "买入阈值", "type": "float", "default": 30, "min": 1, "max": 50, "step": 1,
                "help": "RSI 低于该值视为超卖，触发买入",
            },
            "sell_th": {
                "label": "卖出阈值", "type": "float", "default": 70, "min": 50, "max": 99, "step": 1,
                "help": "RSI 高于该值视为超买，触发清仓",
            },
            "position_pct": {
                "label": "买入仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                "help": "买入时使用的可用资金比例",
            },
        }

    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        if float(params["buy_th"]) >= float(params["sell_th"]):
            raise ValidationError(
                "买入阈值（%s）必须小于卖出阈值（%s）" % (params["buy_th"], params["sell_th"]),
                field="buy_th",
            )

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        period = int(self.params["rsi_period"])
        buy_th = float(self.params["buy_th"])
        sell_th = float(self.params["sell_th"])
        pct = float(self.params["position_pct"])
        orders: List[OrderRequest] = []

        for code in self.symbols:
            bar = bars.get(code)
            if bar is None:
                continue
            hist = ctx.history(code, "close", period + 1)
            value = self.rsi(hist, period)
            if value is None:
                continue
            pos = ctx.position(code)
            if value < buy_th:
                if pos is None:
                    order = self.buy_order(
                        ctx, code, bar.close, pct, "RSI(%d)=%.1f < %.0f 超卖买入" % (period, value, buy_th)
                    )
                    if order is not None:
                        orders.append(order)
            elif value > sell_th:
                order = self.sell_order(
                    ctx, code, reason="RSI(%d)=%.1f > %.0f 超买清仓" % (period, value, sell_th)
                )
                if order is not None:
                    orders.append(order)
        return orders
