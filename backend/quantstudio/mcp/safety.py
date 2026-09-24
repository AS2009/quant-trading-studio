# -*- coding: utf-8 -*-
"""写操作保护层：路径护栏、原子写、自动备份、审计日志。

MCP 默认可读写（用户要求「让大模型改策略」），而策略文件是**会被本进程 import 执行的 Python 代码**，
因此所有写操作都要过这一层：

* **路径护栏** ``safe_child``：只接受单层文件名、拒绝 ``..``/绝对路径/子目录，并用 ``realpath``
  再次确认落在允许的目录内（防符号链接逃逸）；
* **原子写** ``atomic_write_text``：先写临时文件再 ``os.replace``，避免半截文件；
* **自动备份** ``backup_file``：覆盖前把旧文件复制到 ``<data>/strategy-backups/``，
  文件名带时间戳，写坏了能回滚（删策略同理，先备份再删）；
* **审计日志** ``AuditLog``：每次写操作追加一行 JSON 到 ``<data>/mcp-audit.log``，
  出问题能查「哪个模型在什么时候改了什么」。

代码本身的正确性由策略校验器（``quantstudio.strategies.lint``）负责——它已经禁止
网络/文件/随机/时间等危险 import 与调用（``FORBIDDEN_MODULES`` / ``FORBIDDEN_CALLS``），
是「能不能落盘」的唯一裁判，本模块不重复实现一套规则。
"""

import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

#: 单个策略源文件的大小上限（防止模型灌进来一个几 MB 的文件）
MAX_SOURCE_BYTES = 128 * 1024
#: 备份目录名（位于数据目录下，不进版本库）
BACKUP_DIRNAME = "strategy-backups"
#: 审计日志文件名（位于数据目录下）
AUDIT_FILENAME = "mcp-audit.log"


class GuardError(Exception):
    """写操作被护栏拒绝（面向模型的可读原因）。"""


def safe_child(directory: str, filename: str, *, suffix: Optional[str] = None) -> str:
    """把 ``filename`` 解析成 ``directory`` 下的安全路径。

    只允许**单层文件名**：``../x.py``、``/etc/passwd``、``a/b.py``、空名一律拒绝；
    ``suffix`` 指定时必须以此结尾；最终用 ``realpath`` 确认仍在 ``directory`` 内。
    """
    if not filename or not isinstance(filename, str):
        raise GuardError("文件名不能为空")
    name = filename.strip()
    if name != os.path.basename(name) or name in (".", ".."):
        raise GuardError("只接受单层文件名，不能包含路径分隔符：%r" % filename)
    if name.startswith("."):
        raise GuardError("文件名不能以点开头：%r" % name)
    if suffix and not name.endswith(suffix):
        raise GuardError("文件名必须以 %s 结尾：%r" % (suffix, name))
    base = os.path.realpath(os.path.abspath(directory))
    target = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(target) != base:
        raise GuardError("拒绝访问目录之外的路径：%r" % filename)
    return target


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> str:
    """原子写文件：同目录临时文件 + ``os.replace``；返回最终路径。"""
    ensure_dir(os.path.dirname(path))
    handle, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def backup_file(path: str, backup_dir: str) -> Optional[str]:
    """把现有文件复制到备份目录（带时间戳）；文件不存在返回 ``None``。"""
    if not os.path.isfile(path):
        return None
    ensure_dir(backup_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = os.path.basename(path)
    target = os.path.join(backup_dir, "%s.%s" % (stem, stamp))
    index = 1
    while os.path.exists(target):                     # 同一秒内多次写入不互相覆盖
        target = os.path.join(backup_dir, "%s.%s-%d" % (stem, stamp, index))
        index += 1
    shutil.copy2(path, target)
    return target


def byte_size(text: str, encoding: str = "utf-8") -> int:
    return len(text.encode(encoding))


def check_source_size(text: str, limit: int = MAX_SOURCE_BYTES) -> None:
    size = byte_size(text)
    if size > limit:
        raise GuardError("内容过大：%d 字节，上限 %d 字节" % (size, limit))


class AuditLog:
    """写操作审计（JSON Lines，追加写，立即 flush）。"""

    def __init__(self, path: str, actor: str = "mcp"):
        self.path = path
        self.actor = actor

    def record(self, tool: str, args: Optional[Dict[str, Any]] = None, ok: bool = True,
               detail: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actor": self.actor,
            "tool": tool,
            "ok": bool(ok),
            "args": _summarize(args or {}),
        }
        if detail:
            entry["detail"] = detail
        if extra:
            entry.update(extra)
        try:
            ensure_dir(os.path.dirname(self.path))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:                                   # 审计失败不能影响主流程
            pass
        return entry

    def tail(self, limit: int = 20) -> List[Dict[str, Any]]:
        """读最近若干条（给 ``system_*`` 类工具排查用）。"""
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-max(1, int(limit)):]
        except OSError:
            return []
        items = []
        for line in lines:
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
        return items


def _summarize(args: Dict[str, Any], limit: int = 400) -> Dict[str, Any]:
    """审计里不落整份源码：长字符串截断，便于回看。"""
    out: Dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 80:
            out[key] = "%s…（%d 字符）" % (value[:60], len(value))
        else:
            out[key] = value
    text = json.dumps(out, ensure_ascii=False)
    if len(text) > limit:
        return {"_truncated": text[:limit]}
    return out


__all__ = [
    "MAX_SOURCE_BYTES",
    "BACKUP_DIRNAME",
    "AUDIT_FILENAME",
    "GuardError",
    "safe_child",
    "ensure_dir",
    "atomic_write_text",
    "backup_file",
    "byte_size",
    "check_source_size",
    "AuditLog",
]
