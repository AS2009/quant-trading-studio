# QuantTrading Studio

**面向真实市场的量化研究 / 模拟交易工具**：真实行情数据源、真实历史回测、可插拔策略库、
自有持仓账户管理与模拟盘下单；界面为原生 HTML/CSS/JS（无构建步骤），后端仅依赖 Flask。

> **重要声明（请先读）**
> - 本项目**默认只做研究与模拟**：不会向任何券商发送真实委托（`trade.mode` 恒为 `paper`）。
>   真实下单需要你自己实现 `backend/quantstudio/trading/adapters.py` 中的适配器并显式开启。
> - 行情来自**公开接口**（新浪实时快照、东方财富 K 线与板块），仅供个人研究学习；
>   接口非官方开放 API，可能变更、限流或停用。**商业用途请购买正规数据服务**（Tushare Pro、Wind、聚宽、米筐、iFinD 等）并替换 Provider 实现。
> - 回测结果**不代表未来收益**；所有指标基于历史数据与简化撮合假设（见「回测口径」），不构成投资建议。
> - 程序不会上传你的持仓、策略或任何本地数据；所有数据都保存在本机 `backend/data/` 下。

---

## 30 秒上手

```bash
git clone <this-repo> && cd quant-trading-studio
./start.sh            # Windows: 双击 start.bat
```

启动后打开 <http://127.0.0.1:8000>：

1. **行情看板** — 真实指数、市场广度与资金、板块涨幅榜、自选股实时行情、个股 K 线（可切换标的与周期）。
2. **策略管理** — 6 个内置可运行策略 + 新建自定义策略（参数化覆盖内置模板）。
3. **回测分析** — 选策略与标的池、设置区间/资金/基准/费用滑点，跑真实历史回测：净值 vs 基准、回撤、
   月度收益、绩效指标（年化/夏普/索提诺/卡玛/Alpha/Beta/胜率/盈亏比/换手/费用）、成交流水、期末持仓。
4. **持仓管理** — 录入你的**真实持仓**（数量/成本/可用数量/现金），实时估值、盈亏与配置分析、权益曲线。
5. **交易（模拟盘）** — 按实时价格模拟成交（T+1、手续费、涨跌停约束），下单/撤单/持仓/成交全部本地记账。
6. **盘口 / L2** — 五档盘口（委比/委差/价差）、逐笔成交（第三方方向标记）、自算四档资金流与主力净额，外加 **L2 工具箱**：大单追踪、资金流分时、封板状态（封单/封成比）、自选池盘口扫描、资金流排行；
   十档 / 逐笔委托 / 委托队列需付费授权，见 [docs/level2.md](docs/level2.md)。

命令行也能用（无需启动网页）：

```bash
python scripts/check_env.py                        # 环境与数据源自检
python scripts/run_backtest.py --list              # 查看可用策略
python scripts/run_backtest.py --strategy st_ma_cross --symbols 600519.SH --start 2023-01-01
python scripts/fetch_data.py --export-csv          # 导出离线 CSV（断网也能研究）
```

---

## Windows 桌面版（原生 GUI，无需浏览器）

同一套核心能力（真实行情 → 策略 → 回测 → 持仓 → 模拟盘）还有**原生 Windows 程序**，
零第三方 GUI 依赖（只用 Python 标准库 tkinter），可直接编译成 exe 双击运行：

```bat
cd desktop
python -m quantstudio_desktop                    :: 从源码启动
python -m quantstudio_desktop --selftest         :: 无界面自检（数据源/策略/回测，退出码 0/1）
powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1   :: 本机打包 exe
```

推送代码后 GitHub Actions（`.github/workflows/build-desktop.yml`）会在 **windows-latest** 上用
PyInstaller 自动编译 onedir + onefile 两种产物，并**对产物跑真实自检**（`--selftest` / `--selftest-gui`
必须 exit 0）后才上传制品；打 `v*` 标签会自动发布 Release 附件。
完整说明见 **[docs/desktop-gui.md](docs/desktop-gui.md)**。

## MCP（给大模型调用）

同一套能力也能通过 **MCP（Model Context Protocol）** 暴露给 Claude Desktop / Claude Code / Cursor 等客户端，
让模型直接查行情、写策略、跑回测、看持仓与模拟盘（纯标准库，克隆即可用，**永不触达真实券商**）。

```bash
python scripts/mcp_server.py              # stdio 服务器（默认可读写）
python scripts/mcp_server.py --read-only  # 一键只读；--selftest 自检，退出码 0/1
```

仓库根的 `.mcp.json` 已配好 Claude Code；客户端接入、41 个工具清单、安全护栏与排障见 **[docs/mcp.md](docs/mcp.md)**。

---

## 架构（分层、可插拔）

```
quant-trading-studio/
├── backend/
│   ├── app.py                     # 唯一入口：python app.py（或 scripts/*.py 做 CLI 任务）
│   ├── quantstudio/               # 核心包（分层依赖单向：api → services → 能力层 → core）
│   │   ├── config.py              # 集中配置（环境变量优先）
│   │   ├── compat.py              # 可选依赖探测（pandas/numpy 预留，缺失自动回退标准库）
│   │   ├── core/                  # 领域模型、抽象接口、交易日历、异常（零依赖）
│   │   ├── data/                  # 行情数据层：sina / eastmoney / csv / sample + 磁盘缓存 + 降级链
│   │   ├── strategies/            # 策略库：6 个内置策略 + 注册表 + 用户自定义策略
│   │   ├── backtest/              # 回测引擎：撮合、账户、绩效指标
│   │   ├── trading/               # 交易通道：模拟盘（默认）+ 真实券商适配点（预留）
│   │   ├── portfolio/             # 真实持仓账户与权益快照
│   │   ├── services/              # 业务编排（含缓存、降级、参数校验）
│   │   └── api/                   # Flask 蓝图（统一信封与 JSON 错误）
│   ├── data_engine.py             # 旧版示例数据引擎：现作为 sample Provider 的离线兜底数据源（非真实行情）
│   ├── data/                      # 运行时数据（自选池/持仓/模拟盘账户/缓存/CSV），格式见 data/README.md
│   └── tests/                     # unittest 测试（零第三方测试依赖）
├── frontend/                      # 原生 SPA（无构建）：见下表
├── scripts/                       # CLI：check_env.py（自检）/ run_backtest.py（回测）/ fetch_data.py（预热导出）
├── requirements.txt               # 仅 flask；pandas/numpy 为可选注释项
├── start.sh / start.bat
└── README.md
```

**扩展方式（每层都留了明确接口）**

| 想做什么 | 改哪里 | 怎么改 |
|---|---|---|
| 换行情源（如 Tushare/Wind） | `quantstudio/data/` | 新写一个 `XxxProvider`，实现 `DataProvider` 协议（7 个方法），在 `data/__init__.py` 注册即可；降级链与缓存自动生效 |
| 加一个策略 | `quantstudio/strategies/local/` | **推荐方式**：复制 `local/_template.py` 写成 `local/<slug>.py`，重启即出现在策略列表；**必须**通过 `python scripts/check_strategies.py` 校验（命名/参数/无未来函数/烟雾回测）。规范与示例见 `docs/` |
| 把自研策略并入库 | `quantstudio/strategies/` + `registry.py` | 作为内置策略发布：文件放包根目录，在 `registry.py` 里 import 并 `register_builtin(cls)`，并加入 `BUILTIN_ORDER` |
| 改交易规则/费用 | `quantstudio/backtest/broker.py`、`core/models.FeeConfig` | 撮合与费用集中在这两处 |
| 加绩效指标 | `quantstudio/backtest/metrics.py` | 新增纯函数并在 `compute_metrics` 里填充 |
| 接真实券商（QMT/富途/easytrader） | `quantstudio/trading/adapters.py` | 实现 `Broker` 协议的 `_do_submit`，并显式开启 `QUANTSTUDIO_ENABLE_LIVE_TRADING=1` |
| 加数据源之外的本地数据 | `data/csv/` | 按 `backend/data/README.md` 的 CSV 规范导入，或用 `scripts/fetch_data.py --export-csv` 导出 |

### 前端模块（`frontend/js/`，纯原生、按职责拆分）

| 文件 | 职责 |
|---|---|
| `api.js` | 全部接口封装、统一错误解析、超时与重试 |
| `store.js` | 全局状态与订阅（当前视图、自选池、选中标的、策略列表、数据源状态） |
| `ui.js` | 通用 UI 原语：toast、弹窗、确认框、格式化（`fmt`）、表格与空状态助手 |
| `charts.js` | ECharts 封装（净值/回撤/月度/K线/板块/环形图/权益曲线）+ 尺寸自适应 |
| `views/market.js` | 行情看板：指数、市场概况、板块、自选股（增删）、个股 K 线 |
| `views/strategies.js` | 策略管理：列表/筛选/新建（参数表单由 `param_schema` 动态生成）/删除 |
| `views/backtest.js` | 回测分析：参数表单、指标卡、四张图、成交流水、期末持仓、警告 |
| `views/portfolio.js` | 持仓管理：账户总览、真实持仓录入/删除、现金与模式切换、权益曲线 |
| `views/trade.js` | 交易（模拟盘）：下单、撤单、委托与成交、账户概览、重置 |
| `views/level2.js` | 盘口 / L2：五档盘口、逐笔成交、四档资金流 |
| `main.js` | 初始化、Tab 路由、数据源状态条、全局错误兜底 |

新增一个页面只需在 `js/views/` 加一个模块并在 `main.js` 注册，无需构建步骤。

---

## 数据来源与降级链

默认 `QUANTSTUDIO_DATA_SOURCE=auto`，**方法级**降级（链中任一源失败自动切换下一个），
因此单个数据源被限流、改接口或断网都不会让接口报错：

```
```
实时快照：新浪(GBK) ─▶ 腾讯 ─▶ 东方财富 ─▶ 同花顺 ─▶ 磁盘缓存(可能滞后) ─▶ CSV ─▶ 示例数据
历史K线 ：东方财富   ─▶ 腾讯 ─▶ 新浪     ─▶ 同花顺 ─▶ 磁盘缓存          ─▶ CSV ─▶ 示例数据
板块行情：东方财富   ─▶ 腾讯 ─▶ CSV      ─▶ 示例数据
市场广度：东方财富   ─▶ 腾讯 ─▶ CSV      ─▶ 示例数据
盘口/逐笔：腾讯(5 档 + 逐笔) ─▶ 新浪(5 档) ─▶ 本地导入文件（详见 docs/level2.md）
```

四个真实源的定位：
- **新浪** `hq.sinajs.cn`：GBK 实时快照，速度快，作为默认实时源（盘口数量单位是**股**）；
- **腾讯** `qt.gtimg.cn` / `web.ifzq.gtimg.cn` / `proxy.finance.qq.com`：实时快照 + **前复权历史 K 线**（默认历史备份源）+ 行业板块（含板块涨跌家数）+ **五档盘口与逐笔成交**（唯一提供逐笔方向标记的免费源）；
- **东方财富** `push2*.eastmoney.com`：历史 K 线、快照、板块、资金流，字段最全，作为默认历史/板块源；
- **同花顺** `d.10jqka.com.cn`：公开 JSONP 接口的当日分时与日线，作为快照/历史的又一路备份（**不含**其付费 Level-2 内容）。

> 实测提醒：东方财富对高频访问较敏感（本机在连续探测后曾出现 `RemoteDisconnected` 全站拒连），
> 此时程序会自动切到腾讯，`meta.source` 会如实显示 `tencent`。这也是「多源互备」存在的意义。

- 每个接口响应都带 `meta`：`{"source": "sina|eastmoney|csv|sample|cache", "stale": bool, "offline": bool, "as_of": "...", "latency_ms": int}`；
- 界面顶部状态条据此显示 **真实行情 / 缓存数据 / 离线演示数据**，`offline=true` 时会明确提示"不可用于交易决策"；
- 缓存为磁盘 JSON，按 `config.CacheTTL` 过期（行情 5s、K线当日 5min、历史 K 线 6h、板块 30s、广度 60s），过期数据仍可作为兜底返回；
- `QUANTSTUDIO_OFFLINE=1` 时完全不发起网络请求（只用缓存/CSV/示例）；
- `QUANTSTUDIO_DATA_SOURCE=csv` 时只读 `data/csv/`（自有数据/离线研究）。

**市场广度口径**（会在 `meta.notes` 与 `MarketBreadth.source` 中标注来源）：
- 东方财富口径：86 个行业板块 `f104/f105/f106` 汇总；
- 腾讯口径（东财不可用时）：31 个行业板块 `zgb`（上涨/下跌家数）汇总 —— **含新三板等，合计家数大于沪深 A 股实际家数**，仅作情绪近似；
- 两市成交额 = 上证指数 + 深证成指成交额；主力净流入为行业板块 `zljlr` 汇总；
- 涨跌停家数腾讯不提供（置 0，notes 说明），北向资金已停止实时披露（返回 `null`）。

要更严谨的口径，替换 `data/eastmoney.py` / `data/tencent.py` 中 `breadth()` 的实现即可。

---

## 回测口径（务必了解，避免误读结果）

- **数据**：前复权日线（`adjust=qfq`），按标的共同交易日对齐，停牌当日不做交易；
- **撮合**：默认市价单以**当日收盘价**成交（可按需改为开盘价模型），成交价计入滑点 —— `slippage_bps`
  （比例，默认 2bp，买入上浮/卖出下浮）与 `slippage_ticks × tick_size`（最小变动价位的跳数）**叠加**，
  并按 tick 取整到**对买方不利**的方向；
- **费用**（`FeeConfig`，可覆盖）：佣金万 2.5（单笔最低 5 元）、印花税卖出 0.05%、过户费双边 0.001%、
  流量费 `flow_fee`（**每笔固定**，默认 0 元，买卖各收一次，兼容旧口径）；
- **买入量反解**：`core/costs.max_buy_qty` 按可用资金**含滑点与全部费用**精确反解整手数，
  策略 `self.max_buy_qty()` 与撮合的资金缩量共用同一实现（费率再高也不会「策略算得出、撮合买不进」）；
- **模拟盘同口径**：`trading/paper.py` 的成交价与费用同样走 `core/costs`（流量费、tick 滑点都生效），
  回测与模拟盘不会出现两套算法（`trading.broker_base.compute_fee` 已改为复用它）；
- **交易约束**：买入数量向下取整到 100 股整数倍；卖出受 **T+1**（当日买入次日可卖）与可卖数量限制；
  **涨跌停不成交**（|当日涨跌幅| ≥ 9.8% 视为封板，封板方向无法成交）；资金不足时自动缩量，缩到 0 则放弃；
- **无未来函数**：策略只能通过 `ctx.history()` 读取**截至当日**的数据（引擎层保证，测试中有专项校验）；
- **绩效口径**：年化收益 = (1+累计收益)^(252/交易日数) − 1（几何）；年化波动 = 日收益标准差×√252；
  夏普 = (年化收益 − 无风险利率)/年化波动（默认无风险利率 0）；索提诺用下行波动；卡玛 = 年化收益/最大回撤；
  最大回撤 = max(1 − 权益/历史峰值) 并给出起止日期；胜率与盈亏比按**平仓交易**统计；Alpha/Beta 用日收益对基准回归；
  另有最长连涨/连跌交易日数、单笔平仓收益极值（FIFO 配对）、日均交易次数（**共 25 项**，字段见 `backtest/metrics.py`）；
- **结果可复现**：相同输入 → 完全相同输出（无随机数、无集合遍历顺序依赖）。

---

## 内置策略（参数可在界面或 API 覆盖）

| id | 名称 | 类别 | 默认参数 | 逻辑 |
|---|---|---|---|---|
| `st_ma_cross` | 双均线趋势策略 | 趋势跟踪 | `short_ma=20, long_ma=60, position_pct=0.95` | 短均线上穿长均线买入，下穿清仓 |
| `st_momentum` | 动量轮动策略 | 动量 | `lookback=20, top_n=3, rebalance_days=20` | 按 N 日涨幅排序持有最强 K 只，定期调仓 |
| `st_grid` | 网格交易策略 | 震荡市 | `grid_pct=0.03, levels=10, base_qty=1000` | 按固定步长分档低买高卖 |
| `st_lowvol` | 低波动率优选策略 | 稳健 | `lookback=60, top_n=5, rebalance_days=5` | 选波动率最低的 K 只等权持有 |
| `st_rsi` | RSI 均值回归策略 | 均值回归 | `rsi_period=14, buy_th=30, sell_th=70` | RSI 超卖买入、超买卖出 |
| `st_turtle` | 海龟突破策略 | 趋势跟踪 | `entry_high=20, exit_low=10, atr_mult=2.0` | 突破 N 日高点开仓，跌破 M 日低点/ATR 止损离场 |

### 自己写策略（本地代码策略）

```bash
cp backend/quantstudio/strategies/local/_template.py backend/quantstudio/strategies/local/my_alpha.py
# 改文件名 / id / 类名三处（breakout_atr.py ↔ st_breakout_atr ↔ BreakoutAtrStrategy）
python scripts/check_strategies.py --path backend/quantstudio/strategies/local/my_alpha.py   # 必须 0 error
python scripts/run_backtest.py --strategy st_my_alpha --symbols 600519.SH --start 2024-01-01
```

规范与文档（**写策略前请先读**）：

| 文档 | 内容 |
|---|---|
| [`docs/strategy-spec.md`](docs/strategy-spec.md) | 命名、目录结构、文件格式、必守规则 R1–R12、校验要求、检查清单 |
| [`docs/strategy-api.md`](docs/strategy-api.md) | `ctx` 上下文、`BaseStrategy` 助手、参数 schema、可用导入清单 |
| [`docs/strategy-examples.md`](docs/strategy-examples.md) | 三个完整示例（单标的突破 / 多标的轮动 / 多指标共振）+ 常见错误对照 |
| [`docs/ai-strategy-guide.md`](docs/ai-strategy-guide.md) | 用 AI 写策略的提示词模板、修复动作表、交付格式 |
| [`docs/level2.md`](docs/level2.md) | 盘口 / L2：能力边界、口径（盘口单位、逐笔方向、资金流分档）、HTTP 与 MCP 用法、接入付费 L2 的两条路 |

`local/` 下已附带三个通过校验的示例：`st_breakout_atr`（通道突破 + ATR 止损）、`st_rotation_topn`（动量轮动 TopN）、
`st_macd_adx`（MACD 金叉 + ADX 趋势过滤 + 布林中轨离场，演示 `macd/kdj/boll/adx/cross` 等新指标怎么用）。
加载失败的文件只会在 `/api/system/status` 的 `local.errors` 中记录，不会影响应用启动。

> 策略只是**示例实现**，用于演示框架如何工作；它们没有经过参数寻优，不代表可盈利。请自行研究、验证与小资金实测。

---

## 配置（全部可选，环境变量前缀 `QUANTSTUDIO_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_SOURCE` | `auto` | `auto/sina/eastmoney/csv/sample` |
| `OFFLINE` | `0` | `1` = 只用本地数据，不联网 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址与端口 |
| `DATA_DIR` / `CACHE_DIR` | `backend/data` / `backend/data/cache` | 数据与缓存目录 |
| `INITIAL_CASH` | `1000000` | 模拟盘初始资金 |
| `BENCHMARK` | `000300.SH` | 回测默认基准（沪深300） |
| `HTTP_TIMEOUT` / `HTTP_RETRIES` | `8` / `2` | 数据源请求超时与重试 |
| `KLINE_DAYS` / `MAX_KLINE_DAYS` | `250` / `1200` | 看板默认 / 最大 K 线根数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 日常使用建议（现实工作流）

1. **先录持仓**：打开「持仓管理」把你券商的真实持仓（代码/数量/成本/可用数量）和可用现金录入；之后每次打开都能看到实时盈亏与配置。
2. **自选池**：把关注的标的加入自选（支持 `600519`、`sh600519`、`600519.SH` 三种写法）。
3. **研究策略**：在「回测分析」选策略与标的池，调整区间/资金/费率/滑点；先看**回撤与换手**，再看收益。
4. **纸上验证**：把参数固定下来后，用「交易」页的模拟盘按信号下单，跑一段时间，对比模拟盘与回测的偏差。
5. **数据留档**：`python scripts/fetch_data.py --export-csv` 定期导出，便于复现与离线研究。
6. **接真实下单前**：务必阅读 `trading/adapters.py` 的接入说明；先跑信号对比（影子模式），并从小资金开始。

---

## API 速查

统一响应信封：`{"data": …, "as_of": "YYYY-MM-DD HH:MM:SS", "meta": {source, stale, offline, ...}}`；
`/api/` 下的错误一律返回 JSON：`{"error": "可读原因", "code": 400, "field": null}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/system/status` | 数据源/离线状态/缓存/计算后端/交易通道 |
| GET | `/api/market/overview` | 指数、市场广度、成交额、资金 |
| GET | `/api/market/sectors?limit=20` | 板块涨幅榜 |
| GET | `/api/market/quotes?codes=600519.SH,300750.SZ` | 实时快照（缺省=自选池） |
| GET | `/api/level2/<code>/orderbook` | 五档盘口 + 委比/委差 + 能力协商 |
| GET | `/api/level2/<code>/ticks?limit=120` | 逐笔成交（含多空统计） |
| GET | `/api/level2/<code>/flow?limit=2000` | 四档资金流与主力净额（自算） |
| GET | `/api/level2/<code>/big-orders?threshold=&limit=` | 大单追踪（单笔金额 ≥ 阈值，时间倒序） |
| GET | `/api/level2/<code>/flow-series?limit=` | 资金流分时序列（分钟聚合 + 累计净额） |
| GET | `/api/level2/<code>/seal` | 封板状态（涨停/跌停、封单额、封成比） |
| GET | `/api/level2/scan?codes=&limit=` | 盘口异动扫描（缺省=自选池，最多 10 只，按委比降序） |
| GET | `/api/level2/flow-rank?codes=&top=` | 资金流排行（缺省=自选池，按主力净额降序） |
| GET | `/api/market/kline?code=&days=&freq=day&adjust=qfq` | K 线 |
| GET/POST/DELETE | `/api/watchlist` `/api/watchlist/<code>` | 自选池读写 |
| GET/POST | `/api/strategies` | 策略列表 / 新建自定义策略 |
| GET/DELETE | `/api/strategies/<id>` | 策略详情 / 删除 |
| GET/POST | `/api/backtest/<id>` | 回测（区间、资金、基准、标的、费率与滑点均可传参；费率含 `flow_fee` 流量费、`slippage_ticks` 跳数滑点、`lot_size`） |
| GET | `/api/portfolio/overview` `/holdings` `/equity` | 账户总览 / 持仓 / 权益曲线 |
| POST/DELETE | `/api/portfolio/holdings` | 录入 / 删除真实持仓 |
| POST | `/api/portfolio/cash` `/api/portfolio/mode` | 设置现金 / 切换 manual·paper 模式 |
| GET/POST/DELETE | `/api/orders` `/api/orders/<id>` | 模拟盘委托（下单/撤单） |
| GET | `/api/fills` | 模拟盘成交 |
| POST | `/api/orders/reset` | 重置模拟盘账户 |

---

## 测试

```bash
cd backend && python -m unittest discover -s tests -v      # 全量：核心层/数据层/回测/策略/交易/接口/跨层集成
python scripts/check_env.py                                # 环境与数据源一键自检
```

- 共 **190+ 个用例**，只依赖标准库（`unittest`），零测试框架依赖；
- 关键覆盖：撮合规则（费用/T+1/涨跌停/缩量）、指标手算对照、**无未来函数专项校验**（把未来行情放大 10 倍不影响当日决策）、
  结果可复现（两次运行逐字节一致）、数据源降级链（主源故障→缓存→CSV→示例）、接口错误码与 JSON 契约、离线模式零网络请求；
- 涉及真实网络的用例在断网时**自动跳过**，不影响离线开发。

## 免责声明

本项目为**开源研究工具**，按"现状"提供，不对数据的准确性、完整性、及时性或任何交易结果作任何担保。
行情数据来自第三方公开接口，可能存在延迟、缺失或错误；回测与模拟撮合为简化模型，与真实成交存在差异。
使用者需自行承担全部风险与合规责任，**任何实盘交易决策与后果均由使用者自负**。市场有风险，投资需谨慎。
