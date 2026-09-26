# 桌面版打包说明（PyInstaller）

把 `desktop/quantstudio_desktop`（Tkinter GUI）+ `backend/quantstudio`（核心）打成
**Windows 原生可执行程序**。运行时**零第三方依赖**（只用标准库 + tkinter），发布物里不含
Flask / pandas / numpy / matplotlib（spec 用 `excludes` 明确排除）。

| 文件 | 作用 |
|---|---|
| `quantstudio.spec` | onedir（目录版，**推荐**：启动快、可增量替换、易排错） |
| `quantstudio-onefile.spec` | onefile（单文件 exe，绿色免安装，首次启动慢） |
| `version_info.txt` | Windows 文件属性里的版本资源（公司/产品/1.0.0.0/版权/描述） |
| `build_windows.ps1` | Windows 本地 / CI 一键打包（含产物自检） |
| `build_unix.sh` | macOS/Linux 开发者在非 Windows 上验证打包 |
| `icon.ico` | **需要自备**（可选）：放进来即自动生效；缺失时自动降级为 PyInstaller 默认图标 |

---

## 1. 依赖

```powershell
# Windows（打包机 / CI）
python -m pip install -r requirements-desktop.txt      # 只有 pyinstaller>=6.6
```

* **必须使用自带 tkinter 的 Python**：<https://www.python.org/downloads/windows/> 的官方安装包即可
  （Microsoft Store 版、部分精简发行版没有 Tk，打包出来会“缺少 tkinter”自检失败）。
* 运行时依赖 = 无。用户机器不需要装 Python，也不需要装任何 pip 包。

---

## 2. 打包命令

### Windows（推荐）

```powershell
# 一键：装依赖 → 核心测试 → 策略校验 → 源码自检 → 打包 → 产物自检
powershell -ExecutionPolicy Bypass -File desktop\build\build_windows.ps1

# 常用参数
pwsh -File desktop\build\build_windows.ps1 -OneFile -Clean   # 额外产出单文件版，先清理 output
pwsh -File desktop\build\build_windows.ps1 -SkipTests        # 只打包（跳过测试）
pwsh -File desktop\build\build_windows.ps1 -SkipDeps         # 不重装依赖
```

### 手动（等价于 CI 里的命令）

```powershell
python -m PyInstaller desktop/build/quantstudio.spec --noconfirm `
    --distpath desktop/build/output/dist --workpath desktop/build/output/work

python -m PyInstaller desktop/build/quantstudio-onefile.spec --noconfirm `
    --distpath desktop/build/output/dist-onefile --workpath desktop/build/output/work-onefile
```

> onedir 与 onefile 用**不同的 distpath/workpath**：同名 EXE 共用工作目录会命中 PyInstaller 缓存，
> 可能导致产物形态串味。

### macOS / Linux（仅用于验证 spec 能否跑通）

```bash
PYTHON=/usr/bin/python3 ./desktop/build/build_unix.sh            # 系统 python 自带 tkinter
./desktop/build/build_unix.sh --onefile --skip-tests --skip-deps
```

### macOS（正式产物：`.app` + zip + dmg）

```bash
# 推荐：用自带 Tcl/Tk 9 的解释器（Homebrew），Tk 会被打进 .app，渲染正常
brew install python-tk@3.12
PYTHON="$(brew --prefix python@3.12)/bin/python3.12" ./desktop/build/build_macos.sh

# 或者先建 venv 装好 PyInstaller，再用 PYTHON=... 指过去
python3.12 -m venv .venv-macos && .venv-macos/bin/pip install -r requirements-desktop.txt
PYTHON=.venv-macos/bin/python bash desktop/build/build_macos.sh --clean
```

要点（都是 macOS 特有，Windows 构建不涉及）：

| 事项 | 说明 |
|---|---|
| 解释器 | **必须自带 tkinter，且优先选 Tcl/Tk ≥ 8.6 的解释器**：Homebrew 的 `python-tk@3.12`（Tcl/Tk 9，会被打进 .app）或 python.org 安装包。`/usr/bin/python3` 虽然也有 tkinter，但那是 **Apple 随系统的 Tk 8.5.9（2009 年）**，在 macOS 10.14+ 上会把窗口画成全白（v1.4.0 就是栽在这里），脚本检测到 8.5 会打印警告。PyInstaller 也要装在这个解释器里（PEP 668 下用 venv） |
| 图标 | `icon.icns` 由 `desktop/build/make_icns.py` 生成（复用 `make_icon.py` 的纯标准库渲染 + 系统 `iconutil` 打包，1024px 也原生渲染，不放大） |
| 架构 | 默认 `auto`：按解释器判定（Homebrew 是单架构 → 产 `arm64` 包；要 universal2 得用 universal2 解释器 + universal2 Tcl/Tk）。PyInstaller 遇到单架构的 `_tkinter.so` 会直接报 `not a fat binary`，所以 auto 比硬写 universal2 可靠 |
| Tcl/Tk | 用 Homebrew/python.org 的解释器时，PyInstaller 会把 `libtcl9.0.dylib` / `libtcl9tk9.0.dylib`（旧版是 `Tcl.framework`/`Tk.framework`）和 `_tcl_data`/`_tk_data` **打进 .app** → 自包含、不依赖系统 Tk，代价是体积 +10 MB。用系统 `/usr/bin/python3` 时相反：什么都不打进去，运行时靠 Apple 的 Tk 8.5（体积小但会白屏） |
| 签名 | 构建脚本做 **ad-hoc 签名**（`codesign --force --deep --sign -`，arm64 不加签名无法启动），可用 `codesign --verify --deep --strict` 复验；**未做开发者签名与公证**，别人首次打开要右键「打开」或 `xattr -dr com.apple.quarantine` 去掉隔离标记 |
| Bundle | `Info.plist` 的 `CFBundleShortVersionString/CFBundleVersion` 从 `desktop/quantstudio_desktop/__init__.py` 的 `__version__` 读，**不重复写版本号**；`LSMinimumSystemVersion=11.0`、`NSHighResolutionCapable=true`（Retina） |
| 命令行 | 参数照常从 `.app` 内可执行文件传入：`QuantTradingStudio.app/Contents/MacOS/QuantTradingStudio --selftest` / `--mcp --selftest` / `--selftest-gui`（`argv_emulation=False` 保证不被劫持） |

产物在 `desktop/build/output-macos/`（下面是以 Homebrew Tk 9 / arm64 实测）：

```
dist/QuantTradingStudio.app                 # 应用本体（约 30 MB，含自带 Tcl/Tk）
pkg/QuantTradingStudio-macos-arm64.zip      # 约 12 MB（ditto 打包，保留符号链接）
pkg/QuantTradingStudio-macos-arm64.dmg      # 约 14 MB（含「应用程序」快捷方式，拖拽安装）
```

`build_unix.sh` 仍然可用，但它只是「验证 spec 能跑通」——不生成 `.app`/`.icns`、不签名、不打 dmg。

---

## 3. 产物

| 形态 | Windows 路径 | 说明 |
|---|---|---|
| onedir | `desktop/build/output/dist/QuantTradingStudio/QuantTradingStudio.exe` | 整个目录一起分发，双击 exe 运行 |
| onefile | `desktop/build/output/dist-onefile/QuantTradingStudio.exe` | 单个 exe，运行时自解压到 `%TEMP%\_MEIxxxx` |

体积参考（实测本机 **macOS arm64 / Python 3.9** 用于校验流程，Windows 会更大一些，
主要差在 `python3xx.dll` 与 Tcl/Tk 库）：

* onedir：**11 MB**（目录）/ 主程序 1.8 MB；纯 Python 归档 1.6 MB
* onefile：**4.4 MB**（单文件）
* Windows 预估：onedir 约 25–35 MB（打包 zip 约 10–15 MB），onefile 约 15–20 MB

`desktop/build/output/` 已在 `.gitignore` 中忽略；CI 只上传 `artifacts/` 下的 zip / exe / 自检报告。

---

## 4. spec 关键点（改打包配置前请读）

* **路径全部从 spec 自身位置推导**（`SPECPATH` → 仓库根），不硬编码绝对路径，本机与 CI 通用；
* `Analysis([desktop/quantstudio_desktop/__main__.py])`，`pathex=[<repo>/backend, <repo>/desktop]`；
* `hiddenimports`：核心包按名字动态导入（数据源工厂、策略注册表、本地策略 drop-in），
  桌面包的 `views/*`（`VIEW_SPECS` + `importlib`）与 `widgets/*`（`__getattr__` 懒加载）静态分析都看不到；
  另外入口脚本自己按「顶层脚本」分析，`from .app import main` 也解析不到，所以
  `quantstudio_desktop.app / .services / .selftest` 必须显式列出；
* **运行时钩子**：PyInstaller 把 `__main__.py` 当顶层脚本执行（`__package__` 为空），
  直接跑会 `ImportError: attempted relative import with no known parent package`。
  spec 在构建期生成一个 runtime hook 把 `__main__.__package__` 补成 `quantstudio_desktop`；
* `datas`：把 `backend/quantstudio/strategies/local/*.py` 以**源码**形式打进
  `quantstudio/strategies/local/`。核心的策略发现逻辑是「扫描目录再 import」，只压进 PYZ 是扫不到的；
* `excludes=["flask","werkzeug","jinja2","pytest","numpy","pandas","matplotlib"]`；
* `console=False`、`disable_windowed_traceback=True`（CI 里不能弹模态错误框，异常走退出码）；
  `icon` 与 `version` 都做了存在性判断 —— **没有 `icon.ico` 也能打包成功**。

---

## 5. 打包后自检（验收标准）

```powershell
$exe = "desktop\build\output\dist\QuantTradingStudio\QuantTradingStudio.exe"
Start-Process -FilePath $exe -ArgumentList "--selftest"     -Wait -PassThru   # 退出码必须是 0
Start-Process -FilePath $exe -ArgumentList "--selftest-gui" -Wait -PassThru   # 退出码必须是 0
Get-Content "$env:TEMP\quantstudio_selftest.txt"                              # 自检报告
```

* 因为是无控制台的 GUI 程序，`--selftest` 的 stdout 可能为空，所以自检模块会**同时**把报告写到
  `%TEMP%\quantstudio_selftest.txt`（macOS/Linux 是 `$TMPDIR/quantstudio_selftest.txt`）；
* **PowerShell 的 `& exe` 不会等待 GUI 程序**（`$LASTEXITCODE` 也不可靠），必须用
  `Start-Process -Wait -PassThru` 再读 `ExitCode`，`build_windows.ps1` 与 CI 都是这么做的；
* 自检 3 段：数据源（真实行情，失败会降级为缓存/CSV/示例并只记警告）→ 策略库 → 回测 → 界面依赖。

---

## 6. 常见问题

**Q1. 杀毒软件/Windows Defender 报毒、SmartScreen 提示“未知发布者”**
PyInstaller 的引导器（bootloader）被大量恶意样本共用，误报很常见，**onefile 比 onedir 更容易被误报**
（会自解压到 `%TEMP%`）。对策：

1. 优先分发 **onedir** 版本（本仓库默认）；
2. 让用户把安装目录加入白名单，或右键 → 属性 → 解除锁定；
3. 正式分发请购买 **代码签名证书**（EV 证书可立即通过 SmartScreen），签名命令：
   `signtool sign /fd sha256 /a /tr <时间戳服务> /td sha256 QuantTradingStudio.exe`；
4. 不要上传样本到 VirusTotal 公共库（会被其他厂商当作特征误报传染）。

**Q2. 打包/自检报“缺少 tkinter”或 `ModuleNotFoundError: No module named '_tkinter'`**
用的是没有 Tk 的 Python。Windows 请装 <https://www.python.org/downloads/windows/> 官方包
（安装时勾选 `tcl/tk and IDLE`）；Linux 需要 `sudo apt install python3-tk`；macOS 用
python.org 安装包或系统 `/usr/bin/python3`（Homebrew 的 python 默认无 Tk）。

**Q3. 打包后的程序把数据写到哪里？**
用户数据目录（不在程序目录，避免 Program Files 只读）：

| 平台 | 默认位置 |
|---|---|
| Windows | `%LOCALAPPDATA%\QuantTradingStudio\data` |
| macOS | `~/Library/Application Support/QuantTradingStudio/data` |
| Linux | `$XDG_DATA_HOME/QuantTradingStudio/data`（默认 `~/.local/share/...`） |

可用环境变量 `QUANTSTUDIO_DATA_DIR` 覆盖；缓存/CSV/持仓/模拟盘账本都在里面。
源码运行则用仓库的 `backend/data/`。

**Q4. 首次启动很慢 / 体积大**
onefile 每次启动都要把内容解压到 `%TEMP%\_MEIxxxx`（首次最慢，杀软实时扫描会再拖几秒）；
onedir 无此开销，直接加载同目录 DLL。追求启动速度就发布 onedir。

**Q5. 怎么加自定义策略？**
把符合 `docs/strategy-spec.md` 的 `<slug>.py` 放进 `backend/quantstudio/strategies/local/`
（文件名 = 策略 id 去掉 `st_` 前缀；`_` 开头的文件不加载），然后：
`python scripts/check_strategies.py` 校验 → **重新打包**（spec 会自动扫描该目录并加进
`hiddenimports` + `datas`，无需改 spec）。运行期可在「策略管理」页看到 `origin=local` 的策略。

**Q6. 怎么换图标？**
把多尺寸的 `icon.ico`（建议含 16/32/48/256）放到 `desktop/build/icon.ico` 即可，spec 会自动使用；
文件名不同则改 spec 里的 `_ICON_FILE`。缺失时打包不会失败，只是用 PyInstaller 默认图标。

**Q7. 版本号在哪里改？**
`desktop/quantstudio_desktop/__init__.py` 的 `__version__`（界面「关于」显示）；
Windows 文件属性用 `desktop/build/version_info.txt` 的 `filevers/prodvers/(1, 4, 2, 0)` 与
`FileVersion/ProductVersion`（发布前手动同步，PyInstaller 不做跨文件校验）。

**Q8. `--selftest` 退出码 1 怎么办？**
读 `%TEMP%\quantstudio_selftest.txt`，最后一段会列出失败项，常见原因：
缺少 tkinter（Q2）、网络全挂且本地没有缓存/CSV（此时才会失败，正常降级只是警告）、
本地策略语法错误（会显示文件名与异常）。

**Q9. 构建缓存把改动吃掉了 / 想干净重来**
删掉 `desktop/build/output/`（脚本加 `-Clean` / `--clean`），必要时加 `--clean` 让 PyInstaller
清空自身缓存（`~/AppData/Local/pyinstaller`）。
