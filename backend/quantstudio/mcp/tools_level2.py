# -*- coding: utf-8 -*-
"""盘口 / 逐笔 / 资金流 / L2 工具箱 分组（``tools_level2``，``group="level2"``）。

| 工具 | 类型 | 说明 |
|---|---|---|
| ``level2_orderbook`` | 只读 | 五档盘口快照 + 委比/委差/价差 + 数据源能力协商 |
| ``level2_ticks`` | 只读 | 最近逐笔成交 + 多空统计（默认 60 条，文本注明覆盖上限与方向口径） |
| ``capital_flow`` | 只读 | 按单笔成交额分档自算的资金流（超大单/大单/中单/小单 + 主力净额） |
| ``l2_big_orders`` | 只读 | 大单追踪（单笔金额 ≥ 阈值，时间倒序；文本最多列 30 条） |
| ``l2_flow_series`` | 只读 | 资金流分时（分钟聚合 + 累计净额 + 四档分档；文本只列最近 10 分钟） |
| ``l2_seal_status`` | 只读 | 封板状态（涨停/跌停、封单量/额、封成比、距涨停） |
| ``l2_scan`` | 只读 | 盘口异动扫描（缺省 = 自选池，最多 10 只，按委比降序） |
| ``l2_flow_rank`` | 只读 | 资金流排行（缺省 = 自选池，按主力净额降序，约 10–25 秒/只） |

口径与边界（写进文本，避免模型当成交易所 Level-2）：

* 五档盘口来自公开快照源（腾讯 / 新浪等），是**快照**；十档行情 / 逐笔委托 / 委托队列没有免费来源；
* 逐笔方向 ``B/S/M`` 是**第三方「盘口方向标记」**，不是交易所 Level-2 的主动买卖判定；
* 逐笔接口约覆盖最近 **4000 笔**，因此大单 / 资金流是「当日至今的近似」，不是全天精确值；
* 封板只按**当前快照**判断，开板次数不承诺；扫描 / 排行是单次快照的静态特征，
  突变检测（挂单骤增 / 大单撤单）需要两次以上采样，本工具不承诺；
* 批量工具（扫描 / 排行）缺省取**自选池**，最多 10 只，每只各取一次快照（排行还要拉逐笔）；
* 八个工具都是只读（``read_only=True`` / ``destructive=False``）；``idempotent=True``
  表示重复调用无副作用（结果随行情变化，快照新鲜度看 ``meta.as_of`` 与 ``meta.notes``）。

写法与其它 ``tools_*.py`` 一致：``handler(ctx, args)`` 返回 ``(文本, 结构化 dict)``，
失败抛 :class:`~quantstudio.mcp.registry.ToolError`（带 hint），由服务器转成 ``isError`` 结果。
"""

from typing import Any, Dict, List, Optional, Tuple

from ..services.common import normalize_code
from .registry import ToolError, obj, p_int, p_list, p_num, p_str

#: 逐笔文本里最多展示多少行（结构化 items 不受影响）
TEXT_TICKS_LIMIT = 60
#: ``level2_ticks`` 的默认 / 上限展示条数
DEFAULT_TICKS_LIMIT = 60
MAX_TICKS_LIMIT = 500
#: ``capital_flow`` 的默认 / 上限样本条数
DEFAULT_FLOW_LIMIT = 2000
MAX_FLOW_LIMIT = 4000
#: ``l2_big_orders`` 默认门槛（单笔成交额，元）= 100 万（= 超大单分档线）
DEFAULT_BIG_ORDER_THRESHOLD = 1000000.0
#: ``l2_big_orders`` 的默认 / 上限条数，以及文本最多列多少条
DEFAULT_BIG_ORDERS_LIMIT = 50
MAX_BIG_ORDERS_LIMIT = 200
TEXT_BIG_ORDERS_LIMIT = 30
#: ``l2_flow_series`` 的默认 / 上限样本条数，以及文本最多列多少分钟
DEFAULT_SERIES_LIMIT = 2000
MAX_SERIES_LIMIT = 4000
TEXT_FLOW_MINUTES = 10
#: ``l2_scan`` 最多扫多少只（单只 = 一次快照）
DEFAULT_SCAN_LIMIT = 10
MAX_SCAN_CODES = 10
#: ``l2_flow_rank`` 的默认 / 上限样本条数与排序只数（单只 = 一次逐笔，约 10–25 秒）
DEFAULT_RANK_SAMPLE = 1000
MAX_RANK_SAMPLE = 4000
DEFAULT_RANK_TOP = 10
MAX_RANK_TOP = 10
#: 批量工具的耗时提示（写进文本，提醒模型别一次传太多只）
RANK_COST_NOTE = "每只约 10–25 秒，最多 10 只（10 只时可能 2–4 分钟）。"

#: 盘口 / 逐笔类工具失败时的统一建议
_LEVEL2_HINT = ("请确认标的代码（示例 600519.SH）；盘口 / 逐笔来自公开行情源（腾讯 / 新浪等），"
                "若数据源不支持该能力、限流或处于离线模式，可先用 system_status 检查数据源，"
                "稍后重试。")
#: 批量工具（扫描 / 排行）失败时的统一建议
_BATCH_HINT = ("请确认标的代码（示例 600519.SH），或先用 watchlist_add 把标的加入自选池"
               "（缺省 codes = 自选池）；批量工具最多 10 只，数据源限流 / 离线时可先用 "
               "system_status 检查数据源，稍后重试。")

_SIDE_LABELS = {"buy": "买", "sell": "卖", "neutral": "中性"}
_BUCKET_ORDER = ("super_big", "big", "mid", "small")


# --------------------------------------------------------------------------- 通用小工具
def _service(ctx: Any) -> Any:
    """Level2 服务（与 Web / 桌面同一套 ``quantstudio.services``）。"""
    return ctx.services().level2


def _require_code(ctx: Any, args: Dict[str, Any]) -> str:
    """取 code 参数并规范化；缺失 / 非法时抛可读的 ToolError。"""
    raw = args.get("code")
    if raw is None or not str(raw).strip():
        raise ToolError(
            "缺少必需参数 code",
            hint="请传入标的代码，例如 600519.SH（也接受 600519 / sh600519）。",
            code="INVALID_ARGS",
        )
    return ctx.call(normalize_code, raw)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _wan(value: Any) -> float:
    """元 → 万元（负数保留符号）。"""
    return _num(value) / 10000.0


def _safe_limit(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把参数夹到 [minimum, maximum]；非数字退回默认值（schema 校验之外的兜底）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _safe_threshold(value: Any) -> float:
    """大单门槛：非数字 / <= 0 一律退回默认 100 万。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BIG_ORDER_THRESHOLD
    return number if number > 0 else DEFAULT_BIG_ORDER_THRESHOLD


def _sides(args: Dict[str, Any]) -> Optional[List[str]]:
    """``sides`` 参数 → 小写方向列表；空 / 缺省返回 ``None``（买卖都看）。"""
    raw = args.get("sides")
    if not raw:
        return None
    if isinstance(raw, str):                      # 容错：模型偶尔会传单个字符串
        raw = [raw]
    items = [str(item).strip().lower() for item in raw]
    return [item for item in items if item] or None


def _source_note(meta: Dict[str, Any]) -> str:
    """文本摘要里的数据来源标记：``来源=tencent；数据时点=...``。"""
    parts = ["来源=%s" % (meta.get("source") or "unknown")]
    if meta.get("stale"):
        parts.append("缓存可能过期")
    if meta.get("offline"):
        parts.append("离线/本地数据")
    if meta.get("as_of"):
        parts.append("数据时点=%s" % meta["as_of"])
    if meta.get("notes"):
        parts.append("注：%s" % "；".join(str(item) for item in meta["notes"]))
    return "；".join(parts)


def _capability_note(caps: Dict[str, Any]) -> str:
    def flag(key: str) -> str:
        return "支持" if caps.get(key) else "不支持"

    return ("数据源能力：%s；逐笔委托=%s；委托队列=%s；本地导入=%s。"
            "十档行情 / 逐笔委托 / 委托队列需付费授权，本项目只做免费源近似。"
            % (caps.get("detail") or "无盘口级能力", flag("orders"), flag("queue"), flag("import")))


def _codes_or_watchlist(ctx: Any, args: Dict[str, Any], what: str) -> Tuple[List[str], bool]:
    """批量工具的标的来源：``codes`` 优先，缺省（或空数组）取当前自选池。

    返回 ``(codes, from_watchlist)``；自选池也为空时抛可读的 ToolError（提示传 ``codes``）。
    """
    raw = args.get("codes")
    codes = [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
    codes = [item for item in codes if item and item.strip()]
    if codes:
        return codes, False

    watchlist = list(ctx.call(ctx.services().market.watchlist) or [])
    if not watchlist:
        raise ToolError(
            "自选池为空，%s 需要至少一个标的代码。" % what,
            hint="请显式传入 codes，例如 {\"codes\": [\"600519.SH\", \"300750.SZ\"]}；"
                 "或先用 watchlist_add 把标的加入自选池后再调用。",
            code="NO_CODES",
        )
    return [str(item) for item in watchlist], True


# --------------------------------------------------------------------------- 文本摘要
def _orderbook_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    caps = dict(data.get("capabilities") or {})
    summary = dict(data.get("summary") or {})
    bids = list(data.get("bids") or [])
    asks = list(data.get("asks") or [])
    price = _num(data.get("price"))
    prev_close = _num(data.get("prev_close"))
    change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close else 0.0

    lines = [
        "五档盘口 %s %s（%s）" % (data.get("code", ""), data.get("name", ""), _source_note(meta)),
        "现价 %.2f（%+.2f%%）｜昨收 %.2f｜委比 %+.2f%%｜委差 %+d 手｜价差 %s｜中间价 %s" % (
            price, change_pct, prev_close, _num(summary.get("imbalance_pct")),
            _int(summary.get("bid_volume")) - _int(summary.get("ask_volume")),
            ("%.3f" % _num(summary.get("spread"))) if _num(summary.get("spread")) else "—",
            ("%.3f" % _num(summary.get("mid"))) if _num(summary.get("mid")) else "—",
        ),
        "挂单：委买 %d 手 / 委卖 %d 手；外盘 %d 手 / 内盘 %d 手（快照字段，与逐笔方向口径不同）" % (
            _int(summary.get("bid_volume")), _int(summary.get("ask_volume")),
            _int(data.get("outer_volume")), _int(data.get("inner_volume")),
        ),
        "",
        "  %-5s %10s %12s %14s" % ("档位", "价格", "手数", "金额(万元)"),
    ]
    for index in range(len(asks) - 1, -1, -1):
        row = dict(asks[index] or {})
        lines.append("  %-5s %10.3f %12d %14.2f" % (
            "卖%d" % (index + 1), _num(row.get("price")), _int(row.get("volume")),
            _wan(row.get("amount"))))
    for index, item in enumerate(bids):
        row = dict(item or {})
        lines.append("  %-5s %10.3f %12d %14.2f" % (
            "买%d" % (index + 1), _num(row.get("price")), _int(row.get("volume")),
            _wan(row.get("amount"))))
    if not bids and not asks:
        lines.append("  （盘口为空：数据源可能暂时不可用，请稍后重试）")
    lines.append("")
    lines.append("档位数量单位=手；金额=万元。买 %d 档 / 卖 %d 档（levels=%s）。" % (
        len(bids), len(asks), data.get("levels", 0)))
    lines.append(_capability_note(caps))
    return "\n".join(lines)


def _ticks_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    stats = dict(data.get("stats") or {})
    items = list(data.get("items") or [])
    shown = items[-TEXT_TICKS_LIMIT:] if len(items) > TEXT_TICKS_LIMIT else items
    lines = [
        "逐笔成交 %s %s（本次样本 %d 笔；%s）" % (
            data.get("code", ""), data.get("name", ""), len(items), _source_note(meta)),
        "",
        "  %-9s %10s %8s %12s %6s" % ("时间", "价格", "手数", "金额(万元)", "方向"),
    ]
    for item in shown:
        row = dict(item or {})
        lines.append("  %-9s %10.3f %8d %12.2f %6s" % (
            row.get("time", ""), _num(row.get("price")), _int(row.get("volume")),
            _wan(row.get("amount")), _SIDE_LABELS.get(str(row.get("side") or ""), "中性")))
    if not items:
        lines.append("  （本次没有取到逐笔：可能尚未开盘、停牌或数据源限流）")
    elif len(items) > len(shown):
        lines.append("（文本只展示最近 %d 条，完整 %d 条见结构化 items）"
                     % (len(shown), len(items)))
    lines.append("")
    lines.append("多空统计（按第三方方向标记）：买 %d 手 / 卖 %d 手 / 中性 %d 手；"
                 "买量占比 %.2f%%；净额（买-卖）%+.2f 万元" % (
                     _int(stats.get("buy_volume")), _int(stats.get("sell_volume")),
                     _int(stats.get("neutral_volume")), _num(stats.get("buy_volume_pct")),
                     _wan(stats.get("net_amount"))))
    lines.append("口径：%s" % (data.get("note") or ""))
    return "\n".join(lines)


def _capital_flow_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    buckets = dict(data.get("buckets") or {})
    lines = [
        "资金流（按单笔成交额分档自算）%s %s（样本 %d 笔；%s）" % (
            data.get("code", ""), data.get("name", ""), _int(data.get("tick_count")),
            _source_note(meta)),
        "",
        "  %-6s %12s %12s %12s %8s %10s" % ("档位", "买入(万元)", "卖出(万元)", "净额(万元)",
                                           "笔数", "买入占比%"),
    ]
    for key in _BUCKET_ORDER:
        row = dict(buckets.get(key) or {})
        lines.append("  %-6s %12.2f %12.2f %+12.2f %8d %10.2f" % (
            row.get("label") or key, _wan(row.get("buy")), _wan(row.get("sell")),
            _wan(row.get("net")), _int(row.get("count")), _num(row.get("buy_pct"))))
    lines.append("")
    lines.append("主力净额（超大单+大单）%+.2f 万元（占样本成交额 %+.2f%%）；"
                 "样本成交额 %.2f 万元；净额（买-卖）%+.2f 万元" % (
                     _wan(data.get("main_net")), _num(data.get("main_net_pct")),
                     _wan(data.get("amount_total")), _wan(data.get("net_amount"))))
    lines.append("口径：按单笔成交额分档（≥100 万 超大单 / 20-100 万 大单 / 5-20 万 中单 / "
                 "<5 万 小单），由逐笔样本自算（约覆盖最近 4000 笔），不是交易所 Level-2 数据；"
                 "方向为第三方盘口标记。")
    return "\n".join(lines)


def _biggest_text(row: Any) -> str:
    """大单统计里的「最大单笔」：``152.30 万元（10:23:45 买 超大单）``。"""
    if not isinstance(row, dict) or not row:
        return "无"
    return "%.2f 万元（%s %s %s）" % (
        _wan(row.get("amount")), row.get("time", ""),
        _SIDE_LABELS.get(str(row.get("side") or ""), "中性"), row.get("bucket_label", ""))


def _big_orders_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    summary = dict(data.get("summary") or {})
    items = list(data.get("items") or [])
    rows = sorted(items, key=lambda row: str((row or {}).get("time") or ""), reverse=True)
    shown = rows[:TEXT_BIG_ORDERS_LIMIT]

    lines = [
        "大单追踪 %s %s（单笔成交额 ≥ %.2f 万元；样本 %d 笔；%s）" % (
            data.get("code", ""), data.get("name", ""), _wan(data.get("threshold")),
            _int(data.get("tick_sample")), _source_note(meta)),
        "",
        "  %-9s %10s %8s %12s %4s %6s" % ("时间", "价格", "手数", "金额(万元)", "方向", "档位"),
    ]
    for item in shown:
        row = dict(item or {})
        lines.append("  %-9s %10.3f %8d %12.2f %4s %6s" % (
            row.get("time", ""), _num(row.get("price")), _int(row.get("volume")),
            _wan(row.get("amount")), _SIDE_LABELS.get(str(row.get("side") or ""), "中性"),
            row.get("bucket_label") or row.get("bucket") or ""))
    if not rows:
        lines.append("  （样本里没有单笔 ≥ 门槛的成交：可调低 threshold 或换更大的样本 / 标的）")
    elif len(rows) > len(shown):
        lines.append("（文本只列时间倒序前 %d 条；其余 %d 条见结构化 items）"
                     % (len(shown), len(rows) - len(shown)))
    lines.append("")
    lines.append("统计：大单 %d 笔（买 %d / 卖 %d）；买入额 %.2f 万元 / 卖出额 %.2f 万元；"
                 "净额 %+.2f 万元；占样本成交额 %.2f%%（买占比 %.2f%%）" % (
                     _int(summary.get("count")), _int(summary.get("buy_count")),
                     _int(summary.get("sell_count")), _wan(summary.get("buy_amount")),
                     _wan(summary.get("sell_amount")), _wan(summary.get("net_amount")),
                     _num(summary.get("amount_share_pct")), _num(summary.get("buy_amount_pct"))))
    lines.append("最大单笔：%s" % _biggest_text(summary.get("biggest")))
    lines.append("口径：%s" % (data.get("note") or
                              "样本约覆盖最近 4000 笔；方向为第三方盘口标记。"))
    lines.append("说明：文本按时间倒序最多列 %d 条，其余在结构化 items 里；"
                 "金额=万元，手数=手。" % TEXT_BIG_ORDERS_LIMIT)
    return "\n".join(lines)


def _flow_series_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    buckets = dict(data.get("buckets") or {})
    series = list(data.get("series") or [])
    recent = series[-TEXT_FLOW_MINUTES:] if len(series) > TEXT_FLOW_MINUTES else series

    lines = [
        "资金流分时 %s %s（样本 %d 笔 / %d 分钟；%s）" % (
            data.get("code", ""), data.get("name", ""), _int(data.get("tick_sample")),
            _int(data.get("minutes")), _source_note(meta)),
        "",
        "主力净额（超大单+大单）%+.2f 万元（占样本成交额 %+.2f%%）；样本成交额 %.2f 万元" % (
            _wan(data.get("main_net")), _num(data.get("main_net_pct")),
            _wan(data.get("amount_total"))),
        "",
        "四档净额（万元）：",
    ]
    for key in _BUCKET_ORDER:
        row = dict(buckets.get(key) or {})
        lines.append("  %-6s %+12.2f  （买入 %.2f / 卖出 %.2f；%d 笔）" % (
            row.get("label") or key, _wan(row.get("net")), _wan(row.get("buy")),
            _wan(row.get("sell")), _int(row.get("count"))))
    lines.append("")
    lines.append("最近 %d 分钟净额（万元）：" % len(recent))
    lines.append("  %-6s %12s %14s %12s %6s" % ("时间", "净额", "累计净额", "成交额", "笔数"))
    for row in recent:
        row = dict(row or {})
        lines.append("  %-6s %+12.2f %+14.2f %12.2f %6d" % (
            row.get("time", ""), _wan(row.get("net")), _wan(row.get("cum_net")),
            _wan(row.get("amount")), _int(row.get("count"))))
    if not recent:
        lines.append("  （样本里没有可聚合的分钟数据：可能尚未开盘或数据源限流）")
    elif len(series) > len(recent):
        lines.append("（文本只列最近 %d 分钟；完整 %d 分钟序列见结构化 series）"
                     % (len(recent), len(series)))
    lines.append("口径：%s" % (data.get("note") or
                              "每分钟净额 = 主动买 − 主动卖（第三方方向标记）。"))
    lines.append("说明：金额=万元；累计净额按时间递增；其余字段在结构化 buckets / meta 里。")
    return "\n".join(lines)


def _seal_distance_text(seal: Dict[str, Any]) -> str:
    """距涨停 / 距跌停的文字（按状态选口径）。"""
    state = str(seal.get("state") or "unknown")
    distance = _num(seal.get("distance_pct"))
    if state == "unknown":
        return "距涨停 —（数据不足）"
    if state == "limit_up":
        return "距涨停 %+.2f%%（已封板）" % distance
    if state == "limit_down":
        return "距跌停 %+.2f%%（已跌停）" % distance
    return "距涨停 %.2f%%" % max(distance, 0.0)


def _seal_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    seal = dict(data.get("seal") or {})
    label = str(seal.get("label") or "数据不足")
    return "\n".join([
        "封板状态 %s %s（%s）" % (data.get("code", ""), data.get("name", ""), _source_note(meta)),
        "状态：%s（涨跌停幅度：%s）" % (label, seal.get("limit_pct_text") or "未知，请自行核对"),
        "现价 %.2f ｜ 昨收 %.2f ｜ 涨停价 %.2f ｜ 跌停价 %.2f" % (
            _num(data.get("price")), _num(data.get("prev_close")),
            _num(seal.get("limit_up_price")), _num(seal.get("limit_down_price"))),
        "距离：%s" % _seal_distance_text(seal),
        "封单：%d 手 / %.2f 万元 ｜ 封成比（封单额 / 当日成交额）%.2f%% ｜ 当日成交额 %.2f 万元" % (
            _int(seal.get("seal_volume")), _wan(seal.get("seal_amount")),
            _num(seal.get("seal_ratio")), _wan(seal.get("amount_total"))),
        "",
        "提示：%s" % (data.get("note") or ""),
        "口径：仅当前快照；开板次数不承诺（需要盘中多次采样），涨停幅度按板块推断"
        "（ST 为 5%，请自行核对名称后再下结论）。",
    ])


def _seal_cell(row: Dict[str, Any]) -> str:
    """扫描表格里的「封板 / 距涨停」列。"""
    state = str(row.get("seal_state") or "")
    if state == "limit_up":
        return "涨停 封单 %.2f 万元" % _wan(row.get("seal_amount"))
    if state == "limit_down":
        return "跌停 封单 %.2f 万元" % _wan(row.get("seal_amount"))
    if state == "normal":
        return "距涨停 %.2f%%" % _num(row.get("distance_pct"))
    return str(row.get("seal_label") or "封板未知")


def _scan_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    requested = _int(data.get("requested"))
    rows = sorted(list(data.get("items") or []),
                  key=lambda row: _num((row or {}).get("imbalance_pct")), reverse=True)
    failures = list(data.get("failures") or [])
    scope = ("自选池 %d 只" % requested) if data.get("from_watchlist") else ("指定 %d 只" % requested)

    lines = [
        "盘口异动扫描（%s；成功 %d 只；%s）" % (scope, _int(data.get("count")), _source_note(meta)),
        "",
        "  %-10s %-8s %9s %9s %10s %-8s %6s %s" % (
            "代码", "名称", "涨跌幅%", "委比%", "委差(手)", "方向", "量比", "封板/距涨停"),
    ]
    for item in rows:
        row = dict(item or {})
        diff = _int(row.get("bid_volume")) - _int(row.get("ask_volume"))
        direction = "买盘占优" if diff > 0 else ("卖盘占优" if diff < 0 else "均衡")
        volume_ratio = row.get("volume_ratio")
        ratio_text = "—" if volume_ratio in (None, "") else "%.2f" % _num(volume_ratio)
        lines.append("  %-10s %-8s %+8.2f%% %+9.2f%% %+10d %-8s %6s %s" % (
            row.get("code", ""), row.get("name", ""), _num(row.get("change_pct")),
            _num(row.get("imbalance_pct")), diff, direction, ratio_text, _seal_cell(row)))
    if not rows:
        lines.append("  （没有取到盘口：标的可能停牌、代码写错或数据源限流）")
    for row in failures:
        row = dict(row or {})
        lines.append("  失败：%s —— %s" % (row.get("code", ""), row.get("error", "")))

    lines.append("")
    lines.append("排序：按委比（委买量-委卖量）/（委买量+委卖量）降序；最多 10 只（每只一次快照）。")
    lines.append("口径：%s" % (data.get("note") or
                              "单次快照的静态特征；突变检测需要两次以上采样，本工具不承诺。"))
    lines.append("单位：涨跌幅 / 委比 = %；委差 = 手（委买-委卖）；量比 — 表示数据源未回填。")
    return "\n".join(lines)


def _flow_rank_text(data: Dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    rows = sorted(list(data.get("items") or []),
                  key=lambda row: _num((row or {}).get("main_net")), reverse=True)
    failures = list(data.get("failures") or [])
    scope = ("自选池 %d 只" % _int(data.get("requested"))) if data.get("from_watchlist") else (
        "指定 %d 只" % _int(data.get("requested")))

    lines = [
        "资金流排行（%s；成功 %d 只；%s）" % (scope, _int(data.get("count")), _source_note(meta)),
        "",
        "  %-10s %-8s %14s %10s %14s %10s" % (
            "代码", "名称", "主力净额(万元)", "占比%", "净额(万元)", "样本笔数"),
    ]
    for item in rows:
        row = dict(item or {})
        lines.append("  %-10s %-8s %+14.2f %+10.2f %+14.2f %10d" % (
            row.get("code", ""), row.get("name", ""), _wan(row.get("main_net")),
            _num(row.get("main_net_pct")), _wan(row.get("net_amount")),
            _int(row.get("tick_count"))))
    if not rows:
        lines.append("  （没有算出行情流：标的可能停牌、代码写错或数据源限流）")
    for row in failures:
        row = dict(row or {})
        lines.append("  失败：%s —— %s" % (row.get("code", ""), row.get("error", "")))

    lines.append("")
    lines.append("排序：按主力净额降序（主力净额 = 超大单 + 大单净额，占比 = 主力净额 / 样本成交额）。")
    lines.append("耗时提示：%s" % RANK_COST_NOTE)
    lines.append("口径：%s" % (data.get("note") or
                              "样本约最近 4000 笔；方向为第三方盘口标记。"))
    lines.append("单位：金额=万元；样本笔数=参与计算的逐笔条数（每只一次逐笔请求）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 注册
def register(registry: Any) -> None:
    """把「盘口 / 逐笔 / 资金流 / L2 工具箱」8 个只读工具注册进 ``registry``。"""
    @registry.tool(
        "level2_orderbook",
        "查看个股五档盘口快照：买一~买五 / 卖一~卖五的量价与金额、现价/涨跌幅、委比/委差、"
        "价差、外盘/内盘，并附带数据源能力协商（是否支持逐笔 / 十档 / 委托队列 / 本地导入）。"
        "适合回答『某只票现在的盘口挂单怎么样』；免费源只提供五档（十档与委托队列需付费授权）。"
        "返回中文摘要与契约结构化数据（含 summary / capabilities / meta；数量单位为手）。",
        obj({"code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                           examples=["600519.SH"])}, required=["code"]),
        group="level2", title="五档盘口", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def level2_orderbook(ctx, args):
        code = _require_code(ctx, args)
        service = _service(ctx)
        try:
            data = ctx.call(service.orderbook, code)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        return _orderbook_text(data), data

    @registry.tool(
        "level2_ticks",
        "查看最近逐笔成交（时间/价格/手数/金额/方向）与多空统计。方向 B/S/M 是第三方「盘口方向标记」，"
        "不是交易所 Level-2 的主动买卖判定；该接口约覆盖最近 4000 笔（源限制），样本越大越占上下文。"
        "默认展示 60 条：结构化 items 返回请求的条数，文本只列最近 60 条并在末尾注明覆盖上限与方向口径。",
        obj({
            "code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                          examples=["600519.SH"]),
            "limit": p_int("返回的逐笔条数（1-500，默认 60；越大越占上下文）",
                           default=DEFAULT_TICKS_LIMIT, minimum=1, maximum=MAX_TICKS_LIMIT),
        }, required=["code"]),
        group="level2", title="逐笔成交", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def level2_ticks(ctx, args):
        code = _require_code(ctx, args)
        limit = _safe_limit(args.get("limit"), DEFAULT_TICKS_LIMIT, 1, MAX_TICKS_LIMIT)
        service = _service(ctx)
        try:
            data = ctx.call(service.ticks, code, limit=limit)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        if not data.get("items"):
            raise ToolError("标的 %s 没有返回逐笔成交数据。" % code, hint=_LEVEL2_HINT,
                            code="NO_DATA")
        return _ticks_text(data), data

    @registry.tool(
        "capital_flow",
        "查看个股资金流（按单笔成交额分档自算）：超大单 / 大单 / 中单 / 小单的买入额、卖出额、"
        "净额与买入占比，以及主力净额（超大单+大单）与占比。适合回答『主力资金在买还是卖』。"
        "样本 = 拉取到的逐笔（默认 2000 笔、上限 4000 笔，约覆盖最近 4000 笔），是当日至今的近似，"
        "不是交易所 Level-2 数据；返回中文摘要与契约结构化数据（buckets / main_net / meta 等）。",
        obj({
            "code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                          examples=["600519.SH"]),
            "limit": p_int("参与计算的逐笔样本条数（1-4000，默认 2000；越大越占上下文）",
                           default=DEFAULT_FLOW_LIMIT, minimum=1, maximum=MAX_FLOW_LIMIT),
        }, required=["code"]),
        group="level2", title="资金流", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def capital_flow(ctx, args):
        code = _require_code(ctx, args)
        limit = _safe_limit(args.get("limit"), DEFAULT_FLOW_LIMIT, 1, MAX_FLOW_LIMIT)
        service = _service(ctx)
        try:
            data = ctx.call(service.capital_flow, code, limit=limit)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        return _capital_flow_text(data), data

    # ================================================================== L2 工具箱（5 个）
    @registry.tool(
        "l2_big_orders",
        "大单追踪：列出逐笔里**单笔成交额 ≥ threshold**（默认 100 万元）的成交，按时间倒序，"
        "并给出笔数、买入额 / 卖出额、净额、占样本成交额比与最大单笔。"
        "适合回答『刚才有没有大买单 / 大单砸盘』；sides 可只看某方向（[\"buy\"] / [\"sell\"]）。"
        "结构化 items 返回请求的条数，文本只按时间倒序列最多 30 条；"
        "口径：样本约覆盖最近 4000 笔，方向为第三方盘口标记（不是交易所 Level-2 的主动买卖判定）。",
        obj({
            "code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                          examples=["600519.SH"]),
            "threshold": p_num("大单门槛：单笔成交额 ≥ 该值（单位元；默认 1000000 = 100 万，"
                               "常用 200000 / 500000 / 1000000 / 2000000）",
                               default=DEFAULT_BIG_ORDER_THRESHOLD, minimum=1.0),
            "limit": p_int("返回的大单条数（1-200，默认 50；越大越占上下文）",
                           default=DEFAULT_BIG_ORDERS_LIMIT, minimum=1,
                           maximum=MAX_BIG_ORDERS_LIMIT),
            "sides": p_list("只看某个方向：[\"buy\"] 只看主动买、[\"sell\"] 只看主动卖；"
                            "省略（或空数组）= 买卖都看",
                            items={"type": "string", "enum": ["buy", "sell", "neutral"]}),
        }, required=["code"]),
        group="level2", title="大单追踪", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def l2_big_orders(ctx, args):
        code = _require_code(ctx, args)
        threshold = _safe_threshold(args.get("threshold"))
        limit = _safe_limit(args.get("limit"), DEFAULT_BIG_ORDERS_LIMIT, 1, MAX_BIG_ORDERS_LIMIT)
        sides = _sides(args)
        service = _service(ctx)
        try:
            data = ctx.call(service.big_orders, code, threshold=threshold, limit=limit, sides=sides)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        return _big_orders_text(data), data

    @registry.tool(
        "l2_flow_series",
        "资金流分时：把逐笔按**分钟**聚合成主动买 / 主动卖 / 净额 / 累计净额曲线，"
        "并给出四档分档净额与主力净额及占比。适合回答『资金是持续流入还是尾盘流出』。"
        "文本只列最近 10 分钟的净额（避免灌满上下文），完整分钟序列与四档明细在结构化 "
        "series / buckets 里；口径：方向为第三方盘口标记，累计净额按时间递增。",
        obj({
            "code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                          examples=["600519.SH"]),
            "limit": p_int("参与聚合的逐笔样本条数（1-4000，默认 2000；越大越占上下文）",
                           default=DEFAULT_SERIES_LIMIT, minimum=1, maximum=MAX_SERIES_LIMIT),
        }, required=["code"]),
        group="level2", title="资金流分时", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def l2_flow_series(ctx, args):
        code = _require_code(ctx, args)
        limit = _safe_limit(args.get("limit"), DEFAULT_SERIES_LIMIT, 1, MAX_SERIES_LIMIT)
        service = _service(ctx)
        try:
            data = ctx.call(service.flow_series, code, limit=limit)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        return _flow_series_text(data), data

    @registry.tool(
        "l2_seal_status",
        "封板状态：此刻是否涨停 / 跌停 / 未封板，涨跌停幅度说明、涨停价、距涨停 %、"
        "封单量与封单额、封成比（封单额 / 当日成交额）。适合回答『这只票封板了吗、封单厚不厚』。"
        "口径：只按**当前快照**判断，仅当前快照；开板次数不承诺（需要盘中多次采样）；"
        "涨跌停幅度按板块推断（主板 10% / 创业板·科创板 20% / 北交所 30%，ST 为 5% 需自行核对）。",
        obj({"code": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                           examples=["600519.SH"])}, required=["code"]),
        group="level2", title="封板状态", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def l2_seal_status(ctx, args):
        code = _require_code(ctx, args)
        service = _service(ctx)
        try:
            data = ctx.call(service.seal_status, code)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_LEVEL2_HINT, code=exc.code)
        return _seal_text(data), data

    @registry.tool(
        "l2_scan",
        "盘口异动扫描：对一组标的一次性取盘口快照，按**委比**降序给出代码 / 名称 / 涨跌幅 / 委比 / "
        "委差与方向 / 量比 / 封板或距涨停（单次快照的静态特征）。"
        "codes 缺省 = 当前自选池（watchlist）；自选池为空时会报错并提示传 codes。最多 10 只，"
        "每只一次快照（约 1 秒/只）；突变检测（挂单骤增 / 大单撤单）需要多次采样，本工具不承诺。",
        obj({
            "codes": p_list("要扫描的标的代码数组，例如 [\"600519.SH\", \"300750.SZ\"]；"
                            "省略（或空数组）= 扫描当前自选池。最多 10 只。",
                            items={"type": "string"}, max_items=MAX_SCAN_CODES),
            "limit": p_int("最多扫描几只（1-10，默认 10；每只都要取一次快照）",
                           default=DEFAULT_SCAN_LIMIT, minimum=1, maximum=MAX_SCAN_CODES),
        }),
        group="level2", title="盘口异动扫描", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def l2_scan(ctx, args):
        codes, from_watchlist = _codes_or_watchlist(ctx, args, "盘口异动扫描")
        limit = _safe_limit(args.get("limit"), DEFAULT_SCAN_LIMIT, 1, MAX_SCAN_CODES)
        service = _service(ctx)
        try:
            data = ctx.call(service.scan, codes, limit=limit)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_BATCH_HINT, code=exc.code)
        payload = dict(data or {})
        payload["from_watchlist"] = from_watchlist
        return _scan_text(payload), payload

    @registry.tool(
        "l2_flow_rank",
        "资金流排行：对一组标的算主力净额（超大单 + 大单）与占比，按主力净额降序，"
        "给出净额 / 买入额 / 卖出额 / 样本成交额 / 样本笔数。适合回答『自选池里资金在买哪几只』。"
        "codes 缺省 = 当前自选池（watchlist）；自选池为空时会报错并提示传 codes。"
        "耗时提示：每只约 10–25 秒，最多 10 只（10 只时可能 2–4 分钟）；"
        "口径：按单笔成交额分档自算，样本约最近 4000 笔，方向为第三方盘口标记。",
        obj({
            "codes": p_list("要排行的标的代码数组，例如 [\"600519.SH\", \"300750.SZ\"]；"
                            "省略（或空数组）= 当前自选池。最多 10 只。",
                            items={"type": "string"}, max_items=MAX_RANK_TOP),
            "limit": p_int("每只参与计算的逐笔样本条数（1-4000，默认 1000）",
                           default=DEFAULT_RANK_SAMPLE, minimum=1, maximum=MAX_RANK_SAMPLE),
            "top": p_int("最多排行几只（1-10，默认 10；每只约 10–25 秒）",
                         default=DEFAULT_RANK_TOP, minimum=1, maximum=MAX_RANK_TOP),
        }),
        group="level2", title="资金流排行", read_only=True, destructive=False,
        idempotent=True, open_world=True,
    )
    def l2_flow_rank(ctx, args):
        codes, from_watchlist = _codes_or_watchlist(ctx, args, "资金流排行")
        limit = _safe_limit(args.get("limit"), DEFAULT_RANK_SAMPLE, 1, MAX_RANK_SAMPLE)
        top = _safe_limit(args.get("top"), DEFAULT_RANK_TOP, 1, MAX_RANK_TOP)
        service = _service(ctx)
        try:
            data = ctx.call(service.flow_rank, codes, limit=limit, top=top)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_BATCH_HINT, code=exc.code)
        payload = dict(data or {})
        payload["from_watchlist"] = from_watchlist
        return _flow_rank_text(payload), payload


__all__ = [
    "register",
    "TEXT_TICKS_LIMIT",
    "DEFAULT_TICKS_LIMIT",
    "MAX_TICKS_LIMIT",
    "DEFAULT_FLOW_LIMIT",
    "MAX_FLOW_LIMIT",
    "DEFAULT_BIG_ORDER_THRESHOLD",
    "DEFAULT_BIG_ORDERS_LIMIT",
    "MAX_BIG_ORDERS_LIMIT",
    "TEXT_BIG_ORDERS_LIMIT",
    "DEFAULT_SERIES_LIMIT",
    "MAX_SERIES_LIMIT",
    "TEXT_FLOW_MINUTES",
    "DEFAULT_SCAN_LIMIT",
    "MAX_SCAN_CODES",
    "DEFAULT_RANK_SAMPLE",
    "MAX_RANK_SAMPLE",
    "DEFAULT_RANK_TOP",
    "MAX_RANK_TOP",
    "RANK_COST_NOTE",
]
