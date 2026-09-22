# -*- coding: utf-8 -*-
"""量化交易软件 - Flask 后端入口。

启动：``cd backend && python app.py``（默认 http://127.0.0.1:8000）
自检：``python app.py --check`` —— 初始化数据源并打印 health / 是否离线，便于排查环境。

技术栈：Flask + 原生 HTML/CSS/JS 前端 + 本地 ECharts；
业务实现位于 ``quantstudio`` 包（services 业务层 + api 接口层）。
"""

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:  # 保证在任意工作目录下启动都能导入 quantstudio
    sys.path.insert(0, BASE_DIR)

from quantstudio import __version__  # noqa: E402
from quantstudio.api import create_app  # noqa: E402
from quantstudio.compat import backend_info  # noqa: E402
from quantstudio.config import get_settings  # noqa: E402
from quantstudio.services import get_services  # noqa: E402
from quantstudio.services.common import provider_meta  # noqa: E402

# 供 `flask run` / WSGI 服务器（如 gunicorn app:app）复用的应用实例
app = create_app()


def _provider_health(services):
    """返回 (provider_name, health_dict, meta)；数据源不可用时给出可读原因。"""
    try:
        provider = services.provider
    except Exception as exc:  # 数据层缺失 / 构造失败
        return "unavailable", {"ok": False, "detail": str(exc)}, provider_meta(None)
    name = getattr(provider, "name", "") or "unknown"
    try:
        health = provider.health() or {}
    except Exception as exc:
        health = {"ok": False, "detail": str(exc)}
    return name, health, provider_meta(provider)


def run_check(settings) -> int:
    """``--check``：只做自检（初始化 provider + 打印 health / 离线标记），不启动服务。"""
    services = get_services(settings)
    name, health, meta = _provider_health(services)
    ok = bool(health.get("ok", False))
    offline = bool(settings.offline or meta.offline)

    print("QuantTrading Studio %s 环境自检" % __version__)
    print("  数据源配置 : %s" % settings.data_source)
    print("  Provider   : %s" % name)
    print("  离线模式   : %s" % ("是" if offline else "否"))
    print("  health     : %s" % (health.get("detail") or health))
    print("  计算后端   : %s" % backend_info().get("mode"))
    print("  数据目录   : %s" % settings.data_dir)
    print("  缓存目录   : %s" % settings.cache_dir)
    print("  自检结果   : %s" % ("通过" if ok else "失败（可继续使用离线/缓存数据）"))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QuantTrading Studio 后端服务")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做环境自检：初始化数据源并打印 health 与是否离线",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.check:
        return run_check(settings)

    application = create_app(settings)
    name, _health, meta = _provider_health(get_services(settings))

    shown = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    print("QuantTrading Studio %s" % __version__)
    print("数据源: %s / provider=%s（离线模式: %s，数据时点: %s）" % (
        settings.data_source,
        name,
        "是" if (settings.offline or meta.offline) else "否",
        meta.as_of or "-",
    ))
    print("服务地址: http://%s:%d" % (shown, settings.port))
    if shown != settings.host:
        print("(已监听 %s，局域网内其他设备可用本机 IP 访问)" % settings.host)
    print("按 Ctrl+C 停止服务")
    application.run(host=settings.host, port=settings.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
