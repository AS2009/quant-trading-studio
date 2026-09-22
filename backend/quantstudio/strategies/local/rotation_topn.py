# -*- coding: utf-8 -*-
"""动量轮动 TopN（st_rotation_topn）。   ← 多标的轮动示例：每 N 个交易日按动量排序等权调仓

信号逻辑
--------
- 调仓日（每 rebalance_days 个交易日，从回测首日起算）：按过去 lookback 个交易日涨幅对标的池降序排序；
- 先卖出不在前 top_n 的持仓（释放资金），再对每个目标标的按「可用资金 × position_pct / top_n」等权买入；
- 非调仓日不做任何操作；已持有且仍在榜内的标的不会被重复买入。

参数
----
- lookback: 动量回看窗口（交易日，默认 20）
- top_n: 持有标的数量（默认 3）
- rebalance_days: 调仓间隔（交易日，默认 20 ≈ 一个月）
- position_pct: 总仓位上限（默认 0.95，等权分配到 top_n 只）

风险与假设
----------
- 全部按收盘价成交，未考虑盘中滑价与流动性；
- 动量策略在震荡市容易「追高杀跌」，连续调仓可能放大换手与费用；
- 等权只按可用资金比例分配，未做波动率平价等风险加权。
"""

from typing import Dict, List

from ...core.errors import ValidationError
from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy


class RotationTopnStrategy(BaseStrategy):
    id = "st_rotation_topn"
    name = "动量轮动 TopN"
    category = "动量"
    desc = "每 N 个交易日按过去若干日涨幅排序，等权持有最强的 K 只标的。"
    universe = "自选股池（≥ top_n 只，多标的）"
    universe_type = "multi"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"lookback": 20, "top_n": 3, "rebalance_days": 20, "position_pct": 0.95}
    default_symbols = ["600519.SH", "000858.SZ", "300750.SZ", "600036.SH", "601318.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "lookback": {"label": "动量窗口", "type": "int", "default": 20, "min": 2, "max": 250, "step": 1,
                         "help": "按过去多少个交易日的涨幅排序"},
            "top_n": {"label": "持有数量", "type": "int", "default": 3, "min": 1, "max": 10, "step": 1,
                      "help": "等权持有的标的数量"},
            "rebalance_days": {"label": "调仓间隔", "type": "int", "default": 20, "min": 1, "max": 120, "step": 1,
                               "help": "每多少个交易日调仓一次（20 ≈ 一个月）"},
            "position_pct": {"label": "总仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                             "help": "调仓时投入的可用资金比例（等权分摊到 top_n 只）"},
        }

    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        if int(params["top_n"]) > 10:
            raise ValidationError("持有数量不能超过 10 只", field="top_n")
        if int(params["rebalance_days"]) < 1:
            raise ValidationError("调仓间隔至少 1 个交易日", field="rebalance_days")

    def on_start(self, ctx) -> None:
        super().on_start(ctx)
        self.days = 0          # 已处理 bar 数（跨日状态只保存「已发生」的事实）

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        self.days += 1
        rebalance_days = int(self.params["rebalance_days"])
        if self.days != 1 and (self.days - 1) % rebalance_days != 0:
            return []
        lookback = int(self.params["lookback"])
        top_n = int(self.params["top_n"])
        pct = float(self.params["position_pct"])

        scores: List[tuple] = []
        for code in bars:
            closes = ctx.history(code, "close", lookback + 1)
            momentum = self.pct_change(closes, lookback)
            if momentum is not None:
                scores.append((code, momentum))
        if not scores:
            return []
        scores.sort(key=lambda item: (-item[1], item[0]))     # 排序稳定：动量相同则按代码字典序
        targets = [code for code, _ in scores[:top_n]]
        ctx.log("调仓日 #%d：目标 %s" % (self.days, ", ".join(targets)))

        orders: List[OrderRequest] = []
        holdings = {pos.code: pos for pos in ctx.positions()}
        for code, pos in holdings.items():                    # 先卖后买，释放资金（并满足 T+1 约束）
            if code not in targets and code in bars:
                order = self.sell_order(ctx, code, reason="退出前 %d 名" % top_n)
                if order:
                    orders.append(order)
        budget = pct / max(len(targets), 1)
        for code in targets:
            if code in holdings or code not in bars:
                continue
            order = self.buy_order(ctx, code, bars[code].close, budget, reason="动量排名前 %d" % top_n)
            if order:
                orders.append(order)
        return orders

    def on_finish(self, ctx) -> None:
        ctx.log("共处理 %d 个交易日" % self.days)
