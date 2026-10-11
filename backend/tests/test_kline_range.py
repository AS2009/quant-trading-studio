# -*- coding: utf-8 -*-
"""K 线**日期区间取数**与回测区间规划（修复「回测到过去 = 窗口落空」的回归测试）。

背景（修复前的真 bug）
----------------------
``DataProvider.kline()`` 过去只接受 ``days``（返回「截至今天」的最近 N 根），引擎按
``区间交易日数 + 预热`` 估一个 N 再**事后按日期过滤**。于是只要 ``end`` 是过去的日期：

- 取的 N 根结束于今天，过滤后窗口尾部全落在未来 → **起点被静默吃掉**
  （实测 2023-01-01 ~ 2025-06-30 实际只回测 2023-10-27 起）；
- 小区间更糟：过滤后一根不剩 → 直接 ``InsufficientData``
  （实测 2024-01-01 ~ 2024-06-30 报「没有足够的共同交易日（0 天）」）。

修复要点：``kline()`` 增加 ``start`` / ``end``；``end`` 非空时实现**必须**返回「截至 end」
（而不是今天）的窗口。腾讯走服务端日期区间，其余源客户端裁剪。本文件把这些行为钉死。
"""

import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from typing import List, Optional

from quantstudio.backtest.engine import BacktestEngine
from quantstudio.config import Settings
from quantstudio.core.models import BacktestRequest, Bar
from quantstudio.data.base import clean_day, trim_bars
from quantstudio.data.csv_provider import CsvProvider
from quantstudio.data.sample import SampleProvider


def make_bars(days: List[str]) -> List[Bar]:
    """按给定日期造一串单调上涨的 bar（价格不重要，只关心窗口）。"""
    out: List[Bar] = []
    for index, day in enumerate(days):
        close = 10.0 + index
        out.append(Bar(date=day, open=close, high=close + 1, low=close - 1, close=close,
                       volume_wan=100.0, amount_yi=1.0, change_pct=0.0, turnover_pct=1.0))
    return out


def weekday_series(start: str, count: int) -> List[str]:
    """从 ``start`` 起连续 ``count`` 个工作日（不含节假日，够测试用）。"""
    anchor = date.fromisoformat(start)
    days: List[str] = []
    while len(days) < count:
        if anchor.weekday() < 5:
            days.append(anchor.isoformat())
        anchor += timedelta(days=1)
    return days


class TestCleanDay(unittest.TestCase):
    """``clean_day``：脏值一律当「未指定」，绝不抛异常。"""

    def test_both_notations(self):
        self.assertEqual(clean_day("2024-01-02"), "2024-01-02")
        self.assertEqual(clean_day("20240102"), "2024-01-02")

    def test_blank_and_bad_values(self):
        for value in (None, "", "   ", "abc", "2024-13-45", "--", "0"):
            self.assertEqual(clean_day(value), "", "脏值 %r 应视为未指定" % (value,))

    def test_whitespace_and_extra_text(self):
        self.assertEqual(clean_day(" 2024-01-02 "), "2024-01-02")
        self.assertEqual(clean_day("2024-01-02 15:00:00"), "2024-01-02")


class TestTrimBars(unittest.TestCase):
    """``trim_bars``：闭区间裁剪 + 尾部限量。"""

    def setUp(self):
        self.bars = make_bars(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])

    def test_no_window_keeps_tail(self):
        out = trim_bars(self.bars, "", "", 2)
        self.assertEqual([bar.date for bar in out], ["2024-01-03", "2024-01-04"])

    def test_closed_interval(self):
        out = trim_bars(self.bars, "2024-01-02", "2024-01-03", 0)
        self.assertEqual([bar.date for bar in out], ["2024-01-02", "2024-01-03"])

    def test_start_only_and_end_only(self):
        self.assertEqual([b.date for b in trim_bars(self.bars, "2024-01-03", "", 0)],
                         ["2024-01-03", "2024-01-04"])
        self.assertEqual([b.date for b in trim_bars(self.bars, "", "2024-01-02", 0)],
                         ["2024-01-01", "2024-01-02"])

    def test_window_outside_returns_empty(self):
        self.assertEqual(trim_bars(self.bars, "2030-01-01", "2030-12-31", 0), [])


class TestKlineRangeInterface(unittest.TestCase):
    """provider.kline 的区间语义（只给 days 时行为必须保持不变）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-kline-range-")
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        self.sample = SampleProvider(self.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_days_unchanged(self):
        """不给区间时仍是「最近 N 根」（向后兼容）。"""
        bars = self.sample.kline("600519.SH", days=120)
        self.assertEqual(len(bars), 120)
        self.assertEqual(bars, self.sample.kline("600519.SH", days=120, start=None, end=None))

    def test_end_returns_window_ending_at_end(self):
        """核心契约：``end`` 非空时返回**截至 end**（而不是今天）的窗口。"""
        bars = self.sample.kline("600519.SH", days=5000, start="2024-03-01", end="2024-06-30")
        self.assertTrue(bars, "该区间应有数据")
        self.assertGreaterEqual(bars[0].date, "2024-03-01")
        self.assertLessEqual(bars[-1].date, "2024-06-30",
                             "末根必须 <= end，不能取到今天的数据")

    def test_reversed_range_is_normalized(self):
        forward = self.sample.kline("600519.SH", days=5000, start="2024-03-01", end="2024-06-30")
        reversed_ = self.sample.kline("600519.SH", days=5000, start="2024-06-30", end="2024-03-01")
        self.assertEqual([b.date for b in forward], [b.date for b in reversed_])

    def test_window_with_no_trading_day_is_empty_or_raises(self):
        """周末区间：要么空列表、要么 DataSourceError，不能返回窗口外的数据。"""
        try:
            bars = self.sample.kline("600519.SH", days=5000,
                                     start="2024-01-06", end="2024-01-07")
        except Exception as exc:                      # noqa: BLE001 - 允许抛「区间内无数据」
            self.assertIn("K 线", str(exc))
            return
        for bar in bars:
            self.assertGreaterEqual(bar.date, "2024-01-06")
            self.assertLessEqual(bar.date, "2024-01-07")


class TestCsvProviderRange(unittest.TestCase):
    """CSV 源也要支持区间（本地文件没有服务端区间，靠客户端裁剪）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-csv-range-")
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        # 造一份可控 CSV：2024-01-01 起 40 个工作日
        kline_dir = os.path.join(self.tmp, "csv", "kline")
        os.makedirs(kline_dir, exist_ok=True)
        days = weekday_series("2024-01-01", 40)
        with open(os.path.join(kline_dir, "600519.SH.csv"), "w", encoding="utf-8") as handle:
            handle.write("date,open,high,low,close,volume,amount\n")
            for index, day in enumerate(days):
                close = 100.0 + index
                handle.write("%s,%.2f,%.2f,%.2f,%.2f,%d,%d\n"
                             % (day, close, close + 1, close - 1, close, 1000, 100000))
        self.days = days
        self.csv = CsvProvider(self.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_csv_range_is_trimmed(self):
        bars = self.csv.kline("600519.SH", days=1000, freq="day", adjust="qfq",
                              start=self.days[10], end=self.days[20])
        self.assertEqual([b.date for b in bars], self.days[10:21])

    def test_csv_plain_days_unchanged(self):
        self.assertEqual(len(self.csv.kline("600519.SH", days=15)), 15)
        self.assertEqual(self.csv.kline("600519.SH", days=15),
                         self.csv.kline("600519.SH", days=15, start=None, end=None))


class RecordingProvider:
    """记录每次 kline 调用参数；声明 start/end 说明它支持区间取数。"""

    name = "recording"

    def __init__(self, days: List[str]):
        self.days = days
        self.calls: List[dict] = []

    def kline(self, symbol, days=250, freq="day", adjust="qfq", start=None, end=None):
        self.calls.append({"symbol": symbol, "days": days, "freq": freq,
                           "adjust": adjust, "start": start, "end": end})
        bars = make_bars(self.days)
        if start or end:
            bars = [bar for bar in bars
                    if (not start or bar.date >= start) and (not end or bar.date <= end)]
        return bars[-int(days):] if days and int(days) > 0 else bars

    def latest_quotes(self, symbols):
        return []

    def index_quotes(self, symbols):
        return []

    def sectors(self, limit=20):
        return []

    def breadth(self):
        return None

    def resolve_name(self, symbol):
        return symbol

    def health(self):
        return {"ok": True, "detail": "recording", "latency_ms": 0}


class LegacyProvider(RecordingProvider):
    """按**旧协议**实现（不接受 start/end 的测试桩）——引擎必须优雅退回旧行为。"""

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        self.calls.append({"symbol": symbol, "days": days, "freq": freq,
                           "adjust": adjust, "start": None, "end": None})
        bars = make_bars(self.days)
        return bars[-int(days):] if days and int(days) > 0 else bars


class TestEngineRangePlanning(unittest.TestCase):
    """引擎必须把真实区间传给 provider，而不是「取最近 N 根再过滤」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-engine-range-")
        # 覆盖 2018-01-01 ~ 2024-12-31 的连续工作日，足够做久远区间测试
        self.days = weekday_series("2018-01-01", 1800)
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"),
                                 max_kline_days=5000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, start, end=""):
        return BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"],
                               start=start, end=end, initial_cash=1_000_000.0,
                               benchmark="")

    def _engine(self, provider):
        return BacktestEngine(provider, settings=self.settings)

    def test_fetch_days_contract_unchanged(self):
        """``_fetch_days`` 保持 3 元组契约（既有调用方与测试依赖它）。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        span, days, capped = engine._fetch_days(self._request("2023-01-01", "2023-12-29"),
                                                warmup=60)
        self.assertGreater(span, 200, "2023 全年应有 200+ 个交易日")
        self.assertGreaterEqual(days, span + 60)
        self.assertFalse(capped)

    def test_plan_reports_window_and_warmup(self):
        """``_fetch_plan`` 的窗口起点要早于请求起点（给指标预热留量）。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        span, days, _capped, window_start, window_end = engine._fetch_plan(
            self._request("2023-01-01", "2023-12-29"), warmup=60)
        self.assertEqual(window_end, "2023-12-29", "显式 end 必须回传为窗口终点")
        self.assertTrue(window_start, "有 start 时必须给出窗口起点")
        self.assertLess(window_start, "2023-01-01", "窗口起点要早于请求起点（预热带宽）")
        self.assertGreater(span, 200)

    def test_plan_omits_window_end_when_end_empty(self):
        """``end`` 留空 = 「截至今天」，窗口终点为空（保持旧行为）。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        span, _days, _capped, window_start, window_end = engine._fetch_plan(
            self._request("2023-01-01", ""), warmup=60)
        self.assertEqual(window_end, "", "end 留空时不传窗口终点")
        self.assertTrue(window_start)
        self.assertGreater(span, 200)

    def test_engine_passes_real_window_to_supporting_provider(self):
        """支持区间的 provider：收到的应是**真实请求区间**（含预热带宽），而非「最近 N 根」。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        engine.run(self._request("2023-01-01", "2023-12-29"), _RangeProbeStrategy())
        self.assertTrue(provider.calls, "应至少取数一次")
        call = provider.calls[0]
        self.assertEqual(call["end"], "2023-12-29", "end 必须原样传给 provider")
        self.assertTrue(call["start"], "start 必须传给 provider")
        self.assertLess(call["start"], "2023-01-01", "起点要留预热带宽")
        self.assertGreaterEqual(call["days"], 200, "days 仍作为根数上限")

    def test_engine_falls_back_for_legacy_provider(self):
        """旧协议 provider（不接受 start/end）：不能报错，要退回旧行为并正常回测。"""
        provider = LegacyProvider(self.days)
        engine = self._engine(provider)
        result = engine.run(self._request("2023-01-01", "2023-12-29"), _RangeProbeStrategy())
        self.assertIsNotNone(result)
        self.assertFalse(engine._supports_window(), "探测结果应缓存为「不支持」")
        self.assertTrue(all(call["start"] is None for call in provider.calls))

    def test_past_window_actually_covered(self):
        """核心回归：过去区间的**起点不被吃掉**（修复前起点会被静默丢弃）。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        result = engine.run(self._request("2023-01-01", "2023-12-29"), _RangeProbeStrategy())
        self.assertEqual(result.range["start"][:10], "2023-01-02",
                         "区间起点应是 2023 年首个交易日，而不是被后移")
        self.assertEqual(result.range["end"][:10], "2023-12-29")

    def test_narrow_past_window_is_not_empty(self):
        """核心回归：过去的小区间（修复前直接 InsufficientData）现在能跑。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        result = engine.run(self._request("2023-03-01", "2023-04-28"), _RangeProbeStrategy())
        self.assertGreater(result.metrics.trading_days, 20)
        self.assertGreaterEqual(result.range["start"][:10], "2023-03-01")
        self.assertLessEqual(result.range["end"][:10], "2023-04-28")

    def test_warmup_bars_available_for_indicators(self):
        """预热带宽要真的到位：区间首日 ctx.history 必须已能取满 60 根。"""
        provider = RecordingProvider(self.days)
        engine = self._engine(provider)
        strategy = _RangeProbeStrategy()
        engine.run(self._request("2023-06-01", "2023-08-31"), strategy)
        self.assertTrue(strategy.history_lengths, "策略应记录到 history 长度")
        self.assertGreaterEqual(min(strategy.history_lengths), 60,
                                "区间首日的 history 不足 60 根 → 预热没留够")


class _RangeProbeStrategy:
    """最小策略桩：只记录 ctx.history 长度，不交易。"""

    id = "probe_test"
    name = "区间探针"
    category = "自定义"
    desc = "测试用"
    min_bars = 61
    version = "test"
    default_params: dict = {}

    def __init__(self):
        self.spec = None
        self.symbols: List[str] = []
        self.params: dict = {}
        self.history_lengths: List[int] = []
        from quantstudio.core.models import StrategySpec
        self.spec = StrategySpec(id=self.id, name=self.name, category=self.category,
                                 desc=self.desc, min_bars=self.min_bars, version=self.version)

    def set_symbols(self, codes):
        self.symbols = list(codes)

    def set_params(self, params):
        self.params = dict(params or {})

    def param_schema(self):
        return {}

    def validate_params(self, params):
        return dict(params or {})

    def on_start(self, ctx):
        pass

    def on_bar(self, ctx, bars):
        code = self.symbols[0] if self.symbols else ""
        self.history_lengths.append(len(ctx.history(code, "close", 60)))
        return []

    def on_finish(self, ctx):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
