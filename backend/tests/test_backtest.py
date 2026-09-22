# -*- coding: utf-8 -*-
"""回测引擎单元测试（仅标准库 unittest）。

覆盖：费用计算、T+1、涨跌停不成交、资金不足缩量、指标手算对照、
无未来函数（探针策略）、相同输入可复现、InsufficientData。

行情来自本文件内的**确定性 FakeProvider**（自造 500 个交易日，走势由代码决定，
可指定「某个下标之后被极端改写」用于未来函数探测），不依赖网络与 ``quantstudio.data``。
"""

import math
import os
import statistics
import sys
import unittest
from datetime import date, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio.backtest import (  # noqa: E402
    BacktestEngine,
    SimAccount,
    SimulatedBroker,
    compute_metrics,
    drawdown_series,
    monthly_returns,
    normalize_nav,
)
from quantstudio.core.errors import InsufficientData, OrderRejected, ValidationError  # noqa: E402
from quantstudio.core.models import Bar, BacktestRequest, FeeConfig, OrderRequest  # noqa: E402
from quantstudio.strategies.base import BaseStrategy  # noqa: E402

# --------------------------------------------------------------------------- 测试数据


def trading_dates(start: str, days: int) -> List[str]:
    """从 start 起生成 days 个「工作日」（周六日跳过，节假日不模拟）。"""
    y, m, d = (int(part) for part in start.split("-"))
    cur = date(y, m, d)
    out: List[str] = []
    while len(out) < days:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


DEFAULT_CODES = ["600519.SH", "000858.SZ", "300750.SZ", "600036.SH", "601318.SH", "600900.SH", "600276.SH"]

# 每个标的的（基准价、漂移、主周期、主振幅、次周期、次振幅）——完全确定，无随机数
_PROFILES = [
    (60.0, 0.55, 62.0, 0.15, 19.0, 0.04),
    (30.0, 0.05, 55.0, 0.13, 17.0, 0.05),
    (18.0, -0.30, 70.0, 0.12, 23.0, 0.04),
    (12.0, 0.35, 58.0, 0.16, 13.0, 0.05),
    (25.0, 0.00, 66.0, 0.14, 21.0, 0.04),
    (40.0, -0.15, 61.0, 0.11, 15.0, 0.05),
    (22.0, 0.25, 52.0, 0.17, 11.0, 0.05),
]

BENCH_CODE = "000300.SH"      # 基准：始终存在于 FakeProvider 中


def _profile(index: int):
    base, drift, t1, a1, t2, a2 = _PROFILES[index % len(_PROFILES)]
    return base, drift, t1, a1, t2, a2


class FakeProvider:
    """确定性行情源：日线按代码生成，可把某个下标之后的行情整体放大（未来函数探针）。"""

    name = "fake"

    def __init__(self, codes: Optional[List[str]] = None, days: int = 500, start: str = "2023-01-02",
                 mutate_from: Optional[int] = None, mutate_scale: float = 1.0):
        self.codes = list(codes or DEFAULT_CODES)
        if BENCH_CODE not in self.codes:
            self.codes.append(BENCH_CODE)      # 基准必须有数据，保证时间轴连续
        self.days = int(days)
        self.dates = trading_dates(start, self.days)
        self.mutate_from = mutate_from
        self.mutate_scale = float(mutate_scale)
        self.calls: List[str] = []

    # ---- 生成 ----
    def _close(self, index: int, i: int) -> float:
        base, drift, t1, a1, t2, a2 = _profile(index)
        wave = a1 * math.sin(2.0 * math.pi * (i + 7.0 * index) / t1)
        fast = a2 * math.sin(2.0 * math.pi * (i + 3.0 * index) / t2)
        price = base * (1.0 + drift * (float(i) / self.days - 0.5) + wave + fast)
        price = max(price, 1.0)
        if self.mutate_from is not None and i >= self.mutate_from:
            price *= self.mutate_scale      # 只改写「未来」，用于验证无未来函数
        return price

    def series(self, symbol: str) -> List[Bar]:
        if symbol not in self.codes:
            return []
        index = self.codes.index(symbol)
        bars: List[Bar] = []
        prev_close = 0.0
        for i, day in enumerate(self.dates):
            close = self._close(index, i)
            open_ = prev_close if prev_close > 0 else close
            high = max(open_, close) * 1.006
            low = min(open_, close) * 0.994
            change = (close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
            bars.append(Bar(date=day, open=round(open_, 4), high=round(high, 4), low=round(low, 4),
                            close=round(close, 4), volume_wan=1000.0, amount_yi=2.0,
                            change_pct=round(change, 4), turnover_pct=1.2))
            prev_close = close
        return bars

    # ---- DataProvider 协议 ----
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        self.calls.append(symbol)
        bars = self.series(symbol)
        return bars[-int(days):] if days and int(days) > 0 else bars

    def latest_quotes(self, symbols):
        return []

    def index_quotes(self, symbols):
        return []

    def sectors(self, limit: int = 20):
        return []

    def breadth(self):
        return None

    def resolve_name(self, symbol: str) -> Optional[str]:
        return "测试-%s" % symbol

    def health(self):
        return {"ok": True, "detail": "fake", "latency_ms": 0}


class ProbeStrategy(BaseStrategy):
    """探针策略：逐日记录 ctx.history 的长度与末值，并在固定日期买入一次。"""

    id = "probe_test"
    name = "探针策略"
    category = "自定义"
    desc = "仅用于测试：记录 history 快照，验证无未来函数。"
    min_bars = 61
    version = "test"
    default_params = {"lookback": 60, "buy_on_day": 100, "qty": 100}

    @classmethod
    def param_schema(cls):
        return {
            "lookback": {"label": "回看天数", "type": "int", "default": 60, "min": 2, "max": 250, "step": 1,
                         "help": "ctx.history 的窗口长度"},
            "buy_on_day": {"label": "买入日", "type": "int", "default": 100, "min": 1, "max": 1000, "step": 1,
                           "help": "第几个交易日买入（用于对比两次运行）"},
            "qty": {"label": "买入股数", "type": "int", "default": 100, "min": 100, "max": 100000, "step": 100,
                    "help": "买入股数"},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: List[Dict[str, object]] = []
        self.day_index = 0

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        self.day_index += 1
        code = self.symbols[0] if self.symbols else ""
        lookback = int(self.params["lookback"])
        hist = ctx.history(code, "close", lookback)
        self.seen.append({
            "date": ctx.today,
            "len": len(hist),
            "last": hist[-1] if hist else None,
            "first": hist[0] if hist else None,
            "bars_len": len(ctx.bars_since(code, 5)),
        })
        if self.day_index == int(self.params["buy_on_day"]):
            return [OrderRequest(code=code, side="buy", qty=int(self.params["qty"]), price=None, reason="探针买入")]
        return []


# --------------------------------------------------------------------------- 工具

def make_bar(date_str: str, close: float, change_pct: float = 0.0, open_: Optional[float] = None) -> Bar:
    o = open_ if open_ is not None else close
    return Bar(date=date_str, open=o, high=max(o, close) * 1.001, low=min(o, close) * 0.999,
               close=close, volume_wan=100.0, amount_yi=0.1, change_pct=change_pct)


def account_with_position(code: str = "600000.SH", qty: int = 1000, price: float = 10.0,
                         cash: float = 50_000.0) -> SimAccount:
    acct = SimAccount(cash)
    acct.apply_fill(code, "测试股", "buy", price, qty, 0.0, "2024-01-02", "建仓")
    acct.unlock_all()
    return acct


# --------------------------------------------------------------------------- 1. 费用


class TestFee(unittest.TestCase):
    """佣金（含最低 5 元）+ 印花税（仅卖出）+ 过户费。"""

    def setUp(self):
        self.fee = FeeConfig()
        self.broker = SimulatedBroker(self.fee)

    def test_buy_fee(self):
        amount = 100_000.0
        expected = round(max(amount * 0.00025, 5.0), 2) + round(amount * 0.00001, 2)
        self.assertAlmostEqual(self.broker.calc_fee("buy", amount), round(expected, 2), places=6)
        # 25.00 佣金 + 1.00 过户费，无印花税
        self.assertAlmostEqual(self.broker.calc_fee("buy", amount), 26.0, places=6)

    def test_sell_fee_includes_stamp_duty(self):
        amount = 100_000.0
        expected = round(amount * 0.00025, 2) + round(amount * 0.0005, 2) + round(amount * 0.00001, 2)
        self.assertAlmostEqual(self.broker.calc_fee("sell", amount), round(expected, 2), places=6)
        self.assertAlmostEqual(self.broker.calc_fee("sell", amount), 76.0, places=6)

    def test_commission_minimum(self):
        # 成交额 1000 元：佣金 0.25 元 → 取最低 5 元
        self.assertAlmostEqual(self.broker.calc_fee("buy", 1000.0), round(5.0 + 0.01, 2), places=6)
        self.assertAlmostEqual(self.broker.calc_fee("sell", 1000.0), round(5.0 + 0.5 + 0.01, 2), places=6)

    def test_fee_never_negative_and_zero_amount(self):
        self.assertEqual(self.broker.calc_fee("buy", 0.0), 0.0)
        self.assertEqual(self.broker.calc_fee("sell", 0.0), 0.0)
        self.assertEqual(self.broker.calc_fee("buy", float("nan")), 0.0)
        self.assertGreaterEqual(self.broker.calc_fee("sell", 12345.67), 0.0)
        with self.assertRaises(ValidationError):
            self.broker.calc_fee("hold", 1000.0)

    def test_describe_fee_readable(self):
        text = self.broker.describe_fee()
        self.assertIn("佣金", text)
        self.assertIn("印花税", text)
        self.assertIn("过户费", text)
        self.assertIn("滑点", text)


# --------------------------------------------------------------------------- 2. T+1


class TestTPlusOne(unittest.TestCase):

    def setUp(self):
        self.broker = SimulatedBroker(FeeConfig())
        self.acct = SimAccount(100_000.0)
        self.buy_bar = make_bar("2024-01-02", 10.0)

    def test_buy_then_same_day_sell_blocked(self):
        trade = self.broker.execute(OrderRequest("600000.SH", "buy", 1000), self.buy_bar, self.acct,
                                    "测试股", "2024-01-02")
        self.assertIsNotNone(trade)
        pos = self.acct.positions["600000.SH"]
        self.assertEqual(pos.qty, 1000)
        self.assertEqual(pos.available_qty, 0, "T+1：当日买入不可卖")

        same_day = self.broker.execute(OrderRequest("600000.SH", "sell", 1000), self.buy_bar, self.acct,
                                       "测试股", "2024-01-02")
        self.assertIsNone(same_day)
        self.assertEqual(pos.qty, 1000)
        self.assertTrue(any("T+1" in item["reason"] for item in self.broker.rejects))

    def test_apply_fill_rejects_oversell(self):
        self.acct.apply_fill("600000.SH", "测试股", "buy", 10.0, 1000, 0.0, "2024-01-02", "")
        with self.assertRaises(OrderRejected):
            self.acct.apply_fill("600000.SH", "测试股", "sell", 10.0, 1000, 0.0, "2024-01-02", "")
        with self.assertRaises(OrderRejected):
            self.acct.apply_fill("000001.SZ", "无持仓", "sell", 10.0, 100, 0.0, "2024-01-02", "")

    def test_sell_works_after_unlock(self):
        self.acct.apply_fill("600000.SH", "测试股", "buy", 10.0, 1000, 5.0, "2024-01-02", "")
        self.acct.unlock_all()
        trade = self.broker.execute(OrderRequest("600000.SH", "sell", 1000), make_bar("2024-01-03", 11.0),
                                    self.acct, "测试股", "2024-01-03")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.side, "sell")
        self.assertEqual(trade.qty, 1000)
        # 实现盈亏 = 成交额 - 费用 - 加权平均成本(含买入费用)
        price = round(11.0 * (1 - 0.0002), 2)
        cost = (10.0 * 1000 + 5.0) / 1000
        expected = price * 1000 - self.broker.calc_fee("sell", price * 1000) - cost * 1000
        self.assertAlmostEqual(trade.pnl, round(expected, 2), places=2)
        self.assertAlmostEqual(trade.ret_pct, round(expected / (cost * 1000) * 100.0, 2), places=2)
        self.assertNotIn("600000.SH", self.acct.positions)


# --------------------------------------------------------------------------- 3. 涨跌停


class TestLimitUpDown(unittest.TestCase):

    def setUp(self):
        self.broker = SimulatedBroker(FeeConfig())
        self.broker.track_bars("2024-01-02", {"600000.SH": make_bar("2024-01-02", 10.0)})

    def test_limit_up_blocks_buy_but_allows_sell(self):
        limit_up = make_bar("2024-01-03", 11.0, change_pct=10.0)
        self.broker.track_bars("2024-01-03", {"600000.SH": limit_up})
        self.assertTrue(self.broker.is_limit_blocked("600000.SH", limit_up, "buy"))

        acct = SimAccount(100_000.0)
        self.assertIsNone(self.broker.execute(OrderRequest("600000.SH", "buy", 1000), limit_up, acct,
                                             "测试股", "2024-01-03"))
        self.assertEqual(len(acct.trades), 0)

        holder = account_with_position()
        sell = self.broker.execute(OrderRequest("600000.SH", "sell", 1000), limit_up, holder, "测试股", "2024-01-03")
        self.assertIsNotNone(sell, "涨停可以卖出")

    def test_limit_down_blocks_sell_but_allows_buy(self):
        limit_down = make_bar("2024-01-03", 9.0, change_pct=-10.0)
        self.broker.track_bars("2024-01-03", {"600000.SH": limit_down})
        self.assertTrue(self.broker.is_limit_blocked("600000.SH", limit_down, "sell"))

        holder = account_with_position()
        self.assertIsNone(self.broker.execute(OrderRequest("600000.SH", "sell", 1000), limit_down, holder,
                                             "测试股", "2024-01-03"))
        self.assertEqual(holder.positions["600000.SH"].qty, 1000)

        acct = SimAccount(100_000.0)
        buy = self.broker.execute(OrderRequest("600000.SH", "buy", 1000), limit_down, acct, "测试股", "2024-01-03")
        self.assertIsNotNone(buy, "跌停可以买入")

    def test_normal_move_not_blocked(self):
        bar = make_bar("2024-01-03", 10.3, change_pct=3.0)
        self.assertFalse(self.broker.is_limit_blocked("600000.SH", bar, "buy"))
        self.assertFalse(self.broker.limit_reason("600000.SH", bar, "buy"))

    def test_nine_point_nine_pct_counts_as_limit(self):
        bar = make_bar("2024-01-03", 10.99, change_pct=9.9)
        self.assertTrue(self.broker.is_limit_blocked("600000.SH", bar, "buy"))


# --------------------------------------------------------------------------- 4. 资金缩量


class TestCashScaling(unittest.TestCase):

    def setUp(self):
        self.broker = SimulatedBroker(FeeConfig())
        self.acct = SimAccount(10_000.0)

    def test_buy_shrinks_to_affordable_lots(self):
        bar = make_bar("2024-01-02", 10.0)
        trade = self.broker.execute(OrderRequest("600000.SH", "buy", 10_000), bar, self.acct, "测试股", "2024-01-02")
        self.assertIsNotNone(trade)
        price = round(10.0 * 1.0002, 2)
        self.assertEqual(price, 10.0)
        self.assertEqual(trade.qty, 900, "10000 元最多买 900 股（1000 股需 10005 元 > 现金）")
        self.assertGreaterEqual(self.acct.cash, 0.0)
        self.assertAlmostEqual(trade.amount, round(price * 900, 2), places=2)
        self.assertAlmostEqual(self.acct.cash, round(10_000.0 - trade.amount - trade.fee, 2), places=2)
        # 校验「缩到 0 放弃」
        empty = SimAccount(10.0)
        self.assertIsNone(self.broker.execute(OrderRequest("600000.SH", "buy", 100), bar, empty, "测试股", "2024-01-02"))
        self.assertEqual(len(empty.trades), 0)

    def test_max_affordable_qty_is_exact(self):
        price = 10.0
        qty = self.broker.max_affordable_qty(price, 10_000.0)
        amount = price * qty
        self.assertLessEqual(amount + self.broker.calc_fee("buy", amount), 10_000.0 + 1e-6)
        nxt = amount + price * 100
        self.assertGreater(nxt + self.broker.calc_fee("buy", nxt), 10_000.0)

    def test_lot_rounding_and_zero(self):
        bar = make_bar("2024-01-02", 10.0)
        acct = SimAccount(100_000.0)
        trade = self.broker.execute(OrderRequest("600000.SH", "buy", 150), bar, acct, "测试股", "2024-01-02")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.qty, 100)
        self.assertIsNone(self.broker.execute(OrderRequest("600000.SH", "buy", 50), bar, acct, "测试股", "2024-01-02"))

    def test_sell_shrinks_to_available(self):
        holder = account_with_position(qty=500)
        bar = make_bar("2024-01-03", 10.5)
        trade = self.broker.execute(OrderRequest("600000.SH", "sell", 1000), bar, holder, "测试股", "2024-01-03")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.qty, 500)
        self.assertNotIn("600000.SH", holder.positions)


# --------------------------------------------------------------------------- 5. 指标手算


class TestMetricsHandComputed(unittest.TestCase):
    """用一组可以手算的权益序列逐项对照。"""

    def setUp(self):
        self.dates = ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12"]
        self.equity = [100.0, 90.0, 95.0, 105.0, 99.0, 110.0]
        self.bench = [100.0, 98.0, 99.0, 101.0, 100.0, 103.0]
        self.trades = [
            self._sell("2024-01-08", 100.0),
            self._sell("2024-01-09", -50.0),
            self._sell("2024-01-10", 200.0),
        ]
        self.metrics = compute_metrics(self.dates, self.equity, self.bench, self.trades,
                                       initial_cash=100.0, total_fee=12.3, turnover_value=1000.0)

    @staticmethod
    def _sell(day: str, pnl: float):
        from quantstudio.core.models import Trade
        return Trade(date=day, code="600000.SH", name="测试股", side="sell", price=10.0, qty=100,
                     amount=1000.0, fee=1.0, pnl=pnl, ret_pct=1.0, reason="test")

    def test_total_and_annual_return(self):
        self.assertAlmostEqual(self.metrics.total_return_pct, 10.0, places=6)
        expected = ((1.10) ** (252.0 / 5.0) - 1.0) * 100.0
        self.assertAlmostEqual(self.metrics.annual_return_pct, round(expected, 2), places=2)
        self.assertEqual(self.metrics.trading_days, 5)

    def test_max_drawdown_and_dates(self):
        self.assertAlmostEqual(self.metrics.max_drawdown_pct, 10.0, places=6)
        self.assertEqual(self.metrics.max_drawdown_start, "2024-01-05")
        self.assertEqual(self.metrics.max_drawdown_end, "2024-01-08")
        series = drawdown_series(self.dates, self.equity)
        self.assertEqual(len(series), 6)
        self.assertAlmostEqual(series[1]["dd_pct"], 10.0, places=6)
        self.assertAlmostEqual(series[4]["dd_pct"], round((1 - 99.0 / 105.0) * 100, 2), places=2)

    def test_volatility_sharpe_sortino(self):
        rets = [self.equity[i] / self.equity[i - 1] - 1.0 for i in range(1, len(self.equity))]
        vol = statistics.stdev(rets) * math.sqrt(252)
        annual = (1.10) ** (252.0 / 5.0) - 1.0
        self.assertAlmostEqual(self.metrics.volatility_pct, round(vol * 100, 2), places=2)
        self.assertAlmostEqual(self.metrics.sharpe, round(annual / vol, 2), places=2)
        neg = [r for r in rets if r < 0]
        down = statistics.stdev(neg) * math.sqrt(252)
        self.assertAlmostEqual(self.metrics.sortino, round(annual / down, 2), places=2)
        self.assertAlmostEqual(self.metrics.calmar, round(annual / 0.10, 2), places=2)

    def test_trade_stats_and_turnover(self):
        self.assertEqual(self.metrics.trade_count, 3)
        self.assertAlmostEqual(self.metrics.win_rate_pct, 66.67, places=2)
        self.assertAlmostEqual(self.metrics.profit_loss_ratio, 3.0, places=6)
        self.assertAlmostEqual(self.metrics.total_fee, 12.3, places=6)
        avg = sum(self.equity) / len(self.equity)
        self.assertAlmostEqual(self.metrics.turnover_pct, round(1000.0 / avg * 100.0, 2), places=2)

    def test_benchmark_beta_alpha(self):
        self.assertAlmostEqual(self.metrics.benchmark_return_pct, 3.0, places=6)
        strat_rets = [self.equity[i] / self.equity[i - 1] - 1.0 for i in range(1, len(self.equity))]
        bench_rets = [self.bench[i] / self.bench[i - 1] - 1.0 for i in range(1, len(self.bench))]
        mx = sum(bench_rets) / len(bench_rets)
        my = sum(strat_rets) / len(strat_rets)
        n = len(bench_rets)
        var_x = sum((x - mx) ** 2 for x in bench_rets) / (n - 1)
        cov_xy = sum((x - mx) * (y - my) for x, y in zip(bench_rets, strat_rets)) / (n - 1)
        self.assertAlmostEqual(self.metrics.beta, round(cov_xy / var_x, 3), places=3)
        bench_annual = (self.bench[-1] / self.bench[0]) ** (252.0 / (len(self.bench) - 1)) - 1.0
        expected_alpha = (annual_ret := (1.10) ** (252.0 / 5.0) - 1.0) - self.metrics.beta * bench_annual
        self.assertAlmostEqual(self.metrics.alpha_pct, round(expected_alpha * 100.0, 2), places=2)

    def test_all_metrics_finite(self):
        for key, value in self.metrics.to_dict().items():
            if isinstance(value, float):
                self.assertFalse(math.isnan(value), key)
                self.assertFalse(math.isinf(value), key)

    def test_degenerate_inputs_return_zero(self):
        empty = compute_metrics([], [])
        self.assertEqual(empty.total_return_pct, 0.0)
        self.assertEqual(empty.trading_days, 0)
        one = compute_metrics(["2024-01-05"], [100.0])
        self.assertEqual(one.total_return_pct, 0.0)
        self.assertEqual(one.annual_return_pct, 0.0)
        flat = compute_metrics(["d1", "d2", "d3"], [100.0, 100.0, 100.0])
        self.assertEqual(flat.total_return_pct, 0.0)
        self.assertEqual(flat.sharpe, 0.0)          # 波动率为 0 → 分母保护
        self.assertEqual(flat.calmar, 0.0)          # 最大回撤为 0 → 分母保护
        self.assertEqual(flat.sortino, 0.0)
        nan_case = compute_metrics(["d1", "d2"], [float("nan"), float("inf")])
        self.assertEqual(nan_case.total_return_pct, 0.0)

    def test_warnings_channel(self):
        warns: List[str] = []
        compute_metrics(["d1"], [100.0], warnings=warns)
        self.assertTrue(any("不足" in w for w in warns))


class TestSeriesHelpers(unittest.TestCase):

    def test_monthly_returns(self):
        dates = ["2023-12-29", "2024-01-31", "2024-02-29", "2024-03-29"]
        equity = [100.0, 110.0, 121.0, 108.9]
        out = monthly_returns(dates, equity)
        self.assertEqual([item["month"] for item in out], ["2024-01", "2024-02", "2024-03"])
        self.assertAlmostEqual(out[0]["ret_pct"], 10.0, places=6)
        self.assertAlmostEqual(out[1]["ret_pct"], 10.0, places=6)
        self.assertAlmostEqual(out[2]["ret_pct"], -10.0, places=6)
        self.assertEqual(monthly_returns([], []), [])

    def test_normalize_nav(self):
        self.assertEqual(normalize_nav([50.0, 55.0, 45.0]), [1.0, 1.1, 0.9])
        self.assertEqual(normalize_nav([0.0, 5.0]), [1.0, 1.0])
        self.assertEqual(normalize_nav([]), [])


# --------------------------------------------------------------------------- 6/7. 引擎


class TestEngine(unittest.TestCase):

    def setUp(self):
        self.provider = FakeProvider()
        self.request = BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"],
                                       start="2020-01-01", end="2030-01-01", initial_cash=1_000_000.0,
                                       benchmark="000300.SH")

    def test_insufficient_data_raises(self):
        provider = FakeProvider(days=30)
        request = BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"],
                                  start="2020-01-01", end="2030-01-01")
        with self.assertRaises(InsufficientData) as err:
            BacktestEngine(provider).run(request, ProbeStrategy(symbols=["600519.SH"]))
        self.assertIn("600519.SH", str(err.exception))
        self.assertIn("至少", str(err.exception))

    def test_history_has_no_lookahead(self):
        """把「未来」行情放大 10 倍，逐日 history 快照必须完全一致。"""
        cutoff = 300
        engine_a = BacktestEngine(FakeProvider())
        engine_b = BacktestEngine(FakeProvider(mutate_from=cutoff, mutate_scale=10.0))
        probe_a = ProbeStrategy(symbols=["600519.SH"])
        probe_b = ProbeStrategy(symbols=["600519.SH"])
        result_a = engine_a.run(self.request, probe_a)
        result_b = engine_b.run(self.request, probe_b)

        # 两次运行的日期轴一致
        dates_a = [item["date"] for item in result_a.equity]
        dates_b = [item["date"] for item in result_b.equity]
        self.assertEqual(dates_a, dates_b)
        self.assertEqual(len(probe_a.seen), len(probe_b.seen))
        self.assertGreater(len(probe_a.seen), cutoff)

        mismatch_after_cutoff = 0
        for index, (snap_a, snap_b) in enumerate(zip(probe_a.seen, probe_b.seen)):
            self.assertLessEqual(snap_a["len"], 60, "history 不应返回超过请求长度的数据")
            if snap_a == snap_b:
                continue
            mismatch_after_cutoff += 1
            self.assertGreaterEqual(
                index, cutoff, "改动未来数据后，cutoff 之前的决策 / 历史被改变了：%s" % snap_a)
        self.assertGreater(mismatch_after_cutoff, 0, "放大未来数据后，cutoff 之后的历史快照应有变化")
        self.assertGreater(len(result_b.trades), 0)

        # cutoff 当天及之前的历史值必须仍是「未被改写的真实值」
        snapshot_before_cutoff = probe_b.seen[cutoff - 1]
        self.assertIsNotNone(snapshot_before_cutoff["last"])
        self.assertLess(snapshot_before_cutoff["last"], 1000.0,
                        "未来被放大 10 倍后仍不应影响当日 history 取值")
        # cutoff 之前的成交完全一致
        trades_a = [t.to_dict() for t in result_a.trades if t.date < dates_a[cutoff]]
        trades_b = [t.to_dict() for t in result_b.trades if t.date < dates_b[cutoff]]
        self.assertEqual(trades_a, trades_b)

    def test_reproducible_results(self):
        first = BacktestEngine(FakeProvider()).run(self.request, ProbeStrategy(symbols=["600519.SH"]))
        second = BacktestEngine(FakeProvider()).run(self.request, ProbeStrategy(symbols=["600519.SH"]))
        self.assertEqual([p["strategy"] for p in first.nav], [p["strategy"] for p in second.nav])
        self.assertEqual([p["benchmark"] for p in first.nav], [p["benchmark"] for p in second.nav])
        self.assertEqual([e["equity"] for e in first.equity], [e["equity"] for e in second.equity])
        self.assertEqual([t.to_dict() for t in first.trades], [t.to_dict() for t in second.trades])
        self.assertEqual(first.metrics.to_dict(), second.metrics.to_dict())
        self.assertEqual(first.monthly, second.monthly)
        self.assertEqual(first.drawdown, second.drawdown)
        self.assertEqual(first.positions, second.positions)

    def test_result_shape_and_nav_baseline(self):
        result = BacktestEngine(FakeProvider()).run(self.request, ProbeStrategy(symbols=["600519.SH"]))
        self.assertEqual(len(result.nav), len(result.equity))
        self.assertAlmostEqual(result.nav[0]["strategy"], 1.0, places=6)
        self.assertAlmostEqual(result.nav[0]["benchmark"], 1.0, places=6)
        self.assertAlmostEqual(result.equity[0]["equity"], 1_000_000.0, places=2)
        self.assertIn("base_date", result.range)
        self.assertLess(result.range["base_date"], result.range["start"])
        self.assertEqual(result.nav[0]["date"], result.range["base_date"])
        payload = result.to_dict()
        self.assertIn("metrics", payload)
        self.assertIn("trades", payload)
        # 所有对外数值必须可 JSON 序列化且无 NaN
        import json
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.assertIn("total_return_pct", text)
        self.assertEqual(result.metrics.trading_days, len(result.equity) - 1)
        self.assertAlmostEqual(result.metrics.total_return_pct,
                               round((result.equity[-1]["equity"] / result.equity[0]["equity"] - 1) * 100, 2),
                               places=2)

    def test_stopped_symbol_order_not_filled(self):
        """停牌（当日无真实 bar）不产生成交。"""
        provider = FakeProvider()
        bars = provider.series("600519.SH")
        gap_date = bars[200].date

        class GapProvider(FakeProvider):
            def series(self, symbol):
                if symbol != "600519.SH":
                    return super().series(symbol)
                return [b for b in super().series(symbol) if b.date != gap_date]

        request = BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"],
                                  start="2020-01-01", end="2030-01-01", initial_cash=1_000_000.0)

        class BuyOnGap(ProbeStrategy):
            def on_bar(self, ctx, bars):
                if ctx.today == gap_date:
                    self.seen.append({"date": ctx.today, "len": 0, "last": None, "first": None, "bars_len": 0})
                    return [OrderRequest("600519.SH", "buy", 100, None, "停牌日下单")]
                return super().on_bar(ctx, bars)

        result = BacktestEngine(GapProvider()).run(request, BuyOnGap(symbols=["600519.SH"]))
        self.assertFalse([t for t in result.trades if t.date == gap_date and t.reason == "停牌日下单"])
        self.assertTrue(any("未成交" in w for w in result.warnings))

    def test_forward_fill_marking_and_positions(self):
        """停牌日仍按前收盘价估值（前向填充），不产生成交但权益不变。"""
        provider = FakeProvider()
        bars = provider.series("600519.SH")
        gap = bars[250]

        class GapProvider(FakeProvider):
            def series(self, symbol):
                if symbol != "600519.SH":
                    return super().series(symbol)          # 基准照常，时间轴保持连续
                return [b for b in super().series(symbol) if b.date != gap.date]

        result = BacktestEngine(GapProvider()).run(self.request, ProbeStrategy(
            symbols=["600519.SH"], params={"qty": 1000, "buy_on_day": 200}))
        self.assertGreater(len(result.trades), 0)
        dates = [item["date"] for item in result.equity]
        self.assertIn(gap.date, dates)                      # 停牌日仍出现在时间轴上（基准/其他标的交易日）
        index = dates.index(gap.date)
        # 停牌日权益 = 上一日权益（价格前向填充，且无成交）
        self.assertAlmostEqual(result.equity[index]["equity"], result.equity[index - 1]["equity"], places=2)

    def test_unknown_param_override_warns_but_runs(self):
        result = BacktestEngine(FakeProvider()).run(self.request, ProbeStrategy(symbols=["600519.SH"]))
        request = BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"],
                                  start="2020-01-01", end="2030-01-01",
                                  params_override={"lookback": 30})
        result2 = BacktestEngine(FakeProvider()).run(request, ProbeStrategy(symbols=["600519.SH"]))
        self.assertEqual(result2.strategy.params["lookback"], 30)
        self.assertGreater(len(result.nav), 0)
        bad_request = BacktestRequest(strategy_id="probe_test", symbols=["600519.SH"], start="2020-01-01",
                                      end="2030-01-01", params_override={"nope": 1})
        result3 = BacktestEngine(FakeProvider()).run(bad_request, ProbeStrategy(symbols=["600519.SH"]))
        self.assertTrue(any("params_override" in w for w in result3.warnings))


class TestAccountMarkToMarket(unittest.TestCase):

    def test_mark_to_market_and_equity(self):
        acct = SimAccount(100_000.0)
        acct.apply_fill("600000.SH", "测试股", "buy", 10.0, 1000, 5.0, "2024-01-02", "建仓")
        self.assertAlmostEqual(acct.cash, 100_000.0 - 10_000.0 - 5.0, places=6)
        acct.mark_to_market({"600000.SH": 11.0})
        pos = acct.positions["600000.SH"]
        self.assertEqual(pos.market_value, 11_000.0)
        self.assertAlmostEqual(pos.cost, 10.005, places=6)
        self.assertAlmostEqual(pos.total_pnl, 11_000.0 - 10.005 * 1000, places=6)
        self.assertAlmostEqual(pos.return_pct, (11.0 / 10.005 - 1.0) * 100.0, places=6)
        self.assertAlmostEqual(acct.equity(), acct.cash + 11_000.0, places=6)
        snap = acct.snapshot("2024-01-03")
        self.assertEqual(set(snap.keys()), {"date", "equity", "cash", "market_value"})
        self.assertEqual(acct.fees_total, 5.0)
        self.assertEqual(acct.turnover, 10_000.0)

    def test_weighted_average_cost(self):
        acct = SimAccount(100_000.0)
        acct.apply_fill("600000.SH", "测试股", "buy", 10.0, 1000, 5.0, "2024-01-02", "")
        acct.apply_fill("600000.SH", "测试股", "buy", 12.0, 1000, 5.0, "2024-01-03", "")
        expected = (10_000.0 + 5.0 + 12_000.0 + 5.0) / 2000
        self.assertAlmostEqual(acct.positions["600000.SH"].cost, expected, places=6)


class TestBrokerPriceRules(unittest.TestCase):

    def test_slippage_and_fill_modes(self):
        bar = make_bar("2024-01-02", 10.0, open_=9.5)
        close_broker = SimulatedBroker(FeeConfig(), fill_mode="close")
        open_broker = SimulatedBroker(FeeConfig(), fill_mode="open")
        self.assertAlmostEqual(close_broker.fill_price("buy", bar), round(10.0 * 1.0002, 2), places=6)
        self.assertAlmostEqual(close_broker.fill_price("sell", bar), round(10.0 * 0.9998, 2), places=6)
        self.assertAlmostEqual(open_broker.fill_price("buy", bar), round(9.5 * 1.0002, 2), places=6)
        with self.assertRaises(ValidationError):
            SimulatedBroker(FeeConfig(), fill_mode="vwap")

    def test_reject_summary(self):
        broker = SimulatedBroker(FeeConfig())
        broker.track_bars("2024-01-02", {"600000.SH": make_bar("2024-01-02", 10.0)})
        limit_up = make_bar("2024-01-03", 11.0, change_pct=10.0)
        broker.track_bars("2024-01-03", {"600000.SH": limit_up})
        acct = SimAccount(100_000.0)
        broker.execute(OrderRequest("600000.SH", "buy", 1000), limit_up, acct, "测试股", "2024-01-03")
        self.assertEqual(len(broker.rejects), 1)
        summary = broker.reject_summary()
        self.assertEqual(sum(summary.values()), 1)
        self.assertTrue(any("涨停" in key for key in summary))


if __name__ == "__main__":
    unittest.main(verbosity=2)
