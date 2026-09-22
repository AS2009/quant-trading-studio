# -*- coding: utf-8 -*-
"""图表引擎测试：几何纯函数 + 真实 canvas 的 GUI 烟雾测试。

运行方式（**GUI 部分必须用系统 Python**，它自带 tkinter / Tk 8.5.9）::

    cd <repo>
    /usr/bin/python3 -m unittest discover -s desktop/tests -v

说明
----
- ``quantstudio_desktop.widgets.charts`` 依赖 ``tkinter``（``theme`` 也是），
  所以在没有 tkinter 的解释器里整个模块会整体 skip（而不是报 error）；
- GUI 用例创建 ``tk.Tk()`` 后 ``withdraw()``（不弹窗）；创建失败（无显示器）→ skip；
- ``withdraw()`` 的窗口收不到真实的 ``<Configure>``（Tk 不通知未映射窗口），
  因此防抖用例直接调用绑定到 ``<Configure>`` 的处理函数 ``_on_configure()``，
  并用 ``bind("<Configure>")`` 断言绑定确实存在。
"""

import math
import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DESKTOP = os.path.dirname(_HERE)
if _DESKTOP not in sys.path:
    sys.path.insert(0, _DESKTOP)

_TK_ERROR = None
charts = None
theme = None
try:
    import tkinter  # noqa: F401
except Exception as exc:                                          # noqa: BLE001
    _TK_ERROR = exc
else:
    try:
        from quantstudio_desktop import theme                   # noqa: F401
        from quantstudio_desktop.widgets import charts          # noqa: F401
    except Exception as exc:                                      # noqa: BLE001
        _TK_ERROR = exc


def _require_charts():
    if charts is None:
        raise unittest.SkipTest("需要 tkinter 且 quantstudio_desktop 可导入：%s" % _TK_ERROR)


def _sample_bars(count=60, start=10.0):
    bars = []
    price = start
    for index in range(count):
        open_ = price
        close = price + (0.35 if index % 3 else -0.25)
        bars.append({"date": "2024-%02d-%02d" % (index // 28 + 1, index % 28 + 1),
                     "open": open_, "high": max(open_, close) + 0.2, "low": min(open_, close) - 0.2,
                     "close": close, "volume_wan": 1000.0 + (index * 37) % 800})
        price = close
    return bars


# =========================================================================== 纯函数
class NiceTicksTest(unittest.TestCase):
    """``nice_ticks``：覆盖性、取整性、边界。"""

    CASES = [
        (0.0, 100.0), (3.14, 97.2), (-50.0, -10.0), (-7.5, 12.5), (0.0, 1.0),
        (0.0, 0.0), (7.7, 7.7), (-3.3, -3.3), (0.0, 0.0), (1e-9, 1.4e-9),
        (123.456, 123.9), (999.0, 1001.0), (-1e6, 3e6), (2.0, 2.0001),
    ]

    def _step(self, ticks):
        diffs = [round(ticks[index + 1] - ticks[index], 12) for index in range(len(ticks) - 1)]
        self.assertTrue(diffs, "至少要有两格")
        for diff in diffs[1:]:
            self.assertAlmostEqual(diff, diffs[0], places=9, msg="刻度间距必须一致")
        self.assertGreater(diffs[0], 0.0)
        return diffs[0]

    def test_covers_range_and_is_rounded(self):
        for lo, hi in self.CASES:
            with self.subTest(lo=lo, hi=hi):
                ticks = charts.nice_ticks(lo, hi)
                self.assertGreaterEqual(len(ticks), 2)
                self.assertLessEqual(ticks[0], lo + abs(lo) * 1e-9 + 1e-15, "首个刻度要覆盖 lo")
                self.assertGreaterEqual(ticks[-1], hi - abs(hi) * 1e-9 - 1e-15, "末个刻度要覆盖 hi")
                for value in ticks:
                    self.assertTrue(math.isfinite(value))
                step = self._step(ticks)
                exponent = math.floor(math.log10(step))
                fraction = step / (10.0 ** exponent)
                self.assertIn(round(fraction, 6), (1.0, 2.0, 5.0), "步长必须是 1/2/5 × 10^k")
                for value in ticks:
                    self.assertAlmostEqual(value / step, round(value / step), places=6,
                                           msg="刻度必须是步长的整数倍")

    def test_default_count_is_a_hint_not_a_contract(self):
        ticks = charts.nice_ticks(0.0, 100.0)
        self.assertEqual(ticks, [0.0, 50.0, 100.0])
        self.assertLessEqual(len(ticks), 12)

    def test_single_value_makes_symmetric_span(self):
        ticks = charts.nice_ticks(100.0, 100.0)
        self.assertLess(ticks[0], 100.0)
        self.assertGreater(ticks[-1], 100.0)
        zero = charts.nice_ticks(0.0, 0.0)
        self.assertLess(zero[0], 0.0)
        self.assertGreater(zero[-1], 0.0)

    def test_tiny_and_negative_ranges(self):
        tiny = charts.nice_ticks(1e-9, 1.4e-9)
        self.assertLessEqual(tiny[0], 1e-9)
        self.assertGreaterEqual(tiny[-1], 1.4e-9)
        negative = charts.nice_ticks(-50.0, -10.0)
        self.assertLessEqual(negative[0], -50.0)
        self.assertGreaterEqual(negative[-1], -10.0)
        self.assertTrue(any(value < 0 for value in negative))

    def test_reversed_and_invalid_input(self):
        self.assertEqual(charts.nice_ticks(100.0, 0.0), charts.nice_ticks(0.0, 100.0))
        self.assertEqual(charts.nice_ticks(None, 5.0), [])
        self.assertEqual(charts.nice_ticks(1.0, float("nan")), [])
        self.assertEqual(charts.nice_ticks("abc", 5.0), [])

    def test_compute_ticks_returns_padded_range(self):
        ticks, lo, hi = charts.compute_ticks(3.0, 97.0)
        self.assertTrue(ticks)
        self.assertLessEqual(lo, 3.0)
        self.assertGreaterEqual(hi, 97.0)
        self.assertEqual((lo, hi), (ticks[0], ticks[-1]))
        self.assertEqual(charts.compute_ticks(None, 1.0), ([], None, None))
        ticks2, lo2, hi2 = charts.compute_ticks(5.0, 5.0)
        self.assertLessEqual(lo2, 5.0)
        self.assertGreaterEqual(hi2, 5.0)
        self.assertTrue(ticks2)


class MappingTest(unittest.TestCase):
    """``map_value`` / ``map_index`` / ``map_xs`` / ``band_bounds``。"""

    def test_map_value_orientation(self):
        self.assertAlmostEqual(charts.map_value(0.0, 0.0, 10.0, 0.0, 100.0), 100.0)
        self.assertAlmostEqual(charts.map_value(10.0, 0.0, 10.0, 0.0, 100.0), 0.0)
        self.assertAlmostEqual(charts.map_value(5.0, 0.0, 10.0, 0.0, 100.0), 50.0)
        self.assertIsNone(charts.map_value(None, 0.0, 10.0, 0.0, 100.0))
        self.assertAlmostEqual(charts.map_value(3.0, 3.0, 3.0, 0.0, 10.0), 5.0, msg="全等值取中点")

    def test_map_value_is_monotonic(self):
        previous = None
        for value in range(0, 11):
            y = charts.map_value(value, 0.0, 10.0, 20.0, 220.0)
            if previous is not None:
                self.assertLess(y, previous, "值越大 y 越小")
            previous = y

    def test_map_index_endpoints(self):
        self.assertAlmostEqual(charts.map_index(0, 3, 10.0, 110.0), 10.0)
        self.assertAlmostEqual(charts.map_index(2, 3, 10.0, 110.0), 110.0)
        self.assertAlmostEqual(charts.map_index(1, 3, 10.0, 110.0), 60.0)
        self.assertAlmostEqual(charts.map_index(0, 1, 10.0, 110.0), 60.0, msg="单点居中")

    def test_map_xs_modes(self):
        points = charts.map_xs(3, 0.0, 100.0)
        self.assertAlmostEqual(points[0], 0.0)
        self.assertAlmostEqual(points[-1], 100.0)
        bands = charts.map_xs(4, 0.0, 100.0, mode="band")
        self.assertEqual(len(bands), 4)
        self.assertAlmostEqual(bands[0], 12.5)
        self.assertEqual(charts.map_xs(0, 0.0, 100.0), [])

    def test_band_bounds_stay_inside(self):
        left, right = charts.band_bounds(0, 4, 0.0, 100.0, 0.25)
        self.assertGreater(left, 0.0)
        self.assertLess(right, 25.0)
        self.assertLess(left, right)
        last_left, last_right = charts.band_bounds(3, 4, 0.0, 100.0, 0.25)
        self.assertLessEqual(last_right, 100.0)
        self.assertGreater(last_left, 75.0)

    def test_value_range_and_series_range(self):
        self.assertEqual(charts.value_range([]), (None, None))
        self.assertEqual(charts.value_range([None, "x"]), (None, None))
        lo, hi = charts.value_range([1.0, None, 5.0])
        self.assertEqual((lo, hi), (1.0, 5.0))
        flat_lo, flat_hi = charts.value_range([4.0, 4.0, 4.0])
        self.assertLess(flat_lo, 4.0)
        self.assertGreater(flat_hi, 4.0)
        s_lo, s_hi = charts.series_range([{"values": [1.0, 2.0]}, [5.0, None, 7.0]])
        self.assertEqual((s_lo, s_hi), (1.0, 7.0))


class CandleGeometryTest(unittest.TestCase):
    """``candle_geometry`` / ``volume_geometry``。"""

    AREA = (52.0, 16.0, 652.0, 216.0)

    def test_empty(self):
        self.assertEqual(charts.candle_geometry([], 0.0, 1.0, *self.AREA), [])
        self.assertEqual(charts.candle_geometry(None, 0.0, 1.0, *self.AREA), [])

    def test_short_and_equal_data(self):
        bars = [{"date": "d1", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0}]
        geometry = charts.candle_geometry(bars, 9.0, 11.0, *self.AREA)
        self.assertEqual(len(geometry), 1)
        item = geometry[0]
        self.assertTrue(item["valid"])
        self.assertGreaterEqual(item["body_height"], 1.0, "实体最小 1px")
        equal = [{"date": "d%d" % index, "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0}
                 for index in range(5)]
        for item in charts.candle_geometry(equal, 5.0, 5.0, *self.AREA):
            self.assertTrue(item["valid"])
            self.assertGreaterEqual(item["body_height"], 1.0)
            self.assertTrue(self.AREA[1] <= item["body_top"] <= item["body_bottom"] <= self.AREA[3])

    def test_invalid_close_is_skipped(self):
        bars = [{"date": "d1", "close": None}, {"date": "d2", "close": 3.0},
                {"date": "d3", "close": "abc"}, "垃圾", None,
                {"date": "d4", "close": 4.0, "open": 3.5}]
        geometry = charts.candle_geometry(bars, 2.0, 5.0, *self.AREA)
        self.assertEqual(len(geometry), 4, "非 dict 的条目要被丢掉")
        self.assertEqual([item["valid"] for item in geometry], [False, True, False, True])
        self.assertEqual(geometry[1]["close"], 3.0)
        self.assertEqual(geometry[3]["open"], 3.5, "open 缺失时按 close 处理，有值就用值")

    def test_high_low_derived_from_body(self):
        bars = [{"date": "d1", "open": 8.0, "close": 12.0}]           # 没有 high/low
        item = charts.candle_geometry(bars, 8.0, 12.0, *self.AREA)[0]
        self.assertEqual(item["high"], 12.0)
        self.assertEqual(item["low"], 8.0)
        mixed = [{"date": "d1", "open": 8.0, "high": 8.5, "low": 9.5, "close": 12.0}]
        item = charts.candle_geometry(mixed, 8.0, 12.0, *self.AREA)[0]
        self.assertEqual(item["high"], 12.0, "high 与实体矛盾时以实体为准")
        self.assertEqual(item["low"], 8.0)

    def test_ordering_invariant(self):
        bars = _sample_bars(40)
        geometry = charts.candle_geometry(bars, 5.0, 20.0, *self.AREA)
        self.assertEqual(len(geometry), 40)
        for item in geometry:
            self.assertLessEqual(item["y_high"], item["body_top"] + 1e-9)
            self.assertLessEqual(item["body_top"], item["body_bottom"] + 1e-9)
            self.assertLessEqual(item["body_bottom"], item["y_low"] + 1e-9)
            self.assertLess(item["left"], item["right"])

    def test_rising_flag(self):
        bars = [{"date": "up", "open": 1.0, "close": 2.0}, {"date": "down", "open": 2.0, "close": 1.0},
                {"date": "flat", "open": 2.0, "close": 2.0}]
        geometry = charts.candle_geometry(bars, 0.0, 3.0, *self.AREA)
        self.assertEqual([item["color_key"] for item in geometry], ["up", "down", "up"])
        self.assertTrue(geometry[2]["rising"], "平盘按涨处理")

    def test_max_bars_keeps_tail(self):
        bars = _sample_bars(30)
        bars[-1]["close"] = 999.0
        geometry = charts.candle_geometry(bars, 0.0, 1000.0, *self.AREA, max_bars=10)
        self.assertEqual(len(geometry), 10)
        self.assertEqual(geometry[-1]["close"], 999.0)

    def test_volume_geometry(self):
        bars = _sample_bars(5)
        bars[0]["volume_wan"] = 0.0
        bars[1]["volume_wan"] = None
        geometry = charts.volume_geometry(bars, 0.0, 2000.0, 52.0, 150.0, 652.0, 216.0)
        self.assertEqual(len(geometry), 5)
        self.assertEqual(geometry[0]["top"], geometry[0]["bottom"], msg="0 成交不画柱")
        self.assertEqual(geometry[1]["top"], geometry[1]["bottom"], msg="None 成交不画柱")
        self.assertFalse(geometry[1]["valid"])
        for item in geometry[2:]:
            self.assertLessEqual(item["top"], item["bottom"])
            self.assertGreaterEqual(item["bottom"] - item["top"], 1.0)
            self.assertTrue(item["valid"])
        self.assertEqual(geometry[0]["color_key"], geometry[0]["rising"] and "up" or "down")

    def test_volume_geometry_flat_range(self):
        bars = [{"date": "d1", "close": 1.0, "open": 1.0, "volume_wan": 0.0},
                {"date": "d2", "close": 1.0, "open": 1.0, "volume_wan": None}]
        geometry = charts.volume_geometry(bars, 5.0, 5.0, 0.0, 0.0, 100.0, 50.0)
        for item in geometry:
            self.assertEqual(item["top"], item["bottom"], "hi<=lo 时不画柱子")


class DonutSlicesTest(unittest.TestCase):
    """``donut_slices``：占比、0 值项、角度。"""

    def test_percent_sums_to_100(self):
        items = [{"name": "a", "value": 5}, {"name": "b", "value": 3},
                 {"name": "zero", "value": 0}, {"name": "c", "value": 2}]
        slices = charts.donut_slices(items)
        self.assertEqual(len(slices), 4)
        self.assertAlmostEqual(sum(item["pct"] for item in slices), 100.0, places=6)
        self.assertEqual(slices[2]["pct"], 0.0)
        self.assertEqual(slices[2]["span"], 0.0)
        self.assertEqual(slices[2]["extent"], 0.0)
        self.assertTrue(slices[2]["zero"])
        self.assertAlmostEqual(slices[0]["pct"], 50.0)
        self.assertAlmostEqual(sum(item["ratio"] for item in slices), 1.0, places=9)

    def test_clockwise_angles_and_start(self):
        items = [{"name": "a", "value": 1}, {"name": "b", "value": 1}]
        slices = charts.donut_slices(items, start_angle=90.0, clockwise=True)
        self.assertAlmostEqual(slices[0]["start"], 90.0 - 180.0)
        self.assertAlmostEqual(slices[0]["extent"], -180.0)
        self.assertAlmostEqual(slices[1]["start"], 90.0 - 360.0)
        self.assertAlmostEqual(slices[1]["extent"], -180.0)
        counter = charts.donut_slices(items, start_angle=0.0, clockwise=False)
        self.assertAlmostEqual(counter[0]["start"], 0.0)
        self.assertAlmostEqual(counter[0]["extent"], 180.0)

    def test_zero_and_negative_values(self):
        slices = charts.donut_slices([{"name": "a", "value": 0}, {"name": "b", "value": -3}])
        self.assertTrue(all(item["pct"] == 0.0 for item in slices))
        self.assertTrue(all(item["span"] == 0.0 for item in slices))
        slices = charts.donut_slices([{"name": "only", "value": 7}])
        self.assertLessEqual(slices[0]["span"], 359.999, "整圆要压在 360° 以内（Tk 8.5）")
        self.assertGreater(slices[0]["span"], 359.0)

    def test_colors_cycle_and_junk_items(self):
        items = [{"name": "a", "value": 1}, {"name": "b", "value": 1}, {"name": "c", "value": 1},
                 "垃圾", {"name": "none", "value": None}]
        slices = charts.donut_slices(items, colors=["#111111", "#222222"])
        self.assertEqual(len(slices), 5)
        self.assertEqual([item["color"] for item in slices[:3]],
                         ["#111111", "#222222", "#111111"])
        self.assertEqual(slices[4]["pct"], 0.0)


class BarGeometryTest(unittest.TestCase):
    """``bar_geometry``：竖向 / 横向、0 基准、最小尺寸、颜色覆盖。"""

    def test_vertical_bars_grow_from_zero(self):
        items = charts.bar_geometry(["涨", "跌"], [5.0, -5.0], 0.0, 0.0, 100.0, 100.0)
        up, down = items
        self.assertAlmostEqual(up["base"], 50.0)
        self.assertAlmostEqual(down["base"], 50.0)
        self.assertLess(up["top"], 50.0, "正值柱向上长")
        self.assertAlmostEqual(up["bottom"], 50.0)
        self.assertGreater(down["bottom"], 50.0, "负值柱向下长")
        self.assertAlmostEqual(down["top"], 50.0)
        self.assertLess(up["left"], up["right"])

    def test_horizontal_bars(self):
        items = charts.bar_geometry(["涨", "跌"], [5.0, -5.0], 100.0, 0.0, 300.0, 100.0,
                                    horizontal=True)
        up, down = items
        self.assertGreater(up["left"], 100.0, "正值条向右长")
        self.assertLess(down["right"], 300.0)
        self.assertAlmostEqual(up["base"], down["base"])
        self.assertLess(up["top"], up["bottom"])

    def test_min_size_and_invalid_values(self):
        items = charts.bar_geometry(["a", "b", "c"], [0.0, None, "x"], 0.0, 0.0, 90.0, 60.0,
                                    lo=-1.0, hi=1.0)
        self.assertTrue(items[0]["valid"])
        self.assertGreaterEqual(items[0]["bottom"] - items[0]["top"], 1.0)
        self.assertFalse(items[1]["valid"])
        self.assertFalse(items[2]["valid"])
        self.assertEqual(items[1]["value"], None)

    def test_labels_and_colors(self):
        items = charts.bar_geometry(["甲"], [1.0], 0.0, 0.0, 10.0, 10.0, colors=["#ff0000"])
        self.assertEqual(items[0]["label"], "甲")
        self.assertEqual(items[0]["color"], "#ff0000")
        items = charts.bar_geometry(None, [1.0, 2.0], 0.0, 0.0, 10.0, 10.0)
        self.assertEqual([item["label"] for item in items], ["1", "2"])


class SeriesHelpersTest(unittest.TestCase):
    """``line_runs`` / ``moving_average`` / ``sample_indices`` / ``window_*`` / 文本工具。"""

    def test_line_runs_splits_on_none(self):
        runs = charts.line_runs([1.0, None, 3.0, 4.0], 0.0, 5.0, [0.0, 10.0, 20.0, 30.0], 0.0, 100.0)
        self.assertEqual([len(run) for run in runs], [1, 2])
        self.assertEqual([run[0][2] for run in runs], [0, 2])
        self.assertEqual(charts.line_runs([None, None], 0.0, 1.0, [0.0, 1.0], 0.0, 10.0), [])
        self.assertEqual(charts.line_runs([], 0.0, 1.0, [], 0.0, 10.0), [])

    def test_line_runs_clamps_and_subsamples(self):
        runs = charts.line_runs([0.0, 10.0], 0.0, 1.0, [0.0, 10.0], 0.0, 100.0)
        self.assertEqual(runs[0][0][1], 100.0, "超出范围的值被夹在边界")
        self.assertEqual(runs[0][1][1], 0.0)
        many = charts.line_runs(list(range(500)), 0.0, 500.0, [float(i) for i in range(500)],
                                0.0, 100.0, max_points=50)
        total = sum(len(run) for run in many)
        self.assertLessEqual(total, 50)
        self.assertGreater(total, 10)

    def test_moving_average(self):
        self.assertEqual(charts.moving_average([1.0, 2.0, 3.0, 4.0, 5.0], 3),
                         [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(charts.moving_average([1.0, 2.0], 5), [None, None])
        self.assertEqual(charts.moving_average([1.0, None, 3.0], 2), [None, None, None])
        self.assertEqual(charts.moving_average([1.0, 2.0], 0), [None, None])
        lines = charts.moving_averages([1.0, 2.0, 3.0], [0, 2, 2, -1])
        self.assertEqual([line["period"] for line in lines], [2])
        self.assertEqual(lines[0]["values"], [None, 1.5, 2.5])

    def test_sample_indices(self):
        self.assertEqual(charts.sample_indices(0), [])
        self.assertEqual(charts.sample_indices(3, 6), [0, 1, 2])
        picked = charts.sample_indices(100, 6)
        self.assertEqual(len(picked), 6)
        self.assertEqual(picked[0], 0)
        self.assertEqual(picked[-1], 99)
        self.assertEqual(picked, sorted(set(picked)))
        self.assertEqual(charts.sample_indices(50, 1), [0])

    def test_window_span_clamps(self):
        self.assertEqual(charts.window_span(0.55, 1.0), (0.55, 1.0))
        self.assertEqual(charts.window_span(-1, 5), (0.0, 1.0))
        self.assertEqual(charts.window_span(0.9, 0.3), (0.3, 0.9))
        start, end = charts.window_span(0.5, 0.5)
        self.assertGreaterEqual(end - start, 0.009)
        self.assertEqual(charts.window_span(None, None), (0.55, 1.0))
        self.assertEqual(charts.window_span("x", "y"), (0.55, 1.0))

    def test_window_indicator_rect(self):
        rect = charts.window_indicator_rect(0.55, 1.0, 0.0, 400.0, 50.0)
        box, block = rect["box"], rect["block"]
        self.assertGreaterEqual(block[0], box[0])
        self.assertLessEqual(block[2], box[2])
        self.assertLessEqual(box[2], 400.0)
        full = charts.window_indicator_rect(0.0, 1.0, 0.0, 400.0, 50.0)
        self.assertAlmostEqual(full["block"][0], full["box"][0])
        narrow = charts.window_indicator_rect(0.9, 0.95, 0.0, 400.0, 50.0)
        self.assertLess(narrow["block"][2] - narrow["block"][0],
                        full["block"][2] - full["block"][0])

    def test_fmt_axis(self):
        self.assertEqual(charts.fmt_axis(None, "number"), "—")
        self.assertEqual(charts.fmt_axis(0, "number"), "0")
        self.assertEqual(charts.fmt_axis(12.5, "number"), "12.50")
        self.assertEqual(charts.fmt_axis(-3.2, "pct"), "-3.20%")
        self.assertEqual(charts.fmt_axis(12345.0, "money"), theme.fmt_money(12345.0, 0))
        self.assertTrue(charts.fmt_axis(1234.0, "wan").endswith("万"))
        self.assertTrue(charts.fmt_axis(123456789.0, "wan").endswith("亿"))

    def test_text_tools(self):
        self.assertEqual(charts.elide_text("短", 100.0), "短")
        long_text = charts.elide_text("很长很长很长的板块名称", 30.0, 9)
        self.assertLess(len(long_text), 10)
        self.assertTrue(long_text.endswith("…"))
        self.assertEqual(charts.elide_text("abc", 0.0), "")
        self.assertGreater(charts.estimate_text_width("中文", 9), charts.estimate_text_width("ab", 9))

    def test_normalize_series(self):
        series = charts.normalize_series([{"name": "净值", "values": [1, 2]},
                                          [3, 4], "垃圾", {"values": None}])
        self.assertEqual(len(series), 3)
        self.assertEqual(series[0]["name"], "净值")
        self.assertFalse(series[0]["dashed"])
        self.assertTrue(series[0]["color"].startswith("#"))
        self.assertEqual(series[1]["values"], [3, 4])
        self.assertEqual(series[2]["values"], [])
        self.assertEqual(charts.normalize_series(None), [])


# =========================================================================== GUI 烟雾测试
class ChartGuiSmokeTest(unittest.TestCase):
    """真实 canvas：空状态、含 None 数据、尺寸变化重绘、hover。"""

    @classmethod
    def setUpClass(cls):
        _require_charts()
        import tkinter as tk
        try:
            cls.root = tk.Tk()
        except Exception as exc:                                  # noqa: BLE001
            raise unittest.SkipTest("无法创建 Tk 窗口（无显示器？）：%s" % exc)
        cls.root.withdraw()                                       # 不弹窗
        theme.init(cls.root)
        cls.frame = tk.Frame(cls.root, bg=theme.COLORS["panel"])
        cls.frame.pack(fill="both", expand=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:                                         # noqa: BLE001
            pass

    # ---- 工具 ----
    def make(self, factory, **kwargs):
        chart = factory(self.frame, **kwargs)
        chart.pack(fill="both", expand=True)
        self.addCleanup(self._safe_destroy, chart)
        self.flush()
        return chart

    def make_fixed_size(self, factory, **kwargs):
        """不 pack 的图表：绘图区只由 -width/-height 决定，便于断言尺寸变化。"""
        chart = factory(self.frame, **kwargs)
        self.addCleanup(self._safe_destroy, chart)
        return chart

    @staticmethod
    def _safe_destroy(widget):
        try:
            widget.destroy()
        except Exception:                                         # noqa: BLE001
            pass

    def flush(self, milliseconds=80):
        """让 ``after`` 防抖任务真正跑起来（不跑 mainloop）。"""
        try:
            self.root.update_idletasks()
        except Exception:                                         # noqa: BLE001
            return
        time.sleep(milliseconds / 1000.0)
        try:
            self.root.update()
        except Exception:                                         # noqa: BLE001
            pass

    def assert_no_error(self, chart):
        self.assertIsNone(chart.last_error, "redraw 吞掉了异常：%r" % (chart.last_error,))

    def assert_items(self, chart):
        self.assert_no_error(chart)
        self.assertGreater(len(chart.find_all()), 0, "画布上没有任何 canvas 项")

    def assert_empty_state(self, chart, text="暂无数据"):
        """空状态必须是「绘图区居中的灰字」，且带 EMPTY_TAG。"""
        self.assert_no_error(chart)
        items = chart.find_withtag(charts.EMPTY_TAG)
        self.assertEqual(len(items), 1, "空数据时应只有一条空状态文字")
        self.assertEqual(chart.itemcget(items[0], "text"), text)
        self.assertEqual(chart.itemcget(items[0], "fill"), theme.COLORS["text2"], "空状态是灰字")
        x0, y0, x1, y1 = chart.plot_area()
        coords = chart.coords(items[0])
        self.assertAlmostEqual(coords[0], (x0 + x1) / 2.0, places=3, msg="空状态水平居中")
        self.assertAlmostEqual(coords[1], (y0 + y1) / 2.0, places=3, msg="空状态垂直居中")

    def assert_has_empty_state_support(self, factory, empty_call):
        """五种图表都要能画空状态、改空状态文字。"""
        chart = self.make(factory)
        empty_call(chart)
        self.assert_empty_state(chart)
        chart.set_empty("加载中…")
        self.assert_empty_state(chart, "加载中…")
        self.assertEqual(chart.empty_text, "加载中…")

    # ---- 1) 五种图表：正常 / 空 / 含 None ----
    def test_line_chart(self):
        chart = self.make(charts.LineChart)
        chart.set_data([10 + index * 0.3 for index in range(40)],
                       dates=["2024-01-%02d" % (index % 28 + 1) for index in range(40)])
        self.assert_items(chart)
        self.assertGreater(len(chart.find_withtag("series")), 0)
        self.assertGreater(len(chart.find_withtag("fill")), 0, "默认要有渐变填充")
        chart.set_data([1.0, None, 3.0, 4.0], fill=False)
        self.assert_items(chart)
        chart.set_data([None, None])
        self.assert_empty_state(chart)

    def test_multi_line_chart(self):
        chart = self.make(charts.MultiLineChart)
        chart.set_data([{"name": "净值", "values": [1 + index * 0.01 for index in range(30)]},
                        {"name": "基准", "values": [1 + index * 0.004 for index in range(30)],
                         "color": theme.COLORS["cyan"], "dashed": True}],
                       dates=["D%02d" % index for index in range(30)], kind="pct")
        self.assert_items(chart)
        self.assertGreater(len(chart.find_withtag(charts.LEGEND_TAG)), 0, "要有图例")
        chart.set_data([{"name": "空", "values": []}, {"name": "None", "values": [None, None]}])
        self.assert_empty_state(chart)

    def test_candle_chart(self):
        chart = self.make(charts.CandleChart, height=260)
        chart.set_data(_sample_bars(80), ma_periods=(5, 20, 60))
        self.assert_items(chart)
        self.assertGreater(len(chart.find_withtag("candle")), 0)
        self.assertGreater(len(chart.find_withtag("volume")), 0, "默认要画成交量")
        self.assertGreater(len(chart.find_withtag(charts.LEGEND_TAG)), 0, "要有 MA 图例")
        legend_names = [chart.itemcget(item, "text") for item in chart.find_withtag(charts.LEGEND_TAG)
                        if chart.type(item) == "text"]
        self.assertIn("MA5", legend_names)
        self.assertIn("MA20", legend_names)
        self.assertIn("MA60", legend_names, "只看最近 45% 时 MA60 也该有值（均线在全序列上算）")
        self.assertGreaterEqual(len(chart.find_withtag("series")), 3, "三条均线")
        # 70% / 30% 分区：上部蜡烛、下部成交量
        full = charts.ChartBase.plot_area(chart)
        price = chart.plot_area()
        self.assertAlmostEqual((price[3] - price[1]) / (full[3] - full[1]), 0.70, places=2)
        volume = chart._volume_area()
        self.assertGreater(volume[1], price[3], "成交量区在蜡烛区下方")
        self.assertLessEqual(volume[3], full[3] + 1e-9)
        # MA 折线用 warn/brand/purple 三色
        colors_used = set(chart.itemcget(item, "fill") for item in chart.find_withtag("series"))
        self.assertTrue(colors_used.issubset(set(charts.MA_COLORS)), "均线颜色只能用 warn/brand/purple：%s" % colors_used)
        chart.set_data([{"date": "x", "close": None}, {"date": "y", "close": 3.0}])
        self.assert_items(chart)
        chart.set_data([])
        self.assert_empty_state(chart)

    def test_candle_window_and_volumes_flag(self):
        chart = self.make(charts.CandleChart, height=260)
        chart.set_data(_sample_bars(100))
        chart.set_window(0.5, 1.0)
        self.assert_items(chart)
        start, end = chart._window
        self.assertAlmostEqual(start, 0.5)
        visible = len(chart._visible_bars())
        self.assertGreater(visible, 10)
        self.assertLess(visible, 100)
        chart.set_window(-2, 7)                                   # 越界
        self.assertEqual(chart._window, (0.0, 1.0))
        chart.set_window(0.8, 0.2)                                # 反向
        self.assertLess(chart._window[0], chart._window[1])
        chart.set_data(_sample_bars(100), volumes=False)
        self.assertEqual(len(chart.find_withtag("volume")), 0, "volumes=False 不该画成交量")
        self.assert_items(chart)

    def test_bar_chart(self):
        chart = self.make(charts.BarChart)
        chart.set_data(["电子", "医药", "券商", "白酒"], [3.2, -1.4, 0.0, 5.1], kind="pct")
        self.assert_items(chart)
        self.assertGreaterEqual(len(chart.find_withtag("bar")), 3)
        self.assertEqual(chart.color_for_value(1.0), theme.COLORS["up"])
        self.assertEqual(chart.color_for_value(-1.0), theme.COLORS["down"])
        self.assertEqual(chart.color_for_value(0.0), theme.COLORS["flat"])
        chart.set_data(["a", None, "c"], [None, 0.0, "x"])
        self.assert_items(chart)
        chart.set_data([], [])
        self.assert_empty_state(chart)

    def test_bar_chart_horizontal(self):
        chart = self.make(charts.BarChart)
        chart.set_data(["电子元件", "医药制造", "证券"], [3.2, -1.4, 2.0], horizontal=True,
                       kind="pct", show_values=True, positive_color=theme.COLORS["brand"],
                       negative_color=theme.COLORS["cyan"])
        self.assert_items(chart)
        self.assertEqual(chart.padding, charts.HORIZONTAL_PADDING)
        self.assertEqual(chart.color_for_value(1.0), theme.COLORS["brand"])
        self.assertEqual(chart.color_for_value(-1.0), theme.COLORS["cyan"])

    def test_donut_chart(self):
        chart = self.make(charts.DonutChart)
        chart.set_data([{"name": "沪深300", "value": 42.1}, {"name": "中证500", "value": 28.4},
                        {"name": "现金", "value": 0}, {"name": "创业板", "value": 15.5},
                        {"name": "债券", "value": 14.0}])
        self.assert_items(chart)
        self.assertEqual(len(chart.find_withtag("slice")), 4, "0 值项不画扇区")
        self.assertGreater(len(chart.find_withtag(charts.LEGEND_TAG)), 0)
        width, height = chart._effective_size()
        for item in chart.find_withtag("slice"):
            self.assertEqual(chart.itemcget(item, "style"), "arc", "Tk 8.5 支持的环形画法")
            self.assertGreaterEqual(float(chart.itemcget(item, "width")), 8.0, "必须是有厚度的环带")
            box = chart.bbox(item)
            self.assertIsNotNone(box)
            self.assertGreaterEqual(box[0], -2.0)
            self.assertLessEqual(box[2], width + 2.0, "环形不能溢出画布")
            self.assertLessEqual(box[3], height + 2.0)
        legend_texts = [chart.itemcget(item, "text") for item in chart.find_withtag(charts.LEGEND_TAG)
                        if chart.type(item) == "text"]
        self.assertTrue(any(text.endswith("%") for text in legend_texts), "图例要有占比：%s" % legend_texts)
        self.assertIn("沪深300", legend_texts)
        chart.set_data([{"name": "全 0", "value": 0}, {"name": "负", "value": -1}])
        self.assert_empty_state(chart)
        chart.set_data([{"name": "只有一项", "value": 3}])
        self.assert_items(chart)

    def test_all_charts_accept_empty_state_text(self):
        self.assert_has_empty_state_support(charts.LineChart, lambda chart: chart.set_data([]))
        self.assert_has_empty_state_support(charts.MultiLineChart, lambda chart: chart.set_data([]))
        self.assert_has_empty_state_support(charts.CandleChart, lambda chart: chart.set_data([]))
        self.assert_has_empty_state_support(charts.BarChart, lambda chart: chart.set_data([], []))
        self.assert_has_empty_state_support(charts.DonutChart, lambda chart: chart.set_data([]))

    # ---- 2) 畸形数据不崩 ----
    def test_malformed_data_never_raises(self):
        charts_and_data = [
            (charts.LineChart, (["abc", None, {}, 1.0, float("nan"), 3.0],)),
            (charts.MultiLineChart, ([{"name": None, "values": ["x", None, 2.0]}, "垃圾"],)),
            (charts.CandleChart, ([{"close": None}, {"close": "x"}, {}, 5],)),
            (charts.BarChart, (["a", None], [None, float("inf")])),
            (charts.DonutChart, ([{"name": "a", "value": "x"}, {"name": None, "value": None}],)),
        ]
        for factory, args in charts_and_data:
            with self.subTest(chart=factory.__name__):
                chart = self.make(factory)
                chart.set_data(*args)
                self.assert_no_error(chart)
                self.assertGreater(len(chart.find_all()), 0, "空状态也算有内容")


    def test_extreme_sizes_do_not_crash(self):
        """极窄/极扁/1x1 的画布也必须能画完（否则拖动窗口到极限会崩）。"""
        charts_and_data = [
            (charts.LineChart, ([1.0, 2.0, 3.0],)),
            (charts.MultiLineChart, ([{"name": "a", "values": [1.0, 2.0]}],)),
            (charts.CandleChart, (_sample_bars(5),)),
            (charts.BarChart, (["a", "b"], [1.0, -1.0])),
            (charts.DonutChart, ([{"name": "a", "value": 1}, {"name": "b", "value": 2}],)),
        ]
        for factory, args in charts_and_data:
            for width, height in ((1, 1), (8, 12), (30, 240), (240, 30), (900, 420)):
                with self.subTest(chart=factory.__name__, size=(width, height)):
                    chart = self.make_fixed_size(factory, width=width, height=height)
                    chart.set_data(*args)
                    self.assert_no_error(chart)
                    self.assertGreater(len(chart.find_all()), 0)
                    x0, y0, x1, y1 = chart.plot_area()
                    self.assertLess(x0, x1)
                    self.assertLess(y0, y1)
    def test_single_point_and_flat_series(self):
        chart = self.make(charts.LineChart)
        chart.set_data([5.0])
        self.assert_items(chart)
        x0, _y0, x1, _y1 = chart.plot_area()
        self.assertTrue(x0 <= chart.map_x(0) <= x1)
        chart.set_data([7.0] * 15)
        self.assert_items(chart)
        chart = self.make(charts.BarChart)
        chart.set_data(["只有一项"], [0.0])
        self.assert_items(chart)

    # ---- 3) 尺寸变化 → 防抖重绘 ----
    def test_configure_binding_and_debounce(self):
        chart = self.make(charts.LineChart)
        chart.set_data([1.0, 5.0, 3.0, 9.0, 4.0], dates=["a", "b", "c", "d", "e"])
        self.flush()
        self.assertTrue(chart.bind("<Configure>"), "必须绑定 <Configure>")
        calls = []
        original = chart.redraw
        chart.redraw = lambda: (calls.append(1), original())[1]
        # withdraw 的窗口收不到真实 <Configure>，直接调用处理函数验证防抖行为
        chart._on_configure()
        chart._on_configure()
        chart._on_configure()
        self.assertIsNotNone(chart._redraw_job, "应排入防抖任务而不是立刻重绘")
        self.assertEqual(calls, [], "防抖期间不应该重绘")
        self.flush()
        self.assertEqual(len(calls), 1, "连续多次 <Configure> 只重绘一次")
        self.assertIsNone(chart._redraw_job)

    def test_resize_redraws_with_new_size(self):
        chart = self.make_fixed_size(charts.LineChart, width=400, height=240)
        chart.set_data(list(range(20)), dates=["D%d" % index for index in range(20)])
        chart.configure(width=400, height=240)
        self.flush()
        before = chart.plot_area()
        chart.delete("all")
        self.assertEqual(len(chart.find_all()), 0)
        chart.configure(width=760, height=300)
        chart._on_configure()                                     # 模拟 <Configure>
        self.flush()
        after = chart.plot_area()
        self.assertGreater(after[2], before[2], "变宽后绘图区要跟着变宽")
        self.assertGreater(after[3], before[3], "变高后绘图区要跟着变高")
        self.assertGreater(len(chart.find_all()), 0, "尺寸变化后要重绘出来")
        self.assert_no_error(chart)

    # ---- 4) hover ----
    def test_line_hover_tooltip(self):
        chart = self.make_fixed_size(charts.LineChart, width=600, height=240)
        chart.configure(width=600, height=240)
        chart.set_data([10 + index for index in range(20)],
                       dates=["2024-01-%02d" % (index + 1) for index in range(20)])
        self.flush()
        self.assertGreater(len(chart._hover_points), 0)
        x0, y0, x1, y1 = chart.plot_area()
        point_x, point_y = chart._hover_points[10][0], chart._hover_points[10][1]
        chart._on_motion(types.SimpleNamespace(x=int(point_x), y=int(point_y)))
        hover_items = chart.find_withtag(charts.HOVER_TAG)
        self.assertGreaterEqual(len(hover_items), 3, "hover 要有竖线 + 圆点 + 标签")
        kinds = set(chart.type(item) for item in hover_items)
        self.assertIn("line", kinds)
        self.assertIn("text", kinds)
        chart._on_leave()
        self.assertEqual(len(chart.find_withtag(charts.HOVER_TAG)), 0, "移出画布要清掉 hover")
        chart._on_motion(types.SimpleNamespace(x=0, y=0))
        self.assertEqual(len(chart.find_withtag(charts.HOVER_TAG)), 0, "绘图区外不显示 hover")
        chart._on_motion(types.SimpleNamespace(x=int(x0) + 2, y=int((y0 + y1) / 2)))
        self.assertGreaterEqual(len(chart.find_withtag(charts.HOVER_TAG)), 3)
        self.assert_no_error(chart)

    # ---- 5) 坐标映射 ----
    def test_map_x_and_map_y(self):
        chart = self.make(charts.LineChart)
        chart.set_data([1.0, 2.0, 3.0])
        x0, y0, x1, y1 = chart.plot_area()
        self.assertAlmostEqual(chart.map_x(0), x0, places=6)
        self.assertAlmostEqual(chart.map_x(2), x1, places=6)
        self.assertAlmostEqual(chart.map_y(10.0, 0.0, 10.0), y0, places=6)
        self.assertAlmostEqual(chart.map_y(0.0, 0.0, 10.0), y1, places=6)
        self.assertIsNone(chart.map_y(None, 0.0, 10.0))
        self.assertEqual(chart.plot_area()[0], chart.padding[0])

    def test_static_helpers_exposed(self):
        self.assertEqual(charts.ChartBase.nice_ticks(0.0, 10.0), charts.nice_ticks(0.0, 10.0))
        self.assertEqual(charts.ChartBase.fmt_axis(1.5, "pct"), charts.fmt_axis(1.5, "pct"))
        self.assertEqual(charts.ChartBase.compute_ticks(0.0, 1.0), charts.compute_ticks(0.0, 1.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
