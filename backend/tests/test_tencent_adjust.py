# -*- coding: utf-8 -*-
"""腾讯 K 线复权口径：``adjust="qfq"`` = **hfq 缩放到最新真实价**（乘法前复权，离线）。

为什么要有这组用例
------------------
腾讯的 ``qfq`` 是**减法复权**：老价格 = 当下价 - 历史累计复权额，深历史会被减到 0 甚至负数
（实测 600519.SH 取 5000 根：首根收盘 -304.77、2516 天非正价、最坏单日 -1147%，见
``quantstudio/data/tencent.py`` 模块 docstring 的「复权口径」）。所以本模块的 ``qfq`` 改成
「按 ``hfq`` 翻页 + 取一次最新不复权收盘 ``raw_last`` + 整条序列乘 ``k = raw_last / hfq_last``」。

这里的假数据把口径钉死，防止后人改回腾讯的减法复权：
- 翻页请求的 prefix 必须是 ``hfq``（不是 ``qfq``）；
- 最后一根收盘必须等于最新真实价 ``raw_last``（现金 / 手数模拟才与现实同量级）；
- 所有价格必须 > 0；
- 单日涨跌幅必须与 ``hfq`` 版本逐项一致（乘正常数不改变相邻收盘的比值）。

全部离线：子类覆写 ``_get_json`` 返回假 payload（同 ``tests/test_data_providers.py`` 的写法）。
"""

import datetime
import os
import shutil
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.core.errors import DataSourceError       # noqa: E402
from quantstudio.data.tencent import TencentProvider      # noqa: E402
from tests.test_data_providers import _tmp_settings       # noqa: E402

#: 假数据规模：3 页 × 800 根 = 2400 根（把翻页路径也走一遍）
PAGES = 3
PER_PAGE = 800
TOTAL = PAGES * PER_PAGE
NEWEST = datetime.date(2026, 9, 24)
#: 假的后复权序列：最新一根 1100.00、越老每天便宜 0.1（乘法复权：老价格小但不为负）
HFQ_LAST = 1100.0
HFQ_STEP = 0.1
#: 最新**不复权**真实收盘（< hfq 末根，所以缩放系数 k = 500 / 1100 < 1）
RAW_LAST = 500.0
#: 减法复权平移量：老价格 -900 → 负价（复刻腾讯 qfq 深历史的坏样本）
QFQ_SUBTRACT = -900.0


def _hfq_first_close():
    """假序列最早一根的后复权收盘。"""
    return HFQ_LAST - (TOTAL - 1) * HFQ_STEP


class _AdjustTencent(TencentProvider):
    """假腾讯源：``param`` 形如 ``sh600519,day,,<end>,<count>,<prefix>``。

    ``prefix`` 就是第 6 段（``qfq`` / ``hfq`` / 空），所以从请求里能直接看出取的是哪套口径。
    """

    def __init__(self, settings=None, has_hfq=True, qfq_shift=QFQ_SUBTRACT,
                 index_only=False, raw_fail=False):
        TencentProvider.__init__(self, settings)
        self.has_hfq = bool(has_hfq)          # 响应里有没有 hfq 键
        self.qfq_shift = qfq_shift            # qfq 键的平移量（None = 不返回 qfq 键）
        self.index_only = bool(index_only)    # 只返回 day 键（指数：请求复权也没复权键）
        self.raw_fail = bool(raw_fail)        # 最新不复权收盘请求直接失败
        #: 每次请求的 ``(prefix, end, count)``
        self.requests = []

    # ---------------------------------------------------------------- 假数据
    @staticmethod
    def _series_rows(end, count, shift=0.0):
        """从 ``end``（空 = 最新一天）往前排 ``count`` 根自然日，价格随日历升序递增。"""
        anchor = NEWEST if not end else datetime.date(*[int(part) for part in end.split("-")])
        rows = []
        for offset in range(int(count)):
            day = anchor - datetime.timedelta(days=offset)
            close = HFQ_LAST - (NEWEST - day).days * HFQ_STEP + shift
            rows.append([day.isoformat(),
                         "%.3f" % (close * 1.001),
                         "%.3f" % close,
                         "%.3f" % (close * 1.01),
                         "%.3f" % (close * 0.99),
                         "1000.000"])
        return rows

    @staticmethod
    def _day_rows(end, count):
        """不复权（``day`` 键）序列：与后复权同一段日历、整体低 ``HFQ_LAST - RAW_LAST``，
        最新一根收盘正好是今天的真实价（实测 600519：hfq 8764.86 / 不复权 1237.00）。"""
        return _AdjustTencent._series_rows(end, count, shift=RAW_LAST - HFQ_LAST)

    def _payload(self, tx_symbol, prefix, end, count):
        node = {"qt": {tx_symbol: ["1", "测试标的", tx_symbol[2:]]}}
        if self.index_only:
            # 指数：请求任何复权都只返回无复权键 day（实测 sh000001）
            node["day"] = self._series_rows(end, count)
            return {"code": 0, "data": {tx_symbol: node}}
        if self.has_hfq:
            node["hfqday"] = self._series_rows(end, count)
        if self.qfq_shift is not None:
            node["qfqday"] = self._series_rows(end, count, shift=self.qfq_shift)
        if prefix == self.ADJUST_PREFIX["none"]:
            # 不复权：count=1 就是「取最新真实收盘」那一次额外请求
            node["day"] = self._day_rows(end, count)
        return {"code": 0, "data": {tx_symbol: node}}

    def _get_json(self, url, referer="", params=None, headers=None):
        param = str((params or {}).get("param") or "")
        parts = param.split(",")
        tx_symbol, _period, _start, end, count, prefix = parts[:6]
        count = int(count)
        self.requests.append((prefix, end, count))
        if prefix == self.ADJUST_PREFIX["none"] and count == 1 and self.raw_fail:
            raise DataSourceError("模拟「最新不复权收盘价」请求失败")
        return self._payload(tx_symbol, prefix, end, count)

    # ---------------------------------------------------------------- 断言帮手
    def series_requests(self):
        """翻页请求（排除取最新不复权收盘的那 1 次）。"""
        return [item for item in self.requests
                if not (item[0] == self.ADJUST_PREFIX["none"] and item[2] == 1)]

    def raw_requests(self):
        """取最新不复权收盘的请求。"""
        return [item for item in self.requests
                if item[0] == self.ADJUST_PREFIX["none"] and item[2] == 1]


class TencentQfqCaliberTest(unittest.TestCase):
    """``qfq``：hfq 缩放到最新真实价。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_qfq_is_hfq_scaled_to_latest_raw_close(self):
        provider = _AdjustTencent(self.settings)
        bars = provider.kline("600519.SH", days=TOTAL, adjust="qfq")
        hfq = _AdjustTencent(self.settings).kline("600519.SH", days=TOTAL, adjust="hfq")
        self.assertEqual(len(bars), TOTAL)
        self.assertEqual(len(hfq), TOTAL)

        # 末根 == 今日真实价（不复权最新收盘）；所有价格 > 0
        self.assertAlmostEqual(bars[-1].close, RAW_LAST, places=6)
        self.assertGreater(min(bar.close for bar in bars), 0.0)
        self.assertGreater(min(bar.open for bar in bars), 0.0)
        self.assertGreater(min(bar.low for bar in bars), 0.0)

        # 整条序列按同一个 k = raw_last / hfq_last_close 缩放
        factor = RAW_LAST / HFQ_LAST
        for left, right in zip(bars, hfq):
            self.assertEqual(left.date, right.date)
            self.assertAlmostEqual(left.open, right.open * factor, delta=1e-6)
            self.assertAlmostEqual(left.close, right.close * factor, delta=1e-6)
            self.assertAlmostEqual(left.high, right.high * factor, delta=1e-6)
            self.assertAlmostEqual(left.low, right.low * factor, delta=1e-6)
            # 单日涨跌幅与 hfq 版本逐项一致（乘正常数不改变相邻收盘的比值）
            self.assertAlmostEqual(left.change_pct, right.change_pct, delta=1e-6)
            # 成交量不缩放
            self.assertEqual(left.volume_wan, right.volume_wan)

    def test_paging_uses_hfq_prefix_plus_one_raw_request(self):
        provider = _AdjustTencent(self.settings)
        provider.kline("600519.SH", days=TOTAL, adjust="qfq")
        series = provider.series_requests()
        self.assertEqual(len(series), PAGES, provider.requests)
        self.assertTrue(all(item[0] == "hfq" for item in series),
                        "翻页必须用 hfq 前缀（腾讯 qfq 是减法复权）：%r" % (provider.requests,))
        self.assertTrue(all(item[2] == TencentProvider.BARS_PER_REQUEST for item in series))
        self.assertEqual(series[0][1], "", "第一页不带 end：表示「最新一段」")
        # 只多 1 次「最新不复权收盘」请求（days=1、无复权前缀）
        raw = provider.raw_requests()
        self.assertEqual(len(raw), 1, provider.requests)
        self.assertEqual(raw[0][2], 1)

    def test_broken_subtraction_qfq_is_not_used(self):
        """响应同时给了 hfq 与「减法复权」的坏 qfq（老价为负）→ 必须走 hfq 路径。"""
        provider = _AdjustTencent(self.settings, qfq_shift=QFQ_SUBTRACT)
        bars = provider.kline("600519.SH", days=TOTAL, adjust="qfq")
        self.assertGreater(min(bar.close for bar in bars), 0.0,
                           "减法复权的负价绝不能被返回：%r" % (bars[:1],))
        # 首根 = 后复权最早一根 × k（而不是 qfq 键里那个负数）
        self.assertAlmostEqual(bars[0].close, _hfq_first_close() * (RAW_LAST / HFQ_LAST), places=6)
        self.assertLess(_hfq_first_close() + QFQ_SUBTRACT, 0.0, "假样本本身必须是负价样本")

    def test_qfq_with_only_negative_prices_is_rejected(self):
        """只有减法复权的 qfq 键、且整段都是负价 → 宁可不返回，也不返回负价。"""
        provider = _AdjustTencent(self.settings, has_hfq=False, qfq_shift=-1200.0)
        with self.assertRaises(DataSourceError):
            provider.kline("600519.SH", days=TOTAL, adjust="qfq")

    def test_raw_last_unavailable_falls_back_to_unscaled(self):
        """``raw_last`` 取不到 → k=1（退回未缩放的后复权原值），数据仍然可用。"""
        provider = _AdjustTencent(self.settings, raw_fail=True)
        bars = provider.kline("600519.SH", days=TOTAL, adjust="qfq")
        hfq = _AdjustTencent(self.settings).kline("600519.SH", days=TOTAL, adjust="hfq")
        self.assertEqual(len(bars), TOTAL)
        self.assertAlmostEqual(bars[-1].close, HFQ_LAST, places=6)
        self.assertAlmostEqual(bars[0].close, _hfq_first_close(), places=6)
        self.assertGreater(min(bar.close for bar in bars), 0.0)
        for left, right in zip(bars, hfq):
            self.assertAlmostEqual(left.close, right.close, delta=1e-6)
            self.assertAlmostEqual(left.change_pct, right.change_pct, delta=1e-6)


class TencentIndexAndOtherAdjustTest(unittest.TestCase):
    """指数（只有 ``day`` 键）与 ``none`` / ``hfq`` 的原有语义。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_plain_day_key_is_not_scaled(self):
        provider = _AdjustTencent(self.settings, index_only=True)
        bars = provider.kline("000001.SH", days=TOTAL, adjust="qfq")
        self.assertEqual(len(bars), TOTAL)
        # 指数没有复权键：价格原样返回，不做缩放
        self.assertAlmostEqual(bars[-1].close, HFQ_LAST, places=6)
        self.assertAlmostEqual(bars[0].close, _hfq_first_close(), places=6)
        # 也不该为了缩放去额外请求 raw_last
        self.assertEqual(len(provider.series_requests()), PAGES, provider.requests)
        self.assertEqual(provider.raw_requests(), [], provider.requests)

    def test_hfq_returns_raw_backward_adjusted_values(self):
        provider = _AdjustTencent(self.settings)
        bars = provider.kline("600519.SH", days=TOTAL, adjust="hfq")
        self.assertEqual(len(bars), TOTAL)
        self.assertAlmostEqual(bars[-1].close, HFQ_LAST, places=6)          # 原值，不缩放到真实价
        self.assertAlmostEqual(bars[0].close, _hfq_first_close(), places=6)
        self.assertTrue(all(item[0] == "hfq" for item in provider.series_requests()))
        self.assertEqual(provider.raw_requests(), [], "hfq 不需要额外取真实价")

    def test_none_returns_unadjusted_values(self):
        provider = _AdjustTencent(self.settings)
        bars = provider.kline("600519.SH", days=TOTAL, adjust="none")
        self.assertEqual(len(bars), TOTAL)
        self.assertAlmostEqual(bars[-1].close, RAW_LAST, places=6)          # 不复权原值
        # 整条不复权序列比后复权低 HFQ_LAST - RAW_LAST（同一段日历平移），不做任何缩放
        self.assertAlmostEqual(bars[0].close, _hfq_first_close() - (HFQ_LAST - RAW_LAST), places=6)
        self.assertTrue(all(item[0] == "" for item in provider.requests),      # 一律不带复权前缀
                        provider.requests)


if __name__ == "__main__":
    unittest.main(verbosity=2)
