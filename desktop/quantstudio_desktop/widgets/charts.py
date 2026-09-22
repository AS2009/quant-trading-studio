# -*- coding: utf-8 -*-
"""纯 ``tkinter.Canvas`` 图表引擎（零第三方依赖，兼容 macOS 系统 Tk 8.5.9）。

提供五种图表：``LineChart``（折线+渐变填充+hover）/ ``MultiLineChart``（多线+图例）/
``CandleChart``（蜡烛+成交量+均线+显示窗口）/ ``BarChart``（竖柱/横条+正负着色）/
``DonutChart``（环形+多列图例）。

设计约定
--------
- **数据 → 几何**：模块级纯函数完成（``nice_ticks`` / ``compute_ticks`` /
  ``candle_geometry`` / ``volume_geometry`` / ``bar_geometry`` / ``donut_slices`` /
  ``line_runs`` / ``map_value`` …），不接触 canvas，可单独单元测试；
- **几何 → canvas 项**：各图表 ``draw()`` 只做「坐标 → item」的搬运；
- 颜色一律来自 :mod:`quantstudio_desktop.theme`（``COLORS`` 或 ``theme.mix`` 派生）。
  Tk 画布没有 alpha 通道，半透明效果用 ``theme.mix(背景色, 前景色, alpha)`` 预先混色模拟；
- 只使用 Tk 8.5 就有的 canvas 项与选项：不用 ``create_round_rect``、不用旋转文字
  （``-angle`` 是 8.6 才有的）、不用 ``ttk.Spinbox``；``create_arc(style="arc")`` 8.5 可用；
- 稳健性：空数据/单点/全等值/含 ``None`` 的数据都不允许抛异常；
  ``redraw()`` 内部兜底并把异常记到 ``last_error``（设 ``QUANTSTUDIO_CHART_DEBUG=1`` 时打印堆栈）；
- 尺寸变化：``<Configure>`` → 40ms 防抖 → ``redraw()``，拖拽窗口时不会狂重绘。

Widget 约定（供页面使用）::

    chart = widgets.charts.CandleChart(frame, height=260)
    chart.pack(fill="both", expand=True)
    chart.set_data(bars, ma_periods=(5, 20, 60))
    chart.set_window(0.55, 1.0)          # 默认显示最近 45%（CandleChart 的初始值）
"""

import math
import os
import tkinter as tk
import traceback
from typing import Any, Dict, List, Optional, Tuple

from .. import theme

__all__ = [
    # 图表类
    "ChartBase", "LineChart", "MultiLineChart", "CandleChart", "BarChart", "DonutChart",
    # 纯函数（数据 → 几何）
    "nice_ticks", "compute_ticks", "fmt_axis", "map_linear", "map_value", "map_index", "map_xs",
    "band_bounds", "value_range", "series_range", "sample_indices", "moving_average",
    "moving_averages", "line_runs", "candle_geometry", "volume_geometry", "bar_geometry",
    "donut_slices", "window_span", "window_indicator_rect", "estimate_text_width", "elide_text",
    "normalize_series", "clean_values", "to_float", "clamp",
    # 常量
    "DEFAULT_PADDING", "HORIZONTAL_PADDING", "DONUT_PADDING", "EMPTY_TAG", "HOVER_TAG",
    "KINDS", "DONUT_PALETTE",
]

EMPTY_TEXT = "暂无数据"
DEFAULT_PADDING = (52, 16, 18, 34)           # (左, 上, 右, 下)——默认给 Y 轴刻度与日期留位
HORIZONTAL_PADDING = (88, 12, 34, 24)        # 横向条：左侧留给板块名、右侧留给数值
DONUT_PADDING = (18, 14, 18, 14)
EMPTY_TAG = "empty"
HOVER_TAG = "hover"
AXIS_TAG = "axis"
GRID_TAG = "grid"
LEGEND_TAG = "legend"
KINDS = ("number", "money", "pct", "wan")
_DEBUG = os.environ.get("QUANTSTUDIO_CHART_DEBUG", "") not in ("", "0", "false", "False", "no")


# =========================================================================== 数据 → 几何（纯函数）
def clamp(value: float, low: float, high: float) -> float:
    """把 ``value`` 夹到 ``[low, high]``（low 可以大于 high 时自动交换）。"""
    if low > high:
        low, high = high, low
    return max(float(low), min(float(high), float(value)))


def to_float(value) -> Optional[float]:
    """宽松转 float：``None`` / 非数值 / 布尔 / NaN / inf → ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def clean_values(values) -> List[Optional[float]]:
    """逐项 :func:`to_float`（保持长度，无效项为 ``None``，绝不抛异常）。"""
    if values is None:
        return []
    try:
        items = list(values)
    except TypeError:
        return []
    return [to_float(item) for item in items]


def value_range(values, pad: float = 0.0) -> Tuple[Optional[float], Optional[float]]:
    """有效数值的 ``(lo, hi)``；全等值时给出对称区间；无有效值 → ``(None, None)``。

    ``pad`` 为上下额外留白的比例（0.06 表示各留 6%）。
    """
    numbers = [number for number in clean_values(values) if number is not None]
    if not numbers:
        return (None, None)
    lo, hi = min(numbers), max(numbers)
    if hi - lo <= 0:
        span = abs(hi) * 0.05 or 1.0
        lo, hi = lo - span, hi + span
    if pad:
        grow = (hi - lo) * float(pad)
        lo, hi = lo - grow, hi + grow
    return (lo, hi)


def series_range(series_list, pad: float = 0.0) -> Tuple[Optional[float], Optional[float]]:
    """多组数据的联合 ``(lo, hi)``（用于多线图共享 Y 轴）。"""
    pool: List[Any] = []
    for item in series_list or []:
        if isinstance(item, dict):
            pool.extend(list(item.get("values") or []))
        elif isinstance(item, (list, tuple)):
            pool.extend(list(item))
    return value_range(pool, pad)


def map_linear(value, lo: float, hi: float, a: float, b: float) -> Optional[float]:
    """线性映射 ``[lo,hi] → [a,b]``；``value`` 无效返回 ``None``，``lo==hi`` 取中点。"""
    number = to_float(value)
    if number is None:
        return None
    if hi == lo:
        return (a + b) / 2.0
    return a + (number - lo) / float(hi - lo) * (b - a)


def map_value(value, lo: float, hi: float, y0: float, y1: float) -> Optional[float]:
    """数值 → 像素 y（``y0`` 在上、``y1`` 在下，符合 canvas 坐标系）。"""
    return map_linear(value, lo, hi, y1, y0)


def map_index(index: int, count: int, x0: float, x1: float) -> float:
    """第 ``index`` 个点的像素 x（首尾点贴住 x0/x1；只有一个点取中点）。"""
    if count <= 1:
        return (x0 + x1) / 2.0
    position = clamp(index, 0, count - 1)
    return x0 + position / float(count - 1) * (x1 - x0)


def band_bounds(index: int, count: int, a0: float, a1: float, gap_ratio: float = 0.18) -> Tuple[float, float]:
    """把 ``[a0,a1]`` 等分成 ``count`` 个槽，返回第 ``index`` 槽的 ``(起点, 终点)``。

    蜡烛/柱状图用：``gap_ratio`` 是槽内左右留白比例（0.22 → 实体占 78% 槽宽）。
    """
    count = max(1, int(count))
    slot = (a1 - a0) / float(count)
    gap = slot * clamp(gap_ratio, 0.0, 0.9)
    left = a0 + index * slot
    return (left + gap / 2.0, left + slot - gap / 2.0)


def map_xs(count: int, x0: float, x1: float, mode: str = "point", gap_ratio: float = 0.0) -> List[float]:
    """一次性算出每个数据点的像素 x。

    ``mode="point"``：首尾点贴住 x0/x1（折线图）；``mode="band"``：返回等宽槽中心（蜡烛/柱状）。
    """
    count = max(0, int(count))
    if count <= 0:
        return []
    if mode == "band":
        centers = []
        for index in range(count):
            left, right = band_bounds(index, count, x0, x1, gap_ratio)
            centers.append((left + right) / 2.0)
        return centers
    if count == 1:
        return [(x0 + x1) / 2.0]
    return [map_index(index, count, x0, x1) for index in range(count)]


def _clean_tick(value: float, step: float) -> float:
    """去掉二进制浮点噪声（0.30000000000000004 → 0.3）。"""
    if step:
        value = round(value / step) * step
    if step and abs(value) < abs(step) * 1e-9:
        return 0.0
    return float("%.12g" % value)


def nice_ticks(lo: float, hi: float, count: int = 5) -> List[float]:
    """生成覆盖 ``[lo, hi]`` 的"漂亮"刻度（步长为 ``1/2/5 × 10^k``）。

    - 返回值一定满足 ``ticks[0] <= lo`` 且 ``ticks[-1] >= hi``（含端点、完整覆盖）；
    - ``lo == hi`` 时围绕该值造一个 ±5%（或 ±1）的对称区间；
    - ``lo > hi`` 自动交换；输入含 NaN/inf → 返回 ``[]``；
    - ``count`` 只是「大约几格」的建议，刻度数量不保证等于 ``count``。
    """
    low, high = to_float(lo), to_float(hi)
    if low is None or high is None:
        return []
    if high < low:
        low, high = high, low
    if high - low <= 0:
        span = abs(high) * 0.05 or 1.0
        low, high = high - span, high + span
    count = max(2, int(count))
    raw = (high - low) / float(count - 1)
    exponent = int(math.floor(math.log10(raw)))
    step = 10.0 ** (exponent + 1)
    for factor in (1.0, 2.0, 5.0, 10.0):
        if raw <= factor * (10.0 ** exponent):
            step = factor * (10.0 ** exponent)
            break
    if not math.isfinite(step) or step <= 0:
        step = (high - low) / float(count)

    start = math.floor(low / step) * step
    end = math.ceil(high / step) * step
    guard = 0
    while (end - start) / step > 40.0 and guard < 12:      # 极端范围下别把刻度画爆
        step *= 10.0
        start = math.floor(low / step) * step
        end = math.ceil(high / step) * step
        guard += 1
    total = int(round((end - start) / step))
    return [_clean_tick(start + index * step, step) for index in range(max(1, total) + 1)]


def compute_ticks(lo: float, hi: float, count: int = 5) -> Tuple[List[float], Optional[float], Optional[float]]:
    """返回 ``(ticks, lo_padded, hi_padded)``：把数据范围对齐到刻度端点后的映射区间。

    无效输入 → ``([], None, None)``；调用方用 ``lo_padded/hi_padded`` 做像素映射，
    这样数据点永远不会落在网格线之外。
    """
    low, high = to_float(lo), to_float(hi)
    if low is None or high is None:
        return ([], None, None)
    if high < low:
        low, high = high, low
    ticks = nice_ticks(low, high, count)
    if not ticks:
        return ([], low, high)
    return (ticks, ticks[0], ticks[-1])


def sample_indices(count: int, target: int = 6) -> List[int]:
    """在 ``range(count)`` 里均匀抽稀出至多 ``target`` 个索引（首尾一定保留、去重、升序）。"""
    count = int(count)
    if count <= 0:
        return []
    target = max(1, int(target))
    if count <= target:
        return list(range(count))
    if target == 1:
        return [0]
    picked: List[int] = []
    for step in range(target):
        index = int(round(step * (count - 1) / float(target - 1)))
        if index not in picked:
            picked.append(index)
    return picked


def moving_average(values, period: int) -> List[Optional[float]]:
    """简单移动平均（长度与输入一致）；窗口内出现 ``None`` 时该点输出 ``None``。"""
    numbers = clean_values(values)
    period = int(period or 0)
    result: List[Optional[float]] = [None] * len(numbers)
    if period <= 0 or len(numbers) < period:
        return result
    window_sum = 0.0
    window_bad = 0
    for index, number in enumerate(numbers):
        if number is None:
            window_bad += 1
        else:
            window_sum += number
        if index >= period:
            dropped = numbers[index - period]
            if dropped is None:
                window_bad -= 1
            else:
                window_sum -= dropped
        if index >= period - 1 and window_bad == 0:
            result[index] = window_sum / float(period)
    return result


def moving_averages(values, periods) -> List[Dict[str, Any]]:
    """``[(period, ma_values), ...]``；``periods`` 会被去重、过滤非正数并限制到 3 条。"""
    cleaned: List[int] = []
    for item in periods or []:
        try:
            period = int(item)
        except (TypeError, ValueError):
            continue
        if period > 0 and period not in cleaned:
            cleaned.append(period)
    return [{"period": period, "values": moving_average(values, period)} for period in cleaned[:3]]


def line_runs(values, lo: float, hi: float, xs, y0: float, y1: float,
              max_points: Optional[int] = None) -> List[List[Tuple[float, float, int, float]]]:
    """把序列切成「连续有效段」：``[[(x, y, index, value), ...], ...]``。

    ``None``（或非法值）会把折线断开成多段；``max_points`` 用于超长序列抽稀（首尾点保留）。
    """
    numbers = clean_values(values)
    positions = [float(item) for item in (xs or [])]
    limit = min(len(numbers), len(positions))
    if limit <= 0:
        return []
    indices = list(range(limit))
    if max_points is not None and int(max_points or 0) > 1 and len(indices) > int(max_points):
        indices = sample_indices(len(indices), int(max_points))
    runs: List[List[Tuple[float, float, int, float]]] = []
    current: List[Tuple[float, float, int, float]] = []
    for index in indices:
        value = numbers[index]
        if value is None:
            if current:
                runs.append(current)
                current = []
            continue
        y = map_value(value, lo, hi, y0, y1)
        if y is None:
            continue
        current.append((positions[index], clamp(y, y0, y1), index, value))
    if current:
        runs.append(current)
    return runs


def _as_bars(bars, max_bars: Optional[int] = None) -> List[Dict[str, Any]]:
    """只保留 dict 形式的 bar，并可按 ``max_bars`` 取尾部（超长序列的性能兜底）。"""
    if bars is None:
        return []
    try:
        items = [bar for bar in list(bars) if isinstance(bar, dict)]
    except TypeError:
        return []
    if max_bars:
        limit = int(max_bars)
        if limit > 0:
            items = items[-limit:]
    return items


def candle_geometry(bars, lo: float, hi: float, x0: float, y0: float, x1: float, y1: float,
                    min_body: float = 1.0, gap_ratio: float = 0.22,
                    max_bars: Optional[int] = None) -> List[Dict[str, Any]]:
    """蜡烛图几何（纯函数）：返回每根 K 线的像素矩形与影线坐标。

    每根返回 ``{"index","date","open","close","high","low","volume_wan","center","left","right",
    "y_open","y_close","y_high","y_low","body_top","body_bottom","body_height","rising","color_key","valid"}``。

    - ``close`` 无效（``None``/非数值）→ ``valid=False``，其余字段置 0，调用方跳过；
    - ``open`` 缺失按 ``close`` 处理；``high/low`` 缺失或与实体矛盾时用实体端点补齐；
    - 实体最小高度 ``min_body``（1px），全等值数据也能看到一条横线；
    - ``y_high <= body_top <= body_bottom <= y_low`` 一定成立。
    """
    items = _as_bars(bars, max_bars)
    count = len(items)
    top, bottom = float(y0), float(y1)
    result: List[Dict[str, Any]] = []
    for index, bar in enumerate(items):
        left, right = band_bounds(index, count, x0, x1, gap_ratio)
        record: Dict[str, Any] = {
            "index": index, "date": str(bar.get("date") or ""), "open": None, "close": None,
            "high": None, "low": None, "volume_wan": to_float(bar.get("volume_wan")),
            "center": (left + right) / 2.0, "left": left, "right": right,
            "y_open": bottom, "y_close": bottom, "y_high": bottom, "y_low": bottom,
            "body_top": bottom, "body_bottom": bottom, "body_height": 0.0,
            "rising": True, "color_key": "up", "valid": False,
        }
        close = to_float(bar.get("close"))
        if close is None:
            result.append(record)
            continue
        open_ = to_float(bar.get("open"))
        if open_ is None:
            open_ = close
        high = to_float(bar.get("high"))
        low = to_float(bar.get("low"))
        high = max([item for item in (high, open_, close) if item is not None])
        low = min([item for item in (low, open_, close) if item is not None])
        y_open = clamp(map_value(open_, lo, hi, top, bottom), top, bottom)
        y_close = clamp(map_value(close, lo, hi, top, bottom), top, bottom)
        y_high = clamp(map_value(high, lo, hi, top, bottom), top, bottom)
        y_low = clamp(map_value(low, lo, hi, top, bottom), top, bottom)
        body_top, body_bottom = min(y_open, y_close), max(y_open, y_close)
        if body_bottom - body_top < min_body:
            middle = (body_top + body_bottom) / 2.0
            body_top = middle - min_body / 2.0
            body_bottom = middle + min_body / 2.0
        body_top = clamp(body_top, min(y_high, top), bottom)
        body_bottom = clamp(body_bottom, top, max(y_low, bottom))
        rising = close >= open_
        record.update({
            "open": open_, "close": close, "high": high, "low": low,
            "y_open": y_open, "y_close": y_close, "y_high": y_high, "y_low": y_low,
            "body_top": body_top, "body_bottom": body_bottom,
            "body_height": body_bottom - body_top,
            "rising": rising, "color_key": "up" if rising else "down", "valid": True,
        })
        result.append(record)
    return result


def volume_geometry(bars, lo: float, hi: float, x0: float, y0: float, x1: float, y1: float,
                    gap_ratio: float = 0.22, min_height: float = 1.0,
                    max_bars: Optional[int] = None) -> List[Dict[str, Any]]:
    """成交量柱几何：值取 ``bar["volume_wan"]``，基线是 ``y1``（下部区域底边）。

    返回 ``{"index","date","left","right","top","bottom","value","rising","color_key","valid"}``；
    值为 0/负数或缺失时 ``top == bottom == y1``（不画柱子），正数柱至少 ``min_height`` 高。
    """
    items = _as_bars(bars, max_bars)
    count = len(items)
    top_limit = float(min(y0, y1))
    baseline = float(max(y0, y1))
    result: List[Dict[str, Any]] = []
    for index, bar in enumerate(items):
        value = to_float(bar.get("volume_wan"))
        close = to_float(bar.get("close"))
        open_ = to_float(bar.get("open"))
        rising = True if (close is None or open_ is None) else close >= open_
        left, right = band_bounds(index, count, x0, x1, gap_ratio)
        record: Dict[str, Any] = {
            "index": index, "date": str(bar.get("date") or ""), "value": value,
            "left": left, "right": right, "top": baseline, "bottom": baseline,
            "rising": rising, "color_key": "up" if rising else "down", "valid": value is not None,
        }
        if value is not None and value > 0 and hi > lo:
            y = clamp(map_value(value, lo, hi, top_limit, baseline) or baseline, top_limit, baseline)
            if baseline - y < min_height:
                y = max(top_limit, baseline - min_height)
            record["top"] = y
        result.append(record)
    return result


def bar_geometry(labels, values, x0: float, y0: float, x1: float, y1: float, horizontal: bool = False,
                 lo: Optional[float] = None, hi: Optional[float] = None, gap_ratio: float = 0.35,
                 min_size: float = 1.0, colors=None) -> List[Dict[str, Any]]:
    """柱/条几何（纯函数）：竖向柱以 ``y1``、横向条以 ``x0`` 侧为 0 基准。

    返回 ``{"index","label","value","valid","left","right","top","bottom","base","color_value","color"}``；
    ``lo/hi`` 缺省时自动算成「含 0」的范围；柱子尺寸至少 ``min_size``（1px）；
    ``colors`` 是按序号覆盖颜色的列表（可含 ``None`` 表示用涨跌色）。
    """
    numbers = clean_values(values)
    count = len(numbers)
    label_list = ["" if item is None else str(item) for item in (labels or [])]
    color_list = list(colors or [])
    range_lo, range_hi = to_float(lo), to_float(hi)
    if range_lo is None or range_hi is None:
        finite = [item for item in numbers if item is not None]
        range_lo = min([0.0] + finite) if finite else -1.0
        range_hi = max([0.0] + finite) if finite else 1.0
    if range_hi - range_lo <= 0:
        range_lo, range_hi = range_lo - 1.0, range_hi + 1.0
    result: List[Dict[str, Any]] = []
    for index, number in enumerate(numbers):
        label = label_list[index] if index < len(label_list) else str(index + 1)
        color = color_list[index] if index < len(color_list) and color_list[index] else None
        record: Dict[str, Any] = {
            "index": index, "label": label, "value": number, "valid": number is not None,
            "left": float(x0), "right": float(x0), "top": float(y0), "bottom": float(y0),
            "base": None, "color_value": number if number is not None else 0.0, "color": color,
        }
        if number is not None and horizontal:
            band_top, band_bottom = band_bounds(index, count, y0, y1, gap_ratio)
            base = clamp(map_linear(0.0, range_lo, range_hi, x0, x1), x0, x1)
            point = clamp(map_linear(number, range_lo, range_hi, x0, x1), x0, x1)
            left, right = min(base, point), max(base, point)
            if right - left < min_size:
                if number >= 0:
                    right = min(float(x1), left + min_size)
                else:
                    left = max(float(x0), right - min_size)
            record.update({"left": left, "right": right, "top": band_top, "bottom": band_bottom, "base": base})
        elif number is not None:
            band_left, band_right = band_bounds(index, count, x0, x1, gap_ratio)
            base = clamp(map_value(0.0, range_lo, range_hi, y0, y1), y0, y1)
            point = clamp(map_value(number, range_lo, range_hi, y0, y1), y0, y1)
            top, bottom = min(base, point), max(base, point)
            if bottom - top < min_size:
                if number >= 0:
                    top = max(float(y0), bottom - min_size)
                else:
                    bottom = min(float(y1), top + min_size)
            record.update({"left": band_left, "right": band_right, "top": top, "bottom": bottom, "base": base})
        result.append(record)
    return result


def donut_slices(items, start_angle: float = 90.0, clockwise: bool = True, colors=None) -> List[Dict[str, Any]]:
    """环形/饼图切片（纯函数）：返回 Tk ``create_arc`` 可直接用的 ``start``/``extent``。

    每项返回 ``{"index","name","value","ratio","pct","start","extent","span","color","zero"}``：

    - ``pct`` 为未取整的百分比（各项之和 = 100.0），绘图时再用 ``"%d%%"`` 显示；
    - 值为 ``None``/0/负数的项：``ratio = pct = span = extent = 0``（不占角度，但仍在图例里）；
    - 逆时针画时 ``extent`` 为正，顺时针为负（Tk 的角度是从 3 点钟方向逆时针）；
    - ``start_angle`` 默认 90（12 点钟方向），整圆会被压到 359.999° 以避开 Tk 8.5 的整圆边界情况。
    """
    palette = list(colors or DONUT_PALETTE) or list(DONUT_PALETTE)
    records: List[Dict[str, Any]] = []
    numbers: List[float] = []
    for item in items or []:
        data = item if isinstance(item, dict) else {}
        value = to_float(data.get("value"))
        value = value if (value is not None and value > 0) else 0.0
        numbers.append(value)
        records.append({"name": str(data.get("name") or ""), "value": value})
    total = sum(numbers)
    cursor = float(start_angle)
    result: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        value = numbers[index]
        ratio = (value / total) if total > 0 else 0.0
        span = 360.0 * ratio
        if span >= 359.999:
            span = 359.999
        if span <= 0:
            start, extent = cursor, 0.0
        elif clockwise:
            start, extent = cursor - span, -span
            cursor = start
        else:
            start, extent = cursor, span
            cursor = cursor + span
        result.append({
            "index": index, "name": record["name"], "value": value, "ratio": ratio,
            "pct": ratio * 100.0, "start": start, "extent": extent, "span": span,
            "color": palette[index % len(palette)], "zero": value <= 0,
        })
    return result


def window_span(start_ratio, end_ratio, default: Tuple[float, float] = (0.55, 1.0)) -> Tuple[float, float]:
    """把显示窗口比例规整为 ``0 <= start < end <= 1``（至少保留 1% 数据）。"""
    start = to_float(start_ratio)
    end = to_float(end_ratio)
    if start is None:
        start = float(default[0])
    if end is None:
        end = float(default[1])
    start = clamp(start, 0.0, 1.0)
    end = clamp(end, 0.0, 1.0)
    if end < start:
        start, end = end, start
    if end - start < 0.01:
        start = max(0.0, end - 0.01)
        if end - start < 0.01:
            end = min(1.0, start + 0.01)
    return (start, end)


def window_indicator_rect(start_ratio: float, end_ratio: float, x0: float, x1: float, y: float,
                          width: float = 92.0, height: float = 7.0) -> Dict[str, Tuple[float, float, float, float]]:
    """右下角「显示窗口」指示器的像素矩形：``{"box": (l,t,r,b), "block": (l,t,r,b)}``。"""
    right = float(x1) - 2.0
    left = max(float(x0) + 2.0, right - abs(float(width)))
    top = float(y) - abs(float(height)) / 2.0
    bottom = float(y) + abs(float(height)) / 2.0
    start, end = window_span(start_ratio, end_ratio)
    inner = right - left
    block_left = left + inner * start
    block_right = max(block_left + 2.0, left + inner * end)
    return {"box": (left, top, right, bottom), "block": (block_left, top, min(block_right, right), bottom)}


def estimate_text_width(text, font_size: int = 9) -> float:
    """粗略估算文字宽度（CJK 约 1.05 倍字号、其它约 0.58 倍）——用来做省略与图例布宽。"""
    total = 0.0
    for char in str(text):
        total += float(font_size) * (1.05 if ord(char) > 0x2E7F else 0.58)
    return total


def elide_text(text, max_width: float, font_size: int = 9, ellipsis: str = "…") -> str:
    """按估算宽度截断文字并加省略号（Tk 8.5 没有 ``-angle``/自动省略，只能自己算）。"""
    text = str(text)
    if max_width <= 0:
        return ""
    if estimate_text_width(text, font_size) <= max_width:
        return text
    budget = max_width - estimate_text_width(ellipsis, font_size)
    out = ""
    for char in text:
        if estimate_text_width(out + char, font_size) > budget:
            break
        out += char
    return (out + ellipsis) if out else ellipsis


def fmt_axis(value, kind: str = "number") -> str:
    """坐标轴/标签数值格式化：``kind`` ∈ number / money / pct / wan。"""
    number = to_float(value)
    if number is None:
        return "—"
    if kind == "money":
        return theme.fmt_money(number, 0 if abs(number) >= 1000 else 2)
    if kind == "pct":
        return theme.fmt_pct(number, 2, with_sign=False)
    if kind == "wan":
        if abs(number) >= 10000:
            return "%.2f亿" % (number / 10000.0)
        if abs(number) >= 100:
            return "%.0f万" % number
        return "%.1f万" % number
    if number == 0:
        return "0"
    if abs(number) >= 100:
        return theme.fmt_num(number, 0)
    if abs(number) >= 1:
        return theme.fmt_num(number, 2)
    return theme.fmt_num(number, 4)


def normalize_series(series) -> List[Dict[str, Any]]:
    """把 ``MultiLineChart.set_data`` 的输入规整为 ``[{"name","values","color","dashed"}]``。"""
    result: List[Dict[str, Any]] = []
    for index, item in enumerate(series or []):
        if isinstance(item, dict):
            values = item.get("values")
            name = item.get("name")
            color = item.get("color")
            dashed = item.get("dashed")
        elif isinstance(item, (list, tuple)):
            values, name, color, dashed = item, None, None, None
        else:
            continue
        try:
            values = list(values) if values is not None else []
        except TypeError:
            values = []
        result.append({
            "name": str(name or "系列%d" % (index + 1)),
            "values": values,
            "color": color or DONUT_PALETTE[index % len(DONUT_PALETTE)],
            "dashed": bool(dashed),
        })
    return result


def _palette() -> List[str]:
    colors = theme.COLORS
    return [
        colors["brand"], colors["cyan"], colors["warn"], colors["purple"], colors["down"],
        colors["up"], colors["flat"],
        theme.mix(colors["brand"], colors["cyan"], 0.5),
        theme.mix(colors["warn"], colors["up"], 0.5),
        theme.mix(colors["purple"], colors["brand"], 0.5),
    ]


DONUT_PALETTE = _palette()
MA_COLORS = (theme.COLORS["warn"], theme.COLORS["brand"], theme.COLORS["purple"])


# =========================================================================== 基类
class ChartBase(tk.Canvas):
    """所有图表的基类：统一内边距、坐标映射、空状态、防抖重绘与异常兜底。

    ``padding`` 顺序是 ``(左, 上, 右, 下)``；数据为空时画 ``empty_text``（默认「暂无数据」）；
    ``<Configure>`` 触发 40ms 防抖重绘，拖动窗口不会狂重绘。
    """

    empty_text = EMPTY_TEXT

    def __init__(self, master, height: int = 240, padding=DEFAULT_PADDING,
                 empty_text: Optional[str] = None, **kwargs):
        kwargs.setdefault("bg", theme.COLORS["panel"])
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("borderwidth", 0)
        kwargs.setdefault("height", int(height))
        tk.Canvas.__init__(self, master, **kwargs)
        self.padding = padding if padding is not None else DEFAULT_PADDING
        self._height = int(height)
        self._count = 0
        self._redraw_job = None
        self._last_error = None
        if empty_text:
            self.empty_text = str(empty_text)
        self.bind("<Configure>", self._on_configure)

    # ------------------------------------------------------------------ 公开 API
    def set_empty(self, text: str) -> None:
        """改空状态文字（例如「加载中…」）并立即重绘。"""
        self.empty_text = str(text)
        self.redraw()

    def set_data(self, *args, **kwargs):                     # pragma: no cover - 子类实现
        raise NotImplementedError("%s 必须实现 set_data()" % type(self).__name__)

    def has_data(self) -> bool:                              # pragma: no cover - 子类实现
        """是否画得出东西（False → 空状态）。"""
        return False

    def draw(self) -> None:                                  # pragma: no cover - 子类实现
        """把数据画到 canvas 上（子类实现，只在有数据时被调用）。"""

    def redraw(self) -> None:
        """清空重画；任何异常都兜在内部，绝不让 UI 崩。"""
        self._last_error = None
        try:
            self.delete("all")
        except tk.TclError:
            return
        try:
            if not self.has_data():
                self._draw_empty_state()
                return
            self.draw()
        except tk.TclError as exc:                            # 窗口销毁中…忽略
            self._last_error = exc
            if _DEBUG:
                traceback.print_exc()
        except Exception as exc:                              # noqa: BLE001 - 兜底：图表永不让界面崩
            self._last_error = exc
            if _DEBUG:
                traceback.print_exc()

    @property
    def last_error(self) -> Optional[BaseException]:
        """最近一次 ``redraw`` 里被吞掉的异常（调试用）。"""
        return self._last_error

    # ------------------------------------------------------------------ 几何
    def _padding4(self) -> Tuple[float, float, float, float]:
        pad = self.padding
        if isinstance(pad, (int, float)):
            value = float(pad)
            return (value, value, value, value)
        if isinstance(pad, dict):
            return (float(pad.get("left", DEFAULT_PADDING[0])), float(pad.get("top", DEFAULT_PADDING[1])),
                    float(pad.get("right", DEFAULT_PADDING[2])), float(pad.get("bottom", DEFAULT_PADDING[3])))
        try:
            left, top, right, bottom = pad
        except (TypeError, ValueError):
            left, top, right, bottom = DEFAULT_PADDING
        return (float(left), float(top), float(right), float(bottom))

    def _effective_size(self) -> Tuple[int, int]:
        """控件还没被映射（winfo_* == 1）时回落到请求尺寸，保证离屏/测试环境也能画出来。"""
        width = height = 0
        try:
            width, height = int(self.winfo_width()), int(self.winfo_height())
        except Exception:                                     # noqa: BLE001
            width = height = 0
        if width <= 1:
            width = self._fallback_size("width")
        if height <= 1:
            height = self._fallback_size("height")
        return (max(width, 240), max(height, max(self._height, 120)))

    def _fallback_size(self, option: str) -> int:
        try:
            if option == "width":
                return int(self.winfo_reqwidth())
            return int(self.winfo_reqheight())
        except Exception:                                     # noqa: BLE001
            return 0

    def plot_area(self) -> Tuple[float, float, float, float]:
        """绘图区 ``(x0, y0, x1, y1)``（去掉内边距，y 向下）。"""
        width, height = self._effective_size()
        left, top, right, bottom = self._padding4()
        x0 = min(left, max(width - right - 1.0, 0.0))
        x1 = max(width - right, x0 + 1.0)
        y0 = min(top, max(height - bottom - 1.0, 0.0))
        y1 = max(height - bottom, y0 + 1.0)
        return (float(x0), float(y0), float(x1), float(y1))

    def map_x(self, index: int) -> float:
        """第 ``index`` 个数据点的像素 x。"""
        x0, _y0, x1, _y1 = self.plot_area()
        return map_index(index, self._count, x0, x1)

    def map_y(self, value, lo: float, hi: float) -> Optional[float]:
        """数值 → 绘图区内的像素 y。"""
        x0, y0, _x1, y1 = self.plot_area()
        y = map_value(value, lo, hi, y0, y1)
        return None if y is None else clamp(y, min(y0, y1), max(y0, y1))

    # Tk 里 <Configure> 会连续触发，这里做 40ms 防抖
    def _on_configure(self, event=None) -> None:
        widget = getattr(event, "widget", self)
        if widget is not self:
            return
        self._schedule_redraw()

    def _schedule_redraw(self, delay: int = 40) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except Exception:                                 # noqa: BLE001 - 任务已执行/窗口已销毁
                pass
        try:
            self._redraw_job = self.after(int(delay), self._run_scheduled_redraw)
        except Exception:                                     # noqa: BLE001
            self._redraw_job = None

    def _run_scheduled_redraw(self) -> None:
        self._redraw_job = None
        self.redraw()

    def destroy(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except Exception:                                 # noqa: BLE001
                pass
            self._redraw_job = None
        tk.Canvas.destroy(self)

    # 静态工具（模块级纯函数，直接暴露给调用方与测试）
    nice_ticks = staticmethod(nice_ticks)
    fmt_axis = staticmethod(fmt_axis)
    compute_ticks = staticmethod(compute_ticks)

    # ------------------------------------------------------------------ 通用绘制
    def _empty_color(self) -> str:
        return theme.COLORS["text2"]

    def _draw_empty_state(self) -> None:
        """居中灰字空状态（带 ``EMPTY_TAG`` 标签，测试据此断言）。"""
        x0, y0, x1, y1 = self.plot_area()
        self.create_text((x0 + x1) / 2.0, (y0 + y1) / 2.0, text=str(self.empty_text or EMPTY_TEXT),
                         fill=self._empty_color(), font=theme.font(10), tags=(EMPTY_TAG,))

    def _axis_range(self, lo: float, hi: float, count: int = 5) -> Tuple[List[float], float, float]:
        """数据范围 → (刻度, 对齐后的 lo, 对齐后的 hi)。"""
        ticks, low, high = compute_ticks(lo, hi, count)
        if not ticks or low is None or high is None:
            low = float(lo)
            high = float(hi)
            if high <= low:
                high = low + 1.0
            return ([low, high], low, high)
        return (ticks, low, high)

    def _draw_frame(self, x0: float, y0: float, x1: float, y1: float, top: bool = False) -> None:
        """左侧 Y 轴线 + 底部基线（金融风格，不画右边框）。"""
        color = theme.COLORS["line"]
        self.create_line(x0, y0, x0, y1, fill=color, tags=(AXIS_TAG,))
        self.create_line(x0, y1, x1, y1, fill=color, tags=(AXIS_TAG,))
        if top:
            self.create_line(x0, y0, x1, y0, fill=color, tags=(AXIS_TAG,))

    def _draw_y_grid(self, ticks, lo: float, hi: float, kind: str = "number",
                     area: Optional[Tuple[float, float, float, float]] = None) -> None:
        """横向网格线 + 左侧刻度文字。"""
        x0, y0, x1, y1 = area or self.plot_area()
        grid = theme.mix(theme.COLORS["panel"], theme.COLORS["line"], 0.75)
        for tick in ticks:
            y = map_value(tick, lo, hi, y0, y1)
            if y is None:
                continue
            self.create_line(x0, y, x1, y, fill=grid, tags=(GRID_TAG,))
            self.create_text(x0 - 6, y, text=fmt_axis(tick, kind), anchor="e",
                             fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))

    def _draw_x_grid(self, ticks, lo: float, hi: float, kind: str = "number",
                     area: Optional[Tuple[float, float, float, float]] = None) -> None:
        """纵向网格线 + 底部刻度文字（横向条状图用）。"""
        x0, y0, x1, y1 = area or self.plot_area()
        grid = theme.mix(theme.COLORS["panel"], theme.COLORS["line"], 0.75)
        for tick in ticks:
            x = map_linear(tick, lo, hi, x0, x1)
            if x is None:
                continue
            self.create_line(x, y0, x, y1, fill=grid, tags=(GRID_TAG,))
            self.create_text(x, y1 + 5, text=fmt_axis(tick, kind), anchor="n",
                             fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))

    def _draw_x_labels(self, labels, xs, y: float, target: int = 6, max_width: float = 96.0) -> None:
        """X 轴文字：按 ``target`` 抽稀（日期/类别都可能很长，超宽自动省略）。"""
        if not labels or not xs:
            return
        for index in sample_indices(len(labels), target):
            if index >= len(xs):
                continue
            text = elide_text(labels[index], max_width, 8)
            if not text:
                continue
            self.create_text(xs[index], y + 5, text=text, anchor="n",
                             fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))

    def _draw_legend(self, entries, area: Optional[Tuple[float, float, float, float]] = None,
                     line_height: float = 15.0) -> None:
        """右上角图例：色块（虚线系列画虚线段）+ 名称。"""
        if not entries:
            return
        x0, y0, x1, _y1 = area or self.plot_area()
        sample, pad = 14.0, 7.0
        text_width = max(estimate_text_width(item.get("name") or "", 8) for item in entries)
        box_w = sample + 6.0 + text_width + pad * 2.0
        box_h = len(entries) * line_height + pad * 2.0 - 3.0
        left = max(x0 + 4.0, x1 - box_w - 6.0)
        top = y0 + 6.0
        self.create_rectangle(left, top, left + box_w, top + box_h,
                              fill=theme.mix(theme.COLORS["panel"], theme.COLORS["bg"], 0.45),
                              outline=theme.COLORS["line"], tags=(LEGEND_TAG,))
        for position, item in enumerate(entries):
            y = top + pad + position * line_height + line_height / 2.0 - 3.0
            color = item.get("color") or theme.COLORS["flat"]
            if item.get("dashed"):
                self.create_line(left + pad, y, left + pad + sample, y, fill=color,
                                 dash=(4, 3), tags=(LEGEND_TAG,))
            else:
                self.create_rectangle(left + pad, y - 3, left + pad + sample, y + 3,
                                      fill=color, outline="", tags=(LEGEND_TAG,))
            self.create_text(left + pad + sample + 6.0, y, text=str(item.get("name") or ""),
                             anchor="w", fill=theme.COLORS["text2"], font=theme.font(8),
                             tags=(LEGEND_TAG,))

    def _draw_series(self, run, color: str, width: int = 2, dashed: bool = False) -> None:
        """把一段 ``line_runs`` 结果画成折线（单点画圆点）。"""
        if not run:
            return
        if len(run) == 1:
            x, y = run[0][0], run[0][1]
            self.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, outline="", fill=color, tags=("series",))
            return
        coords: List[float] = []
        for x, y, _index, _value in run:
            coords.extend((x, y))
        if dashed:
            self.create_line(coords, fill=color, width=width, dash=(4, 3), tags=("series",))
        else:
            self.create_line(coords, fill=color, width=width, tags=("series",))


# =========================================================================== 折线图
class LineChart(ChartBase):
    """单序列折线图：折线 + 渐变填充（同色系深色近似）+ 可选 hover 提示。

    ``set_data(values, dates=None, color=None, fill=True, kind="number")``；
    hover（``<Motion>``）会在最近点画竖线 + 数值标签，移出画布即删除。
    """

    def __init__(self, master, height: int = 240, padding=DEFAULT_PADDING, empty_text=None,
                 hover: bool = True, **kwargs):
        ChartBase.__init__(self, master, height=height, padding=padding, empty_text=empty_text, **kwargs)
        self.kind = "number"
        self._values: List[Any] = []
        self._dates: List[str] = []
        self._color = theme.COLORS["brand"]
        self._fill = True
        self._hover_points: List[Tuple[float, float, float, int]] = []
        self._hover_enabled = bool(hover)
        self._max_points = 420
        if self._hover_enabled:
            self.bind("<Motion>", self._on_motion)
            self.bind("<Leave>", self._on_leave)

    def set_data(self, values, dates=None, color=None, fill: bool = True, kind: str = "number") -> "LineChart":
        try:
            self._values = list(values) if values is not None else []
        except TypeError:
            self._values = []
        self._dates = [str(item) for item in (dates or [])]
        self.kind = kind if kind in KINDS else "number"
        self._color = color or theme.COLORS["brand"]
        self._fill = bool(fill)
        self._count = len(self._values)
        self.redraw()
        return self

    def has_data(self) -> bool:
        return any(number is not None for number in clean_values(self._values))

    def plot_color(self) -> str:
        return self._color

    def draw(self) -> None:
        x0, y0, x1, y1 = self.plot_area()
        numbers = clean_values(self._values)
        count = len(numbers)
        lo, hi = value_range(numbers, pad=0.06)
        if lo is None:
            self._draw_empty_state()
            return
        if self.kind == "pct":
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        ticks, lo, hi = self._axis_range(lo, hi, 5)
        self._draw_y_grid(ticks, lo, hi, self.kind)
        if lo < 0 < hi:
            zero_y = map_value(0.0, lo, hi, y0, y1)
            self.create_line(x0, zero_y, x1, zero_y, dash=(3, 3), tags=(GRID_TAG,),
                             fill=theme.mix(theme.COLORS["line"], theme.COLORS["text2"], 0.35))
        self._draw_frame(x0, y0, x1, y1)
        xs = map_xs(count, x0, x1)
        runs = line_runs(numbers, lo, hi, xs, y0, y1, max_points=self._max_points)
        for run in runs:
            if self._fill:
                self._draw_gradient_fill(run, y1, self._color)
            self._draw_series(run, self._color)
        if 1 < count <= 30:
            for run in runs:
                for x, y, _index, _value in run:
                    self.create_oval(x - 2, y - 2, x + 2, y + 2, outline="", fill=self._color,
                                     tags=("marker",))
        self._hover_points = [(x, y, value, index) for run in runs for (x, y, index, value) in run]
        self._draw_x_labels(self._dates, xs, y1, target=5)

    def _draw_gradient_fill(self, run, baseline: float, color: str) -> None:
        """Tk 没有 alpha：先铺一层浅色面积，再沿曲线叠几条越来越浓的「带」模拟渐变。"""
        if len(run) < 2:
            return
        panel = theme.COLORS["panel"]
        area: List[float] = []
        for x, y, _index, _value in run:
            area.extend((x, y))
        area.extend((run[-1][0], baseline, run[0][0], baseline))
        self.create_polygon(area, fill=theme.mix(panel, color, 0.16), outline="", tags=("fill",))
        for thickness, alpha in ((14.0, 0.10), (8.0, 0.14), (3.0, 0.18)):
            coords: List[float] = []
            lower: List[Tuple[float, float]] = []
            clipped = False
            for x, y, _index, _value in run:
                edge = min(baseline, y + thickness)
                if edge > y + 0.01:
                    clipped = True
                lower.append((x, edge))
            if not clipped:
                continue
            for x, y, _index, _value in run:
                coords.extend((x, y))
            for x, y in reversed(lower):
                coords.extend((x, y))
            self.create_polygon(coords, fill=theme.mix(panel, color, 0.16 + alpha), outline="",
                                tags=("fill",))

    # ------------------------------------------------------------------ hover
    def _on_leave(self, _event=None) -> None:
        self._clear_hover()

    def _clear_hover(self) -> None:
        try:
            self.delete(HOVER_TAG)
        except tk.TclError:
            pass

    def _on_motion(self, event) -> None:
        if not self._hover_points:
            return
        x0, y0, x1, y1 = self.plot_area()
        if not (x0 - 6 <= event.x <= x1 + 6 and y0 - 6 <= event.y <= y1 + 6):
            self._clear_hover()
            return
        nearest = min(self._hover_points, key=lambda item: abs(item[0] - event.x))
        if abs(nearest[0] - event.x) > 60:
            self._clear_hover()
            return
        self._show_hover(nearest)

    def _show_hover(self, point: Tuple[float, float, float, int]) -> None:
        x, y, value, index = point
        x0, y0, x1, y1 = self.plot_area()
        try:
            self.delete(HOVER_TAG)
            self.create_line(x, y0, x, y1, dash=(3, 3), tags=(HOVER_TAG,),
                             fill=theme.mix(theme.COLORS["panel"], theme.COLORS["text2"], 0.6))
            self.create_oval(x - 3, y - 3, x + 3, y + 3, outline="", fill=self._color, tags=(HOVER_TAG,))
            label = fmt_axis(value, self.kind)
            if 0 <= index < len(self._dates) and self._dates[index]:
                label = "%s  %s" % (self._dates[index], label)
            text_x = min(max(x, x0 + 34.0), max(x1 - 34.0, x0 + 34.0))
            text_y = max(y - 12.0, y0 + 10.0)
            text_id = self.create_text(text_x, text_y, text=label, anchor="s", font=theme.font(8),
                                       fill=theme.COLORS["text"], tags=(HOVER_TAG,))
            box = self.bbox(text_id)
            if box:
                rect = self.create_rectangle(box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3,
                                             fill=theme.mix(theme.COLORS["panel"], theme.COLORS["bg"], 0.3),
                                             outline=theme.COLORS["line"], tags=(HOVER_TAG,))
                self.tag_lower(rect, text_id)
        except tk.TclError:                                   # 窗口正在销毁
            return


# =========================================================================== 多线图
class MultiLineChart(ChartBase):
    """多序列折线图（右上角图例、可选虚线）。

    ``set_data(series, dates=None, kind="number")``，
    ``series=[{"name": "净值", "values": [...], "color": "#4f8cff", "dashed": False}, ...]``。
    """

    def __init__(self, master, height: int = 240, padding=DEFAULT_PADDING, empty_text=None, **kwargs):
        ChartBase.__init__(self, master, height=height, padding=padding, empty_text=empty_text, **kwargs)
        self.kind = "number"
        self._series: List[Dict[str, Any]] = []
        self._dates: List[str] = []
        self._max_points = 420

    def set_data(self, series, dates=None, kind: str = "number") -> "MultiLineChart":
        self._series = normalize_series(series)
        self._dates = [str(item) for item in (dates or [])]
        self.kind = kind if kind in KINDS else "number"
        self._count = max([len(item["values"]) for item in self._series] or [0])
        self.redraw()
        return self

    def has_data(self) -> bool:
        return any(any(value is not None for value in clean_values(item["values"])) for item in self._series)

    def draw(self) -> None:
        x0, y0, x1, y1 = self.plot_area()
        lo, hi = series_range(self._series, pad=0.06)
        if lo is None:
            self._draw_empty_state()
            return
        if self.kind == "pct":
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        ticks, lo, hi = self._axis_range(lo, hi, 5)
        self._draw_y_grid(ticks, lo, hi, self.kind)
        if lo < 0 < hi:
            zero_y = map_value(0.0, lo, hi, y0, y1)
            self.create_line(x0, zero_y, x1, zero_y, dash=(3, 3), tags=(GRID_TAG,),
                             fill=theme.mix(theme.COLORS["line"], theme.COLORS["text2"], 0.35))
        self._draw_frame(x0, y0, x1, y1)
        xs = map_xs(self._count, x0, x1)
        for item in self._series:
            runs = line_runs(item["values"], lo, hi, xs, y0, y1, max_points=self._max_points)
            for run in runs:
                self._draw_series(run, item["color"], width=2, dashed=item["dashed"])
        self._draw_x_labels(self._dates, xs, y1, target=5)
        self._draw_legend([{"name": item["name"], "color": item["color"], "dashed": item["dashed"]}
                           for item in self._series])


# =========================================================================== 蜡烛图
class CandleChart(ChartBase):
    """K 线图：上部 70% 蜡烛（涨红跌绿）+ MA 折线，下部 30% 成交量柱。

    ``set_data(bars, volumes=True, ma_periods=(5, 20, 60))``，
    ``bars=[{"date","open","high","low","close","volume_wan"}, ...]``；
    ``set_window(start_ratio, end_ratio)`` 选择显示区间（0~1，默认 (0.55, 1.0) = 最近 45%），
    右下角有只读的窗口指示条。
    """

    def __init__(self, master, height: int = 260, padding=DEFAULT_PADDING, empty_text=None,
                 volumes: bool = True, ma_periods=(5, 20, 60), **kwargs):
        ChartBase.__init__(self, master, height=height, padding=padding, empty_text=empty_text, **kwargs)
        self._bars: List[Dict[str, Any]] = []
        self._volumes = bool(volumes)
        self._ma_periods = tuple(ma_periods or ())
        self._window = window_span(None, None)
        self._max_bars = 420
        self._candle_gap = 0.22

    def set_data(self, bars, volumes: bool = True, ma_periods=(5, 20, 60)) -> "CandleChart":
        try:
            self._bars = [bar for bar in list(bars or []) if isinstance(bar, dict)]
        except TypeError:
            self._bars = []
        self._volumes = bool(volumes)
        periods: List[int] = []
        for item in ma_periods or ():
            try:
                period = int(item)
            except (TypeError, ValueError):
                continue
            if period > 0 and period not in periods:
                periods.append(period)
        self._ma_periods = tuple(periods[:3])
        self.redraw()
        return self

    def set_window(self, start_ratio, end_ratio) -> "CandleChart":
        """设置显示窗口（0~1 比例）；越界自动夹取、反向自动交换、过窄自动补到 1%。"""
        self._window = window_span(start_ratio, end_ratio)
        self.redraw()
        return self

    def has_data(self) -> bool:
        return any(to_float(bar.get("close")) is not None for bar in self._bars)

    # 价格区（上 70%）——基类的坐标映射默认按这里算
    def plot_area(self) -> Tuple[float, float, float, float]:
        x0, y0, x1, y1 = ChartBase.plot_area(self)
        split = y0 + (y1 - y0) * 0.70
        return (x0, y0, x1, max(split, y0 + 1.0))

    def _volume_area(self) -> Tuple[float, float, float, float]:
        x0, y0, x1, y1 = ChartBase.plot_area(self)
        price_bottom = y0 + (y1 - y0) * 0.70
        return (x0, price_bottom + 2.0, x1, y1)

    def map_x(self, index: int) -> float:
        x0, _y0, x1, _y1 = self.plot_area()
        centers = map_xs(self._count, x0, x1, mode="band")
        if not centers:
            return (x0 + x1) / 2.0
        return centers[int(clamp(index, 0, len(centers) - 1))]

    def _visible_slice(self) -> Tuple[int, int]:
        """当前窗口对应的 ``[start, end)`` 下标（超长区间只保留最近 ``_max_bars`` 根）。"""
        count = len(self._bars)
        if count <= 0:
            return (0, 0)
        start = int(math.floor(self._window[0] * count))
        end = int(math.ceil(self._window[1] * count))
        start = max(0, min(start, count - 1))
        end = min(count, max(end, start + 1))
        if end - start > self._max_bars:                      # 性能兜底：保留最近的一段
            start = end - self._max_bars
        return (start, end)

    def _visible_bars(self) -> List[Dict[str, Any]]:
        start, end = self._visible_slice()
        return self._bars[start:end]

    def draw(self) -> None:
        window_start, window_end = self._visible_slice()
        bars = self._bars[window_start:window_end]
        if not bars:
            self._draw_empty_state()
            return
        x0, y0, x1, y1 = self.plot_area()
        vx0, vy0, vx1, vy1 = self._volume_area()
        count = len(bars)
        closes = [bar.get("close") for bar in bars]
        lows = [bar.get("low") if to_float(bar.get("low")) is not None else bar.get("close") for bar in bars]
        highs = [bar.get("high") if to_float(bar.get("high")) is not None else bar.get("close") for bar in bars]
        lo, hi = series_range([lows, highs, closes], pad=0.03)
        if lo is None:
            self._draw_empty_state()
            return
        ticks, lo, hi = self._axis_range(lo, hi, 5)
        self._draw_y_grid(ticks, lo, hi, "number", (x0, y0, x1, y1))
        self._draw_frame(x0, y0, x1, y1)
        self._draw_frame(x0, y0, x1, y1)

        candles = candle_geometry(bars, lo, hi, x0, y0, x1, y1, min_body=1.0,
                                  gap_ratio=self._candle_gap)
        for item in candles:
            if not item["valid"]:
                continue
            color = theme.COLORS["up"] if item["rising"] else theme.COLORS["down"]
            self.create_line(item["center"], item["y_high"], item["center"], item["y_low"],
                             fill=color, tags=("candle",))
            left, right = item["left"], item["right"]
            if right - left < 1.0:
                left, right = item["center"] - 0.5, item["center"] + 0.5
            self.create_rectangle(left, item["body_top"], right, item["body_bottom"],
                                  fill=color, outline=color, tags=("candle",))

        # MA 折线（颜色：warn / brand / purple）——在完整序列上算均线，再按窗口切片，
        # 这样 MA60 在「只看最近 45%」时也能正常显示。
        centers = map_xs(count, x0, x1, mode="band")
        legend_entries: List[Dict[str, Any]] = []
        full_closes = [bar.get("close") for bar in self._bars]
        for position, item in enumerate(moving_averages(full_closes, self._ma_periods)):
            color = MA_COLORS[position % len(MA_COLORS)]
            name = "MA%d" % item["period"]
            legend_entries.append({"name": name, "color": color, "dashed": False})
            for run in line_runs(item["values"][window_start:window_end], lo, hi, centers, y0, y1):
                self._draw_series(run, color, width=2)

        if self._volumes:
            self._draw_volume_bars(bars, vx0, vy0, vx1, vy1)
            self.create_line(vx0, vy0 - 1.0, vx1, vy0 - 1.0,
                             fill=theme.mix(theme.COLORS["panel"], theme.COLORS["line"], 0.8),
                             tags=(AXIS_TAG,))
        self._draw_x_labels([item.get("date") for item in bars], centers, vy1, target=6, max_width=88.0)

        # 右下角：显示窗口指示（只读，不拖拽）
        box = window_indicator_rect(self._window[0], self._window[1], vx0, vx1, vy1 - 8.0)
        self.create_rectangle(*box["box"], fill=theme.mix(theme.COLORS["panel"], theme.COLORS["bg"], 0.45),
                              outline=theme.COLORS["line"], tags=(AXIS_TAG,))
        self.create_rectangle(*box["block"], fill=theme.COLORS["brand"], outline="", tags=(AXIS_TAG,))
        self.create_text(box["box"][0] - 6.0, (box["box"][1] + box["box"][3]) / 2.0,
                         text="区间 %d%%~%d%%" % (round(self._window[0] * 100), round(self._window[1] * 100)),
                         anchor="e", fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))
        if legend_entries:
            self._draw_legend(legend_entries, (x0, y0, x1, y1))

    def _draw_volume_bars(self, bars, x0, y0, x1, y1) -> None:
        volumes = [bar.get("volume_wan") for bar in bars]
        valid = [number for number in clean_values(volumes) if number is not None and number > 0]
        vmax = max(valid) if valid else 0.0
        bars_geo = volume_geometry(bars, 0.0, vmax if vmax > 0 else 1.0, x0, y0, x1, y1,
                                   gap_ratio=self._candle_gap)
        for item in bars_geo:
            if not item["valid"] or item["top"] >= item["bottom"]:
                continue
            color = theme.COLORS["up"] if item["rising"] else theme.COLORS["down"]
            left, right = item["left"], item["right"]
            if right - left < 1.0:
                middle = (left + right) / 2.0
                left, right = middle - 0.5, middle + 0.5
            self.create_rectangle(left, item["top"], right, item["bottom"],
                                  fill=theme.mix(theme.COLORS["panel"], color, 0.75),
                                  outline=color, tags=("volume",))
        if vmax > 0:
            self.create_text(x0 - 6, y0 + 4, text=fmt_axis(vmax, "wan"), anchor="ne",
                             fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))


# =========================================================================== 柱状/条形图
class BarChart(ChartBase):
    """柱状图（默认竖）与条形图（``horizontal=True``，用于板块涨幅榜）。

    ``set_data(labels, values, horizontal=False, colors=None, kind="pct", show_values=True,
    positive_color=None, negative_color=None)``：正值用 ``up``、负值用 ``down``（可覆盖），
    柱体最小 1px，``show_values`` 在柱端标 ``fmt_pct``/``fmt_num``。
    """

    def __init__(self, master, height: int = 240, padding=None, empty_text=None, **kwargs):
        ChartBase.__init__(self, master, height=height,
                          padding=padding if padding is not None else DEFAULT_PADDING,
                          empty_text=empty_text, **kwargs)
        self._custom_padding = padding is not None
        self._labels: List[str] = []
        self._values: List[Any] = []
        self._horizontal = False
        self._colors: List[Any] = []
        self._show_values = True
        self._positive = None
        self._negative = None
        self.kind = "pct"

    def set_data(self, labels, values, horizontal: bool = False, colors=None, kind: str = "pct",
                 show_values: bool = True, positive_color=None, negative_color=None) -> "BarChart":
        self._labels = ["" if item is None else str(item) for item in (labels or [])]
        try:
            self._values = list(values) if values is not None else []
        except TypeError:
            self._values = []
        self._horizontal = bool(horizontal)
        self._colors = list(colors or [])
        self.kind = kind if kind in KINDS else "number"
        self._show_values = bool(show_values)
        self._positive = positive_color
        self._negative = negative_color
        if not self._custom_padding:
            self.padding = HORIZONTAL_PADDING if self._horizontal else DEFAULT_PADDING
        self._count = len(self._values)
        self.redraw()
        return self

    def has_data(self) -> bool:
        return any(number is not None for number in clean_values(self._values))

    def color_for_value(self, value, index: int = 0) -> str:
        """单根柱子的颜色（``colors`` 覆盖 > 正负色 > flat）。"""
        if 0 <= index < len(self._colors) and self._colors[index]:
            return self._colors[index]
        number = to_float(value)
        if number is None or number == 0:
            return theme.COLORS["flat"]
        if number > 0:
            return self._positive or theme.COLORS["up"]
        return self._negative or theme.COLORS["down"]

    def draw(self) -> None:
        x0, y0, x1, y1 = self.plot_area()
        numbers = clean_values(self._values)
        if self._horizontal:
            self._draw_horizontal(x0, y0, x1, y1, numbers)
        else:
            self._draw_vertical(x0, y0, x1, y1, numbers)

    def _range_with_zero(self, numbers) -> Tuple[float, float]:
        finite = [number for number in numbers if number is not None]
        lo = min([0.0] + finite) if finite else -1.0
        hi = max([0.0] + finite) if finite else 1.0
        if hi - lo <= 0:
            lo, hi = -1.0, 1.0
        return (lo, hi)

    def _draw_vertical(self, x0, y0, x1, y1, numbers) -> None:
        lo, hi = self._range_with_zero(numbers)
        ticks, lo, hi = self._axis_range(lo, hi, 5)
        self._draw_y_grid(ticks, lo, hi, self.kind)
        self._draw_frame(x0, y0, x1, y1)
        items = bar_geometry(self._labels, self._values, x0, y0, x1, y1, horizontal=False,
                             lo=lo, hi=hi, gap_ratio=0.35, colors=self._colors)
        for item in items:
            if not item["valid"]:
                continue
            color = self.color_for_value(item["value"], item["index"])
            left, right = item["left"], item["right"]
            if right - left < 1.0:
                middle = (left + right) / 2.0
                left, right = middle - 0.5, middle + 0.5
            self.create_rectangle(left, item["top"], right, item["bottom"], fill=color,
                                  outline=color, tags=("bar",))
            if self._show_values:
                positive = item["value"] >= 0
                text_y = item["top"] - 4.0 if positive else item["bottom"] + 4.0
                self.create_text((left + right) / 2.0, clamp(text_y, 8.0, y1 + 40.0),
                                 text=self._value_label(item["value"]),
                                 anchor="s" if positive else "n",
                                 fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))
        labels = self._labels
        xs = map_xs(len(self._values), x0, x1)
        if labels:
            self._draw_x_labels(labels, xs, y1, target=10, max_width=64.0)

    def _draw_horizontal(self, x0, y0, x1, y1, numbers) -> None:
        lo, hi = self._range_with_zero(numbers)
        ticks, lo, hi = self._axis_range(lo, hi, 5)
        self._draw_x_grid(ticks, lo, hi, self.kind)
        self._draw_frame(x0, y0, x1, y1)
        items = bar_geometry(self._labels, self._values, x0, y0, x1, y1, horizontal=True,
                             lo=lo, hi=hi, gap_ratio=0.35, colors=self._colors)
        for item in items:
            if not item["valid"]:
                continue
            color = self.color_for_value(item["value"], item["index"])
            top, bottom = item["top"], item["bottom"]
            if bottom - top < 1.0:
                middle = (top + bottom) / 2.0
                top, bottom = middle - 0.5, middle + 0.5
            self.create_rectangle(item["left"], top, item["right"], bottom, fill=color,
                                  outline=color, tags=("bar",))
            self.create_text(x0 - 8.0, (top + bottom) / 2.0,
                             text=elide_text(item["label"], max(x0 - 14.0, 24.0), 9),
                             anchor="e", fill=theme.COLORS["text"], font=theme.font(9),
                             tags=(AXIS_TAG,))
            if self._show_values:
                positive = item["value"] >= 0
                text_x = item["right"] + 5.0 if positive else item["left"] - 5.0
                self.create_text(clamp(text_x, x0 + 10.0, x1 + 30.0), (top + bottom) / 2.0,
                                 text=self._value_label(item["value"]),
                                 anchor="w" if positive else "e",
                                 fill=theme.COLORS["text2"], font=theme.font(8), tags=(AXIS_TAG,))

    def _value_label(self, value) -> str:
        if self.kind == "pct":
            return theme.fmt_pct(value, 2)
        if self.kind == "money":
            return theme.fmt_money(value, 0)
        if self.kind == "wan":
            return fmt_axis(value, "wan")
        return theme.fmt_num(value, 2)


# =========================================================================== 环形图
class DonutChart(ChartBase):
    """环形图：中间挖空 + 右侧多列图例（名称 + 占比）。

    ``set_data(items, colors=None)``，``items=[{"name": "沪深300", "value": 42.1}, ...]``；
    ``value <= 0`` 的项不占角度（占比 0%），但仍列在图例里。
    """

    def __init__(self, master, height: int = 240, padding=None, empty_text=None, **kwargs):
        ChartBase.__init__(self, master, height=height,
                          padding=padding if padding is not None else DONUT_PADDING,
                          empty_text=empty_text, **kwargs)
        self._items: List[Dict[str, Any]] = []
        self._colors: List[Any] = []

    def set_data(self, items, colors=None) -> "DonutChart":
        self._items = [item if isinstance(item, dict) else {"name": str(item), "value": None}
                       for item in (items or [])]
        self._colors = list(colors or [])
        self._count = len(self._items)
        self.redraw()
        return self

    def has_data(self) -> bool:
        for item in self._items:
            value = to_float(item.get("value"))
            if value is not None and value > 0:
                return True
        return False

    def _layout(self) -> Tuple[Tuple[float, float, float, float], Tuple[float, float, float, float]]:
        """返回 (环形区, 图例区)；画布太窄时改成上下排布。"""
        x0, y0, x1, y1 = self.plot_area()
        width = x1 - x0
        legend_w = min(max(width * 0.42, 110.0), 260.0)
        donut = (x0, y0, x1 - legend_w - 12.0, y1)
        legend = (x1 - legend_w, y0, x1, y1)
        if donut[2] - donut[0] < 70.0:
            split = y0 + (y1 - y0) * 0.60
            donut = (x0, y0, x1, max(split - 6.0, y0 + 1.0))
            legend = (x0, split, x1, y1)
        return (donut, legend)

    def draw(self) -> None:
        slices = donut_slices(self._items, start_angle=90.0, clockwise=True,
                              colors=self._colors or DONUT_PALETTE)
        if not any(item["span"] > 0 for item in slices):
            self._draw_empty_state()
            return
        donut_area, legend_area = self._layout()
        cx = (donut_area[0] + donut_area[2]) / 2.0
        cy = (donut_area[1] + donut_area[3]) / 2.0
        radius = max(12.0, min(donut_area[2] - donut_area[0], donut_area[3] - donut_area[1]) / 2.0 - 6.0)
        ring = max(8.0, min(radius * 0.42, 30.0))
        path = max(4.0, radius - ring / 2.0)
        box = (cx - path, cy - path, cx + path, cy + path)
        for item in slices:
            if item["span"] <= 0:
                continue
            self.create_arc(box, start=item["start"], extent=item["extent"], style="arc",
                            width=ring, outline=item["color"], tags=("slice",))
        self.create_text(cx, cy, text="共 %d 项" % len(slices), fill=theme.COLORS["text2"],
                         font=theme.font(9), tags=("center",))
        self._draw_legend_rows(slices, legend_area)

    def _draw_legend_rows(self, slices, legend_area) -> None:
        line_height = 17.0
        height = legend_area[3] - legend_area[1]
        rows = max(1, int(height // line_height))
        columns = max(1, int(math.ceil(len(slices) / float(rows))))
        column_w = (legend_area[2] - legend_area[0]) / float(columns)
        for position, item in enumerate(slices):
            column = position // rows
            row = position % rows
            if column >= columns:
                break
            left = legend_area[0] + column * column_w
            y = legend_area[1] + row * line_height + line_height / 2.0
            self.create_rectangle(left, y - 4, left + 9, y + 4, fill=item["color"], outline="",
                                  tags=(LEGEND_TAG,))
            percent = "%d%%" % int(round(item["pct"]))
            percent_w = estimate_text_width(percent, 9) + 6.0
            name_w = max(column_w - 26.0 - percent_w, 20.0)
            self.create_text(left + 14.0, y, text=elide_text(item["name"], name_w, 9), anchor="w",
                             fill=theme.COLORS["text"], font=theme.font(9), tags=(LEGEND_TAG,))
            self.create_text(left + column_w - 4.0, y, text=percent, anchor="e",
                             fill=theme.COLORS["text2"], font=theme.font(9, mono=True),
                             tags=(LEGEND_TAG,))
