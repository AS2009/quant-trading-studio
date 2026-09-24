# Windows 桌面版（原生 GUI）使用与打包指南

桌面版把同一套核心能力（真实行情 → 策略 → 回测 → 持仓 → 模拟盘）做成**原生 Windows 程序**：
双击 exe 就能用，不需要浏览器、不需要装 Python、不启动任何 Web 服务。

| | Web 版 | 桌面版 |
|---|---|---|
| 界面 | 浏览器里的 HTML/CSS/JS | Tkinter 原生窗口（Windows 上是真正的系统窗口） |
| 运行方式 | `python app.py` → <http://127.0.0.1:8000> | 双击 `QuantTradingStudio.exe`，或 `python -m quantstudio_desktop` |
| 依赖 | Flask（+ 可选 pandas/numpy） | 仅 Python 标准库（tkinter） |
| 能力 | 完全一致：**同一个 `quantstudio` 核心包** | 完全一致（GUI 直接调用服务层，不经 HTTP） |
| 适合 | 多标签研究、脚本调用 API | 日常盯盘、单手操作、离线可用 |

> 桌面版**不发起真实委托**，与 Web 版一样只做研究与模拟交易（`trade.mode = paper`）。

---

## 1. 安装与运行

### 方式 A：用编译好的程序（推荐给使用者）

1. 打开仓库的 **Actions → build-desktop**（或 **Releases** 页面），下载 `QuantTradingStudio-windows-x64` 制品包；
2. 解压到任意目录（例如 `D:\QuantTradingStudio`），双击 **`QuantTradingStudio.exe`**；
3. 首次启动会创建数据目录 `%LOCALAPPDATA%\QuantTradingStudio\data`，无需管理员权限。

单文件版 `QuantTradingStudio.exe`（onefile）也在制品里，体积更小，但每次启动要解压到临时目录、首次较慢，
**推荐用 onedir 目录版**。

### 方式 B：从源码运行（推荐给改代码的人）

```bat
cd desktop
python -m quantstudio_desktop                 :: 直接启动
python -m quantstudio_desktop --view backtest  :: 直接打开回测页
```

要求 Python **3.9+ 且自带 tkinter**（Windows 官方安装包默认带；若报「缺少 tkinter」请重新运行安装包并勾选 tcl/tk）。

### 方式 C：本机自己打包 exe

```powershell
# 在仓库根目录执行；脚本会自动装依赖、跑测试、自检源码、打包 onedir+onefile、再自检产物
powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1

# 只要单文件版；跳过测试（不推荐）
powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1 -OneFile -SkipTests
```

macOS / Linux 上等价脚本是 `desktop/build/build_unix.sh`（用于本地验证；真正交付的是 Windows 产物）。

---

## 2. 界面导览

```
┌──────────────────────────────────────────────────────────────────────┐
│ 文件  视图  工具  帮助                                              │  ← 菜单栏
├──────────────────────────────────────────────────────────────────────┤
│ ● 数据源：新浪实时 · 行情 12:31:05 │ 数据时点：…· 策略 8（内置 6/本地 2）│  ← 顶栏徽标
├───────────┬──────────────────────────────────────────────────────────┤
│ 行情看板  │                                                          │
│ 策略管理  │                    当前页面内容                          │
│ 回测分析  │                                                          │
│ 持仓管理  │                                                          │
│ 交易(模拟)│                                                          │
│ 盘口/L2   │                                                          │
├───────────┴──────────────────────────────────────────────────────────┤
│ 就绪   ⏳                                   2026-09-22 12:31:05      │  ← 状态栏 + 时钟
└──────────────────────────────────────────────────────────────────────┘
```

* **顶栏徽标**：绿=真实数据、黄=磁盘缓存、红=数据源不可用/离线演示数据；右侧显示数据时点与策略数量。
* **状态栏**：左侧是当前操作提示，出现 ⏳ 表示有后台任务在跑（行情抓取、回测都不会卡住界面）。
* **右下角气泡（toast）**：成功/失败/警告提示，几秒后自动消失。

### 快捷键

| 快捷键 | 作用 |
|---|---|
| `Ctrl+R` / `F5` | 刷新当前页（重新拉数据） |
| `Ctrl+1` … `Ctrl+6` | 依次切换到 行情 / 策略 / 回测 / 持仓 / 交易 / 盘口 / L2 |
| `Ctrl+Q` | 退出 |
| `Esc` | 关闭弹窗 / 取消表单 |

### 菜单

* **文件**：刷新当前页、刷新数据源状态、打开数据目录、打开文档目录、退出
* **视图**：六个页面（与左侧导航一致）
* **工具**：数据源自检…（当场探测各数据源连通性）、清空回测缓存、重置模拟盘账户…
* **帮助**：关于（版本/数据目录）、如何编写策略（打开 `docs/strategy-spec.md`）

---

## 3. 六个页面能做什么

| 页面 | 主要内容 | 典型操作 |
|---|---|---|
| **行情看板** | 指数卡片、市场广度与资金、板块涨幅榜、自选股实时行情、个股 K 线（含成交量） | 输入代码加自选；点自选股切 K 线；切换日/周/月周期；`Ctrl+R` 刷新 |
| **策略管理** | 策略清单（内置 / `strategies/local` / 用户），参数 schema 表单，校验结果 | 选策略→设参数→「查看回测」直接跳到回测页；新建自定义策略；查看策略说明 |
| **回测分析** | 参数区（策略、标的池、区间、初始资金、基准、费用与滑点：佣金率/单笔最低佣金/流量费、比例滑点 + 跳数滑点）、净值 vs 基准、回撤曲线、月度收益、绩效指标、成交流水、期末持仓 | 选好参数点「开始回测」；在成交流水里核对每笔买卖与费用 |
| **持仓管理** | 录入真实持仓（代码/数量/成本/可用数量/现金），实时估值、盈亏、行业与单票占比、权益曲线 | 「新增/编辑/删除持仓」；「保存快照」把当日权益写进曲线 |
| **交易（模拟盘）** | 模拟账户资产、下单（买/卖、价格、数量）、订单列表（撤单）、成交流水、当前持仓 | 按实时价模拟成交（T+1、手续费、涨跌停约束），全部本地记账 |
| **盘口 / L2** | 五档盘口（卖 5→卖 1→买 1→买 5，卖绿买红）、逐笔成交（时间/价格/手数/金额/方向，最多 60 行）、资金流分档（超大单/大单/中单/小单净额与买入占比 + 主力净额与占比）、指标卡（现价/涨跌幅/委比/委差/外盘/内盘） | 输入代码点「查询」（`Ctrl+6` 直达）；需要盯盘时勾「自动刷新（3 秒）」；页面顶部灰字标注数据边界：五档是公开源**快照**、逐笔方向是**第三方盘口标记**（非交易所 Level-2），十档/逐笔委托/委托队列需付费授权 |

数据落地在本地文件（`portfolio.json`、`orders.json`、`fills.json` 等），可随时用**文件 → 打开数据目录**查看或备份。

---

## 4. 数据目录与自定义策略

| 运行方式 | 数据目录 |
|---|---|
| 源码运行 | `<仓库>/backend/data/` |
| 打包运行 | `%LOCALAPPDATA%\QuantTradingStudio\data`（程序目录可能只读，所以放用户目录） |
| 任意 | 设环境变量 `QUANTSTUDIO_DATA_DIR` 可覆盖（便于便携版/多账户） |

**自定义策略**：源码运行时把 `py` 文件丢进 `backend/quantstudio/strategies/local/`，重启即可在策略页看到
（`origin=local`）。**打包后的程序无法热插拔策略**——策略源码在只读的解包目录里，需要把文件放进
`backend/quantstudio/strategies/local/` 后重新打包（spec 会自动收集该目录下所有 `.py`）。

写策略前请先读 [`strategy-spec.md`](strategy-spec.md)，并用 `python scripts/check_strategies.py` 自检。

---

## 5. 命令行参数

```bat
python -m quantstudio_desktop --help          :: 参数说明
python -m quantstudio_desktop --version
python -m quantstudio_desktop --selftest      :: 无界面自检：数据源 → 策略 → 回测 → 报告
python -m quantstudio_desktop --selftest-gui  :: 构建整个窗口与所有页面后销毁（CI 用）
python -m quantstudio_desktop --view market    :: 指定启动页（market/strategies/backtest/portfolio/trade/level2）
```

`--selftest` / `--selftest-gui` **退出码 0 表示通过、1 表示失败**，并会把完整输出写到：

```bat
%TEMP%\quantstudio_selftest.txt
```

打包后的程序没有控制台，这个报告文件就是排障的主要依据（CI 也读它）。

---

## 6. GitHub Actions 自动编译

仓库里有两个工作流，推送到 GitHub 后自动生效：

| 工作流 | 触发 | 做什么 |
|---|---|---|
| `.github/workflows/test.yml` | push / PR | Linux 上跑核心测试（3.9/3.11/3.12）+ 策略规范校验 + `compileall`；另有 xvfb 下的桌面测试与 GUI 自检（不阻塞） |
| `.github/workflows/build-desktop.yml` | push 到 main/master、打 `v*` 标签、PR、手动触发 | 先跑核心测试与**桌面测试（142 项）** → 在 **windows-latest** 用 PyInstaller 打包 onedir + onefile → **对产物跑真实自检** → 上传制品 → 打标签时发布 Release |

### 取回编译结果

1. Actions → 选择一次成功的 `build-desktop` 运行；
2. 页面底部 **Artifacts** 下载 `QuantTradingStudio-windows-x64`（含 zip、单文件 exe、`quantstudio_selftest.txt`）。

### 发布版本

```bash
git tag v1.0.0 && git push origin v1.0.0
```

打标签后工作流会额外创建 GitHub Release，并把 zip 与单文件 exe 附在 Release 上——
使用者从 Releases 页面下载即可，无需自己编译。

### 产物自检都验了什么

编译完成后 CI 会**真正运行 exe**（`Start-Process -Wait` 读退出码）：

1. `QuantTradingStudio.exe --selftest` → 必须 exit 0（数据源、策略库、回测引擎在打包环境可用）；
2. `QuantTradingStudio.exe --selftest-gui` → 必须 exit 0（窗口与六个页面能构建，证明 tkinter/tcl 被正确打包）；
3. 单文件版 `--selftest` → 必须 exit 0；
4. 打印 `%TEMP%\quantstudio_selftest.txt` 内容便于排查。

这三步能挡住绝大多数打包事故：漏收数据文件、tcl/tk 缺失、相对导入失败、只读数据目录等。

---

## 7. 打包实现要点（改打包配置前先读）

* **两份 spec**：`desktop/build/quantstudio.spec`（onedir，推荐）、`quantstudio-onefile.spec`（onefile）。
  `pathex` 由 `SPECPATH` 推导，不写死路径；`console=False` 隐藏黑框；图标与版本资源**缺失自动降级**，不会让 CI 失败。
* **hiddenimports**：核心包、桌面页/控件、`tkinter*`、`ssl`、`http.client` 等；动态导入的页面模块必须列进去。
  新增页面时，同时改 `desktop/quantstudio_desktop/views/__init__.py` 的 `VIEW_SPECS` 与 spec 的 `hiddenimports`。
* **local 策略**以源码形式进包（`datas`），否则 `registry.discover_local()` 扫不到。
* **入口相对导入**：spec 会生成一个 runtime hook，把 `__main__.__package__` 补成 `quantstudio_desktop`，
  否则打包后第一行 `from .app import main` 就 `ImportError`。
* **无控制台保护**：`boot.ensure_stdio()` 把 `None` 的 stdout/stderr 指向 `os.devnull`，避免 `print()` 崩窗口。
* **图标**：`desktop/build/icon.ico`（16/24/32/48/64/128/256 七种尺寸）由
  `python desktop/build/make_icon.py` **纯标准库**生成（不依赖 Pillow），配色取自 `theme.COLORS`。
* **体积（CI 实测，v1.2.0）**：目录版 zip **14.0 MB**、单文件 exe **14.0 MB**（Python 3.12 / windows-latest）；
  本机 macOS 打包是 11 MB / 4.4 MB。已 `excludes` 掉 flask/jinja2/pytest/numpy/pandas/matplotlib。
* PyInstaller 引导器**可能被个别杀软误报**，正式分发建议对 exe 做代码签名，或优先分发 onedir 目录版。

---

## 8. 排障

| 现象 | 处理 |
|---|---|
| 双击没反应 / 窗口一闪而过 | 用目录版并在 cmd 里执行 exe 看报错；或读 `%TEMP%\quantstudio_selftest.txt` |
| 报「缺少 tkinter」 | Windows 重新运行 Python 安装包勾选 tcl/tk；或直接用编译好的 exe（自带 tkinter） |
| 顶栏徽标变红「数据源不可用」 | 网络/代理问题；接口是非官方公开行情，可能限流。程序会自动降级到磁盘缓存/演示数据并标注来源 |
| 数据想换个位置/做成便携版 | 设 `QUANTSTUDIO_DATA_DIR=D:\qs-data` 再启动 |
| 想确认这台机器能不能跑 | 命令行加 `--selftest`，看退出码与 `%TEMP%\quantstudio_selftest.txt` |
| 界面字号太小/太大 | 调整 Windows 显示缩放（Tk 会跟随系统 DPI），或改 `theme.init(root, base_size=…)` |

---

## 9. 相关文档

* [`strategy-spec.md`](strategy-spec.md) — 策略编写规范（桌面版策略页「帮助 → 如何编写策略」也是打开它）
* [`strategy-api.md`](strategy-api.md) / [`strategy-examples.md`](strategy-examples.md) — 策略 API 与示例
* [`desktop/README.md`](../desktop/README.md) — 桌面版开发速查（目录结构、测试、spec 细节）
* [`desktop/build/README.md`](../desktop/build/README.md) — 打包脚本与产物说明、Q&A
* [`README.md`](../README.md) — 项目总览与后端架构
