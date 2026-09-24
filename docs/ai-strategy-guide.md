# 让 AI 写出符合规范的策略（AI 写作指南）

本页是给 **AI 助手**（以及用 AI 写策略的人）的操作手册：读什么、按什么顺序产出、怎么自检、
产出什么样的交付物。目标是**一次写对**，而不是反复试错。

---

## 1. 开工前必须读的文件（按顺序）

| 顺序 | 文件 | 用来确定什么 |
|---|---|---|
| 1 | `docs/strategy-spec.md` | 硬性规则：命名、目录、文件格式、R1–R12、规则码、检查清单 |
| 2 | `docs/strategy-api.md` | 能用什么：`ctx` 方法、`BaseStrategy` 助手、参数 schema 字段、可用导入 |
| 3 | `backend/quantstudio/strategies/local/_template.py` | 文件骨架（直接复制改） |
| 4 | `backend/quantstudio/strategies/local/breakout_atr.py` | 单标的、带风控的完整范例 |
| 5 | `backend/quantstudio/strategies/local/rotation_topn.py` | 多标的、定期调仓的完整范例 |
| 6 | `backend/quantstudio/strategies/base.py` | **权威签名**：`buy_order/sell_order/ma/rsi/atr...` 的真实参数（文档与实现冲突时以此为准） |

不要凭记忆写 API：本项目的助手指南明确「数据不足返回 `None`」「只能通过 `ctx` 取数」，
这些细节直接决定 `lint` 能否通过。

---

## 2. 生成流程（8 步）

1. **定 slug**：把策略意图压成一个英文 `snake_case` slug（如 `breakout_atr`、`dual_thrust`、`rsi_bands`）；
2. **查重**：确认 `st_<slug>` 未被占用（`python scripts/check_strategies.py` 的输出里会列出全部策略 id，或在代码中检索）；
3. **选 `category` / `freq` / `universe_type`**：只能从词表里选（见 `docs/strategy-spec.md` §2）；
4. **列出参数**：每个参数给出 `label/type/default/min/max/step/help`，写进 `param_schema`，并让 `default_params` 与之一致；
5. **算 `min_bars`**：取最长指标窗口（如 `lookback`、`entry_days`、`rsi_period`）默认值 + 1，且不低于 60；
6. **写 `on_bar`**：只用 `ctx.history/bars_since/position(s)/cash/total_assets/price/bar` 与
   `self.buy_order/sell_order`；所有指标结果先判 `None`；跨日状态在 `on_start` 初始化；
7. **落盘**：写入 `backend/quantstudio/strategies/local/<slug>.py`，文件头写清「信号逻辑 / 参数 / 风险与假设」；
8. **跑校验并修到 0 error**：
   ```bash
   python scripts/check_strategies.py --path backend/quantstudio/strategies/local/<slug>.py
   python scripts/check_strategies.py --path backend/quantstudio/strategies/local/<slug>.py --json   # 需要结构化结果时
   ```

校验通过后，再用真实数据验证一次（可选但强烈建议）：

```bash
python scripts/run_backtest.py --list                                   # 确认策略已被发现
python scripts/run_backtest.py --strategy st_<slug> --symbols <代码> --start 2024-01-01 --end 2026-09-18
```

---

## 3. 提示词模板（可直接粘贴给 AI）

```text
请在 QuantTrading Studio 项目中新增一个策略。

【必读（按顺序）】
1. docs/strategy-spec.md     —— 硬性规范与规则码
2. docs/strategy-api.md      —— 可用 API（ctx / BaseStrategy / schema 字段）
3. backend/quantstudio/strategies/local/_template.py   —— 文件骨架
4. backend/quantstudio/strategies/local/breakout_atr.py —— 单标的范例
5. backend/quantstudio/strategies/base.py    —— 助手方法的权威签名（冲突时以此为准）

【策略需求】
- 意图：<用一两句话说清信号逻辑，例如「20 日新高突破买入，跌破 10 日新低或 2×ATR 止损离场」>
- 类别：<趋势跟踪/动量/震荡市/稳健/均值回归/统计套利/事件驱动/自定义>
- 标的类型：<single / multi / index>，标的池示例：<如 600519.SH,000858.SZ>
- 频率：<日线 / 周度 / 月度>
- 可调参数：<逐项列出含义与合理范围；不确定时给出你的建议值>
- 其他约束：<如「最多同时持有 3 只」「不做空」「单标的不超过 30% 仓位」>

【产出要求】
1. 只写一个文件：backend/quantstudio/strategies/local/<slug>.py
   （slug 由你确定，需为小写 snake_case；文件名 = st_ 去掉前缀后的 slug 对应关系必须成立）
2. 必须满足规范 R1–R12（无网络/文件/随机/时间依赖，无未来函数，只用 buy_order/sell_order 下单）
3. 交付时给出：
   a. 你选择的 slug / id / 类名 / name / category / freq / universe_type / min_bars 及理由；
   b. 每个参数的含义、默认值与范围；
   c. 你如何保证「不使用未来函数」（具体到哪一行用了 highs[:-1] 之类的写法）；
   d. `python scripts/check_strategies.py --path <文件>` 的**原始输出**；
   e. 用真实数据跑一次 `run_backtest.py` 的关键指标（累计收益/年化/最大回撤/夏普/交易次数/费用）。

【禁止】
- 修改 base.py / registry.py / lint.py / models.py 等框架文件；
- 为了让校验通过而删减参数范围、`min_bars` 或跳过快照判断；
- 直接访问 ctx 的私有成员、或在策略里发网络请求/读写文件；
- 编造回测数字（没有跑就不要写结果）。
```

---

## 4. 校验失败时的修复动作表

| 规则码 | 通常原因 | 修复动作 |
|---|---|---|
| `E_FILE` / `E_ENCODING` / `E_SYNTAX` | 路径写错 / 非 UTF-8 / 用了 3.10+ 语法 | 存为 UTF-8；去掉 `match`、`X \| Y` 等 3.10+ 写法 |
| `E_DOCSTRING` | 忘了模块 docstring | 补上并含「信号逻辑」小节 |
| `E_ID_FORMAT` / `E_ID_FILE_MISMATCH` / `E_CLASS_NAME` | 三处命名不一致 | 文件名 `<slug>.py` ↔ `id = "st_<slug>"` ↔ `class <PascalSlug>Strategy` |
| `E_ID_CONFLICT` | 与内置/已存在策略撞 id | 换 slug（不要复用已有策略名） |
| `E_META_MISSING` | 少写元数据 | 补齐 §1 表中的必填项 |
| `E_CATEGORY` / `E_FREQ` / `E_UNIVERSE_TYPE` | 词表外取值 | 改成词表内的值 |
| `E_SCHEMA_*` / `E_DEFAULT_PARAMS` | schema 字段缺失/范围不合法/与 `default_params` 不一致 | 让键集合与默认值完全一致，数字参数补 `min/max` 且 `min < default < max` |
| `E_VALIDATE_MISS` | 非法参数没被拒绝 | 检查 `min/max` 是否覆盖；跨参数约束写进 `check_params` 并抛 `ValidationError` |
| `E_MIN_BARS` | `min_bars` 太小 | 设为最长窗口 + 1，且 ≥ 60 |
| `E_ON_BAR` / `E_ON_BAR_SIG` / `E_ON_BAR_RETURN` | 方法缺失/签名错/返回类型错 | `def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]`，始终返回列表 |
| `E_IMPORT` / `E_FORBIDDEN_CALL` / `E_FORBIDDEN_ATTR` | 引入了禁用依赖或调用 | 删除 import；日志改用 `ctx.log` |
| `E_PRIVATE_ACCESS` / `E_LOOKAHEAD` | 直接读 `ctx._xxx` 或拿到未来数据 | 只用 `ctx.history/bars_since`，需要历史极值时用 `[:-1]` |
| `E_RELATIVE_IMPORT` | 相对导入层级错误 | `local/` 下：`base` 两级点、`core/config/compat` 三级点 |
| `E_SMOKE` | 合成行情回放时抛异常 | 常见：未判 `None`、未初始化跨日状态、除零、索引越界 |
| `W_MIN_BARS` / `W_DEFAULT_SYMBOLS` / `W_DOCSTRING` / `W_IMPORT` / `W_DEFAULTS` | 建议性问题 | 逐条处理；确实无法满足时在交付说明里解释原因 |

---

## 5. 交付格式（AI 应输出的内容）

```text
【新增文件】backend/quantstudio/strategies/local/<slug>.py
【策略元信息】id / 类名 / name / category / freq / universe_type / min_bars / version
【参数表】参数名 | 类型 | 默认值 | 范围 | 含义
【信号逻辑】买入条件 / 卖出条件 / 调仓节奏 / 仓位规则（逐条）
【无未来函数说明】列出关键行（例如「用 highs[:-1] 排除当日」）
【校验输出】python scripts/check_strategies.py --path <文件> 的原始输出（必须 0 error）
【真实数据回测】命令 + 关键指标（累计/年化/回撤/夏普/交易次数/费用）
【已知局限】至少一条（例如「震荡市容易被假突破反复止损」）
```

---

## 6. 给「人」的三条提醒

1. **校验只是底线**：`lint` 能保证规范与可运行性，不能保证策略有效。回测漂亮 ≠ 未来赚钱，务必小资金/模拟盘验证。
2. **看回撤与换手，再谈收益**：换手率高会显著吃掉收益（本项目已按佣金/单笔最低佣金/印花税/过户费/每笔流量费/滑点计入）。
3. **本地策略不进仓库**：`local/` 是你的实验场；要长期保留就自己备份，或按内置策略的方式并入 `strategies/` 并在 `registry.py` 注册。
