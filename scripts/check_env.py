#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检：数据源连通性、交易日历、缓存目录、可选依赖、模拟盘账户状态。

用法：
    python scripts/check_env.py                 # 完整自检（会访问数据源）
    QUANTSTUDIO_OFFLINE=1 python scripts/check_env.py   # 只检查本地环境
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio import __version__, compat  # noqa: E402
from quantstudio.config import get_settings  # noqa: E402


def line(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def main():
    settings = get_settings()
    line("QuantTrading Studio 环境自检  v%s" % __version__)
    print("Python      :", sys.version.split()[0])
    print("计算后端    :", compat.backend_info())
    print("数据目录    :", settings.data_dir)
    print("缓存目录    :", settings.cache_dir)
    print("数据源配置  :", settings.data_source, "| 离线模式:", settings.offline)
    print("默认基准    :", settings.benchmark, "| 初始资金:", "%.0f" % settings.initial_cash)

    line("1) 交易日历")
    from quantstudio.core.calendar import TradingCalendar

    cal = TradingCalendar()
    print("最近交易日  :", cal.last_trading_day())
    print("当前是否收盘:", cal.is_closed())
    print("节假日表    :", "已加载" if os.path.exists(settings.path("holidays.json")) else "未配置（仅按周末判定，可放 data/holidays.json）")

    line("2) 数据源")
    from quantstudio.data import get_provider

    provider = get_provider()
    print("Provider    :", provider.name)
    if hasattr(provider, "describe"):
        print("可用源      :", provider.describe())
    print("健康检查    :", provider.health())
    if settings.offline:
        print("离线模式：跳过真实行情请求")
        return 0

    try:
        quotes = provider.latest_quotes(["600519.SH", "000001.SH"])
        for q in quotes:
            print("  %-12s %-10s %10.2f  %+6.2f%%  来源=%s" % (q.code, q.name, q.price, q.change_pct, q.source))
    except Exception as exc:  # noqa: BLE001
        print("  实时快照失败:", exc)
    try:
        bars = provider.kline("600519.SH", days=5)
        print("  K 线末 3 根:")
        for b in bars[-3:]:
            print("    %s  开%.2f 高%.2f 低%.2f 收%.2f 量(万手)%.2f" % (b.date, b.open, b.high, b.low, b.close, b.volume_wan))
    except Exception as exc:  # noqa: BLE001
        print("  K 线失败:", exc)
    try:
        sectors = provider.sectors(3)
        print("  板块前 3:", [("%s %+.2f%%" % (s.name, s.change_pct)) for s in sectors])
    except Exception as exc:  # noqa: BLE001
        print("  板块失败:", exc)
    try:
        breadth = provider.breadth()
        print("  市场广度: 上涨 %s / 下跌 %s / 平盘 %s | 两市成交 %.0f 亿" % (
            breadth.up, breadth.down, breadth.flat, breadth.total_amount_yi))
    except Exception as exc:  # noqa: BLE001
        print("  广度失败:", exc)

    line("3) 策略与回测")
    from quantstudio.strategies import list_specs

    specs = list_specs()
    print("可用策略    :", len(specs))
    for spec in specs:
        print("  %-14s %-16s %s" % (spec.id, spec.name, spec.category))

    line("4) 交易通道")
    from quantstudio.trading import available_brokers

    for name, info in available_brokers().items():
        print("  %-6s %s" % (name, info))

    line("5) 本地数据文件")
    for label, path in (
        ("自选池    ", settings.watchlist_path),
        ("持仓账户  ", settings.portfolio_path),
        ("用户策略  ", settings.user_strategies_path),
        ("模拟盘账户", settings.account_path),
        ("CSV 目录  ", settings.csv_dir),
    ):
        print("  %s %s %s" % (label, "存在 " if os.path.exists(path) else "未创建", path))

    print("\n自检完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
