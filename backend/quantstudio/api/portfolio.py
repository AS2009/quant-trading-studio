# -*- coding: utf-8 -*-
"""持仓 / 模拟盘交易接口。

- ``/api/portfolio/*``：账户总览、持仓、权益曲线、增删改、现金、模式
- ``/api/orders``、``/api/fills``：模拟盘委托与成交（默认只做本地记账，不发真实委托）
"""

from flask import Blueprint, current_app, jsonify, request

from ..core.errors import ValidationError
from ..services.common import envelope, parse_int
from .errors import json_body

bp = Blueprint("portfolio", __name__, url_prefix="/api/portfolio")
orders_bp = Blueprint("orders", __name__, url_prefix="/api")


def _services():
    return current_app.config["SERVICES"]


def _envelope(data):
    services = _services()
    return jsonify(envelope(data, settings=services.settings, meta=services.meta()))


# --------------------------------------------------------------------------- 账户 / 持仓


@bp.route("/overview")
def overview():
    return _envelope(_services().portfolio.overview())


@bp.route("/holdings", methods=["GET", "POST"])
def holdings():
    services = _services()
    if request.method == "POST":
        position = services.portfolio.upsert_holding(json_body())
        return _envelope(position), 201
    return _envelope(services.portfolio.holdings())


@bp.route("/holdings/<code>", methods=["DELETE"])
def holding_delete(code):
    return _envelope(_services().portfolio.delete_holding(code))


@bp.route("/equity")
def equity():
    services = _services()
    days = parse_int(
        request.args.get("days"), "days", default=90, minimum=1, maximum=2000, clamp=True
    )
    points, notes = services.portfolio.equity_curve(days)
    # 旧前端把 data 当数组用：额外说明放 meta.notes（避免修改 provider 的 DataMeta）
    meta = services.meta().to_dict()
    meta["notes"] = list(meta.get("notes") or []) + list(notes) + ["权益曲线为逐日估算，仅供研究参考"]
    return jsonify(envelope(points, settings=services.settings, meta=meta))


@bp.route("/cash", methods=["POST"])
def cash():
    payload = json_body()
    if "amount" not in payload:
        raise ValidationError("缺少字段 amount", field="amount")
    return _envelope(_services().portfolio.set_cash(payload.get("amount")))


@bp.route("/mode", methods=["POST"])
def mode():
    payload = json_body()
    return _envelope(_services().portfolio.set_mode(payload.get("mode")))


# --------------------------------------------------------------------------- 模拟盘交易


@orders_bp.route("/orders", methods=["GET", "POST"])
def orders():
    services = _services()
    if request.method == "POST":
        result = services.portfolio.submit_order(json_body())
        return _envelope(result), 201
    limit = parse_int(
        request.args.get("limit"), "limit", default=100, minimum=1, maximum=1000, clamp=True
    )
    return _envelope(services.portfolio.orders(limit))


@orders_bp.route("/orders/<order_id>", methods=["DELETE"])
def order_cancel(order_id):
    return _envelope(_services().portfolio.cancel_order(order_id))


@orders_bp.route("/orders/reset", methods=["POST"])
def orders_reset():
    services = _services()
    payload = {}
    if (request.get_data(cache=True) or b"").strip():
        payload = json_body()
    return _envelope(services.portfolio.reset(payload.get("initial_cash")))


@orders_bp.route("/fills")
def fills():
    limit = parse_int(
        request.args.get("limit"), "limit", default=100, minimum=1, maximum=1000, clamp=True
    )
    return _envelope(_services().portfolio.fills(limit))
