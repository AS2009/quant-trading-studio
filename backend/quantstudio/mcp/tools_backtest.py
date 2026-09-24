# -*- coding: utf-8 -*-
"""回测工具组：``backtest_run`` / ``backtest_cache_info`` / ``backtest_cache_clear``。

设计要点：

* ``backtest_run`` 只读但**重**：真实历史日线回测，默认只回绩效摘要；
  ``include_series=true`` 才带净值 / 回撤 / 月度序列，``include_trades=true`` 才带成交流水，
  两类明细都会截断，文本里明确写出「总数 / 已返回条数」；
* 回测结果由服务层做进程内 LRU 缓存（同参数不重复计算），``backtest_cache_*`` 只管这份缓存，
  **不删除任何数据文件**；
* 本模块不打印任何进度：stdout 只允许协议消息（服务层内部日志走 stderr 不受影响）。
"""

import time
import unicodedata
from typing import Any, Dict, List, Sequence, Tuple

from .registry import ToolError, obj, p_bool, p_list, p_num, p_object, p_str

#: 结构化净值 / 回撤 / 月度序列最多返回的条数（超出只保留最近 N 条）
SERIES_LIMIT = 250
#: 结构化成交流水最多返回的笔数
TRADES_LIMIT = 50

#: 文本里展示的成交流水行数上限（结构化另有 TRADES_LIMIT）
TRADE_ROWS_LIMIT = 30

RUN_DESCRIPTION = (
    "用真实历史日线（前复权）回测一个策略。**真实数据回测可能耗时，几十秒也可能**"
    "（标的越多、区间越长越慢）：建议先用小范围（少量标的 + 几个月）试跑，确认无误再扩大区间。"
    "默认只返回绩效摘要（区间 / 交易日数 / 收益 / 回撤 / 夏普 / 索提诺 / 卡玛 / 胜率 / 盈亏比 / 换手 / 费用 /"
    "交易笔数 / 期末持仓数 / warnings）；需要净值、回撤、月度收益序列时传 include_series=true，"
    "需要逐笔成交流水时传 include_trades=true —— 两者默认关闭且会截断，避免一次性灌满上下文。"
    "strategy_id 可用 strategy_list 查询（内置如 st_ma_cross，或 user_xxx 用户策略）。"
)

_SIDE_LABELS = {"buy": "买入", "sell": "卖出"}


# --------------------------------------------------------------------------- 文本小工具
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _width(text: Any) -> int:
    """按终端显示宽度计算（CJK 记 2 列），用于对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int) -> str:
    return str(text) + " " * max(width - _width(text), 0)

def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """渲染对齐的纯文本表格（列宽按显示宽度计算）。"""
    columns = len(headers)
    widths = [max([_width(headers[i])] + [_width(row[i]) for row in rows]) for i in range(columns)]
    lines = ["  ".join(_pad(headers[i], widths[i]) for i in range(columns)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(columns)))
    for row in rows:
        lines.append("  ".join(_pad(row[i], widths[i]) for i in range(columns)).rstrip())
    return "\n".join(lines)


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return ("%%.%df" % digits) % float(value)
    except (TypeError, ValueError):
        return str(value)


def _signed(value: Any, digits: int = 2) -> str:
    text = _num(value, digits)
    if text == "—" or text.startswith("-"):
        return text
    return "+" + text


def _money(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), ",.%df" % digits)
    except (TypeError, ValueError):
        return str(value)


def _drawdown(value: Any, digits: int = 2) -> str:
    """最大回撤统一显示成负数（服务层既可能给正幅值，也可能给负百分数）。"""
    try:
        return _num(-abs(float(value)), digits)
    except (TypeError, ValueError):
        return _num(value, digits)


def _tail(items: Sequence[Any], limit: int) -> Tuple[List[Any], int, bool]:
    """最多取最近 ``limit`` 条；返回 (已取, 总数, 是否截断)。"""
    rows = list(items or [])
    if len(rows) > limit:
        return rows[-limit:], len(rows), True
    return rows, len(rows), False


def _label(mapping: Dict[str, str], value: Any) -> str:
    text = str(value or "").strip().lower()
    return mapping.get(text, text or "—")


# --------------------------------------------------------------------------- 参数映射
def _run_options(args: Dict[str, Any]) -> Dict[str, Any]:
    """MCP 参数 → 服务层 options：只带显式给出的键，缺省交给 settings / 策略默认值。"""
    options: Dict[str, Any] = {}
    symbols = args.get("symbols")
    if symbols:
        options["symbols"] = [str(item) for item in symbols]
    for key in ("start", "end", "benchmark"):
        value = args.get(key)
        if value:
            options[key] = str(value)
    for key in ("initial_cash", "commission_rate", "slippage_bps"):
        if args.get(key) is not None:
            options[key] = args[key]
    params = args.get("params")
    if isinstance(params, dict) and params:
        options["params"] = dict(params)
    return options


# --------------------------------------------------------------------------- 工具实现
def _backtest_run(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    strategy_id = str(args["strategy_id"])
    options = _run_options(args)
    data = ctx.jsonable(ctx.call(ctx.backtest.run, strategy_id, options))
    if not isinstance(data, dict):
        raise ToolError("回测服务返回了意外的结果类型：%s" % type(data).__name__,
                        hint="请重试；若持续失败请查看服务端日志。", code="SERVICE")

    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    strategy = data.get("strategy") if isinstance(data.get("strategy"), dict) else {}
    range_ = data.get("range") if isinstance(data.get("range"), dict) else {}
    nav_all = list(data.get("nav") or [])
    drawdown_all = list(data.get("drawdown") or [])
    monthly_all = list(data.get("monthly") or [])
    trades_all = list(data.get("trades") or [])
    positions = list(data.get("positions") or [])
    warnings = [str(item) for item in (data.get("warnings") or [])]
    trade_total = data.get("trade_total")
    if trade_total is None:
        trade_total = metrics.get("trade_count") or len(trades_all)
    trade_total = int(trade_total or 0)

    include_series = bool(args.get("include_series"))
    include_trades = bool(args.get("include_trades"))

    start = str(range_.get("start") or metrics.get("start") or request.get("start") or "?")
    end = str(range_.get("end") or metrics.get("end") or request.get("end") or "?")
    trading_days = int(metrics.get("trading_days") or 0)
    generated_at = _now()

    rows = [
        ("累计收益", _signed(metrics.get("total_return_pct")), "%"),
        ("年化收益", _signed(metrics.get("annual_return_pct")), "%"),
        ("最大回撤", _drawdown(metrics.get("max_drawdown_pct")), "%"),
        ("夏普比率", _num(metrics.get("sharpe")), "—"),
        ("索提诺比率", _num(metrics.get("sortino")), "—"),
        ("卡玛比率", _num(metrics.get("calmar")), "—"),
        ("胜率", _num(metrics.get("win_rate_pct")), "%"),
        ("盈亏比", _num(metrics.get("profit_loss_ratio")), "—"),
        ("换手率", _num(metrics.get("turnover_pct")), "%"),
        ("总费用", _money(metrics.get("total_fee")), "元"),
        ("交易笔数", "%d" % trade_total, "笔"),          # metrics.trade_count = 已平仓笔数
        ("期末持仓", "%d" % len(positions), "只"),
        ("基准收益", _signed(metrics.get("benchmark_return_pct")), "%"),
    ]

    lines = [
        "回测完成：%s（%s）" % (strategy.get("name") or strategy_id, strategy.get("id") or strategy_id),
        "区间：%s ~ %s（%d 个交易日）｜初始资金：%s 元｜基准：%s"
        % (start, end, trading_days, _money(request.get("initial_cash")),
           request.get("benchmark") or "—"),
        "",
        _table(("指标", "数值", "单位"), rows),
        "说明：交易笔数按已平仓笔数统计（metrics.trade_count）；逐笔买卖成交见 include_trades。",
    ]
    dd_start = str(metrics.get("max_drawdown_start") or "")
    dd_end = str(metrics.get("max_drawdown_end") or "")
    if dd_start or dd_end:
        lines.append("最大回撤区间：%s ~ %s" % (dd_start or "?", dd_end or "?"))

    structured: Dict[str, Any] = {
        "strategy": {"id": strategy.get("id") or strategy_id, "name": strategy.get("name") or ""},
        "range": dict(range_),
        "request": dict(request),
        "metrics": dict(metrics),
        "trade_total": trade_total,
        "positions": positions,
        "positions_count": len(positions),
        "warnings": warnings,
        "series_included": include_series,
        "trades_included": include_trades,
        "generated_at": generated_at,
    }

    if include_series:
        nav, nav_total, nav_truncated = _tail(nav_all, SERIES_LIMIT)
        drawdown, dd_total, dd_truncated = _tail(drawdown_all, SERIES_LIMIT)
        monthly, month_total, month_truncated = _tail(monthly_all, SERIES_LIMIT)
        structured["series"] = {
            "nav": nav,
            "drawdown": drawdown,
            "monthly": monthly,
            "nav_points": len(nav),
            "nav_total": nav_total,
            "nav_truncated": nav_truncated,
            "drawdown_points": len(drawdown),
            "drawdown_total": dd_total,
            "drawdown_truncated": dd_truncated,
            "monthly_points": len(monthly),
            "monthly_total": month_total,
            "monthly_truncated": month_truncated,
        }
        lines.append("")
        lines.append("明细序列（结构化字段 series）：")
        lines.append("- 净值：返回 %d / 共 %d 条%s"
                     % (len(nav), nav_total, "（已截断，只保留最近 %d 条）" % SERIES_LIMIT
                        if nav_truncated else ""))
        if nav:
            last = nav[-1]
            lines.append("  最新 %s：策略 %s，基准 %s"
                         % (last.get("date") or "?", _num(last.get("strategy"), 4),
                            _num(last.get("benchmark"), 4)))
        lines.append("- 回撤：返回 %d / 共 %d 条%s"
                     % (len(drawdown), dd_total, "（已截断，只保留最近 %d 条）" % SERIES_LIMIT
                        if dd_truncated else ""))
        if drawdown:
            last_dd = drawdown[-1]
            lines.append("  最新 %s：%s%%"
                         % (last_dd.get("date") or "?", _num(last_dd.get("dd_pct"))))
        lines.append("- 月度收益：返回 %d / 共 %d 条%s"
                     % (len(monthly), month_total, "（已截断，只保留最近 %d 条）" % SERIES_LIMIT
                        if month_truncated else ""))
        if nav_truncated or dd_truncated or month_truncated:
            lines.append("  说明：序列已按上限截断，需要更早的数据请缩短回测区间后分段查看。")

    if include_trades:
        trades, trades_total, trades_truncated = _tail(trades_all, TRADES_LIMIT)
        structured["trades"] = trades
        structured["trades_shown"] = len(trades)
        structured["trades_total"] = trades_total
        structured["trades_truncated"] = trades_truncated
        lines.append("")
        if trades:
            lines.append("成交流水（结构化字段 trades，逐笔买卖）：返回 %d / 共 %d 笔%s"
                         % (len(trades), trades_total,
                            "（已截断，只保留最近 %d 笔）" % TRADES_LIMIT if trades_truncated else ""))
            rows = [
                [row.get("date") or "", row.get("code") or "", _label(_SIDE_LABELS, row.get("side")),
                 _num(row.get("price"), 3), row.get("qty") or 0, _money(row.get("amount")),
                 _money(row.get("fee"))]
                for row in trades[:TRADE_ROWS_LIMIT]
            ]
            lines.append(_table(("日期", "代码", "方向", "价格", "数量", "金额(元)", "费用(元)"), rows))
            if len(trades) > TRADE_ROWS_LIMIT:
                lines.append("（文本只展示最近 %d 笔，结构化字段 trades 内共 %d 笔）"
                             % (TRADE_ROWS_LIMIT, len(trades)))
        else:
            lines.append("成交流水：区间内没有逐笔成交记录。")

    if warnings:
        lines.append("")
        lines.append("提示 / 警告（%d 条）：" % len(warnings))
        for item in warnings[:20]:
            lines.append("  - %s" % item)
        if len(warnings) > 20:
            lines.append("  …（共 %d 条，其余见结构化字段 warnings）" % len(warnings))

    lines.append("")
    lines.append("生成时间：%s｜数据来源：quantstudio 回测引擎（真实历史日线，前复权；同参数结果走进程内缓存）"
                 % generated_at)
    return "\n".join(lines), structured


def _backtest_cache_info(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    info = ctx.jsonable(ctx.call(ctx.backtest.cache_info))
    if not isinstance(info, dict):
        raise ToolError("缓存服务返回了意外的结果类型：%s" % type(info).__name__, code="SERVICE")
    keys = [str(item) for item in (info.get("keys") or [])]
    lines = [
        "回测结果缓存（进程内 LRU；只读查询）",
        "已缓存：%s / %s 条" % (info.get("size"), info.get("capacity")),
    ]
    if keys:
        lines.append("缓存键（最多显示 10 个，形如 <策略id>:<参数摘要>）：")
        for key in keys[:10]:
            lines.append("  - %s" % key)
        if len(keys) > 10:
            lines.append("  …（共 %d 个）" % len(keys))
    else:
        lines.append("当前没有缓存结果：下一次 backtest_run 会重新计算。")
    lines.append("查询时间：%s" % _now())
    return "\n".join(lines), info


def _backtest_cache_clear(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("backtest_cache_clear", args)
    tool = "backtest_cache_clear"
    info = ctx.jsonable(ctx.call(ctx.backtest.clear_cache))
    if not isinstance(info, dict):
        raise ToolError("缓存服务返回了意外的结果类型：%s" % type(info).__name__, code="SERVICE")
    count = int(info.get("size") or 0)
    ctx.record(tool, args, True, "清空回测缓存 %d 条" % count)
    lines = [
        "已清空回测结果缓存：清理 %d 条。" % count,
        "说明：只清缓存，不删除任何回测数据或用户文件；下一次 backtest_run 会重新计算（真实数据回测可能耗时）。",
        "生成时间：%s" % _now(),
    ]
    return "\n".join(lines), info


# --------------------------------------------------------------------------- 注册
def register(registry: Any) -> None:
    """把回测工具注册进 :class:`~quantstudio.mcp.registry.Registry`。"""
    @registry.tool(
        "backtest_run",
        RUN_DESCRIPTION,
        obj(
            {
                "strategy_id": p_str("策略 id（内置如 st_ma_cross；用户策略 user_xxx；可用 strategy_list 查询）",
                                     examples=["st_ma_cross"]),
                "symbols": p_list("标的代码列表，如 ['600519.SH']；省略则用策略默认标的或自选池",
                                  items={"type": "string"}),
                "start": p_str("回测起始日 YYYY-MM-DD（省略用引擎默认区间）", pattern=r"^\d{4}-\d{2}-\d{2}$",
                               examples=["2024-01-01"]),
                "end": p_str("回测结束日 YYYY-MM-DD（省略用引擎默认区间）", pattern=r"^\d{4}-\d{2}-\d{2}$",
                             examples=["2024-06-30"]),
                "initial_cash": p_num("初始资金（元），默认取系统设置", minimum=0.0),
                "benchmark": p_str("基准代码，如 000300.SH；省略用系统默认", examples=["000300.SH"]),
                "commission_rate": p_num("佣金费率（小数，如 0.0003 = 万三）", minimum=0.0, maximum=0.05),
                "slippage_bps": p_num("滑点（基点，1 bp = 万分之一）", minimum=0.0, maximum=500.0),
                "params": p_object("策略参数覆盖，如 {\"short_ma\": 10, \"long_ma\": 30}"),
                "include_series": p_bool("是否返回净值 / 回撤 / 月度序列（默认 false，只回摘要）", default=False),
                "include_trades": p_bool("是否返回逐笔成交流水（默认 false，只回摘要）", default=False),
            },
            required=("strategy_id",),
            description="执行真实历史回测；默认只回摘要，明细按需开启且会截断。",
        ),
        group="backtest",
        title="回测",
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
    def backtest_run(ctx, args):                    # noqa: ANN001 - 注册器回调
        return _backtest_run(ctx, args)

    @registry.tool(
        "backtest_cache_info",
        "查看回测结果进程内缓存（容量 / 已缓存条数 / 缓存键）。只读，不触发任何计算。",
        obj(),
        group="backtest",
        title="回测缓存信息",
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
    def backtest_cache_info(ctx, args):             # noqa: ANN001
        return _backtest_cache_info(ctx, args)

    @registry.tool(
        "backtest_cache_clear",
        "清空回测结果的进程内缓存（下次 backtest_run 重新计算）。只清缓存，**不删除任何数据文件**；不需要读缓存时也可不调用。",
        obj(),
        group="backtest",
        title="清空回测缓存",
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
    def backtest_cache_clear(ctx, args):            # noqa: ANN001
        return _backtest_cache_clear(ctx, args)


__all__ = ["register", "SERIES_LIMIT", "TRADES_LIMIT"]
