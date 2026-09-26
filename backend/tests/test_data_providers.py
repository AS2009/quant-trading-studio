# -*- coding: utf-8 -*-
"""真实行情数据层单元测试（标准库 unittest）。

运行
----
::

    # 仓库根目录
    python -m unittest discover -s backend/tests -v
    # 或 backend 目录
    cd backend && python -m unittest discover -s tests -v

需要网络的用例（东财 K 线 / 新浪快照）在断网或数据源不可用时会 ``skipTest``，
不会让整套测试失败。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from quantstudio.config import Settings                             # noqa: E402
from quantstudio.core.errors import (                                   # noqa: E402
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
    ValidationError,
)
from quantstudio.core.models import (                                   # noqa: E402
    Bar,
    IndexQuote,
    MarketBreadth,
    OrderBook,
    OrderBookLevel,
    Quote,
    SectorQuote,
    Tick,
)
from quantstudio.data import (                                          # noqa: E402
    build_provider,
    get_provider,
    reset_provider,
    settings_with_source,
)
from quantstudio.data import level2                                      # noqa: E402
from quantstudio.data import symbols as sym                             # noqa: E402
from quantstudio.data.base import BaseHTTPProvider                      # noqa: E402
from quantstudio.data.cache import DiskCache                            # noqa: E402
from quantstudio.data.composite import CompositeProvider                # noqa: E402
from quantstudio.data.csv_provider import CsvProvider                   # noqa: E402
from quantstudio.data.eastmoney import EastmoneyProvider                # noqa: E402
from quantstudio.data.sample import SampleProvider                      # noqa: E402
from quantstudio.data.sina import SinaProvider                          # noqa: E402
from quantstudio.data.tencent import TencentProvider                    # noqa: E402


def _tmp_settings(**kwargs):
    """临时目录隔离的 Settings（绝不污染仓库 data 目录）。"""
    tmp = tempfile.mkdtemp(prefix="qts-data-test-")
    params = dict(
        data_dir=tmp,
        cache_dir=os.path.join(tmp, "cache"),
        data_source="auto",
        http_timeout=6,
        http_retries=1,
    )
    params.update(kwargs)
    return Settings(**params), tmp


class SymbolsTest(unittest.TestCase):
    """代码规范化与转换。"""

    def test_normalize_variants(self):
        cases = {
            "600519": "600519.SH",
            "sh600519": "600519.SH",
            "SH600519": "600519.SH",
            "600519.SH": "600519.SH",
            "600519.SS": "600519.SH",
            "600519.XSHG": "600519.SH",
            "300750": "300750.SZ",
            "300750.XSHE": "300750.SZ",
            "sz300750": "300750.SZ",
            "000001.SH": "000001.SH",
            "000001.SZ": "000001.SZ",
            "1.600519": "600519.SH",
            "0.300750": "300750.SZ",
            "430047": "430047.BJ",
            "bj430047": "430047.BJ",
            " 600036 ": "600036.SH",
            "510300": "510300.SH",
            "159915": "159915.SZ",
            "200011": "200011.SZ",       # 深市 B 股
            "900901": "900901.SH",       # 沪市 B 股
            "920001": "920001.BJ",       # 北交所新代码段
        }
        for raw, expected in cases.items():
            self.assertEqual(sym.normalize(raw), expected, raw)

    def test_normalize_invalid(self):
        for bad in ("", "  ", None, "abc", "60051", "6005190", "600519.XX", "12345"):
            with self.assertRaises(SymbolNotFound):
                sym.normalize(bad)

    def test_market_inference_rules(self):
        self.assertEqual(sym.normalize("600000"), "600000.SH")     # 6 → SH
        self.assertEqual(sym.normalize("000001.SZ"), "000001.SZ")  # 0 → SZ
        self.assertEqual(sym.normalize("002594"), "002594.SZ")     # 2 → SZ
        self.assertEqual(sym.normalize("300059"), "300059.SZ")     # 3 → SZ
        self.assertEqual(sym.normalize("830799"), "830799.BJ")     # 8 → BJ
        self.assertEqual(sym.normalize("159915"), "159915.SZ")     # 深市基金
        with self.assertRaises(SymbolNotFound):
            sym.infer_market("700000")                             # 7 开头无法判断

    def test_kind_of(self):
        self.assertEqual(sym.kind_of("000001.SH"), "index")
        self.assertEqual(sym.kind_of("000300.SH"), "index")
        self.assertEqual(sym.kind_of("000905.SH"), "index")
        self.assertEqual(sym.kind_of("000016.SH"), "index")
        self.assertEqual(sym.kind_of("000688.SH"), "index")
        self.assertEqual(sym.kind_of("399001.SZ"), "index")
        self.assertEqual(sym.kind_of("399006.SZ"), "index")
        self.assertEqual(sym.kind_of("399673.SZ"), "index")
        self.assertEqual(sym.kind_of("000852.SH"), "index")
        self.assertEqual(sym.kind_of("600519.SH"), "stock")
        self.assertEqual(sym.kind_of("000001.SZ"), "stock")
        self.assertEqual(sym.kind_of("430047.BJ"), "stock")
        self.assertEqual(sym.kind_of("159915.SZ"), "etf")
        self.assertEqual(sym.kind_of("510300.SH"), "etf")
        self.assertEqual(sym.kind_of("588000.SH"), "etf")

    def test_index_names_table(self):
        required = {
            "000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指",
            "000688.SH": "科创50", "000300.SH": "沪深300", "000905.SH": "中证500",
            "000016.SH": "上证50", "399673.SZ": "创业板50",
        }
        for code, name in required.items():
            self.assertEqual(sym.INDEX_NAMES.get(code), name, code)

    def test_source_symbols(self):
        self.assertEqual(sym.to_eastmoney_secid("600519.SH"), "1.600519")
        self.assertEqual(sym.to_eastmoney_secid("300750.SZ"), "0.300750")
        self.assertEqual(sym.to_eastmoney_secid("430047.BJ"), "0.430047")
        self.assertEqual(sym.to_eastmoney_secid("000001.SH"), "1.000001")
        self.assertEqual(sym.to_sina_symbol("600519.SH"), "sh600519")
        self.assertEqual(sym.to_sina_symbol("300750"), "sz300750")
        self.assertEqual(sym.to_sina_symbol("430047"), "bj430047")
        self.assertEqual(sym.display_code("sh600519"), "600519")
        self.assertEqual(sym.with_market("600519"), "600519.SH")
        self.assertEqual(sym.market_of("300750"), "SZ")
        self.assertEqual(sym.to_eastmoney_secids(["600519", "bad", "300750"]), ["1.600519", "0.300750"])


class CacheTest(unittest.TestCase):
    """磁盘缓存：读写 / 过期 / 原子写 / 失败不抛。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qts-cache-test-")
        self.cache = DiskCache(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_set_get_roundtrip_and_ttl(self):
        key = self.cache.make_key("kline", ["600519.SH", 30, "day", "qfq"])
        self.assertTrue(self.cache.set(key, [{"date": "2026-09-21", "close": 1.0}], 60))
        self.assertEqual(self.cache.get(key), [{"date": "2026-09-21", "close": 1.0}])
        # 过期后 get 返回 None，get_stale 仍能取到
        self.cache.set(key, {"v": 1}, 0)
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(self.cache.get_stale(key), {"v": 1})
        # get_stale 不存在的 key → None
        self.assertIsNone(self.cache.get_stale("missing|key"))

    def test_namespace_subdir_and_sha1_filename(self):
        key = self.cache.make_key("quotes", ["600519.SH,300750.SZ"])
        self.cache.set(key, [1, 2, 3], 30)
        entry_files = os.listdir(os.path.join(self.dir, "quotes"))
        self.assertEqual(len(entry_files), 1)
        self.assertEqual(len(entry_files[0].split(".")[0]), 40)     # sha1 hex
        self.assertFalse([name for name in os.listdir(os.path.join(self.dir, "quotes"))
                          if name.endswith(".tmp")])

    def test_stats_and_clear(self):
        self.cache.set(self.cache.make_key("quotes", ["a"]), {"x": 1}, 30)
        self.cache.set(self.cache.make_key("kline", ["b"]), [{"x": 1}], 30)
        stats = self.cache.stats()
        self.assertEqual(stats["entries"], 2)
        self.assertIn("quotes", stats["namespaces"])
        self.assertGreater(stats["bytes"], 0)
        removed = self.cache.clear()
        self.assertEqual(removed, 2)
        self.assertEqual(self.cache.stats()["entries"], 0)

    def test_bad_dir_never_raises(self):
        path = os.path.join(self.dir, "file-not-dir")
        with open(path, "w") as fh:
            fh.write("x")
        broken = DiskCache(os.path.join(path, "sub"))
        self.assertFalse(broken.set("quotes|a", {"x": 1}, 10))
        self.assertIsNone(broken.get("quotes|a"))
        self.assertIsNone(broken.get_stale("quotes|a"))
        self.assertEqual(broken.clear(), 0)
        broken.stats()      # 不抛异常


# --------------------------------------------------------------------------- 固定样本
def _sina_line(symbol, fields):
    return 'var hq_str_%s="%s";' % (symbol, ",".join(fields))


def _stock_fields(name, open_, prev, last, high, low, volume_shares, amount_yuan,
                  day="2026-09-21", clock="15:34:59"):
    return [name, "%.3f" % open_, "%.3f" % prev, "%.3f" % last, "%.3f" % high, "%.3f" % low,
            "%.3f" % last, "%.3f" % (last + 0.01), str(volume_shares), "%.3f" % amount_yuan] \
        + ["0"] * 20 + [day, clock, "00"]


SINA_SAMPLE = "\n".join([
    _sina_line("sh600519", _stock_fields("贵州茅台", 1259.0, 1257.12, 1252.57, 1259.95, 1250.8,
                                         2501689, 3135910045.0)),
    _sina_line("sz000000", []),                                  # 停牌 / 不存在的标的
    _sina_line("sh000001", _stock_fields("上证指数", 3920.2731, 3911.8714, 3949.9068,
                                         3950.9394, 3918.1282, 502354877, 946819124309.0)),
    _sina_line("sz399001", _stock_fields("深证成指", 13716.223, 13640.873, 13730.021,
                                         13778.998, 13643.950, 63170807053, 1084694363403.187)),
    _sina_line("sz159915", _stock_fields("创业板", 3.421, 3.391, 3.418, 3.449, 3.394,
                                         1513054639, 5181806533.315)),
]) + "\n"



class _StubSina(SinaProvider):
    def __init__(self, text_or_bytes, settings=None, charset=""):
        SinaProvider.__init__(self, settings)
        raw = text_or_bytes if isinstance(text_or_bytes, bytes) else text_or_bytes.encode("utf-8")
        self._raw = raw
        self._charset = charset

    def _fetch(self, url, referer="", params=None, headers=None):
        return self._raw, self._charset


class SinaParseTest(unittest.TestCase):
    """新浪快照解析：GBK 解码、成交量单位、停牌容错（全离线）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gbk_decode(self):
        text = "上证指数,3920.27"
        self.assertEqual(BaseHTTPProvider._decode(text.encode("gbk"), "GBK"), text)
        self.assertEqual(BaseHTTPProvider._decode(text.encode("gbk"), ""), text)       # 探测成功
        self.assertEqual(BaseHTTPProvider._decode(text.encode("utf-8"), ""), text)
        self.assertEqual(BaseHTTPProvider._decode(b""), "")

    def test_latest_quotes_from_gbk_bytes(self):
        provider = _StubSina(SINA_SAMPLE.encode("gbk"), self.settings, charset="GBK")
        quotes = {item.code: item for item in provider.latest_quotes(
            ["600519.SH", "000000.SZ", "000001.SH", "399001.SZ", "159915.SZ"])}
        self.assertNotIn("000000.SZ", quotes)                  # 停牌 / 空内容 → 跳过
        self.assertEqual(provider.last_skipped, ["000000.SZ"])

        maotai = quotes["600519.SH"]
        self.assertEqual(maotai.name, "贵州茅台")               # GBK 解码正确
        self.assertAlmostEqual(maotai.price, 1252.57, places=2)
        self.assertAlmostEqual(maotai.prev_close, 1257.12, places=2)
        self.assertAlmostEqual(maotai.change_pct, -0.3619, places=3)
        self.assertAlmostEqual(maotai.volume_wan, 2501689 / 100.0 / 1e4, places=4)   # 股 → 万手
        self.assertAlmostEqual(maotai.amount_yi, 3135910045.0 / 1e8, places=4)
        self.assertEqual(maotai.ts, "2026-09-21 15:34:59")
        self.assertEqual(maotai.source, "sina")

        sh_index = quotes["000001.SH"]
        self.assertAlmostEqual(sh_index.volume_wan, 502354877 / 1e4, places=2)       # 沪指 → 手
        sz_index = quotes["399001.SZ"]
        self.assertAlmostEqual(sz_index.volume_wan, 63170807053 / 100.0 / 1e4, places=2)  # 深指 → 股
        etf = quotes["159915.SZ"]
        self.assertAlmostEqual(etf.volume_wan, 1513054639 / 100.0 / 1e4, places=3)   # ETF → 份

    def test_index_quotes_from_gbk_bytes(self):
        provider = _StubSina(SINA_SAMPLE.encode("gbk"), self.settings, charset="GBK")
        items = {item.code: item for item in provider.index_quotes(["000001.SH", "399001.SZ"])}
        self.assertAlmostEqual(items["000001.SH"].point, 3949.9068, places=3)
        self.assertEqual(items["000001.SH"].name, "上证指数")
        self.assertAlmostEqual(items["399001.SZ"].prev_close, 13640.873, places=3)
        self.assertGreater(items["399001.SZ"].amount_yi, 1000.0)

    def test_all_empty_raises(self):
        provider = _StubSina(_sina_line("sh600519", []) + "\n", self.settings)
        with self.assertRaises(DataSourceError):
            provider.latest_quotes(["600519.SH"])

    def test_unsupported_capabilities_raise(self):
        provider = SinaProvider(self.settings)
        with self.assertRaises(ProviderUnavailable):
            provider.kline("600519.SH", 30)
        with self.assertRaises(ProviderUnavailable):
            provider.sectors(5)
        with self.assertRaises(ProviderUnavailable):
            provider.breadth()

    def test_offline_mode_never_calls_network(self):
        settings, tmp = _tmp_settings(offline=True)
        try:
            provider = SinaProvider(settings)
            with self.assertRaises(ProviderUnavailable):
                provider.latest_quotes(["600519.SH"])
            health = provider.health()
            self.assertFalse(health["ok"])
            self.assertIn("离线模式", health["detail"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


KLINE_SAMPLE_PAYLOAD = {
    "rc": 0,
    "data": {
        "code": "600519", "market": 1, "name": "贵州茅台",
        "klines": [
            "2026-09-17,1257.98,1266.98,1267.60,1254.00,17554,2217338283.00,1.08,0.71,8.98,0.14",
            "2026-09-18,1262.99,1257.12,1265.88,1256.10,24891,3135849108.00,0.77,-0.78,-9.86,0.20",
            "2026-09-21,1259.00,1252.57,1259.95,1250.80,25017,3135910045.00,0.73,-0.36,-4.55,0.20",
        ],
    },
}

SNAPSHOT_SAMPLE_PAYLOAD = {
    "rc": 0,
    "data": {"total": 2, "diff": [
        {"f2": 1252.57, "f3": -0.36, "f4": -4.55, "f5": 25017, "f6": 3135910045.0, "f8": 0.2,
         "f9": 17.59, "f10": 1.26, "f12": "600519", "f13": 1, "f14": "贵州茅台", "f15": 1259.95,
         "f16": 1250.8, "f17": 1259.0, "f18": 1257.12, "f20": 1565814710965, "f21": 1565814710965,
         "f23": 6.23},
        {"f2": "-", "f3": "-", "f4": "-", "f5": "-", "f6": "-", "f8": "-", "f9": "-", "f10": "-",
         "f12": "300750", "f13": 0, "f14": "宁德时代", "f15": "-", "f16": "-", "f17": "-",
         "f18": "-", "f20": "-", "f21": "-", "f23": "-"},
    ]},
}

BOARD_SAMPLE_PAYLOAD = {
    "rc": 0,
    "data": {"total": 496, "diff": [
        {"f2": 4827.59, "f3": 7.97, "f12": "BK1599", "f14": "其他医疗服务", "f62": 48168824.0,
         "f104": 5, "f105": 0, "f106": 0, "f128": "南华生物", "f140": "000504"},
        {"f2": 1632.85, "f3": 7.35, "f12": "BK1341", "f14": "房产租赁经纪", "f62": 265949626.0,
         "f104": 3, "f105": 1, "f106": 0, "f128": "我爱我家", "f140": "000560"},
    ]},
}


class _FakeEastmoney(EastmoneyProvider):
    """用固定 payload 替换 HTTP，离线验证字段映射。"""

    def __init__(self, payload, settings=None, base_url=""):
        EastmoneyProvider.__init__(self, settings)
        self._payload = payload
        self._base_url = base_url
        self.calls = []

    def _get_json(self, url, referer="", params=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if self._base_url and not url.startswith(self._base_url):
            raise AssertionError("请求了非预期接口：%s" % url)
        return self._payload


class EastmoneyParseTest(unittest.TestCase):
    """东财字段映射（固定样本，离线）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kline_fields(self):
        provider = _FakeEastmoney(KLINE_SAMPLE_PAYLOAD, self.settings,
                                  base_url=EastmoneyProvider.KLINE_URL)
        bars = provider.kline("600519", 3, freq="day", adjust="qfq")
        self.assertEqual(len(bars), 3)
        self.assertEqual([bar.date for bar in bars], ["2026-09-17", "2026-09-18", "2026-09-21"])
        last = bars[-1]
        self.assertAlmostEqual(last.open, 1259.0, places=2)
        self.assertAlmostEqual(last.close, 1252.57, places=2)
        self.assertAlmostEqual(last.high, 1259.95, places=2)
        self.assertAlmostEqual(last.low, 1250.80, places=2)
        self.assertAlmostEqual(last.volume_wan, 25017 / 1e4, places=4)          # 手 → 万手
        self.assertAlmostEqual(last.amount_yi, 3135910045.0 / 1e8, places=4)    # 元 → 亿元
        self.assertAlmostEqual(last.change_pct, -0.36, places=3)
        self.assertAlmostEqual(last.turnover_pct, 0.20, places=3)
        params = provider.calls[0]["params"]
        self.assertEqual(params["secid"], "1.600519")
        self.assertEqual(params["klt"], 101)
        self.assertEqual(params["fqt"], 1)
        self.assertEqual(params["lmt"], 3)
        self.assertEqual(params["fields2"], "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
        self.assertEqual(provider.known_name("600519.SH"), "贵州茅台")

    def test_kline_freq_adjust_mapping(self):
        provider = _FakeEastmoney(KLINE_SAMPLE_PAYLOAD, self.settings)
        provider.kline("600519.SH", 3, freq="week", adjust="hfq")
        params = provider.calls[0]["params"]
        self.assertEqual((params["klt"], params["fqt"]), (102, 2))
        provider.kline("600519.SH", 3, freq="month", adjust="none")
        params = provider.calls[1]["params"]
        self.assertEqual((params["klt"], params["fqt"]), (103, 0))
        with self.assertRaises(ValidationError):
            provider.kline("600519.SH", 3, freq="hour")
        with self.assertRaises(ValidationError):
            provider.kline("600519.SH", 3, adjust="weird")

    def test_snapshot_fields(self):
        provider = _FakeEastmoney(SNAPSHOT_SAMPLE_PAYLOAD, self.settings,
                                  base_url=EastmoneyProvider.SNAPSHOT_URL)
        quotes = {item.code: item for item in provider.latest_quotes(["600519", "300750"])}
        maotai = quotes["600519.SH"]
        self.assertAlmostEqual(maotai.price, 1252.57, places=2)
        self.assertAlmostEqual(maotai.prev_close, 1257.12, places=2)
        self.assertAlmostEqual(maotai.change, -4.55, places=2)
        self.assertAlmostEqual(maotai.change_pct, -0.36, places=3)
        self.assertAlmostEqual(maotai.volume_wan, 25017 / 1e4, places=4)
        self.assertAlmostEqual(maotai.amount_yi, 3135910045.0 / 1e8, places=4)
        self.assertAlmostEqual(maotai.turnover_pct, 0.20, places=3)
        self.assertAlmostEqual(maotai.pe_ttm, 17.59, places=2)
        self.assertAlmostEqual(maotai.pb, 6.23, places=2)
        self.assertAlmostEqual(maotai.market_cap_yi, 1565814710965 / 1e8, places=2)
        self.assertEqual(maotai.name, "贵州茅台")
        self.assertEqual(maotai.source, "eastmoney")
        # 停牌（全 "-"）→ 跳过
        self.assertNotIn("300750.SZ", quotes)

    def test_index_quotes_use_point(self):
        payload = {"rc": 0, "data": {"total": 1, "diff": [
            {"f2": 3949.91, "f3": 0.97, "f4": 38.04, "f5": 502354877, "f6": 946819124308.7,
             "f8": 1.04, "f9": "-", "f10": 1.09, "f12": "000001", "f13": 1, "f14": "上证指数",
             "f15": 3950.94, "f16": 3918.13, "f17": 3920.27, "f18": 3911.87, "f20": 0, "f21": 0,
             "f23": "-"},
        ]}}
        provider = _FakeEastmoney(payload, self.settings)
        items = provider.index_quotes(["000001.SH"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].code, "000001.SH")
        self.assertEqual(items[0].name, "上证指数")
        self.assertAlmostEqual(items[0].point, 3949.91, places=2)
        self.assertAlmostEqual(items[0].change_pct, 0.97, places=2)
        self.assertAlmostEqual(items[0].amount_yi, 946819124308.7 / 1e8, places=2)

    def test_sectors_fields(self):
        provider = _FakeEastmoney(BOARD_SAMPLE_PAYLOAD, self.settings,
                                  base_url=EastmoneyProvider.BOARD_URL)
        rows = provider.sectors(2)
        self.assertEqual([item.code for item in rows], ["BK1599", "BK1341"])   # 按涨幅降序
        first = rows[0]
        self.assertEqual(first.name, "其他医疗服务")
        self.assertAlmostEqual(first.change_pct, 7.97, places=2)
        self.assertAlmostEqual(first.net_inflow_yi, 48168824.0 / 1e8, places=4)
        self.assertEqual(first.up_count, 5)
        self.assertEqual(first.down_count, 0)
        self.assertEqual(first.leading_stock, "南华生物")
        self.assertEqual(first.leading_code, "000504")
        params = provider.calls[0]["params"]
        self.assertEqual(params["fs"], "m:90+t:2+f:!50")
        self.assertEqual(params["fid"], "f3")
        self.assertEqual(params["po"], 1)
        self.assertEqual(params["fltt"], 2)

    def test_breadth_sums(self):
        payload = {
            "rc": 0,
            "data": {"total": 496, "diff": [
                {"f12": "BK1", "f14": "A", "f3": 1.0, "f62": 100000000.0, "f104": 10, "f105": 4, "f106": 1},
                {"f12": "BK2", "f14": "B", "f3": 0.5, "f62": 50000000.0, "f104": 6, "f105": 2, "f106": 0},
            ]},
        }
        index_payload = {
            "rc": 0,
            "data": {"total": 2, "diff": [
                {"f2": 3949.91, "f3": 0.97, "f4": 38.04, "f5": 1, "f6": 946819124308.7, "f8": 1.0,
                 "f9": "-", "f10": 1.0, "f12": "000001", "f13": 1, "f14": "上证指数", "f15": 0,
                 "f16": 0, "f17": 0, "f18": 0, "f20": 0, "f21": 0, "f23": "-",
                 "f104": 1200, "f105": 700, "f106": 50},
                {"f2": 13730.02, "f3": 0.65, "f4": 89.15, "f5": 1, "f6": 1084694363403.19, "f8": 2.0,
                 "f9": "-", "f10": 1.0, "f12": "399001", "f13": 0, "f14": "深证成指", "f15": 0,
                 "f16": 0, "f17": 0, "f18": 0, "f20": 0, "f21": 0, "f23": "-",
                 "f104": 2100, "f105": 1100, "f106": 80},
            ]},
        }

        class _Multi(_FakeEastmoney):
            def _get_json(self, url, referer="", params=None, headers=None):
                self.calls.append({"url": url, "params": dict(params or {})})
                if url.startswith(EastmoneyProvider.BOARD_URL):
                    return payload
                return index_payload

        provider = _Multi(BOARD_SAMPLE_PAYLOAD, self.settings)
        breadth = provider.breadth()
        self.assertEqual(breadth.up, 3300)          # 1200 + 2100（指数快照口径）
        self.assertEqual(breadth.down, 1800)
        self.assertEqual(breadth.flat, 130)
        self.assertEqual(breadth.total, 5230)
        self.assertAlmostEqual(breadth.main_net_inflow_yi, 1.5, places=4)   # (1e8 + 5e7) / 1e8
        self.assertAlmostEqual(breadth.total_amount_yi,
                               (946819124308.7 + 1084694363403.19) / 1e8, places=2)
        self.assertEqual(breadth.source, "eastmoney")

    def test_breadth_falls_back_to_board_sums(self):
        # 指数快照里没有 f104/f105/f106 → 使用行业板块成分汇总口径
        class _BoardsOnly(_FakeEastmoney):
            def _get_json(self, url, referer="", params=None, headers=None):
                self.calls.append({"url": url, "params": dict(params or {})})
                if url.startswith(EastmoneyProvider.BOARD_URL):
                    return BOARD_SAMPLE_PAYLOAD
                return {"rc": 0, "data": {"total": 0, "diff": []}}

        provider = _BoardsOnly(BOARD_SAMPLE_PAYLOAD, self.settings)
        breadth = provider.breadth()
        self.assertEqual(breadth.up, 8)          # 5 + 3（行业板块 f104 汇总口径）
        self.assertEqual(breadth.down, 1)
        self.assertEqual(breadth.flat, 0)
        self.assertEqual(breadth.total, 9)
        self.assertEqual(breadth.total_amount_yi, 0.0)   # 指数快照缺失
        self.assertTrue(any("板块" in note for note in provider.last_notes))


class EastmoneyLiveTest(unittest.TestCase):
    """东财真实网络（不可用时 skip），验证真实数据确实能解析。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings(http_timeout=8, http_retries=1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_kline(self):
        provider = EastmoneyProvider(self.settings)
        try:
            bars = provider.kline("600519.SH", 5)
        except DataSourceError as exc:
            self.skipTest("东财 K 线不可用（网络 / 限流）：%s" % exc)
        self.assertGreaterEqual(len(bars), 1)
        dates = [bar.date for bar in bars]
        self.assertEqual(dates, sorted(dates))
        self.assertGreater(bars[-1].close, 0)
        self.assertGreater(bars[-1].volume_wan, 0)
        self.assertRegex(bars[-1].date, r"^\d{4}-\d{2}-\d{2}$")

    def test_live_quotes_and_health(self):
        provider = EastmoneyProvider(self.settings)
        try:
            quotes = provider.latest_quotes(["600519.SH", "300750.SZ"])
        except DataSourceError as exc:
            self.skipTest("东财快照不可用（网络 / 限流）：%s" % exc)
        self.assertTrue(quotes)
        codes = [item.code for item in quotes]
        self.assertIn("600519.SH", codes)
        self.assertTrue(all(item.source == "eastmoney" for item in quotes))

    def test_live_sina(self):
        provider = SinaProvider(self.settings)
        try:
            quotes = provider.latest_quotes(["600519.SH", "000001.SH"])
        except DataSourceError as exc:
            self.skipTest("新浪不可用（网络）：%s" % exc)
        codes = [item.code for item in quotes]
        self.assertIn("600519.SH", codes)
        self.assertTrue(all(item.name for item in quotes))


class CsvProviderTest(unittest.TestCase):
    """本地 CSV 数据源。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings(data_source="csv")
        self.csv_dir = self.settings.csv_dir
        os.makedirs(os.path.join(self.csv_dir, "kline"), exist_ok=True)
        with open(os.path.join(self.csv_dir, "quotes.csv"), "w", encoding="utf-8") as fh:
            fh.write("code,name,price,prev_close,open,high,low,volume,amount,ts\n")
            fh.write("600519.SH,贵州茅台,1252.57,1257.12,1259.0,1259.95,1250.8,2501689,"
                     "3135910045,2026-09-21 15:00:00\n")
            fh.write("000001.SH,上证指数,3949.91,3911.87,3920.27,3950.94,3918.13,502354877,"
                     "946819124308.7,2026-09-21 15:00:00\n")
        with open(os.path.join(self.csv_dir, "kline", "600519.SH.csv"), "w", encoding="utf-8") as fh:
            fh.write("date,open,high,low,close,volume,amount,change_pct,turnover_pct\n")
            fh.write("2026-09-14,1260,1270,1250,1265,20000,2500000000,0.4,0.20\n")
            fh.write("2026-09-15,1265,1275,1255,1270,21000,2600000000,0.4,0.21\n")
            fh.write("2026-09-21,1259,1260,1250,1252.57,25017,3135910045,-0.36,0.20\n")
        with open(os.path.join(self.csv_dir, "kline_all.csv"), "w", encoding="utf-8") as fh:
            fh.write("code,date,open,high,low,close,volume,amount\n")
            fh.write("300750.SZ,2026-09-18,303.04,303.60,295.50,297.10,494883,14755805343.98\n")
            fh.write("300750.SZ,2026-09-21,297.10,298.00,296.10,297.50,490000,14600000000.00\n")
        with open(os.path.join(self.csv_dir, "sectors.csv"), "w", encoding="utf-8") as fh:
            fh.write("code,name,change_pct,net_inflow,up_count,down_count,leading_stock\n")
            fh.write("BK1,半导体,3.21,12.5,40,6,中芯国际\n")
            fh.write("BK2,银行,-0.85,-320000000,3,20,招商银行\n")
        with open(os.path.join(self.csv_dir, "breadth.csv"), "w", encoding="utf-8") as fh:
            fh.write("key,value\nup,3200\ndown,1800\nflat,200\nlimit_up,45\nlimit_down,6\n"
                     "total_amount,15000\nmain_net_inflow,-120.5\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quotes(self):
        provider = CsvProvider(self.settings)
        quotes = {item.code: item for item in provider.latest_quotes(["600519.SH", "300750.SZ"])}
        self.assertEqual(list(quotes), ["600519.SH"])
        item = quotes["600519.SH"]
        self.assertEqual(item.name, "贵州茅台")
        self.assertAlmostEqual(item.price, 1252.57, places=2)
        self.assertAlmostEqual(item.volume_wan, 2501689 / 100.0 / 1e4, places=4)   # 股 → 万手
        self.assertAlmostEqual(item.amount_yi, 3135910045 / 1e8, places=4)         # 元 → 亿元
        self.assertEqual(item.ts, "2026-09-21 15:00:00")
        self.assertEqual(item.source, "csv")
        with self.assertRaises(ProviderUnavailable):
            provider.latest_quotes(["600000.SH"])     # CSV 里没有该标的

    def test_index_quotes(self):
        provider = CsvProvider(self.settings)
        items = provider.index_quotes(["000001.SH"])
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0].point, 3949.91, places=2)
        self.assertEqual(items[0].name, "上证指数")

    def test_kline(self):
        provider = CsvProvider(self.settings)
        bars = provider.kline("600519.SH", 2)
        self.assertEqual([bar.date for bar in bars], ["2026-09-15", "2026-09-21"])   # 截尾取最近 2 根
        self.assertAlmostEqual(bars[-1].close, 1252.57, places=2)
        self.assertAlmostEqual(bars[-1].volume_wan, 25017 / 1e4, places=4)          # 手 → 万手
        self.assertAlmostEqual(bars[-1].amount_yi, 3135910045 / 1e8, places=4)
        self.assertAlmostEqual(bars[-1].change_pct, -0.36, places=3)
        # kline_all.csv（带 code 列）
        bars_other = provider.kline("300750.SZ", 10)
        self.assertEqual(len(bars_other), 2)
        self.assertAlmostEqual(bars_other[-1].close, 297.50, places=2)
        with self.assertRaises(ProviderUnavailable):
            provider.kline("601318.SH", 10)

    def test_kline_weekly_aggregation(self):
        provider = CsvProvider(self.settings)
        bars = provider.kline("600519.SH", 10, freq="week")
        self.assertEqual(len(bars), 2)                       # 9/14 + 9/15 同一周，9/21 另一周
        week1 = bars[0]
        self.assertAlmostEqual(week1.open, 1260.0, places=2)
        self.assertAlmostEqual(week1.high, 1275.0, places=2)
        self.assertAlmostEqual(week1.close, 1270.0, places=2)
        self.assertAlmostEqual(week1.volume_wan, (20000 + 21000) / 1e4, places=4)

    def test_sectors_and_breadth(self):
        provider = CsvProvider(self.settings)
        rows = provider.sectors(5)
        self.assertEqual([item.name for item in rows], ["半导体", "银行"])
        self.assertAlmostEqual(rows[0].net_inflow_yi, 12.5, places=2)              # 小额 → 视为亿元
        self.assertAlmostEqual(rows[1].net_inflow_yi, -3.2, places=2)              # 大额 → 视为元
        breadth = provider.breadth()
        self.assertEqual((breadth.up, breadth.down, breadth.flat), (3200, 1800, 200))
        self.assertEqual(breadth.limit_up, 45)
        self.assertEqual(breadth.total, 5200)
        self.assertAlmostEqual(breadth.total_amount_yi, 15000.0, places=2)
        self.assertAlmostEqual(breadth.main_net_inflow_yi, -120.5, places=2)

    def test_missing_dir_raises_unavailable(self):
        settings, tmp = _tmp_settings(data_source="csv")
        try:
            provider = CsvProvider(settings)
            for call in (lambda: provider.latest_quotes(["600519.SH"]),
                         lambda: provider.kline("600519.SH", 5),
                         lambda: provider.sectors(5),
                         lambda: provider.breadth()):
                with self.assertRaises(ProviderUnavailable):
                    call()
            self.assertFalse(provider.health()["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resolve_name(self):
        provider = CsvProvider(self.settings)
        self.assertEqual(provider.resolve_name("600519"), "贵州茅台")
        self.assertIsNone(provider.resolve_name("601318.SH"))


# --------------------------------------------------------------------------- 腾讯（固定样本 + 可选真实网络）
#: 真实响应片段（2026-09-21 实测）：[日期, 开, 收, 高, 低, 成交量(手)]
TX_KLINE_ROWS = [
    ["2026-09-18", "1262.990", "1257.120", "1265.880", "1256.100", "24891.000"],
    ["2026-09-21", "1259.000", "1252.570", "1259.950", "1250.800", "25017.000"],
]
#: 口径变更（腾讯 qfq 曾按减法复权返回，深历史出负价）：本模块的 ``qfq`` = **hfq 缩放到最新
#: 真实价**，所以 qfq 请求命中的是 ``hfqday``；这里让 hfq 末根收盘 == 不复权最新收盘 1252.57
#: （缩放系数 k=1），样本断言值因此与口径变更前完全一致。``day`` 供 qfq 路径取「最新不复权
#: 收盘」用（1 次额外请求）。
TX_KLINE_PAYLOAD = {"code": 0, "data": {"sh600519": {
    "hfqday": TX_KLINE_ROWS,
    "day": [["2026-09-24", "1250.000", "1252.570", "1258.800", "1249.100", "22000.000"]],
    "qt": {"sh600519": "1~贵州茅台~600519~1252.57~1257.12~1259.00~25017".split("~")},
    "prec": 1257.12,
}}}
#: 指数请求 qfq 时返回的是无复权键 ``day``（实测 sh000001 行为）
TX_INDEX_KLINE_PAYLOAD = {"code": 0, "data": {"sh000001": {
    "day": [["2026-09-18", "3891.960", "3911.870", "3919.670", "3888.500", "485712507.000"],
            ["2026-09-21", "3920.270", "3949.910", "3950.940", "3918.130", "502354877.000"]],
    "qt": {"sh000001": ("1~上证指数~000001~3949.91~3911.87~3920.27~502354877~~20260921161400"
                        "~38.04~0.97~3950.94~3918.13~3949.91/502354877/946819124309").split("~")},
    "prec": 3911.87,
}}}
TX_SNAPSHOT_TEXT = "\n".join([
    'v_sh600519="1~贵州茅台~600519~1252.57~1257.12~1259.00~25017~11200~13817~1252.57~1~1252.56~15'
    '~1252.55~110~1252.50~24~1252.45~1~1252.86~57~1252.97~1~1253.00~3~1253.12~1~1253.13~5~~20260921161437'
    '~-4.55~-0.36~1259.95~1250.80~1252.57/25017/3135910045~25017~313591~0.20~19.23";',
    'v_sh000001="1~上证指数~000001~3949.91~3911.87~3920.27~502354877~0~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00'
    '~0~0.00~0~0.00~0~0.00~0~0.00~0~0.00~0~~20260921161400~38.04~0.97~3950.94~3918.13'
    '~3949.91/502354877/946819124309~502354877~94681912~1.04";',
    'v_sz399001=""',
    "",
])
TX_RANK_PAYLOAD = {"code": 0, "msg": "ok", "data": {"total": 3, "offset": 0, "rank_list": [
    {"code": "pt01801120", "name": "食品饮料", "zdf": "0.48", "zljlr": "15368.13", "zgb": "112/122",
     "zsz": "36614.94", "hsl": "1.49", "zxj": "13612.73",
     "lzg": {"code": "sh601579", "name": "会稽山", "zdf": "9.99", "zxj": "36.12"}},
    {"code": "pt01801080", "name": "电子", "zdf": "3.86", "zljlr": "-296330.68", "zgb": "314/494",
     "zsz": "249780.45", "hsl": "3.83", "zxj": "9011.67",
     "lzg": {"code": "sh688678", "name": "福立旺", "zdf": "13.17", "zxj": "27.93"}},
    {"code": "pt01801050", "name": "电力设备", "zdf": "-0.15", "zljlr": "-121141.42", "zgb": "200/300",
     "zsz": "1000.00", "hsl": "1.00", "zxj": "8574.63",
     "lzg": {"code": "sz300001", "name": "特锐德", "zdf": "5.00", "zxj": "20.00"}},
]}}


class _StubTencent(TencentProvider):
    """用固定样本替换网络（字段映射代码路径与真实调用完全一致）。"""

    def __init__(self, settings=None, fail=False):
        TencentProvider.__init__(self, settings)
        self.fail = fail
        self.calls = []

    def _gate(self, method):
        self.calls.append(method)
        if self.fail:
            raise DataSourceError("模拟腾讯故障")

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        self._gate("kline")
        return TencentProvider.kline(self, symbol, days, freq, adjust)

    def latest_quotes(self, symbols):
        self._gate("latest_quotes")
        return TencentProvider.latest_quotes(self, symbols)

    def index_quotes(self, symbols):
        self._gate("index_quotes")
        return TencentProvider.index_quotes(self, symbols)

    def sectors(self, limit=20):
        self._gate("sectors")
        return TencentProvider.sectors(self, limit)

    def breadth(self):
        self._gate("breadth")
        return TencentProvider.breadth(self)

    def resolve_name(self, symbol):
        self._gate("resolve_name")
        return TencentProvider.resolve_name(self, symbol)

    def _get_json(self, url, referer="", params=None, headers=None):
        self.calls.append(("json", str((params or {}).get("param") or url.rsplit("/", 1)[-1])))
        if url.startswith(TencentProvider.KLINE_URL):
            param = str((params or {}).get("param") or "")
            return TX_INDEX_KLINE_PAYLOAD if param.startswith("sh000001") else TX_KLINE_PAYLOAD
        if url.startswith(TencentProvider.RANK_URL):
            return TX_RANK_PAYLOAD
        raise AssertionError("未预期的腾讯接口：%s" % url)

    def _get_text(self, url, referer="", params=None, encoding="", headers=None):
        if url.startswith(TencentProvider.SNAPSHOT_URL):
            return TX_SNAPSHOT_TEXT
        raise AssertionError("未预期的腾讯接口：%s" % url)


class TencentParseTest(unittest.TestCase):
    """腾讯字段映射（固定样本，离线）：开-收-高-低顺序 / 手→万手 / 板块 zgb+zljlr。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kline_open_close_high_low_order(self):
        provider = _StubTencent(self.settings)
        bars = provider.kline("600519.SH", 2)
        self.assertEqual([bar.date for bar in bars], ["2026-09-18", "2026-09-21"])
        last = bars[-1]
        self.assertAlmostEqual(last.open, 1259.00, places=2)      # 第 2 段是「开」
        self.assertAlmostEqual(last.close, 1252.57, places=2)     # 第 3 段是「收」
        self.assertAlmostEqual(last.high, 1259.95, places=2)      # 第 4 段是「高」
        self.assertAlmostEqual(last.low, 1250.80, places=2)       # 第 5 段是「低」
        self.assertAlmostEqual(last.volume_wan, 25017 / 1e4, places=4)   # 手 → 万手
        self.assertEqual(last.amount_yi, 0.0)                     # 腾讯 K 线不返回成交额
        self.assertAlmostEqual(bars[-2].change_pct, 0.0, places=4)
        self.assertAlmostEqual(last.change_pct, -0.3619, places=3)        # 相邻收盘自算
        self.assertEqual(provider.known_name("600519.SH"), "贵州茅台")     # qt 里顺手记名称

    def test_kline_index_uses_plain_day_key(self):
        provider = _StubTencent(self.settings)
        bars = provider.kline("000001.SH", 2)
        self.assertEqual(len(bars), 2)
        self.assertAlmostEqual(bars[-1].close, 3949.91, places=2)
        self.assertAlmostEqual(bars[-1].volume_wan, 502354877 / 1e4, places=2)

    def test_kline_freq_and_adjust_validation(self):
        provider = _StubTencent(self.settings)
        with self.assertRaises(ValidationError):
            provider.kline("600519.SH", 2, freq="hour")
        with self.assertRaises(ValidationError):
            provider.kline("600519.SH", 2, adjust="weird")

    def test_snapshot_fields(self):
        provider = _StubTencent(self.settings)
        quotes = {item.code: item for item in provider.latest_quotes(["600519.SH", "000001.SH", "399001.SZ"])}
        self.assertNotIn("399001.SZ", quotes)                      # 空行 → 跳过
        self.assertEqual(provider.last_skipped, ["399001.SZ"])
        maotai = quotes["600519.SH"]
        self.assertEqual(maotai.name, "贵州茅台")
        self.assertAlmostEqual(maotai.price, 1252.57, places=2)
        self.assertAlmostEqual(maotai.prev_close, 1257.12, places=2)
        self.assertAlmostEqual(maotai.open, 1259.00, places=2)
        self.assertAlmostEqual(maotai.high, 1259.95, places=2)
        self.assertAlmostEqual(maotai.low, 1250.80, places=2)
        self.assertAlmostEqual(maotai.change, -4.55, places=2)
        self.assertAlmostEqual(maotai.change_pct, -0.36, places=3)
        self.assertAlmostEqual(maotai.volume_wan, 25017 / 1e4, places=4)      # 手 → 万手
        self.assertAlmostEqual(maotai.amount_yi, 3135910045 / 1e8, places=4)  # 索引 35 第三段（元）
        self.assertAlmostEqual(maotai.turnover_pct, 0.20, places=3)           # 索引 38 换手率
        self.assertEqual(maotai.ts, "2026-09-21 16:14:37")                    # 索引 30 YYYYMMDDHHMMSS
        self.assertEqual(maotai.source, "tencent")
        index = quotes["000001.SH"]
        self.assertAlmostEqual(index.price, 3949.91, places=2)
        self.assertAlmostEqual(index.amount_yi, 946819124309 / 1e8, places=2)
        self.assertAlmostEqual(index.volume_wan, 502354877 / 1e4, places=2)

    def test_index_quotes(self):
        provider = _StubTencent(self.settings)
        items = provider.index_quotes(["000001.SH"])
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0].point, 3949.91, places=2)
        self.assertAlmostEqual(items[0].change_pct, 0.97, places=2)
        self.assertAlmostEqual(items[0].amount_yi, 946819124309 / 1e8, places=2)

    def test_sectors_mapping_and_sort(self):
        provider = _StubTencent(self.settings)
        rows = provider.sectors(2)
        self.assertEqual([item.name for item in rows], ["电子", "食品饮料"])     # 按 zdf 降序
        top = rows[0]
        self.assertEqual(top.code, "pt01801080")
        self.assertAlmostEqual(top.change_pct, 3.86, places=2)
        self.assertAlmostEqual(top.net_inflow_yi, -296330.68 / 1e4, places=4)  # 万元 → 亿元
        self.assertEqual((top.up_count, top.down_count), (314, 494))           # zgb "314/494"
        self.assertEqual(top.leading_stock, "福立旺")
        self.assertEqual(top.leading_code, "688678.SH")                        # sh688678 → 规范代码
        # 板块接口必须拉全量再本地排序（否则只是「价格前十」的子集）
        calls = [item for item in provider.calls if isinstance(item, tuple)]
        self.assertTrue(calls)

    def test_breadth_caliber_and_notes(self):
        provider = _StubTencent(self.settings)
        breadth = provider.breadth()
        self.assertEqual((breadth.up, breadth.down, breadth.flat), (626, 916, 0))  # zgb 求和；腾讯无平盘家数
        self.assertEqual(breadth.total, 1542)
        self.assertEqual((breadth.limit_up, breadth.limit_down), (0, 0))
        self.assertAlmostEqual(breadth.total_amount_yi, 946819124309 / 1e8, places=2)  # 399001 空 → 只算上证
        # 引擎对金额统一四舍五入到 2 位（亿元），期望值同样取 2 位
        self.assertAlmostEqual(breadth.main_net_inflow_yi,
                               round((15368.13 - 296330.68 - 121141.42) / 1e4, 2), places=2)
        self.assertEqual(breadth.source, "tencent")
        self.assertTrue(any("腾讯行业板块口径（含新三板，合计家数偏大）" in note
                            for note in provider.last_notes), provider.last_notes)

    def test_resolve_name_and_health(self):
        provider = _StubTencent(self.settings)
        self.assertEqual(provider.resolve_name("600519.SH"), "贵州茅台")
        health = provider.health()
        self.assertTrue(health["ok"])
        self.assertIn("腾讯可用", health["detail"])


class TencentLiveTest(unittest.TestCase):
    """腾讯真实网络（不可用时 skip）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings(http_timeout=10, http_retries=1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_kline_stock_and_index(self):
        provider = TencentProvider(self.settings)
        try:
            bars = provider.kline("600519.SH", 30)
            index_bars = provider.kline("000300.SH", 30)
        except DataSourceError as exc:
            self.skipTest("腾讯 K 线不可用：%s" % exc)
        self.assertGreaterEqual(len(bars), 1)
        dates = [bar.date for bar in bars]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(1200 < bars[-1].close < 1300, bars[-1].close)               # 茅台真实量级
        self.assertTrue(4000 < index_bars[-1].close < 5000, index_bars[-1].close)   # 沪深300 点位量级

    def test_live_sectors_and_breadth(self):
        provider = TencentProvider(self.settings)
        try:
            rows = provider.sectors(5)
            breadth = provider.breadth()
        except DataSourceError as exc:
            self.skipTest("腾讯板块不可用：%s" % exc)
        self.assertTrue(rows)
        self.assertTrue(all(item.name for item in rows))
        self.assertGreater(breadth.total, 1000)                 # 行业板块汇总口径（含新三板）
        self.assertGreater(breadth.total_amount_yi, 1000.0)     # 两市成交额（亿元）
# --------------------------------------------------------------------------- 降级链
class _FailProvider:
    """所有方法都失败的假数据源。"""

    def __init__(self, exc=None, name="failing"):
        self.name = name
        self.exc = exc or DataSourceError("模拟故障")
        self.calls = []

    def _boom(self, method, *args, **kwargs):
        self.calls.append(method)
        raise self.exc

    def latest_quotes(self, symbols):
        return self._boom("latest_quotes")

    def index_quotes(self, symbols):
        return self._boom("index_quotes")

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        return self._boom("kline")

    def sectors(self, limit=20):
        return self._boom("sectors")

    def breadth(self):
        return self._boom("breadth")

    def resolve_name(self, symbol):
        return self._boom("resolve_name")

    def health(self):
        return {"ok": False, "detail": "模拟故障", "latency_ms": 0}


class _FlakyProvider:
    """先成功后失败的假主源（用于验证「主源异常 → 回落缓存」）。"""

    name = "flaky"

    def __init__(self, source_name="sina"):
        self.name = source_name
        self.fail = False

    def _value(self, *args, **kwargs):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return None

    def latest_quotes(self, symbols):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return [Quote(code=sym.normalize(item), name="主源", price=1234.5, prev_close=1200.0,
                      change=34.5, change_pct=2.87, volume_wan=1.0, amount_yi=2.0,
                      ts="2026-09-21 15:00:00", source=self.name) for item in symbols]

    def index_quotes(self, symbols):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return [IndexQuote(code=sym.normalize(item), name="主源指数", point=3900.0,
                           prev_close=3880.0, change=20.0, change_pct=0.52, source=self.name)
                for item in symbols]

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return [Bar(date="2026-09-21", open=1.0, high=2.0, low=0.5, close=1.5,
                    volume_wan=1.0, amount_yi=0.1) for _ in range(min(days, 3))]

    def sectors(self, limit=20):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return [SectorQuote(code="BK1", name="主源板块", change_pct=1.0)]

    def breadth(self):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return MarketBreadth(up=3000, down=2000, flat=100, total=5100, total_amount_yi=9000.0,
                             source=self.name)

    def resolve_name(self, symbol):
        if self.fail:
            raise DataSourceError("模拟主源故障")
        return "主源名称"

    def health(self):
        return {"ok": not self.fail, "detail": "flaky", "latency_ms": 0}


class CompositeFallbackTest(unittest.TestCase):
    """降级链：主源正常 / 主源异常（缓存、过期缓存）/ 无缓存 → 示例。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()
        self.sample = SampleProvider(self.settings)
        self.sina = _FlakyProvider("sina")
        self.tencent = _StubTencent(self.settings)      # 固定样本，不依赖网络
        self.eastmoney = _FlakyProvider("eastmoney")
        self.ths = _FlakyProvider("ths")                # 新增源同样用替身：测试绝不联网
        self.composite = CompositeProvider(self.settings, providers={
            "sina": self.sina, "tencent": self.tencent, "eastmoney": self.eastmoney,
            "ths": self.ths, "file": None,
            "csv": CsvProvider(self.settings), "sample": self.sample,
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fail_all_primary(self):
        self.sina.fail = True
        self.eastmoney.fail = True
        self.tencent.fail = True
        self.ths.fail = True
    def test_primary_ok_meta(self):
        quotes = self.composite.latest_quotes(["600519.SH"])
        self.assertEqual(len(quotes), 1)
        meta = self.composite.last_meta
        self.assertEqual(meta.source, "sina")
        self.assertFalse(meta.offline)
        self.assertFalse(meta.stale)
        self.assertEqual(quotes[0].source, "sina")
        # 主源成功的结果已写入磁盘缓存
        self.assertGreaterEqual(self.composite.cache.stats()["entries"], 1)

    def test_sina_disabled_falls_back_to_tencent(self):
        """验收点 3：禁用新浪（并模拟东财被阻断）后，快照 / K 线 / 板块都落到腾讯。"""
        self.sina.fail = True                                        # 新浪故障
        quotes = self.composite.latest_quotes(["600519.SH"])
        self.assertEqual(self.composite.last_meta.source, "tencent")
        self.assertFalse(self.composite.last_meta.offline)           # 仍是真实源，不算离线
        self.assertAlmostEqual(quotes[0].price, 1252.57, places=2)
        self.assertEqual(quotes[0].source, "tencent")
        self.eastmoney.fail = True                                   # 模拟东财被网络阻断
        bars = self.composite.kline("600519.SH", 2)                  # 历史链：eastmoney→tencent
        self.assertEqual(self.composite.last_meta.source, "tencent")
        self.assertFalse(self.composite.last_meta.offline)
        self.assertAlmostEqual(bars[-1].close, 1252.57, places=2)
        self.assertTrue(self.composite.sectors(2))                   # 板块链同样落到腾讯
        self.assertEqual(self.composite.last_meta.source, "tencent")
        breadth = self.composite.breadth()
        self.assertEqual(self.composite.last_meta.source, "tencent")
        self.assertGreater(breadth.total, 0)
        self.assertTrue(any("腾讯行业板块口径" in note for note in self.composite.last_meta.notes))

    def test_primary_failure_falls_back_to_cache(self):
        self.composite.latest_quotes(["600519.SH"])       # 先成功一次，落缓存
        self._fail_all_primary()
        quotes = self.composite.latest_quotes(["600519.SH"])
        meta = self.composite.last_meta
        self.assertEqual(meta.source, "cache")
        self.assertTrue(meta.offline)                     # 已降级为本地数据
        self.assertFalse(meta.stale)                      # 缓存未过期
        self.assertAlmostEqual(quotes[0].price, 1234.5, places=2)

    def test_expired_cache_returns_stale(self):
        self.composite.latest_quotes(["600519.SH"])
        key = self.composite._key("quotes", ["600519.SH"])
        entry = self.composite.cache.get_entry(key)
        self.assertIsNotNone(entry)
        self.composite.cache.set(key, entry["value"], 0)  # 立刻过期
        self._fail_all_primary()
        quotes = self.composite.latest_quotes(["600519.SH"])
        meta = self.composite.last_meta
        self.assertEqual(meta.source, "cache")
        self.assertTrue(meta.offline)
        self.assertTrue(meta.stale)
        self.assertAlmostEqual(quotes[0].price, 1234.5, places=2)

    def test_no_cache_falls_back_to_sample(self):
        self._fail_all_primary()
        quotes = self.composite.latest_quotes(["600519.SH"])
        meta = self.composite.last_meta
        self.assertEqual(meta.source, "sample")
        self.assertTrue(meta.offline)
        self.assertTrue(quotes)
        self.assertTrue(all(item.source == "sample" for item in quotes))
        self.assertTrue(any("失败" in note or "异常" in note for note in meta.notes))

    def test_csv_before_sample(self):
        settings, tmp = _tmp_settings(data_source="auto")
        try:
            csv_dir = settings.csv_dir
            os.makedirs(csv_dir, exist_ok=True)
            with open(os.path.join(csv_dir, "quotes.csv"), "w", encoding="utf-8") as fh:
                fh.write("code,name,price,prev_close,volume,amount\n")
                fh.write("600519.SH,本地茅台,1000.0,990.0,100000,100000000\n")
            sample = SampleProvider(settings)
            composite = CompositeProvider(settings, providers={
                "sina": _FailProvider(), "tencent": _FailProvider(), "eastmoney": _FailProvider(),
                "ths": _FailProvider(), "file": None,
                "csv": CsvProvider(settings), "sample": sample,
            })
            quotes = composite.latest_quotes(["600519.SH"])
            self.assertEqual(composite.last_meta.source, "csv")
            self.assertTrue(composite.last_meta.offline)
            self.assertEqual(quotes[0].name, "本地茅台")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kline_chain_and_validation(self):
        bars = self.composite.kline("600519.SH", 30)
        self.assertEqual(self.composite.last_meta.source, "eastmoney")
        self.assertEqual(len(bars), 3)
        self.assertRegex(self.composite.last_meta.as_of, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self._fail_all_primary()
        cached = self.composite.kline("600519.SH", 30)
        self.assertEqual(self.composite.last_meta.source, "cache")
        sample_bars = self.composite.kline("600519.SH", 15)     # 不同 days → 缓存未命中 → 示例
        self.assertEqual(self.composite.last_meta.source, "sample")
        self.assertEqual(len(sample_bars), 15)
        with self.assertRaises(ValidationError):
            self.composite.kline("600519.SH", 30, freq="hour")
        with self.assertRaises(SymbolNotFound):
            self.composite.kline("bad-code", 30)

    def test_sectors_breadth_never_500(self):
        self._fail_all_primary()
        self.assertTrue(self.composite.sectors(5))
        self.assertEqual(self.composite.last_meta.source, "sample")
        breadth = self.composite.breadth()
        self.assertGreater(breadth.total, 0)
        self.assertEqual(self.composite.last_meta.source, "sample")
        self.assertTrue(self.composite.last_meta.offline)

    def test_resolve_name_chain(self):
        self.assertEqual(self.composite.resolve_name("600519.SH"), "主源名称")
        self.composite.latest_quotes(["600519.SH"])       # 便于后面命中缓存
        self.assertEqual(self.composite.resolve_name("000001.SH"), "上证指数")  # 内置指数表
        self.assertEqual(self.composite.resolve_name("bad"), None)

    def test_describe_and_sources(self):
        described = self.composite.describe()
        self.assertEqual(described["provider"], "composite")
        self.assertEqual(described["mode"], "auto")
        self.assertEqual(described["realtime_chain"],
                         ["sina", "tencent", "eastmoney", "ths", "cache", "csv", "sample"])
        self.assertEqual(described["history_chain"],
                         ["eastmoney", "tencent", "sina", "ths", "cache", "csv", "sample"])
        self.assertEqual(described["sectors_chain"],
                         ["eastmoney", "tencent", "cache", "csv", "sample"])
        self.assertEqual(described["breadth_chain"],
                         ["eastmoney", "tencent", "cache", "csv", "sample"])
        self.assertEqual(self.composite.sources(),
                         ["sina", "tencent", "eastmoney", "ths", "file", "cache", "csv", "sample"])
        self.assertIn("sources", described)
        self.assertIn("cache", described)
        # cache 是内部兜底层：必须如实报告，不能显示成「构造失败的数据源」
        cache_entry = described["sources"]["cache"]
        self.assertEqual(cache_entry["kind"], "internal")
        self.assertTrue(cache_entry["enabled"])
        for key in ("entries", "hits", "stale_hits"):
            self.assertIn(key, cache_entry)
        self.assertNotIn("available", cache_entry)
        self.assertNotIn("health", cache_entry)
        self.assertEqual(described["sources"]["sina"]["kind"], "provider")
        health = self.composite.health()
        self.assertIn("ok", health)
        self.assertIn("detail", health)
        self.assertIn("latency_ms", health)

    def test_protocol_shape(self):
        for method in ("latest_quotes", "kline", "index_quotes", "sectors",
                       "breadth", "resolve_name", "health"):
            self.assertTrue(callable(getattr(self.composite, method, None)), method)


class OfflineAndDisabledNetworkTest(unittest.TestCase):
    """QUANTSTUDIO_OFFLINE=1 与断网（超时极小）场景。"""

    def test_offline_never_touches_network(self):
        settings, tmp = _tmp_settings(offline=True)
        try:
            sample = SampleProvider(settings)
            composite = CompositeProvider(settings, providers={
                "sina": SinaProvider(settings), "tencent": TencentProvider(settings),
                "eastmoney": EastmoneyProvider(settings),
                "csv": CsvProvider(settings), "sample": sample,
            })
            quotes = composite.latest_quotes(["600519.SH"])
            self.assertEqual(composite.last_meta.source, "sample")
            self.assertTrue(composite.last_meta.offline)
            self.assertTrue(quotes)
            self.assertTrue(composite.sectors(3))
            self.assertGreater(composite.breadth().total, 0)
            self.assertTrue(composite.kline("600519.SH", 20))
            health = composite.health()
            self.assertTrue(health["ok"])                      # 本地兜底可用
            self.assertIn("离线模式", health["detail"])
            self.assertFalse(health["sources"]["sina"]["health"]["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_timeout_falls_back_with_offline_meta(self):
        # 把超时压到 1ms：真实源必然失败，但不允许抛异常
        settings, tmp = _tmp_settings(http_timeout=0.001, http_retries=0)
        try:
            sample = SampleProvider(settings)
            composite = CompositeProvider(settings, providers={
                "sina": SinaProvider(settings), "tencent": TencentProvider(settings),
                "eastmoney": EastmoneyProvider(settings),
                "csv": CsvProvider(settings), "sample": sample,
            })
            quotes = composite.latest_quotes(["600519.SH"])
            meta = composite.last_meta
            self.assertTrue(quotes)
            self.assertTrue(meta.offline)
            self.assertIn(meta.source, ("sample", "cache"))
            self.assertTrue(any("失败" in note for note in meta.notes), meta.notes)
            self.assertTrue(composite.kline("600519.SH", 10))
            self.assertTrue(composite.last_meta.offline)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FactoryTest(unittest.TestCase):
    """工厂与单例。"""

    def test_build_by_data_source(self):
        settings, tmp = _tmp_settings()
        try:
            self.assertIsInstance(build_provider(settings), CompositeProvider)
            self.assertIsInstance(build_provider(_with(settings, data_source="sample")), SampleProvider)
            self.assertIsInstance(build_provider(_with(settings, data_source="csv")), CsvProvider)
            self.assertIsInstance(build_provider(_with(settings, data_source="eastmoney")), EastmoneyProvider)
            sina_provider = build_provider(_with(settings, data_source="sina"))
            self.assertIsInstance(sina_provider, CompositeProvider)
            self.assertEqual(sina_provider.name, "sina")
            self.assertEqual(sina_provider.describe()["realtime_chain"][0], "sina")
            self.assertIsInstance(sina_provider.providers["sina"], SinaProvider)
            self.assertIsInstance(sina_provider.providers["eastmoney"], EastmoneyProvider)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_provider_singleton(self):
        settings, tmp = _tmp_settings(data_source="sample")
        try:
            reset_provider()
            first = get_provider(settings)
            self.assertIs(first, get_provider(settings))
            other = get_provider(_with(settings, data_source="csv"))
            self.assertIsNot(first, other)
            reset_provider()
            self.assertIsNot(get_provider(settings), other)
        finally:
            reset_provider()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_tencent_mode(self):
        """data_source=tencent（用 settings_with_source 派生，config.py 的允许列表不在数据层职责内）。"""
        settings, tmp = _tmp_settings()
        try:
            tencent_provider = build_provider(settings_with_source(settings, "tencent"))
            self.assertIsInstance(tencent_provider, CompositeProvider)
            self.assertEqual(tencent_provider.name, "composite")
            self.assertEqual(tencent_provider.mode, "tencent")
            self.assertEqual(tencent_provider.describe()["realtime_chain"][0], "tencent")
            self.assertEqual(tencent_provider.describe()["history_chain"][0], "tencent")
            self.assertEqual(tencent_provider.describe()["sectors_chain"][0], "tencent")
            self.assertIsInstance(tencent_provider.providers["tencent"], TencentProvider)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SampleConsistencyTest(unittest.TestCase):
    """任务 2 的验收点：同一代码任意 ``days`` 下，同一日期的价格必须完全相同。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings(data_source="sample")
        self.provider = SampleProvider(self.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_consistent(self, code):
        short = self.provider.kline(code, 100)
        mid = self.provider.kline(code, 300)
        long_series = self.provider.kline(code, 800)
        self.assertEqual(len(short), 100)
        self.assertEqual(len(mid), 300)
        self.assertEqual(len(long_series), 800)
        self.assertEqual([bar.date for bar in long_series[-100:]], [bar.date for bar in short])
        for left, right, other in zip(short, mid[-100:], long_series[-100:]):
            self.assertEqual(left.date, right.date)
            self.assertEqual(left.date, other.date)
            self.assertEqual(left.close, right.close, left.date)
            self.assertEqual(left.close, other.close, left.date)
            self.assertEqual(left.open, other.open, left.date)
            self.assertEqual(left.high, other.high, left.date)
            self.assertEqual(left.low, other.low, left.date)
            self.assertEqual(left.volume_wan, other.volume_wan, left.date)
        again = self.provider.kline(code, 100)
        self.assertEqual([bar.to_dict() for bar in again], [bar.to_dict() for bar in short])

    def test_days_independent_for_stock_index_and_pool_code(self):
        for code in ("600519.SH", "000300.SH", "000001.SH", "999001.SH"):
            self._assert_consistent(code)

    def test_last_close_matches_snapshot(self):
        for code in ("600519.SH", "000300.SH", "999001.SH"):
            bars = self.provider.kline(code, 50)
            quote = self.provider.latest_quotes([code])[0]
            self.assertAlmostEqual(bars[-1].close, quote.price, places=2, msg=code)
            self.assertAlmostEqual(bars[-1].volume_wan, quote.volume_wan, places=2, msg=code)

    def test_index_scale_is_point_like(self):
        for code, low, high in (("000300.SH", 3000, 6000), ("399001.SZ", 8000, 20000),
                                ("000001.SH", 2500, 6000)):
            bars = self.provider.kline(code, 300)
            self.assertTrue(low < bars[-1].close < high, (code, bars[-1].close))

    def test_series_starts_not_before_2023(self):
        bars = self.provider.kline("600519.SH", 5000)      # days 超过序列长度 → 返回整条序列
        self.assertGreaterEqual(bars[0].date, "2023-01-01")
        self.assertEqual(bars[-1].date, self.provider._series_dates()[-1])
        self.assertGreaterEqual(len(bars), 250)            # 至少覆盖 1 年交易日，够回测用

    def test_index_quotes_consistent_with_series(self):
        for code in ("000001.SH", "399001.SZ", "399006.SZ", "000688.SH"):
            item = self.provider.index_quotes([code])[0]
            bars = self.provider.kline(code, 10)
            self.assertAlmostEqual(item.point, bars[-1].close, places=2, msg=code)
            self.assertAlmostEqual(item.prev_close, bars[-2].close, places=2, msg=code)

    def test_weekly_monthly_are_consistent_suffix(self):
        weekly = self.provider.kline("000300.SH", 10, freq="week")
        weekly_again = self.provider.kline("000300.SH", 40, freq="week")
        self.assertEqual([bar.date for bar in weekly_again[-10:]], [bar.date for bar in weekly])
        self.assertEqual([bar.close for bar in weekly_again[-10:]], [bar.close for bar in weekly])

def _with(settings, **kwargs):
    """在既有 Settings 上派生一份新配置（避免依赖环境变量）。"""
    params = {
        "data_dir": settings.data_dir,
        "cache_dir": settings.cache_dir,
        "data_source": kwargs.get("data_source", settings.data_source),
        "offline": kwargs.get("offline", settings.offline),
        "http_timeout": settings.http_timeout,
        "http_retries": settings.http_retries,
    }
    params.update(kwargs)
    return Settings(**params)



# --------------------------------------------------------------------------- 盘口 / 逐笔（离线）
def _tx_quote_fields(name, code, price, prev_close, bids, asks, outer=0, inner=0,
                     ts="20260924161444"):
    """按腾讯快照 ``~`` 字段顺序拼一行（9-18 买档、19-28 卖档、30 时间）。"""
    fields = ["1", name, code, "%.2f" % price, "%.2f" % prev_close, "%.2f" % price, "0",
              str(outer), str(inner)]
    for bid_price, bid_volume in bids:
        fields += ["%.2f" % bid_price, str(bid_volume)]
    for ask_price, ask_volume in asks:
        fields += ["%.2f" % ask_price, str(ask_volume)]
    return fields + ["", ts]


def _tx_quote_line(code, fields):
    return 'v_%s="%s";' % (code, "~".join(fields))


def _tx_tick_row(seq, clock, price, side, hands=1):
    """腾讯逐笔一行：序号/时间/价格/涨跌/手数/金额/方向。"""
    return "%d/%s/%.2f/0.00/%d/%d/%s" % (seq, clock, price, hands, int(price * hands * 100), side)


def _tx_tick_page(rows):
    return 'v_detail_data_sh600519=[0,"%s"];' % "|".join(rows)


class _StubTencentL2(TencentProvider):
    """覆写 ``_fetch`` 注入假 HTTP（腾讯盘口 / 逐笔共用，绝不联网）。"""

    def __init__(self, settings=None, body=b"", charset="", pages=None):
        TencentProvider.__init__(self, settings)
        self._body = body
        self._charset = charset
        self.pages = pages or {}
        self.requests = []

    def _fetch(self, url, referer="", params=None, headers=None):
        self.requests.append({"url": url, "params": dict(params or {}), "referer": referer})
        if params and "p" in params:
            return self.pages.get(str(int(params["p"])), "").encode("gbk", "ignore"), ""
        return self._body, self._charset


class TencentOrderBookTest(unittest.TestCase):
    """腾讯五档盘口：GBK 解码 / 买一卖一 / 外盘内盘 / 时间戳（全部离线）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _provider(self, lines, charset="GBK"):
        body = ("\n".join(lines) + "\n").encode("gbk")
        return _StubTencentL2(self.settings, body=body, charset=charset)

    def test_fields(self):
        fields = _tx_quote_fields(
            "贵州茅台", "600519", 1237.0, 1251.24,
            bids=[(1237.00, 13), (1236.95, 4), (1236.85, 1), (1236.83, 1), (1236.51, 2)],
            asks=[(1237.05, 1), (1237.50, 1), (1237.70, 1), (1237.90, 1), (1237.97, 1)],
            outer=13918, inner=17321)
        provider = self._provider([_tx_quote_line("sh600519", fields)], charset="")   # 无 charset 头
        book = provider.orderbook("600519.SH")
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(book.name, "贵州茅台")              # GBK 解码正确
        self.assertEqual(book.source, "tencent")
        self.assertAlmostEqual(book.price, 1237.0, places=2)
        self.assertAlmostEqual(book.prev_close, 1251.24, places=2)
        self.assertEqual(book.ts, "2026-09-24 16:14:44")     # 索引 30 的 YYYYMMDDHHMMSS
        self.assertEqual(book.levels, 5)
        self.assertEqual((len(book.bids), len(book.asks)), (5, 5))
        self.assertAlmostEqual(book.bids[0].price, 1237.0, places=3)
        self.assertEqual(book.bids[0].volume, 13)            # 单位：手
        self.assertAlmostEqual(book.bids[0].amount, 1237.0 * 13 * 100, places=2)
        self.assertEqual(book.bids[4].volume, 2)
        self.assertAlmostEqual(book.asks[0].price, 1237.05, places=3)
        self.assertEqual(book.outer_volume, 13918)
        self.assertEqual(book.inner_volume, 17321)
        request = provider.requests[0]
        self.assertTrue(request["url"].endswith("q=sh600519"), request["url"])
        self.assertEqual(request["referer"], TencentProvider.referer)

    def test_all_zero_or_empty_raises(self):
        zero = _tx_quote_fields("贵州茅台", "600519", 0.0, 0.0,
                                bids=[(0.0, 0)] * 5, asks=[(0.0, 0)] * 5)
        with self.assertRaises(ProviderUnavailable):
            self._provider([_tx_quote_line("sh600519", zero)]).orderbook("600519.SH")
        with self.assertRaises(ProviderUnavailable):
            self._provider(['v_sh600519="";']).orderbook("600519.SH")
        with self.assertRaises(SymbolNotFound):
            self._provider(['v_sh600519="";']).orderbook("bad-code")


class TencentTicksTest(unittest.TestCase):
    """腾讯逐笔：多页组装 / limit 截断 / 空页到底 / 升序返回（全部离线）。"""

    #: 4 页 × 3 条 = 12 条（第 4 页起为空 → 到底）
    TIMES = ["09:25:02", "09:30:00", "09:31:11", "09:32:00", "09:33:29", "09:34:02",
             "09:35:15", "09:36:01", "09:37:11", "09:38:04", "09:39:20", "09:40:59"]
    SIDES = ("B", "S", "M")

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()
        self.pages = {}
        for page in range(4):
            rows = []
            for offset in range(3):
                seq = page * 3 + offset
                rows.append(_tx_tick_row(seq, self.TIMES[seq], 1250.0 - seq * 0.5,
                                         self.SIDES[seq % 3], hands=seq + 1))
            self.pages[str(page)] = _tx_tick_page(rows)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _provider(self):
        return _StubTencentL2(self.settings, pages=self.pages)

    def test_ascending_assembly_and_limit_truncation(self):
        provider = self._provider()
        ticks = provider.ticks("600519.SH", limit=5)
        self.assertEqual(len(ticks), 5)                      # 不超过 limit
        self.assertEqual([tick.time for tick in ticks], self.TIMES[-5:])
        self.assertEqual([tick.side for tick in ticks],
                         ["sell", "neutral", "buy", "sell", "neutral"])
        pages_requested = [int(item["params"]["p"]) for item in provider.requests
                           if item["params"]]
        self.assertIn(4, pages_requested)                    # 探测到首个空页（第 4 页）
        self.assertEqual(max(pages_requested), 4)            # 空页即停，不继续向后扫

    def test_limit_larger_than_available_returns_all(self):
        provider = self._provider()
        ticks = provider.ticks("600519.SH", limit=999)
        self.assertEqual(len(ticks), 12)
        self.assertEqual([tick.time for tick in ticks], self.TIMES)
        pages_requested = [int(item["params"]["p"]) for item in provider.requests
                           if item["params"]]
        self.assertEqual(min(pages_requested), 0)            # 翻到第 0 页即停（无负页码）

    def test_page_request_shape(self):
        provider = self._provider()
        provider.ticks("600519.SH", limit=3)
        first = provider.requests[0]
        self.assertTrue(first["url"].startswith(TencentProvider.TICKS_URL))
        self.assertEqual(first["params"]["appn"], "detail")
        self.assertEqual(first["params"]["action"], "data")
        self.assertEqual(first["params"]["c"], "sh600519")
        self.assertEqual(first["params"]["p"], 0)
        self.assertEqual(first["referer"], TencentProvider.referer)

    def test_all_pages_empty_or_bad_limit(self):
        provider = _StubTencentL2(self.settings, pages={})
        with self.assertRaises(ProviderUnavailable):
            provider.ticks("600519.SH", limit=140)
        with self.assertRaises(ValidationError):
            provider.ticks("600519.SH", limit="bad")


def _sina_book_fields(name, open_, prev, last, bids, asks, day="2026-09-24", clock="14:59:00"):
    """按新浪 ``list=`` 字段顺序拼一行（10 起买档、20 起卖档、30 日期 / 31 时间）。"""
    fields = [name, "%.3f" % open_, "%.3f" % prev, "%.3f" % last, "%.3f" % last, "%.3f" % last,
              "%.3f" % bids[0][0], "%.3f" % asks[0][0], "12345600", "987654321.0"]
    for price, shares in bids:
        fields += [str(shares), "%.3f" % price]
    for price, shares in asks:
        fields += [str(shares), "%.3f" % price]
    return fields + [day, clock, "00"]


class _StubSinaL2(SinaProvider):
    """覆写 ``_fetch`` 注入假 HTTP（新浪盘口，绝不联网）。"""

    def __init__(self, settings=None, body=b"", charset="", fail=False):
        SinaProvider.__init__(self, settings)
        self._body = body
        self._charset = charset
        self.fail = fail
        self.requests = []

    def _fetch(self, url, referer="", params=None, headers=None):
        self.requests.append({"url": url, "referer": referer})
        if self.fail:
            raise DataSourceError("模拟新浪网络故障")
        return self._body, self._charset


class SinaOrderBookTest(unittest.TestCase):
    """新浪五档盘口：股 → 手换算 / Referer / 空响应（全部离线）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fields(self):
        fields = _sina_book_fields(
            "贵州茅台", 1250.0, 1251.24, 1237.0,
            bids=[(1237.0, 1300), (1236.95, 400), (1236.85, 100), (1236.83, 300), (1236.51, 200)],
            asks=[(1237.05, 100), (1237.50, 500), (1237.70, 100), (1237.90, 100), (1237.97, 200)])
        text = _sina_line("sh600519", fields)
        provider = _StubSinaL2(self.settings, body=text.encode("gbk"))
        book = provider.orderbook("600519.SH")
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(book.name, "贵州茅台")
        self.assertEqual(book.source, "sina")
        self.assertEqual(book.levels, 5)
        self.assertAlmostEqual(book.price, 1237.0, places=3)
        self.assertAlmostEqual(book.prev_close, 1251.24, places=3)
        self.assertEqual(book.ts, "2026-09-24 14:59:00")
        self.assertEqual((len(book.bids), len(book.asks)), (5, 5))
        self.assertAlmostEqual(book.bids[0].price, 1237.0, places=3)
        self.assertEqual(book.bids[0].volume, 13)            # 1300 股 → 13 手
        self.assertAlmostEqual(book.bids[0].amount, 1237.0 * 1300, places=2)
        self.assertEqual(book.bids[3].volume, 3)             # 300 股 → 3 手
        self.assertAlmostEqual(book.asks[1].price, 1237.5, places=3)
        self.assertEqual(book.outer_volume, 0)               # 新浪快照没有外盘 / 内盘
        request = provider.requests[0]
        self.assertTrue(request["url"].endswith("list=sh600519"), request["url"])
        self.assertEqual(request["referer"], SinaProvider.referer)

    def test_empty_or_failed_raises_provider_unavailable(self):
        empty = _StubSinaL2(self.settings, body=_sina_line("sh600519", []).encode("gbk"))
        with self.assertRaises(ProviderUnavailable):
            empty.orderbook("600519.SH")
        broken = _StubSinaL2(self.settings, fail=True)
        with self.assertRaises(ProviderUnavailable):
            broken.orderbook("600519.SH")


def _sample_book(source="tencent", price=1237.0):
    return OrderBook(
        code="600519.SH", name="贵州茅台", price=price, prev_close=1251.24,
        ts="2026-09-24 14:59:00", source=source, levels=5,
        bids=[OrderBookLevel(price=price - 0.01, volume=13,
                             amount=round((price - 0.01) * 13 * 100, 2))],
        asks=[OrderBookLevel(price=price + 0.01, volume=7,
                             amount=round((price + 0.01) * 7 * 100, 2))],
        outer_volume=100, inner_volume=200,
    )


class _Level2Stub:
    """只有 orderbook / ticks 的替身源（注入 CompositeProvider 验证降级链）。"""

    def __init__(self, name="stub", book=None, ticks=None, exc=None):
        self.name = name
        self.book = book
        self.ticks_value = list(ticks or [])
        self.exc = exc
        self.calls = []

    def orderbook(self, code):
        self.calls.append(("orderbook", code))
        if self.exc:
            raise self.exc
        return self.book

    def ticks(self, code, limit=600):
        self.calls.append(("ticks", code, limit))
        if self.exc:
            raise self.exc
        return list(self.ticks_value)


class CompositeLevel2Test(unittest.TestCase):
    """CompositeProvider 的盘口 / 逐笔 / 资金流（注入替身源，全部离线）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()
        self.fail_exc = ProviderUnavailable("模拟盘口故障")
        self.tencent = _Level2Stub("tencent", book=_sample_book("tencent"))
        self.sina = _Level2Stub("sina", book=_sample_book("sina", price=1236.0))
        self.composite = CompositeProvider(self.settings, providers={
            "sina": self.sina,
            "tencent": self.tencent,
            "eastmoney": _Level2Stub("eastmoney", exc=self.fail_exc),
            "csv": CsvProvider(self.settings),
            "sample": SampleProvider(self.settings),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tencent_first(self):
        book = self.composite.orderbook("600519.SH")
        self.assertEqual(book.source, "tencent")
        self.assertEqual(self.composite.last_meta.source, "tencent")
        self.assertFalse(self.composite.last_meta.offline)
        self.assertEqual([call[0] for call in self.tencent.calls], ["orderbook"])
        self.assertEqual(self.sina.calls, [])                 # 腾讯成功 → 不碰新浪

    def test_falls_back_to_sina_when_tencent_fails(self):
        self.tencent.exc = self.fail_exc
        book = self.composite.orderbook("600519.SH")
        self.assertEqual(book.source, "sina")
        self.assertEqual(self.composite.last_meta.source, "sina")
        self.assertFalse(self.composite.last_meta.offline)
        self.assertEqual([call[0] for call in self.tencent.calls], ["orderbook"])
        self.assertEqual([call[0] for call in self.sina.calls], ["orderbook"])
        self.assertTrue(any("tencent.orderbook 失败" in note
                            for note in self.composite.last_meta.notes))

    def test_all_failed_falls_back_to_cache(self):
        first = self.composite.orderbook("600519.SH")
        self.tencent.exc = self.fail_exc
        self.sina.exc = self.fail_exc
        cached = self.composite.orderbook("600519.SH")
        self.assertEqual(self.composite.last_meta.source, "cache")
        self.assertTrue(self.composite.last_meta.offline)
        self.assertAlmostEqual(cached.price, first.price, places=3)
        self.assertIsInstance(cached.bids[0], OrderBookLevel)  # 嵌套档位也要还原
        self.assertAlmostEqual(cached.bids[0].amount, first.bids[0].amount, places=2)

    def test_all_failed_raises_data_source_error(self):
        self.tencent.exc = self.fail_exc
        self.sina.exc = self.fail_exc
        with self.assertRaises(DataSourceError):               # ProviderUnavailable 是其子类
            self.composite.orderbook("600519.SH")
        self.assertEqual(self.composite.last_meta.source, "unavailable")

    def test_ticks_and_capital_flow(self):
        self.tencent.ticks_value = [
            Tick(time="09:30:00", price=100.0, volume=15000, amount=1_500_000.0, side="buy"),
            Tick(time="09:31:00", price=100.0, volume=3000, amount=300_000.0, side="sell"),
            Tick(time="09:32:00", price=100.0, volume=600, amount=60_000.0, side="buy"),
            Tick(time="09:33:00", price=100.0, volume=100, amount=10_000.0, side="buy"),
        ]
        items = self.composite.ticks("600519.SH", 10)
        self.assertEqual(len(items), 4)
        self.assertEqual(self.composite.last_meta.source, "tencent")
        self.assertEqual([call[0] for call in self.tencent.calls], ["ticks"])
        flow = self.composite.capital_flow("600519.SH", 10)
        self.assertEqual(flow.code, "600519.SH")
        self.assertEqual(flow.source, "tencent")
        self.assertEqual(flow.ts, "09:33:00")                  # 取最新一条逐笔
        self.assertEqual(flow.tick_count, 4)
        self.assertAlmostEqual(flow.main_net, 1_200_000.0, places=2)   # 超大买 150 万 − 大卖 30 万
        self.assertEqual(flow.buckets["super_big"]["count"], 1)
        self.assertAlmostEqual(flow.buckets["big"]["net"], -300_000.0, places=2)


class Level2ExportTest(unittest.TestCase):
    """``data.level2`` 导出与能力协商（协议只加 docstring，不加必需方法）。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_module_export_and_capabilities(self):
        # 模块级 `from quantstudio.data import level2` 本身就是「导出契约」的断言
        self.assertTrue(callable(level2.parse_tencent_orderbook))
        self.assertTrue(callable(level2.compute_capital_flow))
        caps = level2.capabilities(TencentProvider(self.settings))
        self.assertTrue(caps["orderbook"])
        self.assertEqual(caps["orderbook_levels"], 5)
        self.assertTrue(caps["ticks"])
        self.assertFalse(caps["orders"])
        self.assertFalse(caps["queue"])
        sina_caps = level2.capabilities(SinaProvider(self.settings))
        self.assertTrue(sina_caps["orderbook"])
        self.assertFalse(sina_caps["ticks"])
        composite_caps = level2.capabilities(CompositeProvider(self.settings))
        self.assertTrue(composite_caps["orderbook"])
        self.assertTrue(composite_caps["ticks"])
        self.assertEqual(composite_caps["orderbook_levels"], 5)

    def test_data_provider_protocol_still_matches(self):
        from quantstudio.core.interfaces import DataProvider
        self.assertIsInstance(TencentProvider(self.settings), DataProvider)
        self.assertIsInstance(CompositeProvider(self.settings), DataProvider)

if __name__ == "__main__":
    unittest.main(verbosity=2)
