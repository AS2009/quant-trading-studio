# -*- coding: utf-8 -*-
"""行情 / 自选池 / 盘口（Level2）接口（``/api/market/*``、``/api/watchlist``、``/api/level2/*``）。"""

from flask import Blueprint, current_app, jsonify, request

from ..core.errors import ValidationError
from ..services.common import envelope, normalize_codes, parse_adjust, parse_freq, parse_int
from ..services.level2_service import DEFAULT_FLOW_LIMIT, DEFAULT_TICKS_LIMIT, MAX_TICKS
from .errors import json_body

bp = Blueprint("market", __name__, url_prefix="/api")


def _services():
    return current_app.config["SERVICES"]


def _envelope(data):
    services = _services()
    return jsonify(
        envelope(data, settings=services.settings, meta=services.market.meta())
    )


def _level2_envelope(data):
    """Level2 接口信封：meta 直接用服务层产物（含命中缓存的注明）。"""
    services = _services()
    meta = dict(data.get("meta") or {}) if isinstance(data, dict) else None
    return jsonify(
        envelope(data, settings=services.settings, meta=meta or services.level2.meta())
    )


@bp.route("/market/overview")
def market_overview():
    return _envelope(_services().market.overview())


@bp.route("/market/sectors")
def market_sectors():
    services = _services()
    limit = parse_int(
        request.args.get("limit"),
        "limit",
        default=services.settings.sector_limit,
        minimum=1,
        maximum=200,
        clamp=True,
    )
    return _envelope(services.market.sectors(limit))


@bp.route("/market/quotes")
def market_quotes():
    services = _services()
    raw = request.args.get("codes")
    codes = normalize_codes(raw) if raw and raw.strip() else None
    return _envelope(services.market.quotes(codes))


@bp.route("/market/kline")
def market_kline():
    services = _services()
    code = request.args.get("code")
    if not code or not str(code).strip():
        raise ValidationError("缺少参数 code（示例：600519.SH）", field="code")
    days = parse_int(
        request.args.get("days"),
        "days",
        default=services.settings.default_kline_days,
        minimum=1,
        maximum=services.settings.max_kline_days,
        clamp=True,
    )
    freq = parse_freq(request.args.get("freq"), "day")
    adjust = parse_adjust(request.args.get("adjust"), "qfq")
    return _envelope(services.market.kline(code, days=days, freq=freq, adjust=adjust))


@bp.route("/watchlist")
def watchlist():
    return _envelope(_services().market.watchlist_quotes())


@bp.route("/watchlist", methods=["POST"])
def watchlist_add():
    payload = json_body()
    code = payload.get("code")
    if code is None or not str(code).strip():
        raise ValidationError("缺少字段 code", field="code")
    codes = _services().market.add_to_watchlist(code)
    return _envelope({"codes": codes}), 201


@bp.route("/watchlist/<code>", methods=["DELETE"])
def watchlist_remove(code):
    codes = _services().market.remove_from_watchlist(code)
    return _envelope({"codes": codes})


@bp.route("/level2/<code>/orderbook")
def level2_orderbook(code):
    return _level2_envelope(_services().level2.orderbook(code))


@bp.route("/level2/<code>/ticks")
def level2_ticks(code):
    services = _services()
    limit = parse_int(
        request.args.get("limit"),
        "limit",
        default=DEFAULT_TICKS_LIMIT,
        minimum=1,
        maximum=MAX_TICKS,
        clamp=True,
    )
    return _level2_envelope(services.level2.ticks(code, limit=limit))


@bp.route("/level2/<code>/flow")
def level2_capital_flow(code):
    services = _services()
    limit = parse_int(
        request.args.get("limit"),
        "limit",
        default=DEFAULT_FLOW_LIMIT,
        minimum=1,
        maximum=MAX_TICKS,
        clamp=True,
    )
    return _level2_envelope(services.level2.capital_flow(code, limit=limit))
