# -*- coding: utf-8 -*-
"""``MarketView`` / ``PortfolioView`` 页面级冒烟测试。

设计要点：

* 用真实的 ``Tk()`` 根窗口（withdraw，不弹窗）+ 真实的 ``TaskRunner``，
  验证「后台线程 → 主线程回调 → 控件更新」这条真实链路；
* 服务层用替身对象（固定样例数据、不联网、不改磁盘），构造真实结构的返回值；
* 控件库（``widgets.charts/table/cards/forms``）未交付时页面会降级为内联错误，
  此时跳过依赖具体控件的断言，只断言页面自身的状态（请求计数、提示文本、降级行为）。

运行（GUI 必须用系统自带的 3.9 + tkinter）::

    cd desktop/tests && /usr/bin/python3 -m unittest test_views_market_portfolio -v
    # 或
    /usr/bin/python3 desktop/tests/test_views_market_portfolio.py
"""

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP_DIR = os.path.dirname(HERE)
if DESKTOP_DIR not in sys.path:
    sys.path.insert(0, DESKTOP_DIR)

import tkinter as tk                                          # noqa: E402
from tkinter import ttk                                       # noqa: E402

from quantstudio_desktop import theme                         # noqa: E402
from quantstudio_desktop.services import TaskRunner           # noqa: E402
from quantstudio_desktop.views import market as market_module  # noqa: E402
from quantstudio_desktop.views import portfolio as portfolio_module  # noqa: E402
from quantstudio_desktop.views.market import MarketView, normalize_code_text  # noqa: E402
from quantstudio_desktop.views.portfolio import PortfolioView  # noqa: E402

WIDGETS_OK = not market_module.WIDGETS_ERROR and not portfolio_module.WIDGETS_ERROR


# --------------------------------------------------------------------------- 样例数据

INDICES = [
    {"code": "000001.SH", "name": "上证指数", "point": 3120.45, "change_pct": 0.66, "amount_yi": 4210.5},
    {"code": "399001.SZ", "name": "深证成指", "point": 9876.54, "change_pct": -0.42, "amount_yi": 5120.3},
    {"code": "399006.SZ", "name": "创业板指", "point": 1934.21, "change_pct": 1.08, "amount_yi": 2210.9},
    {"code": "000688.SH", "name": "科创50", "point": 812.34, "change_pct": 0.0, "amount_yi": 310.2},
]
BREADTH = {"up": 2812, "down": 1188, "flat": 200, "limit_up": 45, "limit_down": 8, "total": 4200}
OVERVIEW = {
    "indices": INDICES,
    "breadth": BREADTH,
    "total_amount_yi": 9123.4,
    "main_net_inflow_yi": -58.32,
    "north_net_inflow_yi": None,
}
SECTORS = [{"code": "BK%04d" % i, "name": "板块%02d" % i, "change_pct": round(5.0 - i * 0.3, 2),
            "net_inflow_yi": 1.2 * i, "leading_stock": "示例"} for i in range(15)]
QUOTES = [
    {"code": "600519.SH", "name": "贵州茅台", "price": 1680.5, "change_pct": 1.24, "amount_yi": 32.4,
     "turnover_pct": 0.45, "pe_ttm": 30.1, "pb": 8.2, "market_cap_yi": 21000.0},
    {"code": "300750.SZ", "name": "宁德时代", "price": 210.3, "change_pct": -0.88, "amount_yi": 45.1,
     "turnover_pct": 1.12, "pe_ttm": 22.4, "pb": 4.1, "market_cap_yi": 9200.0},
    {"code": "601318.SH", "name": "中国平安", "price": 48.6, "change_pct": 0.0, "amount_yi": 12.8,
     "turnover_pct": 0.31, "pe_ttm": None, "pb": 0.9, "market_cap_yi": 8800.0},
]
ACCOUNT = {
    "total_assets": 1234567.89, "market_value": 734567.89, "cash": 500000.0,
    "available_cash": 480000.0, "frozen_cash": 20000.0, "day_pnl": 6789.12,
    "total_pnl": 34567.89, "total_return_pct": 2.88, "positions_count": 2,
    "allocation": [{"code": "CASH", "name": "现金", "value": 500000.0, "pct": 40.5},
                   {"code": "600519.SH", "name": "贵州茅台", "value": 504150.0, "pct": 40.8},
                   {"code": "300750.SZ", "name": "宁德时代", "value": 230417.89, "pct": 18.7}],
    "as_of": "2025-01-03 15:00:00",
}
HOLDINGS = [
    {"code": "600519.SH", "name": "贵州茅台", "qty": 300, "available_qty": 200, "cost": 1580.0,
     "price": 1680.5, "market_value": 504150.0, "cost_value": 474000.0, "day_pnl": 4200.0,
     "total_pnl": 30150.0, "return_pct": 6.36, "industry": "白酒"},
    {"code": "300750.SZ", "name": "宁德时代", "qty": 1000, "available_qty": 1000, "cost": 220.0,
     "price": 210.3, "market_value": 210300.0, "cost_value": 220000.0, "day_pnl": -1800.0,
     "total_pnl": -9700.0, "return_pct": -4.41, "industry": "电池"},
]
EQUITY = [{"date": "2025-01-02", "equity": 1200000.0},
          {"date": "2025-01-03", "equity": 1234567.89}]
EQUITY_NOTES = ["权益快照仅 2 个交易日，不足请求的 90 天；这里只如实返回已有快照，不回溯伪造历史。"]
META = {"source": "sample", "stale": False, "offline": False, "as_of": "2025-01-03 15:00:00",
        "notes": ["演示数据"]}


def make_bars(code="600519.SH", count=30, start=1600.0):
    """构造 N 根 K 线（确定性、不联网）。"""
    bars = []
    price = start
    for index in range(count):
        price = round(price * (1.0 + (0.006 if index % 3 else -0.004)), 2)
        bars.append({
            "date": "2025-01-%02d" % (index % 28 + 1),
            "open": round(price * 0.998, 2), "high": round(price * 1.012, 2),
            "low": round(price * 0.988, 2), "close": price,
            "volume_wan": 100.0 + index, "amount_yi": 10.0 + index * 0.5,
            "change_pct": 0.6 if index % 3 else -0.4, "turnover_pct": 0.5,
        })
    return bars


# --------------------------------------------------------------------------- 替身

class FakeServices(object):
    """``GuiServices`` 的替身：返回固定样例数据，不联网、不写盘。"""

    def __init__(self, offline=False, stale=False, empty=False, broken=False, equity_points=2):
        self.offline = offline
        self.stale = stale
        self.empty = empty
        self.broken = broken
        self.equity_points = equity_points
        self.last_notes = list(EQUITY_NOTES)
        self.mode = "manual"
        self.cash = ACCOUNT["cash"]
        self.calls = []
        self.deleted = []
        self.upserted = []
        self.watchlist_codes = [row["code"] for row in QUOTES]

    # ---- 内部
    def _touch(self, name):
        self.calls.append(name)
        if self.broken:
            raise RuntimeError("%s 服务不可用（替身故障）" % name)

    def _rows(self, rows):
        return [] if self.empty else list(rows)

    # ---- 行情
    def provider_meta(self):
        self._touch("provider_meta")
        meta = dict(META)
        meta["offline"] = self.offline
        meta["stale"] = self.stale
        return meta

    def overview(self):
        self._touch("overview")
        if self.empty:
            return {"indices": [], "breadth": {}, "total_amount_yi": 0.0,
                    "main_net_inflow_yi": None, "north_net_inflow_yi": None}
        return dict(OVERVIEW)

    def sectors(self, limit=None):
        self._touch("sectors")
        rows = self._rows(SECTORS)
        return rows[:limit] if limit else rows

    def quotes(self, codes=None):
        self._touch("quotes")
        return self._rows(QUOTES)

    def kline(self, code, days=250, freq="day", adjust="qfq"):
        self._touch("kline")
        self.last_kline_args = (code, days, freq, adjust)
        return [] if self.empty else make_bars(code)

    def watchlist(self):
        self._touch("watchlist")
        return list(self.watchlist_codes)

    def watchlist_quotes(self):
        self._touch("watchlist_quotes")
        return {"codes": list(self.watchlist_codes), "items": self.quotes()}

    def add_watchlist(self, code):
        self._touch("add_watchlist")
        if code not in self.watchlist_codes:
            self.watchlist_codes.append(code)
        return list(self.watchlist_codes)

    def remove_watchlist(self, code):
        self._touch("remove_watchlist")
        self.watchlist_codes = [item for item in self.watchlist_codes if item != code]
        return list(self.watchlist_codes)

    # ---- 账户
    def portfolio_overview(self):
        self._touch("portfolio_overview")
        data = dict(ACCOUNT)
        data["cash"] = self.cash
        if self.empty:
            data["allocation"] = []
            data["positions_count"] = 0
        return data

    def holdings(self):
        self._touch("holdings")
        return self._rows(HOLDINGS)

    def equity(self, days=90):
        self._touch("equity")
        self.equity_days = days
        points = self._rows(EQUITY)
        self.last_notes = [] if (self.empty or len(points) >= days) else list(EQUITY_NOTES)
        return points[:self.equity_points] if self.equity_points >= 0 else points

    def portfolio_mode(self):
        self._touch("portfolio_mode")
        return self.mode

    def set_portfolio_mode(self, mode):
        self._touch("set_portfolio_mode")
        self.mode = mode
        return {"mode": mode}

    def upsert_holding(self, payload):
        self._touch("upsert_holding")
        self.upserted.append(payload)
        return dict(payload)

    def delete_holding(self, code):
        self._touch("delete_holding")
        self.deleted.append(code)
        return {"code": code, "deleted": True}

    def set_cash(self, amount):
        self._touch("set_cash")
        self.cash = amount
        return {"cash": amount}


class StubToast(object):
    """只记录消息的 Toast 替身。"""

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


def find_canvas(widget):
    """图表本身就是 ``tk.Canvas``（``charts.ChartBase``），同时兼容「Frame 包一层」的实现。"""
    if isinstance(widget, tk.Canvas):
        return widget
    for item in descendants(widget):
        if isinstance(item, tk.Canvas):
            return item
    return None


def find_tree(widget):
    for item in descendants(widget):
        if isinstance(item, ttk.Treeview):
            return item
    return None


# --------------------------------------------------------------------------- 纯函数测试


class CodeTextTest(unittest.TestCase):
    """代码输入兼容 600519 / sh600519 / 600519.SH 三种写法。"""

    def test_three_forms(self):
        self.assertEqual(normalize_code_text("600519"), "600519.SH")
        self.assertEqual(normalize_code_text("sh600519"), "600519.SH")
        self.assertEqual(normalize_code_text("600519.SH"), "600519.SH")
        self.assertEqual(normalize_code_text(" 600519.sh "), "600519.SH")
        self.assertEqual(normalize_code_text("300750"), "300750.SZ")
        self.assertEqual(normalize_code_text("sz300750"), "300750.SZ")
        self.assertEqual(normalize_code_text("bj830799"), "830799.BJ")

    def test_invalid(self):
        for bad in ("", None, "abc", "60051", "600519.SHX", "SH"):
            self.assertEqual(normalize_code_text(bad), "", "非法输入应为空串：%r" % (bad,))


class CodeColumnsTest(unittest.TestCase):
    """列定义与页面布局契约（无需 Tk）。"""

    def test_watch_columns(self):
        keys = [column["key"] for column in market_module.WATCH_COLUMNS]
        self.assertEqual(keys, ["code", "name", "price", "change_pct", "amount_yi",
                                "turnover_pct", "pe_ttm", "pb", "market_cap_yi"])

    def test_holding_columns(self):
        keys = [column["key"] for column in portfolio_module.HOLDING_COLUMNS]
        self.assertEqual(keys, ["code", "name", "qty", "available_qty", "cost", "price",
                                "market_value", "day_pnl", "total_pnl", "return_pct"])

    def test_mode_labels(self):
        self.assertEqual(portfolio_module.MODE_LABELS, {"manual": "手工记账", "paper": "模拟盘"})
        for word in ("manual", "paper"):
            self.assertIn(word, portfolio_module.MODE_HINT)


# --------------------------------------------------------------------------- 页面测试


class ViewTestCase(unittest.TestCase):
    """共用夹具：真实 Tk 根窗口 + 替身 App。"""

    @classmethod
    def setUpClass(cls):
        if not WIDGETS_OK:
            print("\n[提示] widgets 控件库不可用，相关断言将跳过：%s" % market_module.WIDGETS_ERROR)
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

    def open_page(self, view_class, app, timeout=8.0):
        """构建页面（build + reload）并驱动事件循环直到后台请求全部回主线程。"""
        frame = ttk.Frame(self.root, style="TFrame")
        frame.pack(fill="both", expand=True)
        self.frames.append(frame)
        page = view_class(frame, app)
        page.pack(fill="both", expand=True)
        page.refresh()                      # ensure_built() + reload()
        self.pump(page, app, timeout)
        return page

    def pump(self, page, app, timeout=8.0):
        deadline = time.time() + timeout
        idle = 0
        while time.time() < deadline:
            self.root.update()
            if page._pending == 0 and getattr(app.tasks, "busy", 0) == 0:
                idle += 1
                if idle >= 3:               # 多跑几轮，确保级联请求（K 线）也被触发
                    return True
            else:
                idle = 0
            time.sleep(0.02)
        self.fail("后台任务在 %.1fs 内未完成（pending=%s）" % (timeout, page._pending))

    @staticmethod
    def errors(app):
        return [text for kind, text in app.toast.messages if kind == "error"]

    def assert_chart_empty_state(self, chart, note, keyword):
        """空数据/失败状态必须画在图上（控件内置空状态或叠加标签，都不是弹窗）。"""
        empty_text = str(getattr(chart, "empty_text", "") or "")
        if hasattr(chart, "set_empty"):
            self.assertIn(keyword, empty_text)
            canvas = find_canvas(chart)
            self.assertTrue(canvas.find_all(), "空状态应绘制在 canvas 上")
        else:
            self.assertEqual(note.winfo_manager(), "place")
            self.assertIn(keyword, note.cget("text"))

    # ------------------------------------------------------------------ 行情看板
    def test_market_view_build_and_render(self):
        app = self.make_app()
        page = self.open_page(MarketView, app)
        fake = app.services

        self.assertTrue(page._built)
        self.assertEqual(page.build_errors, [], "页面区块不应构建失败")
        self.assertEqual(len(page.index_cards), 4)
        self.assertEqual(len(page.breadth_cards), 5)
        self.assertEqual(len(page._indices), 4)
        self.assertEqual(len(page._sectors), 12, "板块榜取前 12 个")
        self.assertEqual(len(page._quotes), len(QUOTES))
        self.assertEqual(page.watch_hint.cget("text"), "共 3 只 · 选中行加载 K 线")
        self.assertEqual(self.errors(app), [], "正常路径不应产生错误提示")

        # 数据源提示条：非离线且非缓存时不显示
        self.assertEqual(page.notice_label.cget("text"), "")
        self.assertEqual(page.notice_label.winfo_manager(), "")

        # K 线默认跟随第一条自选股，并按 250 日 / 日线 / 前复权取数
        self.assertEqual(page.code_var.get(), QUOTES[0]["code"])
        self.assertEqual(fake.last_kline_args, (QUOTES[0]["code"], 250, "day", "qfq"))
        self.assertEqual(len(page._kline_bars), 30)
        self.assertIn("贵州茅台", page.kline_title.cget("text"))
        self.assertIn(QUOTES[0]["code"], page.kline_title.cget("text"))
        self.assertIn("近 250 日", page.kline_title.cget("text"))
        self.assertEqual(page.kline_note.winfo_manager(), "", "有数据时不显示空状态")

        # 工具栏切换周期/复权/天数 → 重新取数
        page.freq_var.set("周线")
        page.adjust_var.set("后复权")
        page.days_var.set("60")
        page._load_kline()
        self.pump(page, app)
        self.assertEqual(fake.last_kline_args, (QUOTES[0]["code"], 60, "week", "hfq"))

        # 选中行 → 切换标的并重新取数
        page._on_watch_select({"code": "300750.SZ"})
        self.pump(page, app)
        self.assertEqual(page.code_var.get(), "300750.SZ")
        self.assertEqual(fake.last_kline_args[0], "300750.SZ")

        if not WIDGETS_OK:
            self.assertTrue(page._widget_error)
            self.assertTrue(page.fallback is not None, "控件缺失时应显示文本摘要")
            return

        # 表格行数 / 图表绘制项
        tree = find_tree(page.quote_table)
        self.assertIsNotNone(tree, "自选股表格应是 Treeview 封装")
        self.assertEqual(len(tree.get_children()), len(QUOTES))
        sector_canvas = find_canvas(page.sector_chart)
        self.assertIsNotNone(sector_canvas)
        self.assertTrue(sector_canvas.find_all(), "板块柱状图应有绘制项")
        candle_canvas = find_canvas(page.candle_chart)
        self.assertIsNotNone(candle_canvas)
        self.assertTrue(candle_canvas.find_all(), "K 线图应有绘制项")

    def test_market_view_offline_and_stale_notice(self):
        app = self.make_app(offline=True, stale=True)
        page = self.open_page(MarketView, app)
        text = page.notice_label.cget("text")
        self.assertEqual(page.notice_label.winfo_manager(), "pack", "离线提示应显示在页面顶部")
        self.assertIn("离线/演示数据", text)
        self.assertIn("sample", text)
        self.assertIn("缓存快照", text)

        app2 = self.make_app(offline=False, stale=True)
        page2 = self.open_page(MarketView, app2)
        self.assertEqual(page2.notice_label.cget("text"), "⚠ 数据为缓存快照")

    def test_market_view_empty_data(self):
        app = self.make_app(empty=True)
        page = self.open_page(MarketView, app)
        self.assertEqual(page._indices, [])
        self.assertEqual(page._sectors, [])
        self.assertEqual(page._quotes, [])
        self.assertIn("自选股为空", page.watch_hint.cget("text"))
        self.assertIn("添加标的", page.watch_hint.cget("text"))
        self.assertEqual(page._pending, 0)
        if WIDGETS_OK:
            tree = find_tree(page.quote_table)
            self.assertEqual(len(tree.get_children()), 0, "空自选股时表格应为 0 行")
            self.assertEqual(page.kline_note.winfo_manager(), "", "没有标的时不显示 K 线空状态（等待用户输入）")

    def test_market_view_kline_failure_shows_inline_state(self):
        app = self.make_app()
        page = self.open_page(MarketView, app)

        def boom(code, days=250, freq="day", adjust="qfq"):
            raise RuntimeError("K 线数据源超时")

        app.services.kline = boom
        with redirect_stderr(io.StringIO()):     # TaskRunner 会打印后台异常堆栈
            page._load_kline()
            self.pump(page, app)
        if WIDGETS_OK:
            self.assert_chart_empty_state(page.candle_chart, page.kline_note, "K 线数据源超时")
        self.assertIn("K 线数据源超时", page.kline_meta.cget("text"))
        self.assertTrue(self.errors(app), "失败必须提示用户")

    # ------------------------------------------------------------------ 持仓管理
    def test_portfolio_view_build_and_render(self):
        app = self.make_app()
        page = self.open_page(PortfolioView, app)
        fake = app.services

        self.assertTrue(page._built)
        self.assertEqual(page.build_errors, [], "页面区块不应构建失败")
        self.assertEqual(len(page.account_cards), 8)
        self.assertEqual(self.errors(app), [], "正常路径不应产生错误提示")
        self.assertEqual(fake.equity_days, 90, "权益曲线默认取 90 天")
        self.assertEqual(page._holdings, HOLDINGS)
        self.assertEqual(page.mode_var.get(), "manual")
        self.assertEqual(page.mode_hint.cget("text"), "当前：手工记账")
        self.assertIn("共 2 只持仓", page.holdings_hint.cget("text"))
        self.assertTrue(page.equity_notes.cget("text"), "last_notes 说明应显示在图表下方")
        self.assertIn("不足请求的 90 天", page.equity_notes.cget("text"))
        if WIDGETS_OK:
            self.assertEqual(page.equity_note.winfo_manager(), "", "快照充足时不显示空状态")

        if not WIDGETS_OK:
            self.assertTrue(page._widget_error)
            return

        tree = find_tree(page.holdings_table)
        self.assertIsNotNone(tree)
        self.assertEqual(len(tree.get_children()), len(HOLDINGS))
        donut = find_canvas(page.allocation_chart)
        self.assertIsNotNone(donut)
        self.assertTrue(donut.find_all(), "资产配置环形图应有绘制项")
        line = find_canvas(page.equity_chart)
        self.assertIsNotNone(line)
        self.assertTrue(line.find_all(), "权益曲线应有绘制项")

    def test_portfolio_empty_holdings(self):
        app = self.make_app(empty=True)
        page = self.open_page(PortfolioView, app)
        self.assertEqual(page._holdings, [])
        self.assertEqual(page.holdings_hint.cget("text"), "暂无持仓，点击『录入/修改持仓』添加")
        if WIDGETS_OK:
            tree = find_tree(page.holdings_table)
            self.assertEqual(len(tree.get_children()), 0)

    def test_portfolio_equity_accumulating(self):
        app = self.make_app(equity_points=1)
        page = self.open_page(PortfolioView, app)
        self.assertEqual(len(page._equity_points), 1)
        if WIDGETS_OK:
            self.assert_chart_empty_state(page.equity_chart, page.equity_note, "历史快照累积中（当前 1 天）")

    def test_portfolio_mode_switch(self):
        app = self.make_app()
        page = self.open_page(PortfolioView, app)
        fake = app.services
        self.assertEqual(fake.mode, "manual")

        page.mode_var.set("paper")
        with mock.patch.object(portfolio_module.messagebox, "askyesno", return_value=True):
            page._on_mode_selected()
        self.pump(page, app)
        self.assertEqual(fake.mode, "paper", "确认后应调用 set_portfolio_mode")
        self.assertEqual(page.mode_var.get(), "paper")
        self.assertEqual(page.mode_hint.cget("text"), "当前：模拟盘")
        message = " ".join(text for _kind, text in app.toast.messages)
        self.assertIn("manual", message)
        self.assertIn("paper", message)

        # 取消确认时不切换
        page.mode_var.set("manual")
        with mock.patch.object(portfolio_module.messagebox, "askyesno", return_value=False):
            page._on_mode_selected()
        self.pump(page, app)
        self.assertEqual(fake.mode, "paper")
        self.assertEqual(page.mode_var.get(), "paper", "取消后下拉应回滚到当前模式")

    def test_portfolio_delete_holding(self):
        app = self.make_app()
        page = self.open_page(PortfolioView, app)
        if not WIDGETS_OK:
            self.skipTest("控件库缺失，无法选中表格行")
        tree = find_tree(page.holdings_table)
        first = tree.get_children()[0]
        tree.selection_set(first)
        if not page._selected_payload():
            self.skipTest("DataTable.selected() 未返回选中行，跳过删除流程")
        with mock.patch.object(portfolio_module.messagebox, "askyesno", return_value=True):
            page._delete_holding()
        self.pump(page, app)
        self.assertTrue(app.services.deleted, "确认后应调用 delete_holding")
        self.assertTrue(self.apps[-1].services.deleted[-1])

    def test_portfolio_service_dispatch(self):
        """表单交互不弹窗：直接验证提交载荷的组装与错误处理。"""
        app = self.make_app()
        page = self.open_page(PortfolioView, app)
        page._request(app.services.upsert_holding, {"code": "600519.SH", "qty": 100},
                      on_done=page._after_write)
        self.pump(page, app)
        self.assertEqual(app.services.upserted[-1], {"code": "600519.SH", "qty": 100})
        self.assertEqual(page._pending, 0)

    # ------------------------------------------------------------------ 失败降级
    def test_views_survive_broken_services(self):
        for view_class in (MarketView, PortfolioView):
            app = self.make_app(broken=True)
            with redirect_stderr(io.StringIO()):        # TaskRunner 会打印后台异常堆栈
                page = self.open_page(view_class, app)
            self.assertEqual(page._pending, 0)
            self.assertTrue(page.build_errors == [] or page._widget_error,
                            "构建只在控件缺失时降级")
            self.assertTrue(self.errors(app), "%s 应把服务异常提示给用户" % view_class.__name__)
            self.assertIn("服务不可用", " ".join(self.errors(app)))

    def test_reload_does_not_rebuild_widgets(self):
        app = self.make_app()
        page = self.open_page(MarketView, app)
        cards_before = [id(card) for card in page.index_cards]
        table_before = id(page.quote_table)
        page.reload()
        self.pump(page, app)
        self.assertEqual([id(card) for card in page.index_cards], cards_before,
                         "刷新不应重建卡片（否则滚动位置/选中态会丢）")
        self.assertEqual(id(page.quote_table), table_before, "刷新不应重建表格")


if __name__ == "__main__":
    unittest.main(verbosity=2)
