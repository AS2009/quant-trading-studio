# 策略完整示例

本页给出**可以直接复制使用**的完整策略，全部通过 `python scripts/check_strategies.py`（含烟雾回测与无未来函数校验）。

> 下面两段代码与仓库中的 `backend/quantstudio/strategies/local/*.py` 一致（若两者不一致，以仓库文件为准）。
> 复制到 `backend/quantstudio/strategies/local/<slug>.py` 后记得同步修改**文件名 / id / 类名**三处。

---

## 示例 A：通道突破 + ATR 止损（单标的，趋势跟踪）

文件：`backend/quantstudio/strategies/local/breakout_atr.py` → `st_breakout_atr`

要点：用「昨天及之前」的极值判断突破（`highs[:-1]`）、ATR 跟踪止损、跨日状态只在 `on_start` 初始化。

```python
# -*- coding: utf-8 -*-
"""通道突破 + ATR 止损（st_breakout_atr）。   ← 本文件是可直接运行的示例，改完重启即生效

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
```

**验证**

```bash
python scripts/check_strategies.py --path backend/quantstudio/strategies/local/breakout_atr.py
python scripts/run_backtest.py --strategy st_breakout_atr --symbols 600519.SH --start 2024-01-01
```

---

## 示例 B：动量轮动 TopN（多标的，每月调仓）

文件：`backend/quantstudio/strategies/local/rotation_topn.py` → `st_rotation_topn`

要点：多标的打分排序、先卖后买释放资金、等权分配到 `top_n` 只、调仓节奏由 `rebalance_days` 控制、
排序键带二级键 `code` 保证确定性（避免并列时顺序漂移）。

```python
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
```

**验证**

```bash
python scripts/check_strategies.py --path backend/quantstudio/strategies/local/rotation_topn.py
python scripts/run_backtest.py --strategy st_rotation_topn \
  --symbols 600519.SH,000858.SZ,300750.SZ,600036.SH,601318.SH --start 2024-01-01
```

---

## 示例 C：最小可运行骨架（30 行）

适合快速起手：收盘价高于上一日收盘价买入，低于则清仓。

```python
# -*- coding: utf-8 -*-
"""上穿即买（st_min_demo）。

信号逻辑
--------
- 买入：今日收盘价高于昨日收盘价且当前空仓
- 卖出：今日收盘价低于昨日收盘价且持仓

参数
----
- position_pct: 买入仓位（默认 0.95）
"""

from typing import Dict, List

from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy


class MinDemoStrategy(BaseStrategy):
    id = "st_min_demo"
    name = "上穿即买示例"
    category = "自定义"
    desc = "收盘价高于昨日则买入，低于昨日则清仓的演示策略。"
    universe = "自选股（单标的即可）"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"position_pct": 0.95}
    default_symbols = ["600519.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "position_pct": {"label": "买入仓位", "type": "float", "default": 0.95,
                             "min": 0.05, "max": 1.0, "step": 0.05, "help": "可用资金比例"},
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        orders: List[OrderRequest] = []
        for code, bar in bars.items():
            closes = ctx.history(code, "close", 2)
            if len(closes) < 2:
                continue
            pos = ctx.position(code)
            if pos is None and bar.close > closes[-2]:
                order = self.buy_order(ctx, code, bar.close, float(self.params["position_pct"]), reason="收盘价上穿")
                if order:
                    orders.append(order)
            elif pos is not None and bar.close < closes[-2]:
                order = self.sell_order(ctx, code, reason="收盘价下穿")
                if order:
                    orders.append(order)
        return orders
```

> 该骨架**未**放在 `local/` 里（避免污染策略列表）；要用就直接复制为 `local/min_demo.py` 并跑 lint。

---

## 常见错误对照表

| 错误写法 | 现象 / 规则码 | 正确写法 |
|---|---|---|
| `self.ma(closes, 20) > closes[-1]`（未判空） | 烟雾回测抛 `TypeError` → `E_SMOKE` | `if ma20 is not None and ma20 > close:` |
| `print("调仓")` | `E_FORBIDDEN_CALL` | `ctx.log("调仓")` |
| `import requests` / `import os` / `from datetime import datetime` | `E_IMPORT` / `W_IMPORT` | 只做计算；数据由 `ctx` 提供 |
| `future = ctx._series[code][-1]` | `E_PRIVATE_ACCESS`（并可能触发 `E_LOOKAHEAD`） | `ctx.history(code, "close", n)` |
| `highs = ctx.history(code, "high", 20)` 后直接用 `max(highs)` 判断「突破 20 日高点」 | 把今天算进极值，信号偏乐观 | 用 `self.highest(highs[:-1], 20)` |
| `default_params = {"n": 20}` 但 schema 写 `"default": 30` | `E_SCHEMA_DEFAULT_MISMATCH` | 两处保持一致 |
| schema 里数字参数漏了 `min`/`max` | `E_SCHEMA_RANGE` | 补上，且 `min < default < max` |
| `min_bars = 20` 而 `lookback` 默认 200 | `E_MIN_BARS` | `min_bars` ≥ 最长窗口 + 1（建议 ≥ 60） |
| `id = "st_Breakout"` 或 `id = "breakout"` | `E_ID_FORMAT` | `id = "st_breakout"` |
| 文件名 `breakout.py` 但 `id = "st_breakout_atr"` | `E_ID_FILE_MISMATCH` | 文件名与 id 对应起来 |
| `class BreakoutStrategy` 放在 `breakout_atr.py` | `E_CLASS_NAME` | 类名 = 文件名转 PascalCase + `Strategy` |
| 在 `local/` 下写 `from ..core.models import Bar` | `E_RELATIVE_IMPORT`（core 需要三级点） | `from ...core.models import Bar` |
| `on_bar` 里 `return None` 或返回单个 `OrderRequest` | `E_ON_BAR_RETURN` | 始终返回 `List[OrderRequest]`（可空列表） |
| 手动构造 `OrderRequest(qty=-100)` / 自己算手续费 | 被撮合层拒绝或记账错误 | 用 `self.buy_order/sell_order` |
| 用 `set` 迭代决定买卖顺序 | 顺序不稳定 → 回测不可复现（`W_IMPORT` 提示） | 用 `list` + 显式排序键 |
| 在 `on_bar` 里 `self.counter += 1` 但没在 `on_start` 初始化 | 首个 bar 抛 `AttributeError` → `E_SMOKE` | 在 `on_start` 里初始化（或用 `getattr(self, "counter", 0)`） |

```bash
# 改完就跑（三秒内可判断是否合规）
python scripts/check_strategies.py --path backend/quantstudio/strategies/local/<你的文件>.py
```
