# -*- coding: utf-8 -*-
"""<策略中文名>（st_<slug>）。   ← 复制本文件为 local/<slug>.py 后改名，本文件不会被加载

信号逻辑
--------
- 买入：<写清触发条件，例如「MA(n) 上穿 MA(m) 且当前空仓」>
- 卖出：<写清触发条件，例如「跌破 ATR 止损或 MA 死叉」>
- <说明调仓节奏、最大持仓数、是否允许加仓>

参数
----
- <param_key>: <含义与单位>（默认 <默认值>）

风险与假设
----------
- <已知局限：如「依赖收盘价，盘中不触发」「假突破会连续止损」>
"""

from typing import Dict, List

from ...core.errors import ValidationError          # 需要跨参数校验时使用，否则可删除
from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy          # strategies/base.py 上溯两级；core 需三级（...）

# 文件名必须是 local/<slug>.py；下面三处必须一致：
#   文件名 breakout_atr.py  ←→  id = "st_breakout_atr"  ←→  class BreakoutAtrStrategy


class BreakoutAtrStrategy(BaseStrategy):
    # ---- 必填元数据（缺一项校验就不过）----
    id = "st_breakout_atr"                 # st_ + 小写 snake_case，全局唯一
    name = "通道突破 + ATR 止损"            # 中文展示名，4-20 字
    category = "趋势跟踪"                   # 只能取 docs/strategy-spec.md 里的类别词表
    desc = "突破 N 日高点买入，跌破 M 日低点或 ATR 止损离场。"     # ≤ 120 字
    universe = "自选股（单标的即可）"
    universe_type = "single"               # single / multi / index
    freq = "日线"                          # 日线 / 周度 / 月度 / 盘中
    min_bars = 61                          # 必须 ≥ 最长指标窗口 + 1，且建议 ≥ 60（引擎预热 60 根）
    version = "1.0"
    default_params = {"entry_days": 20, "exit_days": 10, "atr_period": 14, "atr_mult": 2.0, "position_pct": 0.95}
    default_symbols = ["600519.SH"]

    # ---- 参数描述：必须与 default_params 完全一致（键相同、default 相同）----
    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "entry_days": {"label": "入场新高周期", "type": "int", "default": 20, "min": 5, "max": 120, "step": 1,
                           "help": "收盘价突破该周期内最高价买入（交易日）"},
            "exit_days": {"label": "离场新低周期", "type": "int", "default": 10, "min": 3, "max": 60, "step": 1,
                          "help": "收盘价跌破该周期内最低价卖出（交易日）"},
            "atr_period": {"label": "ATR 周期", "type": "int", "default": 14, "min": 5, "max": 60, "step": 1,
                           "help": "平均真实波幅周期"},
            "atr_mult": {"label": "ATR 止损倍数", "type": "float", "default": 2.0, "min": 0.5, "max": 6.0, "step": 0.1,
                         "help": "跟踪止损 = 持仓成本 − 该倍数 × ATR"},
            "position_pct": {"label": "买入仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05,
                             "help": "开仓使用的可用资金比例"},
        }

    # ---- 跨参数约束（可选；单参数范围由 param_schema 的 min/max 负责）----
    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        if int(params["exit_days"]) >= int(params["entry_days"]):
            raise ValidationError("离场周期必须小于入场周期", field="exit_days")

    # ---- 跨日状态：只允许保存「已发生」的信息，禁止用它夹带未来数据 ----
    def on_start(self, ctx) -> None:
        super().on_start(ctx)
        self.entry_price = 0.0

    # ---- 唯一的必需方法：返回委托列表，不要直接改账户/自己算费用 ----
    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        orders: List[OrderRequest] = []
        for code, bar in bars.items():
            closes = ctx.history(code, "close", 200)      # 升序，最后一根是今天（严格无未来函数）
            highs = ctx.history(code, "high", 200)
            lows = ctx.history(code, "low", 200)
            if len(closes) < 2:
                continue
            entry_n = int(self.params["entry_days"])
            exit_n = int(self.params["exit_days"])
            atr = self.atr(ctx.bars_since(code, int(self.params["atr_period"]) + 1), int(self.params["atr_period"]))
            prev_high = self.highest(highs[:-1], entry_n)   # 用「昨天及之前」的高点，避免把今天算进去
            prev_low = self.lowest(lows[:-1], exit_n)
            pos = ctx.position(code)
            if pos is None or pos.qty <= 0:
                if prev_high is None or bar.close <= prev_high:
                    continue
                order = self.buy_order(ctx, code, bar.close, float(self.params["position_pct"]),
                                       reason="突破 %d 日高点 %.2f" % (entry_n, prev_high))
                if order:
                    self.entry_price = bar.close
                    orders.append(order)
            else:
                stop = 0.0
                if atr and self.entry_price > 0:
                    stop = self.entry_price - float(self.params["atr_mult"]) * atr
                hit_low = prev_low is not None and bar.close < prev_low
                hit_stop = stop > 0 and bar.close <= stop
                if hit_low or hit_stop:
                    reason = "跌破 %d 日低点" % exit_n if hit_low else "触发 ATR 止损 %.2f" % stop
                    order = self.sell_order(ctx, code, reason=reason)
                    if order:
                        self.entry_price = 0.0
                        orders.append(order)
        return orders
