# -*- coding: utf-8 -*-
"""``Level2View``（盘口 / L2）页面级冒烟测试。

设计要点（与 ``test_views_market_portfolio.py`` 同一套夹具）：

* 用真实的 ``Tk()`` 根窗口（withdraw，不弹窗）+ 真实的 ``TaskRunner``，
  验证「后台线程 → 主线程回调 → 控件更新」这条真实链路；
* ``services.level2`` 用替身（契约 JSON、固定样例、不联网），含 broken / empty 两种情形；
* 控件库（``widgets.cards/table``）未交付时页面降级为内联错误，此时跳过依赖具体控件的断言。

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

    # ---- 断言辅助
    def names(self):
        return [name for name, _kwargs in self.calls]


class FakeServices(object):
    """``GuiServices`` 替身：只有页面用到的 level2 命名空间。"""

    def __init__(self, offline=False, empty=False, broken=False, with_level2=True):
        self.level2 = FakeLevel2(offline=offline, empty=empty, broken=broken) if with_level2 else None


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

    @staticmethod
    def errors(app):
        return [text for kind, text in app.toast.messages if kind == "error"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
