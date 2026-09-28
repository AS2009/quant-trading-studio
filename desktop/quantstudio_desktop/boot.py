# -*- coding: utf-8 -*-
"""启动引导：让「源码运行」与「PyInstaller 打包后运行」都能找到核心包与可写数据目录。

目录策略（v1.5.1 起）：

- **源码运行**：数据目录 = ``<仓库>/backend/data``，用户策略 = 包内 ``backend/quantstudio/strategies/local``
  （开发期布局不变，测试与文档都按这个来）；
- **打包运行（Windows 安装版 / 免安装版）**：用户数据**就在程序所在目录**，并按类别分文件夹 ——
  ``strategies/``（策略 .py）、``data/csv/``（行情 CSV）、``data/level2/``（盘口 CSV）、
  ``data/``（自选池、模拟盘账本、缓存）。用户把策略文件复制进 ``strategies/`` 就能直接用；
  首次启动会把内置示例策略与各文件夹说明复制进去，用户不用去翻用户目录；
- **程序目录不可写时**（装到 Program Files 未放开权限、只读介质、macOS .app 包内）自动退回用户目录
  （Windows ``%LOCALAPPDATA%\\QuantTradingStudio``，macOS ``~/Library/Application Support/QuantTradingStudio``），
  启动信息里会如实标注用的是哪一种；
- 显式 ``QUANTSTUDIO_DATA_DIR`` / ``QUANTSTUDIO_STRATEGIES_DIR`` 永远优先（测试隔离、多份数据并存）。

旧版本把用户数据放在用户目录（``%LOCALAPPDATA%\\QuantTradingStudio``）；切到程序目录后，
首次启动会把旧位置的数据**复制**过来（原位置保留，不影响回退），迁移失败不影响启动。
"""

import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

#: 程序目录下的用户文件夹名（Windows 安装版按此分类）
STRATEGIES_DIRNAME = "strategies"
DATA_DIRNAME = "data"

#: 判定「旧位置确实有用户数据」的标记文件（有其一才迁移）
_LEGACY_MARKERS = ("watchlist.json", "portfolio.json", "paper_account.json",
                   "user_strategies.json", "equity_history.json", "holidays.json")

#: 首次启动写给用户看的说明（只在打包运行时创建，避免污染源码仓库）
_FOLDER_NOTES: Tuple[Tuple[str, str, str], ...] = (
    (STRATEGIES_DIRNAME, "说明-把策略文件复制到这里.txt",
     "这个文件夹放你的策略（Python 文件，*.py）。\n"
     "\n"
     "怎么用\n"
     "  1. 把写好的策略 .py 直接复制到本文件夹；\n"
     "  2. 重启程序（或在「策略」页点「刷新」）即可在策略列表里看到它；\n"
     "  3. 文件名以 _ 开头的文件不会被加载（_template.py 就是模板，复制一份改名再用）。\n"
     "\n"
     "注意\n"
     "  * 一个文件里可以定义多个策略类，每个类要有自己的 id；\n"
     "  * 如果文件有语法/参数错误，只影响它自己：程序照常启动，\n"
     "    并会在「策略」页与自检信息里告诉你哪个文件、什么原因；\n"
     "  * 本文件夹在程序目录内：卸载时默认保留，升级覆盖安装不会动你的文件。\n"),
    (os.path.join(DATA_DIRNAME, "csv"), "说明-把行情CSV复制到这里.txt",
     "这个文件夹放你自己准备的行情 CSV（离线研究 / 自有数据）。\n"
     "\n"
     "怎么用\n"
     "  1. 按 backend/data/README.md 里的列名规范准备 CSV（code,date,open,high,low,close,volume,amount…）；\n"
     "  2. 复制到本文件夹，重启程序或在「行情」页选择数据源 csv；\n"
     "  3. 程序找不到网络源时会自动用这里的文件兜底。\n"),
    (os.path.join(DATA_DIRNAME, "level2"), "说明-把盘口CSV复制到这里.txt",
     "这个文件夹放你自己导出的盘口 / 逐笔 CSV（*.orderbook.csv / *.ticks.csv）。\n"
     "\n"
     "怎么用\n"
     "  1. 文件命名用「代码 + 后缀」，例如 600519.orderbook.csv、600519.ticks.csv；\n"
     "  2. 复制到本文件夹，程序检测到有文件后会把本地导入通道接在「盘口 / L2」页；\n"
     "  3. 列名规范见 docs/level2.md。\n"),
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _candidate_core_dirs():
    """可能包含 `quantstudio` 包的目录（按优先级）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    yield here                                        # 打包后：核心包与本包同处 _MEIPASS
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            yield meipass
            yield os.path.join(meipass, "backend")
    # 源码运行：desktop/quantstudio_desktop → desktop → 仓库根 → backend
    desktop_dir = os.path.dirname(here)
    repo_root = os.path.dirname(desktop_dir)
    yield os.path.join(repo_root, "backend")
    yield repo_root


def ensure_core_path() -> Optional[str]:
    """确保 ``import quantstudio`` 可用；返回实际使用的目录。"""
    for candidate in _candidate_core_dirs():
        if not candidate or not os.path.isdir(candidate):
            continue
        if os.path.isdir(os.path.join(candidate, "quantstudio")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
    return None


def program_dir() -> str:
    """程序所在目录（打包后 = exe 所在目录；源码运行返回空串）。"""
    if not is_frozen():
        return ""
    executable = getattr(sys, "executable", "") or ""
    if not executable:
        return ""
    return os.path.dirname(os.path.abspath(executable))


def is_writable_dir(path: str) -> bool:
    """目录能否写（不存在则尝试创建）：真写一个探针文件，不看权限位（UAC/虚拟化下更可靠）。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    probe = os.path.join(path, ".quantstudio-write-probe.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return True


def user_data_dir(app_name: str = "QuantTradingStudio") -> str:
    """跨平台的用户数据目录（程序目录不可写时的回退位置，也是旧版本的位置）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, app_name, "data")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name, "data")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, app_name, "data")


def user_strategies_dir(app_name: str = "QuantTradingStudio") -> str:
    """用户数据目录同级下的策略目录（打包运行时的默认策略位置）。"""
    return os.path.join(os.path.dirname(user_data_dir(app_name)), STRATEGIES_DIRNAME)


def core_strategies_dir() -> str:
    """包内自带的策略目录（内置示例 / 模板），用于首次启动时「播种」到用户目录。"""
    core = ensure_core_path()
    if not core:
        return ""
    return os.path.join(core, "quantstudio", "strategies", "local")


# ====================================================================== 目录布局
def layout() -> Dict[str, object]:
    """解析本次运行的用户目录布局。

    返回 ``{"root", "data_dir", "strategies_dir", "mode", "writable"}``：

    * ``mode="source"``  源码运行（仓库内，开发期布局不变）；
    * ``mode="install"`` 打包运行且**程序目录可写** → 数据与策略都在程序目录（Windows 安装版 / 免安装版）；
    * ``mode="user"``    打包运行但程序目录不可写 → 退回用户目录（macOS .app、装到 Program Files 且无权限）；
    * ``mode="explicit"`` 用户显式设置了 ``QUANTSTUDIO_DATA_DIR``（测试隔离 / 多份数据并存）。
    """
    explicit_data = os.environ.get("QUANTSTUDIO_DATA_DIR", "").strip()
    explicit_strategies = os.environ.get("QUANTSTUDIO_STRATEGIES_DIR", "").strip()

    if explicit_data:
        return {"root": os.path.dirname(os.path.abspath(explicit_data)), "data_dir": explicit_data,
                "strategies_dir": explicit_strategies or "", "mode": "explicit", "writable": True}

    if not is_frozen():
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))
        backend = os.path.join(repo_root, "backend")
        return {"root": backend, "data_dir": os.path.join(backend, "data"),
                "strategies_dir": explicit_strategies or "", "mode": "source", "writable": True}

    # 只有 Windows 的安装版/免安装版才把数据放在程序目录：那边这是「解压即用」的常规做法，
    # 且安装包会给这些文件夹放开 Users 修改权限。macOS 的 .app 是**包**（往包里写会破坏签名，
    # 也会被 Gatekeeper 当成异常），Linux 也保持原样 —— 它们继续用用户目录。
    program = program_dir()
    if program and sys.platform == "win32":
        data_dir = os.path.join(program, DATA_DIRNAME)
        if is_writable_dir(os.path.join(data_dir, "cache")):
            return {"root": program, "data_dir": data_dir,
                    "strategies_dir": explicit_strategies or os.path.join(program, STRATEGIES_DIRNAME),
                    "mode": "install", "writable": True}

    data_dir = user_data_dir()
    if not is_writable_dir(data_dir):
        data_dir = os.path.join(os.path.expanduser("~"), ".quantstudio", "data")
        os.makedirs(data_dir, exist_ok=True)
    return {"root": os.path.dirname(data_dir), "data_dir": data_dir,
            "strategies_dir": explicit_strategies or user_strategies_dir(),
            "mode": "user", "writable": is_writable_dir(data_dir)}


def _has_legacy_data(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if any(os.path.isfile(os.path.join(path, name)) for name in _LEGACY_MARKERS):
        return True
    for sub in ("csv", "level2"):
        directory = os.path.join(path, sub)
        if os.path.isdir(directory) and any(entry.endswith(".csv") for entry in os.listdir(directory)):
            return True
    return False


def _copy_tree(source: str, target: str, skip: Tuple[str, ...] = ()) -> int:
    """把 ``source`` 的内容复制进 ``target``（已存在的文件不覆盖），返回复制条目数。"""
    copied = 0
    os.makedirs(target, exist_ok=True)
    for name in sorted(os.listdir(source)):
        if name in skip:
            continue
        src = os.path.join(source, name)
        dst = os.path.join(target, name)
        try:
            if os.path.isdir(src):
                copied += _copy_tree(src, dst, skip=())
            elif not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
        except OSError:
            continue                                    # 单个文件失败不影响其它（迁移尽力而为）
    return copied


def migrate_legacy_data(data_dir: str) -> bool:
    """把旧版本用户目录里的数据复制到新的数据目录（只在旧位置有数据、新位置没有时做一次）。"""
    legacy = user_data_dir()
    if os.path.abspath(legacy) == os.path.abspath(data_dir):
        return False
    if not _has_legacy_data(legacy) or _has_legacy_data(data_dir):
        return False
    # 缓存目录（cache/）不迁移：可重建、体积大、跨机可能失效
    return _copy_tree(legacy, data_dir, skip=("cache",)) > 0


def seed_strategies(strategies_dir: str) -> int:
    """首次启动时把包内自带策略（示例 + 模板）复制到用户策略目录。

    只在目录里**一个 .py 都没有**时播种：用户删掉示例后不会「复活」。
    """
    if not strategies_dir:
        return 0
    source = core_strategies_dir()
    if not source or not os.path.isdir(source):
        return 0
    try:
        existing = [name for name in os.listdir(strategies_dir) if name.endswith(".py")]
    except OSError:
        return 0
    if existing:
        return 0
    copied = 0
    for name in sorted(os.listdir(source)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        try:
            shutil.copy2(os.path.join(source, name), os.path.join(strategies_dir, name))
            copied += 1
        except OSError:
            continue
    return copied


def write_folder_notes(root: str) -> List[str]:
    """在每个可导入文件夹里放一份「放什么文件」的说明（已存在则不动）。"""
    written: List[str] = []
    for folder, filename, text in _FOLDER_NOTES:
        directory = os.path.join(root, folder)
        target = os.path.join(directory, filename)
        try:
            os.makedirs(directory, exist_ok=True)
            if os.path.exists(target):
                continue
            with open(target, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(text)
            written.append(target)
        except OSError:
            continue
    return written


def ensure_data_dir() -> str:
    """确定并创建数据目录，返回路径（同时导出 ``QUANTSTUDIO_DATA_DIR``）。"""
    resolved = layout()
    data_dir = str(resolved["data_dir"])
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        data_dir = os.path.join(os.path.expanduser("~"), ".quantstudio", "data")
        os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("QUANTSTUDIO_DATA_DIR", data_dir)
    strategies_dir = str(resolved["strategies_dir"] or "")
    if strategies_dir:
        os.environ.setdefault("QUANTSTUDIO_STRATEGIES_DIR", strategies_dir)
    return data_dir


def prepare_dirs() -> Dict[str, object]:
    """创建用户目录（数据 / 策略 / 各类导入文件夹），并在打包运行时播种 + 迁移。

    只在打包运行时写说明文件与复制示例；源码运行保持仓库干净。
    """
    resolved = layout()
    data_dir = ensure_data_dir()
    resolved["data_dir"] = data_dir
    strategies_dir = str(resolved["strategies_dir"] or "")
    if strategies_dir:
        os.environ.setdefault("QUANTSTUDIO_STRATEGIES_DIR", strategies_dir)
    migrated = False
    seeded = 0
    notes: List[str] = []
    if is_frozen():
        migrated = migrate_legacy_data(data_dir)
        if migrated:
            notes.append("已把旧用户目录的数据复制到 %s（原位置保留）" % data_dir)
        if strategies_dir:
            try:
                os.makedirs(strategies_dir, exist_ok=True)
                seeded = seed_strategies(strategies_dir)
            except OSError:
                notes.append("策略目录不可写，未能放入示例策略：%s" % strategies_dir)
        notes.extend("已生成说明文件：%s" % path for path in write_folder_notes(str(resolved["root"])))
    resolved["migrated"] = migrated
    resolved["seeded"] = seeded
    resolved["notes"] = notes
    return resolved


def ensure_stdio() -> None:
    """保证 ``sys.stdout/stderr`` 可用，并且能在非 UTF-8 控制台下打印中文。

    * PyInstaller ``--windowed``（无控制台）构建下这两个流可能是 ``None``，任何
      ``print()`` 都会抛 ``AttributeError`` —— 兜底指向 ``os.devnull``；
    * Windows 的控制台/管道代码页常常不是 UTF-8（cp1252 / cp936），``print("中文")``
      会抛 ``UnicodeEncodeError``；这里统一切到 UTF-8 且 ``errors="replace"``，
      保证「打印」永远不会打断界面线程。
    """
    if sys.stdout is None or sys.stderr is None:
        try:
            devnull = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            devnull = None
        if devnull is not None:
            if sys.stdout is None:
                sys.stdout = devnull
            if sys.stderr is None:
                sys.stderr = devnull
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if encoding in ("utf8", "utf8mb4", "cp65001"):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 被包装/重定向的流可能不支持
            pass


def prepare() -> dict:
    """导入核心包之前调用一次。"""
    ensure_stdio()
    core_dir = ensure_core_path()
    resolved = prepare_dirs()
    return {"frozen": is_frozen(), "core_dir": core_dir, "data_dir": resolved["data_dir"],
            "strategies_dir": resolved.get("strategies_dir") or "", "root": resolved.get("root") or "",
            "mode": resolved.get("mode") or "", "user_data_dir": user_data_dir(),
            "migrated": bool(resolved.get("migrated")), "seeded": int(resolved.get("seeded") or 0),
            "notes": list(resolved.get("notes") or []),
            "python": sys.version.split()[0], "stdio_guarded": sys.stdout is not None}
