# -*- coding: utf-8 -*-
"""核心层测试：模型序列化、交易日历、配置解析。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio import compat  # noqa: E402
from quantstudio.config import CacheTTL, Settings  # noqa: E402
from quantstudio.core.calendar import TradingCalendar, fmt, parse_date  # noqa: E402
from quantstudio.core.errors import ValidationError  # noqa: E402
from quantstudio.core.models import (  # noqa: E402
    Account,
    Bar,
    BacktestMetrics,
    BacktestRequest,
    BacktestResult,
    DataMeta,
    FeeConfig,
    Order,
    OrderRequest,
    Position,
    Quote,
    StrategySpec,
    Trade,
)


class TestModels(unittest.TestCase):
    def test_json_serializable_and_no_nan(self):
        payload = {
            "quote": Quote(code="600519.SH", name="贵州茅台", price=1252.57, change_pct=-0.36).to_dict(),
            "bar": Bar(date="2026-09-21", open=1259.0, high=1259.95, low=1250.8, close=1252.57).to_dict(),
            "position": Position(code="600519.SH", qty=100, cost=1500.0, price=1252.57).to_dict(),
            "account": Account(total_assets=125257.0, cash=0.0).to_dict(),
            "order": Order(order_id="o1", code="600519.SH").to_dict(),
            "req": OrderRequest(code="600519.SH", side="buy", qty=100).to_dict(),
            "trade": Trade(date="2026-09-21", code="600519.SH", name="贵州茅台", side="buy", price=1252.57, qty=100).to_dict(),
            "spec": StrategySpec(id="st_ma_cross", name="双均线趋势策略").to_dict(),
            "meta": DataMeta(source="eastmoney", as_of="2026-09-21 15:00").to_dict(),
            "metrics": BacktestMetrics(total_return_pct=12.3).to_dict(),
        }
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("贵州茅台", text)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)

    def test_backtest_result_contract(self):
        spec = StrategySpec(id="st_ma_cross", name="双均线趋势策略")
        req = BacktestRequest(strategy_id="st_ma_cross", symbols=["600519.SH"])
        result = BacktestResult(strategy=spec, request=req)
        result.nav = [{"date": "2023-01-03", "strategy": 1.0, "benchmark": 1.0}]
        result.trades = [Trade(date="2023-02-01", code="600519.SH", name="贵州茅台",
                               side="sell", price=1800.0, qty=100, pnl=5000.0, ret_pct=2.86)]
        data = result.to_dict()
        for key in ("strategy", "range", "nav", "drawdown", "monthly", "metrics",
                    "trades", "trade_total", "request"):
            self.assertIn(key, data)
        self.assertEqual(data["trade_total"], 0)  # metrics.trade_count 默认为 0
        json.dumps(data, ensure_ascii=False)

    def test_fee_defaults(self):
        fee = FeeConfig()
        self.assertGreater(fee.commission_rate, 0)
        self.assertGreater(fee.stamp_duty_rate, 0)
        self.assertEqual(fee.lot_size, 100)
        json.dumps(fee.to_dict(), allow_nan=False)


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.cal = TradingCalendar()

    def test_weekend_excluded(self):
        self.assertFalse(self.cal.is_trading_day("2026-09-19"))  # 周六
        self.assertFalse(self.cal.is_trading_day("2026-09-20"))  # 周日
        self.assertTrue(self.cal.is_trading_day("2026-09-21"))   # 周一

    def test_days_and_shift(self):
        days = self.cal.trading_days("2026-09-14", "2026-09-21")
        self.assertEqual(days, ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"])
        self.assertEqual(self.cal.shift("2026-09-21", -1), "2026-09-18")
        self.assertEqual(self.cal.shift("2026-09-18", 1), "2026-09-21")

    def test_holidays_from_file(self):
        cal = TradingCalendar(holidays=["2026-09-21"])
        self.assertFalse(cal.is_trading_day("2026-09-21"))
        self.assertEqual(cal.last_trading_day("2026-09-21"), "2026-09-18")

    def test_parse_formats(self):
        self.assertEqual(fmt("2026/9/21"), "2026-09-21")
        self.assertEqual(parse_date("2026-09-21").day, 21)
        with self.assertRaises(ValueError):
            parse_date("not-a-date")


class TestConfig(unittest.TestCase):
    def test_env_override(self):
        os.environ["QUANTSTUDIO_DATA_SOURCE"] = "csv"
        os.environ["QUANTSTUDIO_OFFLINE"] = "1"
        try:
            s = Settings()
            self.assertEqual(s.data_source, "csv")
            self.assertTrue(s.offline)
            self.assertTrue(os.path.isdir(s.cache_dir))
            self.assertTrue(s.watchlist_path.endswith("watchlist.json"))
            self.assertEqual(len(s.index_symbols), 4)
            self.assertGreaterEqual(len(s.default_watchlist), 5)
        finally:
            os.environ.pop("QUANTSTUDIO_DATA_SOURCE", None)
            os.environ.pop("QUANTSTUDIO_OFFLINE", None)

    def test_invalid_source_rejected(self):
        os.environ["QUANTSTUDIO_DATA_SOURCE"] = "nope"
        try:
            with self.assertRaises(Exception):
                Settings()
        finally:
            os.environ.pop("QUANTSTUDIO_DATA_SOURCE", None)

    def test_cache_ttl_and_backend_info(self):
        ttl = CacheTTL().to_dict()
        self.assertIn("quotes", ttl)
        info = compat.backend_info()
        self.assertIn(info["mode"], ("stdlib", "pandas+numpy"))

    def test_validation_error_carries_field(self):
        err = ValidationError("days 需为整数", field="days")
        self.assertEqual(err.field, "days")


if __name__ == "__main__":
    unittest.main(verbosity=2)
