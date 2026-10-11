# -*- coding: utf-8 -*-
"""模拟盘涨跌停约束（修复 #2a 任务 A）单元测试：口径必须与回测 ``SimulatedBroker`` 一致。

规则：最新价相对 ``quote.prev_close`` 的涨跌幅 ``>= 9.8%`` 视为涨停（拒绝买入）、
``<= -9.8%`` 视为跌停（拒绝卖出）；封板方向不可成交，反向仍可成交；
``prev_close`` 缺失或非正时跳过检查，不因此拒单。

运行：``cd backend && python -m unittest discover -s tests -p "test_paper_limits.py" -v``
零第三方依赖（仅标准库），不触网（行情由 fake provider 注入）。
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from quantstudio.backtest.broker import DEFAULT_LIMIT_PCT, SimulatedBroker        # noqa: E402
from quantstudio.core.errors import DataSourceError                              # noqa: E402
from quantstudio.core.models import Bar, OrderRequest, Quote                     # noqa: E402
from quantstudio.trading import PaperBroker                                      # noqa: E402


class FakeQuoteProvider:
    """固定行情假数据源：``price`` / ``prev_close`` 可在测试中随时修改（不触网）。"""

    name = "fake-limits"

    def __init__(self, price=10.0, prev_close=10.0, label="测试股"):
        self.price = float(price)
        self.prev_close = float(prev_close)
        self.label = label
        self.fail = False

    def latest_quotes(self, symbols):
        if self.fail:
            raise DataSourceError("行情源不可用（测试注入）")
        return [
            Quote(
                code=str(code),
                name=self.label,
                price=self.price,
                prev_close=self.prev_close,
                ts="2026-09-21 10:00:00",
                source="fake",
            )
            for code in symbols
        ]

    def resolve_name(self, symbol):
        return self.label


class Clock:
    """可推进的测试时钟（T+1 解禁）。"""

    def __init__(self, day="2026-09-21"):
        self.day = day

    def __call__(self):
        return self.day + " 10:30:00"

    def set(self, day):
        self.day = day


class PaperLimitTestCase(unittest.TestCase):
    """任务 A：|涨跌幅| >= 9.8% 视为封板 —— 涨停拒买、跌停拒卖（与回测同口径）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-limits-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_broker(self, price=10.0, prev_close=10.0, cash=1_000_000.0, day="2026-09-21"):
        self.provider = FakeQuoteProvider(price=price, prev_close=prev_close)
        self.clock = Clock(day)
        self.broker = PaperBroker(
            provider=self.provider,
            account_path=os.path.join(self.tmp, "paper_account.json"),
            initial_cash=cash,
            slippage_bps=0.0,
            clock=self.clock,
        )
        return self.broker

    def set_quote(self, price, prev_close, day=None):
        """换一个行情快照（可同时推进到次一交易日）。"""
        self.provider.price = float(price)
        self.provider.prev_close = float(prev_close)
        if day:
            self.clock.set(day)

    # ① 涨停拒买

    def test_limit_up_rejects_buy_and_reports_pct(self):
        broker = self.make_broker(price=11.0, prev_close=10.0)              # +10.00% 封板
        order = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(order.status, "rejected")
        self.assertIn("涨停", order.reason)
        self.assertIn("封板", order.reason)
        self.assertIn("+10.00%", order.reason)                              # 理由含具体涨跌幅
        self.assertEqual(broker.fills(), [])                                # 未产生成交
        self.assertEqual(broker.account().cash, 1_000_000.0)
        self.assertEqual([item.order_id for item in broker.orders()], [order.order_id])

        # 恰好 9.8% 也算封板（判定是 >= 阈值）
        broker2 = self.make_broker(price=10.98, prev_close=10.0)            # +9.80%
        exact = broker2.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(exact.status, "rejected")
        self.assertIn("涨停", exact.reason)

    def test_just_below_limit_pct_is_tradable(self):
        broker = self.make_broker(price=10.97, prev_close=10.0)             # +9.70%，未封板
        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")
        self.assertAlmostEqual(buy.avg_price, 10.97, places=6)

    # ② 跌停拒卖

    def test_limit_down_rejects_sell_and_reports_pct(self):
        broker = self.make_broker(price=10.0, prev_close=10.0)
        self.assertEqual(broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100)).status, "filled")

        self.set_quote(price=9.0, prev_close=10.0, day="2026-09-22")        # -10.00% 跌停
        sell = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sell.status, "rejected")
        self.assertIn("跌停", sell.reason)
        self.assertIn("-10.00%", sell.reason)
        self.assertEqual(len(broker.fills()), 1)                            # 只有买入那一笔成交
        self.assertEqual(broker.fills()[0].side, "buy")
        position = broker.positions()[0]
        self.assertEqual(position.qty, 100)                                 # 持仓未被卖出
        self.assertEqual(position.available_qty, 100)

        # 恰好 -9.8% 也算封板
        self.set_quote(price=9.02, prev_close=10.0)
        exact = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(exact.status, "rejected")
        self.assertIn("跌停", exact.reason)

    def test_just_above_limit_down_is_tradable(self):
        broker = self.make_broker(price=10.0, prev_close=10.0)
        broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.set_quote(price=9.03, prev_close=10.0, day="2026-09-22")       # -9.70%，未封板
        sold = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sold.status, "filled")
        self.assertAlmostEqual(sold.avg_price, 9.03, places=6)

    # ③ 方向性：涨停可卖、跌停可买（与回测一致）

    def test_limit_up_allows_sell(self):
        broker = self.make_broker(price=10.0, prev_close=10.0)
        broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.set_quote(price=11.0, prev_close=10.0, day="2026-09-22")       # 涨停
        sold = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sold.status, "filled")
        self.assertAlmostEqual(sold.avg_price, 11.0, places=6)

    def test_limit_down_allows_buy(self):
        broker = self.make_broker(price=9.0, prev_close=10.0)               # -10.00% 跌停
        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")
        self.assertAlmostEqual(buy.avg_price, 9.0, places=6)

    # ④ 边界：缺前收盘不拒单；缺行情仍走原拒单路径

    def test_missing_prev_close_skips_limit_check(self):
        broker = self.make_broker(price=11.0, prev_close=0.0)               # 无前收盘（+10% 形状）
        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")                              # 跳过检查，不因此拒单

        self.set_quote(price=11.0, prev_close=-1.0, day="2026-09-22")       # 前收盘非正
        sold = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sold.status, "filled")

    def test_missing_quote_still_rejected_by_reference_price(self):
        broker = self.make_broker(price=10.0, prev_close=10.0)
        self.provider.fail = True                                           # 行情不可用
        rejected = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("最新价", rejected.reason)                            # 原路径行为不变
        self.assertNotIn("封板", rejected.reason)

    # ⑤ 与回测口径逐项对齐

    def test_threshold_and_behavior_match_backtest(self):
        self.assertEqual(PaperBroker.limit_pct, DEFAULT_LIMIT_PCT)
        self.assertEqual(SimulatedBroker().limit_pct, DEFAULT_LIMIT_PCT)

        sim = SimulatedBroker()
        # 引擎在撮合前逐日 track_bars：第二根 bar 的回调会记下第一根的收盘价作为前收盘
        sim.track_bars("2026-09-19", {
            "600519.SH": Bar(date="2026-09-19", open=10.0, high=10.0, low=10.0, close=10.0),
        })
        sim.track_bars("2026-09-20", {
            "600519.SH": Bar(date="2026-09-20", open=10.0, high=10.0, low=10.0, close=10.0),
        })
        self.assertAlmostEqual(sim.prev_close("600519.SH"), 10.0, places=6)
        up = Bar(date="2026-09-21", open=11.0, high=11.0, low=11.0, close=11.0)
        down = Bar(date="2026-09-21", open=9.0, high=9.0, low=9.0, close=9.0)
        self.assertTrue(sim.is_limit_blocked("600519.SH", up, "buy"))
        self.assertFalse(sim.is_limit_blocked("600519.SH", up, "sell"))
        self.assertTrue(sim.is_limit_blocked("600519.SH", down, "sell"))
        self.assertFalse(sim.is_limit_blocked("600519.SH", down, "buy"))

        # 同一组数字：模拟盘也必须给出同样的结论
        up_broker = self.make_broker(price=11.0, prev_close=10.0)
        up_order = up_broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(up_order.status, "rejected")
        self.assertIn("涨停", up_order.reason)

        down_broker = self.make_broker(price=9.0, prev_close=10.0)
        down_order = down_broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(down_order.status, "rejected")
        self.assertIn("跌停", down_order.reason)        # 即使无持仓，也要先按跌停封板拒单

        self.assertTrue(any("涨跌停" in rule for rule in PaperBroker.rules))
        self.assertAlmostEqual(PaperBroker().describe()["limit_pct"], DEFAULT_LIMIT_PCT, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
