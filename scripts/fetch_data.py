#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据预热与导出：把真实行情拉到本地缓存 / 导出 CSV 供离线使用。

用法：
    python scripts/fetch_data.py --warm --days 800          # 预热自选池 K 线与快照
    python scripts/fetch_data.py --export-csv               # 导出到 backend/data/csv/
    python scripts/fetch_data.py --status                   # 查看缓存统计
"""

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from quantstudio.config import get_settings  # noqa: E402
from quantstudio.data import get_provider  # noqa: E402


def read_watchlist(settings):
    if os.path.exists(settings.watchlist_path):
        try:
            with open(settings.watchlist_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            codes = raw.get("codes") if isinstance(raw, dict) else raw
            if isinstance(codes, list) and codes:
                return [str(c) for c in codes]
        except (OSError, ValueError):
            pass
    return list(settings.default_watchlist)


def main():
    parser = argparse.ArgumentParser(description="行情数据预热 / 导出")
    parser.add_argument("--symbols", default="", help="标的，逗号分隔（默认自选池）")
    parser.add_argument("--days", type=int, default=250, help="K 线天数")
    parser.add_argument("--warm", action="store_true", help="预热缓存（K线 + 快照 + 板块 + 广度）")
    parser.add_argument("--export-csv", action="store_true", help="导出 CSV 到 data/csv/")
    parser.add_argument("--status", action="store_true", help="打印缓存与数据源状态")
    args = parser.parse_args()

    settings = get_settings()
    provider = get_provider()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or read_watchlist(settings)
    symbols = list(dict.fromkeys(symbols + list(settings.index_symbols)))

    if args.status or not (args.warm or args.export_csv):
        print("数据源      :", provider.name)
        if hasattr(provider, "describe"):
            print("可用源      :", provider.describe())
        print("缓存目录    :", settings.cache_dir)
        print("自选池      :", ", ".join(read_watchlist(settings)))
        print("健康检查    :", provider.health())
        if not (args.warm or args.export_csv):
            print("\n提示：加上 --warm 预热缓存，或 --export-csv 导出离线数据。")
            return 0

    if args.warm or args.export_csv:
        out_dir = settings.csv_dir
        if args.export_csv:
            os.makedirs(os.path.join(out_dir, "kline"), exist_ok=True)
        ok, failed = 0, []
        for code in symbols:
            try:
                bars = provider.kline(code, days=args.days)
            except Exception as exc:  # noqa: BLE001
                failed.append((code, str(exc)))
                continue
            ok += 1
            if args.export_csv:
                path = os.path.join(out_dir, "kline", "%s.csv" % code.replace("/", "_"))
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["date", "open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_pct"])
                    for b in bars:
                        writer.writerow([b.date, b.open, b.high, b.low, b.close,
                                         round(b.volume_wan * 1e4), round(b.amount_yi * 1e8),
                                         b.change_pct, b.turnover_pct])
            print("  %-12s %4d 根  最新 %s 收 %.2f" % (code, len(bars), bars[-1].date if bars else "-", bars[-1].close if bars else 0))
        print("\n完成：成功 %d 个，失败 %d 个" % (ok, len(failed)))
        for code, err in failed:
            print("  失败 %s: %s" % (code, err))
        if args.export_csv:
            print("CSV 已导出到:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
