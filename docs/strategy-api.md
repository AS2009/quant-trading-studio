# 策略 API 参考

写策略时你能用的**全部**接口。任何超出本页的能力（读文件、发请求、访问 `ctx` 私有字段）都是被禁止的，
`lint` 会直接判错 —— 这样回测结果才是可复现、可比较、可审计的。

---

## 1. 策略类必须提供的元数据

| 类属性 | 类型 | 必填 | 说明 / 约束 |
|---|---|---|---|
| `id` | str | ✅ | `st_` + 小写 snake_case，`^st_[a-z0-9_]{2,36}$`，全局唯一，且与文件名一致 |
| `name` | str | ✅ | 中文展示名，4–20 字 |
| `category` | str | ✅ | 词表：趋势跟踪 / 动量 / 震荡市 / 稳健 / 均值回归 / 统计套利 / 事件驱动 / 自定义 |
| `desc` | str | ✅ | ≤ 120 字，一句话说清逻辑 |
| `universe` | str | ✅ | 标的池的人类可读说明（展示给用户） |
| `universe_type` | str | ✅ | `single` / `multi` / `index` |
| `freq` | str | ✅ | 日线 / 周度 / 月度 / 盘中 |
| `min_bars` | int | ✅ | 最少需要的 bar 数；规则见规范 R9（≥ 最长窗口 + 1，建议 ≥ 60） |
| `version` | str | ✅ | `1.0` / `1.0.0` |
| `default_params` | dict | ✅ | 参数默认值；键集合必须与 `param_schema` 完全一致 |
| `default_symbols` | list[str] | 建议 | 未显式选择标的时的兜底池 |
| `lot_size` | int | 可选 | 最小交易单位，默认 100（A 股股票） |

---

## 2. 策略上下文 `ctx`（唯一的数据入口）

| 方法 | 返回 | 说明 |
|---|---|---|
| `ctx.today` | `str` | 当前 bar 的日期（`YYYY-MM-DD`） |
| `ctx.history(symbol, field="close", n=60)` | `List[float]` | 截至**今日（含）**的最近 n 个值，升序；`field ∈ {open, high, low, close, volume_wan, amount_yi, change_pct, turnover_pct}` |
| `ctx.bars_since(symbol, n)` | `List[Bar]` | 截至今日的最近 n 根 `Bar` 对象（用于 ATR 等需要高低价的指标） |
| `ctx.price(symbol)` | `float` | 今日收盘价（等价于 `bars[symbol].close`） |
| `ctx.bar(symbol)` | `Bar \| None` | 今日 bar |
| `ctx.position(symbol)` | `Position \| None` | 当前持仓（无持仓返回 `None`） |
| `ctx.positions()` | `List[Position]` | 全部持仓 |
| `ctx.cash()` | `float` | 可用资金（回测中为账户现金；未成交委托不计入） |
| `ctx.total_assets()` | `float` | 总资产 = 现金 + 持仓市值 |
| `ctx.log(message)` | `None` | 写入回测日志（会出现在结果的 `warnings`/日志里）；**不要用 `print`** |

> `history`/`bars_since` 的第 n 个元素就是今天，**不存在**任何能取到明天之后数据的入口。
> 需要「昨天为止」的极值（例如突破策略不要把自己算进去）时，用 `ctx.history(...)[:-1]`。

### `Bar`

`date, open, high, low, close, volume_wan(万手), amount_yi(亿元), change_pct(%), turnover_pct(%)`

### `Position`

`code, name, qty, available_qty, cost, price, market_value, cost_value, day_pnl, total_pnl, return_pct, industry`

- `qty` 是持仓总量，`available_qty` 是**可卖数量**（受 T+1 限制：当日买入的部分为 0）；
- 卖出时用 `self.sell_order(ctx, code)` 会自动按 `available_qty` 与整手规则处理，无需自己判断。

---

## 3. `BaseStrategy` 提供的方法

### 3.1 生命周期

| 方法 | 何时调用 | 说明 |
|---|---|---|
| `on_start(ctx)` | 回测/模拟盘开始时一次 | 初始化跨日状态；建议调用 `super().on_start(ctx)` |
| `on_bar(ctx, bars) -> List[OrderRequest]` | 每个交易日一次 | **必须实现**；`bars` 仅包含当日有行情的标的 |
| `on_finish(ctx)` | 结束时一次 | 收尾、统计（可用 `ctx.log`） |

### 3.2 下单助手（唯一允许的下单方式）

| 方法 | 说明 |
|---|---|
| `self.buy_order(ctx, code, price, pct=1.0, reason="")` → `OrderRequest \| None` | 按「可用资金 × pct」能买的最大整手数生成市价买单；数量为 0 时返回 `None` |
| `self.sell_order(ctx, code, qty=None, reason="")` → `OrderRequest \| None` | 卖出可卖数量的整手；不传 `qty` 即全部清仓；无可卖数量返回 `None` |
| `self.max_buy_qty(ctx, price, pct=1.0)` → `int` | 只算数量、不下单（需要自定义数量逻辑时使用） |

`OrderRequest` 字段：`code`、`side`（`buy`/`sell`）、`qty`、`price`（`None` = 市价）、`reason`（会展示在交易记录里，**建议写清触发原因**）。

引擎对委托的处理（决定了你写策略时的假设）：市价单按当日收盘价 ± 滑点成交；数量向下取整到 100 股；
买入受可用资金限制（不足自动缩量，缩到 0 则放弃）；卖出受 T+1 与可卖数量限制；
`|当日涨跌幅| ≥ 9.8%` 视为封板方向不可成交；费用按佣金/印花税/过户费计算（见根 README「回测口径」）。

### 3.3 指标助手（纯标准库实现，数据不足返回 `None`）

| 方法 | 说明 |
|---|---|
| `self.ma(values, n)` | 最近 n 个值的算术平均 |
| `self.ema(values, n)` | 指数移动平均（以最初 n 个值的均值为种子） |
| `self.std(values, n)` | 最近 n 个值的样本标准差（n ≥ 2） |
| `self.pct_change(values, n=1)` | `values[-1]/values[-1-n] - 1`（**小数**，0.05 = +5%） |
| `self.highest(values, n)` / `self.lowest(values, n)` | 最近 n 个值的最大/最小值 |
| `self.rsi(values, n=14)` | RSI（Wilder 平滑），0–100 |
| `self.atr(bars, n=14)` | ATR（Wilder 平滑），输入为 `ctx.bars_since(...)` 的 `List[Bar]` |

> 所有助手都接受「升序、最后一根是今日」的序列（即 `ctx.history` 的返回值）。
> 返回值一律为 `None` 表示数据不足，**必须先判空**（`lint` 的烟雾回测会暴露未判空的 `TypeError`）。

### 3.4 运行时与调试

| 方法 | 说明 |
|---|---|
| `self.params` | 已校验并补齐默认值的参数字典（按 `param_schema` 顺序） |
| `self.symbols` | 本次运行的标的池（回测引擎可能用配置兜底） |
| `self.set_params(dict)` | 运行时改参（会被重新校验；引擎用于 `params_override`） |
| `self.set_symbols(list)` | 运行时更换标的池 |
| `self.describe()` | 可读描述（id/name/params/symbols/category），调试用 |
| `self.spec` | 本策略的 `StrategySpec`（元数据 + 参数 schema） |
| `self.position_of(ctx, code)` | 等价于 `ctx.position(code)` |

---

## 4. 参数 schema 字段

```python
@classmethod
def param_schema(cls) -> Dict[str, Dict[str, object]]:
    return {
        "lookback": {
            "label": "回看周期",     # 必填：前端表单标签
            "type": "int",           # 必填：int / float / bool / str
            "default": 20,           # 必填：必须与 default_params 中的值一致
            "min": 2,                # 必填（数字类型）：下界，含
            "max": 250,              # 必填（数字类型）：上界，含；必须 > min
            "step": 1,               # 数字类型建议填写：前端步长（正数）
            "help": "比较窗口（交易日）",   # 必填：说明含义与单位
            # "choices": ["a", "b"],  # type=str 时必填：白名单
        },
        ...
    }
```

- 校验行为由基类统一实现：缺失取默认值、未知参数报错、类型/范围/白名单不合法报 `ValidationError`（接口层转 400，消息可直接展示给用户）；
- **跨参数约束**（例如「短均线必须小于长均线」）写在 `check_params` 里，抛 `ValidationError(..., field="参数名")`；
- 前端表单会根据 schema 自动渲染，所以 `label`/`help` 要写人话，`min`/`max`/`step` 要能约束住有效取值范围。

---

## 5. 可用导入清单

```python
from typing import Dict, List, Optional, Any      # 类型标注
import math                                       # 计算
from ...core.models import Bar, OrderRequest      # 领域模型（local/ 下为三级点）
from ...core.errors import ValidationError        # 跨参数校验
from ..base import BaseStrategy                   # 策略基类（两级点）
```

标准库白名单（不会告警）：`math`、`statistics`、`typing`、`dataclasses`、`collections`、`itertools`、
`functools`、`decimal`、`fractions`、`enum`、`abc`、`copy`、`operator`、`bisect`、`heapq`、`string`、`textwrap`。

**禁止**：`requests/urllib/http/httpx/socket`（网络）、`os/io/pathlib/shutil/subprocess/tempfile`（文件与系统）、
`threading/multiprocessing/asyncio`（并发）、`random/time/datetime`（破坏可复现性）、`logging`（请用 `ctx.log`）、
`pickle/sqlite3`（持久化）。

> 相对导入层级容易写错：本地策略在 `strategies/local/` 下，比内置策略深一层 ——
> 访问 `base` 用 **两个点** (`from ..base import BaseStrategy`)，访问 `core`/`config`/`compat` 用 **三个点**
> (`from ...core.models import Bar`)。写错会被 `lint` 判为 `E_RELATIVE_IMPORT` / `E_IMPORT`。
