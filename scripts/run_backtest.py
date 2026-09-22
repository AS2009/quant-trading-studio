#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行回测：把策略跑在真实历史数据上，输出指标 / 交易 / 净值文件。

用法：
    python scripts/run_backtest.py --strategy st_ma_cross --symbols 600519.SH --start 2023-01-01 --end 2026-09-18
    python scripts/run_backtest.py --strategy st_momentum --symbols 600519.SH,300750.SZ,002594.SZ \
        --out /tmp/bt.json --csv /tmp/bt_nav.csv --cash 500000 --benchmark 000300.SH
"""

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio.backtest.engine import BacktestEngine  # noqa: E402
from quantstudio.config import get_settings  # noqa: E402
from quantstudio.core.calendar import TradingCalendar  # noqa: E402
from quantstudio.core.models import BacktestRequest, FeeConfig  # noqa: E402
from quantstudio.data import get_provider  # noqa: E402
from quantstudio.strategies import create, list_specs  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="QuantTrading Studio 命令行回测")
    p.add_argument("--strategy", default="st_ma_cross", help="策略 id（--list 可查看全部）")
    p.add_argument("--symbols", default="", help="标的池，逗号分隔；留空用自选池")
    p.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（默认上一交易日）")
    p.add_argument("--cash", type=float, default=None, help="初始资金（默认取配置）")
    p.add_argument("--benchmark", default="", help="基准代码（默认取配置）")
    p.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"], help="复权方式")
    p.add_argument("--slippage-bps", type=float, default=2.0, help="滑点（基点）")
    p.add_argument("--commission-rate", type=float, default=0.00025, help="佣金费率（双边）")
    p.add_argument("--param", action="append", default=[], help="策略参数覆盖，形如 --param short_ma=10")
    p.add_argument("--out", default="", help="结果 JSON 输出路径")
    p.add_argument("--csv", default="", help="净值 CSV 输出路径")
    p.add_argument("--list", action="store_true", help="列出可用策略")
    return p.parse_args()


def main():
    args = parse_args()
    specs = list_specs()
    if args.list:
        print("可用策略：")
        for spec in specs:
            print("  %-14s %-18s %-8s min_bars=%s" % (spec.id, spec.name, spec.category, spec.min_bars))
            print("      参数:", json.dumps(spec.params, ensure_ascii=False))
        return 0

    settings = get_settings()
    params = {}
    for item in args.param:
        if "=" not in item:
            print("参数格式应为 key=value：%r" % item, file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        try:
            params[key.strip()] = json.loads(value)
        except ValueError:
            params[key.strip()] = value

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    request = BacktestRequest(
        strategy_id=args.strategy,
        symbols=symbols,
        start=args.start,
        end=args.end,
        initial_cash=args.cash if args.cash is not None else settings.initial_cash,
        benchmark=args.benchmark or settings.benchmark,
        fee=FeeConfig(commission_rate=args.commission_rate, slippage_bps=args.slippage_bps),
        params_override=params,
    )

    spec = next((s for s in specs if s.id == args.strategy), None)
    if spec is None:
        print("未知策略 %r，用 --list 查看可用策略" % args.strategy, file=sys.stderr)
        return 2
    strategy = create(args.strategy, params=params, symbols=symbols)
    engine = BacktestEngine(provider=get_provider(), calendar=TradingCalendar())
    result = engine.run(request, strategy)
    data = result.to_dict()
    metrics = data["metrics"]

    print("=" * 72)
    print("策略        : %s（%s）  参数: %s" % (spec.name, spec.id, json.dumps(data["request"]["params_override"], ensure_ascii=False) or "默认"))
    print("标的池      : %s" % (", ".join(request.symbols) or "策略默认"))
    print("区间        : %s ~ %s  共 %s 个交易日" % (data["range"].get("start"), data["range"].get("end"), metrics.get("trading_days")))
    print("初始资金    : %s 元   基准: %s" % ("{:,.0f}".format(request.initial_cash), request.benchmark))
    print("-" * 72)
    print("累计收益    : %+.2f%%   （基准 %+.2f%%，超额 %+.2f%%）" % (
        metrics["total_return_pct"], metrics["benchmark_return_pct"],
        metrics["total_return_pct"] - metrics["benchmark_return_pct"]))
    print("年化收益    : %+.2f%%   年化波动 %.2f%%" % (metrics["annual_return_pct"], metrics["volatility_pct"]))
    print("最大回撤    : %.2f%%（%s ~ %s）" % (metrics["max_drawdown_pct"], metrics["max_drawdown_start"], metrics["max_drawdown_end"]))
    print("夏普/索提诺/卡玛: %.2f / %.2f / %.2f" % (metrics["sharpe"], metrics["sortino"], metrics["calmar"]))
    print("胜率/盈亏比/交易次数: %.2f%% / %.2f / %s" % (metrics["win_rate_pct"], metrics["profit_loss_ratio"], metrics["trade_count"]))
    print("Alpha/Beta  : %+.2f%% / %.2f   总费用 %s 元   换手率 %.1f%%" % (
        metrics["alpha_pct"], metrics["beta"], "{:,.2f}".format(metrics["total_fee"]), metrics["turnover_pct"]))
    if data["warnings"]:
        print("-" * 72)
        for w in data["warnings"]:
            print("警告:", w)
    if data["trades"]:
        print("-" * 72)
        print("最近 5 笔交易：")
        for t in data["trades"][-5:]:
            pnl = "" if t["pnl"] is None else "%+.2f 元 (%+.2f%%)" % (t["pnl"], t["ret_pct"] or 0)
            print("  %s %s %-10s %s x %s @ %.2f  %s" % (
                t["date"], "买入" if t["side"] == "buy" else "卖出", t["name"], t["side"], t["qty"], t["price"], pnl))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print("\n结果已写入:", args.out)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "strategy_nav", "benchmark_nav", "drawdown_pct"])
            dd = {x["date"]: x["dd_pct"] for x in data["drawdown"]}
            for row in data["nav"]:
                writer.writerow([row["date"], row["strategy"], row["benchmark"], dd.get(row["date"], "")])
        print("净值已写入:", args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
