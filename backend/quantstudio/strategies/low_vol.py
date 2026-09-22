# -*- coding: utf-8 -*-
"""低波动率优选策略（st_lowvol，多标的）。

信号逻辑
--------
- 每 ``rebalance_days``（默认 5 个交易日 ≈ 周度）调仓一次；
- 用最近 ``lookback``（默认 60）个交易日的**日收益样本波动率**排序，取波动率最低的 ``top_n``（默认 5）只等权持有；
- 调仓日先卖出不在目标名单的持仓，再按 ``总资产 × position_pct / top_n`` 的目标市值买入 / 补仓；
- 波动率并列时按代码升序，保证可复现；停牌标的当日不参与调仓。
"""

from typing import Dict, List

from ..core.errors import ValidationError
from ..core.models import Bar, OrderRequest
from .base import BaseStrategy


class LowVolStrategy(BaseStrategy):
    id = "st_lowvol"
    name = "低波动率优选策略"
    category = "稳健"
    desc = "从候选池中挑选过去 60 日波动率最低的 5 只股票等权持有，每 5 个交易日调仓，追求低回撤。"
    universe = "中证800成分股 / 自选股池（多标的）"
    universe_type = "multi"
    freq = "日线"
    min_bars = 62
    version = "1.0"
    default_params = {"lookback": 60, "top_n": 5, "rebalance_days": 5, "position_pct": 0.95}
    default_symbols = [
        "600519.SH", "300750.SZ", "002594.SZ", "600036.SH", "601318.SH",
        "000858.SZ", "600900.SH", "601012.SH", "600276.SH", "300059.SZ",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._days = 0
        self._last_rebalance = 0

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "lookback": {
                "label": "波动率回看天数", "type": "int", "default": 60, "min": 10, "max": 250, "step": 1,
                "help": "用最近多少个交易日的日收益计算波动率",
            },
            "top_n": {
                "label": "持仓数量", "type": "int", "default": 5, "min": 1, "max": 20, "step": 1,
                "help": "每次调仓持有波动率最低的标的数量（等权）",
            },
            "rebalance_days": {
                "label": "调仓间隔", "type": "int", "default": 5, "min": 1, "max": 60, "step": 1,
                "help": "多少个交易日调仓一次（5 ≈ 周度）",
            },
            "position_pct": {
                "label": "总仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                "help": "调仓后目标持仓占总资产的比例",
            },
        }

    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        # 波动率的样本标准差至少需要 2 个日收益（schema 的 min=10 已覆盖，此处保留钩子供扩展）
        if int(params["lookback"]) < 2:
            raise ValidationError("波动率回看天数至少为 2", field="lookback")

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
                continue
            hist = ctx.history(code, "close", lookback + 1)
            if len(hist) < lookback + 1:
                continue
            rets = []
            for i in range(1, len(hist)):
                if hist[i - 1] <= 0:
                    rets = []
                    break
                rets.append(hist[i] / hist[i - 1] - 1.0)
            if len(rets) < lookback:
                continue
            vol = self.std(rets, lookback)
            if vol is None:
                continue
            scores[code] = vol
        if not scores:
            return []

        self._last_rebalance = self._days
        ranked = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))[:top_n]
        targets = [code for code, _ in ranked]
        orders: List[OrderRequest] = []

        released_cash = 0.0
        for pos in ctx.positions():
            if pos.code not in targets and pos.available_qty > 0:
                order = self.sell_order(ctx, pos.code, reason="波动率升出最低 %d 名" % top_n)
                if order is not None:
                    orders.append(order)
                    released_cash += pos.market_value

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
                reason="波动率最低前 %d 名（%.2f%%），等权 %.0f%%"
                       % (top_n, scores[code] * 100.0, 100.0 / len(targets)),
            ))
        return orders
