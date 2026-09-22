# -*- coding: utf-8 -*-
"""回测接口（``/api/backtest/<strategy_id>``，GET query / POST JSON 等价）。"""

import json

from flask import Blueprint, current_app, jsonify, request

from ..core.errors import ValidationError
from ..services.common import envelope
from .errors import json_body

bp = Blueprint("backtest", __name__, url_prefix="/api/backtest")


def _services():
    return current_app.config["SERVICES"]


def _query_params():
    params = {}
    for key in request.args:
        params[key] = request.args.get(key)
    raw = params.get("params")
    if raw not in (None, ""):
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ValidationError("params 需为 JSON 对象字符串", field="params")
        if not isinstance(parsed, dict):
            raise ValidationError("params 需为 JSON 对象", field="params")
        params["params"] = parsed
    return params


@bp.route("/<strategy_id>", methods=["GET", "POST"])
def backtest(strategy_id):
    services = _services()
    params = json_body() if request.method == "POST" else _query_params()
    result = services.backtest.run(strategy_id, params)
    return jsonify(envelope(result, settings=services.settings, meta=services.meta()))
