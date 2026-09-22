# -*- coding: utf-8 -*-
"""网格交易策略（st_grid，单标的）。

信号逻辑
--------
- 中枢 = 回测区间内**首个 bar 的收盘价**；
- 档位 ``level = floor(ln(price / 中枢) / ln(1 + grid_pct))``，并限幅到 ``[-levels, +levels]``；
- **跌破一档**（level 下降）→ 买入 ``base_qty × |level|`` 股；
- **涨过一档**（level 上升）→ 卖出 ``base_qty × |level|`` 股（不超过当日可卖数量，受 T+1 限制）；
- 数量由撮合层按 ``lot_size`` 向下取整，资金不足时按可用资金缩量；
- 网格策略在震荡行情中低买高卖，单边下跌会持续加仓（可用 ``levels`` 控制最大档位）。
"""

import math
from typing import Dict, List

from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class GridStrategy(BaseStrategy):
    id = "st_grid"
    name = "网格交易策略"
    category = "震荡市"
    desc = "以首个 bar 收盘价为中枢按 3% 分档，跌破一档买入 base_qty×档位，涨过一档卖出，赚取震荡波段收益。"
    universe = "震荡区间标的（单标的）"
    universe_type = "single"
    freq = "日线"
    min_bars = 20
    version = "1.0"
    default_params = {"grid_pct": 0.03, "levels": 10, "base_qty": 1000}
    default_symbols = ["600519.SH", "600036.SH", "600900.SH"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._center = 0.0
        self._level = 0

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "grid_pct": {
                "label": "网格间距", "type": "float", "default": 0.03, "min": 0.005, "max": 0.2, "step": 0.005,
                "help": "相邻网格的价格间距（0.03 = 3%）",
            },
            "levels": {
                "label": "最大档位", "type": "int", "default": 10, "min": 1, "max": 50, "step": 1,
                "help": "中枢上下的最大网格档数（限制极端行情下的仓位）",
            },
            "base_qty": {
                "label": "每档股数", "type": "int", "default": 1000, "min": 100, "max": 100000, "step": 100,
                "help": "每档基准股数（实际下单 = 每档股数 × 档位）",
            },
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        code = self.symbols[0] if self.symbols else ""
        bar = bars.get(code)
        if bar is None or bar.close <= 0:
            return []
        price = float(bar.close)
        if self._center <= 0:
            self._center = price
            ctx.log("网格中枢 = 首个 bar 收盘价 %.2f（%s）" % (price, ctx.today))
            return []

        grid_pct = float(self.params["grid_pct"])
        levels = int(self.params["levels"])
        base_qty = int(self.params["base_qty"])
        if grid_pct <= 0 or levels <= 0:
            return []

        raw_level = int(math.floor(math.log(price / self._center) / math.log(1.0 + grid_pct)))
        level = max(-levels, min(levels, raw_level))
        if level == self._level:
            return []

        orders: List[OrderRequest] = []
        if level < self._level:
            order = self.buy_order(
                ctx, code, price, 1.0,
                "网格跌破第 %d 档（中枢 %.2f → 现价 %.2f）" % (abs(level), self._center, price),
            )
            if order is not None:
                order.qty = max(base_qty * abs(level), 0)   # 档位越深买入越多
                orders.append(order)
        else:
            order = self.sell_order(
                ctx, code, base_qty * abs(level),
                "网格涨过第 %d 档（中枢 %.2f → 现价 %.2f）" % (abs(level), self._center, price),
            )
            if order is not None:
                orders.append(order)
        if orders:
            self._level = level
        return orders
