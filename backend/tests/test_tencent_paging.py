# -*- coding: utf-8 -*-
"""腾讯 K 线翻页：验证「最多 MAX_REQUESTS 页」与「往前翻页确实在变老」。

历史：MAX_REQUESTS 曾经是 3（约 2400 根 ≈ 9.6 年），后来实测接口能回到 2001 年，
放宽到 8 页（约 6400 根 ≈ 25 年）。这些测试用假数据锁住翻页行为本身，不打网络。
"""

import datetime
import os
import shutil
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.data.tencent import TencentProvider          # noqa: E402
from tests.test_data_providers import _tmp_settings            # noqa: E402


class _PagingTencent(TencentProvider):
    """假的分页源：每次请求都返回满 800 根、且一页比一页老。"""

    def __init__(self, settings=None, bars_per_page=None):
        super().__init__(settings)
        self.ends = []                                  # 每次请求用的 end 参数
        self.newest = datetime.date(2026, 9, 24)
        self.per_page = int(bars_per_page or self.BARS_PER_REQUEST)

    def _get_json(self, url, referer="", params=None, headers=None):
        param = str((params or {}).get("param") or "")
        parts = param.split(",")
        symbol, _period, _start, end, _count, _prefix = parts[:6]
        self.ends.append(end)
        # 第 n 页：从「上一页最早那天 - 1」开始往前排 per_page 根自然日（顺序无所谓，合并时按日期排序）
        anchor = self.newest if not end else datetime.date(*[int(p) for p in end.split("-")])
        rows = []
        for offset in range(self.per_page):
            day = anchor - datetime.timedelta(days=offset)
            price = 100.0 + offset / 100.0
            rows.append([day.isoformat(), "%.2f" % price, "%.2f" % (price + 0.1),
                         "%.2f" % (price + 0.2), "%.2f" % (price - 0.1), "1000"])
        return {"data": {symbol: {"qfqday": rows, "qt": {symbol: ["1", "名称", "600519"]}}}}


class TencentPagingTest(unittest.TestCase):
    """翻页上限与「越翻越老」。"""

    def setUp(self):
        self.settings, self.tmp = _tmp_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stops_after_max_requests(self):
        provider = _PagingTencent(self.settings)
        bars = provider.kline("600519.SH", days=100000)      # 想要远超上限的根数
        self.assertEqual(len(provider.ends), TencentProvider.MAX_REQUESTS,
                         "必须恰好翻 %d 页后停（每页都是一次 HTTP 请求）" % TencentProvider.MAX_REQUESTS)
        expected = TencentProvider.BARS_PER_REQUEST * TencentProvider.MAX_REQUESTS
        self.assertEqual(len(bars), expected, "合并后的根数 = 页数 × 每页 800")
        self.assertEqual(provider.ends[0], "", "第一页不带 end：表示「最新一段」")
        # 后续每页的 end = 已合并数据里**最早一天的前一天**（_prev_day(min(merged))），
        # 这样一页比一页老、且不重复取同一段。
        expected_ends = [""]
        cursor = provider.newest
        for _ in range(TencentProvider.MAX_REQUESTS - 1):
            oldest = cursor - datetime.timedelta(days=TencentProvider.BARS_PER_REQUEST - 1)
            cursor = oldest - datetime.timedelta(days=1)
            expected_ends.append(cursor.isoformat())
        self.assertEqual(provider.ends, expected_ends, "翻页的 end 序列必须是「一页比一页老」")
        last_page_oldest = (datetime.date(*[int(p) for p in expected_ends[-1].split("-")])
                            - datetime.timedelta(days=TencentProvider.BARS_PER_REQUEST - 1))
        self.assertEqual(bars[0].date, last_page_oldest.isoformat(), "最早一根 = 最后一页的最早一天")

    def test_partial_page_stops_early(self):
        """服务端返回不满一页 = 没有更多历史了，提前结束（实测 600519 第 8 页只剩 412 根）。"""
        provider = _PagingTencent(self.settings, bars_per_page=120)
        bars = provider.kline("600519.SH", days=100000)
        self.assertEqual(len(provider.ends), 1, "首屏就不满一页 → 不该再翻")
        self.assertEqual(len(bars), 120)

    def test_max_requests_covers_twenty_years(self):
        """上限要够长：8 页 × 800 ≈ 6400 根，按每年约 244 个交易日算 ≈ 26 年。"""
        covered_years = TencentProvider.BARS_PER_REQUEST * TencentProvider.MAX_REQUESTS / 244.0
        self.assertGreater(covered_years, 20.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
