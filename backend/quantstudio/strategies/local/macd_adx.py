# -*- coding: utf-8 -*-
"""MACD 金叉 + ADX 趋势过滤 + 布林中轨离场（多指标共振示例）。

信号逻辑
--------
1. **入场**：MACD 的 DIF 上穿 DEA（金叉），且 ADX ≥ ``adx_min``（趋势强度足够，过滤震荡市里的假信号），
   且当前无持仓 → 按 ``position_pct`` 比例买入。
2. **离场**：DIF 下穿 DEA（死叉）**或** 收盘价跌破布林中轨（20 日均线，趋势走弱）→ 清仓。
3. 所有指标取「升序、最后一根是今日」的序列（``ctx.history`` 的返回值），
   严格无未来函数：判断金叉用「前一根、当根」两个点，不用未来数据。

为什么这样组合（写策略时可参考的取舍）
--------------------------------------
* 单靠 MACD 在震荡市会反复被打脸，ADX 是**趋势强度**指标，ADX 低说明方向性弱；
* 布林中轨≈20 日均线，作为「趋势是否还在」的粗筛，比固定百分比止损更能跟随趋势；
* 指标数据不足时（``None``）直接跳过，不做任何猜测——这也是校验器要求写法。
"""

from typing import Dict, List

from quantstudio.core.models import Bar, OrderRequest
from quantstudio.strategies.base import BaseStrategy


class MacdAdxStrategy(BaseStrategy):
    id = "st_macd_adx"
    name = "MACD 金叉 + ADX 过滤"
    category = "趋势跟踪"
    desc = "DIF 上穿 DEA 且 ADX 高于阈值时买入；死叉或跌破布林中轨离场。"
    freq = "日线"
    universe = "自选股（单标的即可）"
    universe_type = "single"
    min_bars = 61
    version = "1.0"
    default_params = {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "adx_period": 14,
        "adx_min": 20.0,
        "position_pct": 0.95,
    }
    default_symbols = ["600519.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "fast": {"label": "MACD 快线", "type": "int", "default": 12, "min": 2, "max": 60, "step": 1,
                     "help": "EMA 快线周期"},
            "slow": {"label": "MACD 慢线", "type": "int", "default": 26, "min": 5, "max": 120, "step": 1,
                     "help": "EMA 慢线周期，必须大于快线"},
            "signal": {"label": "DEA 周期", "type": "int", "default": 9, "min": 2, "max": 60, "step": 1,
                       "help": "DIF 的 EMA 周期"},
            "adx_period": {"label": "ADX 周期", "type": "int", "default": 14, "min": 5, "max": 60, "step": 1,
                           "help": "趋势强度指标的平滑周期"},
            "adx_min": {"label": "ADX 阈值", "type": "float", "default": 20.0, "min": 0.0, "max": 60.0,
                        "step": 0.5, "help": "低于该值视为震荡市，不入场"},
            "position_pct": {"label": "买入仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0,
                             "step": 0.05, "help": "开仓使用的可用资金比例"},
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        orders: List[OrderRequest] = []
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        signal = int(self.params["signal"])
        adx_period = int(self.params["adx_period"])
        adx_min = float(self.params["adx_min"])
        position_pct = float(self.params["position_pct"])

        for code in sorted(bars.keys()):
            bar = bars[code]
            window = max(slow + signal, 2 * adx_period + 2, 25)
            closes = ctx.history(code, "close", window + 2)
            highs = ctx.history(code, "high", window + 2)
            lows = ctx.history(code, "low", window + 2)
            if len(closes) < window or len(highs) < window or len(lows) < window:
                continue

            diff = self.macd(closes, fast, slow, signal)
            adx = self.adx(highs, lows, closes, adx_period)
            boll = self.boll(closes, 20, 2.0)
            if diff is None or adx is None or boll is None:
                continue
            dif, dea, _hist = diff
            _adx, _plus, _minus = adx
            _upper, mid, _lower = boll

            # 前一根的 DIF/DEA：用「去掉当根」的序列再算一次（不用未来数据）
            prev_diff = self.macd(closes[:-1], fast, slow, signal)
            prev_dif = prev_diff[0] if prev_diff else None
            prev_dea = prev_diff[1] if prev_diff else None

            pos = ctx.position(code)
            holding = pos is not None and pos.qty > 0

            if not holding:
                if prev_dif is None or prev_dea is None:
                    continue
                golden = prev_dif <= prev_dea and dif > dea          # DIF 上穿 DEA
                strong = adx[0] >= adx_min                          # 趋势强度达标
                if golden and strong:
                    order = self.buy_order(ctx, code, bar.close, position_pct,
                                           reason="MACD 金叉 + ADX %.1f" % adx[0])
                    if order:
                        orders.append(order)
                continue

            if prev_dif is not None and prev_dea is not None:
                dead = prev_dif >= prev_dea and dif < dea            # DIF 下穿 DEA
                if dead:
                    order = self.sell_order(ctx, code, reason="MACD 死叉")
                    if order:
                        orders.append(order)
                    continue
            if mid > 0 and bar.close < mid:
                order = self.sell_order(ctx, code, reason="跌破布林中轨 %.2f" % mid)
                if order:
                    orders.append(order)
        return orders
