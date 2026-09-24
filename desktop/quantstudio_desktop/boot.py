# -*- coding: utf-8 -*-
"""启动引导：让「源码运行」与「PyInstaller 打包后运行」都能找到核心包与可写数据目录。

- 源码运行：把仓库的 ``backend/`` 目录加入 ``sys.path``，从而能 ``import quantstudio``；
- 打包运行：核心包已被 PyInstaller 收集到 ``sys._MEIPASS``，同样加入 ``sys.path``；
- 数据目录：打包后**不能**写进程序目录（可能位于 Program Files，只读），
  因此默认改用用户目录（Windows: ``%LOCALAPPDATA%\\QuantTradingStudio\\data``），
  除非用户显式设置了 ``QUANTSTUDIO_DATA_DIR``。
"""

import os
import sys
from typing import Optional


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


def user_data_dir(app_name: str = "QuantTradingStudio") -> str:
    """跨平台的用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, app_name, "data")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name, "data")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, app_name, "data")


def ensure_data_dir() -> str:
    """确定并创建数据目录（打包运行时默认放到用户目录）。"""
    explicit = os.environ.get("QUANTSTUDIO_DATA_DIR", "").strip()
    if explicit:
        path = explicit
    elif is_frozen():
        path = user_data_dir()
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))
        path = os.path.join(repo_root, "backend", "data")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.join(os.path.expanduser("~"), ".quantstudio", "data")
        os.makedirs(path, exist_ok=True)
    os.environ.setdefault("QUANTSTUDIO_DATA_DIR", path)
    return path


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
    data_dir = ensure_data_dir()
    return {"frozen": is_frozen(), "core_dir": core_dir, "data_dir": data_dir,
            "python": sys.version.split()[0], "stdio_guarded": sys.stdout is not None}
