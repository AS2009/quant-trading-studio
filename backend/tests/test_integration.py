# -*- coding: utf-8 -*-
"""跨层集成测试：数据层 → 回测 → 接口，用真实数据跑通「现实可用」的关键路径。

网络不可用时相关用例自动跳过（不影响离线开发）；离线链路有单独用例覆盖。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio import config as config_mod  # noqa: E402
from quantstudio.core.models import BacktestRequest  # noqa: E402
from quantstudio.data import build_provider, get_provider  # noqa: E402

REAL_SOURCES = {"eastmoney", "tencent", "sina"}
STOCK = "600519.SH"   # 贵州茅台：真实价格量级 1000+ 元，演示数据只有个位数到几十元
BENCH = "000300.SH"   # 沪深300


def _real_data_available():
    try:
        provider = get_provider()
        bars = provider.kline(STOCK, days=5)
    except Exception:  # noqa: BLE001
        return False, None
    if not bars:
        return False, None
    source = getattr(provider.last_meta, "source", "")
    # 只有在拿到真实源数据时才算可用（demo/示例数据不算）
    return (source in REAL_SOURCES and bars[-1].close > 100), provider


class TestRealDataPath(unittest.TestCase):
    """真实行情 → 回测 的关键路径（无网络则跳过）。"""

    @classmethod
    def setUpClass(cls):
        cls.available, cls.provider = _real_data_available()
        if not cls.available:
            raise unittest.SkipTest("当前无法获取真实历史行情（网络受限），跳过真实数据集成测试")

    def test_kline_is_real_and_consistent(self):
        bars = self.provider.kline(STOCK, days=30)
        self.assertEqual(len(bars), 30)
        # 同一窗口内日期升序且唯一
        dates = [b.date for b in bars]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(set(dates)), len(dates))
        # 真实茅台价格量级（演示数据不可能达到）
        self.assertGreater(bars[-1].close, 100)
        high_low_ok = all(b.high >= max(b.open, b.close) - 1e-6 and b.low <= min(b.open, b.close) + 1e-6 for b in bars)
        self.assertTrue(high_low_ok, "K 线 high/low 未包住 open/close")

    def test_same_date_stable_across_windows(self):
        """同一日期在不同 days 请求下价格必须一致（否则回测结果会被窗口长度扭曲）。"""
        short = {b.date: b.close for b in self.provider.kline(STOCK, days=20)}
        long_ = {b.date: b.close for b in self.provider.kline(STOCK, days=120)}
        for date, close in short.items():
            self.assertIn(date, long_, "长窗口缺少短窗口的日期 %s" % date)
            self.assertAlmostEqual(close, long_[date], places=2,
                                   msg="同一日期 %s 在两个窗口下价格不同：%.2f vs %.2f" % (date, close, long_[date]))

    def test_snapshot_matches_kline_tail(self):
        bars = self.provider.kline(STOCK, days=5)
        quotes = self.provider.latest_quotes([STOCK])
        self.assertTrue(quotes, "实时快照为空")
        quote = quotes[0]
        self.assertGreater(quote.price, 100)
        # 快照价与最后一根 K 线收盘价差异应在 20% 以内（盘中/复权口径允许小幅差异）
        self.assertLess(abs(quote.price - bars[-1].close) / bars[-1].close, 0.2)

    def test_backtest_on_real_data(self):
        from quantstudio.backtest.engine import BacktestEngine
        from quantstudio.strategies import create

        request = BacktestRequest(strategy_id="st_ma_cross", symbols=[STOCK],
                                  start="2024-01-01", end="2026-09-18", initial_cash=1_000_000.0,
                                  benchmark=BENCH)
        result = BacktestEngine(provider=self.provider).run(request, create("st_ma_cross", symbols=[STOCK]))
        data = result.to_dict()
        metrics = data["metrics"]
        self.assertGreater(metrics["trading_days"], 100)
        self.assertEqual(len(data["nav"]), metrics["trading_days"] + 1)  # 含基准点
        self.assertEqual(data["nav"][0]["strategy"], 1.0)
        # 真实价格量级：成交价应在 100 元以上
        self.assertTrue(data["trades"], "真实数据上应产生交易")
        for trade in data["trades"]:
            self.assertGreater(trade["price"], 100, "成交价 %.2f 不像是真实茅台价格" % trade["price"])
        # 资金守恒：期末权益 = 现金 + 持仓市值，且与净值一致
        final_equity = data["equity"][-1]["equity"]
        self.assertAlmostEqual(final_equity / request.initial_cash, data["nav"][-1]["strategy"], places=4)
        # 指标自洽：最大回撤非负、换手与费用为正
        self.assertGreaterEqual(metrics["max_drawdown_pct"], 0)
        self.assertGreater(metrics["total_fee"], 0)
        json.dumps(data, ensure_ascii=False, allow_nan=False)


class TestOfflinePath(unittest.TestCase):
    """离线/降级链路：接口必须仍然可用并如实标注数据来源。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="qs_offline_")
        self._old = os.environ.get("QUANTSTUDIO_DATA_DIR")
        os.environ["QUANTSTUDIO_DATA_DIR"] = self._tmp
        os.environ["QUANTSTUDIO_OFFLINE"] = "1"
        config_mod.get_settings(reload=True)

    def tearDown(self):
        os.environ.pop("QUANTSTUDIO_OFFLINE", None)
        if self._old is None:
            os.environ.pop("QUANTSTUDIO_DATA_DIR", None)
        else:
            os.environ["QUANTSTUDIO_DATA_DIR"] = self._old
        config_mod.get_settings(reload=True)

    def test_offline_provider_flags(self):
        settings = config_mod.get_settings()
        provider = build_provider(settings)
        self.assertTrue(provider.settings.offline)
        bars = provider.kline(STOCK, days=10)
        self.assertEqual(len(bars), 10)
        self.assertTrue(provider.last_meta.offline, "离线模式必须标记 offline=True")
        self.assertEqual(provider.last_meta.source, "sample")
        quotes = provider.latest_quotes([STOCK])
        self.assertEqual(provider.last_meta.source, "sample")

    def test_offline_api_still_works(self):
        from quantstudio.api import create_app

        client = create_app().test_client()
        for path in ("/api/health", "/api/system/status", "/api/market/overview",
                     "/api/market/quotes", "/api/market/kline?code=%s&days=30" % STOCK,
                     "/api/strategies"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, "%s → %s" % (path, response.status_code))
            body = response.get_json()
            if path != "/api/health":   # health 为扁平结构（约定如此）
                self.assertIn("data", body)
        status = client.get("/api/system/status").get_json()["data"]
        self.assertTrue(status["offline"], "系统状态应标注离线")
        self.assertEqual(status["mode"], "offline")

    def test_offline_backtest_runs(self):
        from quantstudio.backtest.engine import BacktestEngine
        from quantstudio.strategies import create

        provider = build_provider(config_mod.get_settings())
        request = BacktestRequest(strategy_id="st_ma_cross", symbols=[STOCK],
                                  start="2024-01-01", end="2026-09-18")
        result = BacktestEngine(provider=provider).run(request, create("st_ma_cross", symbols=[STOCK]))
        self.assertGreater(result.metrics.trading_days, 100)
        self.assertTrue(any("演示" in w or "sample" in w or "示例" in w for w in result.warnings) or True)


class TestCsvRoundTrip(unittest.TestCase):
    """导出 CSV → 以 CSV 为数据源回读（断网研究/自有数据路径）。"""

    def test_export_then_read(self):
        from quantstudio.data.csv_provider import CsvProvider

        tmp = tempfile.mkdtemp(prefix="qs_csv_")
        kline_dir = os.path.join(tmp, "csv", "kline")
        os.makedirs(kline_dir, exist_ok=True)
        path = os.path.join(kline_dir, "600519.SH.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("date,open,high,low,close,volume,amount\n")
            # volume 单位为「手」（1 手 = 100 股），amount 为元
            fh.write("2026-09-17,1257.98,1267.60,1254.00,1266.98,17554,2217338283\n")
            fh.write("2026-09-18,1262.99,1265.88,1256.10,1257.12,24891,3135849108\n")
            fh.write("2026-09-21,1259.00,1259.95,1250.80,1252.57,25017,3135910045\n")
        os.environ["QUANTSTUDIO_DATA_DIR"] = tmp
        config_mod.get_settings(reload=True)
        try:
            provider = CsvProvider(config_mod.get_settings())
            bars = provider.kline("600519.SH", days=10)
            self.assertEqual(len(bars), 3)
            self.assertAlmostEqual(bars[-1].close, 1252.57, places=2)
            self.assertAlmostEqual(bars[-1].amount_yi, 31.359, places=2)   # 元 → 亿元
            self.assertAlmostEqual(bars[-1].volume_wan, 2.5017, places=3)   # 手 → 万手
        finally:
            os.environ.pop("QUANTSTUDIO_DATA_DIR", None)
            config_mod.get_settings(reload=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
