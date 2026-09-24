# -*- coding: utf-8 -*-
"""本地 Level-2 文件导入通道（纯标准库、离线可用、零新增依赖）。

用途
----
免费源只有五档盘口与逐笔成交；**十档盘口 / 逐笔委托 / 委托队列**需要付费授权
（券商 / 迅投 QMT / Wind 等）。本项目不逆向任何客户端，只提供这条「把用户自己
导出的文件喂进来」的通道：解析结果与免费源产物是**同一套结构**
（:class:`~quantstudio.core.models.OrderBook` / :class:`~quantstudio.core.models.Tick`），
上层的盘口页 / MCP 无需区分数据来源。

文件约定
--------
- 列名**大小写不敏感**（前后空白与 BOM 会被去掉，也接受常见中文别名）；
- 编码：UTF-8（含 BOM）或 GBK / GB18030；
- **允许首行是表头**（按列名取列，列顺序可换）；无表头时按下列顺序取列；
- 空行、以 ``#`` 开头的注释行会被忽略；坏行（列数不足 / 非法数字 / 非法枚举）跳过；
- 所有解析函数**绝不抛异常**。

盘口文件 ``<code>.orderbook.csv``
    列：``side,level,price,volume[,amount]``

    - ``side``：``bid``（买盘，别名 ``b`` / ``buy`` / ``买``）/ ``ask``（卖盘，别名
      ``a`` / ``sell`` / ``卖``），大小写不敏感；
    - ``level``：档位，从 **1** 开始（1 = 买一 / 卖一），整数，可为任意档（十档即 1..10）；
    - ``price``：价格（元）；
    - ``volume``：挂单量，单位**手**（整数；1 手 = 100 股）；
    - ``amount``：可选，挂单金额（元）；省略、为空或非正数时按
      ``price × volume × 100`` 估算。
    输出：``bids`` 按价格**降序**、``asks`` 按价格**升序**（同价时按 level 升序），
    ``levels`` = 实际返回的档数（两侧较大值），``source="file"``。
    同一 ``(side, level)`` 重复时保留首次出现的有效行；``levels=N`` 时每侧只保留前 N 档。

逐笔文件 ``<code>.ticks.csv``
    列：``time,price,volume[,amount][,side]``

    - ``time``：``HH:MM:SS``（也接受 ``HH:MM`` 或 ``YYYY-MM-DD HH:MM:SS``，只取时间部分并补零）；
    - ``price``：成交价（元）；
    - ``volume``：成交量，单位**手**（整数）；
    - ``amount``：可选，成交金额（元）；省略、为空或非正数时按 ``price × volume × 100`` 估算；
    - ``side``：可选，``buy`` / ``sell`` / ``neutral``（大小写不敏感，另兼容腾讯式
      ``B`` / ``S`` / ``M``），缺省 ``neutral``，其它取值按 ``neutral`` 处理。
    输出：``List[Tick]``，**保持文件顺序**（不排序、不去重；文件里的先后即为原样）。
    无表头且只有 4 列时，第 4 列是 ``buy`` / ``sell`` 之类的方向词则视为 ``side``（金额按估算）。

文件名 / 目录（:class:`Level2FileProvider`）
-------------------------------------------
- 盘口：``<dir>/<code>.orderbook.csv``；逐笔：``<dir>/<code>.ticks.csv``；
- ``<code>`` 支持 ``600519.SH`` / ``600519`` / ``sh600519`` / ``600519-SH``
  （``-``、``_``、空格、``/`` 等价于 ``.``），查询时按规范化后的多种写法依次查找；
- 默认目录：环境变量 ``QUANTSTUDIO_LEVEL2_DIR``，其次 ``settings.data_dir/level2``；
- 目录或文件缺失、文件无有效数据时，Provider 方法抛
  :class:`~quantstudio.core.errors.ProviderUnavailable`，交给上层降级。
"""

import csv
import io
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings, get_settings
from ..core.errors import ProviderUnavailable, SymbolNotFound
from ..core.models import OrderBook, OrderBookLevel, Tick
from . import level2 as level2_mod
from . import symbols

__all__ = [
    "LEVEL2_DIR_ENV",
    "ORDERBOOK_SUFFIX",
    "TICKS_SUFFIX",
    "load_orderbook_csv",
    "load_ticks_csv",
    "Level2FileProvider",
]

#: 导入目录环境变量
LEVEL2_DIR_ENV = "QUANTSTUDIO_LEVEL2_DIR"
#: 盘口文件名后缀
ORDERBOOK_SUFFIX = ".orderbook.csv"
#: 逐笔文件名后缀
TICKS_SUFFIX = ".ticks.csv"
#: 1 手 = 100 股（金额估算系数）
_SHARES_PER_LOT = 100.0
_LEVEL2_SUFFIXES = (ORDERBOOK_SUFFIX, TICKS_SUFFIX)

# --------------------------------------------------------------------------- 列名定义

_ORDERBOOK_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "side": ("side", "方向", "买卖"),
    "level": ("level", "档", "档位", "档数"),
    "price": ("price", "价格", "委托价"),
    "volume": ("volume", "vol", "qty", "数量", "手数", "委托量", "挂单量"),
    "amount": ("amount", "金额", "委托金额", "挂单金额"),
}
_ORDERBOOK_REQUIRED = ("side", "level", "price", "volume")
_ORDERBOOK_POS = {"side": 0, "level": 1, "price": 2, "volume": 3, "amount": 4}

_TICK_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "time": ("time", "时间"),
    "price": ("price", "价格", "成交价"),
    "volume": ("volume", "vol", "qty", "数量", "手数", "成交量"),
    "amount": ("amount", "金额", "成交额"),
    "side": ("side", "方向", "买卖", "性质"),
}
_TICK_REQUIRED = ("time", "price", "volume")
_TICK_POS = {"time": 0, "price": 1, "volume": 2, "amount": 3, "side": 4}

_BID_WORDS = ("bid", "b", "buy", "买", "买盘")
_ASK_WORDS = ("ask", "a", "sell", "卖", "卖盘")
_TICK_SIDE_WORDS = ("buy", "sell", "neutral", "b", "s", "m", "n", "买", "卖", "中性")

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# --------------------------------------------------------------------------- 基础工具

def _decode(raw: bytes) -> str:
    """字节 → 文本：UTF-8（含 BOM）→ GBK → GB18030 → 兜底 replace。"""
    for codec in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_rows(path: Any) -> List[List[str]]:
    """读 CSV 为「单元格列表」列表；文件不存在 / 为空 / 读失败一律返回 ``[]``。"""
    if not path:
        return []
    try:
        with open(str(path), "rb") as handle:
            raw = handle.read()
    except (OSError, TypeError, ValueError):
        return []
    if not raw:
        return []
    text = _decode(raw)
    rows: List[List[str]] = []
    try:
        for line in csv.reader(io.StringIO(text)):
            cells = [str(cell).strip() for cell in line]
            if not cells or not any(cells):
                continue
            if cells[0].startswith("#"):
                continue
            rows.append(cells)
    except csv.Error:
        return rows
    return rows


def _float(value: Any, default: float = 0.0) -> float:
    """宽容浮点：去千分位 / 空白，``-``、``null``、NaN、Inf 都算无效。"""
    text = str(value if value is not None else "").strip().replace(",", "").replace(" ", "")
    if not text or text.lower() in ("-", "--", "null", "none", "nan", "n/a"):
        return default
    try:
        number = float(text)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    number = _float(value, default=float("nan"))
    if math.isnan(number):
        return default
    return int(number)


def _is_number(value: Any) -> bool:
    text = str(value if value is not None else "").strip().replace(",", "").replace(" ", "")
    if not text:
        return False
    try:
        return math.isfinite(float(text))
    except (TypeError, ValueError):
        return False


def _positive_int(value: Any) -> int:
    """正整数（用于 ``levels`` / ``limit``）；非法或 ≤0 返回 0（= 不限）。"""
    if value is None:
        return 0
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _clean_code(code: Any) -> str:
    """``600519-SH`` / ``600519_SH`` / ``600519 SH`` → ``600519.SH``（大写、分隔符统一）。"""
    text = str(code if code is not None else "").strip().upper()
    for char in (" ", "\t", "-", "_", "/"):
        text = text.replace(char, ".")
    while ".." in text:
        text = text.replace("..", ".")
    return text.strip(".")


def _canonical(code: Any) -> str:
    """能规范化成 ``600519.SH`` 就返回它，否则返回 ``""``（不抛异常）。"""
    text = str(code if code is not None else "").strip()
    if not text:
        return ""
    for candidate in (text, _clean_code(text)):
        try:
            return symbols.normalize(candidate)
        except SymbolNotFound:
            continue
    return ""


def _norm_code(code: Any) -> str:
    """规范化为 ``600519.SH``；识别不了时原样返回（去空白），空则返回 ``""``。"""
    text = str(code if code is not None else "").strip()
    return _canonical(text) or text


def _code_from_path(path: Any) -> str:
    """从 ``600519.SH.orderbook.csv`` 这类文件名反推代码；反推不出返回 ``""``。"""
    name = os.path.basename(str(path if path is not None else ""))
    lower = name.lower()
    for known in _LEVEL2_SUFFIXES:
        if lower.endswith(known):
            name = name[: len(name) - len(known)]
            break
    else:
        name = os.path.splitext(name)[0]
    return _canonical(name)


def _stems(code: Any) -> List[str]:
    """代码的各种写法（用于定位文件），按优先级去重。"""
    raw = str(code if code is not None else "").strip()
    out: List[str] = []

    def add(value: Any) -> None:
        text = str(value if value is not None else "").strip()
        if text and text not in out:
            out.append(text)

    add(raw)
    cleaned = _clean_code(raw)
    add(cleaned)
    for candidate in (raw, cleaned):
        canonical = _canonical(candidate)
        if not canonical:
            continue
        add(canonical)                               # 600519.SH
        add(canonical.split(".", 1)[0])              # 600519
        add(canonical.replace(".", "_"))             # 600519_SH
        add(canonical.replace(".", "-"))             # 600519-SH
        try:
            sina = symbols.to_sina_symbol(canonical)
        except SymbolNotFound:                      # pragma: no cover - canonical 已校验
            continue
        add(sina)                                    # sh600519
        add(sina.upper())                            # SH600519
    return out


# --------------------------------------------------------------------------- 表头 / 取列

def _header(rows: List[List[str]], columns: Dict[str, Tuple[str, ...]]) -> Tuple[Optional[Dict[str, int]], List[List[str]]]:
    """识别表头：返回 ``(列名 → 下标, 数据行)``；首行不是表头时返回 ``(None, rows)``。"""
    if not rows:
        return None, []
    first = [str(cell).strip().lower() for cell in rows[0]]
    known = set()
    for names in columns.values():
        known.update(names)
    cells = [cell for cell in first if cell]
    if not cells or not set(cells).issubset(known) or not (set(cells) & set(columns)):
        return None, rows
    index: Dict[str, int] = {}
    for position, cell in enumerate(first):
        for name, names in columns.items():
            if cell in names and name not in index:
                index[name] = position
    return index, rows[1:]


def _cell(row: List[str], index: Optional[Dict[str, int]], name: str, position: int) -> str:
    """按表头映射（有表头）或固定位置（无表头）取列；越界返回空串。"""
    if index is not None:
        at = index.get(name)
        return row[at] if (at is not None and at < len(row)) else ""
    return row[position] if position < len(row) else ""


def _time_text(value: Any) -> str:
    """时间文本：``9:30`` → ``09:30:00``；``2026-09-24 09:30:02`` 只取时间部分。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    token = text.split()[-1]
    match = _TIME_RE.match(token)
    if not match:
        return text
    hour, minute, second = match.groups()
    return "%02d:%s:%s" % (int(hour), minute, second or "00")


def _tick_side(value: Any) -> str:
    text = str(value if value is not None else "").strip().lower()
    if text in ("buy", "b", "买", "买盘"):
        return "buy"
    if text in ("sell", "s", "卖", "卖盘"):
        return "sell"
    return "neutral"


# --------------------------------------------------------------------------- 盘口解析

def load_orderbook_csv(path: Any, code: str = "", levels: Any = None) -> Optional[OrderBook]:
    """解析盘口 CSV（``side,level,price,volume[,amount]``，见模块 docstring）。

    - 文件不存在 / 为空 / 无任何有效档位 → ``None``（**不抛异常**）；
    - ``bids`` 价格降序、``asks`` 价格升序，``levels`` = 实际返回档数（两侧较大值）；
    - ``volume`` 单位手，金额缺省按 ``price × volume × 100`` 估算；
    - ``levels`` 为正整数时每侧只保留前 N 档（0 / 非法 / None = 不截断）。
    """
    rows = _read_rows(path)
    if not rows:
        return None
    index, data = _header(rows, _ORDERBOOK_COLUMNS)
    if index is not None and any(name not in index for name in _ORDERBOOK_REQUIRED):
        return None
    bids: List[Tuple[int, OrderBookLevel]] = []
    asks: List[Tuple[int, OrderBookLevel]] = []
    seen = set()
    for row in data:
        side_word = _cell(row, index, "side", _ORDERBOOK_POS["side"]).strip().lower()
        if side_word in _BID_WORDS:
            side = "bid"
        elif side_word in _ASK_WORDS:
            side = "ask"
        else:
            continue
        level = _int(_cell(row, index, "level", _ORDERBOOK_POS["level"]), 0)
        price = _float(_cell(row, index, "price", _ORDERBOOK_POS["price"]), 0.0)
        volume = _int(_cell(row, index, "volume", _ORDERBOOK_POS["volume"]), 0)
        if level < 1 or price <= 0 or volume <= 0:
            continue
        if (side, level) in seen:
            continue
        seen.add((side, level))
        amount = _float(_cell(row, index, "amount", _ORDERBOOK_POS["amount"]), 0.0)
        if amount <= 0:
            amount = price * volume * _SHARES_PER_LOT
        item = OrderBookLevel(price=round(price, 3), volume=volume, amount=round(amount, 2))
        (bids if side == "bid" else asks).append((level, item))
    if not bids and not asks:
        return None
    limit = _positive_int(levels)
    bid_levels = _ordered_levels(bids, "bid", limit)
    ask_levels = _ordered_levels(asks, "ask", limit)
    return OrderBook(
        code=_norm_code(code) or _code_from_path(path),
        source="file",
        levels=max(len(bid_levels), len(ask_levels)),
        bids=bid_levels,
        asks=ask_levels,
    )


def _ordered_levels(items: List[Tuple[int, OrderBookLevel]], side: str, limit: int) -> List[OrderBookLevel]:
    """先按 level 升序，再按价格（买降 / 卖升）稳定排序；``limit`` 截断每侧档数。"""
    items.sort(key=lambda pair: pair[0])
    items.sort(key=lambda pair: pair[1].price, reverse=(side == "bid"))
    ordered = [item for _, item in items]
    return ordered[:limit] if limit else ordered


# --------------------------------------------------------------------------- 逐笔解析

def load_ticks_csv(path: Any, code: str = "") -> List[Tick]:
    """解析逐笔 CSV（``time,price,volume[,amount][,side]``，见模块 docstring）。

    - 文件不存在 / 为空 / 无有效行 → ``[]``（**不抛异常**）；
    - 保持文件顺序；``code`` 仅为接口一致性保留（:class:`Tick` 不带 code）；
    - ``volume`` 单位手，金额缺省按 ``price × volume × 100`` 估算。
    """
    rows = _read_rows(path)
    if not rows:
        return []
    index, data = _header(rows, _TICK_COLUMNS)
    if index is not None and any(name not in index for name in _TICK_REQUIRED):
        return []
    ticks: List[Tick] = []
    for row in data:
        time_text = _time_text(_cell(row, index, "time", _TICK_POS["time"]))
        price = _float(_cell(row, index, "price", _TICK_POS["price"]), 0.0)
        volume = _int(_cell(row, index, "volume", _TICK_POS["volume"]), 0)
        if not time_text or price <= 0 or volume <= 0:
            continue
        amount_raw = _cell(row, index, "amount", _TICK_POS["amount"])
        side_word = _cell(row, index, "side", _TICK_POS["side"]).strip().lower()
        if index is None and amount_raw and not _is_number(amount_raw):
            # 无表头且只有 4 列时，第 4 列可能是 side（金额按估算）
            if amount_raw.strip().lower() in _TICK_SIDE_WORDS:
                if not side_word:
                    side_word = amount_raw.strip().lower()
                amount_raw = ""
        amount = _float(amount_raw, 0.0)
        if amount <= 0:
            amount = price * volume * _SHARES_PER_LOT
        ticks.append(Tick(
            time=time_text,
            price=round(price, 3),
            volume=volume,
            amount=round(amount, 2),
            side=_tick_side(side_word),
        ))
    return ticks


# --------------------------------------------------------------------------- Provider

class Level2FileProvider:
    """本地 Level-2 文件数据源（用户自有付费权限导出的十档盘口 / 逐笔）。

    与免费源 Provider 同一 duck-typing 接口：``orderbook`` / ``ticks`` /
    ``capabilities`` / ``health``；``name = "file"``、``ORDERBOOK_LEVELS = 10``、
    ``LEVEL2_IMPORT = True``（:func:`quantstudio.data.level2.capabilities` 据此声明
    「十档 + 本地导入」）。文件命名与目录约定见模块 docstring，**不联网**。
    """

    name = "file"
    ORDERBOOK_LEVELS = 10
    LEVEL2_IMPORT = True

    def __init__(self, directory: Any = None, settings: Optional[Settings] = None):
        self.settings = settings if settings is not None else get_settings()
        chosen = str(directory if directory is not None else "").strip()
        if not chosen:
            chosen = os.environ.get(LEVEL2_DIR_ENV, "").strip()
        if not chosen:
            data_dir = str(getattr(self.settings, "data_dir", "") or "").strip()
            chosen = os.path.join(data_dir, "level2") if data_dir else ""
        self.directory = chosen

    # ------------------------------------------------------------------ 查找
    def _find(self, code: Any, suffix: str) -> str:
        """按代码的多种写法在导入目录里找 ``<code><suffix>``；找不到返回 ``""``。"""
        if not self.directory or not os.path.isdir(self.directory):
            return ""
        for stem in _stems(code):
            for name in (stem + suffix, stem.lower() + suffix):
                path = os.path.join(self.directory, name)
                if os.path.isfile(path):
                    return path
        return ""

    # ------------------------------------------------------------------ 盘口
    def orderbook(self, code: str) -> OrderBook:
        """读 ``<dir>/<code>.orderbook.csv`` → :class:`OrderBook`；缺失抛 ProviderUnavailable。"""
        path = self._find(code, ORDERBOOK_SUFFIX)
        if not path:
            raise ProviderUnavailable(
                "Level-2 盘口文件不存在：%s（目录 %s，命名 <code>%s）"
                % (code, self.directory or "-", ORDERBOOK_SUFFIX)
            )
        book = load_orderbook_csv(path, code=code)
        if book is None:
            raise ProviderUnavailable("Level-2 盘口文件无有效档位：%s" % path)
        return book

    # ------------------------------------------------------------------ 逐笔
    def ticks(self, code: str, limit: int = 600) -> List[Tick]:
        """读 ``<dir>/<code>.ticks.csv`` → 按时间升序的**最后** ``limit`` 条。

        文件缺失 / 无有效行抛 :class:`ProviderUnavailable`；``limit`` 非法回退 600，
        ``limit ≤ 0`` 返回空列表。
        """
        path = self._find(code, TICKS_SUFFIX)
        if not path:
            raise ProviderUnavailable(
                "Level-2 逐笔文件不存在：%s（目录 %s，命名 <code>%s）"
                % (code, self.directory or "-", TICKS_SUFFIX)
            )
        items = load_ticks_csv(path, code=code)
        if not items:
            raise ProviderUnavailable("Level-2 逐笔文件无有效数据：%s" % path)
        items.sort(key=lambda item: item.time)
        try:
            top = int(limit)
        except (TypeError, ValueError):
            top = 600
        if top <= 0:
            return []
        return items[-top:]

    # ------------------------------------------------------------------ 能力 / 自检
    def capabilities(self) -> Dict[str, Any]:
        """能力协商（直接复用 :func:`quantstudio.data.level2.capabilities`）。"""
        return level2_mod.capabilities(self)

    def health(self) -> Dict[str, Any]:
        """自检：只检查导入目录里有没有 ``*.orderbook.csv`` / ``*.ticks.csv``（不联网）。"""
        started = time.time()
        files: List[str] = []
        if self.directory and os.path.isdir(self.directory):
            try:
                files = sorted(
                    name for name in os.listdir(self.directory)
                    if name.lower().endswith(_LEVEL2_SUFFIXES)
                )
            except OSError:                       # pragma: no cover - 目录被并发删除
                files = []
        if files:
            detail = "Level-2 导入目录就绪（%s）：%s" % (self.directory, "、".join(files[:6]))
        else:
            detail = (
                "未找到 Level-2 导入文件（目录 %s）；请放 <code>%s / <code>%s，"
                "或设置环境变量 %s" % (self.directory or "-", ORDERBOOK_SUFFIX, TICKS_SUFFIX, LEVEL2_DIR_ENV)
            )
        return {
            "ok": bool(files),
            "detail": detail,
            "latency_ms": int((time.time() - started) * 1000),
            "source": self.name,
        }
