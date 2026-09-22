# backend/data —— 运行时数据目录

本目录存放**用户自己的数据**（会被程序读写）。除示例文件外，其它文件都是首次运行时自动生成的，
可以安全删除以恢复初始状态。

| 文件 / 目录 | 作用 | 是否自动生成 |
|---|---|---|
| `watchlist.json` | 自选股池 `{"codes":["600519.SH",...]}` | 首次启动用默认池生成 |
| `portfolio.json` | 真实持仓账户（手工录入或界面增删改） | 首次访问持仓页生成 |
| `user_strategies.json` | 用户自定义策略 | 新建策略时生成 |
| `paper_account.json` | 模拟盘账户（现金/持仓/委托/成交） | 首次下单时生成 |
| `equity_history.json` | 每日账户权益快照（真实权益曲线来源） | 每日首次访问时追加 |
| `holidays.json` | 交易日历节假日表（可选，见示例） | 需手动创建 |
| `cache/` | 行情磁盘缓存（可随时删除） | 自动 |
| `csv/` | 本地 CSV 数据源（离线/自有数据） | 需手动导入 |

## 代码格式

统一使用 `代码.市场` 写法：`600519.SH`（沪）、`300750.SZ`（深）、`000001.SH`（上证指数）、
`159915.SZ`（ETF）。界面与接口也接受 `600519`、`sh600519`、`SH600519`，后端会自动规范化。

## 本地 CSV 数据源（`QUANTSTUDIO_DATA_SOURCE=csv` 时启用）

把下列文件放进 `data/csv/`，程序在**实时数据源不可用**或**显式指定 csv** 时读取它们。
字段单位：`amount` 为**元**；成交量在 `quotes.csv` 中为**股**，在 K 线文件中为**手**（内部统一换算成万手展示）。

1. `csv/quotes.csv`（实时快照）
   ```
   code,name,price,prev_close,open,high,low,volume,amount,ts
   600519.SH,贵州茅台,1252.57,1257.12,1259.00,1259.95,1250.80,2501700,3135910045,2026-09-21 15:00:00
   ```
2. `csv/kline/600519.SH.csv`（单标的历史日线，按日期升序）
   ```
   date,open,high,low,close,volume,amount,change_pct,turnover_pct
   2026-09-18,1262.99,1265.88,1256.10,1257.12,24891,3135849108,-0.78,0.20
   ```
   也支持单文件 `csv/kline_all.csv`，需额外包含 `code` 列。
3. `csv/sectors.csv`（板块）：`code,name,change_pct,net_inflow,up_count,down_count,leading_stock`
4. `csv/breadth.csv`（市场广度，两列）：`key,value`，key ∈ up/down/flat/limit_up/limit_down/total/total_amount/main_net_inflow

> 提示：`python scripts/fetch_data.py --export-csv` 可以把在线抓到的真实行情导出成上述 CSV，
> 之后即使断网也能用 `QUANTSTUDIO_DATA_SOURCE=csv` 或离线模式继续研究和回测。

## 行情数据来源与合规

默认使用**公开行情接口**（新浪实时快照、东方财富 K 线与板块），仅用于个人研究学习。
这些接口非官方开放 API，可能变更或限流；商业使用请购买正规数据服务（如 Tushare Pro、
聚宽、米筐、Wind、同花顺 iFinD）并替换 `backend/quantstudio/data/` 下的 Provider 实现。
程序不会上传你的持仓、策略与任何本地数据。
