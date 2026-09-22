# -*- coding: utf-8 -*-
"""Flask 应用工厂：注册业务蓝图 + 托管前端静态资源 + 统一 JSON 错误。

``static_folder=None``：前端目录用绝对路径托管，因此在任意工作目录启动都可用。
应用工厂额外支持 ``services`` / ``provider`` 注入替身，便于测试与排查。
"""

import os
from typing import Optional

from flask import Flask, send_from_directory

from ..config import PROJECT_DIR, Settings, get_settings
from ..services import Services, get_services

FRONTEND_DIR = os.path.normpath(os.path.join(PROJECT_DIR, "frontend"))


def create_app(
    settings: Optional[Settings] = None,
    services: Optional[Services] = None,
    provider=None,
) -> Flask:
    settings = settings or get_settings()
    if services is None:
        if provider is None:
            services = get_services(settings)
        else:
            services = Services(settings=settings, provider=provider)

    app = Flask(__name__, static_folder=None)
    app.config["SETTINGS"] = settings
    app.config["SERVICES"] = services
    app.config["JSON_AS_ASCII"] = False
    try:  # Flask >= 2.2 使用 app.json 提供器
        app.json.ensure_ascii = False
    except Exception:  # pragma: no cover - 兼容更老版本
        pass

    from .backtest import bp as backtest_bp
    from .market import bp as market_bp
    from .portfolio import bp as portfolio_bp, orders_bp
    from .strategies import bp as strategies_bp
    from .system import bp as system_bp

    app.register_blueprint(market_bp)
    app.register_blueprint(strategies_bp)
    app.register_blueprint(backtest_bp)
    app.register_blueprint(portfolio_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(system_bp)

    from .errors import register_error_handlers

    register_error_handlers(app)

    @app.route("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory(FRONTEND_DIR, path)

    return app
