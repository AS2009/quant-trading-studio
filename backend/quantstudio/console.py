# -*- coding: utf-8 -*-
"""控制台输出编码兜底：让 CLI 在 Windows（cp1252 / cp936 等非 UTF-8 代码页）下也能打印中文。

问题：Windows 上 Python 的 ``sys.stdout`` 往往不是 UTF-8（控制台代码页、重定向到管道
时都可能是 ``cp1252``/``cp936``）。此时 ``print("中文")`` 会抛
``UnicodeEncodeError: 'charmap' codec can't encode ...``，脚本以非 0 退出——
GitHub Actions 的 windows-latest 上就是这样红掉的（``strategies/lint.py`` 打印中文提示时）。

做法：把标准流显式切换到 UTF-8 且 ``errors="replace"``：
    * 中文能正常输出（现代终端与 CI 日志都按 UTF-8 解析）；
    * 个别无法表示的字符退化成 ``?``，**绝不因为「打印」把程序搞崩**。

只影响「怎么把文字写到终端」，不动任何业务逻辑，也不改变返回码语义。
"""

import sys
from typing import List, Tuple

UTF8 = "utf-8"


def _normalized_encoding(stream) -> str:
    return (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")


def is_utf8(stream) -> bool:
    """该流是否已经是 UTF-8（UTF-8 及其子集不需要处理）。"""
    return _normalized_encoding(stream) in ("utf8", "utf8mb4", "cp65001")


def force_utf8_output(*streams: str) -> List[Tuple[str, str]]:
    """把指定的标准流切到 UTF-8（``errors="replace"``）。

    参数是 ``sys`` 上的属性名，默认 ``("stdout", "stderr")``。
    返回 ``[(流名, 切换前编码), ...]``，只列出**真正改动过**的流；
    流为 ``None``（PyInstaller windowed 构建）、不支持 ``reconfigure``（已被包装/替换）
    或本来就是 UTF-8 时直接跳过，永不抛异常。
    """
    names = streams or ("stdout", "stderr")
    changed: List[Tuple[str, str]] = []
    for name in names:
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if is_utf8(stream):
            continue
        original = getattr(stream, "encoding", "") or ""
        try:
            reconfigure(encoding=UTF8, errors="replace")
        except Exception:  # noqa: BLE001 - 被重定向/包装过的流可能不支持，忽略即可
            continue
        changed.append((name, original))
    return changed


__all__ = ["force_utf8_output", "is_utf8", "UTF8"]
