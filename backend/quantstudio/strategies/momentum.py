# -*- coding: utf-8 -*-
"""动量轮动策略（st_momentum，多标的）。

信号逻辑
--------
- 每 ``rebalance_days``（默认 20 个交易日 ≈ 月度）调仓一次；
- 用最近 ``lookback``（默认 20）个交易日涨幅排序，取涨幅最高的 ``top_n``（默认 3）只，**等权**持有；
- 调仓日：先卖出不在目标名单里的持仓（先卖后买，卖出资金当日即可用于买入），
  再按 ``总资产 × position_pct / top_n`` 的目标市值买入 / 补仓，现金不足时按剩余现金缩量；
- 排名并列时按代码升序，保证结果可复现；停牌（当日无 bar）的标的当日不参与调仓。
"""

from typing import Dict, List

from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    id = "st_momentum"
    name = "动量轮动策略"
    category = "动量"
    desc = "按过去 20 个交易日涨幅对候选池排序，持有动量最强的 3 只标的，每 20 个交易日（约月度）调仓。"
    universe = "宽基指数 / 行业 ETF / 自选股池（多标的）"
    universe_type = "multi"
    freq = "日线"
    min_bars = 22
    version = "1.0"
    default_params = {"lookback": 20, "top_n": 3, "rebalance_days": 20, "position_pct": 0.95}
    default_symbols = [
        "600519.SH", "300750.SZ", "002594.SZ", "600036.SH", "601318.SH",
        "000858.SZ", "600900.SH", "601012.SH",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._days = 0
        self._last_rebalance = 0

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "lookback": {
                "label": "动量回看天数", "type": "int", "default": 20, "min": 2, "max": 250, "step": 1,
                "help": "用最近多少个交易日的涨幅排序",
            },
            "top_n": {
                "label": "持仓数量", "type": "int", "default": 3, "min": 1, "max": 20, "step": 1,
                "help": "每次调仓持有的标的数量（等权）",
            },
            "rebalance_days": {
                "label": "调仓间隔", "type": "int", "default": 20, "min": 1, "max": 120, "step": 1,
                "help": "多少个交易日调仓一次（20 ≈ 月度）",
            },
            "position_pct": {
                "label": "总仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                "help": "调仓后目标持仓占总资产的比例",
            },
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        self._days += 1
        lookback = int(self.params["lookback"])
        top_n = int(self.params["top_n"])
        interval = int(self.params["rebalance_days"])
        pct = float(self.params["position_pct"])

        if self._days - self._last_rebalance < interval:
            return []

        scores = {}
        for code in self.symbols:
            if code not in bars:
                continue                      # 停牌/无当日行情，本次不参与
            hist = ctx.history(code, "close", lookback + 1)
            if len(hist) < lookback + 1 or hist[0] <= 0:
                continue
            scores[code] = hist[-1] / hist[0] - 1.0
        if not scores:
            return []

        self._last_rebalance = self._days
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        targets = [code for code, _ in ranked]
        orders: List[OrderRequest] = []

        # 1) 先卖出不在目标名单中的持仓（卖出所得当日可用于买入）
        released_cash = 0.0
        for pos in ctx.positions():
            if pos.code not in targets and pos.available_qty > 0:
                order = self.sell_order(ctx, pos.code, reason="跌出动量前 %d 名" % top_n)
                if order is not None:
                    orders.append(order)
                    released_cash += pos.market_value

        # 2) 等权买入 / 补仓
        budget = ctx.total_assets() * pct / max(len(targets), 1)
        cash_pool = (ctx.cash() + released_cash) * pct
        for code in targets:
            bar = bars.get(code)
            if bar is None or bar.close <= 0:
                continue
            price = float(bar.close)
            pos = ctx.position(code)
            current = float(pos.market_value) if pos is not None else 0.0
            need = budget - current
            lot = self.lot_size
            if need < price * lot:
                continue
            qty = int(need / price // lot) * lot
            max_qty = int(cash_pool / (price * 1.001) // lot) * lot
            qty = min(qty, max_qty)
            if qty <= 0:
                continue
            cash_pool -= qty * price * 1.001
            orders.append(OrderRequest(
                code=code, side="buy", qty=qty, price=None,
                reason="动量排名前 %d（%d 日涨幅 %+.2f%%），等权 %.0f%%"
                       % (top_n, lookback, scores[code] * 100.0, 100.0 / len(targets)),
            ))
        return orders
