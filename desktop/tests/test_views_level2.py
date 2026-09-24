# -*- coding: utf-8 -*-
"""``Level2View``（盘口 / L2）页面级冒烟测试。

设计要点（与 ``test_views_market_portfolio.py`` 同一套夹具）：

* 用真实的 ``Tk()`` 根窗口（withdraw，不弹窗）+ 真实的 ``TaskRunner``，
  验证「后台线程 → 主线程回调 → 控件更新」这条真实链路；
* ``services.level2`` 用替身（契约 JSON、固定样例、不联网），含 broken / empty 两种情形，
  并覆盖 4 个工具区块（大单追踪 / 资金流分时 / 封板状态 / 扫描与排行）的渲染、空状态、
  失败内联、``failures`` 提示、排行进行中不阻塞、自动刷新不触发工具；
* 控件库（``widgets.cards/charts/table``）未交付时页面降级为内联错误，此时跳过依赖具体控件的断言。

运行（GUI 必须用系统自带的 3.9 + tkinter）::

    cd desktop && PYTHONPATH=..:. /usr/bin/python3 -m unittest discover -s tests
    /usr/bin/python3 desktop/tests/test_views_level2.py
"""

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP_DIR = os.path.dirname(HERE)
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

import tkinter as tk                                          # noqa: E402
from tkinter import ttk                                       # noqa: E402

from quantstudio_desktop import theme                         # noqa: E402
from quantstudio_desktop.services import TaskRunner           # noqa: E402
from quantstudio_desktop.views import level2 as level2_module  # noqa: E402
from quantstudio_desktop.views.level2 import Level2View       # noqa: E402

WIDGETS_OK = not level2_module.WIDGETS_ERROR
CODE = "600519.SH"


# --------------------------------------------------------------------------- 契约 JSON 样例


def make_levels(count=5, base=1680.0):
    """五档：买 1..买 N（向下依次 0.01）、卖 1..卖 N（向上依次 0.01）。"""
    bids = [{"price": round(base - index * 0.01, 2), "volume": 100 + index * 10,
             "amount": round((base - index * 0.01) * (100 + index * 10) * 100, 2)}
            for index in range(count)]
    asks = [{"price": round(base + 0.01 + index * 0.01, 2), "volume": 120 + index * 10,
             "amount": round((base + 0.01 + index * 0.01) * (120 + index * 10) * 100, 2)}
            for index in range(count)]
    return bids, asks


def make_orderbook(offline=False, empty=False, levels=5):
    bids, asks = make_levels(levels)
    if empty:
        bids, asks = [], []
    return {
        "code": CODE, "name": "贵州茅台", "price": 1680.5, "prev_close": 1660.0,
        "ts": "2025-01-03 14:55:03", "source": "sample", "levels": levels,
        "bids": bids, "asks": asks, "outer_volume": 12000, "inner_volume": 9800,
        "summary": {"bid_volume": sum(item["volume"] for item in bids),
                    "ask_volume": sum(item["volume"] for item in asks),
                    "bid_amount": sum(item["amount"] for item in bids),
                    "ask_amount": sum(item["amount"] for item in asks),
                    "imbalance_pct": 12.34, "ratio": 1.28, "spread": 0.01, "mid": 1680.5},
        "capabilities": {"orderbook": True, "orderbook_levels": 5, "ticks": True,
                         "orders": False, "queue": False, "import": False},
        "meta": {"source": "sample", "stale": False, "offline": offline,
                 "as_of": "2025-01-03 14:55:03", "notes": ["演示数据"] if offline else []},
    }


def make_ticks(count=70, offline=False, empty=False):
    items = []
    for index in range(count):
        side = ("buy", "sell", "neutral")[index % 3]
        items.append({"time": "14:%02d:%02d" % (index // 60, index % 60),
                      "price": round(1680.0 + index * 0.01, 2), "volume": 10 + index,
                      "amount": round((10 + index) * 100 * (1680.0 + index * 0.01), 2),
                      "side": side, "change": 0.01 if index % 2 else -0.01})
    if empty:
        items = []
    return {
        "code": CODE, "name": "贵州茅台", "count": len(items), "shown": len(items),
        "items": items,
        "stats": {"count": len(items), "buy_volume": 600, "sell_volume": 500,
                  "neutral_volume": 120, "buy_amount": 1000.0, "sell_amount": 900.0,
                  "buy_volume_pct": 49.2, "sell_volume_pct": 41.0, "net_amount": 100.0},
        "pages": 1, "note": "约覆盖最近 4000 笔",
        "meta": {"source": "sample", "stale": False, "offline": offline, "as_of": ""},
    }


def make_flow(offline=False, empty=False):
    empty_row = {"label": "", "buy": 0.0, "sell": 0.0, "net": 0.0, "count": 0, "buy_pct": 0.0}
    buckets = {
        "super_big": {"label": "超大单", "buy": 320000.0, "sell": 180000.0, "net": 140000.0,
                      "count": 6, "buy_pct": 64.0},
        "big": {"label": "大单", "buy": 210000.0, "sell": 250000.0, "net": -40000.0,
                "count": 14, "buy_pct": 45.65},
        "mid": {"label": "中单", "buy": 120000.0, "sell": 90000.0, "net": 30000.0,
                "count": 32, "buy_pct": 57.14},
        "small": {"label": "小单", "buy": 60000.0, "sell": 70000.0, "net": -10000.0,
                  "count": 88, "buy_pct": 46.15},
    }
    if empty:                               # 没有逐笔样本：金额与占比全 0（页面应画内联空状态）
        buckets = {key: dict(empty_row) for key in ("super_big", "big", "mid", "small")}
    return {
        "code": CODE, "name": "贵州茅台", "ts": "2025-01-03 14:55:03", "source": "sample",
        "amount_total": 0.0 if empty else 1400000.0,
        "buy_amount": 0.0 if empty else 710000.0,
        "sell_amount": 0.0 if empty else 690000.0,
        "net_amount": 0.0 if empty else 20000.0,
        "main_net": 0.0 if empty else 100000.0,
        "main_net_pct": 0.0 if empty else 7.14, "net_pct": 0.0 if empty else 1.43,
        "tick_count": 0 if empty else 140, "buckets": buckets,
        "meta": {"source": "sample", "stale": False, "offline": offline, "as_of": ""},
    }

# ---- 4 个 L2 工具区块的契约 JSON（字段与 backend/services/level2_service.py 一致）


TOOL_META = {"source": "sample", "stale": False, "offline": False, "as_of": ""}


def make_big_orders(threshold=1000000.0, count=12, empty=False):
    """大单追踪：时间倒序；偶数下标为卖、奇数下标为买（倒序后第一行是买 → 便于断言买红）。"""
    items = []
    for index in range(count):
        amount = round(threshold * (1.0 + index * 0.1), 2)
        items.append({
            "time": "14:%02d:00" % (index + 30),
            "price": round(1680.0 + index * 0.5, 2),
            "volume": 100 + index * 10,
            "amount": amount,
            "side": "buy" if index % 2 else "sell",
            "bucket": "super_big" if amount >= 1000000.0 else "big",
            "bucket_label": "超大单" if amount >= 1000000.0 else "大单",
        })
    items.reverse()                             # 契约：时间倒序（最新在前）
    if empty:
        items = []
    buy_amount = round(sum(item["amount"] for item in items if item["side"] == "buy"), 2)
    sell_amount = round(sum(item["amount"] for item in items if item["side"] == "sell"), 2)
    gross = buy_amount + sell_amount
    return {
        "code": CODE, "name": "贵州茅台", "threshold": threshold,
        "count": len(items), "shown": len(items), "items": items,
        "summary": {
            "count": len(items),
            "buy_count": sum(1 for item in items if item["side"] == "buy"),
            "sell_count": sum(1 for item in items if item["side"] == "sell"),
            "buy_amount": buy_amount, "sell_amount": sell_amount,
            "net_amount": round(buy_amount - sell_amount, 2),
            "buy_amount_pct": round(buy_amount / gross * 100.0, 2) if gross else 0.0,
            "amount_share_pct": 34.56 if gross else 0.0,
            "biggest": items[0] if items else None,
        },
        "tick_sample": 4000,
        "note": "样本 = 拉取到的逐笔（约覆盖最近 4000 笔）；方向为第三方盘口标记。",
        "meta": dict(TOOL_META),
    }


def make_flow_series(minutes=6, empty=False):
    """资金流分时：每分钟净额 (i-2)×3 万，累计净额随之上下（-6/-9/-9/-6/0/+9 万元）。"""
    series = []
    cumulative = 0.0
    for index in range(minutes):
        net = (index - 2) * 30000.0
        cumulative += net
        series.append({
            "time": "09:%02d" % (31 + index),
            "buy": 100000.0 + index * 1000.0, "sell": 100000.0 - index * 1000.0,
            "net": net, "cum_net": round(cumulative, 2),
            "amount": 500000.0, "count": 20 + index,
        })
    if empty:
        series = []
    flow = make_flow(empty=empty)
    return {
        "code": CODE, "name": "贵州茅台", "minutes": len(series),
        "tick_sample": 0 if empty else 140, "series": series,
        "buckets": flow["buckets"],
        "main_net": flow["main_net"], "main_net_pct": flow["main_net_pct"],
        "amount_total": flow["amount_total"],
        "note": "每分钟净额 = 该分钟主动买 − 主动卖（第三方方向标记）；累计净额按时间递增。",
        "meta": dict(TOOL_META),
    }


def make_seal(state="limit_up", empty=False):
    """封板状态：默认涨停（封单 12,345 手 / 2.254 亿 / 封成比 12.5%）。"""
    if empty:
        seal = {"state": "unknown", "label": "数据不足", "limit_pct": 0.1, "limit_pct_text": "10%",
                "limit_up_price": 0.0, "limit_down_price": 0.0, "distance_pct": 0.0,
                "seal_volume": 0, "seal_amount": 0.0, "seal_ratio": 0.0, "amount_total": 0.0}
        price, prev_close = 0.0, 0.0
    elif state == "limit_down":
        seal = {"state": "limit_down", "label": "跌停", "limit_pct": 0.1, "limit_pct_text": "10%",
                "limit_up_price": 1826.0, "limit_down_price": 1494.0, "distance_pct": 0.0,
                "seal_volume": 8000, "seal_amount": 119520000.0, "seal_ratio": 6.64,
                "amount_total": 1800000000.0}
        price, prev_close = 1494.0, 1660.0
    else:
        seal = {"state": "limit_up", "label": "涨停", "limit_pct": 0.1, "limit_pct_text": "10%",
                "limit_up_price": 1826.0, "limit_down_price": 1494.0, "distance_pct": 0.0,
                "seal_volume": 12345, "seal_amount": 225400000.0, "seal_ratio": 12.5,
                "amount_total": 1800000000.0}
        price, prev_close = 1826.0, 1660.0
    return {
        "code": CODE, "name": "贵州茅台", "price": price, "prev_close": prev_close,
        "seal": seal,
        "seal_text": "%s（%s）" % (seal["label"], seal["limit_pct_text"]),
        "note": "只按当前快照判断此刻是否封板；开板次数需要盘中多次采样，不在单次调用里给出。",
        "meta": dict(TOOL_META),
    }


def make_scan(empty=False):
    """扫描：契约顺序（600519 委比 12.34 在前），页面应按委比降序重排成 000001 在前。"""
    items = []
    if not empty:
        items = [
            {"code": "600519.SH", "name": "贵州茅台", "price": 1680.5, "change_pct": 1.23,
             "imbalance_pct": 12.34, "ratio": 1.28, "spread": 0.01, "bid_volume": 600,
             "ask_volume": 700, "volume_ratio": 1.5, "seal_state": "limit_up",
             "seal_label": "涨停", "seal_amount": 225400000.0, "distance_pct": 2.1},
            {"code": "000001.SZ", "name": "平安银行", "price": 11.2, "change_pct": -0.8,
             "imbalance_pct": 30.5, "ratio": 1.9, "spread": 0.01, "bid_volume": 4000,
             "ask_volume": 5000, "volume_ratio": None, "seal_state": "normal",
             "seal_label": "未封板", "seal_amount": 0.0, "distance_pct": 9.9},
        ]
    return {
        "count": len(items), "requested": len(items), "items": items, "failures": [],
        "note": "单次快照的静态特征排序；突变检测（挂单骤增/大单撤单）需要两次以上采样，本工具不承诺。",
        "meta": dict(TOOL_META),
    }


def make_flow_rank(empty=False):
    """排行：契约顺序把亏的放前面，页面应按主力净额降序重排成 600519 在前。"""
    items = []
    if not empty:
        items = [
            {"code": "000001.SZ", "name": "平安银行", "main_net": -5000000.0,
             "main_net_pct": -12.5, "net_amount": -1000000.0, "amount_total": 40000000.0,
             "tick_count": 900},
            {"code": "600519.SH", "name": "贵州茅台", "main_net": 1000000.0,
             "main_net_pct": 7.14, "net_amount": 200000.0, "amount_total": 14000000.0,
             "tick_count": 140},
        ]
    return {
        "count": len(items), "requested": len(items), "items": items, "failures": [],
        "note": "主力净额 = 超大单 + 大单净额（按单笔成交额分档自算，样本约最近 4000 笔）。",
        "meta": dict(TOOL_META),
    }


META_OFFLINE = {"source": "sample", "stale": False, "offline": True,
                "as_of": "", "notes": ["离线演示"]}


# --------------------------------------------------------------------------- 替身


class FakeLevel2(object):
    """``services.level2`` 替身：返回契约 JSON，记录调用参数（不联网）。"""

    def __init__(self, offline=False, empty=False, broken=False):
        self.offline = offline
        self.empty = empty
        self.broken = broken
        self.calls = []
        self.scan_failure_rows = []             # 测试注入的 failures（逐标的失败）
        self.rank_failure_rows = []

    # ---- 内部
    def _touch(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.broken:
            raise RuntimeError("%s 服务不可用（替身故障）" % name)

    def _meta(self, payload):
        if self.offline:
            payload["meta"] = dict(META_OFFLINE)
        return payload

    # ---- 契约方法
    def orderbook(self, code):
        self._touch("orderbook", {"code": code})
        payload = make_orderbook(offline=self.offline, empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    def ticks(self, code, limit=600):
        self._touch("ticks", {"code": code, "limit": limit})
        payload = make_ticks(offline=self.offline, empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    def capital_flow(self, code, limit=2000):
        self._touch("capital_flow", {"code": code, "limit": limit})
        payload = make_flow(offline=self.offline, empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    # ---- 契约方法：4 个 L2 工具
    def big_orders(self, code, threshold=1000000.0, limit=50, sides=None):
        self._touch("big_orders", {"code": code, "threshold": threshold, "limit": limit,
                                   "sides": sides})
        payload = make_big_orders(threshold=threshold, empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    def flow_series(self, code, limit=2000):
        self._touch("flow_series", {"code": code, "limit": limit})
        payload = make_flow_series(empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    def seal_status(self, code):
        self._touch("seal_status", {"code": code})
        payload = make_seal(empty=self.empty)
        payload["code"] = code
        return self._meta(payload)

    def scan(self, codes, limit=10):
        self._touch("scan", {"codes": list(codes), "limit": limit})
        payload = make_scan(empty=self.empty)
        payload["requested"] = len(list(codes))
        payload["failures"] = [dict(row) for row in self.scan_failure_rows]
        if payload["failures"]:
            payload["count"] = max(0, payload["count"] - len(payload["failures"]))
        return self._meta(payload)

    def flow_rank(self, codes, limit=1000, top=10):
        self._touch("flow_rank", {"codes": list(codes), "limit": limit, "top": top})
        payload = make_flow_rank(empty=self.empty)
        payload["requested"] = len(list(codes))
        payload["failures"] = [dict(row) for row in self.rank_failure_rows]
        if payload["failures"]:
            payload["count"] = max(0, payload["count"] - len(payload["failures"]))
        return self._meta(payload)

    # ---- 断言辅助
    def names(self):
        return [name for name, _kwargs in self.calls]

    def kwargs_of(self, name):
        """最近一次 ``name`` 调用的参数（没有则 None）。"""
        for call_name, kwargs in reversed(self.calls):
            if call_name == name:
                return kwargs
        return None


class FakeServices(object):
    """``GuiServices`` 替身：level2 命名空间 + 自选池（页面用到的全部能力）。"""

    def __init__(self, offline=False, empty=False, broken=False, with_level2=True,
                 watchlist=None, watch_error=None, with_watchlist=True):
        self.level2 = FakeLevel2(offline=offline, empty=empty, broken=broken) if with_level2 else None
        self._watchlist = list(watchlist or [])
        self._watch_error = watch_error
        self._with_watchlist = with_watchlist
        self.watch_calls = 0
        if not with_watchlist:
            self.watchlist = None               # 模拟「服务层没有自选池」：页面要给可读备注

    def watchlist(self):
        self.watch_calls += 1
        if self._watch_error:
            raise RuntimeError(self._watch_error)
        return list(self._watchlist)


class StubToast(object):
    def __init__(self):
        self.messages = []

    def show(self, text, kind="info", **kwargs):
        self.messages.append((kind, str(text)))
        return None


class StubApp(object):
    """只提供 ``BaseView`` 需要的四样东西：services / tasks / toast / status。"""

    def __init__(self, root, services):
        self.services = services
        self.tasks = TaskRunner(root, max_workers=4)
        self.toast = StubToast()
        self.statuses = []

    def status(self, text):
        self.statuses.append(str(text))


# --------------------------------------------------------------------------- 工具


def descendants(widget):
    out = []
    try:
        children = widget.winfo_children()
    except Exception:                       # noqa: BLE001 - 控件已销毁
        return out
    for child in children:
        out.append(child)
        out.extend(descendants(child))
    return out


def find_tree(widget):
    for item in descendants(widget):
        if isinstance(item, ttk.Treeview):
            return item
    return None


# --------------------------------------------------------------------------- 纯函数 / 列定义


class ConstantsTest(unittest.TestCase):
    """列定义与口径常量（无需 Tk）。"""

    def test_columns(self):
        self.assertEqual([column["key"] for column in level2_module.LEVEL_COLUMNS],
                         ["slot", "price", "volume", "amount_wan"])
        self.assertEqual([column["key"] for column in level2_module.TICK_COLUMNS],
                         ["time", "price", "volume", "amount_wan", "side"])
        self.assertEqual([column["key"] for column in level2_module.FLOW_COLUMNS],
                         ["label", "buy_wan", "sell_wan", "net_wan", "buy_pct", "count"])

    def test_tool_columns(self):
        """工具表列定义：顺序与「只有一列着色」的约定（避免多列抢行色）。"""
        self.assertEqual([column["key"] for column in level2_module.BIG_ORDER_COLUMNS],
                         ["time", "price", "volume", "amount_wan", "side", "bucket"])
        self.assertEqual([column["key"] for column in level2_module.SCAN_COLUMNS],
                         ["code", "name", "price", "change_pct", "imbalance_pct", "diff",
                          "volume_ratio", "seal", "distance_pct"])
        self.assertEqual([column["key"] for column in level2_module.RANK_COLUMNS],
                         ["code", "name", "main_wan", "main_pct", "tick_count"])
        self.assertEqual([column["key"] for column in level2_module.BIG_ORDER_COLUMNS
                          if column["kind"] == "badge"], ["side"])
        self.assertEqual(level2_module.BIG_ORDER_COLUMNS[4]["badge_colors"],
                         {"买": "up", "卖": "down", "中性": "flat"})
        for columns, color_key in ((level2_module.SCAN_COLUMNS, "change_pct"),
                                   (level2_module.RANK_COLUMNS, "main_wan")):
            numeric = [column["key"] for column in columns
                       if column["kind"] in ("num", "pct", "int", "money")]
            self.assertEqual(numeric, [color_key], "每张工具表只允许一列按数值着色")

    def test_tool_constants_and_notes(self):
        """阈值下拉、上限、耗时提示与口径灰字（页面上必须如实写清）。"""
        self.assertEqual([label for label, _value in level2_module.BIG_ORDER_THRESHOLDS],
                         ["20 万", "50 万", "100 万", "200 万"])
        self.assertEqual(dict(level2_module.BIG_ORDER_THRESHOLDS)["100 万"], 1000000.0)
        self.assertEqual(level2_module.DEFAULT_BIG_THRESHOLD, 1000000.0)
        self.assertEqual(level2_module.BIG_ORDER_LIMIT, 50)
        self.assertEqual(level2_module.TOOL_CODES_MAX, 10)
        self.assertEqual(level2_module.SCAN_LIMIT, 10)
        self.assertEqual(level2_module.RANK_LIMIT, 1000)
        self.assertEqual(level2_module.RANK_TOP, 10)
        self.assertEqual(level2_module.FLOW_SERIES_LIMIT, level2_module.FLOW_LIMIT)
        self.assertEqual(level2_module.SEAL_BADGE_KINDS["limit_up"], "up")
        self.assertEqual(level2_module.SEAL_BADGE_KINDS["limit_down"], "down")
        for keyword in ("4000 笔", "第三方盘口标记"):
            self.assertIn(keyword, level2_module.BIG_ORDER_NOTE)
        self.assertIn("开板次数", level2_module.SEAL_NOTE)
        self.assertIn("累计净额", level2_module.FLOW_SERIES_NOTE)
        for keyword in ("进行中", "2–4 分钟"):
            self.assertIn(keyword, level2_module.RANK_RUNNING_TEXT)
        self.assertIn("2–4 分钟", level2_module.TOOL_LATENCY_HINT)
        self.assertIn("不跑下方工具", level2_module.AUTO_REFRESH_SCOPE)
        self.assertIn("委比降序", level2_module.SCAN_NOTE)
        self.assertIn("主力净额降序", level2_module.RANK_NOTE)

    def test_slot_colors_sell_green_buy_red(self):
        self.assertEqual(level2_module.SLOT_COLORS["卖1"], "down")
        self.assertEqual(level2_module.SLOT_COLORS["卖5"], "down")
        self.assertEqual(level2_module.SLOT_COLORS["买1"], "up")
        self.assertEqual(level2_module.SLOT_COLORS["买5"], "up")

    def test_limits_and_disclaimer(self):
        self.assertEqual(level2_module.TICK_LIMIT, 60)
        self.assertEqual(level2_module.AUTO_REFRESH_MS, 3000)
        for keyword in ("公开源快照", "第三方盘口标记", "非交易所", "付费授权"):
            self.assertIn(keyword, level2_module.DISCLAIMER)

    def test_view_identity(self):
        self.assertEqual(Level2View.id, "level2")
        self.assertEqual(Level2View.title, "盘口 / L2")

    def test_code_normalization(self):
        self.assertEqual(Level2View._normalize("600519"), CODE)
        self.assertEqual(Level2View._normalize(" sh600519 "), CODE)
        self.assertEqual(Level2View._normalize(CODE), CODE)
        self.assertEqual(Level2View._normalize(""), "")
        self.assertEqual(Level2View._normalize("abc"), "")

    def test_registered_in_view_specs(self):
        from quantstudio_desktop.views import VIEW_SPECS

        keys = [item[0] for item in VIEW_SPECS]
        self.assertIn("level2", keys)
        self.assertEqual(keys[-1], "level2", "新页排在最后 → 快捷键是 Ctrl+%d" % len(keys))
        self.assertEqual(VIEW_SPECS[-1][1], "盘口 / L2")
        self.assertEqual(VIEW_SPECS[-1][2], "quantstudio_desktop.views.level2")
        self.assertEqual(VIEW_SPECS[-1][3], "Level2View")


# --------------------------------------------------------------------------- 页面测试


class ViewTestCase(unittest.TestCase):
    """共用夹具：真实 Tk 根窗口 + 替身 App。"""

    @classmethod
    def setUpClass(cls):
        if not WIDGETS_OK:
            print("\n[提示] widgets 控件库不可用，相关断言将跳过：%s" % level2_module.WIDGETS_ERROR)
        try:
            cls.root = tk.Tk()
        except Exception as exc:            # noqa: BLE001 - 无显示器环境
            raise unittest.SkipTest("Tk 初始化失败，跳过 GUI 测试：%s" % exc)
        cls.root.withdraw()
        theme.init(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:                   # noqa: BLE001
            pass

    def setUp(self):
        self.apps = []
        self.frames = []

    def tearDown(self):
        for frame in self.frames:
            try:
                frame.destroy()
            except Exception:               # noqa: BLE001
                pass
        for app in self.apps:
            try:
                app.tasks.shutdown()
            except Exception:               # noqa: BLE001
                pass
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.root.unbind_all(sequence)
            except Exception:               # noqa: BLE001
                pass
        try:
            self.root.update_idletasks()
        except Exception:                   # noqa: BLE001
            pass

    # ---- 夹具
    def make_app(self, **kwargs):
        services = kwargs.pop("services", None) or FakeServices(**kwargs)
        app = StubApp(self.root, services)
        self.apps.append(app)
        return app

    def open_page(self, app, code=CODE, query=True, timeout=20.0):
        """构建页面并（可选）输入代码后 refresh，驱动事件循环直到后台请求全部回主线程。"""
        frame = ttk.Frame(self.root, style="TFrame")
        frame.pack(fill="both", expand=True)
        self.frames.append(frame)
        page = Level2View(frame, app)
        page.pack(fill="both", expand=True)
        page.ensure_built()
        if code:
            page.code_var.set(code)
        if query:
            page.reload()
        self.pump(page, app, timeout)
        return page

    def pump(self, page, app, timeout=20.0):
        deadline = time.time() + timeout
        idle = 0
        while time.time() < deadline:
            self.root.update()
            if page._pending == 0 and getattr(app.tasks, "busy", 0) == 0:
                idle += 1
                if idle >= 3:
                    return True
            else:
                idle = 0
            time.sleep(0.02)
        self.fail("后台任务在 %.1fs 内未完成（pending=%s）" % (timeout, page._pending))

    def wait_until(self, condition, timeout=15.0, message="条件未在 %.1fs 内满足"):
        """驱动事件循环直到 ``condition()`` 为真（用于「还有任务在飞」的场景，pump 会等不到）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.root.update()
            except Exception:               # noqa: BLE001 - 控件销毁中
                pass
            try:
                if condition():
                    return True
            except Exception:               # noqa: BLE001 - 控件还没渲染好，继续等
                pass
            time.sleep(0.02)
        self.fail(message % timeout)

    @staticmethod
    def row_tags(tree, index):
        """行 iid 形如 ``r0``（见 ``widgets/table.DataTable``）。"""
        iid = tree.get_children()[index]
        tags = tree.item(iid, "tags")
        return list(tags) if tags else []

    @staticmethod
    def tag_foreground(tree, tag):
        options = tree.tag_configure(tag)
        value = options.get("foreground") if isinstance(options, dict) else None
        return str(value) if value else ""

    def assert_row_color(self, tree, index, color_key):
        """断言某行因「档位/方向」着色：行 tag 的 foreground == theme.COLORS[color_key]。"""
        expected = theme.COLORS[color_key]
        tags = self.row_tags(tree, index)
        colors = [self.tag_foreground(tree, tag) for tag in tags]
        self.assertIn(expected, colors, "第 %d 行应为 %s（tags=%s）" % (index, color_key, tags))

    # ------------------------------------------------------------------ 构建 + 渲染
    def test_page_builds_and_renders_contract_data(self):
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2

        self.assertTrue(page._built)
        self.assertEqual(page.build_errors, [], "页面区块不应构建失败")
        self.assertEqual(page._pending, 0)
        self.assertEqual(len(page.metric_cards), 6)
        self.assertEqual(len(page.flow_cards), 2)
        self.assertEqual(sorted(fake.names()), ["capital_flow", "orderbook", "ticks"])
        limits = dict((name, kwargs.get("limit")) for name, kwargs in fake.calls)
        self.assertEqual(limits["ticks"], level2_module.TICK_REQUEST_LIMIT)
        self.assertEqual(limits["capital_flow"], level2_module.FLOW_LIMIT)
        self.assertEqual(self.errors(app), [], "正常路径不应产生错误提示")

        # 指标卡：现价 / 涨跌幅 / 委比 / 委差 / 外盘 / 内盘
        self.assertEqual(page.metric_cards["price"].value_label.cget("text"), "1,680.50")
        self.assertEqual(page.metric_cards["change_pct"].value_label.cget("text"), "+1.23%")
        self.assertEqual(page.metric_cards["imbalance"].value_label.cget("text"), "+12.34%")
        # 委差 = 委买 600 手 − 委卖 700 手 = -100
        self.assertEqual(page.metric_cards["diff"].value_label.cget("text"), "-100")
        self.assertEqual(page.metric_cards["outer"].value_label.cget("text"), "12,000")
        self.assertEqual(page.metric_cards["inner"].value_label.cget("text"), "9,800")
        self.assertIn("昨收 1,660.00", page.metric_cards["price"].sub_label.cget("text"))

        # 资金流卡：主力净额 / 主力净额占比
        self.assertEqual(page.flow_cards["main"].value_label.cget("text"), "10.00")
        self.assertEqual(page.flow_cards["main_pct"].value_label.cget("text"), "+7.14%")

        # 五档表：10 行，行序 卖5 → 卖1 → 买1 → 买5
        self.assertEqual([row["slot"] for row in page._orderbook_rows],
                         ["卖5", "卖4", "卖3", "卖2", "卖1", "买1", "买2", "买3", "买4", "买5"])
        self.assertGreater(page._orderbook_rows[0]["price"], page._orderbook_rows[9]["price"],
                           "最高卖价应排在最上面")
        # 逐笔表：最多 60 行（替身给 70 条）
        self.assertEqual(len(page._tick_rows), level2_module.TICK_LIMIT)
        # 资金流表：四档 + 净额可着色
        self.assertEqual([row["label"] for row in page._flow_rows],
                         ["超大单", "大单", "中单", "小单"])
        self.assertEqual(page._flow_rows[0]["net_wan"], 14.0)
        self.assertEqual(page._flow_rows[0]["buy_pct"], "+64.00%")

        # 能力位与合规标注：都要出现在页面上
        self.assertIn("5 档盘口", page.level_title.cget("text"))
        self.assertIn("需付费授权", page.level_title.cget("text"))
        self.assertIn("公开源快照", level2_module.DISCLAIMER)

        if not WIDGETS_OK:
            self.assertTrue(page.fallback is not None, "控件缺失时应显示文本摘要")
            return

        level_tree = find_tree(page.level_table)
        tick_tree = find_tree(page.tick_table)
        flow_tree = find_tree(page.flow_table)
        for tree, expected in ((level_tree, 10), (tick_tree, 60), (flow_tree, 4)):
            self.assertIsNotNone(tree)
            self.assertEqual(len(tree.get_children()), expected)
        self.assertFalse(page.level_table.empty_visible())
        # 表格行数与空状态必须互相一致（没有数据才显示空状态）

        # 卖绿买红：卖 5 行绿（down）、买 1 行红（up）
        self.assert_row_color(level_tree, 0, "down")
        self.assert_row_color(level_tree, 4, "down")
        self.assert_row_color(level_tree, 5, "up")
        self.assert_row_color(level_tree, 9, "up")
        # 逐笔方向：买红卖绿（第 2 行 side=sell → 绿）
        self.assertEqual(level_tree.set(level_tree.get_children()[0], "slot"), "卖5")
        self.assertEqual(level_tree.set(level_tree.get_children()[9], "slot"), "买5")
        self.assert_row_color(tick_tree, 0, "up")
        self.assert_row_color(tick_tree, 1, "down")

    def test_flow_status_line_uses_wan(self):
        """资金流状态行必须按万元换算（曾把元当万元显示）。"""
        app = self.make_app(empty=True)
        page = self.open_page(app, code="")
        payload = make_flow()
        payload["meta"] = dict(META_OFFLINE)
        page._render_flow(payload)
        text = page.status_label.cget("text")
        self.assertIn("主力净额 10.00 万元", text)
        self.assertNotIn("100,000.00 万元", text)
        self.assertFalse(page.flow_table.empty_visible())
        # 无逐笔样本时不留一排 0，改为内联空状态
        page._render_flow(make_flow(empty=True))
        self.assertEqual(page._flow_rows, [])
        self.assertIn("逐笔样本", page.status_label.cget("text"))
        self.assertTrue(page.flow_table.empty_visible())

    def test_reload_keeps_widgets(self):
        app = self.make_app()
        page = self.open_page(app)
        cards_before = [id(card) for card in page.metric_cards.values()]
        tables_before = [id(page.level_table), id(page.tick_table), id(page.flow_table)]
        page.reload()
        self.pump(page, app)
        self.assertEqual([id(card) for card in page.metric_cards.values()], cards_before,
                         "刷新不应重建卡片（否则滚动位置/选中态会丢）")
        self.assertEqual([id(page.level_table), id(page.tick_table), id(page.flow_table)],
                         tables_before, "刷新不应重建表格")
        self.assertEqual(app.services.level2.names().count("orderbook"), 2)

    def test_empty_code_shows_guidance_without_requests(self):
        app = self.make_app()
        page = self.open_page(app, code="")
        self.assertEqual(app.services.level2.calls, [], "空代码不应发起任何请求")
        self.assertEqual(page._pending, 0)
        self.assertIn("输入标的代码", page.status_label.cget("text"))
        if WIDGETS_OK:
            self.assertTrue(page.level_table.empty_visible())
            self.assertIn("输入标的代码", page.level_table.empty_text)
            self.assertTrue(page.tick_table.empty_visible())
            self.assertTrue(page.flow_table.empty_visible())

    def test_invalid_code_does_not_call_service(self):
        app = self.make_app()
        page = self.open_page(app, code="not-a-code")
        self.assertEqual(app.services.level2.calls, [])
        self.assertEqual(self.errors(app), [])

    # ------------------------------------------------------------------ 空 / 离线
    def test_empty_payload_draws_inline_empty_state(self):
        app = self.make_app(empty=True)
        page = self.open_page(app)
        self.assertEqual(page._orderbook_rows, [])
        self.assertEqual(page._tick_rows, [])
        self.assertEqual(page._flow_rows, [])
        self.assertEqual(page._pending, 0)
        if WIDGETS_OK:
            self.assertTrue(page.level_table.empty_visible(), "空五档要显示内联空状态")
            self.assertTrue(page.tick_table.empty_visible())
            self.assertTrue(page.flow_table.empty_visible())
            self.assertIn("五档数据为空", page.level_table.empty_text)
            self.assertEqual(len(find_tree(page.level_table).get_children()), 0)

    def test_offline_meta_shows_notice_and_empty_state(self):
        app = self.make_app(offline=True, empty=True)
        page = self.open_page(app)
        self.assertEqual(page.notice_label.winfo_manager(), "pack", "离线提示应显示在页面顶部")
        text = page.notice_label.cget("text")
        self.assertIn("离线", text)
        self.assertIn("不可用于交易决策", text)
        if WIDGETS_OK:
            self.assertIn("离线/演示数据", page.level_table.empty_text)
            self.assertIn("离线/演示数据", page.tick_table.empty_text)
            self.assertIn("离线/演示数据", page.flow_table.empty_text)

    def test_offline_meta_with_data_still_renders_rows(self):
        app = self.make_app(offline=True)
        page = self.open_page(app)
        self.assertEqual(len(page._orderbook_rows), 10, "离线也只是标注来源，不丢已有数据")
        self.assertIn("离线/演示数据", page.notice_label.cget("text"))
        self.assertNotEqual(page.notice_label.winfo_manager(), "", "离线提示条必须可见")

    # ------------------------------------------------------------------ 失败
    def test_broken_service_shows_inline_empty_state_not_crash(self):
        app = self.make_app(broken=True)
        with redirect_stderr(io.StringIO()):        # TaskRunner 会打印后台异常堆栈
            page = self.open_page(app)
        self.assertEqual(page._pending, 0)
        self.assertTrue(self.errors(app), "服务异常必须提示用户")
        self.assertIn("服务不可用", " ".join(self.errors(app)))
        if WIDGETS_OK:
            self.assertTrue(page.level_table.empty_visible(), "失败要画内联空状态而不是崩溃")
            self.assertIn("加载失败", page.level_table.empty_text)
            self.assertIn("服务不可用", page.level_table.empty_text)
            self.assertTrue(page.tick_table.empty_visible())
            self.assertTrue(page.flow_table.empty_visible())
            self.assertEqual(page.metric_cards["price"].value_label.cget("text"), "—")

    def test_missing_level2_namespace_shows_reason(self):
        services = FakeServices(with_level2=False)
        app = self.make_app(services=services)
        with redirect_stderr(io.StringIO()):
            page = self.open_page(app)
        self.assertEqual(page._pending, 0)
        if WIDGETS_OK:
            self.assertTrue(page.level_table.empty_visible())
            self.assertIn("level2", page.level_table.empty_text)
        self.assertIn("level2", page.status_label.cget("text"))

    def test_partial_failure_keeps_other_blocks(self):
        """逐笔失败时，五档/资金流仍要有数据（区块之间互不拖累）。"""
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2

        def boom(code, limit=600):
            raise RuntimeError("逐笔数据源超时")

        fake.ticks = boom
        with redirect_stderr(io.StringIO()):
            page.reload()
            self.pump(page, app)
        self.assertEqual(len(page._orderbook_rows), 10)
        self.assertEqual([row["label"] for row in page._flow_rows],
                         ["超大单", "大单", "中单", "小单"])
        if WIDGETS_OK:
            self.assertTrue(page.tick_table.empty_visible())
            self.assertIn("逐笔数据源超时", page.tick_table.empty_text)
            self.assertFalse(page.level_table.empty_visible())

    # ------------------------------------------------------------------ 自动刷新
    def test_auto_refresh_off_does_not_request(self):
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2
        before = len(fake.calls)

        self.assertEqual(page.auto_var.get(), "0", "自动刷新默认关闭")
        self.assertIsNone(page._auto_job, "关闭时不应挂定时器")
        page._auto_tick()                       # 模拟定时器到期
        self.pump(page, app)
        self.assertEqual(len(fake.calls), before, "关闭状态下定时回调不得发起请求")
        self.assertIsNone(page._auto_job)

    def test_auto_refresh_on_requests_and_off_cancels(self):
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2
        before = len(fake.calls)

        page.auto_check.invoke()                # 勾选「自动刷新（3 秒）」
        self.assertEqual(page.auto_var.get(), "1")
        self.assertIsNotNone(page._auto_job, "开启后应挂 3 秒定时器")

        with redirect_stderr(io.StringIO()):
            page._auto_tick()                   # 模拟定时器到期
            self.pump(page, app)
        self.assertGreater(len(fake.calls), before, "开启后定时回调应重新查询")
        self.assertEqual(len(fake.calls) - before, 3, "三个契约接口各查一次")
        self.assertIsNotNone(page._auto_job, "回调结束后应继续排下一次")

        page.auto_check.invoke()                # 取消勾选
        self.assertEqual(page.auto_var.get(), "0")
        self.assertIsNone(page._auto_job, "关闭后必须清掉定时器")
        after_off = len(fake.calls)
        page._auto_tick()
        self.pump(page, app)
        self.assertEqual(len(fake.calls), after_off, "关闭后不得再发起请求")

    def test_auto_refresh_skips_request_when_page_hidden(self):
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2
        page.auto_check.invoke()
        before = len(fake.calls)
        page.pack_forget()                      # 切到别的页面（app.select_view 的行为）
        page._auto_tick()
        self.pump(page, app)
        self.assertEqual(len(fake.calls), before, "页面不在前台时不发请求")
        page.pack(fill="both", expand=True)
        page._cancel_auto()

    # ------------------------------------------------------------------ 工具：大单追踪
    def test_big_orders_block_renders_rows_cards_and_colors(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        fake = app.services.level2
        page.big_button.invoke()
        self.pump(page, app)

        self.assertEqual(fake.kwargs_of("big_orders")["threshold"], level2_module.DEFAULT_BIG_THRESHOLD)
        self.assertEqual(fake.kwargs_of("big_orders")["limit"], level2_module.BIG_ORDER_LIMIT)
        self.assertEqual(len(page.big_rows), 12, "替身给 12 笔大单，全部展示")
        # 时间倒序：最新一笔（index=11）在前，方向买、档位超大单
        self.assertEqual(page.big_rows[0]["time"], "14:41:00")
        self.assertEqual(page.big_rows[0]["price"], "1,685.50")
        self.assertEqual(page.big_rows[0]["volume"], "210")
        self.assertEqual(page.big_rows[0]["amount_wan"], "210.00")
        self.assertEqual(page.big_rows[0]["side"], "买")
        self.assertEqual(page.big_rows[0]["bucket"], "超大单")
        # 统计行：笔数 / 买 / 卖 / 净额（万元）
        self.assertEqual(page.big_cards["count"].value_label.cget("text"), "12")
        self.assertEqual(page.big_cards["buy"].value_label.cget("text"), "960.00")
        self.assertEqual(page.big_cards["sell"].value_label.cget("text"), "900.00")
        self.assertEqual(page.big_cards["net"].value_label.cget("text"), "60.00")
        self.assertIn("买 6 笔 / 卖 6 笔", page.big_cards["count"].sub_label.cget("text"))
        self.assertIn("占样本成交额", page.big_cards["net"].sub_label.cget("text"))
        # 标题行与灰字口径（样本 4000 笔 + 第三方盘口标记）
        self.assertIn("阈值 100 万", page.big_title.cget("text"))
        self.assertIn("样本 4,000 笔", page.big_title.cget("text"))
        self.assertIn("最大单笔 210.00 万元", page.big_title.cget("text"))
        self.assertIn("4000 笔", page.big_note.cget("text"))
        self.assertIn("第三方盘口标记", page.big_note.cget("text"))
        self.assertEqual(self.errors(app), [])

        if not WIDGETS_OK:
            return
        tree = find_tree(page.big_table)
        self.assertIsNotNone(tree)
        self.assertEqual(len(tree.get_children()), 12)
        self.assertFalse(page.big_table.empty_visible())
        self.assert_row_color(tree, 0, "up")            # 买 → 红
        self.assert_row_color(tree, 1, "down")          # 卖 → 绿
        self.assertEqual(tree.set(tree.get_children()[0], "side"), "买")
        self.assertEqual(tree.set(tree.get_children()[1], "side"), "卖")

    def test_big_orders_threshold_dropdown_is_passed_through(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        self.assertEqual(page._selected_threshold(), level2_module.DEFAULT_BIG_THRESHOLD)
        self.assertEqual([label for label, _value in level2_module.BIG_ORDER_THRESHOLDS],
                         list(page.big_box.cget("values")))
        page.big_threshold_var.set("20 万")
        page.big_button.invoke()
        self.pump(page, app)
        self.assertEqual(app.services.level2.kwargs_of("big_orders")["threshold"], 200000.0)
        self.assertIn("阈值 20 万", page.big_title.cget("text"))

    def test_big_orders_without_code_shows_hint_and_sends_nothing(self):
        app = self.make_app()
        page = self.open_page(app, code="")
        page.big_button.invoke()
        self.pump(page, app)
        self.assertEqual(app.services.level2.calls, [], "没有标的时不得发请求")
        self.assertIn("输入代码", page.big_title.cget("text"))
        if WIDGETS_OK:
            self.assertTrue(page.big_table.empty_visible())
            self.assertIn("输入代码", page.big_table.empty_text)

    # ------------------------------------------------------------------ 工具：资金流分时
    def test_flow_series_block_draws_chart_and_bucket_cards(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        page.series_button.invoke()
        self.pump(page, app)

        # x = 分钟（HH:MM），y = 累计净额 / 1e4 万元
        self.assertEqual(page.series_labels, ["09:31", "09:32", "09:33", "09:34", "09:35", "09:36"])
        self.assertEqual(page.series_values, [-6.0, -9.0, -9.0, -6.0, 0.0, 9.0])
        self.assertEqual(page.series_cards["main"].value_label.cget("text"), "10.00")
        self.assertEqual(page.series_cards["main_pct"].value_label.cget("text"), "+7.14%")
        self.assertEqual(page.series_cards["minutes"].value_label.cget("text"), "6")
        # 四档净额小结：红涨绿跌（数值本身交给 StatCard 按符号着色）
        self.assertEqual(page.series_bucket_cards["super_big"].value_label.cget("text"), "14.00")
        self.assertEqual(page.series_bucket_cards["big"].value_label.cget("text"), "-4.00")
        self.assertEqual(page.series_bucket_cards["mid"].value_label.cget("text"), "3.00")
        self.assertEqual(page.series_bucket_cards["small"].value_label.cget("text"), "-1.00")
        self.assertIn("分钟 6 个", page.series_title.cget("text"))
        self.assertIn("最新累计净额 9.00 万元", page.series_title.cget("text"))
        self.assertEqual(self.errors(app), [])

        if not WIDGETS_OK:
            return
        self.assertTrue(page.flow_chart.has_data(), "折线图必须画出来")
        # 主力净额为正 → 曲线红（红涨绿跌）
        self.assertEqual(page.flow_chart.plot_color(), theme.COLORS["up"])

    def test_flow_series_single_minute_shows_inline_empty_state(self):
        app = self.make_app()
        page = self.open_page(app, code="", query=False)
        payload = make_flow_series(minutes=1)
        page._render_flow_series(payload)
        self.assertEqual(len(page.series_values), 1)
        if WIDGETS_OK:
            self.assertIn("分钟序列不足", page.flow_chart.empty_text)
        # 无逐笔样本：卡片全部「—」，不留一排 0 假装有数据
        page._render_flow_series(make_flow_series(empty=True))
        self.assertEqual(page.series_values, [])
        for key in level2_module.BUCKET_ORDER:
            self.assertEqual(page.series_bucket_cards[key].value_label.cget("text"), "—")
        if WIDGETS_OK:
            self.assertIn("分钟序列不足", page.flow_chart.empty_text)

    def test_flow_series_error_is_inline(self):
        app = self.make_app()
        page = self.open_page(app, code="", query=False)
        page.code_var.set(CODE)
        page._on_series_error(RuntimeError("逐笔源超时"))
        self.assertIn("加载失败", page.series_title.cget("text"))
        self.assertIn("逐笔源超时", self.status_line(page))
        if WIDGETS_OK:
            self.assertIn("加载失败", page.flow_chart.empty_text)
        self.assertTrue(self.errors(app))

    # ------------------------------------------------------------------ 工具：封板状态
    def test_seal_block_renders_badge_and_key_values(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        page.seal_button.invoke()
        self.pump(page, app)

        self.assertEqual(page.seal_state, "limit_up")
        self.assertEqual(page.seal_badge.cget("text"), "涨停")
        self.assertEqual(page.seal_badge.kind, "up")
        self.assertEqual(page.seal_kv_left.get("现价"), "1,826.00")
        self.assertEqual(page.seal_kv_left.get("昨收"), "1,660.00")
        self.assertEqual(page.seal_kv_left.get("涨停价"), "1,826.00")
        self.assertEqual(page.seal_kv_right.get("封单量（手）"), "12,345")
        self.assertEqual(page.seal_kv_right.get("封单额（万元）"), "22,540.00")
        self.assertEqual(page.seal_kv_right.get("封成比 %"), "12.50%")
        self.assertIn("10%", page.seal_title.cget("text"))
        self.assertIn("开板次数", page.seal_note.cget("text"))
        self.assertEqual(self.errors(app), [])

    def test_seal_block_limit_down_and_unknown(self):
        app = self.make_app()
        page = self.open_page(app, code="", query=False)
        page._render_seal(make_seal(state="limit_down"))
        self.assertEqual(page.seal_state, "limit_down")
        self.assertEqual(page.seal_badge.cget("text"), "跌停")
        self.assertEqual(page.seal_badge.kind, "down")
        self.assertEqual(page.seal_kv_right.get("封成比 %"), "6.64%")
        page._render_seal(make_seal(empty=True))
        self.assertEqual(page.seal_state, "unknown")
        self.assertEqual(page.seal_badge.cget("text"), "数据不足")
        self.assertEqual(page.seal_badge.kind, "flat")
        self.assertEqual(page.seal_kv_left.get("涨停价"), "—")
        self.assertEqual(page.seal_kv_right.get("封单额（万元）"), "—")

    # ------------------------------------------------------------------ 工具：扫描 / 排行
    def test_scan_block_sorts_by_imbalance_and_reports_failures(self):
        services = FakeServices(watchlist=["000001.SZ", "600519.SH", "bad-code"])
        app = self.make_app(services=services)
        page = self.open_page(app, code=CODE, query=False)
        fake = app.services.level2
        fake.scan_failure_rows = [{"code": "300750.SZ", "error": "快照超时"}]
        page.scan_button.invoke()
        self.pump(page, app)

        # 标的 = 输入 + 自选池（去重、去掉非法代码）
        self.assertEqual(fake.kwargs_of("scan")["codes"], [CODE, "000001.SZ"])
        self.assertEqual(fake.kwargs_of("scan")["limit"], level2_module.SCAN_LIMIT)
        self.assertIn("本次标的（2 只）", page.tool_codes_label.cget("text"))
        self.assertIn("000001.SZ", page.tool_codes_label.cget("text"))
        # 按委比降序：000001（30.5）排在 600519（12.34）前
        self.assertEqual([row["code"] for row in page.scan_rows], ["000001.SZ", "600519.SH"])
        self.assertEqual(page.scan_rows[0]["diff"], "-1,000")       # 4000 − 5000 手
        self.assertEqual(page.scan_rows[0]["volume_ratio"], "—")
        self.assertEqual(page.scan_rows[0]["seal"], "未封板")
        self.assertEqual(page.scan_rows[1]["imbalance_pct"], "+12.34%")
        self.assertIn("成功 2 只 · 失败 1 只", page.scan_title.cget("text"))
        self.assertIn("失败 1 只", page.scan_failures.cget("text"))
        self.assertIn("300750.SZ", page.scan_failures.cget("text"))
        self.assertIn("快照超时", page.scan_failures.cget("text"))
        self.assertEqual(self.errors(app), [])

        if not WIDGETS_OK:
            return
        tree = find_tree(page.scan_table)
        self.assertEqual(len(tree.get_children()), 2)
        self.assert_row_color(tree, 0, "down")          # 跌 0.80% → 绿
        self.assert_row_color(tree, 1, "up")            # 涨 1.23% → 红

    def test_flow_rank_block_sorts_by_main_net(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        page.include_watch_var.set("0")                 # 只看输入标的
        fake = app.services.level2
        fake.rank_failure_rows = [{"code": "300750.SZ", "error": "逐笔拉取失败"}]
        page.rank_button.invoke()
        self.pump(page, app)

        self.assertEqual(fake.kwargs_of("flow_rank")["codes"], [CODE])
        self.assertEqual(fake.kwargs_of("flow_rank")["limit"], level2_module.RANK_LIMIT)
        self.assertEqual(fake.kwargs_of("flow_rank")["top"], level2_module.RANK_TOP)
        # 按主力净额降序：+100 万（600519）排在 −500 万（000001）前
        self.assertEqual([row["code"] for row in page.rank_rows], ["600519.SH", "000001.SZ"])
        self.assertEqual(page.rank_rows[0]["main_wan"], 100.0)
        self.assertEqual(page.rank_rows[1]["main_wan"], -500.0)
        self.assertEqual(page.rank_rows[0]["tick_count"], "140")
        self.assertIn("失败 1 只", page.rank_failures.cget("text"))
        self.assertIn("300750.SZ", page.rank_failures.cget("text"))
        self.assertIn("已完成", page.rank_status.cget("text"))
        self.assertIn("600519.SH", page.rank_status.cget("text"))
        self.assertEqual(self.errors(app), [])

        if not WIDGETS_OK:
            return
        tree = find_tree(page.rank_table)
        self.assertEqual(len(tree.get_children()), 2)
        self.assert_row_color(tree, 0, "up")
        self.assert_row_color(tree, 1, "down")
        self.assertEqual(tree.set(tree.get_children()[0], "main_wan"), "100.00")
        self.assertEqual(tree.set(tree.get_children()[1], "main_wan"), "-500.00")

    def test_rank_running_stays_non_blocking_and_shows_latency_hint(self):
        """排行进行中：只有它自己的按钮禁用，其它区块照常完成。"""
        import threading

        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        fake = app.services.level2
        started = threading.Event()
        release = threading.Event()

        def slow_rank(codes, limit=1000, top=10):
            started.set()
            release.wait(20.0)
            payload = make_flow_rank()
            payload["requested"] = len(list(codes))
            payload["meta"] = dict(TOOL_META)
            return payload

        fake.flow_rank = slow_rank
        page.rank_button.invoke()
        self.assertTrue(started.wait(10.0), "排行任务应已进入后台线程")
        # 进行中文案 + 只锁自己
        self.assertIn("进行中", page.rank_status.cget("text"))
        self.assertIn("2–4 分钟", page.rank_status.cget("text"))
        self.assertEqual(str(page.rank_button.cget("state")), "disabled")
        self.assertEqual(str(page.scan_button.cget("state")), "normal")
        self.assertEqual(str(page.big_button.cget("state")), "normal")
        self.assertEqual(str(page.series_button.cget("state")), "normal")
        self.assertEqual(str(page.seal_button.cget("state")), "normal")
        self.assertIn("最多约 2–4 分钟", page.rank_table.empty_text)

        # 排行还在飞的时候，大单区块可以正常跑完
        page.big_button.invoke()
        self.wait_until(lambda: len(page.big_rows) == 12, message="大单区块应能在排行进行中完成")
        self.assertGreaterEqual(page._pending, 1, "排行应仍在进行中")
        self.assertEqual(str(page.rank_button.cget("state")), "disabled")

        release.set()
        self.wait_until(lambda: len(page.rank_rows) == 2 and str(page.rank_button.cget("state")) == "normal",
                        message="排行完成后应恢复按钮并渲染结果")
        self.pump(page, app)
        self.assertEqual(page._pending, 0)
        self.assertEqual(len(page.rank_rows), 2)

    def test_tool_blocks_without_codes_fail_inline_without_request(self):
        app = self.make_app()
        page = self.open_page(app, code="", query=False)
        page.include_watch_var.set("0")
        with redirect_stderr(io.StringIO()):
            page.scan_button.invoke()
            page.rank_button.invoke()
            self.pump(page, app)
        self.assertEqual(app.services.level2.calls, [])
        self.assertIn("没有可用标的", page.scan_title.cget("text"))
        self.assertIn("没有可用标的", page.rank_title.cget("text"))
        self.assertEqual(str(page.scan_button.cget("state")), "normal")
        self.assertEqual(str(page.rank_button.cget("state")), "normal")

    def test_tool_codes_resolution_dedup_cap_and_watchlist_failure(self):
        services = FakeServices(watchlist=["000001.SZ", "sh600519", "bad-code", "300750.SZ"])
        app = self.make_app(services=services)
        page = self.open_page(app, code="", query=False)
        codes, notes = page._resolve_codes("600519", True)
        self.assertEqual(codes, [CODE, "000001.SZ", "300750.SZ"], "去重 + 规范化 + 去掉非法代码")
        self.assertEqual(notes, [])
        codes, _notes = page._resolve_codes("600519", False)
        self.assertEqual(codes, [CODE], "不勾自选池时只用输入框")

        many = ["%06d.SH" % (600000 + index) for index in range(20)]
        app2 = self.make_app(services=FakeServices(watchlist=many))
        page2 = self.open_page(app2, code="", query=False)
        codes2, notes2 = page2._resolve_codes("", True)
        self.assertEqual(len(codes2), level2_module.TOOL_CODES_MAX)
        self.assertTrue(any("只取前 %d 只" % level2_module.TOOL_CODES_MAX in note for note in notes2))

        app3 = self.make_app(services=FakeServices(watchlist=["000001.SZ"], watch_error="磁盘不可读"))
        page3 = self.open_page(app3, code="", query=False)
        codes3, notes3 = page3._resolve_codes(CODE, True)
        self.assertEqual(codes3, [CODE], "自选池读取失败不影响手输标的")
        self.assertTrue(any("自选池读取失败" in note for note in notes3))

        app4 = self.make_app(services=FakeServices(with_watchlist=False))
        page4 = self.open_page(app4, code="", query=False)
        codes4, notes4 = page4._resolve_codes(CODE, True)
        self.assertEqual(codes4, [CODE])
        self.assertTrue(any("未提供自选池" in note for note in notes4))

    # ------------------------------------------------------------------ 工具：空 / 失败 / 自动刷新
    def test_tool_blocks_empty_data_draw_inline_empty_state(self):
        app = self.make_app(empty=True)
        page = self.open_page(app, code=CODE, query=False)
        for button in (page.big_button, page.series_button, page.seal_button, page.scan_button,
                       page.rank_button):
            button.invoke()
        self.pump(page, app)

        self.assertEqual(page._pending, 0)
        self.assertEqual(page.big_rows, [])
        self.assertEqual(page.series_values, [])
        self.assertEqual(page.scan_rows, [])
        self.assertEqual(page.rank_rows, [])
        self.assertEqual(page.seal_state, "unknown")
        self.assertEqual(page.big_cards["count"].value_label.cget("text"), "0")
        self.assertEqual(page.series_cards["main"].value_label.cget("text"), "—")
        if WIDGETS_OK:
            self.assertTrue(page.big_table.empty_visible(), "空大单要显示内联空状态")
            self.assertIn("没有成交", page.big_table.empty_text)
            self.assertTrue(page.scan_table.empty_visible())
            self.assertIn("扫描无结果", page.scan_table.empty_text)
            self.assertTrue(page.rank_table.empty_visible())
            self.assertIn("排行无结果", page.rank_table.empty_text)
            self.assertIn("分钟序列不足", page.flow_chart.empty_text)
        self.assertEqual(self.errors(app), [], "空数据不是错误：不要弹错误提示")
        # 按钮全部恢复可用
        for button in (page.big_button, page.series_button, page.seal_button, page.scan_button,
                       page.rank_button):
            self.assertEqual(str(button.cget("state")), "normal")

    def test_tool_blocks_broken_service_shows_inline_failure_and_recovers(self):
        app = self.make_app(broken=True)
        with redirect_stderr(io.StringIO()):            # TaskRunner 会打印后台异常堆栈
            page = self.open_page(app, code=CODE, query=False)
            for button in (page.big_button, page.series_button, page.seal_button, page.scan_button,
                           page.rank_button):
                button.invoke()
            self.pump(page, app)

        errors = " ".join(self.errors(app))
        self.assertIn("服务不可用", errors, "服务异常必须提示用户")
        self.assertGreaterEqual(len(self.errors(app)), 4, "5 个工具失败各自都要有提示")
        # 每个工具的失败都写在自己的区块里（不弹窗、不互相覆盖）
        self.assertIn("大单追踪加载失败", page.big_title.cget("text"))
        self.assertIn("资金流分时加载失败", page.series_title.cget("text"))
        self.assertIn("封板状态加载失败", page.seal_title.cget("text"))
        self.assertIn("扫描自选池加载失败", page.scan_title.cget("text"))
        self.assertIn("资金流排行加载失败", page.rank_title.cget("text"))
        if WIDGETS_OK:
            self.assertTrue(page.big_table.empty_visible())
            self.assertIn("加载失败", page.big_table.empty_text)
            self.assertIn("服务不可用", page.big_table.empty_text)
            self.assertIn("加载失败", page.scan_table.empty_text)
            self.assertIn("加载失败", page.rank_table.empty_text)
            self.assertIn("加载失败", page.flow_chart.empty_text)
        self.assertIn("加载失败", page.seal_title.cget("text"))
        self.assertIn("加载失败", page.rank_title.cget("text"))
        for button in (page.big_button, page.series_button, page.seal_button, page.scan_button,
                       page.rank_button):
            self.assertEqual(str(button.cget("state")), "normal", "失败后要能重试")

    def test_auto_refresh_does_not_run_tool_blocks(self):
        app = self.make_app()
        page = self.open_page(app)
        fake = app.services.level2
        page.auto_check.invoke()
        before = len(fake.calls)
        with redirect_stderr(io.StringIO()):
            page._auto_tick()
            self.pump(page, app)
        names = [name for name, _kwargs in fake.calls[before:]]
        self.assertEqual(sorted(names), ["capital_flow", "orderbook", "ticks"],
                         "自动刷新只跑盘口 / 逐笔 / 资金流分档")
        for tool in ("big_orders", "flow_series", "seal_status", "scan", "flow_rank"):
            self.assertNotIn(tool, names)
        page._cancel_auto()
        # 页面提示也要写明自动刷新的作用范围
        texts = [widget.cget("text") for widget in descendants(page)
                 if isinstance(widget, ttk.Label) and "自动刷新" in str(widget.cget("text"))]
        self.assertTrue(any("不跑下方工具" in text for text in texts),
                        "自动刷新旁边的灰字要写清不跑工具：%s" % texts)

    def test_tool_blocks_keep_widgets_across_reruns(self):
        app = self.make_app()
        page = self.open_page(app, code=CODE, query=False)
        page.big_button.invoke()
        self.pump(page, app)
        before = [id(page.big_table), id(page.big_cards["net"]), id(page.flow_chart),
                  id(page.seal_badge), id(page.scan_table), id(page.rank_table)]
        page.big_button.invoke()
        page.series_button.invoke()
        page.seal_button.invoke()
        self.pump(page, app)
        after = [id(page.big_table), id(page.big_cards["net"]), id(page.flow_chart),
                 id(page.seal_badge), id(page.scan_table), id(page.rank_table)]
        self.assertEqual(after, before, "重复查询工具不应重建控件")

    @staticmethod
    def status_line(page):
        return page.status_label.cget("text")

    @staticmethod
    def errors(app):
        return [text for kind, text in app.toast.messages if kind == "error"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
