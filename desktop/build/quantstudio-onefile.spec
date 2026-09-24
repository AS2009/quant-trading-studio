# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— **onefile**（单文件版，绿色免安装）。

构建（建议在仓库根目录执行）::

    python -m PyInstaller desktop/build/quantstudio-onefile.spec --noconfirm \
        --distpath desktop/build/output/dist-onefile --workpath desktop/build/output/work-onefile

产物::

    Windows : desktop/build/output/dist-onefile/QuantTradingStudio.exe
    Linux   : desktop/build/output/dist-onefile/QuantTradingStudio
    macOS   : dist-onefile/QuantTradingStudio.app/Contents/MacOS/QuantTradingStudio

与 ``quantstudio.spec`` 的差别只有「打包形态」：onedir 把依赖平铺在目录里（启动快），
onefile 把全部内容塞进单个可执行文件、运行时解包到 ``%TEMP%\\_MEIxxxx``（首次启动更慢、
体积与内存略高，反病毒软件更容易误报）。两份 spec 的 hiddenimports / excludes / 资源降级策略一致，
详细注释见 ``quantstudio.spec``；**改动时请同时改两个文件**。
"""

import os
import sys

# --------------------------------------------------------------------------- 路径
_SPEC_DIR = os.path.abspath(globals().get("SPECPATH") or globals().get("__file__") or os.getcwd())
_REPO_ROOT = os.path.abspath(os.path.join(_SPEC_DIR, os.pardir, os.pardir))   # desktop/build -> 仓库根
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
_DESKTOP_DIR = os.path.join(_REPO_ROOT, "desktop")
_APP_MAIN = os.path.join(_DESKTOP_DIR, "quantstudio_desktop", "__main__.py")
_ICON_FILE = os.path.join(_SPEC_DIR, "icon.ico")
_VERSION_FILE = os.path.join(_SPEC_DIR, "version_info.txt")
_LOCAL_STRATEGY_DIR = os.path.join(_BACKEND_DIR, "quantstudio", "strategies", "local")

if not os.path.exists(_APP_MAIN):
    raise SystemExit(
        "[quantstudio-onefile.spec] 找不到 %s\n"
        "  请在仓库根目录执行：python -m PyInstaller desktop/build/quantstudio-onefile.spec ..." % _APP_MAIN)

# --------------------------------------------------------------------------- 隐式导入
# 核心包（动态导入 / 工厂构造，静态分析看不到）
HIDDEN_IMPORTS = """
quantstudio
quantstudio.data
quantstudio.data.tencent
quantstudio.data.sina
quantstudio.data.eastmoney
quantstudio.data.csv_provider
quantstudio.data.sample
quantstudio.backtest
quantstudio.strategies
quantstudio.strategies.local
quantstudio.trading
quantstudio.portfolio
quantstudio.services
""".split()

# 桌面包（views/* 由 VIEW_SPECS + importlib 载入；widgets/* 由 __getattr__ 懒加载）
HIDDEN_IMPORTS += """
quantstudio_desktop
quantstudio_desktop.widgets.charts
quantstudio_desktop.widgets.table
quantstudio_desktop.widgets.cards
quantstudio_desktop.widgets.forms
quantstudio_desktop.widgets.toast
quantstudio_desktop.views.market
quantstudio_desktop.views.strategies
quantstudio_desktop.views.backtest
quantstudio_desktop.views.portfolio
quantstudio_desktop.views.trade
quantstudio_desktop.views.level2
""".split()

# GUI 用到的标准库模块
HIDDEN_IMPORTS += """
tkinter
tkinter.ttk
tkinter.font
tkinter.messagebox
tkinter.filedialog
sqlite3
zlib
json
csv
ssl
http.client
urllib.request
quantstudio_desktop.app
quantstudio_desktop.services
quantstudio_desktop.selftest
""".split()

# MCP 服务器（桌面版 --mcp）：tools_*.py 由 importlib 动态导入、resources/prompts 惰性导入
HIDDEN_IMPORTS += """
quantstudio.mcp
quantstudio.mcp.cli
quantstudio.mcp.protocol
quantstudio.mcp.registry
quantstudio.mcp.context
quantstudio.mcp.safety
quantstudio.mcp.server
quantstudio.mcp.tools
quantstudio.mcp.tools_market
quantstudio.mcp.tools_strategy
quantstudio.mcp.tools_backtest
quantstudio.mcp.tools_portfolio
quantstudio.mcp.tools_level2
quantstudio.data.level2
quantstudio.data.level2_import
quantstudio.data.ths
quantstudio.services.level2_service
quantstudio.mcp.resources
quantstudio.mcp.prompts
""".split()

# 本地策略 drop-in 目录：模块名运行期才拼出来，按目录扫描补齐
if os.path.isdir(_LOCAL_STRATEGY_DIR):
    for _filename in sorted(os.listdir(_LOCAL_STRATEGY_DIR)):
        if _filename.endswith(".py") and _filename != "__init__.py":
            HIDDEN_IMPORTS.append("quantstudio.strategies.local.%s" % _filename[:-3])

# --------------------------------------------------------------------------- 排除
# Web 框架与科学计算栈：桌面版（Tkinter GUI）完全不需要，排除可显著减小体积
EXCLUDES = [
    "flask",
    "werkzeug",
    "jinja2",
    "pytest",
    "numpy",
    "pandas",
    "matplotlib",
]

# --------------------------------------------------------------------------- 数据文件
# 本地策略「源码即代码」：源码必须落在磁盘上，registry 才能扫描到并 import
DATAS = []
if os.path.isdir(_LOCAL_STRATEGY_DIR):
    for _filename in sorted(os.listdir(_LOCAL_STRATEGY_DIR)):
        if _filename.endswith(".py"):
            DATAS.append((os.path.join(_LOCAL_STRATEGY_DIR, _filename),
                          os.path.join("quantstudio", "strategies", "local")))

# MCP 文档资源：打包后 ``ctx.repo_root`` 指向包根，按同样相对层级铺开 docs/ 与 README
for _rel_dir in ("docs",):
    _src_dir = os.path.join(_REPO_ROOT, _rel_dir)
    if os.path.isdir(_src_dir):
        DATAS.append((_src_dir, _rel_dir))
if os.path.isfile(os.path.join(_REPO_ROOT, "README.md")):
    DATAS.append((os.path.join(_REPO_ROOT, "README.md"), "."))
if os.path.isfile(os.path.join(_BACKEND_DIR, "data", "README.md")):
    DATAS.append((os.path.join(_BACKEND_DIR, "data", "README.md"),
                  os.path.join("backend", "data")))

# --------------------------------------------------------------------------- 可选资源（缺失即降级，不让 CI 失败）
_EXE_KWARGS = {}
if os.path.exists(_ICON_FILE):
    _EXE_KWARGS["icon"] = _ICON_FILE
else:
    print("[quantstudio-onefile.spec] 未找到 %s，使用默认图标" % _ICON_FILE)

if sys.platform == "win32" and os.path.exists(_VERSION_FILE):
    _EXE_KWARGS["version"] = _VERSION_FILE
else:
    print("[quantstudio-onefile.spec] 跳过 Windows 版本资源（platform=%s）" % sys.platform)

# --------------------------------------------------------------------------- 运行时钩子
# 入口 quantstudio_desktop/__main__.py 是包内相对导入（from .app import main），
# PyInstaller 把入口脚本当顶层 __main__ 执行（__package__ 为空）会抛
#     ImportError: attempted relative import with no known parent package
# 构建期生成一个 runtime hook 把 __main__ 标记成包的子模块（理由详见 quantstudio.spec）。
_RUNTIME_HOOK_LINES = [
    "# -*- coding: utf-8 -*-",
    "# 由 desktop/build/quantstudio-onefile.spec 构建期生成：修正入口脚本的包上下文。",
    "",
    "import sys",
    "",
    "_main = sys.modules.get('__main__')",
    "if _main is not None and not getattr(_main, '__package__', None):",
    "    _main.__package__ = 'quantstudio_desktop'",
    "",
]


def _write_runtime_hook():
    # 写到 workpath（构建目录，不进仓库），返回运行时钩子的路径
    hook_dir = os.path.join(globals().get("workpath") or _SPEC_DIR, "runtime_hooks")
    os.makedirs(hook_dir, exist_ok=True)
    hook_path = os.path.join(hook_dir, "pyi_rth_quantstudio_desktop.py")
    with open(hook_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(_RUNTIME_HOOK_LINES))
    return hook_path


RUNTIME_HOOKS = [_write_runtime_hook()]



# --------------------------------------------------------------------------- 构建
a = Analysis(
    [_APP_MAIN],
    pathex=[_BACKEND_DIR, _DESKTOP_DIR],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,                       # onefile：依赖与数据都进可执行文件
    a.datas,
    [],
    name="QuantTradingStudio",        # Windows 产物：QuantTradingStudio.exe
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,              # None = 运行时解包到系统临时目录（%TEMP%\_MEIxxxx）
    console=False,                    # GUI 程序：不弹控制台窗口
    disable_windowed_traceback=True,  # CI 里不能弹模态错误框，异常走退出码
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    **_EXE_KWARGS
)
