# -*- coding: utf-8 -*-
"""策略指标助手（二）的回归测试：MACD / KDJ / BOLL / CCI / WR / BIAS / OBV / ADX / TRIX / PSY。

期望值都用**手算**或**不变量**（常数序列 → 0/中值；单调序列 → 方向性）写死，避免
「用同一套算法验证自己」。所有指标都遵循同一约定：取最后一根；数据不足返回 None / []。
"""

import math
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.strategies.base import BaseStrategy as B  # noqa: E402


class SequenceHelperTests(unittest.TestCase):
    def test_ref(self):
        values = [1.0, 2.0, 3.0]
        self.assertEqual(B.ref(values, 1), 2.0)
        self.assertEqual(B.ref(values, 2), 1.0)
        self.assertIsNone(B.ref(values, 3), "数据不足要返回 None")
        self.assertIsNone(B.ref(values, -1), "负数偏移非法")

    def test_cross_and_cross_under(self):
        a = [1.0, 1.0, 2.0]
        b = [1.5, 1.5, 1.5]
        self.assertTrue(B.cross(a, b), "上一根 1.0≤1.5、最新 2.0>1.5 → 上穿")
        self.assertFalse(B.cross_under(a, b))
        self.assertTrue(B.cross_under(b, a))
        self.assertFalse(B.cross(a[:1], b[:1]), "长度不足不报错、返回 False")
        self.assertFalse(B.cross([1.0, None], [1.0, 1.0]), "含非法值返回 False")

    def test_ema_series_hand_computed(self):
        # alpha = 2/(3+1) = 0.5；种子取第一个值
        self.assertEqual(B.ema_series([1.0, 2.0, 3.0], 3), [1.0, 1.5, 2.25])
        self.assertEqual(B.ema_series([1.0, 2.0], 5), [], "数据不足返回空")

    def test_sma_series_hand_computed(self):
        # 中国式 SMA：alpha = m/n = 1/3
        out = B.sma_series([1.0, 2.0, 3.0], 3, 1)
        self.assertAlmostEqual(out[0], 1.0)
        self.assertAlmostEqual(out[1], 1 + (2 - 1) / 3.0, places=10)
        self.assertAlmostEqual(out[2], out[1] + (3 - out[1]) / 3.0, places=10)
        self.assertAlmostEqual(B.sma([1.0, 2.0, 3.0], 3, 1), out[-1], places=10)
        self.assertEqual(B.sma_series([1.0, 2.0], 2, 3), [], "m > n 非法")


class ConstantSeriesTests(unittest.TestCase):
    """常数序列是最强的不变量：振荡型指标必须落在中值/零值上。"""

    flat = [10.0] * 40
    highs = [10.0] * 40
    lows = [10.0] * 40

    def test_macd_is_zero(self):
        self.assertEqual(B.macd(self.flat), (0.0, 0.0, 0.0))

    def test_kdj_is_neutral(self):
        self.assertEqual(B.kdj(self.highs, self.lows, self.flat), (50.0, 50.0, 50.0))

    def test_boll_collapses_to_middle(self):
        self.assertEqual(B.boll(self.flat), (10.0, 10.0, 10.0))

    def test_cci_and_bias_and_trix_are_zero(self):
        self.assertEqual(B.cci(self.highs, self.lows, self.flat), 0.0)
        self.assertEqual(B.bias(self.flat), 0.0)
        self.assertEqual(B.trix(self.flat), (0.0, 0.0))

    def test_wr_is_none_when_range_is_zero(self):
        self.assertIsNone(B.wr(self.highs, self.lows, self.flat), "最高=最低时区间为 0")


class HandComputedTests(unittest.TestCase):
    def test_boll_known_window(self):
        # [1,2,3,4,5]：中轨 3，总体方差 ((4+1+0+1+4)/5)=2 → 标准差 √2
        upper, mid, lower = B.boll([1.0, 2.0, 3.0, 4.0, 5.0], 5, 2.0)
        self.assertAlmostEqual(mid, 3.0, places=10)
        self.assertAlmostEqual(upper, 3.0 + 2 * math.sqrt(2), places=6)
        self.assertAlmostEqual(lower, 3.0 - 2 * math.sqrt(2), places=6)

    def test_wr_known_values(self):
        # HHV=12, LLV=8, C=11 → (12-11)/(12-8)*-100 = -25
        self.assertAlmostEqual(B.wr([10.0, 11.0, 12.0], [8.0, 9.0, 10.0], [9.0, 10.0, 11.0], 3),
                               -25.0, places=6)

    def test_bias_known_value(self):
        # MA5 = 12，C = 20 → (20-12)/12*100 = 66.6667
        self.assertAlmostEqual(B.bias([10.0, 10.0, 10.0, 10.0, 20.0], 5), 66.6667, places=3)

    def test_obv_known_sequence(self):
        # 涨 +200、跌 -300、涨 +400、平 0 → 300
        self.assertAlmostEqual(B.obv([10.0, 11.0, 10.0, 11.0, 11.0],
                                     [100.0, 200.0, 300.0, 400.0, 500.0]), 300.0, places=6)

    def test_psy_known_ratio(self):
        # 5 根 → 4 次比较，其中 2 次上涨 → 50%
        self.assertAlmostEqual(B.psy([10.0, 11.0, 10.0, 11.0, 10.0], 4), 50.0, places=6)

    def test_sma_known_value(self):
        # 常数序列下 SMA 恒等于该常数
        self.assertAlmostEqual(B.sma([7.0] * 20, 5, 1), 7.0, places=10)


class DirectionalTests(unittest.TestCase):
    """单调序列只断言方向/关系，不写死数值（避免把实现细节当规格）。"""

    rising_closes = [100.0 + i for i in range(80)]
    highs = [c + 1 for c in rising_closes]
    lows = [c - 1 for c in rising_closes]

    def test_macd_positive_on_uptrend(self):
        dif, dea, hist = B.macd(self.rising_closes)
        self.assertGreater(dif, 0.0)
        self.assertAlmostEqual(hist, 2 * (dif - dea), places=6, msg="HIST 必须等于 2×(DIF−DEA)")

    def test_kdj_high_on_uptrend(self):
        k, d, j = B.kdj(self.highs, self.lows, self.rising_closes)
        self.assertGreater(k, 70.0)
        self.assertAlmostEqual(j, 3 * k - 2 * d, places=3)
        self.assertGreaterEqual(k, d, "持续上涨时 K 应在 D 上方")

    def test_adx_relations(self):
        adx, plus_di, minus_di = B.adx(self.highs, self.lows, self.rising_closes, 14)
        self.assertGreater(adx, 0.0)
        self.assertGreater(plus_di, minus_di, "单调上涨时 +DI 必须大于 −DI")
        self.assertLessEqual(adx, 100.0)

    def test_trix_positive_on_uptrend(self):
        trix, matrix = B.trix(self.rising_closes)
        self.assertGreater(trix, 0.0)
        self.assertGreater(matrix, 0.0)
        self.assertLess(abs(matrix - trix), 0.05, "TRIX 与其均线应贴近（仍在收敛，不要求严格相等）")

    def test_boll_orders_and_cci_sign(self):
        upper, mid, lower = B.boll(self.rising_closes)
        self.assertGreater(upper, mid)
        self.assertGreater(mid, lower)
        self.assertGreater(B.cci(self.highs, self.lows, self.rising_closes), 0.0, "持续上涨 CCI > 0")
        self.assertGreater(B.bias(self.rising_closes, 6), 0.0, "上涨时乖离率为正")
        # 上涨到区间高位时 %R 接近 0（负值但大于 -50）；跌到区间低位时才接近 -100
        self.assertGreater(B.wr(self.highs, self.lows, self.rising_closes, 14), -50.0)


class RobustnessTests(unittest.TestCase):
    def test_insufficient_data_returns_none(self):
        cases = [
            ("macd", lambda: B.macd([1.0, 2.0], 12, 26, 9)),
            ("kdj", lambda: B.kdj([1.0], [1.0], [1.0], 9)),
            ("boll", lambda: B.boll([1.0], 20)),
            ("cci", lambda: B.cci([1.0], [1.0], [1.0], 14)),
            ("wr", lambda: B.wr([1.0], [1.0], [1.0], 14)),
            ("bias", lambda: B.bias([1.0], 6)),
            ("obv", lambda: B.obv([1.0], [1.0])),
            ("adx", lambda: B.adx([1.0] * 10, [1.0] * 10, [1.0] * 10, 14)),
            ("trix", lambda: B.trix([1.0, 2.0], 12, 9)),
            ("psy", lambda: B.psy([1.0, 2.0], 12)),
            ("sma", lambda: B.sma([1.0], 14, 1)),
        ]
        for name, call in cases:
            with self.subTest(indicator=name):
                self.assertIsNone(call(), "%s 数据不足应返回 None" % name)

    def test_dirty_values_are_rejected(self):
        """停牌/缺失字段会带 NaN，绝不能算出被污染的结果。"""
        bad = [1.0, 2.0, float("nan"), 4.0, 5.0, 6.0]
        with_none = [1.0, None, 3.0, 4.0, 5.0, 6.0]
        for values in (bad, with_none):
            with self.subTest(values=values):
                self.assertIsNone(B.macd(values, 2, 3, 2))
                self.assertIsNone(B.boll(values, 3))
                self.assertIsNone(B.bias(values, 3))
                self.assertEqual(B.ema_series(values, 3), [])
                self.assertIsNone(B.psy(values, 3))

    def test_zero_and_negative_prices_do_not_crash(self):
        zeros = [0.0] * 40
        self.assertEqual(B.macd(zeros), (0.0, 0.0, 0.0))
        self.assertEqual(B.boll(zeros), (0.0, 0.0, 0.0))
        self.assertIsNone(B.bias(zeros), "MA 为 0 时乖离率无定义")
        self.assertEqual(B.trix(zeros), (0.0, 0.0))
        self.assertEqual(B.obv([1.0, 0.0], [10.0, 20.0]), -20.0)

    def test_invalid_parameters(self):
        values = [float(i) for i in range(1, 60)]
        self.assertIsNone(B.macd(values, 26, 12, 9), "fast 必须小于 slow")
        self.assertIsNone(B.boll(values, 1), "n 至少为 2")
        self.assertEqual(B.ema_series(values, 0), [])
        self.assertEqual(B.sma_series(values, 0, 1), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
