# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— **onedir**（目录版，推荐：启动快、便于排查）。

构建（建议在仓库根目录执行）::

    python -m PyInstaller desktop/build/quantstudio.spec --noconfirm \
        --distpath desktop/build/output/dist --workpath desktop/build/output/work

产物::

    Windows : desktop/build/output/dist/QuantTradingStudio/QuantTradingStudio.exe
    Linux   : desktop/build/output/dist/QuantTradingStudio/QuantTradingStudio
    macOS   : desktop/build/output/dist/QuantTradingStudio.app（BUNDLE 包：Info.plist + icon.icns，
              默认按 universal2 构建 → Intel 与 Apple Silicon 通用；旁边仍有同名的 COLLECT 目录）

要点
----
* 全部路径从本 spec 所在目录推导（PyInstaller 注入的 ``SPECPATH``），**不硬编码绝对路径**，
  本机与 CI（windows-latest）都能直接跑；
* 入口是 ``desktop/quantstudio_desktop/__main__.py``；``pathex`` 同时给出 ``backend/`` 与 ``desktop/``，
  这样 PyInstaller 的静态分析能解析 ``import quantstudio`` 与包内的相对导入；
* 核心包大量使用「按名字动态导入」（数据源工厂 / 策略注册表 / 页面注册表 / 本地策略 drop-in），
  静态分析看不到，必须列进 ``hiddenimports``（见 ``HIDDEN_IMPORTS``）；
* 入口脚本里的相对导入需要 ``RUNTIME_HOOKS`` 兜底（见下）；
* ``EXCLUDES`` 显式排除 Web 框架与科学计算栈：桌面版是 Tkinter 原生 GUI，
  不需要 Flask/pandas/numpy/matplotlib，排除后体积明显更小；
* ``console=False``（GUI 程序，无控制台窗口）；``--selftest`` 的报告会写到
  ``%TEMP%/quantstudio_selftest.txt``（见 ``desktop/quantstudio_desktop/selftest.py``），CI 靠它取输出；
* ``icon`` 与 Windows 版本资源都做了**降级**：图标文件缺失时不传 ``icon=``（CI 不会因为没有图标失败），
  版本资源只在 ``sys.platform == "win32"`` 时传（非 Windows 上 PyInstaller 不支持该参数）。
* **macOS**：``COLLECT`` 之后再用 ``BUNDLE`` 包成 ``.app``——``Info.plist`` 里的版本号直接
  从 ``desktop/quantstudio_desktop/__init__.py`` 的 ``__version__`` 读，**不在这里重复写**；
  图标用 ``icon.icns``（由 ``desktop/build/make_icns.py`` 生成）；架构由环境变量
  ``QUANTSTUDIO_MACOS_ARCH``（默认 ``universal2``）控制，签名留给 ``build_macos.sh`` 做 ad-hoc。
"""

import os
import re
import sys

# --------------------------------------------------------------------------- 路径
# PyInstaller 执行 spec 时会注入 SPECPATH（本文件所在目录）；__file__ 仅作兜底。
_SPEC_DIR = os.path.abspath(globals().get("SPECPATH") or globals().get("__file__") or os.getcwd())
_REPO_ROOT = os.path.abspath(os.path.join(_SPEC_DIR, os.pardir, os.pardir))   # desktop/build -> 仓库根
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
_DESKTOP_DIR = os.path.join(_REPO_ROOT, "desktop")
_APP_MAIN = os.path.join(_DESKTOP_DIR, "quantstudio_desktop", "__main__.py")
_ICON_ICO = os.path.join(_SPEC_DIR, "icon.ico")
_ICON_ICNS = os.path.join(_SPEC_DIR, "icon.icns")
_VERSION_FILE = os.path.join(_SPEC_DIR, "version_info.txt")
_LOCAL_STRATEGY_DIR = os.path.join(_BACKEND_DIR, "quantstudio", "strategies", "local")

for _path in (_APP_MAIN, _BACKEND_DIR, _DESKTOP_DIR):
    if not os.path.exists(_path):
        raise SystemExit(
            "[quantstudio.spec] 找不到 %s\n"
            "  请在仓库根目录执行：python -m PyInstaller desktop/build/quantstudio.spec ..." % _path)



# --------------------------------------------------------------------------- 平台开关（macOS）
# macOS 产物是 .app；其余平台保持原样（Windows 的 .exe 与 Linux 的裸可执行文件）。
_IS_MAC = sys.platform == "darwin"

#: macOS 架构：**默认不指定**（PyInstaller 用解释器自己的架构）——
#: Homebrew 的 python-tk 是单架构，硬要 universal2 会直接报「not a fat binary」；
#: 想打通用二进制就设 QUANTSTUDIO_MACOS_ARCH=universal2（要求解释器与 Tcl/Tk 都是 fat binary）。
_MAC_ARCH = os.environ.get("QUANTSTUDIO_MACOS_ARCH", "").strip() or None

#: .app 的 Info.plist 里要写的版本号与反向域名标识
_BUNDLE_ID = "com.quantstudio.desktop"


def _read_app_version():
    """从 ``desktop/quantstudio_desktop/__init__.py`` 读 ``__version__``（单一事实来源）。"""
    init_file = os.path.join(_DESKTOP_DIR, "quantstudio_desktop", "__init__.py")
    try:
        with open(init_file, encoding="utf-8") as handle:
            match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', handle.read(), re.M)
    except OSError:
        return "0.0.0"
    if not match:
        print("[quantstudio.spec] 未在 %s 里找到 __version__，Info.plist 用 0.0.0" % init_file)
        return "0.0.0"
    return match.group(1)


_APP_VERSION = _read_app_version()

# --------------------------------------------------------------------------- 隐式导入
# 核心包（quantstudio）：按名字动态导入 / 工厂构造，静态分析看不到
_CORE_HIDDEN = [
    "quantstudio",
    "quantstudio.data",
    "quantstudio.data.tencent",
    "quantstudio.data.sina",
    "quantstudio.data.eastmoney",
    "quantstudio.data.csv_provider",
    "quantstudio.data.sample",
    "quantstudio.backtest",
    "quantstudio.strategies",
    "quantstudio.strategies.local",
    "quantstudio.trading",
    "quantstudio.portfolio",
    "quantstudio.services",
]

# 桌面包：views/* 由 views/__init__.py 的 VIEW_SPECS + importlib 动态载入，
# widgets/* 由 widgets/__init__.py 的 __getattr__ 懒加载 —— 两者静态分析都看不到
_DESKTOP_HIDDEN = [
    "quantstudio_desktop",
    "quantstudio_desktop.widgets.charts",
    "quantstudio_desktop.widgets.table",
    "quantstudio_desktop.widgets.cards",
    "quantstudio_desktop.widgets.forms",
    "quantstudio_desktop.widgets.toast",
    # 入口脚本按「顶层脚本」分析，它自己的相对导入解析不到，必须显式列出：
    #   __main__.py -> from .app import main；app.main() 里再 from .selftest import ...
    "quantstudio_desktop.app",
    "quantstudio_desktop.services",
    "quantstudio_desktop.selftest",
    "quantstudio_desktop.views.market",
    "quantstudio_desktop.views.strategies",
    "quantstudio_desktop.views.backtest",
    "quantstudio_desktop.views.portfolio",
    "quantstudio_desktop.views.trade",
    "quantstudio_desktop.views.level2",
    # MCP 服务器（桌面版 `--mcp`）：tools_*.py 由 tools.py 的 importlib 动态导入，
    # resources/prompts 在 cli.build_server 里惰性导入 —— 静态分析都看不到，必须显式列。
    "quantstudio.mcp",
    "quantstudio.mcp.cli",
    "quantstudio.mcp.protocol",
    "quantstudio.mcp.registry",
    "quantstudio.mcp.context",
    "quantstudio.mcp.safety",
    "quantstudio.mcp.server",
    "quantstudio.mcp.tools",
    "quantstudio.mcp.tools_market",
    "quantstudio.mcp.tools_strategy",
    "quantstudio.mcp.tools_backtest",
    "quantstudio.mcp.tools_portfolio",
    "quantstudio.mcp.tools_level2",
    # 盘口 / L2 的数据源与服务层（静态导入本可被分析到，显式列出以防重构后漏掉）
    "quantstudio.data.level2",
    "quantstudio.data.level2_import",
    "quantstudio.data.ths",
    "quantstudio.services.level2_service",
    "quantstudio.mcp.resources",
    "quantstudio.mcp.prompts",
]

# GUI 用到的标准库模块（PyInstaller 钩子通常能覆盖，显式列出以保证跨版本稳定）
_STDLIB_HIDDEN = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "sqlite3",        # 核心不用，但常被间接引用，留着以免第三方代码缺模块
    "zlib",
    "json",
    "csv",
    "ssl",
    "http.client",
    "urllib.request",
]


def _local_strategy_hidden():
    """``quantstudio/strategies/local/*.py`` 是 drop-in 目录，模块名运行期才拼出来。

    这里按目录扫描补进 hiddenimports，并把源码作为 data 一起打包：
    ``registry.LOCAL_DIR`` 指向包目录下的 ``local/``，只有磁盘上真的有 .py 文件，
    ``discover_local()`` 才能发现它们（PyInstaller 收进 PYZ 的模块不会出现在目录里）。
    """
    modules = []
    if not os.path.isdir(_LOCAL_STRATEGY_DIR):
        return modules
    for filename in sorted(os.listdir(_LOCAL_STRATEGY_DIR)):
        if filename.endswith(".py") and filename != "__init__.py":
            modules.append("quantstudio.strategies.local.%s" % filename[:-3])
    return modules


HIDDEN_IMPORTS = _CORE_HIDDEN + _DESKTOP_HIDDEN + _STDLIB_HIDDEN + _local_strategy_hidden()

# --------------------------------------------------------------------------- 排除
# 桌面版不需要 Web 框架（核心 api/ 层是给 Flask 前端用的，桌面版直接调 services/），
# 也不需要科学计算栈（回测/指标全部标准库实现，pandas/numpy 只是可选加速）。
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
# 本地策略「源码即代码」：源码进包，registry 才能扫描到并 import
DATAS = []
if os.path.isdir(_LOCAL_STRATEGY_DIR):
    for filename in sorted(os.listdir(_LOCAL_STRATEGY_DIR)):
        if filename.endswith(".py"):
            DATAS.append((os.path.join(_LOCAL_STRATEGY_DIR, filename),
                          os.path.join("quantstudio", "strategies", "local")))

# MCP 的文档资源（resources）与提示词模板要能读到项目文档：
# 打包后 ``ctx.repo_root`` 指向包根（onedir 是 ``_internal``），所以按同样的相对层级铺开放。
# 缺了它们，``--mcp --selftest`` 会因为「资源数 0」失败（CI 就是这么发现的）。
for _rel_dir in ("docs",):
    _src_dir = os.path.join(_REPO_ROOT, _rel_dir)
    if os.path.isdir(_src_dir):
        DATAS.append((_src_dir, _rel_dir))
if os.path.isfile(os.path.join(_REPO_ROOT, "README.md")):
    DATAS.append((os.path.join(_REPO_ROOT, "README.md"), "."))
if os.path.isfile(os.path.join(_BACKEND_DIR, "data", "README.md")):
    DATAS.append((os.path.join(_BACKEND_DIR, "data", "README.md"),
                  os.path.join("backend", "data")))

# --------------------------------------------------------------------------- 可选资源
_EXE_KWARGS = {}
_ICON_FILE = _ICON_ICNS if _IS_MAC else _ICON_ICO
if os.path.exists(_ICON_FILE):
    _EXE_KWARGS["icon"] = _ICON_FILE          # 缺失时降级为 PyInstaller 默认图标，不让 CI 失败
else:
    print("[quantstudio.spec] 未找到 %s，使用默认图标" % _ICON_FILE)

if sys.platform == "win32" and os.path.exists(_VERSION_FILE):
    _EXE_KWARGS["version"] = _VERSION_FILE    # 版本资源仅 Windows 有效
else:
    print("[quantstudio.spec] 跳过 Windows 版本资源（platform=%s）" % sys.platform)


# --------------------------------------------------------------------------- 运行时钩子
# 入口 quantstudio_desktop/__main__.py 用的是包内相对导入（from .app import main），
# 而 PyInstaller 把入口脚本当顶层 __main__ 执行（__package__ 为空），会抛
#     ImportError: attempted relative import with no known parent package
# 这里在构建期生成一个 runtime hook：PyInstaller 执行 runtime hook 时，
# sys.modules['__main__'] 已经是那个即将执行入口脚本的模块对象，补上 __package__ 即可。
_RUNTIME_HOOK_LINES = [
    "# -*- coding: utf-8 -*-",
    "# 由 desktop/build/quantstudio.spec 构建期生成：修正入口脚本的包上下文（相对导入）。",
    "",
    "import sys",
    "",
    "_main = sys.modules.get('__main__')",
    "if _main is not None and not getattr(_main, '__package__', None):",
    "    _main.__package__ = 'quantstudio_desktop'",
    "",
]


def _write_runtime_hook():
    """把上面这段钩子写到 workpath（构建目录，不进仓库），返回其路径。"""
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
    [],
    exclude_binaries=True,
    name="QuantTradingStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                    # GUI 程序：不弹控制台窗口
    disable_windowed_traceback=True,  # CI 里不能弹模态错误框（会卡住流水线），异常走退出码
    argv_emulation=False,             # macOS：不劫持命令行参数（--selftest 要能收到）
    target_arch=(_MAC_ARCH if _IS_MAC else None),   # macOS：默认用解释器架构，可用 QUANTSTUDIO_MACOS_ARCH 指定
    codesign_identity=None,
    entitlements_file=None,
    **_EXE_KWARGS
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="QuantTradingStudio",        # 产物目录：dist/QuantTradingStudio/
)

if _IS_MAC:
    # .app 包：Info.plist 的版本号与 __version__ 同源；图标用 icon.icns；签名留给构建脚本做 ad-hoc。
    # 注意：--selftest / --mcp 这些命令行参数走 Contents/MacOS/QuantTradingStudio，
    # argv_emulation=False 保证能收到。
    _BUNDLE_KWARGS = {}
    if os.path.exists(_ICON_ICNS):
        _BUNDLE_KWARGS["icon"] = _ICON_ICNS      # 缺失时不传：让 PyInstaller 用默认图标而不是报错
    else:
        print("[quantstudio.spec] 未找到 %s（可执行 python desktop/build/make_icns.py 生成），"
              "使用 PyInstaller 默认图标" % _ICON_ICNS)

    app = BUNDLE(
        coll,
        name="QuantTradingStudio.app",
        bundle_identifier=_BUNDLE_ID,
        version=_APP_VERSION,
        info_plist={
            "CFBundleName": "QuantTrading Studio",
            "CFBundleDisplayName": "QuantTrading Studio",
            "CFBundleShortVersionString": _APP_VERSION,
            "CFBundleVersion": _APP_VERSION,
            "CFBundleDevelopmentRegion": "zh_CN",
            "NSHumanReadableCopyright":
                "Copyright (C) 2025 QuantTrading Studio. All rights reserved.",
            "NSHighResolutionCapable": True,          # Retina 清晰渲染
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.finance",
        },
        **_BUNDLE_KWARGS
    )
    _DIST_DIR = globals().get("DISTPATH") or os.path.join(_REPO_ROOT, "dist")
    print("[quantstudio.spec] macOS .app 版本 %s（arch=%s）→ %s"
          % (_APP_VERSION, _MAC_ARCH, os.path.join(_DIST_DIR, "QuantTradingStudio.app")))
