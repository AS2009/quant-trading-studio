# -*- coding: utf-8 -*-
"""标的代码规范化与多源代码转换（数据层最底层公共模块，其它模块都依赖它）。

统一格式
--------
``600519.SH`` —— 6 位数字 + 市场后缀（SH / SZ / BJ）。
所有 Provider 对外只接受/返回这种格式；各家数据源的代码格式在边界处转换。

可接受的输入写法（``normalize``）
--------------------------------
- ``600519``            裸 6 位代码
- ``sh600519`` / ``SH600519``   前缀式
- ``600519.SH`` / ``600519.SZ`` / ``600519.BJ``
- ``600519.XSHG`` / ``600519.XSHE``   聚宽式后缀
- ``600519.SS`` / ``600519.SZ``       雅虎式后缀
- ``000001.SH``         指数（带后缀，不会与平安银行 000001.SZ 混淆）
- ``1.600519`` / ``0.300750``         东方财富 secid（前缀 1=沪，0=深/北）

市场推断规则
------------
- ``6`` / ``9`` 开头      → SH（沪市主板 / 沪市 B 股）
- ``920`` 开头            → BJ（北交所新代码段，**优先于**上面的 9→SH 规则）
- ``15`` / ``16`` / ``18`` 开头 → SZ（深市基金 / LOF / 封闭式基金，159915 创业板 ETF）
- ``0`` / ``2`` / ``3`` 开头 → SZ（深市主板 / 深市 B 股 / 创业板）
- ``4`` / ``8`` 开头      → BJ（北交所）
- ``5`` / ``1`` 开头      → SH（沪市基金 / 债券，如 510300 沪深300ETF、110043 可转债）
- 其它                    → 抛 :class:`~quantstudio.core.errors.SymbolNotFound`

类型判定（``kind_of``）
-----------------------
- 命中 :data:`INDEX_NAMES`、或沪市 ``000xxx``、或深市 ``399xxx`` → ``index``
- 前缀在 :data:`ETF_PREFIXES`（159 / 51 / 56 / 58）                 → ``etf``
- 其它                                                              → ``stock``
"""

import re
from typing import List, Optional, Sequence, Tuple

from ..core.errors import SymbolNotFound

__all__ = [
    "INDEX_NAMES", "ETF_PREFIXES", "MARKETS",
    "normalize", "with_market", "display_code", "market_of", "digits_of",
    "kind_of", "is_index_code", "is_etf_code",
    "to_eastmoney_secid", "to_eastmoney_secids", "to_sina_symbol",
]

MARKETS = ("SH", "SZ", "BJ")

#: 已知指数表（代码 → 名称）。无名称可用时的兜底，也是 ``kind_of`` 的判定依据之一。
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000688.SH": "科创50",
    "000852.SH": "中证1000",
    "000905.SH": "中证500",
    "399001.SZ": "深证成指",
    "399005.SZ": "中小100",
    "399006.SZ": "创业板指",
    "399673.SZ": "创业板50",
}

#: ETF 代码前缀（159xxx.SZ / 51xxxx.SH / 56xxxx.SH / 58xxxx.SH）
ETF_PREFIXES = ("159", "51", "56", "58")

# 后缀 → 市场
_SUFFIX_MAP = {
    "SH": "SH", "SS": "SH", "XSHG": "SH", "SHA": "SH", "SHSE": "SH",
    "SZ": "SZ", "XSHE": "SZ", "SHE": "SZ", "SZSE": "SZ",
    "BJ": "BJ", "BSE": "BJ", "NEEQ": "BJ", "BJSE": "BJ",
}
_PREFIX_MARKETS = ("SH", "SZ", "BJ")
_DIGITS_RE = re.compile(r"^\d{6}$")


def digits_of(code: str) -> str:
    """取 6 位数字部分（``600519.SH`` → ``600519``）。"""
    return normalize(code).split(".", 1)[0]


def market_of(code: str) -> str:
    """取市场后缀（``600519`` → ``SH``）。"""
    return normalize(code).split(".", 1)[1]


def display_code(code: str) -> str:
    """前端展示用的裸 6 位代码（``sh600519`` → ``600519``）。"""
    return digits_of(code)


def with_market(code: str) -> str:
    """确保带市场后缀（等价于 :func:`normalize`，语义化别名）。"""
    return normalize(code)


def normalize(code: str) -> str:
    """把各种写法统一成 ``600519.SH``。

    无法识别时抛 :class:`SymbolNotFound`（它是 ``DataSourceError`` 的子类）。
    """
    if code is None:
        raise SymbolNotFound("标的代码不能为空")
    raw = str(code).strip().upper().replace(" ", "").replace("/", ".")
    if not raw:
        raise SymbolNotFound("标的代码不能为空")

    market = None
    if "." in raw:
        head, _, tail = raw.rpartition(".")
        if tail in _SUFFIX_MAP:
            market = _SUFFIX_MAP[tail]
            raw = head
        elif head in ("0", "1", "2") and _DIGITS_RE.match(tail or ""):
            # 东方财富 secid：1.600519（沪） / 0.300750（深） / 0.430047（北）
            raw = tail
            market = "SH" if head == "1" else infer_market(raw)
        else:
            raise SymbolNotFound("无法识别的标的代码：%r（后缀 %r 未知）" % (code, tail))

    if len(raw) > 2 and raw[:2] in _PREFIX_MARKETS:
        rest = raw[2:]
        if _DIGITS_RE.match(rest):
            # sh600519 / sz300750 / bj430047；有后缀时以后缀为准
            market = market or raw[:2]
            raw = rest

    if not _DIGITS_RE.match(raw):
        raise SymbolNotFound("无法识别的标的代码：%r（应为 6 位数字，可带市场后缀）" % (code,))
    return "%s.%s" % (raw, market or infer_market(raw))


def infer_market(digits: str) -> str:
    """按代码段推断市场（见模块 docstring）。"""
    text = str(digits or "").strip().upper()
    if not _DIGITS_RE.match(text):
        raise SymbolNotFound("无法识别的 6 位代码：%r" % (digits,))
    if text.startswith("920"):
        return "BJ"          # 北交所新代码段（920xxx），优先于 9→SH
    if text.startswith(("15", "16", "18")):
        return "SZ"          # 深市基金 / LOF / 封闭式基金（159915 创业板 ETF、160632 LOF）
    head = text[0]
    if head in ("6", "9"):
        return "SH"
    if head in ("0", "2", "3"):
        return "SZ"
    if head in ("4", "8"):
        return "BJ"
    if head in ("5", "1"):
        return "SH"
    raise SymbolNotFound(
        "无法推断 %s 的市场（6/9→SH，0/2/3→SZ，4/8→BJ，5/1→SH）" % text
    )


def kind_of(code: str) -> str:
    """标的类型：``stock`` / ``index`` / ``etf``。"""
    norm = normalize(code)
    if norm in INDEX_NAMES:
        return "index"
    digits = norm.split(".", 1)[0]
    if digits.startswith(ETF_PREFIXES):
        return "etf"
    market = norm.split(".", 1)[1]
    # 沪市 000xxx 与深市 399xxx 均为指数代码段（沪市不存在 0 开头的个股）
    if market == "SH" and digits.startswith("000"):
        return "index"
    if market == "SZ" and digits.startswith("399"):
        return "index"
    return "stock"


def is_index_code(code: str) -> bool:
    return kind_of(code) == "index"


def is_etf_code(code: str) -> bool:
    return kind_of(code) == "etf"


def to_eastmoney_secid(code: str) -> str:
    """东方财富 secid：沪市 ``1.`` 前缀，深市/北交所 ``0.`` 前缀。

    ``600519.SH`` → ``1.600519``；``300750.SZ`` → ``0.300750``。
    """
    norm = normalize(code)
    digits, market = norm.split(".", 1)
    return ("1." if market == "SH" else "0.") + digits


def to_eastmoney_secids(codes: Sequence[str]) -> List[str]:
    """批量转 secid（自动跳过无法识别的代码）。"""
    out = []
    for item in codes or []:
        try:
            out.append(to_eastmoney_secid(item))
        except SymbolNotFound:
            continue
    return out


def to_sina_symbol(code: str) -> str:
    """新浪代码：``sh600519`` / ``sz300750`` / ``bj430047``（小写前缀）。"""
    norm = normalize(code)
    digits, market = norm.split(".", 1)
    return market.lower() + digits


def split_code(code: str) -> Tuple[str, str]:
    """``600519.SH`` → ``("600519", "SH")``。"""
    norm = normalize(code)
    digits, market = norm.split(".", 1)
    return digits, market


def name_hint(code: str) -> Optional[str]:
    """已知指数名称兜底（未知返回 None）；供各 Provider 复用。"""
    try:
        return INDEX_NAMES.get(normalize(code))
    except SymbolNotFound:
        return None
