# -*- coding: utf-8 -*-
"""盘口 / 逐笔 / 资金流工具分组（``tools_level2``，``group="level2"``）。

| 工具 | 类型 | 说明 |
|---|---|---|
| ``level2_orderbook`` | 只读 | 五档盘口快照 + 委比/委差/价差 + 数据源能力协商 |
| ``level2_ticks`` | 只读 | 最近逐笔成交 + 多空统计（默认 60 条，文本注明覆盖上限与方向口径） |
| ``capital_flow`` | 只读 | 按单笔成交额分档自算的资金流（超大单/大单/中单/小单 + 主力净额） |

口径与边界（写进文本，避免模型当成交易所 Level-2）：

* 五档盘口来自公开快照源（腾讯 / 新浪等），是**快照**；十档行情 / 逐笔委托 / 委托队列没有免费来源；
* 逐笔方向 ``B/S/M`` 是**第三方「盘口方向标记」**，不是交易所 Level-2 的主动买卖判定；
* 逐笔接口约覆盖最近 **4000 笔**，因此资金流是「当日至今的近似」，不是全天精确值；
* 三个工具都是只读（``read_only=True`` / ``destructive=False``）；``idempotent=True``
  表示重复调用无副作用（结果随行情变化，快照新鲜度看 ``meta.as_of`` 与 ``meta.notes``）。

写法与其它 ``tools_*.py`` 一致：``handler(ctx, args)`` 返回 ``(文本, 结构化 dict)``，
失败抛 :class:`~quantstudio.mcp.registry.ToolError`（带 hint），由服务器转成 ``isError`` 结果。
"""

from typing import Any, Dict

from ..services.common import normalize_code
from .registry import ToolError, obj, p_int, p_str

#: 逐笔文本里最多展示多少行（结构化 items 不受影响）
TEXT_TICKS_LIMIT = 60
#: ``level2_ticks`` 的默认 / 上限展示条数
DEFAULT_TICKS_LIMIT = 60
MAX_TICKS_LIMIT = 500
#: ``capital_flow`` 的默认 / 上限样本条数
DEFAULT_FLOW_LIMIT = 2000
MAX_FLOW_LIMIT = 4000

#: 盘口 / 逐笔类工具失败时的统一建议
_LEVEL2_HINT = ("请确认标的代码（示例 600519.SH）；盘口 / 逐笔来自公开行情源（腾讯 / 新浪等），"
                "若数据源不支持该能力、限流或处于离线模式，可先用 system_status 检查数据源，"
                "稍后重试。")

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


# --------------------------------------------------------------------------- 注册
def register(registry: Any) -> None:
    """把「盘口 / 逐笔 / 资金流」3 个只读工具注册进 ``registry``。"""
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


__all__ = [
    "register",
    "TEXT_TICKS_LIMIT",
    "DEFAULT_TICKS_LIMIT",
    "MAX_TICKS_LIMIT",
    "DEFAULT_FLOW_LIMIT",
    "MAX_FLOW_LIMIT",
]
