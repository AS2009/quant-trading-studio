# -*- coding: utf-8 -*-
"""行情 / 自选池 / 系统工具分组（``tools_market``）。

| 工具 | 类型 | 说明 |
|---|---|---|
| ``market_overview`` | 只读 | 大盘指数 / 涨跌广度 / 两市成交额与资金（亿元） |
| ``market_quotes`` | 只读 | 个股实时快照（缺省 = 自选池） |
| ``market_kline`` | 只读 | 历史 K 线（默认 60 根、上限 400 根，文本只给首尾与统计） |
| ``market_sectors`` | 只读 | 板块涨幅榜 |
| ``watchlist_list`` | 只读 | 自选池代码列表 |
| ``watchlist_add`` / ``watchlist_remove`` | 写 | 修改本地 ``watchlist.json``（只读模式不可见） |
| ``system_status`` | 只读 | 版本 / 数据源 / 离线状态 / 缓存 / 交易日历 |
| ``system_audit`` | 只读 | 最近写操作审计（``mcp-audit.log``） |

写法与 :mod:`quantstudio.mcp.registry` 的约定一致：``handler(ctx, args)`` 返回
``(人类可读文本, 结构化 dict)``；失败抛 :class:`~quantstudio.mcp.registry.ToolError`
（带 ``hint`` 告诉模型下一步），由服务器转成 ``isError=true`` 的结果，不会打崩进程。
所有业务读写都走 ``quantstudio.services``，本模块只做参数映射、异常翻译与结果整形。
"""

import json
import os
from typing import Any, Dict, List

from ..services.common import normalize_code, provider_meta
from .registry import ToolError, obj, p_int, p_list, p_str

#: K 线一次最多返回多少根（防止一次把上下文灌满）
MAX_KLINE_BARS = 400
#: K 线默认返回多少根
DEFAULT_KLINE_BARS = 60
#: K 线文本摘要里首尾各展示多少根（中间省略，完整序列在结构化数据里）
_KLINE_HEAD_TAIL = 5

#: 行情类工具失败时的统一建议（限流 / 离线 / 代码写错都能照着做下一步）
_MARKET_HINT = ("请确认标的代码（示例 600519.SH）；若数据源限流或处于离线模式，稍后重试，"
                "或先用 system_status 检查数据源、market_overview 查看市场概况。")

_FREQ_LABELS = {"day": "日线", "week": "周线", "month": "月线"}
_ADJUST_LABELS = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}


# --------------------------------------------------------------------------- 通用小工具
def _meta_dict(ctx: Any) -> Dict[str, Any]:
    """读取当前数据源元信息（``DataMeta`` → dict）；任何异常都兜底，绝不阻塞工具。"""
    try:
        return ctx.jsonable(ctx.market.meta())
    except Exception:                             # noqa: BLE001 - 状态类信息不应让工具失败
        pass
    try:
        return ctx.jsonable(provider_meta(getattr(ctx.market, "provider", None)))
    except Exception:                             # noqa: BLE001
        return {"source": "unknown", "stale": False, "offline": True, "as_of": "",
                "latency_ms": 0, "notes": ["数据源元信息不可用"]}


def _is_offline(ctx: Any, meta: Dict[str, Any]) -> bool:
    """离线判定 = settings.offline 或数据源自报 offline。"""
    return bool(meta.get("offline") or getattr(ctx.settings, "offline", False))


def _first_source(rows: List[Dict[str, Any]]) -> str:
    """行情行内自带的来源（``Quote.source`` = sina / tencent / ...），没有则返回空串。"""
    for row in rows or []:
        source = (row or {}).get("source")
        if source:
            return str(source)
    return ""


def _source_note(source: str, meta: Dict[str, Any]) -> str:
    """文本摘要里的数据来源标记：``来源=sina；数据时点=...``。"""
    parts = ["来源=%s" % (source or "unknown")]
    if meta.get("stale"):
        parts.append("缓存可能过期")
    if meta.get("offline"):
        parts.append("离线/本地数据")
    if meta.get("as_of"):
        parts.append("数据时点=%s" % meta["as_of"])
    return "；".join(parts)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把参数夹到 [minimum, maximum]；非数字退回默认值（schema 校验之外的兜底）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _fmt_yi(value: Any) -> str:
    return "无" if value is None else "%.2f 亿元" % _num(value)


def _fmt_ratio(count_up: Any, count_down: Any) -> str:
    return "%s/%s" % (count_up if count_up is not None else "-",
                      count_down if count_down is not None else "-")


def _kline_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """区间统计：首末收盘、区间涨跌幅、最高/最低、均量。"""
    closes = [_num(row.get("close")) for row in rows]
    highs = [_num(row.get("high")) for row in rows]
    lows = [_num(row.get("low")) for row in rows]
    volumes = [_num(row.get("volume_wan")) for row in rows]
    first, last = closes[0], closes[-1]
    change_pct = ((last - first) / first * 100.0) if first else 0.0
    return {
        "start": rows[0].get("date", ""),
        "end": rows[-1].get("date", ""),
        "first_close": first,
        "last_close": last,
        "change_pct": round(change_pct, 2),
        "high": max(highs) if highs else 0.0,
        "low": min(lows) if lows else 0.0,
        "avg_volume_wan": round(sum(volumes) / len(volumes), 2) if volumes else 0.0,
    }


def _kline_row(row: Dict[str, Any]) -> str:
    return "%-10s %9.2f %9.2f %9.2f %9.2f %+9.2f%% %12.2f" % (
        row.get("date", ""), _num(row.get("open")), _num(row.get("high")),
        _num(row.get("low")), _num(row.get("close")), _num(row.get("change_pct")),
        _num(row.get("volume_wan")),
    )


def _kline_table(rows: List[Dict[str, Any]]) -> List[str]:
    """只展示首尾各 5 根（不足 10 根时全展示），完整序列在结构化 ``bars`` 里。"""
    lines = ["%-10s %9s %9s %9s %9s %9s %12s" % (
        "日期", "开盘", "最高", "最低", "收盘", "涨跌幅%", "成交量(万手)")]
    if len(rows) <= _KLINE_HEAD_TAIL * 2:
        lines.extend(_kline_row(row) for row in rows)
        return lines
    lines.extend(_kline_row(row) for row in rows[:_KLINE_HEAD_TAIL])
    hidden = len(rows) - _KLINE_HEAD_TAIL * 2
    lines.append("…（中间 %d 根省略，完整序列见结构化 bars）" % hidden)
    lines.extend(_kline_row(row) for row in rows[-_KLINE_HEAD_TAIL:])
    return lines


def _audit_args(entry: Dict[str, Any]) -> str:
    try:
        text = json.dumps(entry.get("args") or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(entry.get("args"))
    return text[:120] + ("…" if len(text) > 120 else "")


# --------------------------------------------------------------------------- 注册
def register(registry: Any) -> None:
    """把「行情 / 自选池 / 系统」9 个工具注册进 ``registry``（服务器装配时调用）。"""
    # ================================================================== 行情
    @registry.tool(
        "market_overview",
        "查看 A 股大盘概览：主要指数的点位与涨跌幅、涨跌家数（含涨停/跌停）、两市成交额与"
        "主力/北向资金净流入（单位亿元）。适合回答『今天大盘怎么样』、判断市场环境，"
        "或在其它行情工具失败/限流时先看整体。无参数；返回人类可读摘要与结构化数据"
        "（indices / breadth / total_amount_yi / main_net_inflow_yi / north_net_inflow_yi）。",
        obj(),
        group="market", read_only=True, idempotent=True, open_world=True,
    )
    def market_overview(ctx, args):
        data = ctx.call(ctx.market.overview)
        meta = _meta_dict(ctx)
        indices = list(data.get("indices") or [])
        breadth = dict(data.get("breadth") or {})
        source = (_first_source(indices) or str(breadth.get("source") or "")
                  or str(meta.get("source") or "unknown"))

        lines = ["市场概览（%s）" % _source_note(source, meta), ""]
        lines.append("%-10s %-8s %10s %9s %8s %12s" % (
            "代码", "名称", "点位", "涨跌额", "涨跌幅%", "成交额(亿元)"))
        for row in indices:
            lines.append("%-10s %-8s %10.2f %+9.2f %+8.2f%% %12.2f" % (
                row.get("code", ""), row.get("name", ""), _num(row.get("point")),
                _num(row.get("change")), _num(row.get("change_pct")),
                _num(row.get("amount_yi"))))
        if not indices:
            lines.append("（指数数据为空：数据源可能暂时不可用，请稍后重试）")
        lines.append("")
        lines.append("广度：涨 %s / 跌 %s / 平 %s；涨停 %s / 跌停 %s；合计 %s 只" % (
            breadth.get("up", 0), breadth.get("down", 0), breadth.get("flat", 0),
            breadth.get("limit_up", 0), breadth.get("limit_down", 0), breadth.get("total", 0)))
        lines.append("资金：两市成交额 %s；主力净流入 %s；北向净流入 %s" % (
            _fmt_yi(data.get("total_amount_yi")), _fmt_yi(data.get("main_net_inflow_yi")),
            _fmt_yi(data.get("north_net_inflow_yi"))))
        lines.append("单位说明：指数点位=点；涨跌额=点；成交额/资金=亿元；涨跌幅=%。")

        structured = {
            "as_of": meta.get("as_of") or "",
            "source": source,
            "offline": _is_offline(ctx, meta),
            "stale": bool(meta.get("stale")),
            "meta": meta,
            "indices": indices,
            "breadth": breadth,
            "total_amount_yi": _num(data.get("total_amount_yi")),
            "main_net_inflow_yi": data.get("main_net_inflow_yi"),
            "north_net_inflow_yi": data.get("north_net_inflow_yi"),
        }
        return "\n".join(lines), structured

    @registry.tool(
        "market_quotes",
        "查询个股实时行情快照：现价、涨跌额/幅、成交额、换手率、市盈率/市净率、总市值与数据来源。"
        "不传 codes 时查询当前自选池；适合用户问『自选池/某几只股票现在怎么样』时调用。"
        "返回对齐表格摘要与结构化 quotes 列表（每行含 code/name/price/change_pct/amount_yi/source 等字段）。",
        obj({"codes": p_list(
            "要查询的标的代码数组，例如 ['600519.SH', '300750.SZ']；省略（或空数组）则查询自选池。最多 50 只。",
            items={"type": "string"}, max_items=50)}),
        group="market", read_only=True, idempotent=True, open_world=True,
    )
    def market_quotes(ctx, args):
        codes = args.get("codes") or None
        try:
            rows = ctx.call(ctx.market.quotes, codes)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_MARKET_HINT, code=exc.code)
        rows = list(rows or [])
        meta = _meta_dict(ctx)
        source = _first_source(rows) or str(meta.get("source") or "unknown")
        from_watchlist = codes is None
        scope = "自选池" if from_watchlist else ", ".join(codes)

        lines = ["行情快照（%s；%s）" % (scope, _source_note(source, meta)), ""]
        lines.append("%-10s %-8s %10s %9s %8s %12s %8s  %s" % (
            "代码", "名称", "现价", "涨跌额", "涨跌幅%", "成交额(亿元)", "换手率%", "来源"))
        for row in rows:
            lines.append("%-10s %-8s %10.2f %+9.2f %+8.2f%% %12.2f %8.2f  %s" % (
                row.get("code", ""), row.get("name", ""), _num(row.get("price")),
                _num(row.get("change")), _num(row.get("change_pct")),
                _num(row.get("amount_yi")), _num(row.get("turnover_pct")),
                row.get("source", "") or source))
        if not rows:
            lines.append("（没有取到行情：自选池为空或数据源暂不可用；"
                         "可先用 watchlist_list 检查自选池、system_status 检查数据源）")
        lines.append("")
        lines.append("单位说明：价格/涨跌额=元；成交额=亿元；成交量=万手；换手率=%；"
                     "涨跌幅为相对昨收的百分比；各行的 ts 为数据时点。")

        structured = {
            "as_of": meta.get("as_of") or "",
            "source": source,
            "offline": _is_offline(ctx, meta),
            "stale": bool(meta.get("stale")),
            "meta": meta,
            "from_watchlist": from_watchlist,
            "codes": [row.get("code", "") for row in rows],
            "count": len(rows),
            "quotes": rows,
        }
        return "\n".join(lines), structured

    @registry.tool(
        "market_kline",
        "获取个股/指数的历史 K 线（日线/周线/月线，前复权/后复权/不复权）。适合看走势、算均线/波动，"
        "或为策略回测准备数据；默认 60 根、上限 400 根（防止一次灌满上下文）。"
        "文本只给首尾各 5 根与区间统计，完整序列在结构化 bars 里（每根含 date/open/high/low/close/"
        "volume_wan/amount_yi/change_pct/turnover_pct）。",
        obj({
            "symbol": p_str("标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                            examples=["600519.SH", "300750.SZ"]),
            "days": p_int("返回最近多少根 K 线（默认 60，上限 400；越大越占上下文）",
                          default=DEFAULT_KLINE_BARS, minimum=1, maximum=MAX_KLINE_BARS),
            "freq": p_str("K 线周期：day=日线（默认）、week=周线、month=月线",
                          default="day", enum=["day", "week", "month"]),
            "adjust": p_str("复权方式：qfq=前复权（默认）、hfq=后复权、none=不复权",
                            default="qfq", enum=["qfq", "hfq", "none"]),
        }, required=["symbol"]),
        group="market", read_only=True, idempotent=True, open_world=True,
    )
    def market_kline(ctx, args):
        raw_symbol = args.get("symbol")
        if raw_symbol is None or not str(raw_symbol).strip():
            raise ToolError("缺少必需参数 symbol", hint="请传入标的代码，例如 600519.SH。",
                            code="INVALID_ARGS")
        symbol = ctx.call(normalize_code, raw_symbol)
        days = _safe_int(args.get("days"), DEFAULT_KLINE_BARS, 1, MAX_KLINE_BARS)
        freq = str(args.get("freq") or "day").strip().lower()
        adjust = str(args.get("adjust") or "qfq").strip().lower()
        if freq not in _FREQ_LABELS:
            raise ToolError("参数 freq 取值非法：%r" % args.get("freq"),
                            hint="可选值：day（日线）、week（周线）、month（月线）。", code="INVALID_ARGS")
        if adjust not in _ADJUST_LABELS:
            raise ToolError("参数 adjust 取值非法：%r" % args.get("adjust"),
                            hint="可选值：qfq（前复权）、hfq（后复权）、none（不复权）。",
                            code="INVALID_ARGS")

        try:
            rows = ctx.call(ctx.market.kline, symbol, days=days, freq=freq, adjust=adjust)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_MARKET_HINT, code=exc.code)
        rows = list(rows or [])
        if not rows:
            raise ToolError("标的 %s 没有返回 K 线数据。" % symbol, hint=_MARKET_HINT,
                            code="NO_DATA")

        meta = _meta_dict(ctx)
        source = str(meta.get("source") or "unknown")
        summary = _kline_summary(rows)
        lines = ["K 线 %s · %s · %s · 共 %d 根（%s ~ %s；%s）" % (
            symbol, _FREQ_LABELS[freq], _ADJUST_LABELS[adjust], len(rows),
            summary["start"], summary["end"], _source_note(source, meta)), ""]
        lines.extend(_kline_table(rows))
        lines.append("")
        lines.append("区间统计：涨跌幅 %+.2f%%（首收 %.2f → 末收 %.2f）；最高 %.2f / 最低 %.2f；"
                     "均量 %.2f 万手" % (
                         summary["change_pct"], summary["first_close"], summary["last_close"],
                         summary["high"], summary["low"], summary["avg_volume_wan"]))
        lines.append("单位说明：价格=元；成交量=万手；成交额=亿元；涨跌幅为当日相对昨收的百分比。")
        if not any(_num(row.get("amount_yi")) for row in rows):
            lines.append("提示：当前数据源未回填 K 线成交额/换手率（结构化字段为 0），请勿当作真实值。")

        structured = {
            "symbol": symbol,
            "freq": freq,
            "adjust": adjust,
            "days": days,
            "count": len(rows),
            "source": source,
            "offline": _is_offline(ctx, meta),
            "stale": bool(meta.get("stale")),
            "as_of": meta.get("as_of") or "",
            "meta": meta,
            "summary": summary,
            "bars": rows,
        }
        return "\n".join(lines), structured

    @registry.tool(
        "market_sectors",
        "查看板块涨幅榜：板块代码/名称、涨跌幅、主力净流入（亿元）与上涨/下跌家数，按涨幅降序。"
        "适合判断市场热点、解释个股所在板块表现时调用。返回对齐表格摘要与结构化 sectors 列表。",
        obj({"limit": p_int("返回板块数量（1-50，默认 12；越大越占上下文）",
                            default=12, minimum=1, maximum=50)}),
        group="market", read_only=True, idempotent=True, open_world=True,
    )
    def market_sectors(ctx, args):
        limit = _safe_int(args.get("limit"), 12, 1, 50)
        try:
            rows = ctx.call(ctx.market.sectors, limit)
        except ToolError as exc:
            raise ToolError(exc.message, hint=_MARKET_HINT, code=exc.code)
        rows = list(rows or [])
        meta = _meta_dict(ctx)
        source = _first_source(rows) or str(meta.get("source") or "unknown")

        lines = ["板块涨幅榜（前 %d 个；%s）" % (len(rows), _source_note(source, meta)), ""]
        lines.append("%-8s %-10s %9s %14s %10s" % (
            "代码", "名称", "涨跌幅%", "主力净流入(亿元)", "上涨/下跌"))
        for row in rows:
            lines.append("%-8s %-10s %+9.2f %14.2f %10s" % (
                row.get("code", ""), row.get("name", ""), _num(row.get("change_pct")),
                _num(row.get("net_inflow_yi")),
                _fmt_ratio(row.get("up_count"), row.get("down_count"))))
        if not rows:
            lines.append("（没有取到板块数据：数据源可能暂时不可用，请稍后重试）")
        lines.append("")
        lines.append("单位说明：涨跌幅=%；主力净流入=亿元（负数为净流出）；上涨/下跌=板块内家数。")

        structured = {
            "as_of": meta.get("as_of") or "",
            "source": source,
            "offline": _is_offline(ctx, meta),
            "stale": bool(meta.get("stale")),
            "meta": meta,
            "limit": limit,
            "count": len(rows),
            "sectors": rows,
        }
        return "\n".join(lines), structured

    # ================================================================== 自选池
    @registry.tool(
        "watchlist_list",
        "读取当前自选池的标的代码列表（本地 watchlist.json，不请求行情）。"
        "适合在查询行情前确认自选池内容，或用户问『我的自选股有哪些』时调用。"
        "返回文本列表与结构化 codes/count；增删用 watchlist_add / watchlist_remove。",
        obj(),
        group="watchlist", read_only=True, idempotent=True, open_world=False,
    )
    def watchlist_list(ctx, args):
        codes = list(ctx.call(ctx.market.watchlist) or [])
        text = "自选池（%d 个）：%s\n提示：watchlist_add / watchlist_remove 可增删；market_quotes 缺省查询这里。" % (
            len(codes), ", ".join(codes) if codes else "（空）")
        return text, {"codes": codes, "count": len(codes)}

    @registry.tool(
        "watchlist_add",
        "把一只股票加入自选池（写入本地 watchlist.json），返回更新后的完整代码列表。"
        "适合用户说『把 XXX 加自选』时调用；写操作，只读模式（--read-only）下不可用。",
        obj({"code": p_str("要加入自选池的标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                           examples=["600519.SH"])}, required=["code"]),
        group="watchlist", read_only=False, idempotent=True, destructive=False, open_world=False,
    )
    def watchlist_add(ctx, args):
        ctx.write_guard("watchlist_add", args)
        try:
            code = ctx.call(normalize_code, args.get("code"))
            before = list(ctx.call(ctx.market.watchlist) or [])
            codes = list(ctx.call(ctx.market.add_to_watchlist, code) or [])
        except ToolError as exc:
            ctx.record("watchlist_add", args, ok=False, detail=exc.message)
            raise
        changed = code not in before
        ctx.record("watchlist_add", args, ok=True,
                   detail="%s（%s）；自选池 %d 个" % (code, "新增" if changed else "已存在", len(codes)))
        text = "已加入自选池：%s%s（当前 %d 个）：%s" % (
            code, "" if changed else "（原本已在池中，列表未变）", len(codes), ", ".join(codes))
        return text, {"codes": codes, "count": len(codes), "added": code, "changed": changed}

    @registry.tool(
        "watchlist_remove",
        "把一只股票从自选池移除（写入本地 watchlist.json），返回更新后的完整代码列表。"
        "适合用户说『把 XXX 移出自选』时调用；写操作，只读模式（--read-only）下不可用。",
        obj({"code": p_str("要移出自选池的标的代码，例如 600519.SH（也接受 600519 / sh600519）",
                           examples=["600519.SH"])}, required=["code"]),
        group="watchlist", read_only=False, idempotent=True, destructive=False, open_world=False,
    )
    def watchlist_remove(ctx, args):
        ctx.write_guard("watchlist_remove", args)
        try:
            code = ctx.call(normalize_code, args.get("code"))
            before = list(ctx.call(ctx.market.watchlist) or [])
            codes = list(ctx.call(ctx.market.remove_from_watchlist, code) or [])
        except ToolError as exc:
            ctx.record("watchlist_remove", args, ok=False, detail=exc.message)
            raise
        changed = code in before
        ctx.record("watchlist_remove", args, ok=True,
                   detail="%s（%s）；自选池 %d 个" % (code, "移除" if changed else "原本不在", len(codes)))
        text = "已从自选池移除：%s%s（当前 %d 个）：%s" % (
            code, "" if changed else "（原本不在池中，列表未变）", len(codes), ", ".join(codes))
        return text, {"codes": codes, "count": len(codes), "removed": code, "changed": changed}

    # ================================================================== 系统
    @registry.tool(
        "system_status",
        "查看运行环境与数据源状态：版本、模式（real/cache/offline）、数据源降级链与可用性、是否离线、"
        "数据时点、交易日历、缓存统计、后端（numpy/pandas）、策略/交易/自选池概况。"
        "适合在行情异常、接口限流或其它工具失败后首先排查；不发起真实行情请求。"
        "返回文本摘要与结构化 status/provider_meta/source/offline/mode。",
        obj(),
        group="system", read_only=True, idempotent=True, open_world=False,
    )
    def system_status(ctx, args):
        status = dict(ctx.call(ctx.market.system_status) or {})
        meta = _meta_dict(ctx)
        offline = _is_offline(ctx, meta)
        mode = str(status.get("mode") or ("offline" if offline else "real"))
        source = str(meta.get("source") or "unknown")
        provider = dict(status.get("provider") or {})
        calendar = dict(status.get("market") or {})
        cache = dict(status.get("cache") or {})
        stats = dict(cache.get("stats") or {})
        backend = dict(status.get("backend") or {})
        trade = dict(status.get("trade") or {})
        strategies = dict(status.get("strategies") or {})

        lines = ["系统状态（quantstudio-mcp）",
                 "版本        %s" % status.get("version", "?"),
                 "模式        %s（%s；离线=%s）" % (mode, _source_note(source, meta), offline),
                 "数据源      active=%s；可用=%s；链路=%s" % (
                     provider.get("active", "?"),
                     ", ".join(str(item) for item in (provider.get("available") or [])) or "无",
                     ", ".join(str(item) for item in (provider.get("sources") or [])) or "无"),
                 "交易日历    %s（最近交易日 %s）" % (
                     "已收盘" if calendar.get("is_closed") else "未收盘",
                     calendar.get("last_trading_day") or "未知"),
                 "缓存        命中 %s / 未命中 %s；条目 %s/%s" % (
                     stats.get("hits", 0), stats.get("misses", 0),
                     stats.get("size", 0), stats.get("max_size", 0)),
                 "后端        numpy=%s pandas=%s；mode=%s" % (
                     backend.get("numpy"), backend.get("pandas"), backend.get("mode", "?")),
                 "交易        mode=%s；brokers=%s" % (
                     trade.get("mode", "?"), ", ".join(sorted(trade.get("brokers") or {})) or "无"),
                 "策略        共 %s 个（内置 %s / 本地 %s）" % (
                     strategies.get("total", 0), strategies.get("builtin", 0),
                     strategies.get("local", 0)),
                 "自选池      %s 个；数据目录 %s" % (
                     status.get("watchlist_count", 0), status.get("data_dir", ""))]
        for error in (status.get("local") or {}).get("errors") or []:
            lines.append("本地策略错误 %s: %s" % (error.get("file", "?"), error.get("error", "")))

        structured = {
            "status": status,
            "provider_meta": meta,
            "source": source,
            "offline": offline,
            "mode": mode,
            "as_of": meta.get("as_of") or status.get("as_of") or "",
        }
        return "\n".join(lines), structured

    @registry.tool(
        "system_audit",
        "读取最近的 MCP 写操作审计（每次 watchlist/策略/持仓等写工具调用都留痕：时间、工具、参数摘要、"
        "是否成功、备注）。适合用户问『刚才模型改了什么』或写操作排查时调用；"
        "没有审计文件时返回空列表而不是报错。返回结构化 entries（最新在最后）与 count。",
        obj({"limit": p_int("返回最近多少条审计记录（1-200，默认 20）",
                            default=20, minimum=1, maximum=200)}),
        group="system", read_only=True, idempotent=True, open_world=False,
    )
    def system_audit(ctx, args):
        limit = _safe_int(args.get("limit"), 20, 1, 200)
        entries = list(ctx.call(ctx.audit.tail, limit) or [])
        path = str(getattr(ctx.audit, "path", "") or "")

        if entries:
            lines = ["写操作审计（取到最近 %d 条，最新在最后；文件 %s）" % (len(entries), path), ""]
            for entry in entries:
                lines.append("%s  %-20s %s  %s" % (
                    entry.get("ts", ""), entry.get("tool", ""),
                    "成功" if entry.get("ok", True) else "失败", _audit_args(entry)))
                if entry.get("detail"):
                    lines.append("    └ %s" % entry["detail"])
        else:
            lines = ["最近没有写操作审计记录（%s%s）。" % (
                path or "（未配置数据目录）",
                "，文件不存在" if path and not os.path.isfile(path) else "，文件为空")]
        structured = {"path": path, "limit": limit, "count": len(entries), "entries": entries}
        return "\n".join(lines), structured


__all__ = ["register", "MAX_KLINE_BARS", "DEFAULT_KLINE_BARS"]
