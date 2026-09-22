# 策略编写规范（v1）

本规范定义 QuantTrading Studio 中「一个策略」的**命名、目录、文件格式、编写规则与校验要求**。
它同时服务于三类作者：人、AI 助手、以及从其他框架迁移过来的代码。

> 规范不是建议：**除标注「建议」的条目外，全部为硬性要求**，并由 `python scripts/check_strategies.py`
> 自动校验（源码静态检查 + 运行期校验 + 烟雾回测 + 无未来函数校验）。
> 未通过校验的策略**不会被加载**，但也不会影响应用启动（见「失败处理」）。

---

## 1. 先选对方式：两种「新增策略」

| 方式 | 适用场景 | 产物 | 是否需要写代码 | 出现位置 |
|---|---|---|---|---|
| **参数化实例**（界面「策略管理 → 新建策略」） | 只想用内置逻辑、换一组参数 | `backend/data/user_strategies.json` 中的一条记录（含 `template` 字段指向内置策略） | 否 | 策略列表，带「自定义」标记 |
| **本地策略**（本规范主体） | 需要新的信号逻辑 | `backend/quantstudio/strategies/local/<slug>.py` | 是 | 策略列表，`origin=local` |

内置策略（`backend/quantstudio/strategies/*.py`）由仓库维护、随版本发布；本地策略属于使用者自己的代码，**不进仓库**（`local/` 目录下的文件默认被 `.gitignore` 之外的约定隔离：只需不提交即可）。

---

## 2. 目录结构与文件命名

```
backend/quantstudio/strategies/
├── base.py              # 策略基类与指标助手（禁止修改）
├── registry.py          # 注册表 + local/ 自动发现（禁止在业务代码里绕开它）
├── lint.py              # 规范校验器
├── user.py              # 界面「新建策略」的 JSON 持久化
├── ma_cross.py ...      # 内置策略：文件与 id 一一对应
└── local/               # ← 本地策略放这里，重启后自动加载
    ├── __init__.py
    ├── _template.py     # 模板：以 _ 开头，不会被加载（复制它来写新策略）
    └── breakout_atr.py  # 可直接运行的示例（st_breakout_atr）
```

**硬性命名约定（文件 ↔ id ↔ 类名 必须三处一致）**

| 对象 | 规则 | 示例 |
|---|---|---|
| 文件名 | `<slug>.py`，`slug` 为小写 `snake_case` | `breakout_atr.py` |
| 策略 `id` | `st_` + `slug`，正则 `^st_[a-z0-9_]{2,36}$`，全局唯一 | `st_breakout_atr` |
| 类名 | `slug` 转 PascalCase + `Strategy` | `BreakoutAtrStrategy` |
| 展示名 `name` | 中文，4–20 字，不含换行 | `通道突破 + ATR 止损` |
| 参数键 | 小写 `snake_case`，带单位/语义后缀（`_pct/_days/_period/_mult/_ratio`），≤ 24 字符 | `position_pct` |
| `version` | `1.0` 或 `1.0.0`；逻辑变更时递增 | `1.1` |

**词表（不在词表内会被拒绝）**

- `category`（类别）：`趋势跟踪`、`动量`、`震荡市`、`稳健`、`均值回归`、`统计套利`、`事件驱动`、`自定义`
- `freq`（频率）：`日线`、`周度`、`月度`、`盘中`
- `universe_type`（标的池类型）：`single`（单标的）、`multi`（多标的轮动）、`index`（指数级别）

---

## 3. 文件格式（固定骨架）

```python
# -*- coding: utf-8 -*-
"""<策略中文名>（st_<slug>）。

信号逻辑
--------
- 买入：<触发条件>
- 卖出：<触发条件>

参数
----
- <param_key>: <含义与单位>（默认 <值>）

风险与假设
----------
- <已知局限>
"""

from typing import Dict, List

from ..base import BaseStrategy                              # strategies/base.py
from ...core.models import Bar, OrderRequest                 # 注意：local/ 深一层 → 三级点
from ...core.errors import ValidationError                   # 需要跨参数校验时才导入

class XxxStrategy(BaseStrategy):
    id = "st_xxx"
    name = "…"
    category = "趋势跟踪"
    desc = "一句话说明（≤ 120 字）"
    universe = "自选股（单标的即可）"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"lookback": 20, "position_pct": 0.95}
    default_symbols = ["600519.SH"]        # 建议提供：未选标的时的兜底池

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "lookback": {"label": "回看周期", "type": "int", "default": 20,
                         "min": 2, "max": 250, "step": 1, "help": "比较窗口（交易日）"},
            "position_pct": {"label": "买入仓位", "type": "float", "default": 0.95,
                             "min": 0.05, "max": 1.0, "step": 0.05, "help": "可用资金比例"},
        }

    @classmethod
    def check_params(cls, params) -> None:      # 可选：跨参数约束
        ...

    def on_start(self, ctx) -> None:            # 可选：初始化跨日状态
        super().on_start(ctx)
        ...

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        ...                                     # 唯一必需方法
```

要点：

- **一个文件一个策略类**；文件内不要定义其他 `BaseStrategy` 子类（`lint` 只注册本模块定义的类，多类会告警 `W_MULTI_CLASS`）。
- 模块 docstring 必须存在，且包含「信号逻辑」小节（`E_DOCSTRING` / `W_DOCSTRING`）。
- `default_params` 与 `param_schema` 的**键集合必须完全一致**，同键的 `default` 值也必须一致（`E_SCHEMA_KEYS` / `E_SCHEMA_DEFAULT_MISMATCH`）。

---

## 4. 必须遵守的编写规则

| # | 规则 | 违反时的规则码 |
|---|---|---|
| R1 | 只允许导入标准库计算类模块（`math/statistics/typing/dataclasses/collections/itertools/functools/decimal/...`）与本项目 `quantstudio.*`；**禁止**网络（`requests/urllib/http/socket`）、文件与系统（`os/io/pathlib/shutil/subprocess/threading`）、`random`、`time/datetime` | `E_IMPORT`、`W_IMPORT`、`W_IMPORT_UNKNOWN` |
| R2 | 禁止调用 `print/open/eval/exec/compile/__import__/input/exit`；日志用 `ctx.log()` | `E_FORBIDDEN_CALL` |
| R3 | 禁止访问 `ctx` 的私有成员（`ctx._xxx`）——那等于绕过引擎偷看未来数据 | `E_PRIVATE_ACCESS` |
| R4 | **严禁未来函数**：只能用 `ctx.history/bars_since/position(s)/cash/total_assets/price/bar`，它们都只返回「截至今日」的数据 | `E_LOOKAHEAD` |
| R5 | 下单只能用 `self.buy_order(...)` / `self.sell_order(...)`（自动整手、预留费用、T+1 与可卖校验）；不要自己算手续费、不要直接改账户 | `E_ON_BAR_RETURN` |
| R6 | `on_bar` 必须返回 `List[OrderRequest]`（可为空列表），不得返回 `None` 或裸 `bool` | `E_ON_BAR_RETURN` |
| R7 | 指标助手（`ma/ema/std/rsi/atr/highest/lowest/pct_change`）在数据不足时返回 `None`，使用者必须判空后再决策 | 烟雾回测会暴露 `TypeError` → `E_SMOKE` |
| R8 | 参数取值必须落在 `param_schema` 声明的 `[min, max]` 内；跨参数约束写在 `check_params`，非法取值抛 `ValidationError` | `E_VALIDATE_MISS`、`E_SCHEMA_*` |
| R9 | `min_bars` ≥ 最长指标窗口 + 1（`lint` 从参数名与默认值静态推断）；建议 ≥ 60（回测引擎至少预热 60 根） | `E_MIN_BARS`、`W_MIN_BARS` |
| R10 | 结果必须可复现：禁止随机数、禁止依赖当前时间、禁止依赖 `set`/`dict` 遍历顺序做决策 | `E_IMPORT`、`W_IMPORT` |
| R11 | 代码须兼容 **Python 3.9**（无 `match`、无 `X | Y` 类型联合、无 `dataclass(slots=)`），文件为 UTF-8 | `E_SYNTAX`、`E_ENCODING` |
| R12 | 策略必须是**纯计算**：不写文件、不发请求、不改全局状态；跨日状态只放在实例属性上（`self.xxx`） | `E_FORBIDDEN_CALL`、`E_IMPORT` |

> 关于 R4 的判定方式：`lint` 会把第 N 根之后的行情**放大 10 倍**再跑一遍，若前 N 根的委托发生变化，就判定存在未来函数。
> 因此「先用 `ctx.history` 取全量再取 `[-1]` 之外的未来值」这类写法一定会被抓到。

---

## 5. 校验要求

```bash
# 推荐入口（仓库内任意目录）
python scripts/check_strategies.py                       # 校验内置 + local/ 全部策略
python scripts/check_strategies.py --path backend/quantstudio/strategies/local/my.py
python scripts/check_strategies.py --json                # 机器可读（CI / AI 自检）
python scripts/check_strategies.py --no-smoke            # 只做静态 + 运行期校验（更快）

# 等价写法
cd backend && python -m quantstudio.strategies.lint
```

退出码：`0` = 无 error（warning 不拦截）；`1` = 存在 error；`2` = 用法错误。

校验分三层，**任何一层不通过都必须先修复再提交**：

1. **源码层（AST）**：编码/头注释、Python 3.9 语法、文件↔id↔类名一致、docstring、必填元数据、禁用 import 与调用、`on_bar` 签名；
2. **运行期层**：`meta()` 可被 JSON 序列化、类别/频率/标的池类型在词表内、`param_schema` 与 `default_params` 完全一致、每个数字参数声明 `min/max` 且 `min < max`、`default` 落在区间内、`str` 参数提供 `choices`、**非法参数必须被 `validate_params` 拒绝**、`min_bars` 足够；
3. **行为层（烟雾回测）**：用确定性合成行情（260 根）分别以「空仓」「持仓」两种状态逐 bar 调用 `on_bar`，断言不抛异常、返回值类型正确，并执行无未来函数校验。

### 规则码速查

**错误（error，必须修复）**

| 规则码 | 含义 |
|---|---|
| `E_FILE` / `E_ENCODING` / `E_SYNTAX` | 文件不可读 / 非 UTF-8 / Python 语法错误（含 3.9 兼容） |
| `E_DOCSTRING` | 缺少模块 docstring |
| `E_CLASS` / `E_BASE` / `E_CLASS_NAME` | 未定义策略类 / 未继承 `BaseStrategy` / 类名与文件名不符 |
| `E_META_MISSING` / `E_META` / `E_META_JSON` | 必填元数据缺失 / `meta()` 调用失败 / 无法安全序列化 |
| `E_ID_FORMAT` / `E_ID_FILE_MISMATCH` / `E_ID_CONFLICT` | id 命名非法 / 与文件名不一致 / 与已注册策略冲突 |
| `E_NAME_LEN` / `E_DESC` / `E_DESC_LEN` / `E_VERSION` | 名称长度、描述缺失/超长、版本格式 |
| `E_CATEGORY` / `E_FREQ` / `E_UNIVERSE_TYPE` | 类别/频率/标的池类型不在词表内 |
| `E_SCHEMA` / `E_SCHEMA_TYPE` / `E_SCHEMA_FIELD` / `E_SCHEMA_PTYPE` / `E_SCHEMA_KEYS` / `E_SCHEMA_RANGE` / `E_SCHEMA_STEP` / `E_SCHEMA_DEFAULT` / `E_SCHEMA_DEFAULT_MISMATCH` / `E_SCHEMA_CHOICES` / `E_DEFAULT_PARAMS` | 参数 schema 缺失或不合规（键不一致、缺字段、类型非法、min≥max、default 越界、step 非正、str 无 choices、`default_params` 为空） |
| `E_VALIDATE` / `E_VALIDATE_MISS` | 默认参数自校验失败 / 非法参数未被拦截 |
| `E_MIN_BARS` | `min_bars` 小于最长指标窗口 + 1 |
| `E_ON_BAR` / `E_ON_BAR_SIG` / `E_ON_BAR_RETURN` | 未实现 `on_bar` / 签名错误 / 返回值类型错误 |
| `E_IMPORT` / `E_FORBIDDEN_CALL` / `E_FORBIDDEN_ATTR` / `E_PRIVATE_ACCESS` / `E_RELATIVE_IMPORT` | 违规依赖、调用、属性访问、私有成员访问、相对导入层级错误 |
| `E_SMOKE` / `E_LOOKAHEAD` | 烟雾回测抛异常 / 检测到未来函数 |

**警告（warning，不拦截但应处理）**

| 规则码 | 含义 |
|---|---|
| `W_HEADER` / `W_DOCSTRING` | 缺少统一头注释 / docstring 未包含「信号逻辑」小节 |
| `W_MIN_BARS` | `min_bars` < 60（回测引擎至少预热 60 根） |
| `W_DEFAULT_SYMBOLS` | 未提供 `default_symbols` |
| `W_DEFAULTS` | `validate_params({})` 结果与 `default_params` 不一致 |
| `W_MULTI_CLASS` | 文件内定义了多个类（只有本模块的 `BaseStrategy` 子类会被注册） |
| `W_IMPORT` / `W_IMPORT_UNKNOWN` | 使用了不建议/非白名单模块 |
| `W_VALIDATE_EXC` | 非法参数用例抛出了非 `ValidationError` 异常 |
| `W_LOOKAHEAD_SKIP` | 无未来函数校验未能执行 |

### 失败处理

- `local/` 下**单个文件加载失败只会被记录**（`quantstudio.strategies.local_status()` 可查，`/api/system/status` 暴露），其余策略照常工作，应用不会因此启动失败；
- 校验不通过的策略**不会出现在策略列表中**，也不会参与回测；
- 修改 `local/` 下的文件后**重启服务**即生效（`lint` 支持热重载，应用不做热加载以保证回测可复现）。

---

## 6. 上线前检查清单

- [ ] 文件放在 `local/<slug>.py`，文件名、`id`、类名三者一致（`st_<slug>` / `<PascalSlug>Strategy`）
- [ ] 模块 docstring 写清「信号逻辑 / 参数 / 风险与假设」
- [ ] 元数据齐全：`id/name/category/desc/universe/universe_type/freq/min_bars/version/default_params`
- [ ] `param_schema` 每个参数都有 `label/type/default/min/max/step/help`，且与 `default_params` 一致
- [ ] 跨参数约束写在 `check_params`，非法取值抛 `ValidationError`
- [ ] 只用 `ctx` 的公开方法与 `self.buy_order/sell_order`，无 `print/open/import 违规`
- [ ] 指标返回 `None` 时已判空；`min_bars` 覆盖最长窗口
- [ ] `python scripts/check_strategies.py --path <你的文件>` 输出 `OK`（无 error）
- [ ] 用真实数据跑一次回测（界面「回测分析」或 `python scripts/run_backtest.py --strategy <id>`），确认成交价、换手、回撤合理
- [ ] 需要跟随时，更新 `version` 并在 docstring 里注明变更点
