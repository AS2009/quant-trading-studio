# MCP：让大模型直接操作本项目

> 本文面向**使用者**：怎么把 QuantTrading Studio 接到 Claude Desktop / Claude Code / Cursor / Cline 等
> 支持 MCP 的客户端里，让模型查行情、写策略、跑回测、看持仓 / 模拟盘。

---

## 1. 它是什么

[MCP](https://modelcontextprotocol.io)（Model Context Protocol）是「模型 ↔ 本地工具」的标准协议。
本仓库自带一个 **stdio 服务器**，把项目已有的能力（与 Web / 桌面版**同一套服务层**）暴露给大模型：

* **tools（33 个）**：行情、自选池、策略读写与校验、回测、持仓账本、模拟盘、系统状态；
* **resources（8 个文档）**：`quantstudio://docs/...`，模型需要时自己读策略规范 / 示例 / 数据目录说明；
* **prompts（3 个模板）**：写策略、复盘回测、看盘简报，客户端里点一下就能带完整工作流。

实现是**纯 Python 标准库**（`backend/quantstudio/mcp/`），没有新增任何第三方依赖：
`git clone` 之后就能接入，不需要先装 `mcp` SDK。服务器是本地子进程，只读写仓库内的本地文件
与公开行情接口，**永远不会触达任何真实券商 / 交易通道**。

---

## 2. 5 分钟接入

三种客户端配置的都是同一件事：**用什么命令启动 `scripts/mcp_server.py`**。

### 2.1 Claude Code（推荐：仓库里已经配好）

仓库根目录的 [`.mcp.json`](../.mcp.json) 就是项目级配置，Claude Code 在该项目下会自动识别：

```json
{
  "mcpServers": {
    "quantstudio": {
      "command": "python",
      "args": ["scripts/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

在项目目录里打开 Claude Code，首次会询问是否信任并使用该 MCP 服务器；用 `/mcp` 可以查看连接状态。
也可以手动加：`claude mcp add quantstudio -- python scripts/mcp_server.py`。

> 这里的路径是**相对仓库根目录**的，`scripts/mcp_server.py` 会自己处理 `sys.path`，所以能直接跑。

### 2.2 Claude Desktop

编辑 `claude_desktop_config.json`（没有就新建），把路径换成**你自己的仓库绝对路径**：

* **macOS**：`~/Library/Application Support/Claude/claude_desktop_config.json`
* **Windows**：`%APPDATA%\Claude\claude_desktop_config.json`（即 `C:\Users\<你>\AppData\Roaming\Claude\...`）

```json
{
  "mcpServers": {
    "quantstudio": {
      "command": "python",
      "args": ["/绝对路径/quant-trading-studio/scripts/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Windows 写成 `"args": ["C:\\代码\\quant-trading-studio\\scripts\\mcp_server.py"]` 这种反斜杠双写。
保存后**完全退出并重启** Claude Desktop，聊天框里会出现工具图标。

### 2.3 Cursor

* 项目级：仓库里新建 `.cursor/mcp.json`；
* 全局：`~/.cursor/mcp.json`。

```json
{
  "mcpServers": {
    "quantstudio": {
      "command": "python",
      "args": ["/绝对路径/quant-trading-studio/scripts/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

在 Cursor 的 Settings → MCP 里确认 `quantstudio` 是绿灯。Cline 等其它客户端同理，
字段都是 `command` / `args` / `env`。

### 2.4 Windows 上没装 Python：用桌面版 exe

打包好的桌面程序本身就是 Python 运行时，加 `--mcp` 就变成 MCP 服务器（不建窗口）：

```json
{
  "mcpServers": {
    "quantstudio": {
      "command": "C:\\Program Files\\QuantTradingStudio\\QuantTradingStudio.exe",
      "args": ["--mcp"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

`--mcp` 后面可以继续加 MCP 自己的参数，例如 `["--mcp", "--read-only"]`。
exe 的下载与打包见 [desktop-gui.md](desktop-gui.md)。

> **路径提示**：Claude Desktop / Cursor 启动子进程的工作目录不一定是仓库根目录，
> 所以这两个客户端请写**绝对路径**；Claude Code 的项目级 `.mcp.json` 可以直接用相对路径。

---

## 3. 工具清单

> 下表以 `backend/quantstudio/mcp/tools_*.py` 实际注册的工具为准（共 **33** 个）。
> 带 `--read-only` 启动时，**写**工具会从 `tools/list` 里消失（客户端看不到就调不到）。
> 「只读但重」= 不改数据，但可能返回大量结果（如回测），请按需开启参数。

### 行情（4）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `market_overview` | 大盘指数、涨跌广度、两市成交额与主力/北向资金 | 只读 |
| `market_quotes` | 个股实时快照（不传 `codes` 时查自选池） | 只读 |
| `market_kline` | 历史 K 线（日/周/月，前/后复权；默认 60 根、上限 400 根） | 只读 |
| `market_sectors` | 板块涨幅榜与主力净流入 | 只读 |

### 自选池（3）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `watchlist_list` | 自选池代码列表 | 只读 |
| `watchlist_add` | 把股票加入自选池（写 `watchlist.json`） | **写** |
| `watchlist_remove` | 把股票移出自选池 | **写** |

### 策略（8）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `strategy_list` | 全部策略（内置 / 本地代码 / 用户）与运行状态 | 只读 |
| `strategy_get` | 单个策略的定义与参数 schema | 只读 |
| `strategy_read_source` | 读取策略 Python 源码（改代码前先读） | 只读 |
| `strategy_lint` | 校验策略代码（静态规范 + 运行期 + 烟雾回测 + 无未来函数） | 只读 |
| `strategy_write_source` | 写入 `strategies/local/<slug>.py`，写完热加载并自动校验 | **写** |
| `strategy_delete_source` | 删除本地代码策略（需 `confirm=true`，删除前备份） | **写** |
| `strategy_user_create` | 新建「用户策略」（内置模板 + 参数，不落代码文件） | **写** |
| `strategy_user_delete` | 删除用户策略条目（先备份 `user_strategies.json`） | **写** |

### 回测（3）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `backtest_run` | 用真实历史日线回测策略；默认只回摘要，`include_series=true` 带净值/回撤/月度序列，`include_trades=true` 带成交流水。费用可传 `commission_rate` / `commission_min` / `flow_fee`（每笔固定费）/ `slippage_bps` / `slippage_ticks` + `tick_size`（跳数滑点）/ `lot_size` | 只读但重 |
| `backtest_cache_info` | 查看进程内回测缓存 | 只读 |
| `backtest_cache_clear` | 清空进程内回测缓存 | **写**（仅缓存） |

### 持仓账本（7）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `portfolio_overview` | 账本总览（总资产 / 市值 / 现金 / 盈亏 / 持仓数 / 模式） | 只读 |
| `portfolio_holdings` | 持仓明细（数量 / 成本 / 现价 / 市值 / 盈亏） | 只读 |
| `portfolio_equity` | 权益曲线（默认最近 90 天） | 只读 |
| `portfolio_upsert_holding` | 新增 / 更新一条持仓 | **写** |
| `portfolio_delete_holding` | 删除一条持仓 | **写** |
| `portfolio_set_cash` | 设置账本现金 | **写** |
| `portfolio_set_mode` | 在 `manual`（自有账本）与 `paper`（模拟盘）之间切换 | **写** |

### 模拟盘（6）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `paper_account` | 模拟账户汇总（资产 / 市值 / 现金 / 盈亏） | 只读 |
| `paper_orders` | 委托流水（最新在前） | 只读 |
| `paper_fills` | 成交流水（含费用） | 只读 |
| `paper_submit_order` | 模拟下单（**永不触达真实券商**） | **写** |
| `paper_cancel_order` | 撤销未成交 / 部分成交的模拟委托 | **写** |
| `paper_reset` | 清空模拟盘账本（**不可撤销**） | **写** |

### 系统（2）

| 工具 | 作用 | 写操作 |
|---|---|---|
| `system_status` | 版本、模式、数据源降级链、交易日历、缓存、后端、策略/自选概况 | 只读 |
| `system_audit` | 最近的写操作审计（谁在什么时候改了什么） | 只读 |

### 文档资源（8）

模型不会一次性拿到全部文档，需要时按 uri 读取：

| uri | 内容 |
|---|---|
| `quantstudio://docs/strategy-spec.md` | 策略编写规范（写代码前必读） |
| `quantstudio://docs/strategy-examples.md` | 完整示例与可复制骨架 |
| `quantstudio://docs/strategy-api.md` | 策略可用 API 参考 |
| `quantstudio://docs/ai-strategy-guide.md` | 让 AI 一次写对策略的流程与提示词 |
| `quantstudio://docs/desktop-gui.md` | Windows 桌面版使用与打包 |
| `quantstudio://docs/README.md` | 项目总览（仓库根 README） |
| `quantstudio://docs/data-dir.md` | 运行时数据目录与文件格式 |
| `quantstudio://docs/docs-index.md` | 文档索引 |

另外声明了模板 `quantstudio://strategies/{strategy_id}`（表示「策略源码也能读」）；
实际读源码请用 `strategy_read_source` 工具。

### 提示词模板（3）

| 名称 | 参数 | 用途 |
|---|---|---|
| `write_strategy` | `idea`（必需）、`universe`（可选） | 从想法到规范代码、lint 迭代、回测验证的完整工作流 |
| `review_backtest` | `strategy_id`（必需）、`symbol`（可选） | 回测复盘：收益 / 回撤 / 夏普 / 胜率 / 换手与费用、过拟合检查、结论 |
| `daily_watch` | `symbols`（可选） | 看盘简报：市场情绪 → 个股异动 → 关注点 |

---

## 4. 典型工作流

### 4.1 写一个策略（模型最常用的路径）

1. `strategy_list` / `strategy_get` 了解现有策略与参数；
2. `strategy_read_source` 读一个风格接近的参考实现；
3. 读资源 `quantstudio://docs/strategy-spec.md` 与 `quantstudio://docs/strategy-examples.md`；
4. `strategy_write_source` 落盘 —— 写入会自动跑策略校验器并返回问题清单；
5. 按问题迭代重写（必要时 `strategy_lint` 复查），直到没有问题；
6. `backtest_run`（需要细看时 `include_series=true`）验证，报告指标与风险。

对应提示词模板：**`write_strategy`**。

### 4.2 盘中 / 盘后看盘

`market_overview` → `market_quotes`（关注标的）→ 每只票 `market_kline`（近 60 根），
输出「市场情绪 → 个股异动 → 关注点」三段式简报，并附数据来源与「不构成投资建议」声明。
对应提示词模板：**`daily_watch`**。

### 4.3 复盘一次回测

`strategy_get` 确认参数 → `backtest_run`（`include_series=true`）→ 让模型按
收益 / 风险 / 风险调整 / 交易质量 / 集中度逐项给数字，再做参数敏感性与区间稳定性检查，
最后给出「值得继续 / 需改进 / 不建议」的结论。对应提示词模板：**`review_backtest`**。

---

## 5. 安全与保护（如实说明）

* **默认是「可读写」**：目的是让模型能真的帮你改策略。启动时加 `--read-only` 即一键只读，
  写工具会从清单里消失并且调用会被拒绝（见下）。
* **写策略的护栏**（`backend/quantstudio/mcp/safety.py` + `tools_strategy.py`）：
  * 策略 id 必须匹配 `^st_[a-z0-9_]{2,36}$`；
  * 只能写 `strategies/local/`（单层文件名 + `realpath` 复核，拒绝 `../`、绝对路径与子目录）；
  * **原子写**：先写临时文件再 `os.replace`，不会出现半截文件；
  * 覆盖 / 删除前自动备份到 `<数据目录>/strategy-backups/`（文件名带时间戳，可回滚）；
  * 单个源文件 **≤ 128 KB**（`MAX_SOURCE_BYTES`）；
  * 危险 import / 调用（网络、文件、随机、时间等）由**策略校验器**直接判错，不是靠模型自觉。
* **审计日志**：每次写操作追加一行 JSON 到 `<数据目录>/mcp-audit.log`
  （时间、工具、参数摘要、结果），也可以用 `system_audit` 让模型自己回看。
* **永不触达真实券商**：交易能力只有本地模拟盘（`paper_*`）与手工持仓账本（`portfolio_*`），
  MCP 层没有、也不会新增任何券商下单通道。
* `stdout` 只允许出现协议消息：日志、异常、调试输出全部走 `stderr`
  （标准输出混入日志会让客户端直接解析崩溃）。

**改成只读模式**：JSON 里不能写注释，所以直接改 `args` ——
`["scripts/mcp_server.py", "--read-only"]`；桌面版 exe 则是 `["--mcp", "--read-only"]`。

---

## 6. 排障

| 现象 | 原因与处理 |
|---|---|
| 客户端显示服务器启动失败 / 红点 | 多半是 `python` 不在 PATH。改成 Python 的**绝对路径**，或 Windows 用 `py -3` 启动器、或直接用桌面版 exe + `--mcp`。 |
| Claude Desktop 看不到工具 | 配置文件路径不对或没重启；完全退出客户端再打开，并看它的日志（macOS：`~/Library/Logs/Claude/mcp*.log`；Windows：`%APPDATA%\Claude\logs`）。 |
| Cursor/Claude Desktop 报找不到 `scripts/mcp_server.py` | 子进程工作目录不是仓库根。把 `args` 改成脚本的**绝对路径**。 |
| 中文输出乱码 / `UnicodeEncodeError` | 配置里保留 `"PYTHONIOENCODING": "utf-8"`；脚本自身也会强制 UTF-8。 |
| 想确认服务器本身没问题 | 在仓库根跑 `python scripts/mcp_server.py --selftest`，退出码 0 表示通过；完整报告写在 `%TEMP%\quantstudio_mcp_selftest.txt`（macOS/Linux 为 `/tmp` 或 `$TMPDIR` 下同名文件）。报告里有握手、工具数、调用只读工具、错误码等检查结果。 |
| 能否手工跟服务器对话 | 可以：`python scripts/mcp_server.py` 后在终端逐行输入 JSON-RPC（stdio 就是一行一条 JSON），`Ctrl+D` / 输入结束即退出。 |
| 为什么不能往 stdout 打日志 | stdio 传输里 stdout 是**协议通道**，多打一行文本会让客户端 JSON 解析失败；所以本服务器所有日志都在 stderr（`[mcp] ...`）。 |

**协议版本**：本服务器声明 **2025-06-18**；客户端若请求 2025-06-18 / 2025-03-26 / 2024-11-05 中任一个，
服务器回显客户端版本（最大化兼容），其它版本则回自己的最高版本，由客户端决定是否降级。

---

## 7. 相关文档

* [strategy-spec.md](strategy-spec.md) —— 策略编写规范（写策略前必读）
* [ai-strategy-guide.md](ai-strategy-guide.md) —— 让 AI 一次写对策略的提示词与自检循环
* [desktop-gui.md](desktop-gui.md) —— Windows 桌面版（含 `--mcp` 模式与打包）
* [README.md](README.md) —— 文档索引
