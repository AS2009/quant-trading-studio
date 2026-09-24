# -*- coding: utf-8 -*-
"""L2 工具箱口径测试（``quantstudio.data.level2_tools``，全部离线）。

覆盖用户可感知的五个工具：大单追踪、封单/涨跌停、资金流分时、盘口扫描、排行。
期望值全部手算写死（不依赖实现），样本是合成的固定数据 + 一个真实结构（涨跌停按板块推断）。
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.core.models import OrderBook, OrderBookLevel, Quote, Tick  # noqa: E402
from quantstudio.data import level2_tools as LT  # noqa: E402


def _tick(time, price, volume, amount, side):
    return Tick(time=time, price=price, volume=volume, amount=amount, side=side)


# 四笔：150 万买 / 30.03 万卖 / 250.5 万买 / 9.99 万中性（总额 440.52 万）
TICKS = [
    _tick("09:30:05", 100.00, 1500, 1_500_000.0, "buy"),
    _tick("09:30:40", 100.10, 300, 300_300.0, "sell"),
    _tick("09:31:02", 100.20, 2500, 2_505_000.0, "buy"),
    _tick("09:31:20", 99.90, 100, 99_900.0, "neutral"),
]


def _book(price, prev_close, bids=(), asks=(), code="600519.SH"):
    return OrderBook(code=code, name="测试股", price=price, prev_close=prev_close,
                     bids=[OrderBookLevel(price=p, volume=v, amount=round(p * v * 100, 2))
                           for p, v in bids],
                     asks=[OrderBookLevel(price=p, volume=v, amount=round(p * v * 100, 2))
                           for p, v in asks],
                     levels=max(len(bids), len(asks)))


class TestLimitPct(unittest.TestCase):

    def test_by_board(self):
        cases = {
            "600519.SH": (0.10, "主板"),
            "000001.SZ": (0.10, "主板"),
            "300750.SZ": (0.20, "创业板"),
            "301269.SZ": (0.20, "创业板"),
            "688981.SH": (0.20, "科创板"),
            "830799.BJ": (0.30, "北交所"),
            "430418.BJ": (0.30, "北交所"),
        }
        for code, (pct, keyword) in cases.items():
            got_pct, text = LT.limit_pct_for(code)
            self.assertAlmostEqual(got_pct, pct, places=4, msg=code)
            self.assertIn(keyword, text, code)
        # ST 无法从代码判断：按主板 10% 并在说明里提示核对
        self.assertIn("ST", LT.limit_pct_for("600519.SH")[1])
        self.assertAlmostEqual(LT.limit_pct_for(None)[0], 0.10, places=4)


class TestBigOrders(unittest.TestCase):

    def test_filter_threshold_order_and_side(self):
        rows = LT.filter_big_orders(TICKS, 1_000_000.0)
        self.assertEqual([row["time"] for row in rows], ["09:31:02", "09:30:05"], "倒序：最新在前")
        self.assertEqual(rows[0]["bucket"], "super_big")
        self.assertEqual(rows[0]["bucket_label"], "超大单")
        # 阈值降到 20 万 → 三笔（中性那笔只有 9.99 万仍被排除）
        self.assertEqual(len(LT.filter_big_orders(TICKS, 200_000.0)), 3)
        self.assertEqual(len(LT.filter_big_orders(TICKS, 200_000.0, sides=("buy",))), 2)
        self.assertEqual(len(LT.filter_big_orders(TICKS, 200_000.0, sides=("sell",))), 1)
        self.assertEqual(len(LT.filter_big_orders(TICKS, 1_000_000.0, limit=1)), 1)
        self.assertEqual(LT.filter_big_orders([], 1_000_000.0), [])
        # 非法阈值退回默认（100 万）
        self.assertEqual(len(LT.filter_big_orders(TICKS, "abc")), 2)

    def test_summary_hand_checked(self):
        summary = LT.big_orders_summary(TICKS, 1_000_000.0)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["buy_count"], 2)
        self.assertEqual(summary["sell_count"], 0)
        self.assertAlmostEqual(summary["buy_amount"], 4_005_000.0, places=2)
        self.assertAlmostEqual(summary["net_amount"], 4_005_000.0, places=2)
        self.assertAlmostEqual(summary["buy_amount_pct"], 100.0, places=2)
        # 占样本成交额 = 400.5 万 / 440.52 万 = 90.92%
        self.assertAlmostEqual(summary["amount_share_pct"], 90.92, places=2)
        self.assertEqual(summary["biggest"]["time"], "09:31:02")
        self.assertAlmostEqual(summary["biggest"]["amount"], 2_505_000.0, places=2)


class TestMinuteFlow(unittest.TestCase):

    def test_grouping_and_cumulative(self):
        series = LT.minute_flow(TICKS)
        self.assertEqual([row["time"] for row in series], ["09:30", "09:31"])
        first, second = series
        self.assertAlmostEqual(first["buy"], 1_500_000.0, places=2)
        self.assertAlmostEqual(first["sell"], 300_300.0, places=2)
        self.assertAlmostEqual(first["net"], 1_199_700.0, places=2)
        self.assertAlmostEqual(first["cum_net"], 1_199_700.0, places=2)
        self.assertEqual(first["count"], 2)
        self.assertAlmostEqual(second["net"], 2_505_000.0, places=2)
        # 累计 = 前一分钟 + 本分钟（中性那笔不计方向）
        self.assertAlmostEqual(second["cum_net"], 3_704_700.0, places=2)
        self.assertAlmostEqual(second["amount"], 2_604_900.0, places=2)
        self.assertEqual(LT.minute_flow([]), [])

    def test_amount_falls_back_to_price_volume(self):
        rows = LT.minute_flow([Tick(time="09:30:01", price=10.0, volume=200, side="buy")])
        self.assertAlmostEqual(rows[0]["buy"], 200_000.0, places=2)   # 10 × 200 × 100


class TestSealStatus(unittest.TestCase):

    def test_limit_up_with_seal(self):
        book = _book(110.0, 100.0, bids=[(110.0, 20_000), (109.9, 100)])
        seal = LT.seal_status(book, amount_total=500_000_000.0)
        self.assertEqual(seal["state"], "limit_up")
        self.assertEqual(seal["label"], "涨停")
        self.assertAlmostEqual(seal["limit_up_price"], 110.0, places=2)
        self.assertAlmostEqual(seal["limit_down_price"], 90.0, places=2)
        self.assertEqual(seal["seal_volume"], 20_000)
        self.assertAlmostEqual(seal["seal_amount"], 220_000_000.0, places=2)
        # 封成比 = 2.2 亿 / 5 亿 = 44%
        self.assertAlmostEqual(seal["seal_ratio"], 44.0, places=2)
        self.assertAlmostEqual(seal["distance_pct"], 0.0, places=2)

    def test_limit_up_without_seal_on_best_bid(self):
        # 价格涨停但买一不是涨停价（涨停价上没有挂单）→ 认涨停但不给封单
        book = _book(110.0, 100.0, bids=[(109.5, 500)])
        seal = LT.seal_status(book)
        self.assertEqual(seal["state"], "limit_up")
        self.assertEqual(seal["seal_volume"], 0)
        self.assertAlmostEqual(seal["seal_amount"], 0.0, places=2)

    def test_limit_down_uses_best_ask(self):
        book = _book(90.0, 100.0, asks=[(90.0, 5_000)])
        seal = LT.seal_status(book)
        self.assertEqual(seal["state"], "limit_down")
        self.assertEqual(seal["seal_volume"], 5_000)
        self.assertAlmostEqual(seal["seal_amount"], 45_000_000.0, places=2)

    def test_normal_distance_to_limit_up(self):
        book = _book(100.0, 100.0, bids=[(100.0, 10)], asks=[(100.1, 10)])
        seal = LT.seal_status(book)
        self.assertEqual(seal["state"], "normal")
        self.assertAlmostEqual(seal["distance_pct"], 10.0, places=2)   # 涨停价 110，距离 10%

    def test_missing_data(self):
        self.assertEqual(LT.seal_status(None)["state"], "unknown")
        self.assertEqual(LT.seal_status(_book(0.0, 0.0))["state"], "unknown")
        # 科创板 20% 口径：涨停价 = 昨收 × 1.2
        book = _book(12.0, 10.0, bids=[(12.0, 100)], code="688981.SH")
        seal = LT.seal_status(book)
        self.assertEqual(seal["state"], "limit_up")
        self.assertAlmostEqual(seal["limit_up_price"], 12.0, places=2)
        self.assertIn("20%", seal["limit_pct_text"])


class TestScanAndRank(unittest.TestCase):

    def test_scan_book_features(self):
        book = _book(100.0, 99.0, bids=[(100.0, 300), (99.9, 100)], asks=[(100.1, 100)])
        row = LT.scan_book(book)
        self.assertEqual(row["code"], "600519.SH")
        self.assertAlmostEqual(row["change_pct"], 1.01, places=2)
        self.assertEqual(row["bid_volume"], 400)
        self.assertEqual(row["ask_volume"], 100)
        self.assertAlmostEqual(row["imbalance_pct"], 60.0, places=2)   # (400-100)/500
        self.assertEqual(row["seal_state"], "normal")
        self.assertEqual(LT.scan_book(None), {})

    def test_scan_book_uses_quote_fields(self):
        book = _book(100.0, 99.0, bids=[(100.0, 300)], asks=[(100.1, 100)])
        quote = Quote(code="600519.SH", name="贵州茅台", price=100.0, prev_close=99.0,
                      change_pct=1.25, volume_ratio=2.5)
        row = LT.scan_book(book, quote=quote)
        self.assertAlmostEqual(row["change_pct"], 1.25, places=2)
        self.assertAlmostEqual(row["volume_ratio"], 2.5, places=2)

    def test_rank_rows(self):
        rows = [
            {"code": "A", "main_net": 1_000_000.0},
            {"code": "B", "main_net": -500_000.0},
            {"code": "C", "main_net": 3_000_000.0},
            {"code": "D"},                                  # 缺字段按 0
        ]
        self.assertEqual([row["code"] for row in LT.rank_rows(rows)], ["C", "A", "D", "B"])
        self.assertEqual([row["code"] for row in LT.rank_rows(rows, top=2)], ["C", "A"])
        self.assertEqual([row["code"] for row in LT.rank_rows(rows, reverse=False)][:2], ["B", "D"])
        self.assertEqual(LT.rank_rows([{"code": "A", "main_net": "bad"}]), [{"code": "A", "main_net": "bad"}])
        self.assertEqual(LT.rank_rows(None), [])


class TestPriceDistribution(unittest.TestCase):

    def test_buckets_and_weights(self):
        rows = LT.price_distribution(TICKS, bins=3)
        self.assertEqual(len(rows), 3)
        total_volume = sum(row["volume"] for row in rows)
        self.assertEqual(total_volume, 1500 + 300 + 2500 + 100)
        # 金额守恒
        self.assertAlmostEqual(sum(row["amount"] for row in rows),
                               sum(tick.amount for tick in TICKS), places=2)
        # 每档区间连续且升序
        for previous, current in zip(rows, rows[1:]):
            self.assertLessEqual(previous["low"], current["low"])
        self.assertEqual(LT.price_distribution([], 3), [])
        self.assertEqual(LT.price_distribution([Tick(time="x", price=0.0, volume=0)], 3), [])

    def test_single_price_returns_one_bucket(self):
        rows = LT.price_distribution([Tick(time="09:30", price=10.0, volume=100, amount=100_000.0)], 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["volume"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
