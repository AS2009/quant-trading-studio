# -*- coding: utf-8 -*-
"""磁盘缓存：把真实行情落盘，网络异常时仍能返回「最近一次成功的数据」。

设计要点
--------
- **key**：``hashlib.sha1`` 对 ``namespace|arg1|arg2|...`` 序列化后的字符串取摘要；
  文件按 namespace 分子目录（``<cache_dir>/kline/<sha1>.json``）。
- **原子写**：先写 ``*.tmp``（含 pid/序号避免并发撞名），再 ``os.replace``。
- **永不抛异常**：目录不可写、JSON 损坏、磁盘 IO 失败一律降级为「无缓存」（读写返回 None/False）。
- **记录写入时间与 TTL**：``get`` 只在 ``now - written_at <= ttl`` 时返回；``get_stale``
  忽略 TTL 取旧值（供降级链使用）。
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

__all__ = ["DiskCache", "make_key"]

#: namespace 里允许出现的字符（其余替换为下划线，避免路径穿越）
_NS_SAFE_RE = re.compile(r"[^0-9A-Za-z_.\-]")


def make_key(namespace: str, args: Optional[List[Any]] = None) -> str:
    """拼出缓存的逻辑 key（namespace 在最前面，供分目录使用）。"""
    parts = [str(namespace or "misc")]
    for item in args or []:
        parts.append("" if item is None else str(item))
    return "|".join(parts)


class DiskCache:
    """按 namespace 分目录的 JSON 磁盘缓存（线程安全，失败静默降级）。"""

    def __init__(self, cache_dir: str):
        self.cache_dir = str(cache_dir or "")
        self.enabled = bool(self.cache_dir)
        self.hits = 0
        self.misses = 0
        self.stale_hits = 0
        self.writes = 0
        self.errors: List[str] = []
        self._lock = threading.RLock()
        if self.enabled:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
            except OSError as exc:
                self.enabled = False
                self._note("创建缓存目录失败：%s" % exc)

    # ------------------------------------------------------------------ 工具
    def _note(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message)

    def make_key(self, namespace: str, args: Optional[List[Any]] = None) -> str:
        """实例方法别名（便于 ``cache.make_key(...)`` 调用）。"""
        return make_key(namespace, args)

    def _path(self, key: str) -> str:
        text = str(key or "")
        namespace = text.split("|", 1)[0] or "misc"
        namespace = _NS_SAFE_RE.sub("_", namespace)[:32] or "misc"
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, namespace, digest + ".json")

    @staticmethod
    def _read_entry(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        return entry

    # ------------------------------------------------------------------ 读写
    def get(self, key: str) -> Any:
        """取未过期缓存（不存在/读失败/已过期 → None）。"""
        entry = self.get_entry(key)
        if entry is None:
            return None
        written_at = float(entry.get("written_at") or 0)
        ttl = float(entry.get("ttl") or 0)
        if ttl <= 0 or (time.time() - written_at) > ttl:
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return entry.get("value")

    def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        """取原始条目（含 written_at / ttl），不做过期判断。"""
        if not self.enabled:
            return None
        with self._lock:
            try:
                path = self._path(key)
                if not os.path.exists(path):
                    return None
                return self._read_entry(path)
            except Exception as exc:  # 读缓存失败绝不能影响主流程
                self._note("读取缓存失败：%s" % exc)
                return None

    def get_stale(self, key: str) -> Any:
        """忽略 TTL 取旧值（用于网络异常时的降级）；没有旧值返回 None。"""
        entry = self.get_entry(key)
        if entry is None:
            return None
        with self._lock:
            self.stale_hits += 1
        return entry.get("value")

    def set(self, key: str, value: Any, ttl: float) -> bool:
        """原子写入（临时文件 + os.replace）；失败返回 False，不抛异常。"""
        if not self.enabled:
            return False
        payload = {
            "key": str(key),
            "written_at": time.time(),
            "ttl": float(ttl or 0),
            "value": value,
        }
        tmp = ""
        try:
            with self._lock:
                path = self._path(key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd, tmp = tempfile.mkstemp(
                    prefix=os.path.basename(path) + ".", suffix=".tmp",
                    dir=os.path.dirname(path),
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, allow_nan=False)
                os.replace(tmp, path)
                self.writes += 1
            return True
        except Exception as exc:
            self._note("写入缓存失败：%s" % exc)
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False

    # ------------------------------------------------------------------ 维护
    def clear(self, namespace: Optional[str] = None) -> int:
        """清空缓存（或指定 namespace）；返回删除的文件数。"""
        removed = 0
        if not self.enabled:
            return 0
        with self._lock:
            try:
                if namespace:
                    target = os.path.join(self.cache_dir, _NS_SAFE_RE.sub("_", namespace)[:32])
                    if os.path.isdir(target):
                        removed = len(os.listdir(target))
                        shutil.rmtree(target, ignore_errors=True)
                    return removed
                for name in os.listdir(self.cache_dir):
                    full = os.path.join(self.cache_dir, name)
                    if os.path.isdir(full):
                        try:
                            removed += len(os.listdir(full))
                        except OSError:
                            pass
                        shutil.rmtree(full, ignore_errors=True)
                    elif name.endswith(".json"):
                        os.remove(full)
                        removed += 1
            except Exception as exc:
                self._note("清空缓存失败：%s" % exc)
        return removed

    def stats(self) -> Dict[str, Any]:
        """缓存统计（供 /api/system/status 展示）。"""
        entries = 0
        total_bytes = 0
        namespaces: Dict[str, int] = {}
        if self.enabled:
            try:
                for name in sorted(os.listdir(self.cache_dir)):
                    full = os.path.join(self.cache_dir, name)
                    if not os.path.isdir(full):
                        continue
                    count = 0
                    for fname in os.listdir(full):
                        if not fname.endswith(".json"):
                            continue
                        count += 1
                        try:
                            total_bytes += os.path.getsize(os.path.join(full, fname))
                        except OSError:
                            pass
                    if count:
                        namespaces[name] = count
                        entries += count
            except Exception as exc:
                self._note("统计缓存失败：%s" % exc)
        return {
            "enabled": self.enabled,
            "dir": self.cache_dir,
            "entries": entries,
            "bytes": total_bytes,
            "namespaces": namespaces,
            "hits": self.hits,
            "misses": self.misses,
            "stale_hits": self.stale_hits,
            "writes": self.writes,
            "errors": list(self.errors),
        }
