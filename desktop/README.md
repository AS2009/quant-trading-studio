# QuantTrading Studio 桌面版（Windows 原生 GUI）

Tkinter/ttk 原生窗口的量化工作台：**不需要浏览器、不需要端口、不需要 Flask**，双击 exe 就能用。
同一个核心（`backend/quantstudio`）的两个前端之一 —— Web 版见仓库根 `README.md`。

```
desktop/
├── quantstudio_desktop/        桌面程序包（源码即全部，无第三方依赖）
│   ├── __main__.py             python -m quantstudio_desktop 入口（GUI / --selftest / --selftest-gui）
│   ├── app.py                  主窗口：菜单栏、顶栏（数据源徽标）、左侧导航、内容区、状态栏、提示层
│   ├── boot.py                 启动引导：找核心包（源码/打包两种形态）、确定数据目录、兜底 stdout/stderr
│   ├── services.py             GUI 服务桥接 + TaskRunner（耗时操作丢后台线程，结果回主线程渲染）
│   ├── theme.py                深色金融风配色与字体（ttk 用 clam 主题以便 Windows 上完整着色）
│   ├── selftest.py             无界面自检 / GUI 自检（CI 验收用）
│   ├── widgets/                charts（Canvas 绘图）/ table（Treeview）/ cards / forms / toast
│   └── views/                  market 行情 / strategies 策略 / backtest 回测 / portfolio 持仓 / trade 模拟盘
├── tests/                      桌面测试（unittest，需要 tkinter）
├── build/                      打包配置（spec / 脚本 / 版本资源 / 说明）→ 见 build/README.md
└── README.md                   本文件
```

---

## 1. 环境要求

| 场景 | 要求 |
|---|---|
| 跑源码 | Python **3.9+**，且自带 **tkinter**（python.org 官方安装包 / 系统 `/usr/bin/python3`；Linux 需 `apt install python3-tk`） |
| 跑打包产物 | Windows 10/11，**什么都不用装**（Python 与依赖都在产物里） |
| 打包 | `python -m pip install -r requirements-desktop.txt`（只有 PyInstaller） |

运行时第三方依赖：**零**。行情用标准库 `urllib`，回测/绩效/策略全部标准库实现，图表用 `tk.Canvas` 手绘。

---

## 2. 运行

```bash
# 方式一（推荐）：在 desktop/ 目录下运行
cd desktop
python -m quantstudio_desktop                    # 启动图形界面
python -m quantstudio_desktop --version          # 版本
python -m quantstudio_desktop --view backtest    # 直接打开某个页面
```

```bash
# 方式二：把 desktop/ 加入 sys.path
PYTHONPATH=desktop python -m quantstudio_desktop
```

Windows 上可双击打包产物 `QuantTradingStudio.exe`，或用 `start.bat` 启动 Web 版（见根 README）。

### 自检 / 排障

```bash
python -m quantstudio_desktop --selftest         # 数据源 + 策略库 + 回测 + 界面依赖，退出码 0/1
python -m quantstudio_desktop --selftest-gui     # 构建主窗口与全部页面后销毁，退出码 0/1
```

自检报告同时写入 `%TEMP%\quantstudio_selftest.txt`（macOS/Linux：`$TMPDIR/quantstudio_selftest.txt`），
无控制台的打包版也能拿到输出：

```powershell
Start-Process .\QuantTradingStudio.exe -ArgumentList "--selftest" -Wait -PassThru   # 读 ExitCode
Get-Content "$env:TEMP\quantstudio_selftest.txt"
```

---

## 3. 数据放在哪

* **源码运行**：仓库里的 `backend/data/`（缓存 `cache/`、自有 CSV `csv/`、持仓与模拟盘账本）。
* **打包运行**：`%LOCALAPPDATA%\QuantTradingStudio\data`（macOS `~/Library/Application Support/...`，
  Linux `~/.local/share/...`）—— 不会写进程序目录，避免 Program Files 只读导致的失败。
* 覆盖方式：环境变量 `QUANTSTUDIO_DATA_DIR=D:\qs-data`。
* 常用开关（见 `backend/quantstudio/config.py`）：`QUANTSTUDIO_DATA_SOURCE=auto|sina|eastmoney|csv|sample`、
  `QUANTSTUDIO_OFFLINE=1`（只用本地缓存/CSV，不发网络请求）。

行情来自公开接口（新浪快照 / 东方财富 K 线 / 腾讯备源），失败会按
「真实源 → 磁盘缓存 → 本地 CSV → 示例数据」逐级降级，顶栏徽标会显示当前来源，**不会把演示数据伪装成实时行情**。

---

## 4. 快捷键

| 快捷键 | 作用 |
|---|---|
| `Ctrl+1` … `Ctrl+5` | 依次切换到 行情看板 / 策略管理 / 回测分析 / 持仓管理 / 交易（模拟盘） |
| `Ctrl+R` 或 `F5` | 刷新当前页（重新拉数据） |
| `Ctrl+Q` | 退出（会等待后台任务收尾） |
| `Esc` | 关闭弹窗（自检 / 日志 / 策略详情等只读对话框） |

菜单栏还有：文件（刷新、打开数据目录/文档目录、退出）、视图（五个页面）、
工具（数据源自检、清空回测缓存、重置模拟盘账户）、帮助（关于、如何编写策略）。

---

## 5. 打包成 exe

```powershell
# Windows：一键（装依赖 → 测试 → 策略校验 → 源码自检 → 打包 onedir/onefile → 产物自检）
powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1 -OneFile
```

产物在 `desktop/build/output/dist/QuantTradingStudio/`（onedir，推荐）与
`desktop/build/output/dist-onefile/QuantTradingStudio.exe`（单文件）。
依赖、体积、图标、版本资源、杀软误报等细节见 **[`build/README.md`](build/README.md)**。

CI：`.github/workflows/build-desktop.yml` 在 windows-latest 上跑核心测试 → 打包 →
`--selftest` / `--selftest-gui` 产物自检 → 上传 zip/exe；打 `v*` tag 时自动发布 Release。

---

## 6. 测试

```bash
python -m unittest discover -s backend/tests -v     # 核心（与 Web 版共用，需要 flask）
python -m unittest discover -s desktop/tests -v     # 桌面（需要 tkinter）
python scripts/check_strategies.py                  # 策略规范校验（内置 + local/）
python -m compileall -q backend desktop scripts     # 语法编译检查
```

---

## 7. 与 Web 版的关系

| | Web 版 | 桌面版（本包） |
|---|---|---|
| 界面 | `frontend/` 原生 HTML/CSS/JS（无构建步骤） | Tkinter/ttk 原生窗口 |
| 后端 | Flask 蓝图 `backend/quantstudio/api/` | 直接调用 `backend/quantstudio/services/`（同一套业务服务层） |
| 启动 | `./start.sh` / `start.bat`，浏览器打开 127.0.0.1:8000 | `python -m quantstudio_desktop` 或双击 exe |
| 依赖 | Flask | 无第三方依赖（标准库 + tkinter） |
| 数据 | 同一份核心与数据目录，功能口径一致（行情/策略/回测/持仓/模拟盘） |

两边**共用同一个核心包**：改一处业务逻辑，两边同时生效；差别只在界面与进程形态。
需要多标签浏览器体验、给同事发链接 → 用 Web 版；想要一个不占端口的原生窗口程序 → 用桌面版。

> 免责声明同根 README：默认只做研究与模拟交易（`trade.mode=paper`），不会向任何券商发送真实委托；
> 行情接口非官方开放 API，仅供个人研究学习，商业用途请购买正规数据服务。
