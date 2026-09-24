# -*- coding: utf-8 -*-
"""本地 Level-2 文件导入通道测试（``quantstudio.data.level2_import``，全部离线）。

用 ``tempfile`` 写样例 CSV，覆盖：

- 盘口解析：十档、买降卖升排序、``volume`` 单位手、``amount`` 缺省按 ``price × volume × 100`` 估算、
  ``levels`` 截断与实际档数；
- 逐笔解析：文件顺序保留、金额缺省估算、方向取值（``buy`` / ``sell`` / ``neutral`` / ``B`` / ``S`` / ``M``）；
- 宽容性：表头 / BOM / GBK、列顺序可变、坏行跳过、缺列、空文件、文件缺失不抛异常；
- :class:`Level2FileProvider`：文件名多种写法、默认目录（环境变量 / ``data_dir/level2``）、
  ``ticks`` 取时间升序的最后 N 条、缺文件抛 ``ProviderUnavailable``、``capabilities`` / ``health``；
- **结构兼容**：导入产物能被 ``level2.orderbook_summary`` / ``level2.compute_capital_flow``
  直接消费（与免费源产物同一套结构）。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.config import Settings                                    # noqa: E402
from quantstudio.core.errors import ProviderUnavailable                    # noqa: E402
from quantstudio.core.models import OrderBook, OrderBookLevel, Tick        # noqa: E402
from quantstudio.data import level2 as L                                   # noqa: E402
from quantstudio.data.level2_import import (                               # noqa: E402
    LEVEL2_DIR_ENV,
    Level2FileProvider,
    load_orderbook_csv,
    load_ticks_csv,
)

# --------------------------------------------------------------------------- 固定样本

ORDERBOOK_HEADER = "side,level,price,volume,amount"

# 十档盘口，行序故意打乱（含金额缺省行），用于验证排序与金额估算
ORDERBOOK_ROWS = [
    "ask,3,1237.70,1,123770",
    "bid,5,1236.51,2,247302",
    "bid,1,1237.00,13,1608100",
    "ask,1,1237.05,1,123705",
    "bid,7,1236.30,10,",
    "ask,10,1238.50,6,743100",
    "bid,3,1236.85,1,123685",
    "ask,6,1238.10,3,",
    "bid,2,1236.95,4,494780",
    "ask,4,1237.90,1,123790",
    "bid,10,1236.00,9,1112400",
    "ask,2,1237.50,1,123750",
    "bid,6,1236.40,7,",
    "ask,8,1238.30,5,619150",
    "bid,4,1236.83,1,123683",
    "ask,5,1237.97,1,123797",
    "bid,8,1236.20,11,1359820",
    "bid,9,1236.10,12,1483320",
    "ask,7,1238.20,2,247640",
    "ask,9,1238.40,4,495360",
]

TICKS_HEADER = "time,price,volume,amount,side"

# 十笔逐笔，文件里按时间**降序**（用于验证 load 保序 + provider 取最后 N 条）
TICKS_ROWS = [
    "09:30:11,1246.00,37,4616731,sell",
    "09:30:08,1248.88,267,,buy",
    "09:30:05,1249.49,364,45436615,S",
    "09:30:02,1250.08,75,9377425,neutral",
    "09:31:00,1249.00,10,1249000,M",
    "09:31:05,1248.50,20,,B",
    "09:31:09,1249.20,5,62460,sell",
    "09:31:12,1249.60,8,99968,buy",
    "09:31:15,1249.80,3,37494,neutral",
    "09:31:20,1250.10,12,1500120,sell",
]


class _FileTestCase(unittest.TestCase):
    """统一的临时目录 + 写文件辅助。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-l2-import-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(text.encode(encoding))
        return path

    def _orderbook_path(self, code="600519.SH", rows=None, header=ORDERBOOK_HEADER,
                        encoding="utf-8", bom=False):
        lines = ([header] if header else []) + list(rows if rows is not None else ORDERBOOK_ROWS)
        text = "\n".join(lines) + "\n"
        if bom:
            text = "\ufeff" + text
        return self._write("%s.orderbook.csv" % code, text, encoding)

    def _ticks_path(self, code="600519.SH", rows=None, header=TICKS_HEADER, encoding="utf-8"):
        lines = ([header] if header else []) + list(rows if rows is not None else TICKS_ROWS)
        return self._write("%s.ticks.csv" % code, "\n".join(lines) + "\n", encoding)


# --------------------------------------------------------------------------- 盘口解析

class OrderBookCsvTest(_FileTestCase):

    def test_parse_ten_levels_sorted_units_and_amount(self):
        path = self._orderbook_path(code="600519", header=None)     # 600519.orderbook.csv
        book = load_orderbook_csv(path)
        self.assertIsInstance(book, OrderBook)
        self.assertEqual(book.source, "file")
        self.assertEqual(book.levels, 10, "两侧各 10 档 → levels = 10")
        self.assertEqual(book.code, "600519.SH", "无 code 参数时从文件名反推")
        self.assertEqual(len(book.bids), 10)
        self.assertEqual(len(book.asks), 10)
        # 买盘价格降序、卖盘价格升序
        self.assertEqual([level.price for level in book.bids],
                         sorted([level.price for level in book.bids], reverse=True))
        self.assertEqual([level.price for level in book.asks],
                         sorted([level.price for level in book.asks]))
        self.assertEqual(book.bids[0].to_dict(),
                         {"price": 1237.0, "volume": 13, "amount": 1608100.0})
        self.assertEqual(book.asks[0].to_dict(),
                         {"price": 1237.05, "volume": 1, "amount": 123705.0})
        # volume 单位是手；amount 缺省按 价格 × 手数 × 100 估算
        # 买七 1236.30 × 10 手 → 1236.30 × 10 × 100 = 1,236,300
        self.assertEqual(book.bids[6].to_dict(),
                         {"price": 1236.3, "volume": 10, "amount": 1236300.0})
        # 卖六 1238.10 × 3 手 → 1238.10 × 3 × 100 = 371,430
        self.assertEqual(book.asks[5].to_dict(),
                         {"price": 1238.1, "volume": 3, "amount": 371430.0})

    def test_code_argument_is_normalized(self):
        path = self._orderbook_path()
        book = load_orderbook_csv(path, code="600519")
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(load_orderbook_csv(path, code="600519-SH").code, "600519.SH")
        self.assertEqual(load_orderbook_csv(path, code="sh600519").code, "600519.SH")

    def test_levels_argument_trims_each_side(self):
        path = self._orderbook_path()
        book = load_orderbook_csv(path, code="600519.SH", levels=5)
        self.assertEqual(book.levels, 5)
        self.assertEqual(len(book.bids), 5)
        self.assertEqual(len(book.asks), 5)
        self.assertAlmostEqual(book.bids[-1].price, 1236.51, places=3)
        self.assertAlmostEqual(book.asks[-1].price, 1237.97, places=3)
        # 非法 levels（0 / 负数 / 文字）→ 不截断
        self.assertEqual(load_orderbook_csv(path, levels=0).levels, 10)
        self.assertEqual(load_orderbook_csv(path, levels=-3).levels, 10)
        self.assertEqual(load_orderbook_csv(path, levels="十档").levels, 10)

    def test_header_bom_gbk_and_reordered_columns(self):
        reference = load_orderbook_csv(self._orderbook_path())
        # BOM + 大写列名
        upper = ORDERBOOK_HEADER.upper()
        with_bom = load_orderbook_csv(self._orderbook_path(rows=ORDERBOOK_ROWS, header=upper, bom=True))
        self.assertEqual(with_bom.to_dict(), reference.to_dict())
        # GBK + 中文注释行 + 表头
        gbk_rows = ["# 贵州茅台 十档盘口（用户导出）"] + ORDERBOOK_ROWS
        gbk = load_orderbook_csv(self._orderbook_path(rows=gbk_rows, encoding="gbk"))
        self.assertEqual(gbk.to_dict(), reference.to_dict())
        # 表头列顺序可变（按列名取列）
        rows = ["1,1237.00,bid,13,1608100", "1,1237.05,ask,1,123705"]
        reordered = load_orderbook_csv(
            self._orderbook_path(rows=rows, header="level,price,side,volume,amount"))
        self.assertEqual((len(reordered.bids), len(reordered.asks)), (1, 1))
        self.assertEqual(reordered.bids[0].to_dict(),
                         {"price": 1237.0, "volume": 13, "amount": 1608100.0})

    def test_bad_rows_are_skipped(self):
        rows = [
            "# 注释行",
            "",
            "bid,1,10.00,5,5000",
            "ask,1,10.01,3,3003",
            "hold,2,10.02,4,4008",          # 非法 side
            "bid,0,9.99,5,4995",            # level < 1
            "ask,2,abc,4,4008",             # 价格非法
            "bid,3,9.98,-4,3992",           # 数量非正
            "ask,,10.03,2,2006",            # level 缺失
            "bid,4,9.97,2",                 # 4 列，金额缺省 → 1994.00
            "ask",                          # 列数不足
            "bid,4,9.96,9,8964",            # 与上一行 (bid,4) 重复 → 丢弃
        ]
        book = load_orderbook_csv(self._orderbook_path(rows=rows))
        self.assertEqual([level.price for level in book.bids], [10.0, 9.97])
        self.assertEqual([level.price for level in book.asks], [10.01])
        self.assertEqual(book.levels, 2)
        self.assertEqual(book.bids[1].to_dict(),
                         {"price": 9.97, "volume": 2, "amount": 1994.0})

    def test_missing_required_column_returns_none(self):
        # 表头缺 volume → 无法解析
        path = self._orderbook_path(rows=["bid,1,1237.00,13"], header="side,level,price")
        self.assertIsNone(load_orderbook_csv(path))
        # 表头缺 side
        path = self._orderbook_path(rows=["1,1237.00,13"], header="level,price,volume")
        self.assertIsNone(load_orderbook_csv(path))

    def test_missing_empty_and_broken_file_returns_none(self):
        self.assertIsNone(load_orderbook_csv(os.path.join(self.tmp, "nope.orderbook.csv")))
        self.assertIsNone(load_orderbook_csv(self.tmp))
        empty = self._write("empty.orderbook.csv", "")
        self.assertIsNone(load_orderbook_csv(empty))
        blank = self._write("blank.orderbook.csv", "   \n\n# only comment\n")
        self.assertIsNone(load_orderbook_csv(blank))
        header_only = self._orderbook_path(rows=[])
        self.assertIsNone(load_orderbook_csv(header_only))
        self.assertIsNone(load_orderbook_csv(None))
        self.assertIsNone(load_orderbook_csv(""))


# --------------------------------------------------------------------------- 逐笔解析

class TicksCsvTest(_FileTestCase):

    def test_parse_keeps_file_order_and_estimates_amount(self):
        path = self._ticks_path(header=None)          # 600519.SH.ticks.csv 无表头
        ticks = load_ticks_csv(path, code="600519.SH")
        self.assertEqual(len(ticks), 10)
        self.assertTrue(all(isinstance(item, Tick) for item in ticks))
        # 保持文件顺序（文件里是降序，不做排序）
        self.assertEqual(ticks[0].time, "09:30:11")
        self.assertEqual(ticks[-1].time, "09:31:20")
        self.assertEqual([item.time for item in ticks],
                         [row.split(",")[0] for row in TICKS_ROWS])
        self.assertEqual(ticks[0].to_dict(),
                         {"time": "09:30:11", "price": 1246.0, "volume": 37,
                          "amount": 4616731.0, "side": "sell", "change": 0.0})
        # 金额缺省：1248.88 × 267 × 100 = 33,345,096.00
        self.assertEqual(ticks[1].amount, 33345096.0)
        self.assertEqual(ticks[1].side, "buy")
        # 大写方向 B / S / M（M → neutral）
        self.assertEqual([item.side for item in ticks],
                         ["sell", "buy", "sell", "neutral", "neutral",
                          "buy", "sell", "buy", "neutral", "sell"])

    def test_no_header_positional_and_side_in_fourth_column(self):
        rows = [
            "09:30:01,100.00,150,1500000,buy",
            "09:30:02,100.00,50",
            "09:30:03,100.00,10,sell",      # 第 4 列是方向词 → side，金额估算 100,000
            "9:25:00,99.90,7,,neutral",     # 时间补零
        ]
        ticks = load_ticks_csv(self._ticks_path(rows=rows, header=None))
        self.assertEqual(len(ticks), 4)
        self.assertEqual([item.side for item in ticks], ["buy", "neutral", "sell", "neutral"])
        self.assertEqual(ticks[2].amount, 100000.0)
        self.assertEqual(ticks[3].time, "09:25:00")
        self.assertEqual([item.volume for item in ticks], [150, 50, 10, 7])

    def test_datetime_time_and_reordered_header(self):
        rows = [
            "buy,2026-09-24 09:30:01,10,1250.00,1250000",
            "sell,2026-09-24 09:30:02,20,1250.10,",
        ]
        ticks = load_ticks_csv(self._ticks_path(rows=rows, header="side,time,volume,price,amount"))
        self.assertEqual([item.time for item in ticks], ["09:30:01", "09:30:02"])
        self.assertEqual([item.side for item in ticks], ["buy", "sell"])
        self.assertEqual(ticks[1].amount, 2500200.0)     # 1250.10 × 20 × 100

    def test_bad_rows_are_skipped(self):
        rows = [
            "",
            "# 注释",
            "09:30:01,100.00,10,100000,buy",
            "bad-time-line",
            "09:30:02,,10,100000,buy",      # 价格缺失
            "09:30:03,100.00,abc,1,buy",    # 数量非法
            "09:30:04,0,10,1,buy",          # 价格为 0
            "09:30:05,100.00,0,1,buy",      # 数量为 0
            ",100.00,10,1,buy",             # 时间缺失
            "09:30:06,100.00,10,-5,unknown",  # 金额非正 → 估算；方向未知 → neutral
        ]
        ticks = load_ticks_csv(self._ticks_path(rows=rows))
        self.assertEqual(len(ticks), 2)
        self.assertEqual([item.time for item in ticks], ["09:30:01", "09:30:06"])
        self.assertEqual(ticks[1].amount, 100000.0)
        self.assertEqual(ticks[1].side, "neutral")

    def test_missing_required_column_returns_empty(self):
        path = self._ticks_path(rows=["09:30:01,100.00"], header="time,price")
        self.assertEqual(load_ticks_csv(path), [])
        path = self._ticks_path(rows=["100.00,10,09:30:01"], header="price,volume,time")
        self.assertEqual(len(load_ticks_csv(path)), 1, "列序可变，time 存在即可")

    def test_missing_empty_and_broken_file_returns_empty(self):
        self.assertEqual(load_ticks_csv(os.path.join(self.tmp, "nope.ticks.csv")), [])
        self.assertEqual(load_ticks_csv(self.tmp), [])
        self.assertEqual(load_ticks_csv(self._write("empty.ticks.csv", "")), [])
        self.assertEqual(load_ticks_csv(self._ticks_path(rows=[])), [])
        self.assertEqual(load_ticks_csv(None), [])
        self.assertEqual(load_ticks_csv(""), [])


# --------------------------------------------------------------------------- Provider

class Level2FileProviderTest(_FileTestCase):

    def setUp(self):
        super(Level2FileProviderTest, self).setUp()
        self._orderbook_path(code="600519.SH")
        self._ticks_path(code="600519.SH")
        self.provider = Level2FileProvider(directory=self.tmp)

    def test_declared_attributes_and_capabilities(self):
        self.assertEqual(self.provider.name, "file")
        self.assertEqual(self.provider.ORDERBOOK_LEVELS, 10)
        self.assertTrue(self.provider.LEVEL2_IMPORT)
        caps = self.provider.capabilities()
        self.assertEqual(caps, {
            "orderbook": True, "orderbook_levels": 10, "ticks": True,
            "orders": False, "queue": False, "import": True,
            "detail": "10 档盘口、逐笔成交",
        })
        # 与免费源共用同一能力协商口径
        self.assertEqual(L.capabilities(self.provider), caps)

    def test_orderbook_reads_file(self):
        book = self.provider.orderbook("600519.SH")
        self.assertIsInstance(book, OrderBook)
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(book.source, "file")
        self.assertEqual(book.levels, 10)
        # 传入不同写法同样命中同一个文件
        for code in ("600519", "sh600519", "600519-SH", "600519.SH"):
            self.assertEqual(self.provider.orderbook(code).code, "600519.SH", code)

    def test_orderbook_file_name_variants(self):
        for name in ("600519.SH.orderbook.csv", "600519.orderbook.csv",
                     "sh600519.orderbook.csv", "600519-SH.orderbook.csv"):
            directory = os.path.join(self.tmp, name.replace(".csv", ""))
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("side,level,price,volume,amount\nbid,1,10.00,5,5000\nask,1,10.01,3,3003\n")
            book = Level2FileProvider(directory=directory).orderbook("600519.SH")
            self.assertEqual(book.bids[0].price, 10.0, name)
            self.assertEqual(book.asks[0].price, 10.01, name)

    def test_ticks_returns_last_n_in_time_ascending_order(self):
        ticks = self.provider.ticks("600519.SH", limit=3)
        self.assertEqual([item.time for item in ticks],
                         ["09:31:12", "09:31:15", "09:31:20"])
        self.assertEqual(ticks[0].amount, 99968.0)
        # 默认 limit=600：全部返回且按时间升序
        all_ticks = self.provider.ticks("600519.SH")
        self.assertEqual(len(all_ticks), 10)
        self.assertEqual([item.time for item in all_ticks],
                         sorted(item.time for item in all_ticks))
        self.assertEqual(all_ticks[0].time, "09:30:02")
        # limit 非法 → 回退 600；limit ≤ 0 → 空列表（文件本身存在）
        self.assertEqual(len(self.provider.ticks("600519.SH", limit=None)), 10)
        self.assertEqual(self.provider.ticks("600519.SH", limit=0), [])
        with self.assertRaises(ProviderUnavailable):
            self.provider.ticks("600519.HK")
        ticks_with_limit = self.provider.ticks("600519.SH", limit="2")
        self.assertEqual([item.time for item in ticks_with_limit], ["09:31:15", "09:31:20"])

    def test_missing_file_raises_provider_unavailable(self):
        empty_dir = os.path.join(self.tmp, "empty")
        os.makedirs(empty_dir, exist_ok=True)
        provider = Level2FileProvider(directory=empty_dir)
        with self.assertRaises(ProviderUnavailable):
            provider.orderbook("600519.SH")
        with self.assertRaises(ProviderUnavailable):
            provider.ticks("600519.SH")
        with self.assertRaises(ProviderUnavailable):
            provider.orderbook("000001.SZ")
        # 目录不存在同样抛 ProviderUnavailable（而不是 OSError）
        missing = Level2FileProvider(directory=os.path.join(self.tmp, "nope"))
        with self.assertRaises(ProviderUnavailable):
            missing.orderbook("600519.SH")
        with self.assertRaises(ProviderUnavailable):
            missing.ticks("600519.SH")

    def test_empty_file_raises_provider_unavailable(self):
        directory = os.path.join(self.tmp, "blank")
        os.makedirs(directory, exist_ok=True)
        for name in ("600519.SH.orderbook.csv", "600519.SH.ticks.csv"):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write("side,level,price,volume,amount\n")
        provider = Level2FileProvider(directory=directory)
        with self.assertRaises(ProviderUnavailable):
            provider.orderbook("600519.SH")
        with self.assertRaises(ProviderUnavailable):
            provider.ticks("600519.SH")

    def test_default_directory_env_then_data_dir(self):
        other = tempfile.mkdtemp(prefix="qts-l2-default-")
        try:
            settings = Settings(data_dir=other, cache_dir=os.path.join(other, "cache"))
            # 未设环境变量 → settings.data_dir/level2
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(LEVEL2_DIR_ENV, None)
                provider = Level2FileProvider(settings=settings)
                self.assertEqual(provider.directory, os.path.join(other, "level2"))
            # 环境变量优先
            with mock.patch.dict(os.environ, {LEVEL2_DIR_ENV: self.tmp}):
                provider = Level2FileProvider(settings=settings)
                self.assertEqual(provider.directory, self.tmp)
                self.assertEqual(provider.orderbook("600519.SH").code, "600519.SH")
            # 显式 directory 参数优先于环境变量
            explicit = Level2FileProvider(directory=other, settings=settings)
            self.assertEqual(explicit.directory, other)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_health_is_offline_and_reports_files(self):
        health = self.provider.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["source"], "file")
        self.assertIn("600519.SH.orderbook.csv", health["detail"])
        empty_dir = os.path.join(self.tmp, "empty2")
        os.makedirs(empty_dir, exist_ok=True)
        self.assertFalse(Level2FileProvider(directory=empty_dir).health()["ok"])
        self.assertFalse(Level2FileProvider(directory="").health()["ok"])


# --------------------------------------------------------------------------- 与免费源同一结构

class Level2StructureCompatibilityTest(_FileTestCase):

    def test_imported_orderbook_feeds_orderbook_summary(self):
        book = load_orderbook_csv(self._orderbook_path(code="600519.SH"), code="600519.SH")
        summary = L.orderbook_summary(book)
        # 委买 13+4+1+1+2+7+10+11+12+9 = 70 手；委卖 1×5 + 3+2+5+4+6 = 25 手
        self.assertEqual(summary["bid_volume"], 70)
        self.assertEqual(summary["ask_volume"], 25)
        # 委比 = (70-25)/(70+25) × 100 = 47.37%
        self.assertAlmostEqual(summary["imbalance_pct"], 47.37, places=2)
        self.assertAlmostEqual(summary["ratio"], 2.8, places=2)
        self.assertAlmostEqual(summary["spread"], 0.05, places=3)
        self.assertAlmostEqual(summary["mid"], 1237.025, places=3)
        self.assertGreater(summary["bid_amount"], 0.0)

    def test_imported_ticks_feed_compute_capital_flow(self):
        ticks = load_ticks_csv(self._ticks_path())
        self.assertEqual(len(ticks), 10)
        flow = L.compute_capital_flow(ticks, code="600519.SH", source="file")
        self.assertEqual(flow.tick_count, 10)
        self.assertEqual(flow.source, "file")
        self.assertGreater(flow.amount_total, 0.0)
        self.assertGreater(flow.buy_amount, 0.0)
        self.assertGreater(flow.sell_amount, 0.0)
        self.assertAlmostEqual(flow.net_amount, round(flow.buy_amount - flow.sell_amount, 2), places=2)
        self.assertEqual(set(flow.buckets), {"super_big", "big", "mid", "small"})
        self.assertEqual(L.flow_to_dict(flow)["buckets"]["super_big"]["label"], "超大单")

    def test_imported_ticks_feed_capital_flow_hand_checked(self):
        rows = [
            "09:30:01,100.00,150,1500000,buy",     # 150 万：超大单买
            "09:30:02,100.00,300,300000,sell",     # 30 万：大单卖
            "09:30:03,100.00,60,60000,buy",        # 6 万：中单买
        ]
        ticks = load_ticks_csv(self._ticks_path(rows=rows))
        flow = L.compute_capital_flow(ticks, code="600519.SH", source="file")
        self.assertAlmostEqual(flow.amount_total, 1860000.0, places=2)
        self.assertAlmostEqual(flow.main_net, 1200000.0, places=2)      # 150 万 − 30 万
        self.assertAlmostEqual(flow.main_net_pct,
                               round(1200000.0 / 1860000.0 * 100.0, 2), places=2)
        payload = L.flow_to_dict(flow)["buckets"]
        self.assertEqual(payload["super_big"]["count"], 1)
        self.assertAlmostEqual(payload["big"]["net"], -300000.0, places=2)
        self.assertAlmostEqual(payload["mid"]["buy"], 60000.0, places=2)

    def test_provider_output_is_consumable_directly(self):
        provider = Level2FileProvider(directory=self.tmp)
        self._orderbook_path(code="600519.SH")
        self._ticks_path(code="600519.SH")
        summary = L.orderbook_summary(provider.orderbook("600519.SH"))
        self.assertEqual(summary["bid_volume"], 70)
        ticks = provider.ticks("600519.SH", limit=5)
        self.assertEqual(len(ticks), 5)
        flow = L.compute_capital_flow(ticks, code="600519.SH")
        self.assertEqual(flow.tick_count, 5)
        self.assertIsInstance(Level2FileProvider(directory=self.tmp).orderbook("600519.SH").bids[0],
                              OrderBookLevel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
