# -*- coding: utf-8 -*-
"""模拟盘交易通道 + 真实持仓账户服务的单元测试（unittest，标准库，不依赖网络）。

运行：``cd backend && python -m unittest discover -s tests -p "test_trading.py" -v``
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from quantstudio.config import Settings                                          # noqa: E402
from quantstudio.backtest.broker import SimulatedBroker                                    # noqa: E402
from quantstudio.core import costs                                                          # noqa: E402
from quantstudio.core.errors import BrokerUnavailable, DataSourceError, ValidationError  # noqa: E402
from quantstudio.core.models import FeeConfig, OrderRequest, Quote               # noqa: E402
from quantstudio.portfolio import PortfolioService                               # noqa: E402
from quantstudio.trading import (                                                # noqa: E402
    LiveBrokerStub,
    PaperBroker,
    available_brokers,
    compute_fee,
    normalize_code,
)


class FakeProvider:
    """自带固定最新价的假数据源（不触网）。"""

    name = "fake"

    def __init__(self, prices=None, names=None, prev_close=None):
        self.prices = {"600519.SH": 1500.0} if prices is None else dict(prices)
        self.names = {"600519.SH": "贵州茅台"} if names is None else dict(names)
        self.prev_close = dict(prev_close or {})
        self.fail = False

    def latest_quotes(self, symbols):
        if self.fail:
            raise DataSourceError("行情源不可用（测试注入）")
        out = []
        for code in symbols:
            if code in self.prices:
                out.append(Quote(
                    code=code,
                    name=self.names.get(code, ""),
                    price=float(self.prices[code]),
                    prev_close=float(self.prev_close.get(code, 0.0) or 0.0),
                    ts="2026-09-21 10:00:00",
                    source="fake",
                ))
        return out

    def resolve_name(self, symbol):
        if self.fail:
            raise DataSourceError("解析失败（测试注入）")
        return self.names.get(symbol)


class Clock:
    """可推进的测试时钟。"""

    def __init__(self, day="2026-09-21"):
        self.day = day

    def __call__(self):
        return self.day + " 10:30:00"

    def set(self, day):
        self.day = day


class TradingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-trading-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---- 工具 ----

    def make_broker(self, cash=1_000_000.0, prices=None, day="2026-09-21", slippage=0.0,
                    path=None, prev_close=None):
        provider = FakeProvider(prices=prices, prev_close=prev_close)
        clock = Clock(day)
        broker = PaperBroker(
            provider=provider,
            account_path=path,
            initial_cash=cash,
            slippage_bps=slippage,
            clock=clock,
        )
        return broker, provider, clock

    # ① 数量校验与资金不足拒单

    def test_reject_invalid_qty_side_and_funds(self):
        broker, _, _ = self.make_broker(cash=1000.0, prices={"600519.SH": 1500.0})

        zero = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=0))
        self.assertEqual(zero.status, "rejected")
        self.assertIn("大于 0", zero.reason)

        odd = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=150))
        self.assertEqual(odd.status, "rejected")
        self.assertIn("整数倍", odd.reason)

        negative = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=-100))
        self.assertEqual(negative.status, "rejected")

        bad_side = broker.submit(OrderRequest(code="600519.SH", side="hold", qty=100))
        self.assertEqual(bad_side.status, "rejected")
        self.assertIn("buy", bad_side.reason)

        poor = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(poor.status, "rejected")
        self.assertIn("资金不足", poor.reason)

        missing = broker.submit(OrderRequest(code="999999.SZ", side="buy", qty=100))
        self.assertEqual(missing.status, "rejected")
        self.assertIn("最新价", missing.reason)

        # 拒单不改变账户，且都会出现在委托流水里
        self.assertEqual(broker.account().cash, 1000.0)
        self.assertEqual(len(broker.orders()), 6)
        self.assertEqual(broker.fills(), [])

    # ② T+1

    def test_t1_today_buy_not_sellable_tomorrow_ok(self):
        broker, _, clock = self.make_broker(cash=100_000.0, prices={"600519.SH": 10.0}, day="2026-09-21")

        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")
        self.assertEqual(broker.positions()[0].available_qty, 0)      # 当日买入不可卖

        blocked = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(blocked.status, "rejected")
        self.assertIn("可卖", blocked.reason)
        self.assertIn("T+1", blocked.reason)

        clock.set("2026-09-22")                                       # 次一交易日解禁
        self.assertEqual(broker.positions()[0].available_qty, 100)
        sold = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sold.status, "filled")
        self.assertEqual(broker.positions(), [])

    # ③ 费用与回测口径一致

    def test_fee_matches_backtest_convention(self):
        broker, _, clock = self.make_broker(cash=100_000.0, prices={"600519.SH": 10.0}, day="2026-09-21")
        cfg = FeeConfig()

        # 买入：佣金 max(1000*0.00025, 5)=5 + 过户费 0.01 = 5.01（无印花税）
        self.assertAlmostEqual(compute_fee(1000.0, "buy", cfg), 5.01, places=6)
        # 卖出：再加印花税 1000*0.0005 = 0.5 → 5.51
        self.assertAlmostEqual(compute_fee(1000.0, "sell", cfg), 5.51, places=6)

        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")
        self.assertAlmostEqual(buy.avg_price, 10.0, places=6)         # 滑点 0
        self.assertAlmostEqual(buy.fee, 5.01, places=6)
        self.assertAlmostEqual(broker.fills()[0].fee, 5.01, places=6)
        self.assertAlmostEqual(broker.account().cash, 100_000.0 - 1000.0 - 5.01, places=6)

        clock.set("2026-09-22")
        sell = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100))
        self.assertEqual(sell.status, "filled")
        self.assertAlmostEqual(sell.fee, 5.51, places=6)
        self.assertAlmostEqual(broker.account().cash, 100_000.0 - 5.01 - 5.51, places=6)
        # 无涨跌价差时，累计盈亏 = -(买入费 + 卖出费)
        self.assertAlmostEqual(broker.account().total_pnl, -10.52, places=6)

    def test_flow_fee_and_tick_slippage_follow_backtest_convention(self):
        """模拟盘与回测共用 ``core.costs``：流量费生效、tick 滑点生效、费用逐分一致。"""
        cfg = FeeConfig(flow_fee=1.5, slippage_ticks=1.0, tick_size=0.01)
        broker, _, _ = self.make_broker(cash=100_000.0, prices={"600519.SH": 10.0}, slippage=0.0)
        broker.fee = cfg

        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")
        # 成交价 = 基准价 + 1 跳（比例滑点由模拟盘的 slippage_bps=0 提供）
        self.assertAlmostEqual(buy.avg_price, 10.01, places=6)
        self.assertAlmostEqual(buy.avg_price,
                               costs.exec_price("buy", 10.0, FeeConfig(slippage_bps=0.0, slippage_ticks=1.0)),
                               places=6)
        # 费用 = 佣金 5.00 + 过户费 0.01 + 流量费 1.50（与回测 calc_fee 完全一致）
        amount = round(buy.avg_price * 100, 2)
        self.assertAlmostEqual(buy.fee, 6.51, places=6)
        self.assertAlmostEqual(buy.fee, costs.fee_total("buy", amount, cfg), places=6)
        self.assertAlmostEqual(buy.fee, SimulatedBroker(cfg).calc_fee("buy", amount), places=6)
        self.assertAlmostEqual(compute_fee(amount, "buy", cfg), buy.fee, places=6)

    # ④ 限价单挂单 / 撤单

    def test_limit_pending_and_cancel(self):
        broker, _, _ = self.make_broker(cash=1_000_000.0, prices={"600519.SH": 1500.0})

        pending = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100, price=1400.0))
        self.assertEqual(pending.status, "new")
        self.assertIn("手动撤单", pending.reason)
        self.assertEqual(broker.fills(), [])
        self.assertEqual(broker.account().cash, 1_000_000.0)

        cancelled = broker.cancel(pending.order_id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(broker.cancel(pending.order_id).status, "cancelled")   # 幂等
        with self.assertRaises(ValidationError):
            broker.cancel("NOT-EXIST")

        # 限价可成交：买入限价 >= 最新价 → 按最新价成交
        filled = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100, price=1600.0))
        self.assertEqual(filled.status, "filled")
        self.assertAlmostEqual(filled.avg_price, 1500.0, places=6)

        # 无持仓的限价卖单即使挂单也要被拒（不产生垃圾委托）
        naked = broker.submit(OrderRequest(code="600519.SH", side="sell", qty=100, price=1600.0))
        self.assertEqual(naked.status, "rejected")
        self.assertIn("可卖", naked.reason)

    def test_market_order_uses_slippage(self):
        broker, _, _ = self.make_broker(cash=1_000_000.0, prices={"600519.SH": 10.0}, slippage=20.0)
        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertAlmostEqual(buy.avg_price, 10.02, places=6)        # 20bps 上浮
        self.assertAlmostEqual(broker.positions()[0].price, 10.0, places=6)

    def test_provider_failure_and_quote_fallback(self):
        broker, provider, _ = self.make_broker(cash=1_000_000.0, prices={"600519.SH": 10.0})
        buy = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(buy.status, "filled")

        provider.fail = True                                          # 行情不可用：账户/持仓不崩
        account = broker.account()
        self.assertEqual(account.positions_count, 1)
        self.assertGreater(broker.positions()[0].market_value, 0)
        rejected = broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        self.assertEqual(rejected.status, "rejected")

    # ⑤ 账户持久化

    def test_account_persistence_roundtrip(self):
        path = os.path.join(self.tmp, "paper_account.json")
        broker, _, _ = self.make_broker(cash=100_000.0, prices={"600519.SH": 10.0},
                                        day="2026-09-21", path=path)
        self.assertEqual(broker.submit(OrderRequest(code="600519.SH", side="buy", qty=200)).status, "filled")
        self.assertTrue(os.path.exists(path))

        reloaded, _, reload_clock = self.make_broker(cash=12345.0, prices={"600519.SH": 10.0},
                                                     day="2026-09-22", path=path)
        self.assertEqual(reloaded.account().cash, broker.account().cash)
        self.assertEqual(reloaded.account().positions_count, 1)
        self.assertEqual(len(reloaded.fills()), 1)
        self.assertEqual(len(reloaded.orders()), 1)
        pos = reloaded.positions()[0]
        self.assertEqual(pos.qty, 200)
        self.assertEqual(pos.available_qty, 200)                      # 跨日后 T+1 解禁
        self.assertEqual(reloaded.cash_flow()[0]["type"], "buy")

        sell = reloaded.submit(OrderRequest(code="600519.SH", side="sell", qty=200))
        self.assertEqual(sell.status, "filled")
        self.assertAlmostEqual(reloaded.account().total_pnl,
                               -(compute_fee(2000.0, "buy") + compute_fee(2000.0, "sell")), places=6)

    def test_reset_clears_account_and_file(self):
        path = os.path.join(self.tmp, "paper_account.json")
        broker, _, _ = self.make_broker(cash=100_000.0, prices={"600519.SH": 10.0}, path=path)
        broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100))
        account = broker.reset(initial_cash=50_000.0)
        self.assertEqual(account.cash, 50_000.0)
        self.assertEqual(account.positions_count, 0)
        self.assertEqual(broker.orders(), [])
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["cash"], 50_000.0)

    # ⑥ 账户文件损坏

    def test_corrupt_account_is_backed_up_and_reset(self):
        path = os.path.join(self.tmp, "paper_account.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")

        broker, _, _ = self.make_broker(cash=500_000.0, prices={"600519.SH": 10.0}, path=path)
        self.assertEqual(broker.account().cash, 500_000.0)
        self.assertEqual(broker.account().positions_count, 0)
        self.assertTrue(os.path.exists(path + ".bak"))
        with open(path + ".bak", "r", encoding="utf-8") as fh:
            self.assertIn("not json", fh.read())
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["cash"], 500_000.0)

    # 通道元信息

    def test_available_brokers_and_live_stub(self):
        brokers = available_brokers()
        self.assertEqual(set(brokers), {"paper", "live"})
        self.assertFalse(brokers["paper"]["is_live"])
        self.assertTrue(brokers["paper"]["available"])
        self.assertTrue(brokers["paper"]["rules"])
        self.assertTrue(brokers["live"]["is_live"])
        self.assertFalse(brokers["live"]["available"])
        self.assertEqual(brokers["live"]["enable_flag"], "QUANTSTUDIO_ENABLE_LIVE_TRADING")

        stub = LiveBrokerStub()
        self.assertTrue(stub.is_live)
        with self.assertRaises(BrokerUnavailable):
            stub.submit(OrderRequest(code="600519.SH", side="buy", qty=100))

    def test_normalize_code(self):
        self.assertEqual(normalize_code("600519"), "600519.SH")
        self.assertEqual(normalize_code("000001.sz"), "000001.SZ")
        self.assertEqual(normalize_code("430047"), "430047.BJ")
        with self.assertRaises(ValidationError):
            normalize_code("abc")


class PortfolioServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-portfolio-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = Settings(data_dir=self.tmp)
        self.clock = Clock("2026-09-21")
        self.provider = FakeProvider(
            prices={"600519.SH": 1600.0, "000001.SZ": 12.0},
            names={"600519.SH": "贵州茅台", "000001.SZ": "平安银行"},
            prev_close={"600519.SH": 1580.0},
        )
        self.service = PortfolioService(provider=self.provider, settings=self.settings, clock=self.clock)

    # ⑦ 录入 → 勾稽 → 删除 → 权益曲线

    def test_manual_holdings_account_and_equity(self):
        # 文件不存在时创建空结构
        self.assertTrue(os.path.exists(self.settings.portfolio_path))
        self.assertEqual(self.service.mode, "manual")
        self.assertEqual(self.service.holdings(), [])
        self.assertEqual(self.service.account().total_assets, 0.0)

        self.service.set_cash(50000.0)
        first = self.service.upsert_holding({"code": "600519", "name": "贵州茅台",
                                             "qty": 100, "cost": 1500.0, "available_qty": 100})
        self.assertEqual(first.code, "600519.SH")                     # code 规范化
        self.service.upsert_holding({"code": "000001.SZ", "qty": 1000, "cost": 10.0})

        holdings = self.service.holdings()
        self.assertEqual(len(holdings), 2)
        maotai = [p for p in holdings if p.code == "600519.SH"][0]
        self.assertAlmostEqual(maotai.market_value, 160000.0, places=6)
        self.assertAlmostEqual(maotai.total_pnl, 10000.0, places=6)
        self.assertAlmostEqual(maotai.day_pnl, (1600.0 - 1580.0) * 100, places=6)
        self.assertAlmostEqual(maotai.return_pct, 6.67, places=2)

        account = self.service.account()
        self.assertAlmostEqual(account.total_assets, account.cash + account.market_value, places=6)
        self.assertAlmostEqual(account.total_assets, 222000.0, places=6)
        self.assertAlmostEqual(account.market_value, sum(p.market_value for p in holdings), places=6)
        self.assertAlmostEqual(account.day_pnl, sum(p.day_pnl for p in holdings), places=6)
        self.assertAlmostEqual(account.day_pnl, 2000.0, places=6)
        self.assertAlmostEqual(account.total_pnl, 12000.0, places=6)
        self.assertEqual(account.positions_count, 2)
        self.assertAlmostEqual(sum(row["value"] for row in account.allocation),
                               account.total_assets, places=6)

        # 权益曲线：真实快照按日累积、幂等、升序返回，历史不足会附 notes
        curve1 = self.service.equity_curve(days=90)
        self.assertEqual(curve1["count"], 1)
        self.assertAlmostEqual(curve1["items"][0]["total_assets"], 222000.0, places=6)
        self.assertTrue(any("不足" in note for note in curve1["notes"]))

        self.clock.set("2026-09-22")
        self.service.set_cash(60000.0)
        curve2 = self.service.equity_curve(days=90)
        self.assertEqual(curve2["count"], 2)
        self.assertEqual([row["date"] for row in curve2["items"]], ["2026-09-21", "2026-09-22"])
        self.assertAlmostEqual(curve2["items"][1]["total_assets"], 232000.0, places=6)
        self.assertEqual(self.service.equity_curve(days=90)["count"], 2)   # 同日幂等

        # 删除持仓 + 持久化（重新构造服务读回）
        self.assertTrue(self.service.delete_holding("000001.SZ"))
        self.assertEqual(len(self.service.holdings()), 1)
        with self.assertRaises(ValidationError):
            self.service.delete_holding("000001.SZ")

        reloaded = PortfolioService(provider=self.provider, settings=self.settings, clock=self.clock)
        self.assertEqual(reloaded.mode, "manual")
        self.assertEqual(len(reloaded.holdings()), 1)
        self.assertAlmostEqual(reloaded.account().cash, 60000.0, places=6)
        self.assertEqual(reloaded.equity_curve(days=90)["total_days"], 2)

    def test_manual_validation_and_quote_fallback(self):
        self.service.set_cash(10000.0)
        with self.assertRaises(ValidationError):
            self.service.upsert_holding({"code": "600519.SH", "qty": 0, "cost": 1500.0})
        with self.assertRaises(ValidationError):
            self.service.upsert_holding({"code": "600519.SH", "qty": 100, "cost": -1})
        with self.assertRaises(ValidationError):
            self.service.upsert_holding({"code": "600519.SH", "qty": 100, "cost": 1500.0,
                                         "available_qty": 200})
        with self.assertRaises(ValidationError):
            self.service.upsert_holding({"code": "abc", "qty": 100, "cost": 1500.0})
        with self.assertRaises(ValidationError):
            self.service.set_cash(-1)
        with self.assertRaises(ValidationError):
            self.service.set_mode("live")

        self.service.upsert_holding({"code": "600519.SH", "qty": 100, "cost": 1500.0})
        self.provider.fail = True                                     # 行情不可用：成本价兜底
        pos = self.service.holdings()[0]
        self.assertEqual(pos.price, 0.0)
        self.assertAlmostEqual(pos.market_value, 150000.0, places=6)
        self.assertEqual(pos.total_pnl, 0.0)
        account = self.service.account()
        self.assertAlmostEqual(account.total_assets, 160000.0, places=6)

    def test_manual_corrupt_file_and_equity_corrupt_file(self):
        with open(self.settings.portfolio_path, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        with open(os.path.join(self.tmp, "equity_history.json"), "w", encoding="utf-8") as fh:
            fh.write("[[[")

        service = PortfolioService(provider=self.provider, settings=self.settings, clock=self.clock)
        self.assertEqual(service.holdings(), [])
        self.assertEqual(service.account().cash, 0.0)
        self.assertTrue(os.path.exists(self.settings.portfolio_path + ".bak"))
        curve = service.equity_curve(days=10)                          # 损坏文件备份后重建，不崩
        self.assertEqual(curve["count"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "equity_history.json.bak")))

    def test_paper_mode_reads_paper_broker(self):
        self.service.set_mode("paper")
        self.assertEqual(self.service.mode, "paper")
        self.assertEqual(self.service.holdings(), [])
        self.assertEqual(self.service.account().cash, self.settings.initial_cash)

        from quantstudio.trading import get_paper_broker
        broker = get_paper_broker(settings=self.settings, provider=self.provider)
        self.assertEqual(broker.submit(OrderRequest(code="600519.SH", side="buy", qty=100)).status, "filled")

        account = self.service.account()
        self.assertAlmostEqual(account.total_assets, account.cash + account.market_value, places=6)
        self.assertAlmostEqual(broker.fills()[0].price, 1600.32, places=2)      # 默认 2bps 滑点
        self.assertAlmostEqual(account.market_value, 160000.0, places=2)       # 持仓按最新价估值
        self.assertEqual(account.positions_count, 1)
        self.service.set_mode("manual")
        self.assertEqual(self.service.holdings(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
