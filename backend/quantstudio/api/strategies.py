# -*- coding: utf-8 -*-
"""策略管理接口（``/api/strategies``）。"""

from flask import Blueprint, current_app, jsonify, request

from ..core.models import DataMeta
from ..services.common import envelope, now_str
from .errors import json_body

bp = Blueprint("strategies", __name__, url_prefix="/api/strategies")


def _services():
    return current_app.config["SERVICES"]


def _envelope(data):
    services = _services()
    # 策略配置与行情无关：不回带行情 meta，避免前端误显示"数据来源=sample/离线"
    meta = DataMeta(source="config", as_of=now_str())
    return jsonify(envelope(data, settings=services.settings, meta=meta))


@bp.route("", methods=["GET", "POST"])
def strategies():
    services = _services()
    if request.method == "POST":
        created = services.strategies.create_strategy(json_body())
        return _envelope(created), 201
    return _envelope(services.strategies.list_strategies())


@bp.route("/<strategy_id>", methods=["GET", "DELETE"])
def strategy_detail(strategy_id):
    services = _services()
    if request.method == "DELETE":
        return _envelope(services.strategies.delete_strategy(strategy_id))
    return _envelope(services.strategies.get_strategy(strategy_id))
