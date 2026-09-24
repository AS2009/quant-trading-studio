# -*- coding: utf-8 -*-
"""交易费用模型测试（``quantstudio.core.costs`` 与 ``SimulatedBroker`` 的接线）。

覆盖：
- 佣金（含单笔最低 5 元）、印花税（仅卖出）、过户费（双边）、每笔固定流量费；
- 比例滑点 ``slippage_bps`` 与跳数滑点 ``slippage_ticks`` 的叠加与取整方向；
- **默认参数下与改造前逐分一致的回归锁**（期望值直接写死，不依赖实现）；
- ``cash_delta`` 符号、非法入参返回零成本、``core.costs`` 与 broker 的单一事实来源接线。

全部为标准库 unittest，不访问网络。
"""

import json
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.backtest.broker import SimulatedBroker  # noqa: E402
from quantstudio.backtest.portfolio import SimAccount  # noqa: E402
from quantstudio.core import costs  # noqa: E402
from quantstudio.core.errors import ValidationError  # noqa: E402
from quantstudio.core.models import SIDE_BUY, SIDE_SELL, Bar, FeeConfig, OrderRequest, Position  # noqa: E402
from quantstudio.strategies.base import BaseStrategy  # noqa: E402

DEFAULT = FeeConfig()

# 改造前的历史口径（com.backtest.broker 旧实现）：买入上浮 / 卖出下浮，四舍五入到分
LEGACY_BPS = 2.0


def legacy_exec_price(side: str, price: float) -> float:
    ratio = 1.0 + (LEGACY_BPS / 10000.0)
    value = price * (ratio if side == SIDE_BUY else (2.0 - ratio))
    return round(value, 2)


def make_bar(date: str = "2024-01-02", close: float = 200.0, open_: float = None) -> Bar:
    price = float(close)
    return Bar(date=date, open=float(open_ if open_ is not None else price),
               high=price, low=price, close=price, change_pct=0.0)


# --------------------------------------------------------------------------- 1. 费用分项


class TestFeeItems(unittest.TestCase):
    """佣金 / 印花税 / 过户费 / 流量费的口径。"""

    def test_commission_minimum_on_small_amount(self):
        # 成交额 999 元：佣金 0.24975 元 → 取最低 5 元
        items = costs.fee_items(SIDE_BUY, 999.0, DEFAULT)
        self.assertEqual(items["commission"], 5.0)
        # 成交额 200040 元：佣金 50.01 元 > 最低佣金
        items = costs.fee_items(SIDE_BUY, 200040.0, DEFAULT)
        self.assertEqual(items["commission"], 50.01)

    def test_stamp_duty_only_on_sell(self):
        buy = costs.fee_items(SIDE_BUY, 200040.0, DEFAULT)
        sell = costs.fee_items(SIDE_SELL, 200040.0, DEFAULT)
        self.assertEqual(buy["stamp_duty"], 0.0)
        self.assertEqual(sell["stamp_duty"], 100.02)        # 200040 × 0.05%
        self.assertEqual(sell["stamp_duty"] - buy["stamp_duty"], 100.02)

    def test_transfer_fee_charged_on_both_sides(self):
        buy = costs.fee_items(SIDE_BUY, 200040.0, DEFAULT)
        sell = costs.fee_items(SIDE_SELL, 200040.0, DEFAULT)
        self.assertEqual(buy["transfer_fee"], 2.0)          # 200040 × 0.001%
        self.assertEqual(sell["transfer_fee"], buy["transfer_fee"])

    def test_flow_fee_is_fixed_per_order_on_both_sides(self):
        cfg = FeeConfig(commission_rate=0.0, commission_min=0.0, stamp_duty_rate=0.0,
                        transfer_fee_rate=0.0, slippage_bps=0.0, flow_fee=0.1)
        # 只留流量费：买卖各收一次，且与成交额无关
        self.assertEqual(costs.fee_items(SIDE_BUY, 100.0, cfg)["fee_total"], 0.1)
        self.assertEqual(costs.fee_items(SIDE_SELL, 100.0, cfg)["fee_total"], 0.1)
        self.assertEqual(costs.fee_items(SIDE_BUY, 9_999_999.0, cfg)["fee_total"], 0.1)
        # 每笔都收：同一笔订单只收一次，两笔订单收两次
        first = costs.order_cost(SIDE_BUY, 10.0, 100, cfg)["fee_total"]
        second = costs.order_cost(SIDE_BUY, 10.0, 100, cfg)["fee_total"]
        self.assertEqual(first, 0.1)
        self.assertEqual(second, 0.1)
        self.assertEqual(round(first + second, 2), 0.2)

    def test_flow_fee_adds_to_total(self):
        cfg = FeeConfig(flow_fee=0.1)
        buy = costs.fee_items(SIDE_BUY, 200040.0, cfg)
        self.assertEqual(buy["flow_fee"], 0.1)
        self.assertEqual(buy["fee_total"], 52.11)           # 50.01 佣金 + 2.00 过户费 + 0.10 流量费
        self.assertEqual(buy["fee_total"], round(
            costs.fee_items(SIDE_BUY, 200040.0, DEFAULT)["fee_total"] + 0.1, 2))

    def test_flow_fee_default_is_zero(self):
        self.assertEqual(DEFAULT.flow_fee, 0.0)
        self.assertEqual(costs.fee_items(SIDE_SELL, 200040.0, DEFAULT)["flow_fee"], 0.0)

    def test_negative_rates_never_produce_negative_fee(self):
        cfg = FeeConfig(commission_rate=-0.001, commission_min=-5.0, stamp_duty_rate=-0.001,
                        transfer_fee_rate=-0.001, flow_fee=-1.0)
        for side in (SIDE_BUY, SIDE_SELL):
            items = costs.fee_items(side, 100000.0, cfg)
            for key, value in items.items():
                self.assertGreaterEqual(value, 0.0)

    def test_zero_and_non_finite_amount_cost_nothing(self):
        for amount in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
            for side in (SIDE_BUY, SIDE_SELL):
                items = costs.fee_items(side, amount, DEFAULT)
                self.assertEqual(items["fee_total"], 0.0)
                self.assertEqual(items["commission"], 0.0)
                self.assertEqual(items["stamp_duty"], 0.0)
                self.assertEqual(items["transfer_fee"], 0.0)
                self.assertEqual(items["flow_fee"], 0.0)

    def test_total_equals_sum_of_items(self):
        for side in (SIDE_BUY, SIDE_SELL):
            items = costs.fee_items(side, 123456.78, FeeConfig(flow_fee=0.1))
            total = round(items["commission"] + items["stamp_duty"] + items["transfer_fee"]
                          + items["flow_fee"], 2)
            self.assertEqual(items["fee_total"], total)
        # fee_total() 与 fee_items()['fee_total'] 同源
        self.assertEqual(costs.fee_total(SIDE_SELL, 100000.0, DEFAULT), 76.0)

    def test_invalid_inputs_raise_validation_error(self):
        with self.assertRaises(ValidationError):
            costs.fee_items("hold", 1000.0, DEFAULT)
        with self.assertRaises(ValidationError):
            costs.fee_total("buy", "abc", DEFAULT)


# --------------------------------------------------------------------------- 2. 成交价


class TestExecPrice(unittest.TestCase):
    """滑点与最小变动价位取整。"""

    PRICES = (0.01, 1.23, 9.99, 10.0, 12.34, 55.55, 99.99, 200.0, 1234.56, 3000.0)

    def test_default_matches_legacy_formula_exactly(self):
        """默认 slippage_ticks=0 时，与改造前的 round(价 × (1 ± bps/10000), 2) 逐分一致。"""
        for price in self.PRICES:
            self.assertEqual(costs.exec_price(SIDE_BUY, price, DEFAULT), legacy_exec_price(SIDE_BUY, price))
            self.assertEqual(costs.exec_price(SIDE_SELL, price, DEFAULT), legacy_exec_price(SIDE_SELL, price))
        # 具体数值锁死（10 元 → 买入 10.0、卖出 10.0，因为 10.002 / 9.998 取整到分）
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, DEFAULT), 10.0)
        self.assertEqual(costs.exec_price(SIDE_SELL, 10.0, DEFAULT), 10.0)
        # 200 元：1 bp 为 0.02 元，正好落在分位上
        self.assertEqual(costs.exec_price(SIDE_BUY, 200.0, DEFAULT), 200.04)
        self.assertEqual(costs.exec_price(SIDE_SELL, 200.0, DEFAULT), 199.96)

    def test_buy_pays_more_and_sell_receives_less(self):
        for price in self.PRICES:
            self.assertGreaterEqual(costs.exec_price(SIDE_BUY, price, DEFAULT), price)
            self.assertLessEqual(costs.exec_price(SIDE_SELL, price, DEFAULT), price)

    def test_ticks_move_price_against_the_buyer(self):
        cfg = FeeConfig(slippage_bps=0.0, slippage_ticks=3.0, tick_size=0.01)
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, cfg), 10.03)      # 买入上浮
        self.assertEqual(costs.exec_price(SIDE_SELL, 10.0, cfg), 9.97)      # 卖出下浮
        self.assertGreater(costs.exec_price(SIDE_BUY, 10.0, cfg), 10.0)
        self.assertLess(costs.exec_price(SIDE_SELL, 10.0, cfg), 10.0)

    def test_bps_and_ticks_stack(self):
        """同时给比例滑点与跳数滑点时两者叠加（且都比单给一种更不利）。"""
        bps_only = FeeConfig(slippage_bps=2.0, slippage_ticks=0.0, tick_size=0.01)
        ticks_only = FeeConfig(slippage_bps=0.0, slippage_ticks=1.0, tick_size=0.01)
        both = FeeConfig(slippage_bps=2.0, slippage_ticks=1.0, tick_size=0.01)
        # 10 元：比例后 10.002 → 10.00；+1 跳 0.01 → 10.012 → 向上取整到 10.02
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, bps_only), 10.0)
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, ticks_only), 10.01)
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, both), 10.02)
        self.assertEqual(costs.exec_price(SIDE_SELL, 10.0, bps_only), 10.0)
        self.assertEqual(costs.exec_price(SIDE_SELL, 10.0, ticks_only), 9.99)
        self.assertEqual(costs.exec_price(SIDE_SELL, 10.0, both), 9.98)

    def test_tick_rounding_goes_to_min_tick_grid(self):
        cfg = FeeConfig(slippage_bps=0.0, slippage_ticks=2.0, tick_size=0.01)
        # 33.333 + 0.02 = 33.353 → 买入向上取整到分
        self.assertEqual(costs.exec_price(SIDE_BUY, 33.333, cfg), 33.36)
        self.assertEqual(costs.exec_price(SIDE_SELL, 33.313, cfg), 33.29)
        for price in self.PRICES:
            for side in (SIDE_BUY, SIDE_SELL):
                value = costs.exec_price(side, price, cfg)
                self.assertAlmostEqual(value * 100.0, round(value * 100.0), places=6,
                                       msg="%s %.3f 不是 0.01 的整数倍：%r" % (side, price, value))

    def test_tick_size_default_is_one_cent(self):
        self.assertEqual(DEFAULT.tick_size, 0.01)
        self.assertEqual(DEFAULT.slippage_ticks, 0.0)

    def test_zero_or_negative_price_returns_zero(self):
        for price in (0.0, -1.0, -0.01, float("nan"), float("inf"), None, "abc"):
            for side in (SIDE_BUY, SIDE_SELL):
                self.assertEqual(costs.exec_price(side, price, DEFAULT), 0.0)
        cfg = FeeConfig(slippage_bps=0.0, slippage_ticks=10_000.0, tick_size=0.01)
        self.assertEqual(costs.exec_price(SIDE_SELL, 1.0, cfg), 0.0)   # 跳数把价格推到 <= 0

    def test_invalid_side_raises(self):
        with self.assertRaises(ValidationError):
            costs.exec_price("hold", 10.0, DEFAULT)


# --------------------------------------------------------------------------- 3. 单笔成本


class TestOrderCost(unittest.TestCase):
    """order_cost 的字段、符号与回归锁。"""

    def test_default_buy_regression_lock(self):
        """默认 FeeConfig() 下 200.00 元 × 1000 股的手算结果（写死作为回归锁）。"""
        self.assertEqual(costs.order_cost(SIDE_BUY, 200.0, 1000, DEFAULT), {
            "exec_price": 200.04,          # 200 × (1 + 2bp)
            "amount": 200040.0,
            "commission": 50.01,           # 200040 × 0.025%
            "stamp_duty": 0.0,             # 买入无印花税
            "transfer_fee": 2.0,           # 200040 × 0.001%
            "flow_fee": 0.0,
            "fee_total": 52.01,
            "cash_delta": -200092.01,      # -(成交额 + 费用)
        })

    def test_default_sell_regression_lock(self):
        self.assertEqual(costs.order_cost(SIDE_SELL, 200.0, 1000, DEFAULT), {
            "exec_price": 199.96,          # 200 × (1 - 2bp)
            "amount": 199960.0,
            "commission": 49.99,
            "stamp_duty": 99.98,           # 卖出印花税 0.05%
            "transfer_fee": 2.0,
            "flow_fee": 0.0,
            "fee_total": 151.97,
            "cash_delta": 199808.03,       # +(成交额 - 费用)
        })

    def test_default_small_order_hits_commission_minimum(self):
        self.assertEqual(costs.order_cost(SIDE_BUY, 9.99, 100, DEFAULT), {
            "exec_price": 9.99, "amount": 999.0, "commission": 5.0, "stamp_duty": 0.0,
            "transfer_fee": 0.01, "flow_fee": 0.0, "fee_total": 5.01, "cash_delta": -1004.01,
        })
        self.assertEqual(costs.order_cost(SIDE_SELL, 9.99, 100, DEFAULT)["fee_total"], 5.51)

    def test_cash_delta_sign_and_formula(self):
        buy = costs.order_cost(SIDE_BUY, 200.0, 1000, DEFAULT)
        sell = costs.order_cost(SIDE_SELL, 200.0, 1000, DEFAULT)
        self.assertLess(buy["cash_delta"], 0.0)
        self.assertGreater(sell["cash_delta"], 0.0)
        self.assertAlmostEqual(buy["cash_delta"], -(buy["amount"] + buy["fee_total"]), places=6)
        self.assertAlmostEqual(sell["cash_delta"], sell["amount"] - sell["fee_total"], places=6)

    def test_flow_fee_included_in_total_and_cash_delta(self):
        cfg = FeeConfig(flow_fee=0.1)
        buy = costs.order_cost(SIDE_BUY, 200.0, 1000, cfg)
        sell = costs.order_cost(SIDE_SELL, 200.0, 1000, cfg)
        self.assertEqual(buy["flow_fee"], 0.1)
        self.assertEqual(buy["fee_total"], 52.11)
        self.assertEqual(buy["cash_delta"], -200092.11)
        self.assertEqual(sell["fee_total"], 152.07)
        self.assertEqual(sell["cash_delta"], 199807.93)

    def test_ticks_feed_into_amount_and_fee(self):
        cfg = FeeConfig(slippage_bps=0.0, slippage_ticks=1.0, tick_size=0.01)
        result = costs.order_cost(SIDE_BUY, 10.0, 1000, cfg)
        self.assertEqual(result["exec_price"], 10.01)
        self.assertEqual(result["amount"], 10010.0)
        self.assertEqual(result["commission"], 5.0)         # 10010 × 0.025% = 2.5025 → 最低 5 元
        self.assertEqual(result["transfer_fee"], 0.1)
        self.assertEqual(result["fee_total"], 5.1)

    def test_non_positive_price_or_qty_returns_zero_cost(self):
        for price, qty in ((0.0, 100), (-1.0, 100), (float("nan"), 100), (float("inf"), 100),
                           (10.0, 0), (10.0, -100), (10.0, 0.0), (10.0, float("nan")),
                           (None, 100), (10.0, None), ("abc", 100), (10.0, "abc")):
            for side in (SIDE_BUY, SIDE_SELL):
                result = costs.order_cost(side, price, qty, DEFAULT)
                self.assertEqual(result, costs.zero_cost(),
                                 msg="%s price=%r qty=%r 应返回零成本" % (side, price, qty))
                for value in result.values():
                    self.assertEqual(value, 0.0)

    def test_zero_cost_has_all_documented_fields(self):
        self.assertEqual(sorted(costs.zero_cost().keys()), [
            "amount", "cash_delta", "commission", "exec_price", "fee_total",
            "flow_fee", "stamp_duty", "transfer_fee",
        ])

    def test_invalid_side_raises(self):
        with self.assertRaises(ValidationError):
            costs.order_cost("hold", 10.0, 100, DEFAULT)

    def test_accepts_dict_config(self):
        result = costs.order_cost(SIDE_BUY, 200.0, 1000, {"flow_fee": 0.1, "commission_rate": 0.00025,
                                                          "commission_min": 5.0})
        self.assertEqual(result["fee_total"], 52.11)


# --------------------------------------------------------------------------- 4. FeeConfig


class TestFeeConfigFields(unittest.TestCase):

    def test_new_fields_have_safe_defaults(self):
        fee = FeeConfig()
        self.assertEqual(fee.flow_fee, 0.0)
        self.assertEqual(fee.slippage_ticks, 0.0)
        self.assertEqual(fee.tick_size, 0.01)
        self.assertGreaterEqual(fee.flow_fee, 0.0)
        self.assertGreaterEqual(fee.slippage_ticks, 0.0)
        self.assertGreater(fee.tick_size, 0.0)

    def test_to_dict_carries_new_fields(self):
        data = FeeConfig().to_dict()
        for key in ("flow_fee", "slippage_ticks", "tick_size"):
            self.assertIn(key, data)
        json.dumps(data, ensure_ascii=False, allow_nan=False)


# --------------------------------------------------------------------------- 5. broker 接线


class TestBrokerUsesCosts(unittest.TestCase):
    """SimulatedBroker 必须走 core.costs（单一事实来源），且默认行为不变。"""

    def test_calc_fee_default_values_unchanged(self):
        broker = SimulatedBroker(FeeConfig())
        # 与改造前 test_backtest.TestFee 的期望值完全一致
        self.assertEqual(broker.calc_fee("buy", 100_000.0), 26.0)
        self.assertEqual(broker.calc_fee("sell", 100_000.0), 76.0)
        self.assertEqual(broker.calc_fee("buy", 1000.0), 5.01)
        self.assertEqual(broker.calc_fee("sell", 1000.0), 5.51)
        self.assertEqual(broker.calc_fee("buy", 0.0), 0.0)
        self.assertEqual(broker.calc_fee("buy", float("nan")), 0.0)
        with self.assertRaises(ValidationError):
            broker.calc_fee("hold", 1000.0)

    def test_calc_fee_equals_order_cost_fee_total(self):
        for cfg in (FeeConfig(), FeeConfig(flow_fee=0.1), FeeConfig(commission_rate=0.0003,
                                                                   slippage_ticks=2.0)):
            broker = SimulatedBroker(cfg)
            for side, price, qty in ((SIDE_BUY, 200.0, 1000), (SIDE_SELL, 33.33, 300),
                                     (SIDE_BUY, 9.99, 100)):
                amount = costs.exec_price(side, price, cfg) * qty
                self.assertEqual(broker.calc_fee(side, amount),
                                 costs.order_cost(side, price, qty, cfg)["fee_total"])

    def test_fill_price_equals_costs_exec_price(self):
        for cfg in (FeeConfig(), FeeConfig(slippage_bps=5.0), FeeConfig(slippage_bps=0.0,
                                                                       slippage_ticks=1.0)):
            broker = SimulatedBroker(cfg)
            for price in (10.0, 200.0, 1234.56):
                bar = make_bar(close=price)
                for side in (SIDE_BUY, SIDE_SELL):
                    self.assertEqual(broker.fill_price(side, bar), costs.exec_price(side, price, cfg))

    def test_execute_uses_order_cost(self):
        cfg = FeeConfig(flow_fee=0.1, slippage_ticks=1.0)
        broker = SimulatedBroker(cfg)
        account = SimAccount(1_000_000.0)
        bar = make_bar(close=200.0)
        trade = broker.execute(OrderRequest("600000.SH", "buy", 1000), bar, account, date=bar.date)
        expected = costs.order_cost(SIDE_BUY, 200.0, 1000, cfg)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.price, expected["exec_price"])
        self.assertEqual(trade.amount, expected["amount"])
        self.assertEqual(trade.fee, expected["fee_total"])
        self.assertEqual(trade.fee, broker.calc_fee("buy", trade.amount))
        # 资金守恒：现金减少 = 成交额 + 费用
        self.assertAlmostEqual(account.cash, 1_000_000.0 - expected["amount"] - expected["fee_total"],
                               places=6)

    def test_max_affordable_qty_reserves_flow_fee(self):
        broker = SimulatedBroker(FeeConfig(flow_fee=100.0))
        qty = broker.max_affordable_qty(10.0, 10_000.0)
        amount = 10.04 * qty          # 默认 2bp 滑点后价格
        self.assertLessEqual(amount + broker.calc_fee("buy", amount), 10_000.0 + 1e-6)
        # 留出的固定成本里包含流量费：再加一手就买不起
        nxt = amount + 10.04 * 100
        self.assertGreater(nxt + broker.calc_fee("buy", nxt), 10_000.0)

    def test_describe_fee_mentions_new_parameters(self):
        text = SimulatedBroker(FeeConfig(flow_fee=0.1, slippage_ticks=2.0)).describe_fee()
        for token in ("佣金", "印花税", "过户费", "流量费", "滑点", "最小变动价", "tick"):
            self.assertIn(token, text)
        self.assertIn("0.10 元", text)


# --------------------------------------------------------------------------- 9. 买入量反解


class TestMaxBuyQtySolver(unittest.TestCase):
    """``costs.max_buy_qty``：含滑点与全部费用的精确反解（撮合与策略共用同一实现）。

    期望值手算写死（不依赖实现）：10 元 × 1000 股 = 10000 元刚好等于资金，
    但还要付 5 元最低佣金 + 0.09 元过户费 → 只能买 900 股。
    """

    def assert_exact(self, cfg, price, cash, lot=100, expect=None):
        """断言返回「精确最大值」：自己买得起、再加一手就买不起。"""
        qty = costs.max_buy_qty(price, cash, cfg, lot)
        if expect is not None:
            self.assertEqual(qty, expect)
        self.assertIsInstance(qty, int)
        self.assertEqual(qty % lot, 0, "必须是整手")
        if qty <= 0:
            self.assertGreater(-costs.order_cost(SIDE_BUY, price, lot, cfg)["cash_delta"], cash,
                               "返回 0 时必须连一手都买不起")
            return 0
        self.assertLessEqual(-costs.order_cost(SIDE_BUY, price, qty, cfg)["cash_delta"], cash + 1e-6)
        self.assertGreater(-costs.order_cost(SIDE_BUY, price, qty + lot, cfg)["cash_delta"], cash,
                           "还有余量 → 不是最优解")
        return qty

    def test_default_fee_hand_checked(self):
        self.assertEqual(self.assert_exact(DEFAULT, 10.0, 10_000.0), 900)
        # 900 股实付 9000 + 5.00 佣金 + 0.09 过户费 = 9005.09，剩 994.91 元买不起 100 股
        self.assertAlmostEqual(-costs.order_cost(SIDE_BUY, 10.0, 900, DEFAULT)["cash_delta"],
                               9005.09, places=2)
        # 5000 元的边界：500 股需 5005 元 > 5000 → 400 股
        self.assertEqual(self.assert_exact(DEFAULT, 10.0, 5_000.0), 400)

    def test_zero_fee_buys_exactly_budget(self):
        free = FeeConfig(commission_rate=0.0, commission_min=0.0, stamp_duty_rate=0.0,
                         transfer_fee_rate=0.0, slippage_bps=0.0, slippage_ticks=0.0, tick_size=0.01)
        self.assertEqual(self.assert_exact(free, 10.0, 10_000.0), 1000)
        self.assertEqual(self.assert_exact(free, 10.0, 9_999.99), 900)

    def test_flow_fee_reserved_per_order(self):
        cfg = FeeConfig(flow_fee=100.0)
        self.assertEqual(self.assert_exact(cfg, 10.0, 10_000.0), 900)
        # 900 股需 9105.09 元（含流量费）：刚好够 → 900；差一分 → 只能 800
        self.assertEqual(self.assert_exact(cfg, 10.0, 9_105.09), 900)
        self.assertEqual(self.assert_exact(cfg, 10.0, 9_105.08), 800)

    def test_tick_slippage_uses_worse_price(self):
        cfg = FeeConfig(slippage_bps=0.0, slippage_ticks=1.0, tick_size=0.01)
        self.assertEqual(costs.exec_price(SIDE_BUY, 10.0, cfg), 10.01)
        qty = self.assert_exact(cfg, 10.0, 10_000.0)
        self.assertEqual(qty, 900)
        self.assertEqual(costs.order_cost(SIDE_BUY, 10.0, qty, cfg)["exec_price"], 10.01)

    def test_lot_size_and_high_commission(self):
        self.assertEqual(self.assert_exact(DEFAULT, 7.77, 12_345.0, lot=10), 1580)
        self.assertEqual(self.assert_exact(DEFAULT, 7.77, 12_345.0, lot=1), 1588)
        # 万分之三十的高佣金：9900 股实付 99297.99，再加一手就超过 10 万
        self.assertEqual(self.assert_exact(FeeConfig(commission_rate=0.003), 10.0, 100_000.0), 9900)

    def test_invalid_inputs_return_zero(self):
        cases = [(0.0, 10_000.0, DEFAULT, 100), (-1.0, 10_000.0, DEFAULT, 100),
                 (float("nan"), 10_000.0, DEFAULT, 100), (float("inf"), 10_000.0, DEFAULT, 100),
                 (10.0, 0.0, DEFAULT, 100), (10.0, -5.0, DEFAULT, 100),
                 (10.0, float("nan"), DEFAULT, 100), (10.0, 10_000.0, DEFAULT, 0),
                 (10.0, 10_000.0, DEFAULT, -100), ("abc", 10_000.0, DEFAULT, 100),
                 (None, None, DEFAULT, 100)]
        for price, cash, cfg, lot in cases:
            self.assertEqual(costs.max_buy_qty(price, cash, cfg, lot), 0)
        # 刚好一手 / 差一分的边界：1000 股 + 5 元佣金 + 0.05 元过户费 = 1005.05
        self.assertEqual(self.assert_exact(DEFAULT, 10.0, 1_005.0), 0)
        self.assertEqual(self.assert_exact(DEFAULT, 10.0, 1_006.0), 100)

    def test_monotonic_in_cash(self):
        last = 0
        for cash in (100.0, 1_000.0, 9_999.0, 10_000.0, 123_456.78, 10_000_000.0):
            qty = costs.max_buy_qty(10.0, cash, DEFAULT, 100)
            self.assertGreaterEqual(qty, last, "资金增加时数量不该减少")
            last = qty


# --------------------------------------------------------------------------- 10. 策略与撮合同口径


class _ProbeContext:
    """满足下单助手所需的最小上下文：``cash()`` + ``fee_config()`` + ``lot_size``。"""

    def __init__(self, cash, fee=DEFAULT, lot_size=100):
        self._cash = float(cash)
        self._fee = fee
        self.lot_size = int(lot_size)

    def cash(self):
        return self._cash

    def fee_config(self):
        return self._fee


class _NoFeeContext:
    """拿不到费率信息的第三方上下文（没有 ``fee_config()``）。"""

    def __init__(self, cash):
        self._cash = float(cash)

    def cash(self):
        return self._cash


class _PositionContext(_ProbeContext):
    """再加 ``position()`` 的上下文，用于验证卖出侧的手数口径。"""

    def __init__(self, cash, fee=DEFAULT, lot_size=100, qty=0, available_qty=None):
        super().__init__(cash, fee, lot_size)
        self._qty = int(qty)
        self._available = int(qty if available_qty is None else available_qty)

    def position(self, code):
        if self._qty <= 0:
            return None
        return Position(code=code, name=code, qty=self._qty, available_qty=self._available,
                        cost=10.0, price=10.0)


class _SizingStrategy(BaseStrategy):
    """只测下单助手的最小策略（不参与 on_bar）。"""

    id = "costs_probe"
    name = "费用探针"
    category = "自定义"
    desc = "仅测试用：验证买入量反解"
    default_params = {}

    @classmethod
    def param_schema(cls):
        return {}

    def on_bar(self, ctx, bars):
        return []


class TestStrategySizingMatchesBroker(unittest.TestCase):
    """策略 ``max_buy_qty`` 与撮合 ``max_affordable_qty`` 同口径：给出的量买得起、不丢单。"""

    CONFIGS = (
        FeeConfig(),
        FeeConfig(flow_fee=8.0, slippage_ticks=1.0),
        FeeConfig(slippage_ticks=3.0, tick_size=0.01),
        FeeConfig(commission_rate=0.003, commission_min=5.0),
        FeeConfig(slippage_bps=0.0, slippage_ticks=0.0),
    )

    def test_matches_broker_and_never_rejected(self):
        for cfg in self.CONFIGS:
            broker = SimulatedBroker(cfg)
            for cash in (10_000.0, 33_333.33, 250_000.0):
                qty = _SizingStrategy().max_buy_qty(_ProbeContext(cash, cfg), 10.0, 1.0)
                self.assertEqual(qty, broker.max_affordable_qty(10.0, cash),
                                 "策略与撮合必须给出同一个最大可买量（%r）" % (cfg,))
                self.assertGreater(qty, 0)
                account = SimAccount(cash)
                bar = make_bar(close=10.0)
                trade = broker.execute(OrderRequest("600000.SH", "buy", qty), bar, account, date=bar.date)
                self.assertIsNotNone(trade, "策略给出的最大量不该被拒单")
                self.assertEqual(trade.qty, qty)
                self.assertGreaterEqual(account.cash, 0.0, "现金不能为负")
                self.assertEqual(broker.rejects, [])
                # 实扣资金与 core.costs 同口径
                expected = costs.order_cost(SIDE_BUY, 10.0, qty, cfg)
                self.assertAlmostEqual(trade.amount, expected["amount"], places=2)
                self.assertAlmostEqual(trade.fee, expected["fee_total"], places=2)

    def test_pct_scales_budget(self):
        cfg = FeeConfig(flow_fee=8.0)
        strategy = _SizingStrategy()
        full = strategy.max_buy_qty(_ProbeContext(100_000.0, cfg), 10.0, 1.0)
        half = strategy.max_buy_qty(_ProbeContext(100_000.0, cfg), 10.0, 0.5)
        self.assertEqual(full, costs.max_buy_qty(10.0, 100_000.0, cfg, 100))
        self.assertEqual(half, costs.max_buy_qty(10.0, 50_000.0, cfg, 100))
        self.assertLess(half, full)
        # pct > 1 视为 1（不会透支）
        self.assertEqual(strategy.max_buy_qty(_ProbeContext(100_000.0, cfg), 10.0, 3.0), full)

    def test_uses_context_lot_size(self):
        qty = _SizingStrategy().max_buy_qty(_ProbeContext(10_000.0, DEFAULT, lot_size=10), 10.0, 1.0)
        self.assertEqual(qty, costs.max_buy_qty(10.0, 10_000.0, DEFAULT, 10))
        self.assertEqual(qty % 10, 0)

    def test_sell_order_uses_the_same_lot_as_buy(self):
        """买卖两侧手数口径必须一致：10 股一手的账户里，30 股持仓不能被判成 0 手。"""
        strategy = _SizingStrategy()                       # 策略自身 lot_size = 100
        ctx = _PositionContext(10_000.0, DEFAULT, lot_size=10, qty=30, available_qty=30)
        order = strategy.sell_order(ctx, "600000.SH")
        self.assertIsNotNone(order, "10 股一手的账户里 30 股不该卖不掉（旧 bug：买卖口径不一致）")
        self.assertEqual(order.qty, 30)
        self.assertEqual(strategy.max_buy_qty(ctx, 10.0, 1.0) % 10, 0, "买入侧同样是 10 股的整数倍")
        # T+1 只解锁一半时按可卖数量取整到同一手数
        half = _PositionContext(10_000.0, DEFAULT, lot_size=10, qty=30, available_qty=25)
        self.assertEqual(strategy.sell_order(half, "600000.SH").qty, 20)
        # 上下文没给 lot_size 时才退回策略自身的 100
        self.assertEqual(strategy.max_buy_qty(_NoFeeContext(10_000.0), 10.0, 1.0) % 100, 0)

    def test_tolerance_boundary_does_not_lose_a_lot(self):
        """上界估算与资金校验共用同一容差：0.07 元 × 100 股 = 7.000000000000001 也买得起。"""
        free = FeeConfig(commission_rate=0.0, commission_min=0.0, stamp_duty_rate=0.0,
                         transfer_fee_rate=0.0, slippage_bps=0.0, slippage_ticks=0.0, tick_size=0.01)
        self.assertEqual(self.assert_exact_free(free, 0.07, 7.0), 100)
        self.assertEqual(self.assert_exact_free(free, 0.07, 14.0), 200)
        self.assertEqual(self.assert_exact_free(free, 0.07, 6.99), 0)
        # 与撮合同口径：求解器说买得起，broker 就不该拒单
        broker = SimulatedBroker(free)
        account = SimAccount(7.0)
        trade = broker.execute(OrderRequest("600000.SH", "buy", 100), make_bar(close=0.07), account,
                               date="2024-01-02")
        self.assertIsNotNone(trade)
        self.assertEqual(trade.qty, 100)
        self.assertGreaterEqual(account.cash, 0.0)
        self.assertEqual(broker.rejects, [])

    def assert_exact_free(self, cfg, price, cash, lot=100):
        """与 TestMaxBuyQtySolver.assert_exact 同义的解耦版本（供本类复用）。"""
        qty = costs.max_buy_qty(price, cash, cfg, lot)
        self.assertEqual(qty % lot, 0)
        if qty <= 0:
            self.assertGreater(-costs.order_cost(SIDE_BUY, price, lot, cfg)["cash_delta"], cash)
            return 0
        self.assertLessEqual(-costs.order_cost(SIDE_BUY, price, qty, cfg)["cash_delta"], cash + 1e-6)
        self.assertGreater(-costs.order_cost(SIDE_BUY, price, qty + lot, cfg)["cash_delta"], cash)
        return qty

    def test_normalize_fee(self):
        """费率归一化：None / dict / FeeConfig 都能用，非法类型直接报错（不再静默换默认值）。"""
        self.assertEqual(costs.normalize_fee(None).lot_size, FeeConfig().lot_size)
        custom = FeeConfig(commission_rate=0.003)
        self.assertIs(costs.normalize_fee(custom), custom)
        from_dict = costs.normalize_fee({"commission_rate": 0.003, "unknown_key": 1})
        self.assertEqual(from_dict.commission_rate, 0.003)
        with self.assertRaises(ValidationError):
            costs.normalize_fee("0.003")

    def test_falls_back_when_fee_unknown(self):
        strategy = _SizingStrategy()
        # 没有 fee_config()：退化为预留 0.1% 的保守估算，且不会超过「含费用的真实上限」
        legacy = strategy.max_buy_qty(_NoFeeContext(100_000.0), 10.0, 1.0)
        self.assertEqual(legacy, int(100_000.0 * 0.999 / 10.0 // 100) * 100)
        self.assertLessEqual(legacy, costs.max_buy_qty(10.0, 100_000.0, DEFAULT, 100))
        # fee_config() 返回 None（烟雾回测）同样退化
        self.assertEqual(strategy.max_buy_qty(_ProbeContext(100_000.0, None), 10.0, 1.0), legacy)
        # 非法 price / pct 依然返回 0
        self.assertEqual(strategy.max_buy_qty(_ProbeContext(100_000.0, DEFAULT), 0.0, 1.0), 0)
        self.assertEqual(strategy.max_buy_qty(_ProbeContext(100_000.0, DEFAULT), 10.0, 0.0), 0)

    def test_buy_order_none_when_unaffordable(self):
        strategy = _SizingStrategy()
        self.assertIsNone(strategy.buy_order(_ProbeContext(500.0, DEFAULT), "600000.SH", 10.0, 1.0))
        order = strategy.buy_order(_ProbeContext(100_000.0, DEFAULT), "600000.SH", 10.0, 1.0,
                                   reason="测试买入")
        self.assertIsNotNone(order)
        self.assertEqual(order.side, SIDE_BUY)
        self.assertEqual(order.qty, costs.max_buy_qty(10.0, 100_000.0, DEFAULT, 100))


if __name__ == "__main__":
    unittest.main()
