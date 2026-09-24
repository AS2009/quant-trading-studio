# -*- coding: utf-8 -*-
"""盘口 / 逐笔 / 资金流的**口径实现**（纯函数、零网络，便于离线测试）。

数据来源与边界
--------------
- **五档盘口**：腾讯 ``qt.gtimg.cn`` / 新浪 ``hq.sinajs.cn`` 等公开快照接口，档数固定 5；
- **逐笔成交**：腾讯 ``stock.gtimg.cn/data/index.php?appn=detail``，字段为
  ``序号/时间/价格/涨跌/手数/金额/方向``，方向取值 ``B`` / ``S`` / ``M``。
  这是**第三方标注的「盘口方向」**，不是交易所 Level-2 的主动/被动判定，
  全天口径请与快照的「外盘 / 内盘」交叉参考；该接口约覆盖最近 4000 笔（分页拉取）；
- **资金流**：不依赖任何「主力资金」接口，直接由逐笔按**单笔成交额分档**自算——
  ≥100 万 超大单、20–100 万 大单、5–20 万 中单、<5 万 小单（与东财公开口径一致）；
  主力净额 = 超大单 + 大单的（买入额 − 卖出额）；
- **十档 / 逐笔委托 / 委托队列**：没有公开免费来源，需要付费授权（券商 / 迅投 QMT / Wind 等），
  见 :func:`capabilities` 与 ``docs/level2.md``。

解析函数一律不抛异常：解析不出返回 ``None`` / 空列表，由上层决定是否降级。
"""

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.models import CapitalFlow, OrderBook, OrderBookLevel, Tick

__all__ = [
    "BUCKETS", "BUCKET_LABELS", "SUPER_BIG_AMOUNT", "BIG_AMOUNT", "MID_AMOUNT",
    "parse_tencent_orderbook", "parse_sina_orderbook", "parse_tencent_ticks",
    "orderbook_summary", "classify_bucket", "compute_capital_flow", "flow_to_dict",
    "capabilities", "summarize_ticks",
]

# --------------------------------------------------------------------------- 口径常量

SUPER_BIG_AMOUNT = 1_000_000.0     # ≥ 100 万：超大单
BIG_AMOUNT = 200_000.0             # 20–100 万：大单
MID_AMOUNT = 50_000.0              # 5–20 万：中单；< 5 万：小单

BUCKETS = ("super_big", "big", "mid", "small")
BUCKET_LABELS = {"super_big": "超大单", "big": "大单", "mid": "中单", "small": "小单"}

_TENCENT_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


# --------------------------------------------------------------------------- 内部工具

def _f(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _payload(raw: Any) -> str:
    """取回 ``key="..."`` 或 ``key=[0,"..."]`` 里的引号内容（拿不到就原样返回）。"""
    text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    quoted = re.search(r'"(.*)"', text, re.S)
    if quoted:
        return quoted.group(1)
    return text.strip().strip("[],;")


def _side(letter: Any) -> str:
    mark = str(letter or "").strip().upper()
    if mark == "B":
        return "buy"
    if mark == "S":
        return "sell"
    return "neutral"


def _tencent_ts(value: Any) -> str:
    text = str(value or "").strip()
    match = _TENCENT_TS.match(text)
    if not match:
        return text
    year, month, day, hour, minute, second = match.groups()
    return "%s-%s-%s %s:%s:%s" % (year, month, day, hour, minute, second)


def _norm_code(code: Any) -> str:
    """``sh600519`` / ``600519`` → ``600519.SH``（识别不了就原样返回）。"""
    text = str(code or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if len(text) == 8 and text[:2] in ("SH", "SZ", "BJ") and text[2:].isdigit():
        return "%s.%s" % (text[2:], text[:2])
    if text.isdigit() and len(text) == 6:
        market = "SH" if text[0] in ("5", "6", "9") else ("BJ" if text[0] in ("4", "8") else "SZ")
        return "%s.%s" % (text, market)
    return text


def _level(price: Any, volume: Any) -> Optional[OrderBookLevel]:
    """腾讯口径的档位：数量单位是**手**，金额按 价格 × 手数 × 100 估算。"""
    price_value = _f(price, 0.0)
    volume_value = _i(volume, 0)
    if price_value <= 0 or volume_value <= 0:
        return None
    return OrderBookLevel(price=round(price_value, 3), volume=volume_value,
                          amount=round(price_value * volume_value * 100.0, 2))


def _level_from_shares(price: Any, shares: Any) -> Optional[OrderBookLevel]:
    """新浪口径的档位：数量单位是**股**，换算为手（不足 1 手按 1 手），金额按股数如实计算。"""
    price_value = _f(price, 0.0)
    share_value = _i(shares, 0)
    if price_value <= 0 or share_value <= 0:
        return None
    hands = int(share_value // 100)
    return OrderBookLevel(price=round(price_value, 3), volume=hands if hands > 0 else 1,
                          amount=round(price_value * share_value, 2))


# --------------------------------------------------------------------------- 盘口解析


def parse_tencent_orderbook(raw: Any, code: str = "") -> Optional[OrderBook]:
    """解析腾讯快照 ``qt.gtimg.cn/q=sh600519`` 的五档盘口（GBK 需调用方先解码）。"""
    body = _payload(raw)
    fields = body.split("~")
    if len(fields) < 30:
        return None
    symbol = code or fields[2]
    bids: List[OrderBookLevel] = []
    asks: List[OrderBookLevel] = []
    for index in range(5):
        bid = _level(fields[9 + index * 2], fields[10 + index * 2])
        ask = _level(fields[19 + index * 2], fields[20 + index * 2])
        if bid is not None:
            bids.append(bid)
        if ask is not None:
            asks.append(ask)
    if not bids and not asks:
        return None
    return OrderBook(
        code=_norm_code(symbol),
        name=fields[1].strip(),
        price=_f(fields[3]),
        prev_close=_f(fields[4]),
        ts=_tencent_ts(fields[30]) if len(fields) > 30 else "",
        source="tencent",
        levels=max(len(bids), len(asks)),
        bids=bids,
        asks=asks,
        outer_volume=_i(fields[7]),
        inner_volume=_i(fields[8]),
    )


def parse_sina_orderbook(raw: Any, code: str = "") -> Optional[OrderBook]:
    """解析新浪快照 ``hq.sinajs.cn/list=sh600519`` 的五档盘口（GBK 需调用方先解码）。

    注意：新浪的档位数量单位是**股**（腾讯是手），本函数统一换算成手。
    """
    text = raw.decode("gbk", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    match = re.search(r'="([^"]*)"', text)
    if not match:
        return None
    fields = match.group(1).split(",")
    if len(fields) < 30:
        return None
    found = re.search(r"hq_str_([A-Za-z0-9]+)", text)
    symbol = code or (found.group(1) if found else "")
    bids: List[OrderBookLevel] = []
    asks: List[OrderBookLevel] = []
    for index in range(5):
        bid = _level_from_shares(fields[11 + index * 2], fields[10 + index * 2])
        ask = _level_from_shares(fields[21 + index * 2], fields[20 + index * 2])
        if bid is not None:
            bids.append(bid)
        if ask is not None:
            asks.append(ask)
    if not bids and not asks:
        return None
    ts = "%s %s" % (fields[30].strip(), fields[31].strip()) if len(fields) > 31 else ""
    return OrderBook(
        code=_norm_code(symbol),
        name=fields[0].strip(),
        price=_f(fields[3]),
        prev_close=_f(fields[2]),
        ts=ts,
        source="sina",
        levels=max(len(bids), len(asks)),
        bids=bids,
        asks=asks,
    )


def orderbook_summary(book: Optional[OrderBook]) -> Dict[str, Any]:
    """盘口派生指标：委买/委卖量额、委比、委差、价差、中间价。"""
    if book is None:
        return {
            "bid_volume": 0, "ask_volume": 0, "bid_amount": 0.0, "ask_amount": 0.0,
            "imbalance_pct": 0.0, "ratio": 0.0, "spread": 0.0, "mid": 0.0,
        }
    bid_volume = sum(level.volume for level in book.bids)
    ask_volume = sum(level.volume for level in book.asks)
    bid_amount = round(sum(level.amount for level in book.bids), 2)
    ask_amount = round(sum(level.amount for level in book.asks), 2)
    total = bid_volume + ask_volume
    best_bid = book.bids[0].price if book.bids else 0.0
    best_ask = book.asks[0].price if book.asks else 0.0
    spread = round(best_ask - best_bid, 3) if (best_bid > 0 and best_ask > 0) else 0.0
    mid = round((best_bid + best_ask) / 2.0, 3) if (best_bid > 0 and best_ask > 0) else 0.0
    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "bid_amount": bid_amount,
        "ask_amount": ask_amount,
        "imbalance_pct": round((bid_volume - ask_volume) / total * 100.0, 2) if total else 0.0,
        "ratio": round(bid_volume / float(ask_volume), 2) if ask_volume else 0.0,
        "spread": spread,
        "mid": mid,
    }


# --------------------------------------------------------------------------- 逐笔解析

def parse_tencent_ticks(raw: Any, code: str = "") -> List[Tick]:
    """解析腾讯逐笔明细 ``appn=detail&action=data``；解析不出返回空列表。"""
    body = _payload(raw)
    if not body:
        return []
    ticks: List[Tick] = []
    for item in body.split("|"):
        parts = item.split("/")
        if len(parts) < 7:
            continue
        price = _f(parts[2])
        volume = _i(parts[4])
        if price <= 0 or volume <= 0:
            continue
        amount = _f(parts[5])
        ticks.append(Tick(
            time=parts[1].strip(),
            price=round(price, 3),
            volume=volume,
            amount=round(amount if amount > 0 else price * volume * 100.0, 2),
            side=_side(parts[6]),
            change=round(_f(parts[3]), 3),
        ))
    return ticks


def summarize_ticks(ticks: Sequence[Tick]) -> Dict[str, Any]:
    """逐笔的多空统计：笔数、手数、金额与主动买卖占比（按第三方方向标记）。"""
    buy_volume = sell_volume = neutral_volume = 0
    buy_amount = sell_amount = 0.0
    for tick in ticks or []:
        if tick.side == "buy":
            buy_volume += tick.volume
            buy_amount += tick.amount
        elif tick.side == "sell":
            sell_volume += tick.volume
            sell_amount += tick.amount
        else:
            neutral_volume += tick.volume
    total_volume = buy_volume + sell_volume + neutral_volume
    return {
        "count": len(ticks or []),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "neutral_volume": neutral_volume,
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "buy_volume_pct": round(buy_volume / total_volume * 100.0, 2) if total_volume else 0.0,
        "sell_volume_pct": round(sell_volume / total_volume * 100.0, 2) if total_volume else 0.0,
        "net_amount": round(buy_amount - sell_amount, 2),
    }


# --------------------------------------------------------------------------- 资金流

def classify_bucket(amount: Any) -> str:
    """按单笔成交额分档：≥100 万 超大单 / 20–100 万 大单 / 5–20 万 中单 / <5 万 小单。"""
    value = _f(amount, 0.0)
    if value >= SUPER_BIG_AMOUNT:
        return "super_big"
    if value >= BIG_AMOUNT:
        return "big"
    if value >= MID_AMOUNT:
        return "mid"
    return "small"


def compute_capital_flow(ticks: Iterable[Tick], code: str = "", name: str = "",
                         ts: str = "", source: str = "") -> CapitalFlow:
    """由逐笔自算资金流（分档买卖额、净额、主力净额及其占比）。"""
    buckets: Dict[str, Dict[str, float]] = {
        key: {"buy": 0.0, "sell": 0.0, "net": 0.0, "count": 0} for key in BUCKETS
    }
    buy_amount = sell_amount = total = 0.0
    count = 0
    for tick in ticks or []:
        count += 1
        amount = _f(tick.amount, 0.0)
        if amount <= 0:
            amount = _f(tick.price * tick.volume * 100.0, 0.0)
        total += amount
        bucket = buckets[classify_bucket(amount)]
        bucket["count"] += 1
        if tick.side == "buy":
            buy_amount += amount
            bucket["buy"] += amount
        elif tick.side == "sell":
            sell_amount += amount
            bucket["sell"] += amount
    for bucket in buckets.values():
        bucket["buy"] = round(bucket["buy"], 2)
        bucket["sell"] = round(bucket["sell"], 2)
        bucket["net"] = round(bucket["buy"] - bucket["sell"], 2)
    main_net = round(buckets["super_big"]["net"] + buckets["big"]["net"], 2)
    net_amount = round(buy_amount - sell_amount, 2)
    return CapitalFlow(
        code=code,
        name=name,
        ts=ts,
        source=source,
        amount_total=round(total, 2),
        buy_amount=round(buy_amount, 2),
        sell_amount=round(sell_amount, 2),
        net_amount=net_amount,
        main_net=main_net,
        main_net_pct=round(main_net / total * 100.0, 2) if total else 0.0,
        net_pct=round(net_amount / total * 100.0, 2) if total else 0.0,
        buckets=buckets,
        tick_count=count,
    )


def flow_to_dict(flow: Optional[CapitalFlow]) -> Dict[str, Any]:
    """资金流 → JSON 结构（附带每档中文名与买入占比，供前端直接渲染）。"""
    if flow is None:
        return {}
    payload = flow.to_dict()
    buckets = payload.get("buckets") or {}
    enriched: Dict[str, Dict[str, Any]] = {}
    for key in BUCKETS:
        row = dict(buckets.get(key) or {"buy": 0.0, "sell": 0.0, "net": 0.0, "count": 0})
        gross = row["buy"] + row["sell"]
        row["label"] = BUCKET_LABELS[key]
        row["buy_pct"] = round(row["buy"] / gross * 100.0, 2) if gross else 0.0
        enriched[key] = row
    payload["buckets"] = enriched
    return payload


# --------------------------------------------------------------------------- 能力协商

def capabilities(provider: Any) -> Dict[str, Any]:
    """报告数据源（或复合 provider）能提供哪些「盘口级」能力。

    - ``orderbook`` / ``orderbook_levels``：是否有 :meth:`orderbook`、最多几档；
    - ``ticks``：是否有逐笔成交（:meth:`ticks`）；
    - ``orders`` / ``queue``：逐笔委托 / 委托队列（**付费 Level-2 专有**，免费源恒为 False）；
    - ``import``：是否走本地导入通道（用户自己导出的 Level-2 文件）。
    """
    def has(name: str) -> bool:
        return callable(getattr(provider, name, None))

    declared = getattr(provider, "ORDERBOOK_LEVELS", None)
    try:
        levels = int(declared) if declared else 5
    except (TypeError, ValueError):
        levels = 5
    orderbook = has("orderbook")
    detail: List[str] = []
    if orderbook:
        detail.append("%d 档盘口" % max(levels, 1))
    if has("ticks"):
        detail.append("逐笔成交")
    if has("order_events"):
        detail.append("逐笔委托")
    if has("order_queue"):
        detail.append("委托队列")
    return {
        "orderbook": orderbook,
        "orderbook_levels": max(levels, 1) if orderbook else 0,
        "ticks": has("ticks"),
        "orders": has("order_events"),
        "queue": has("order_queue"),
        "import": bool(getattr(provider, "LEVEL2_IMPORT", False)),
        "detail": "、".join(detail) or "无盘口级能力",
    }
