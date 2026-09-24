# -*- coding: utf-8 -*-
"""同花顺（10jqka）Provider 的**离线**单元测试。

样本来源
--------
下面的常量是 2026-09-24 本机对同花顺**公开网页接口**的真实抓取（``curl`` + ``Referer:
https://stockpage.10jqka.com.cn/``），只把 ``data`` 字段裁剪到几根 / 几点、``year`` 索引省略，
其余字段原样（含 ``\\uXXXX`` 转义的中文名）；测试通过覆写 :meth:`ThsProvider._fetch` 注入，
**整个文件不发任何网络请求**（未配置的 URL 直接抛 DataSourceError）。

运行
----
::

    PYTHONPATH=backend python -m unittest tests.test_ths_provider -v
    # 仓库根目录
    python -m unittest discover -s backend/tests -t backend
"""

import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from quantstudio.config import Settings                                       # noqa: E402
from quantstudio.core.errors import (                                          # noqa: E402
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
)
from quantstudio.data.ths import ThsProvider, to_ths_code                      # noqa: E402

#: 分时接口真实响应：``hs_600519``（贵州茅台），data 裁剪为 首 3 点 + 末 3 点（真实共 268 点）
TIME_SAMPLE = r'''quotebridge_v6_time_hs_600519_last({"hs_600519": {"name": "\u8d35\u5dde\u8305\u53f0", "open": 0, "stop": 0, "isTrading": 0, "rt": "0930-1130,1300-1500,1505-1530", "tradeTime": ["0930-1130", "1300-1500", "1505-1530"], "pre": "1251.24", "date": "20260924", "data": "0930,1250.01,22911433,1250.010,18329;0931,1249.99,151158970,1249.985,120929;0932,1247.28,29827220,1249.694,23900;1528,1237.00,618500,1237.961,500.00;1529,1237.00,618500,1237.961,500.00;1530,1237.00,742200,1237.961,600.00", "dotsCount": 268, "dates": ["20260924"], "afterTradeTime": "", "marketType": "HS_stock_sh"}})'''

#: 日线 last.js 真实响应：``hs_600519``，data 裁剪为 首 3 根 + 末 3 根（真实共 140 根）
LINE_SAMPLE = r'''quotebridge_v6_line_hs_600519_01_last({"year": {"2001": 86, "2025": 243, "2026": 178}, "total": "6012", "num": 140, "rt": "0930-1130,1300-1500", "start": "20010827", "name": "\u8d35\u5dde\u8305\u53f0", "data": "20260306,1366.98,1379.48,1359.98,1373.98,2915415,4072328800.00,0.233,,,0;20260309,1361.98,1376.88,1355.18,1368.98,3744162,5220095600.00,0.299,,,0;20260310,1376.88,1381.47,1369.98,1373.86,2462592,3457808900.00,0.197,,,0;20260922,1252.15,1265.88,1248.10,1253.80,2457294,3088526100.00,0.197,,900.00,1128420;20260923,1255.03,1271.50,1250.89,1251.24,3098122,3894630800.00,0.248,,100.00,125124;20260924,1250.01,1256.13,1231.05,1237.00,3123935,3867310900.00,0.250,,600.00,742200", "marketType": "HS_stock_sh", "issuePrice": "", "today": "20260924"})'''

#: 按年明细真实响应：``hs_600519/01/2025.js``，data 裁剪为 2 根 + 年末 1 根（真实共 243 根）
LINE_2025_SAMPLE = r'''quotebridge_v6_line_hs_600519_01_2025({"data": "20250102,1444.35,1444.84,1400.35,1408.35,5002870,7490883800.00,0.398,,,0;20250103,1414.85,1415.34,1387.36,1395.35,3262836,4836610300.00,0.260,,,0;20251231,1361.98,1365.98,1349.15,1349.16,3476563,4799456500.00,0.278,,,0"})'''

#: 指数分时真实响应：``hs_399001``（深证成指），data 裁剪为 4 点（真实共 242 点）
INDEX_TIME_SAMPLE = r'''quotebridge_v6_time_hs_399001_last({"hs_399001": {"name": "\u6df1\u8bc1\u6210\u6307", "open": 0, "stop": 0, "isTrading": 0, "rt": "0930-1130,1300-1500", "tradeTime": ["0930-1130", "1300-1500"], "pre": "13636.07", "date": "20260924", "data": "0930,13575.07,7275047600,13599.2526,590391750;0931,13568.07,29614967000,13589.7074,2005950400;1459,13322.49,11650000,13477.8916,241000;1500,13316.97,10844560000,13477.8916,696379000", "dotsCount": 242, "dates": ["20260924"], "afterTradeTime": "", "marketType": "HS_stock_sz"}})'''

#: 指数日线真实响应：``hs_399001``，data 保留最后 3 根 —— 末根是 20260923，**实测指数日线滞后一天**
INDEX_LINE_SAMPLE = r'''quotebridge_v6_line_hs_399001_01_last({"year": {"1991": 230, "2025": 243, "2026": 177}, "total": "8687", "num": 140, "rt": "0930-1130,1300-1500", "start": "19910403", "name": "\u6df1\u8bc1\u6210\u6307", "data": "20260921,13716.22,13779.00,13643.95,13730.02,63170807000,1084694360000.00,2.571,,,0;20260922,13858.62,13914.21,13691.09,13723.74,64579497000,1127554800000.00,2.628,,,0;20260923,13742.55,13742.55,13617.05,13636.07,58523892000,930843550000.00,2.381,,,0", "marketType": "HS_stock_sz", "issuePrice": "", "today": "20260924"})'''

_TIME_KEY = "v6/time/hs_600519/last.js"
_LINE_KEY = "v6/line/hs_600519/01/last.js"
_INDEX_TIME_KEY = "v6/time/hs_399001/last.js"
_INDEX_LINE_KEY = "v6/line/hs_399001/01/last.js"

STOCK_PAYLOADS = {
    _TIME_KEY: TIME_SAMPLE,
    _LINE_KEY: LINE_SAMPLE,
}

INDEX_PAYLOADS = {
    _INDEX_TIME_KEY: INDEX_TIME_SAMPLE,
    _INDEX_LINE_KEY: INDEX_LINE_SAMPLE,
}


def _tmp_settings(**kwargs):
    """临时目录隔离的 Settings（绝不污染仓库 data 目录）。"""
    tmp = tempfile.mkdtemp(prefix="qts-ths-test-")
    params = dict(
        data_dir=tmp,
        cache_dir=os.path.join(tmp, "cache"),
        data_source="auto",
        http_timeout=6,
        http_retries=1,
    )
    params.update(kwargs)
    return Settings(**params), tmp


class _StubThs(ThsProvider):
    """用固定 JSONP 文本替换 HTTP：按 URL 子串匹配，未配置的 URL 抛 DataSourceError。"""

    def __init__(self, payloads, settings=None):
        ThsProvider.__init__(self, settings)
        self.payloads = dict(payloads or {})
        self.calls = []

    def _fetch(self, url, referer="", params=None, headers=None):
        self.calls.append(url)
        for key, text in self.payloads.items():
            if key in url:
                return text.encode("utf-8"), "utf-8"
        raise DataSourceError("stub 未配置该 URL：%s" % url)


class _ThsTestCase(unittest.TestCase):
    """公共脚手架：临时 Settings + 临时缓存目录。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _provider(self, payloads):
        return _StubThs(payloads, self.settings)


# --------------------------------------------------------------------------- 代码映射
class ThsCodeMappingTest(_ThsTestCase):
    """``hs_`` + 6 位数字的代码映射（任务书给的三个例子）。"""

    def test_mapping(self):
        self.assertEqual(to_ths_code("600519.SH"), "hs_600519")
        self.assertEqual(to_ths_code("000001.SZ"), "hs_000001")
        self.assertEqual(to_ths_code("399001.SZ"), "hs_399001")
        self.assertEqual(to_ths_code("sh600519"), "hs_600519")      # 各种写法都先规范化

    def test_mapping_rejects_unknown(self):
        for bad in ("", "abc", "6005190"):
            with self.assertRaises(SymbolNotFound, msg=bad):
                to_ths_code(bad)

    def test_urls_use_hs_prefix(self):
        provider = self._provider(STOCK_PAYLOADS)
        quotes = provider.latest_quotes(["600519.SH"])
        self.assertEqual(len(quotes), 1)
        self.assertEqual(provider.name, "ths")
        self.assertEqual(quotes[0].source, "ths")
        self.assertEqual(provider.calls, [
            "https://d.10jqka.com.cn/v6/time/hs_600519/last.js",
            "https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js",
        ])


# --------------------------------------------------------------------------- 快照
class ThsQuoteTest(_ThsTestCase):
    """``latest_quotes``：现价取分时末点，昨收 / 开高低 / 量额取日线（全离线）。"""

    def test_fields_from_real_samples(self):
        provider = self._provider(STOCK_PAYLOADS)
        quote = provider.latest_quotes(["600519.SH"])[0]
        self.assertEqual(quote.code, "600519.SH")
        self.assertEqual(quote.name, "贵州茅台")
        self.assertAlmostEqual(quote.price, 1237.00, places=2)        # 分时最后一个点 1530
        self.assertAlmostEqual(quote.prev_close, 1251.24, places=2)   # 分时的 pre
        self.assertAlmostEqual(quote.open, 1250.01, places=2)         # 日线 20260924 开
        self.assertAlmostEqual(quote.high, 1256.13, places=2)         # 日线 20260924 高
        self.assertAlmostEqual(quote.low, 1231.05, places=2)          # 日线 20260924 低
        self.assertAlmostEqual(quote.change, -14.24, places=2)
        self.assertAlmostEqual(quote.change_pct, -1.1381, places=3)
        self.assertAlmostEqual(quote.volume_wan, 3123935 / 100.0 / 1e4, places=6)   # 股 → 万手
        self.assertAlmostEqual(quote.amount_yi, 3867310900.0 / 1e8, places=6)
        self.assertAlmostEqual(quote.turnover_pct, 0.250, places=3)   # 日线第 8 位
        self.assertEqual(quote.ts, "2026-09-24 15:30:00")
        self.assertEqual(quote.source, "ths")
        self.assertEqual(provider.last_skipped, [])
        self.assertEqual(provider.last_notes, [])
        self.assertEqual(provider.known_name("600519.SH"), "贵州茅台")

    def test_name_unicode_escape_is_decoded(self):
        """源响应里中文名是 ``\\uXXXX`` 转义，解析后必须是真中文。"""
        self.assertIn(r"\u8d35\u5dde\u8305\u53f0", TIME_SAMPLE)
        provider = self._provider(STOCK_PAYLOADS)
        self.assertEqual(provider.latest_quotes(["600519.SH"])[0].name, "贵州茅台")

    def test_without_daily_source_keeps_minute_price(self):
        """日线不可用：现价 / 昨收仍在，开高低留 0，量额退化为分时点求和，并记笔记。"""
        provider = self._provider({_TIME_KEY: TIME_SAMPLE})
        quote = provider.latest_quotes(["600519.SH"])[0]
        self.assertAlmostEqual(quote.price, 1237.00, places=2)
        self.assertAlmostEqual(quote.prev_close, 1251.24, places=2)
        for field in ("open", "high", "low", "turnover_pct"):
            self.assertEqual(getattr(quote, field), 0.0, field)
        expected_shares = 18329 + 120929 + 23900 + 500 + 500 + 600       # 样本里的 6 个分时点
        expected_yuan = 22911433 + 151158970 + 29827220 + 618500 + 618500 + 742200
        self.assertAlmostEqual(quote.volume_wan, expected_shares / 100.0 / 1e4, places=6)
        self.assertAlmostEqual(quote.amount_yi, expected_yuan / 1e8, places=6)
        self.assertTrue(any("日线不可用" in note for note in provider.last_notes),
                        provider.last_notes)

    def test_index_daily_lag_leaves_ohlc_zero(self):
        """指数日线滞后一天：昨收取日线末根收盘，开高低留 0，量额取分时点求和。"""
        provider = self._provider(INDEX_PAYLOADS)
        quote = provider.latest_quotes(["399001.SZ"])[0]
        self.assertEqual(quote.name, "深证成指")
        self.assertAlmostEqual(quote.price, 13316.97, places=2)          # 分时 1500 点
        self.assertAlmostEqual(quote.prev_close, 13636.07, places=2)     # 分时 pre = 日线末根收盘
        self.assertEqual((quote.open, quote.high, quote.low), (0.0, 0.0, 0.0))
        expected_shares = 590391750 + 2005950400 + 241000 + 696379000
        expected_yuan = 7275047600 + 29614967000 + 11650000 + 10844560000
        self.assertAlmostEqual(quote.volume_wan, expected_shares / 100.0 / 1e4, places=4)
        self.assertAlmostEqual(quote.amount_yi, expected_yuan / 1e8, places=4)
        self.assertEqual(quote.ts, "2026-09-24 15:00:00")
        self.assertTrue(any("日线未更新到 20260924" in note for note in provider.last_notes),
                        provider.last_notes)

    def test_market_mismatch_is_skipped(self):
        """``hs_`` 命名空间会把 000001.SH 落到平安银行（sz）：市场不符时不产出该标的。"""
        payload = TIME_SAMPLE.replace("hs_600519", "hs_000001").replace("HS_stock_sh", "HS_stock_sz")
        provider = self._provider({"v6/time/hs_000001/last.js": payload})
        with self.assertRaises(DataSourceError):
            provider.latest_quotes(["000001.SH"])
        self.assertEqual(provider.last_skipped, ["000001.SH"])
        self.assertTrue(any("不符" in note for note in provider.last_notes), provider.last_notes)
        # 同一份数据按深市请求就是合法的
        self.assertAlmostEqual(provider.latest_quotes(["000001.SZ"])[0].price, 1237.00, places=2)

    def test_everything_failed_raises(self):
        provider = self._provider({})
        with self.assertRaises(DataSourceError):
            provider.latest_quotes(["600519.SH"])

    def test_invalid_payload_raises(self):
        provider = self._provider({_TIME_KEY: "var hq = 不是 JSON;"})
        with self.assertRaises(DataSourceError):
            provider.latest_quotes(["600519.SH"])

    def test_unknown_symbols_return_empty(self):
        provider = self._provider(STOCK_PAYLOADS)
        self.assertEqual(provider.latest_quotes(["???"]), [])
        self.assertEqual(provider.calls, [])


# --------------------------------------------------------------------------- 日线
class ThsKlineTest(_ThsTestCase):
    """``kline``：前复权日线（尽力而为），其它周期 / 复权方式返回 ``[]``。"""

    def test_day_qfq_bars(self):
        provider = self._provider(STOCK_PAYLOADS)
        bars = provider.kline("600519.SH", 5)
        self.assertEqual(len(bars), 5)                       # 样本 6 根 → 取最近 5 根
        self.assertEqual([bar.date for bar in bars],
                         ["2026-03-09", "2026-03-10", "2026-09-22", "2026-09-23", "2026-09-24"])
        last = bars[-1]
        self.assertEqual(last.date, "2026-09-24")
        self.assertAlmostEqual(last.open, 1250.01, places=2)
        self.assertAlmostEqual(last.high, 1256.13, places=2)
        self.assertAlmostEqual(last.low, 1231.05, places=2)
        self.assertAlmostEqual(last.close, 1237.00, places=2)
        self.assertAlmostEqual(last.volume_wan, 3123935 / 100.0 / 1e4, places=6)
        self.assertAlmostEqual(last.amount_yi, 3867310900.0 / 1e8, places=6)
        self.assertAlmostEqual(last.turnover_pct, 0.250, places=3)
        self.assertAlmostEqual(last.change_pct, -1.1381, places=3)          # 由相邻收盘自算
        self.assertAlmostEqual(bars[-2].change_pct, -0.2042, places=3)      # 1251.24 / 1253.80
        self.assertEqual(provider.last_notes, [])

    def test_unsupported_freq_or_adjust_returns_empty(self):
        provider = self._provider(STOCK_PAYLOADS)
        for kwargs in ({"freq": "week"}, {"freq": "month"}, {"adjust": "hfq"},
                       {"adjust": "none"}, {"days": 0}, {"days": "x"}):
            self.assertEqual(provider.kline("600519.SH", **kwargs), [], kwargs)
        self.assertEqual(provider.calls, [])                  # 未受支持的请求不发网络
        self.assertEqual(provider.kline("无法识别"), [])

    def test_year_pagination_is_best_effort(self):
        """last.js 不够时逐年往前抓；某年抓失败就返回已收集到的真实根数并记笔记。"""
        provider = self._provider({
            _TIME_KEY: TIME_SAMPLE,
            _LINE_KEY: LINE_SAMPLE,
            "v6/line/hs_600519/01/2025.js": LINE_2025_SAMPLE,
        })
        bars = provider.kline("600519.SH", 250)
        self.assertEqual(len(bars), 9)                        # 6 根（last.js）+ 3 根（2025.js）
        self.assertEqual(bars[0].date, "2025-01-02")
        self.assertEqual(bars[-1].date, "2026-09-24")
        self.assertEqual([bar.date for bar in bars], sorted(bar.date for bar in bars))
        self.assertTrue(any("v6/line/hs_600519/01/2025.js" in url for url in provider.calls),
                        provider.calls)
        self.assertTrue(any("v6/line/hs_600519/01/2024.js" in url for url in provider.calls),
                        provider.calls)          # 2024 抓失败即止
        self.assertTrue(any("2024 年日线抓取失败" in note for note in provider.last_notes),
                        provider.last_notes)
        self.assertTrue(any("只提供 9 根日线" in note for note in provider.last_notes),
                        provider.last_notes)

    def test_year_pagination_stops_at_history_start(self):
        """``start`` 已到最早年份：不再往前抓，直接返回已有根数。"""
        provider = self._provider({
            _TIME_KEY: TIME_SAMPLE,
            _LINE_KEY: LINE_SAMPLE.replace("20260306,", "20010827,"),   # 最早根 = start 那年
        })
        bars = provider.kline("600519.SH", 7)
        self.assertEqual(len(bars), 6)
        self.assertEqual(bars[0].date, "2001-08-27")
        self.assertEqual(provider.calls,
                         ["https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js"])
        self.assertTrue(any("只提供 6 根日线" in note for note in provider.last_notes),
                        provider.last_notes)

    def test_daily_failure_returns_empty(self):
        provider = self._provider({})
        self.assertEqual(provider.kline("600519.SH", 10), [])
        self.assertTrue(any("日线不可用" in note for note in provider.last_notes),
                        provider.last_notes)


# --------------------------------------------------------------------------- 名称与自检
class ThsNameTest(_ThsTestCase):
    """``resolve_name``：名称来自分时接口，走磁盘缓存。"""

    def test_resolve_name_and_cache(self):
        provider = self._provider({_TIME_KEY: TIME_SAMPLE})
        self.assertEqual(provider.resolve_name("600519.SH"), "贵州茅台")
        calls_after_first = len(provider.calls)
        self.assertEqual(provider.resolve_name("600519.SH"), "贵州茅台")
        self.assertEqual(len(provider.calls), calls_after_first)      # 命中磁盘缓存，不再发请求
        self.assertEqual(provider.resolve_name("600519.SH"), "贵州茅台")

    def test_resolve_name_fallbacks(self):
        provider = self._provider({})
        self.assertIsNone(provider.resolve_name("???"))               # 代码非法
        self.assertEqual(provider.resolve_name("399001.SZ"), "深证成指")   # 取数失败 → 内置指数名

    def test_health_ok_from_sample(self):
        provider = self._provider({_TIME_KEY: TIME_SAMPLE})
        health = provider.health()
        self.assertTrue(health["ok"], health)
        self.assertIn("同花顺可用", health["detail"])
        self.assertEqual(health["source"], "ths")


# --------------------------------------------------------------------------- 离线模式
class ThsOfflineTest(unittest.TestCase):
    """``QUANTSTUDIO_OFFLINE=1``：任何调用都不发起网络请求。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings(offline=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_mode_never_calls_network(self):
        provider = ThsProvider(self.settings)
        with self.assertRaises(ProviderUnavailable):
            provider.latest_quotes(["600519.SH"])
        with self.assertRaises(ProviderUnavailable):
            provider.kline("600519.SH", 30)
        with self.assertRaises(ProviderUnavailable):
            provider._jsonp(provider.TIME_URL % "hs_600519")
        health = provider.health()
        self.assertFalse(health["ok"])
        self.assertIn("离线模式", health["detail"])

    def test_offline_resolve_name_uses_builtin_only(self):
        provider = ThsProvider(self.settings)
        self.assertEqual(provider.resolve_name("399001.SZ"), "深证成指")
        self.assertIsNone(provider.resolve_name("600519.SH"))


if __name__ == "__main__":
    unittest.main()
