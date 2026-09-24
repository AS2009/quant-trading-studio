# -*- coding: utf-8 -*-
"""L2 工具口径：大单追踪、封单/涨跌停、资金流分时、盘口扫描与排行（纯函数，零网络）。

这些工具全部只依赖**五档快照**与**逐笔成交**（免费公开源，见 ``data/level2.py`` 顶部说明），
因此能力边界与「付费 Level-2」不同：

- 大单 / 资金流：由逐笔按单笔金额分档自算，逐笔约覆盖最近 4000 笔 → 是当日至今的**近似**；
- 封单：只按**当前快照**判断此刻是否封板（开板次数需要盘中多次采样，不在单次调用里承诺）；
- 扫描 / 排行：每个标的各取一份快照（+ 逐笔），批量调用请自行控制标的数量。

所有函数都不抛异常：数据不足返回空结构，由上层决定展示方式。
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.models import OrderBook, Tick
from .level2 import (
    BUCKET_LABELS, SUPER_BIG_AMOUNT, _f, _i, classify_bucket, orderbook_summary,
)

__all__ = [
    "limit_pct_for", "filter_big_orders", "big_orders_summary", "minute_flow",
    "seal_status", "scan_book", "rank_rows", "price_distribution",
]


def limit_pct_for(code: Any) -> Tuple[float, str]:
    """按代码推断涨跌停幅度：主板 10%、创业板/科创板 20%、北交所 30%。

    **ST / \\*ST 无法从行情接口判断**（需要名称或交易状态），这里按主板 10% 处理，
    返回值第二项会提示核对；调用方应把该说明透出到界面 / 工具文本里。
    """
    text = str(code or "").strip().upper()
    body = text.split(".")[0]
    market = text.split(".")[1] if "." in text else ""
    if body.startswith(("688", "300", "301")):
        return 0.20, "创业板/科创板 20%"
    if market == "BJ" or body.startswith(("4", "8")):
        return 0.30, "北交所 30%"
    return 0.10, "主板 10%（ST 为 5%，请自行核对）"


def _order_row(tick: Tick) -> Dict[str, Any]:
    bucket = classify_bucket(tick.amount)
    return {
        "time": tick.time,
        "price": tick.price,
        "volume": tick.volume,
        "amount": round(_f(tick.amount), 2),
        "side": tick.side,
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS[bucket],
    }


def filter_big_orders(ticks: Sequence[Tick], threshold: Any = SUPER_BIG_AMOUNT,
                      limit: Optional[int] = None,
                      sides: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """逐笔里**单笔金额 ≥ threshold** 的大单，按时间**倒序**（最新在前）。

    ``sides`` 可传 ``("buy",)`` / ``("sell",)`` 只看某一方向；``limit`` 限制条数（``None`` = 全部）。
    """
    try:
        floor = float(threshold)
    except (TypeError, ValueError):
        floor = SUPER_BIG_AMOUNT
    wanted = set(str(item).lower() for item in (sides or ())) or None
    rows: List[Dict[str, Any]] = []
    for tick in ticks or []:
        if wanted is not None and str(tick.side).lower() not in wanted:
            continue
        if _f(tick.amount) < floor:
            continue
        rows.append(_order_row(tick))
    rows.reverse()
    if limit is not None:
        try:
            top = int(limit)
        except (TypeError, ValueError):
            top = len(rows)
        if top >= 0:
            rows = rows[:top]
    return rows


def big_orders_summary(ticks: Sequence[Tick], threshold: Any = SUPER_BIG_AMOUNT,
                       sides: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """大单统计：笔数、买入额、卖出额、净额、占样本成交额比、最大单笔。

    ``sides`` 与 :func:`filter_big_orders` 同义（只看某方向的成交时，统计也必须只算该方向）。
    """
    rows = filter_big_orders(ticks, threshold, sides=sides)
    buy_amount = sum(row["amount"] for row in rows if row["side"] == "buy")
    sell_amount = sum(row["amount"] for row in rows if row["side"] == "sell")
    sample = sum(_f(tick.amount) for tick in ticks or [])
    biggest = max(rows, key=lambda row: row["amount"]) if rows else None
    gross = buy_amount + sell_amount
    return {
        "threshold": _f(threshold, SUPER_BIG_AMOUNT),
        "count": len(rows),
        "buy_count": sum(1 for row in rows if row["side"] == "buy"),
        "sell_count": sum(1 for row in rows if row["side"] == "sell"),
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "net_amount": round(buy_amount - sell_amount, 2),
        "buy_amount_pct": round(buy_amount / gross * 100.0, 2) if gross else 0.0,
        "amount_share_pct": round((buy_amount + sell_amount) / sample * 100.0, 2) if sample else 0.0,
        "biggest": biggest,
    }


def minute_flow(ticks: Sequence[Tick]) -> List[Dict[str, Any]]:
    """逐笔按**分钟**聚合的资金流序列（含累计净额），用于画日内资金曲线。

    每分钟一项：``{time, buy, sell, net, cum_net, amount, count}``；``time`` 取 ``HH:MM``。
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    cumulative = 0.0
    for tick in ticks or []:
        stamp = str(tick.time or "").strip()
        minute = stamp[:5] if len(stamp) >= 5 else stamp
        row = buckets.get(minute)
        if row is None:
            row = {"time": minute, "buy": 0.0, "sell": 0.0, "net": 0.0, "cum_net": 0.0,
                   "amount": 0.0, "count": 0}
            buckets[minute] = row
            order.append(minute)
        amount = _f(tick.amount) or _f(tick.price * tick.volume * 100.0)
        row["amount"] += amount
        row["count"] += 1
        if tick.side == "buy":
            row["buy"] += amount
        elif tick.side == "sell":
            row["sell"] += amount
    out: List[Dict[str, Any]] = []
    for minute in order:
        row = buckets[minute]
        row["net"] = round(row["buy"] - row["sell"], 2)
        cumulative += row["net"]
        row["buy"] = round(row["buy"], 2)
        row["sell"] = round(row["sell"], 2)
        row["amount"] = round(row["amount"], 2)
        row["cum_net"] = round(cumulative, 2)
        out.append(row)
    return out


def seal_status(book: Optional[OrderBook], amount_total: Any = 0.0, code: str = "") -> Dict[str, Any]:
    """封板状态：涨停 / 跌停 / 未封，以及封单量与封成比。

    ``amount_total`` 传当日成交额（元）时给出**封成比** = 封单额 / 成交额。

    只按**当前快照**判断，因此只能说「此刻是否处于封板状态」；开板次数需要盘中多次采样，
    不在单次调用里承诺（界面 / 工具文本会如实标注）。
    """
    target = code or (book.code if book is not None else "")
    pct, pct_text = limit_pct_for(target)
    empty = {
        "state": "unknown", "label": "数据不足", "limit_pct": pct, "limit_pct_text": pct_text,
        "limit_up_price": 0.0, "limit_down_price": 0.0, "distance_pct": 0.0,
        "seal_volume": 0, "seal_amount": 0.0, "seal_ratio": 0.0, "amount_total": 0.0,
    }
    if book is None or _f(book.prev_close) <= 0:
        return empty
    up = round(_f(book.prev_close) * (1.0 + pct), 2)
    down = round(_f(book.prev_close) * (1.0 - pct), 2)
    best_bid = book.bids[0] if book.bids else None
    best_ask = book.asks[0] if book.asks else None
    price = _f(book.price) or (best_bid.price if best_bid else 0.0) or (best_ask.price if best_ask else 0.0)
    if price <= 0:
        return empty
    total = _f(amount_total)
    state, label = "normal", "未封板"
    seal_volume, seal_amount = 0, 0.0
    if price >= up - 0.005:
        state, label = "limit_up", "涨停"
        if best_bid is not None and abs(best_bid.price - up) < 0.005:
            seal_volume = best_bid.volume
            seal_amount = best_bid.amount
    elif price <= down + 0.005:
        state, label = "limit_down", "跌停"
        if best_ask is not None and abs(best_ask.price - down) < 0.005:
            seal_volume = best_ask.volume
            seal_amount = best_ask.amount
    if state == "limit_up":
        distance = round((price - up) / up * 100.0, 2)
    elif state == "limit_down":
        distance = round((price - down) / down * 100.0, 2)
    else:
        distance = round((up - price) / price * 100.0, 2)
    return {
        "state": state,
        "label": label,
        "limit_pct": pct,
        "limit_pct_text": pct_text,
        "limit_up_price": up,
        "limit_down_price": down,
        "distance_pct": distance,
        "seal_volume": seal_volume,
        "seal_amount": round(seal_amount, 2),
        "seal_ratio": round(seal_amount / total * 100.0, 2) if total > 0 else 0.0,
        "amount_total": round(total, 2),
    }


def scan_book(book: Optional[OrderBook], quote: Any = None) -> Dict[str, Any]:
    """单标的的盘口特征（供「异动扫描 / 资金流排行」表使用；不发起任何请求）。"""
    if book is None:
        return {}
    summary = orderbook_summary(book)
    seal = seal_status(book, code=book.code)
    price = _f(book.price)
    prev_close = _f(book.prev_close)
    change_pct = round((price / prev_close - 1.0) * 100.0, 2) if (price > 0 and prev_close > 0) else 0.0
    volume_ratio: Optional[float] = None
    if quote is not None:
        volume_ratio = _f(getattr(quote, "volume_ratio", None)) or None
        change_pct = _f(getattr(quote, "change_pct", change_pct), change_pct)
    return {
        "code": book.code,
        "name": book.name,
        "price": price,
        "change_pct": change_pct,
        "prev_close": prev_close,
        "bid_volume": summary["bid_volume"],
        "ask_volume": summary["ask_volume"],
        "imbalance_pct": summary["imbalance_pct"],
        "ratio": summary["ratio"],
        "spread": summary["spread"],
        "outer_volume": book.outer_volume,
        "inner_volume": book.inner_volume,
        "volume_ratio": volume_ratio,
        "seal_state": seal["state"],
        "seal_label": seal["label"],
        "seal_amount": seal["seal_amount"],
        "distance_pct": seal["distance_pct"],
    }


def rank_rows(rows: Iterable[Dict[str, Any]], key: str = "main_net", top: Optional[int] = None,
              reverse: bool = True) -> List[Dict[str, Any]]:
    """按 ``key`` 排序（默认主力净额降序），取前 ``top`` 条；缺字段按 0 处理。"""
    items = [dict(row) for row in rows or [] if isinstance(row, dict)]
    items.sort(key=lambda row: _f(row.get(key)), reverse=bool(reverse))
    if top is not None:
        try:
            count = int(top)
        except (TypeError, ValueError):
            count = len(items)
        if count >= 0:
            items = items[:count]
    return items


def price_distribution(ticks: Sequence[Tick], bins: int = 10) -> List[Dict[str, Any]]:
    """成交价的分布（按逐笔金额加权），用于看**成交密集区**；数据不足返回空列表。"""
    rows = [tick for tick in ticks or [] if _f(tick.price) > 0 and _i(tick.volume) > 0]
    if not rows:
        return []
    try:
        count = max(1, min(int(bins), 50))
    except (TypeError, ValueError):
        count = 10
    prices = [_f(tick.price) for tick in rows]
    low, high = min(prices), max(prices)
    if high <= low:
        return [{"low": low, "high": high, "volume": sum(_i(tick.volume) for tick in rows),
                 "amount": round(sum(_f(tick.amount) for tick in rows), 2), "count": len(rows)}]
    step = (high - low) / count
    buckets: List[Dict[str, Any]] = [
        {"low": round(low + step * index, 3), "high": round(low + step * (index + 1), 3),
         "volume": 0, "amount": 0.0, "count": 0} for index in range(count)
    ]
    for tick in rows:
        index = int((_f(tick.price) - low) / step)
        if index >= count:
            index = count - 1
        bucket = buckets[index]
        bucket["volume"] += _i(tick.volume)
        bucket["amount"] += _f(tick.amount)
        bucket["count"] += 1
    for bucket in buckets:
        bucket["amount"] = round(bucket["amount"], 2)
    return [bucket for bucket in buckets if bucket["count"] > 0]
