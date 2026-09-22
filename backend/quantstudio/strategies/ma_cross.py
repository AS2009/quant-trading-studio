# -*- coding: utf-8 -*-
"""双均线趋势策略（st_ma_cross）。

信号逻辑
--------
- 金叉：昨日 ``MA(short) ≤ MA(long)`` 且今日 ``MA(short) > MA(long)`` → **用 position_pct 的可用资金全仓买入**；
- 死叉：昨日 ``MA(short) ≥ MA(long)`` 且今日 ``MA(short) < MA(long)`` → **清仓**（卖出全部可卖数量）；
- 已持仓不重复买入；指标按**截至今日**的历史计算（严格无未来函数）。
"""

from typing import Dict, List

from ..core.errors import ValidationError
from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class MaCrossStrategy(BaseStrategy):
    id = "st_ma_cross"
    name = "双均线趋势策略"
    category = "趋势跟踪"
    desc = "MA20 上穿 MA60 产生买入信号，下穿产生卖出信号，适合单边趋势行情。"
    universe = "沪深300成分股 / 自选股（单标的即可）"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"short_ma": 20, "long_ma": 60, "position_pct": 0.95}
    default_symbols = ["600519.SH", "000858.SZ", "300750.SZ"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "short_ma": {
                "label": "短期均线", "type": "int", "default": 20, "min": 2, "max": 250, "step": 1,
                "help": "短周期均线天数（交易日）",
            },
            "long_ma": {
                "label": "长期均线", "type": "int", "default": 60, "min": 3, "max": 500, "step": 1,
                "help": "长周期均线天数（交易日），必须大于短期均线",
            },
            "position_pct": {
                "label": "买入仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                "help": "金叉时使用的可用资金比例（1.0 = 满仓）",
            },
        }

    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        if int(params["short_ma"]) >= int(params["long_ma"]):
            raise ValidationError(
                "短期均线（%s）必须小于长期均线（%s）" % (params["short_ma"], params["long_ma"]),
                field="short_ma",
            )

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        short_n = int(self.params["short_ma"])
        long_n = int(self.params["long_ma"])
        pct = float(self.params["position_pct"])
        orders: List[OrderRequest] = []

        for code in self.symbols:
            bar = bars.get(code)
            if bar is None:
                continue
            hist = ctx.history(code, "close", long_n + 2)
            if len(hist) < long_n + 1:
                continue
            short_now = self.ma(hist, short_n)
            long_now = self.ma(hist, long_n)
            short_prev = self.ma(hist[:-1], short_n)
            long_prev = self.ma(hist[:-1], long_n)
            if None in (short_now, long_now, short_prev, long_prev):
                continue

            pos = ctx.position(code)
            if short_prev <= long_prev and short_now > long_now:
                if pos is None:
                    order = self.buy_order(
                        ctx, code, bar.close, pct,
                        "MA%d 上穿 MA%d（%.2f > %.2f）" % (short_n, long_n, short_now, long_now),
                    )
                    if order is not None:
                        orders.append(order)
            elif short_prev >= long_prev and short_now < long_now:
                order = self.sell_order(
                    ctx, code, reason="MA%d 下穿 MA%d（%.2f < %.2f）" % (short_n, long_n, short_now, long_now)
                )
                if order is not None:
                    orders.append(order)
        return orders
