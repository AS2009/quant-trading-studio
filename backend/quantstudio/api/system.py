# -*- coding: utf-8 -*-
"""系统接口：``/api/health``（扁平）与 ``/api/system/status``（统一信封）。"""

from flask import Blueprint, current_app, jsonify

from .. import __version__
from ..services.common import envelope, now_str

bp = Blueprint("system", __name__, url_prefix="/api")


def _services():
    return current_app.config["SERVICES"]


@bp.route("/health")
def health():
    """健康检查：始终 200，扁平结构（不套 data 信封）。"""
    services = _services()
    provider_name = "unavailable"
    try:
        provider = services.provider
        provider_name = getattr(provider, "name", "") or "unknown"
        detail = provider.health() or {}
        ok = bool(detail.get("ok", True))
    except Exception:
        ok = False
    return jsonify(
        {
            "ok": ok,
            "version": __version__,
            "as_of": now_str(),
            "provider": provider_name,
            "mode": services.market.mode(),
        }
    )


@bp.route("/system/status")
def system_status():
    services = _services()
    return jsonify(
        envelope(
            services.market.system_status(),
            settings=services.settings,
            meta=services.market.meta(),
        )
    )
