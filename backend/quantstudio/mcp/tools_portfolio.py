# -*- coding: utf-8 -*-
"""持仓账本 + 模拟盘工具组：``portfolio_*`` / ``paper_*``。

边界：

* ``portfolio_*`` 操作的是**自有持仓账本**（本地 JSON 文件，服务层 ``PortfolioService``）；
* ``paper_*`` 操作的是**本地模拟盘**（``quantstudio.trading.PaperBroker``）——永不触达真实券商、
  永不下真单，文本里每次都写明这一点；
* 写工具（``read_only=False``）第一行统一 ``ctx.write_guard(tool, args)``：只读模式下直接拒绝并记审计；
  成功后 ``ctx.record(...)`` 落审计（``<data_dir>/mcp-audit.log``）；``paper_reset`` / ``portfolio_delete_holding``
  标 ``destructive=True``。

服务层字段名与 MCP 对外参数不同（对外 ``quantity`` / ``available``，服务层 ``qty`` / ``available_qty``），
本模块负责翻译；``submit_order`` / ``upsert_holding`` 的 payload 只接受服务层字段名。
"""

import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.errors import OrderRejected, ValidationError
from ..services.common import NotFound
from .registry import ToolError, obj, p_int, p_num, p_str

#: 委托 / 成交流水的默认与上限条数
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
#: 统计「总数」时的抓取上限（服务层 orders/fills 的 limit 上限为 1000）
TOTAL_PROBE_LIMIT = 1000
#: 文本里展示的权益曲线点位数量
EQUITY_TEXT_POINTS = 5

MANUAL = "manual"
PAPER = "paper"
PAPER_NOTE = "本地模拟盘，不触达真实券商"

_SIDE_LABELS = {"buy": "买入", "sell": "卖出"}
_STATUS_LABELS = {"new": "挂单中", "filled": "已成交", "partial": "部分成交",
                  "rejected": "已拒绝", "cancelled": "已撤销"}


# --------------------------------------------------------------------------- 文本小工具
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _width(text: Any) -> int:
    """按终端显示宽度计算（CJK 记 2 列），用于对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int) -> str:
    return str(text) + " " * max(width - _width(text), 0)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
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


def _signed_money(value: Any, digits: int = 2) -> str:
    text = _money(value, digits)
    if text == "—" or text.startswith("-"):
        return text
    return "+" + text


def _label(mapping: Dict[str, str], value: Any) -> str:
    text = str(value or "").strip().lower()
    return mapping.get(text, text or "—")


# --------------------------------------------------------------------------- 服务层调用小工具
def _as_dict(ctx: Any, value: Any) -> Dict[str, Any]:
    data = ctx.jsonable(value)
    if not isinstance(data, dict):
        raise ToolError("服务层返回了意外的结果类型：%s" % type(data).__name__,
                        hint="请重试；若持续失败请查看服务端日志。", code="SERVICE")
    return data


def _write_call(ctx: Any, tool: str, args: Dict[str, Any], fn: Any, *fn_args: Any) -> Any:
    """写工具的服务层调用：把拒绝原因**原样**转成带建议的 :class:`ToolError`，并记失败审计。"""
    try:
        return fn(*fn_args)
    except ToolError:
        raise
    except OrderRejected as exc:
        ctx.record(tool, args, ok=False, detail="委托被拒绝：%s" % exc)
        raise ToolError("模拟盘委托被拒绝：%s" % exc,
                        hint="可改小数量或改价重试；下单前先用 paper_account 看可用资金、"
                             "用 portfolio_holdings / paper_orders 看持仓与在途委托。",
                        code="ORDER_REJECTED") from exc
    except NotFound as exc:
        ctx.record(tool, args, ok=False, detail=str(exc))
        raise ToolError(str(exc), hint="请先用对应的只读工具确认记录是否存在（如 portfolio_holdings / paper_orders）。",
                        code="NOT_FOUND") from exc
    except ValidationError as exc:
        ctx.record(tool, args, ok=False, detail=str(exc))
        raise ToolError("参数不合法：%s" % exc, hint="请按提示修正参数后重试。",
                        code="VALIDATION") from exc
    except Exception as exc:                        # noqa: BLE001 - 与服务层 ctx.call 保持一致
        ctx.record(tool, args, ok=False, detail="%s: %s" % (type(exc).__name__, exc))
        raise ToolError("调用服务失败：%s: %s" % (type(exc).__name__, exc),
                        hint="可先用 system_status 检查数据源与运行环境；若为行情接口限流，稍后重试。",
                        code="SERVICE") from exc


def _whole_number(value: Any, field: str, minimum: int = 1) -> int:
    """把 number 转成正整数（A 股按整股委托；服务层只接受整数 ``qty``）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError("%s 需为数字（当前 %r）" % (field, value),
                        hint="请传整数股数，例如 100。", code="INVALID_ARGS")
    if number != int(number):
        raise ToolError("%s 需为整数（当前 %s）" % (field, value),
                        hint="A 股按整股委托，请四舍五入成整数后重试。", code="INVALID_ARGS")
    result = int(number)
    if result < minimum:
        raise ToolError("%s 不能小于 %d（当前 %d）" % (field, minimum, result),
                        hint="请改大数量后重试。", code="INVALID_ARGS")
    return result


def _paper_broker_account(ctx: Any) -> Optional[Dict[str, Any]]:
    """模拟盘通道账户快照；通道不可用时返回 ``None``（不抛错）。"""
    try:
        broker = ctx.portfolio.broker
        account = broker.account()
    except Exception:                               # noqa: BLE001 - 通道不可用/未初始化
        return None
    try:
        data = ctx.jsonable(account)
    except Exception:                               # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _account_rows(account: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    return [
        ("总资产", _money(account.get("total_assets")), "元"),
        ("持仓市值", _money(account.get("market_value")), "元"),
        ("现金", _money(account.get("cash")), "元"),
        ("可用现金", _money(account.get("available_cash")), "元"),
        ("冻结资金", _money(account.get("frozen_cash")), "元"),
        ("当日盈亏", _signed_money(account.get("day_pnl")), "元"),
        ("累计盈亏", _signed_money(account.get("total_pnl")), "元"),
        ("累计收益率", _signed(account.get("total_return_pct")), "%"),
        ("持仓数量", "%d" % int(account.get("positions_count") or 0), "只"),
    ]


def _equity_parts(raw: Any) -> Tuple[List[Any], List[str]]:
    """``PortfolioService.equity_curve`` 返回 ``(points, notes)``；这里兼容 dict / list 形态。"""
    if isinstance(raw, tuple):
        points = raw[0] if raw else []
        notes = raw[1] if len(raw) > 1 else []
    elif isinstance(raw, dict):
        points = raw.get("points") or raw.get("items") or []
        notes = raw.get("notes") or []
    else:
        points = raw or []
        notes = []
    return list(points or []), [str(item) for item in (notes or [])]


def _equity_value(point: Dict[str, Any]) -> Any:
    return point.get("equity", point.get("total_assets"))


def _normalize_points(ctx: Any, points: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in points:
        row = ctx.jsonable(item) if hasattr(item, "to_dict") else item
        if not isinstance(row, dict):
            raise ToolError("权益曲线元素需为对象，收到 %s" % type(row).__name__, code="SERVICE")
        out.append(dict(row))
    return out


# --------------------------------------------------------------------------- portfolio_* 实现
def _portfolio_overview(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    account = _as_dict(ctx, ctx.call(ctx.portfolio.overview))
    mode = str(ctx.call(ctx.portfolio.mode))
    structured = dict(account)
    structured["mode"] = mode
    structured["generated_at"] = _now()
    lines = [
        "持仓账本总览（来源：本地持仓账本；当前模式：%s）" % mode,
        "",
        _table(("字段", "数值", "单位"), _account_rows(account)),
        "",
    ]
    if mode == PAPER:
        lines.append("提示：paper 模式展示模拟盘账户（本地模拟，不触达真实券商）；模拟盘详情见 paper_account。")
    else:
        lines.append("提示：manual 模式展示自有持仓账本（本地文件）；模拟盘账户见 paper_account。")
    lines.append("数据时间：%s｜生成时间：%s" % (account.get("as_of") or "—", _now()))
    return "\n".join(lines), structured


def _portfolio_holdings(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    mode = str(ctx.call(ctx.portfolio.mode))
    items = ctx.jsonable(ctx.call(ctx.portfolio.holdings))
    if not isinstance(items, list):
        items = [items]
    structured = {"mode": mode, "count": len(items), "items": items, "generated_at": _now()}
    if not items:
        text = ("当前账本没有持仓（0 只；模式：%s）。\n"
                "可用 portfolio_upsert_holding 添加，或先在模拟盘 paper_submit_order 建仓。") % mode
        return text, structured
    rows = [
        [row.get("code") or "", row.get("name") or row.get("code") or "",
         "%d" % int(row.get("qty") or 0), "%d" % int(row.get("available_qty") or 0),
         _num(row.get("cost")), _num(row.get("price")), _money(row.get("market_value")),
         _signed_money(row.get("day_pnl")), _signed_money(row.get("total_pnl")),
         _signed(row.get("return_pct"))]
        for row in items
    ]
    lines = [
        "持仓明细（%d 只；来源：本地持仓账本；模式：%s）" % (len(items), mode),
        _table(("代码", "名称", "数量", "可用", "成本", "现价", "市值(元)", "当日盈亏", "累计盈亏", "收益率%"), rows),
    ]
    return "\n".join(lines), structured


def _portfolio_equity(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    days = int(args.get("days") or 90)
    points, notes = _equity_parts(ctx.call(ctx.portfolio.equity_curve, days))
    points = _normalize_points(ctx, points)
    start = points[0] if points else None
    end = points[-1] if points else None
    structured = {
        "days": days,
        "count": len(points),
        "points": points,
        "notes": notes,
        "start": start,
        "end": end,
        "generated_at": _now(),
    }
    if not points:
        text = ("最近 %d 天内没有权益记录（0 个点）。\n"
                "说明：权益曲线来自本地账本的每日快照，账本变化后才会产生新点。\n"
                "查询时间：%s") % (days, _now())
        return text, structured
    first_value = _equity_value(start) or 0.0
    last_value = _equity_value(end) or 0.0
    change_pct = None
    try:
        if float(first_value):
            change_pct = (float(last_value) / float(first_value) - 1.0) * 100.0
    except (TypeError, ValueError):
        change_pct = None
    lines = [
        "权益曲线（最近 %d 天，共 %d 个点；来源：本地账本）" % (days, len(points)),
        "区间：%s %s 元 → %s %s 元（区间变动 %s%%）"
        % (start.get("date") or "?", _money(first_value), end.get("date") or "?",
           _money(last_value), _signed(change_pct)),
        "最近 %d 个点：" % min(EQUITY_TEXT_POINTS, len(points)),
    ]
    for point in points[-EQUITY_TEXT_POINTS:]:
        lines.append("  %s  %s 元" % (point.get("date") or "?", _money(_equity_value(point))))
    lines.append("（结构化字段 points 返回全量 %d 个点）" % len(points))
    for note in notes[:5]:
        lines.append("说明：%s" % note)
    lines.append("生成时间：%s" % _now())
    return "\n".join(lines), structured


def _portfolio_upsert_holding(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("portfolio_upsert_holding", args)
    tool = "portfolio_upsert_holding"
    quantity = _whole_number(args["quantity"], "quantity")
    payload: Dict[str, Any] = {"code": args["code"], "qty": quantity, "cost": args["cost"]}
    if args.get("available") is not None:
        payload["available_qty"] = _whole_number(args["available"], "available", minimum=0)
    note = str(args.get("note") or "").strip()
    if note:
        payload["note"] = note                       # 服务层当前不落盘，仅进审计日志
    result = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.upsert_holding, payload))
    ctx.record(tool, args, True, "写入持仓 %s × %d 股" % (result.get("code"), quantity))
    lines = [
        "已写入持仓：%s %s" % (result.get("code"), result.get("name") or ""),
        "数量：%d 股（可用 %d 股）｜成本：%s 元｜现价：%s 元｜市值：%s 元"
        % (int(result.get("qty") or 0), int(result.get("available_qty") or 0),
           _num(result.get("cost")), _num(result.get("price")), _money(result.get("market_value"))),
    ]
    if note:
        lines.append("备注：%s（服务层暂不把备注写入账本，仅记录在 MCP 审计日志）" % note)
    lines.append("说明：按代码 upsert；自有持仓账本（manual），不是模拟盘委托。")
    lines.append("生成时间：%s" % _now())
    return "\n".join(lines), result


def _portfolio_delete_holding(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("portfolio_delete_holding", args)
    tool = "portfolio_delete_holding"
    result = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.delete_holding, args["code"]))
    ctx.record(tool, args, True, "删除持仓 %s" % result.get("code"))
    text = ("已删除持仓：%s（**不可撤销**）。\n说明：只删除本地自有持仓账本里的一条记录，不影响模拟盘。\n"
            "生成时间：%s") % (result.get("code"), _now())
    return text, result


def _portfolio_set_cash(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("portfolio_set_cash", args)
    tool = "portfolio_set_cash"
    amount = float(args["amount"])
    result = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.set_cash, amount))
    mode = str(ctx.call(ctx.portfolio.mode))
    ctx.record(tool, args, True, "自有账本现金设为 %s" % amount)
    lines = [
        "自有账本现金已设为 %s 元。" % _money(amount),
        "回显账户（模式 %s）：总资产 %s 元｜持仓市值 %s 元｜现金 %s 元"
        % (mode, _money(result.get("total_assets")), _money(result.get("market_value")),
           _money(result.get("cash"))),
    ]
    if mode == PAPER:
        lines.append("注意：当前模式为 paper，回显的是模拟盘账户；自有账本现金已单独更新，"
                     "切回 manual（portfolio_set_mode）后即可看到。")
    lines.append("生成时间：%s" % _now())
    return "\n".join(lines), result


def _portfolio_set_mode(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("portfolio_set_mode", args)
    tool = "portfolio_set_mode"
    mode = str(args["mode"]).strip().lower()
    result = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.set_mode, mode))
    ctx.record(tool, args, True, "账本模式切换为 %s" % result.get("mode"))
    text = ("账本模式已切换为 %s。\n"
            "manual = 自有持仓账本（本地文件）；paper = 模拟盘账户（本地模拟，不触达真实券商）。\n"
            "之后 portfolio_overview / portfolio_holdings / portfolio_equity 按新模式取数。\n"
            "生成时间：%s") % (result.get("mode"), _now())
    return text, result


# --------------------------------------------------------------------------- paper_* 实现
def _paper_account(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    overview = _as_dict(ctx, ctx.call(ctx.portfolio.overview))
    mode = str(ctx.call(ctx.portfolio.mode))
    broker_account = _paper_broker_account(ctx)
    structured = dict(overview)
    structured["mode"] = mode
    structured["paper_account"] = broker_account
    structured["generated_at"] = _now()
    lines = ["模拟盘账户（%s）" % PAPER_NOTE, "", "当前账本模式：%s" % mode]
    if broker_account is not None:
        lines += ["", "模拟盘通道账户（paper broker 快照）：",
                  _table(("字段", "数值", "单位"), _account_rows(broker_account))]
    else:
        lines.append("未能读取模拟盘通道账户（通道不可用或尚未初始化），以下为账本总览。")
    lines += ["", "账户总览（ctx.portfolio.overview()，按当前模式取数）：",
              _table(("字段", "数值", "单位"), _account_rows(overview))]
    if mode == PAPER:
        lines.append("提示：账本模式为 paper，账户总览与模拟盘通道一致。")
    else:
        lines.append("提示：账本模式为 manual，账户总览展示的是自有持仓账本；"
                     "模拟盘数据以「模拟盘通道账户」为准，或先 portfolio_set_mode paper。")
    lines.append("数据时间：%s｜生成时间：%s" % (overview.get("as_of") or "—", _now()))
    return "\n".join(lines), structured


def _paper_orders(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    limit = int(args.get("limit") or DEFAULT_LIMIT)
    orders = ctx.jsonable(ctx.call(ctx.portfolio.orders, limit))
    if not isinstance(orders, list):
        orders = [orders]
    total = len(orders)
    if len(orders) >= limit:                          # 可能还有更早的记录：探一次上限
        try:
            more = ctx.jsonable(ctx.call(ctx.portfolio.orders, TOTAL_PROBE_LIMIT))
            if isinstance(more, list):
                total = max(total, len(more))
        except ToolError:
            total = len(orders)
    structured = {"limit": limit, "shown": len(orders), "total": total,
                  "orders": orders, "generated_at": _now()}
    lines = ["模拟盘委托流水（%s，最新在前）" % PAPER_NOTE]
    if not orders:
        lines.append("没有委托记录（0 条）。")
        return "\n".join(lines), structured
    rows = [
        [row.get("updated_at") or row.get("created_at") or "", row.get("order_id") or "",
         row.get("code") or "", _label(_SIDE_LABELS, row.get("side")), "%d" % int(row.get("qty") or 0),
         _num(row.get("price")) if row.get("price") is not None else "市价",
         "%d" % int(row.get("filled_qty") or 0), _label(_STATUS_LABELS, row.get("status")),
         _money(row.get("fee"))]
        for row in orders
    ]
    lines.append(_table(("时间", "委托号", "代码", "方向", "数量", "价格", "已成交", "状态", "费用(元)"), rows))
    if total > len(orders):
        lines.append("已显示最近 %d 条，共 %d 条记录；需要更早的记录请调大 limit（上限 %d）。"
                     % (len(orders), total, MAX_LIMIT))
    else:
        lines.append("共 %d 条记录，已全部显示。" % len(orders))
    lines.append("生成时间：%s" % _now())
    return "\n".join(lines), structured


def _paper_fills(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    limit = int(args.get("limit") or DEFAULT_LIMIT)
    fills = ctx.jsonable(ctx.call(ctx.portfolio.fills, limit))
    if not isinstance(fills, list):
        fills = [fills]
    total = len(fills)
    if len(fills) >= limit:
        try:
            more = ctx.jsonable(ctx.call(ctx.portfolio.fills, TOTAL_PROBE_LIMIT))
            if isinstance(more, list):
                total = max(total, len(more))
        except ToolError:
            total = len(fills)
    structured = {"limit": limit, "shown": len(fills), "total": total,
                  "fills": fills, "generated_at": _now()}
    lines = ["模拟盘成交流水（%s，最新在前）" % PAPER_NOTE]
    if not fills:
        lines.append("没有成交记录（0 条）。")
        return "\n".join(lines), structured
    rows = [
        [row.get("ts") or "", row.get("order_id") or "", row.get("code") or "",
         _label(_SIDE_LABELS, row.get("side")), _num(row.get("price"), 3),
         "%d" % int(row.get("qty") or 0), _money(row.get("amount")), _money(row.get("fee"))]
        for row in fills
    ]
    lines.append(_table(("时间", "委托号", "代码", "方向", "价格", "数量", "金额(元)", "费用(元)"), rows))
    if total > len(fills):
        lines.append("已显示最近 %d 条，共 %d 条记录；需要更早的记录请调大 limit（上限 %d）。"
                     % (len(fills), total, MAX_LIMIT))
    else:
        lines.append("共 %d 条记录，已全部显示。" % len(fills))
    lines.append("生成时间：%s" % _now())
    return "\n".join(lines), structured


def _paper_submit_order(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("paper_submit_order", args)
    tool = "paper_submit_order"
    quantity = _whole_number(args["quantity"], "quantity")
    payload: Dict[str, Any] = {
        "code": args["code"],
        "side": str(args["side"]).strip().lower(),
        "qty": quantity,
        "reason": "mcp",
    }
    if args.get("price") is not None:
        payload["price"] = args["price"]
    result = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.submit_order, payload))
    order = result.get("order") if isinstance(result.get("order"), dict) else {}
    fill = result.get("fill") if isinstance(result.get("fill"), dict) else None
    broker_account = _paper_broker_account(ctx)
    account = broker_account
    account_label = "模拟盘现金"
    if account is None:
        try:
            account = _as_dict(ctx, ctx.call(ctx.portfolio.overview))
            account_label = "账户现金（当前账本模式）"
        except ToolError:
            account = {}
    ctx.record(tool, args, True, "模拟盘委托 %s 状态 %s" % (order.get("order_id"), order.get("status")))
    lines = [
        "模拟盘委托回执（%s）" % PAPER_NOTE,
        "委托号：%s｜状态：%s（%s）"
        % (order.get("order_id") or "?", _label(_STATUS_LABELS, order.get("status")), order.get("status") or "?"),
        "标的：%s %s｜方向：%s｜数量：%d 股"
        % (order.get("code") or payload["code"], order.get("name") or "",
           _label(_SIDE_LABELS, order.get("side")), int(order.get("qty") or quantity)),
    ]
    if fill:
        lines.append("成交：%d 股 @ %s 元，成交额 %s 元，费用 %s 元"
                     % (int(fill.get("qty") or 0), _num(fill.get("price"), 3),
                        _money(fill.get("amount")), _money(fill.get("fee"))))
    elif order.get("status") in ("new", "partial"):
        lines.append("尚未成交：委托已挂出（%s），可用 paper_orders 查看、paper_cancel_order 撤销。"
                     % _label(_STATUS_LABELS, order.get("status")))
    if account:
        lines.append("%s：%s 元（可用 %s 元）"
                     % (account_label, _money(account.get("cash")), _money(account.get("available_cash"))))
    if order.get("reason"):
        lines.append("备注：%s" % order.get("reason"))
    lines.append("生成时间：%s" % _now())
    structured = {"order": order, "fill": fill, "account": account or None, "generated_at": _now()}
    return "\n".join(lines), structured


def _paper_cancel_order(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("paper_cancel_order", args)
    tool = "paper_cancel_order"
    order = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.cancel_order, args["order_id"]))
    ctx.record(tool, args, True, "撤销模拟盘委托 %s（%s）" % (order.get("order_id"), order.get("status")))
    if str(order.get("status") or "") == "cancelled":
        head = "已撤销模拟盘委托：%s（状态：cancelled）" % order.get("order_id")
    else:
        head = ("委托 %s 当前状态为 %s，无需撤销（已成交 / 已是终态）。"
                % (order.get("order_id"), _label(_STATUS_LABELS, order.get("status"))))
    lines = [
        head,
        "标的：%s｜方向：%s｜数量：%d 股｜价格：%s"
        % (order.get("code") or "?", _label(_SIDE_LABELS, order.get("side")),
           int(order.get("qty") or 0),
           _num(order.get("price")) if order.get("price") is not None else "市价"),
        "注：仅本地模拟盘（%s）。" % PAPER_NOTE,
        "生成时间：%s" % _now(),
    ]
    return "\n".join(lines), order


def _paper_reset(ctx: Any, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ctx.write_guard("paper_reset", args)
    tool = "paper_reset"
    initial_cash = args.get("initial_cash")
    account = _as_dict(ctx, _write_call(ctx, tool, args, ctx.portfolio.reset, initial_cash))
    ctx.record(tool, args, True, "清空本地模拟盘账本（initial_cash=%s）" % initial_cash)
    structured = dict(account)
    structured["initial_cash"] = None if initial_cash is None else float(initial_cash)
    structured["generated_at"] = _now()
    lines = [
        "已重置模拟盘：**清空本地模拟盘账本**（持仓、委托、成交、现金流全部删除，**不可撤销**）。",
        "初始资金：%s 元｜当前现金：%s 元｜总资产：%s 元"
        % (_money(initial_cash) if initial_cash is not None else "沿用默认",
           _money(account.get("cash")), _money(account.get("total_assets"))),
        "注：只影响本地模拟盘（%s）；自有持仓账本不受影响。" % PAPER_NOTE,
        "生成时间：%s" % _now(),
    ]
    return "\n".join(lines), structured


# --------------------------------------------------------------------------- 注册
def register(registry: Any) -> None:
    """把持仓账本 + 模拟盘工具注册进 :class:`~quantstudio.mcp.registry.Registry`。"""
    @registry.tool(
        "portfolio_overview",
        "查看账本总览（总资产 / 市值 / 现金 / 当日盈亏 / 累计盈亏 / 收益率 / 持仓数 / 当前模式）。"
        "只读；manual=自有持仓账本，paper=模拟盘账户。",
        obj(),
        group="portfolio", title="持仓总览", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_overview(ctx, args):              # noqa: ANN001
        return _portfolio_overview(ctx, args)

    @registry.tool(
        "portfolio_holdings",
        "列出账本全部持仓（代码 / 名称 / 数量 / 可用 / 成本 / 现价 / 市值 / 当日与累计盈亏）。"
        "只读；空账本返回空列表，不报错。",
        obj(),
        group="portfolio", title="持仓明细", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_holdings(ctx, args):              # noqa: ANN001
        return _portfolio_holdings(ctx, args)

    @registry.tool(
        "portfolio_equity",
        "读取权益曲线（最近 N 天的每日总资产，默认 90 天，5-1000）。"
        "文本只给起止与最近 5 个点，结构化字段 points 返回全量。只读。",
        obj({"days": p_int("返回最近多少天（5-1000，默认 90）", default=90, minimum=5, maximum=1000)}),
        group="portfolio", title="权益曲线", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_equity(ctx, args):                # noqa: ANN001
        return _portfolio_equity(ctx, args)

    @registry.tool(
        "portfolio_upsert_holding",
        "新增 / 更新一条自有持仓（按代码 upsert）。写操作：只改本地持仓账本，不产生任何真实或模拟委托。"
        "服务层字段为 qty/available_qty，本工具对外用 quantity/available；note 目前仅记录在审计日志"
        "（账本服务暂不保存备注）。",
        obj(
            {
                "code": p_str("标的代码，如 600519.SH（也接受 600519）", examples=["600519.SH"]),
                "quantity": p_num("持仓数量（股，正整数）", minimum=1.0),
                "cost": p_num("摊薄成本价（元，> 0）", minimum=0.0),
                "available": p_int("可卖数量（默认等于持仓数量）", minimum=0),
                "note": p_str("备注（仅记入 MCP 审计日志）"),
            },
            required=("code", "quantity", "cost"),
            description="写入自有持仓账本；按代码 upsert。",
        ),
        group="portfolio", title="写入持仓", read_only=False, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_upsert_holding(ctx, args):        # noqa: ANN001
        return _portfolio_upsert_holding(ctx, args)

    @registry.tool(
        "portfolio_delete_holding",
        "删除一条自有持仓（按代码）。**破坏性写操作，不可撤销**；只删本地账本记录，不影响模拟盘。",
        obj({"code": p_str("要删除的标的代码，如 600519.SH")}, required=("code",)),
        group="portfolio", title="删除持仓", read_only=False, destructive=True,
        idempotent=True, open_world=False,
    )
    def portfolio_delete_holding(ctx, args):        # noqa: ANN001
        return _portfolio_delete_holding(ctx, args)

    @registry.tool(
        "portfolio_set_cash",
        "设置自有账本现金（≥ 0 元）。写操作：只改本地持仓账本；paper 模式下回显的是模拟盘账户（会提示）。",
        obj({"amount": p_num("现金余额（元，≥ 0）", minimum=0.0)},
            required=("amount",), description="设置自有账本现金。"),
        group="portfolio", title="设置现金", read_only=False, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_set_cash(ctx, args):              # noqa: ANN001
        return _portfolio_set_cash(ctx, args)

    @registry.tool(
        "portfolio_set_mode",
        "切换账本模式：manual=自有持仓账本（本地文件）/ paper=模拟盘账户。写操作：只改本地账本配置。",
        obj({"mode": p_str("账本模式", enum=["manual", "paper"])},
            required=("mode",), description="切换账本取数模式。"),
        group="portfolio", title="切换账本模式", read_only=False, destructive=False,
        idempotent=True, open_world=False,
    )
    def portfolio_set_mode(ctx, args):              # noqa: ANN001
        return _portfolio_set_mode(ctx, args)

    @registry.tool(
        "paper_account",
        "模拟盘账户汇总（总资产 / 市值 / 现金 / 盈亏 / 模式）。%s。只读。" % PAPER_NOTE,
        obj(),
        group="portfolio", title="模拟盘账户", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def paper_account(ctx, args):                   # noqa: ANN001
        return _paper_account(ctx, args)

    @registry.tool(
        "paper_orders",
        "模拟盘委托流水（最新在前，默认 20 条，1-200）。%s。只读。" % PAPER_NOTE,
        obj({"limit": p_int("返回条数（1-200，默认 20）", default=DEFAULT_LIMIT,
                            minimum=1, maximum=MAX_LIMIT)}),
        group="portfolio", title="模拟盘委托", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def paper_orders(ctx, args):                    # noqa: ANN001
        return _paper_orders(ctx, args)

    @registry.tool(
        "paper_fills",
        "模拟盘成交流水（最新在前，默认 20 条，1-200）。%s。只读。" % PAPER_NOTE,
        obj({"limit": p_int("返回条数（1-200，默认 20）", default=DEFAULT_LIMIT,
                            minimum=1, maximum=MAX_LIMIT)}),
        group="portfolio", title="模拟盘成交", read_only=True, destructive=False,
        idempotent=True, open_world=False,
    )
    def paper_fills(ctx, args):                     # noqa: ANN001
        return _paper_fills(ctx, args)

    @registry.tool(
        "paper_submit_order",
        "模拟盘下单（缺省价按现价成交；只进本地模拟盘，**永不触达真实券商**）。写操作，返回委托回执"
        "（委托号 / 状态 / 成交价 / 费用 / 剩余现金）。可能因资金不足、T+1 可卖不足、涨跌停、非整手被拒绝，"
        "此时返回失败原因与下一步建议（改数量 / 先查 paper_account）。",
        obj(
            {
                "code": p_str("标的代码，如 600519.SH", examples=["600519.SH"]),
                "side": p_str("委托方向", enum=["buy", "sell"]),
                "quantity": p_num("委托数量（股，正整数）", minimum=1.0),
                "price": p_num("限价（元，> 0）；省略 = 按现价市价委托", minimum=0.0),
            },
            required=("code", "side", "quantity"),
            description="提交本地模拟盘委托（不触达真实券商）。",
        ),
        group="portfolio", title="模拟盘下单", read_only=False, destructive=False,
        idempotent=False, open_world=True,
    )
    def paper_submit_order(ctx, args):              # noqa: ANN001
        return _paper_submit_order(ctx, args)

    @registry.tool(
        "paper_cancel_order",
        "撤销模拟盘挂单（仅 new / partial 可撤；已成交或终态委托会原样返回并说明）。%s。写操作。" % PAPER_NOTE,
        obj({"order_id": p_str("委托号，如 O12（来自 paper_orders / paper_submit_order）")},
            required=("order_id",)),
        group="portfolio", title="模拟盘撤单", read_only=False, destructive=False,
        idempotent=True, open_world=False,
    )
    def paper_cancel_order(ctx, args):              # noqa: ANN001
        return _paper_cancel_order(ctx, args)

    @registry.tool(
        "paper_reset",
        "**清空本地模拟盘账本**（持仓、委托、成交、现金流全部删除）并可选重设初始资金；**不可撤销**。"
        "只影响本地模拟盘（%s），自有持仓账本不受影响。" % PAPER_NOTE,
        obj({"initial_cash": p_num("重置后的初始资金（元，> 0）；省略沿用当前默认", minimum=0.0)}),
        group="portfolio", title="重置模拟盘", read_only=False, destructive=True,
        idempotent=True, open_world=False,
    )
    def paper_reset(ctx, args):                     # noqa: ANN001
        return _paper_reset(ctx, args)


__all__ = ["register", "DEFAULT_LIMIT", "MAX_LIMIT"]
