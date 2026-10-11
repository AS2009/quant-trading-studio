# -*- coding: utf-8 -*-
"""布林线均值回归（st_gm_boll_reversion）—— 由掘金（gm.api）策略转换而来。

源文件
------
`~/Downloads/布林线均值回归(股票)(1).py`（掘金量化官方示例，平台原注释：仅供参考，不建议直接实盘）。

信号逻辑
--------
- 买入：**没有持仓**且收盘价上穿布林**下轨**（今日在下轨之下、昨日在下轨之上）。
- 卖出：**有持仓**且收盘价上穿布林**上轨**（今日在上轨之上、昨日在上轨之下）。
- 不设加仓、不设止损；每根 bar 评估一次。

参数
----
- ma_period: 布林中轨周期（默认 26）
- std_period: 标准差周期（默认 26）
- std_range: 上下轨与中轨的距离倍数（默认 1.0）
- buy_volume: 开仓股数（默认 200，**必须为 100 的整数倍**）

转换说明（与掘金原版的差异，务必先读）
--------------------------------------
1. **指标口径：已精确对齐**。掘金示例用 pandas `close.rolling(n).std()`，pandas 默认是
   **样本**标准差（ddof=1），而本项目 `BaseStrategy.boll()` 用**总体**标准差（ddof=0，
   与通达信一致）。这里刻意**不用** `boll()`，而是手写样本标准差，让轨道宽度与原策略
   逐值一致（n=26 时两者约有 2% 的差距，直接用 `boll()` 会改变信号触发点）。
2. `order_volume(..., order_type=OrderType_Limit, price=当日收盘)` → `self.buy_order/sell_order`
   （市价语义，本项目按当日收盘价撮合）。原策略「限价 = 当前收盘价」在回测里等价于市价成交。
3. 掘金示例的买入量是 `100` 股（示例占位值）。这里提为参数 `buy_volume`，默认 200
   （≥1 手），**未做参数寻优**，请按自己的资金量调整。
4. 卖出为**清仓**（原策略 `position_effect=PositionEffect_Close` + 固定 100 股；
   本项目 `sell_order` 默认清空该标的持仓，更贴近「平仓」意图）。
5. 原策略的 `on_order_status` / `on_backtest_finished` 回调在本项目没有对应物，
   已舍弃——成交明细与绩效由回测引擎统一输出。

风险与假设
----------
- 依赖收盘价决策、以当日收盘价成交，**盘中不触发**；真实交易里这是「收盘前下单」的近似。
- 均值回归在单边趋势行情里会反复逆势开仓，原策略**没有止损**，回撤可能显著。
- 布林参数与开仓量都未寻优；示例策略不等于可盈利策略。
"""

from typing import Dict, List, Optional

from ...core.errors import ValidationError
from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy


class GmBollReversionStrategy(BaseStrategy):
    """布林线均值回归（掘金示例转换版）。"""

    id = "st_gm_boll_reversion"
    name = "布林线均值回归（GM转换）"
    category = "均值回归"
    desc = "收盘价上穿布林下轨买入、上穿布林上轨卖出；样本标准差口径与掘金原版逐值一致。"
    universe = "单标的；原示例为沪市 600004"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"ma_period": 26, "std_period": 26, "std_range": 1.0, "buy_volume": 200}
    default_symbols = ["600004.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "ma_period": {"label": "中轨周期", "type": "int", "default": 26, "min": 2, "max": 250, "step": 1,
                          "help": "布林线中轨（均线）周期"},
            "std_period": {"label": "标准差周期", "type": "int", "default": 26, "min": 2, "max": 250, "step": 1,
                           "help": "上下轨标准差周期；与原策略同为样本标准差（ddof=1）"},
            "std_range": {"label": "轨道倍数", "type": "float", "default": 1.0, "min": 0.1, "max": 5.0,
                          "step": 0.1, "help": "上下轨 = 中轨 ± 该倍数 × 标准差"},
            "buy_volume": {"label": "开仓股数", "type": "int", "default": 200, "min": 100, "max": 1000000,
                           "step": 100, "help": "每次开仓股数（100 的整数倍，按自己的资金量调整）"},
        }

    @classmethod
    def check_params(cls, params: Dict[str, object]) -> None:
        if int(params["buy_volume"]) % 100 != 0:
            raise ValidationError("开仓股数必须是 100 的整数倍（A 股 1 手 = 100 股）", field="buy_volume")

    # ---- 指标：与原策略口径逐值一致（样本标准差 ddof=1）----

    @staticmethod
    def _mean(values: List[float]) -> float:
        return sum(values) / len(values)

    @classmethod
    def _sample_std(cls, values: List[float], n: int) -> Optional[float]:
        """样本标准差（除以 n-1），对齐 pandas ``rolling(n).std()`` 的默认口径。

        与 `BaseStrategy.std()`（总体标准差，除以 n）**不同**：n=26 时样本口径约宽 2%，
        直接换用会让布林轨道变窄、改变信号触发点，因此这里手写。
        """
        if n < 2 or len(values) < n:
            return None
        window = values[-n:]
        mid = cls._mean(window)
        return (sum((value - mid) ** 2 for value in window) / (n - 1)) ** 0.5

    def _bands(self, closes: List[float]) -> Optional[tuple]:
        """返回 ``(上轨, 中轨, 下轨)``；数据不足返回 ``None``。"""
        ma_n = int(self.params["ma_period"])
        std_n = int(self.params["std_period"])
        k = float(self.params["std_range"])
        if len(closes) < max(ma_n, std_n):
            return None
        mid = self._mean(closes[-ma_n:])
        std = self._sample_std(closes, std_n)
        if std is None:
            return None
        return mid + k * std, mid, mid - k * std

    # ---- 逐 bar 决策 ----

    def on_start(self, ctx) -> None:
        super().on_start(ctx)

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        orders: List[OrderRequest] = []
        need = max(int(self.params["ma_period"]), int(self.params["std_period"])) + 1
        for code, bar in bars.items():
            closes = [float(item) for item in ctx.history(code, "close", need)]
            if len(closes) < need:
                continue                       # 数据不足（新股 / 区间太短）
            today = self._bands(closes)
            yesterday = self._bands(closes[:-1])       # 用昨日轨道做「上穿/下穿」判定
            if today is None or yesterday is None:
                continue
            upper, _mid, lower = today
            prev_upper, _p_mid, prev_lower = yesterday
            close = float(bar.close)
            prev_close = closes[-2]
            pos = ctx.position(code)
            holding = pos is not None and pos.qty > 0

            if not holding:
                # 上穿下轨 → 买入
                if close < lower and prev_close >= prev_lower:
                    order = self._open_order(ctx, code, close)
                    if order:
                        orders.append(order)
            else:
                # 上穿上轨 → 清仓
                if close > upper and prev_close <= prev_upper:
                    order = self.sell_order(ctx, code, reason="收盘价上穿布林上轨 %.2f" % upper)
                    if order:
                        orders.append(order)
        return orders

    def _open_order(self, ctx, code: str, price: float) -> Optional[OrderRequest]:
        """按 ``buy_volume`` 股开仓（资金不足时由引擎自动缩量）。"""
        qty = int(int(self.params["buy_volume"]) / 100) * 100
        if qty <= 0:
            return None
        order = self.buy_order(ctx, code, price, pct=1.0,
                               reason="收盘价上穿布林下轨，买入 %d 股" % qty)
        if order is None:
            return None
        order.qty = qty
        return order
