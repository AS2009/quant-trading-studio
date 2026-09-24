# -*- coding: utf-8 -*-
"""盘口 / 逐笔 / 资金流口径测试（``quantstudio.data.level2``，全部离线）。

固定样本取自真实公开接口的一次快照（腾讯 ``qt.gtimg.cn`` 快照与 ``appn=detail`` 逐笔、
新浪 ``hq.sinajs.cn`` 快照），因此解析口径与线上一致；期望值手算写死，不依赖实现。

覆盖：
- 腾讯 / 新浪五档盘口解析（含**单位差异**：腾讯是手、新浪是股）；
- 逐笔成交解析（``序号/时间/价格/涨跌/手数/金额/方向``）与多空统计；
- 分档口径（≥100 万 超大单 / 20–100 万 大单 / 5–20 万 中单 / <5 万 小单）与主力净额；
- 盘口派生指标（委比 / 委差 / 价差 / 中间价）与能力协商；
- 非法、空、截断输入一律不抛异常。
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.core.models import CapitalFlow, OrderBook, OrderBookLevel, Tick  # noqa: E402
from quantstudio.data import level2 as L  # noqa: E402

# --------------------------------------------------------------------------- 真实样本

# 腾讯快照：买一 1237.00 × 13 手、卖一 1237.05 × 1 手，外盘 13918 / 内盘 17321（单位：手）
TENCENT_BOOK = (
    'v_sh600519="1~贵州茅台~600519~1237.00~1251.24~1250.01~31239~13918~17321~'
    '1237.00~13~1236.95~4~1236.85~1~1236.83~1~1236.51~2~'
    '1237.05~1~1237.50~1~1237.70~1~1237.90~1~1237.97~1~~'
    '20260924161444~-14.24~-1.14~1256.13~1231.05~1237.00/31239/3867310920~31239~386731~0.25~18.99"'
)

# 新浪快照：同一天同一只票，档位数量单位是**股**（买一 1337 股 = 13 手）
SINA_BOOK = (
    'var hq_str_sh600519="贵州茅台,1250.010,1251.240,1237.000,1256.130,1231.050,1237.000,1237.050,'
    '3123935,3867310920.000,1337,1237.000,400,1236.950,100,1236.850,100,1236.830,200,1236.510,'
    '100,1237.050,100,1237.500,100,1237.700,100,1237.900,100,1237.970,2026-09-24,15:34:59,00,'
    'D|600|742200.00";'
)

# 腾讯逐笔：集合竞价 + 早盘数笔（方向 B/S/M、涨跌相对上一笔）
TENCENT_TICKS = (
    'v_detail_data_sh600519=[0,"0/09:25:02/1250.01/0.00/183/22911433/S'
    '|1/09:30:02/1250.08/0.07/75/9377425/M'
    '|2/09:30:05/1249.49/-0.59/364/45436615/S'
    '|3/09:30:08/1248.88/-0.61/267/33320381/M'
    '|4/09:30:11/1246.00/-2.88/37/4616731/S'
    '|5/09:30:14/1248.87/2.87/32/3994582/B"]'
)


class TestTencentOrderBook(unittest.TestCase):

    def test_parse_hand_checked(self):
        book = L.parse_tencent_orderbook(TENCENT_BOOK)
        self.assertIsInstance(book, OrderBook)
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(book.name, "贵州茅台")
        self.assertEqual(book.price, 1237.00)
        self.assertEqual(book.prev_close, 1251.24)
        self.assertEqual(book.levels, 5)
        self.assertEqual(book.source, "tencent")
        self.assertEqual(book.ts, "2026-09-24 16:14:44")
        # 外盘 / 内盘（手）
        self.assertEqual(book.outer_volume, 13918)
        self.assertEqual(book.inner_volume, 17321)
        # 买一 1237.00 × 13 手 → 金额 = 1237 × 13 × 100 = 1,608,100 元
        self.assertEqual(book.bids[0].to_dict(),
                         {"price": 1237.0, "volume": 13, "amount": 1608100.0})
        self.assertEqual(book.asks[0].to_dict(),
                         {"price": 1237.05, "volume": 1, "amount": 123705.0})
        # 买五 1236.51 × 2 手；档位价格降序 / 升序
        self.assertEqual(book.bids[-1].price, 1236.51)
        self.assertEqual([level.price for level in book.bids],
                         sorted([level.price for level in book.bids], reverse=True))
        self.assertEqual([level.price for level in book.asks],
                         sorted([level.price for level in book.asks]))

    def test_summary_hand_checked(self):
        book = L.parse_tencent_orderbook(TENCENT_BOOK)
        summary = L.orderbook_summary(book)
        # 委买 13+4+1+1+2 = 21 手；委卖 1×5 = 5 手
        self.assertEqual(summary["bid_volume"], 21)
        self.assertEqual(summary["ask_volume"], 5)
        # 委比 = (21-5)/(21+5) × 100 = 61.54%
        self.assertAlmostEqual(summary["imbalance_pct"], 61.54, places=2)
        self.assertAlmostEqual(summary["ratio"], 4.2, places=2)
        self.assertAlmostEqual(summary["spread"], 0.05, places=3)
        self.assertAlmostEqual(summary["mid"], 1237.025, places=3)

    def test_broken_input_returns_none(self):
        for raw in ("", "   ", 'v_sh600519="1~2~3"', None, 12345, "v_sh600519="):
            self.assertIsNone(L.parse_tencent_orderbook(raw), repr(raw))


class TestSinaOrderBook(unittest.TestCase):

    def test_unit_is_shares_and_converted_to_hands(self):
        book = L.parse_sina_orderbook(SINA_BOOK)
        self.assertEqual(book.code, "600519.SH")
        self.assertEqual(book.name, "贵州茅台")
        self.assertEqual(book.source, "sina")
        self.assertEqual(book.levels, 5)
        self.assertEqual(book.ts, "2026-09-24 15:34:59")
        # 买一 1337 股 → 13 手；金额按股数如实计算 1237.00 × 1337 = 1,653,869 元
        self.assertEqual(book.bids[0].volume, 13)
        self.assertAlmostEqual(book.bids[0].amount, 1653869.0, places=2)
        # 买五 100 股 → 1 手
        # 买五 200 股 → 2 手
        self.assertEqual(book.bids[-1].volume, 2)
        # 新浪不给外盘 / 内盘
        self.assertEqual(book.outer_volume, 0)
        self.assertEqual(book.inner_volume, 0)

    def test_gbk_bytes_and_odd_lot(self):
        gbk = SINA_BOOK.encode("gbk")
        book = L.parse_sina_orderbook(gbk)
        self.assertEqual(book.bids[0].volume, 13, "GBK 字节流应解析出同样结果")
        # 不足 1 手的挂单按 1 手（0 手会被上层当作无效档位丢掉）
        odd = SINA_BOOK.replace(",1337,1237.000", ",37,1237.000")
        self.assertEqual(L.parse_sina_orderbook(odd).bids[0].volume, 1)

    def test_broken_input_returns_none(self):
        for raw in ("", "garbage", 'var hq_str_sh600519="";', None):
            self.assertIsNone(L.parse_sina_orderbook(raw), repr(raw))


class TestTicks(unittest.TestCase):

    def test_parse_hand_checked(self):
        ticks = L.parse_tencent_ticks(TENCENT_TICKS)
        self.assertEqual(len(ticks), 6)
        first = ticks[0]
        self.assertEqual(first.time, "09:25:02")
        self.assertEqual(first.price, 1250.01)
        self.assertEqual(first.volume, 183)
        self.assertEqual(first.amount, 22911433.0)
        self.assertEqual(first.side, "sell")
        self.assertEqual(first.change, 0.0)
        self.assertEqual([tick.side for tick in ticks],
                         ["sell", "neutral", "sell", "neutral", "sell", "buy"])
        self.assertEqual(ticks[-1].side, "buy")
        self.assertAlmostEqual(ticks[-1].change, 2.87, places=2)

    def test_broken_rows_are_skipped(self):
        raw = ('v_detail_data_sh600519=[0,"0/09:25:02/1250.01/0.00/183/22911433/S'
               '|bad-row|1/09:30:02/0/0.07/75/0/M|2/09:30:05/1249.49/-0.59/0/0/S'
               '|3/09:30:08/1248.88/-0.61/267/33320381/B"]')
        ticks = L.parse_tencent_ticks(raw)
        self.assertEqual(len(ticks), 2, "价格或数量为 0 的行应跳过")
        self.assertEqual(ticks[-1].side, "buy")
        self.assertEqual(L.parse_tencent_ticks(""), [])
        self.assertEqual(L.parse_tencent_ticks(None), [])

    def test_summarize_ticks(self):
        stats = L.summarize_ticks(L.parse_tencent_ticks(TENCENT_TICKS))
        self.assertEqual(stats["count"], 6)
        # 买 32 手；卖 183+364+37 = 584 手；中性 75+267 = 342 手
        self.assertEqual(stats["buy_volume"], 32)
        self.assertEqual(stats["sell_volume"], 584)
        self.assertEqual(stats["neutral_volume"], 342)
        total = 32 + 584 + 342
        self.assertAlmostEqual(stats["buy_volume_pct"], round(32 / total * 100, 2), places=2)
        self.assertLess(stats["net_amount"], 0, "样本里主动卖多于主动买")


class TestCapitalFlow(unittest.TestCase):

    def test_bucket_boundaries(self):
        cases = ((1_000_000.0, "super_big"), (999_999.99, "big"), (200_000.0, "big"),
                 (199_999.99, "mid"), (50_000.0, "mid"), (49_999.99, "small"), (0.0, "small"),
                 (-1.0, "small"), (None, "small"))
        for amount, expected in cases:
            self.assertEqual(L.classify_bucket(amount), expected, repr(amount))

    def test_compute_flow_hand_checked(self):
        # 三笔：150 万买（超大单）、30 万卖（大单）、6 万买（中单）
        ticks = [
            Tick(time="09:30:01", price=100.0, volume=150, amount=1_500_000.0, side="buy"),
            Tick(time="09:30:02", price=100.0, volume=300, amount=300_000.0, side="sell"),
            Tick(time="09:30:03", price=100.0, volume=60, amount=60_000.0, side="buy"),
        ]
        flow = L.compute_capital_flow(ticks, code="600000.SH", name="浦发银行", source="tencent")
        self.assertIsInstance(flow, CapitalFlow)
        self.assertEqual(flow.tick_count, 3)
        self.assertAlmostEqual(flow.amount_total, 1_860_000.0, places=2)
        self.assertAlmostEqual(flow.buy_amount, 1_560_000.0, places=2)
        self.assertAlmostEqual(flow.sell_amount, 300_000.0, places=2)
        self.assertAlmostEqual(flow.net_amount, 1_260_000.0, places=2)
        # 主力净额 = 超大单净额(1,500,000) + 大单净额(-300,000) = 1,200,000
        self.assertAlmostEqual(flow.main_net, 1_200_000.0, places=2)
        self.assertAlmostEqual(flow.main_net_pct, round(1_200_000.0 / 1_860_000.0 * 100.0, 2), places=2)
        payload = L.flow_to_dict(flow)["buckets"]
        self.assertEqual(payload["super_big"]["label"], "超大单")
        self.assertEqual(payload["super_big"]["count"], 1)
        self.assertAlmostEqual(payload["big"]["net"], -300_000.0, places=2)
        self.assertAlmostEqual(payload["mid"]["buy"], 60_000.0, places=2)
        self.assertAlmostEqual(payload["small"]["count"], 0, places=2)
        self.assertAlmostEqual(payload["small"]["buy_pct"], 0.0, places=2)

    def test_flow_direction_ignores_neutral(self):
        ticks = [Tick(time="09:30:01", price=10.0, volume=100, amount=100_000.0, side="neutral")]
        flow = L.compute_capital_flow(ticks)
        self.assertAlmostEqual(flow.amount_total, 100_000.0, places=2)
        self.assertAlmostEqual(flow.buy_amount, 0.0, places=2)
        self.assertAlmostEqual(flow.sell_amount, 0.0, places=2)
        self.assertAlmostEqual(flow.main_net, 0.0, places=2)
        self.assertEqual(L.flow_to_dict(None), {})

    def test_amount_falls_back_to_price_volume(self):
        # 源没给金额时用 价格 × 手数 × 100 估算（100.0 × 30 × 100 = 300,000）
        flow = L.compute_capital_flow([Tick(time="09:30:01", price=100.0, volume=30, side="buy")])
        self.assertAlmostEqual(flow.amount_total, 300_000.0, places=2)
        self.assertEqual(flow.buckets["big"]["count"], 1)


class TestCapabilities(unittest.TestCase):

    class _Free(object):
        def orderbook(self):
            """5 档"""

        def ticks(self):
            """逐笔"""

    class _Paid(object):
        ORDERBOOK_LEVELS = 10
        LEVEL2_IMPORT = True

        def orderbook(self):
            """10 档"""

        def ticks(self):
            """逐笔"""

        def order_events(self):
            """逐笔委托"""

        def order_queue(self):
            """委托队列"""

    def test_free_source(self):
        caps = L.capabilities(self._Free())
        self.assertTrue(caps["orderbook"])
        self.assertEqual(caps["orderbook_levels"], 5)
        self.assertTrue(caps["ticks"])
        self.assertFalse(caps["orders"])
        self.assertFalse(caps["queue"])
        self.assertFalse(caps["import"])
        self.assertIn("5 档盘口", caps["detail"])

    def test_paid_source(self):
        caps = L.capabilities(self._Paid())
        self.assertEqual(caps["orderbook_levels"], 10)
        self.assertTrue(caps["orders"] and caps["queue"] and caps["import"])
        self.assertIn("逐笔委托", caps["detail"])

    def test_empty_source(self):
        caps = L.capabilities(object())
        self.assertFalse(caps["orderbook"])
        self.assertEqual(caps["orderbook_levels"], 0)
        self.assertEqual(caps["detail"], "无盘口级能力")

    def test_levels_fallback_on_bad_declaration(self):
        class _Weird(object):
            ORDERBOOK_LEVELS = "十档"

            def orderbook(self):
                """盘口"""

        self.assertEqual(L.capabilities(_Weird())["orderbook_levels"], 5)


class TestModels(unittest.TestCase):

    def test_to_dict_roundtrip(self):
        level = OrderBookLevel(price=10.0, volume=100, amount=100000.0)
        book = OrderBook(code="600000.SH", bids=[level], asks=[level], levels=1)
        payload = book.to_dict()
        self.assertEqual(payload["code"], "600000.SH")
        self.assertEqual(payload["bids"][0]["volume"], 100)
        self.assertEqual(payload["levels"], 1)
        tick = Tick(time="09:30:00", price=10.0, volume=100, side="buy")
        self.assertEqual(tick.to_dict()["side"], "buy")
        self.assertEqual(CapitalFlow(code="600000.SH").to_dict()["buckets"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
