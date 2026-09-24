# -*- coding: utf-8 -*-
"""策略管理 / 回测分析 / 交易（模拟盘）三个页面的界面级联调测试。

- 用替身服务对象（固定样例数据、不联网）替换 ``app.services``；
- 用同步的任务执行器替换 ``app.tasks``，避免测试依赖事件循环；
- ``Tk()`` 初始化失败（无显示器）时整体 skip。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                                             # noqa: E402
from tkinter import ttk                                          # noqa: E402

from quantstudio_desktop import theme                            # noqa: E402
from quantstudio_desktop.widgets import cards as cards_mod       # noqa: E402
from quantstudio_desktop.widgets import charts as charts_mod     # noqa: E402
from quantstudio_desktop.widgets import table as table_mod       # noqa: E402


# --------------------------------------------------------------------------- 样例数据
def _strategy(strategy_id, name, origin, params, schema, category="趋势跟踪"):
    return {
        "id": strategy_id, "name": name, "category": category,
        "desc": "%s 的说明文本" % name, "params": params, "param_schema": schema,
        "status": "running" if origin == "user" else "paused", "universe": "自选股",
        "universe_type": "multi", "freq": "日线", "builtin": origin == "builtin",
        "origin": origin, "min_bars": 61, "version": "1.0",
    }


SCHEMA = {
    "short_ma": {"label": "短期均线", "type": "int", "default": 20, "min": 2, "max": 250,
                 "help": "短周期均线天数"},
    "long_ma": {"label": "长期均线", "type": "int", "default": 60, "min": 3, "max": 500,
                "help": "长周期均线天数"},
}
STRATEGIES = [
    _strategy("st_ma_cross", "双均线趋势策略", "builtin", {"short_ma": 20, "long_ma": 60}, SCHEMA),
    _strategy("local_grid", "本地网格策略", "local", {"short_ma": 5, "long_ma": 20}, SCHEMA,
              category="震荡市"),
    _strategy("us_demo01", "我的双均线", "user", {"short_ma": 10, "long_ma": 30}, SCHEMA,
              category="自定义"),
]

NAV_DATES = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
METRICS = {
    "total_return_pct": 12.34, "annual_return_pct": 8.56, "max_drawdown_pct": -6.78,
    "max_drawdown_start": "2024-01-03", "max_drawdown_end": "2024-01-05", "sharpe": 1.23,
    "sortino": 1.45, "calmar": 0.98, "volatility_pct": 15.2, "win_rate_pct": 55.5,
    "trade_count": 4, "profit_loss_ratio": 1.8, "turnover_pct": 120.5,
    "benchmark_return_pct": 3.21, "alpha_pct": 5.0, "beta": 0.85, "total_fee": 123.45,
    "start": "2024-01-02", "end": "2024-01-08", "trading_days": 5,
}
BACKTEST_RESULT = {
    "strategy": {"id": "st_ma_cross", "name": "双均线趋势策略"},
    "request": {"strategy_id": "st_ma_cross", "symbols": ["600519.SH"], "start": "2024-01-01",
                "end": "", "initial_cash": 1000000.0, "benchmark": "000300.SH",
                "fee": {"commission_rate": 0.00025, "slippage_bps": 2.0}},
    "range": {"start": "2024-01-02", "end": "2024-01-08"},
    "nav": [{"date": date, "strategy": 1.0 + 0.01 * index, "benchmark": 1.0 + 0.004 * index}
            for index, date in enumerate(NAV_DATES)],
    "drawdown": [{"date": date, "dd_pct": -1.5 * index} for index, date in enumerate(NAV_DATES)],
    "monthly": [{"month": "2024-01", "ret_pct": 1.25}, {"month": "2024-02", "ret_pct": -0.5}],
    "metrics": dict(METRICS),
    "trades": [
        {"date": "2024-01-03", "code": "600519.SH", "name": "贵州茅台", "side": "buy",
         "price": 1650.0, "qty": 100, "amount": 165000.0, "fee": 16.5, "pnl": None,
         "ret_pct": None, "reason": "MA20 上穿 MA60"},
        {"date": "2024-01-08", "code": "600519.SH", "name": "贵州茅台", "side": "sell",
         "price": 1700.0, "qty": 100, "amount": 170000.0, "fee": 101.5, "pnl": 4882.0,
         "ret_pct": 2.96, "reason": "MA20 下穿 MA60"},
    ],
    "trade_total": 4,
    "equity": [{"date": date, "equity": 1000000.0 + 1000 * index}
               for index, date in enumerate(NAV_DATES)],
    "positions": [{"code": "600519.SH", "name": "贵州茅台", "qty": 100, "available_qty": 0,
                   "cost": 1650.16, "price": 1700.0, "market_value": 170000.0,
                   "total_pnl": 4984.0, "return_pct": 3.02}],
    "warnings": ["回测引擎固定使用前复权（qfq）日线"],
}

ACCOUNT = {
    "total_assets": 1012345.67, "market_value": 170000.0, "cash": 842345.67,
    "available_cash": 842345.67, "frozen_cash": 0.0, "day_pnl": 1234.56,
    "total_pnl": 12345.67, "total_return_pct": 1.23, "positions_count": 1,
    "allocation": [], "as_of": "2024-09-20 15:00:00",
}
HOLDINGS = [{"code": "600519.SH", "name": "贵州茅台", "qty": 100, "available_qty": 100,
             "cost": 1650.16, "price": 1700.0, "market_value": 170000.0, "cost_value": 165016.0,
             "day_pnl": 500.0, "total_pnl": 4984.0, "return_pct": 3.02, "industry": "白酒"}]
ORDERS = [
    {"order_id": "PB0001", "code": "600519.SH", "name": "贵州茅台", "side": "buy", "qty": 100,
     "price": None, "filled_qty": 100, "avg_price": 1650.0, "fee": 16.5, "status": "filled",
     "reason": "desktop-manual", "created_at": "2024-09-20 10:00:00",
     "updated_at": "2024-09-20 10:00:00"},
    {"order_id": "PB0002", "code": "000858.SZ", "name": "五粮液", "side": "buy", "qty": 100,
     "price": 120.0, "filled_qty": 0, "avg_price": 0.0, "fee": 0.0, "status": "new",
     "reason": "限价 120.00 低于最新价 130.00，挂单等待", "created_at": "2024-09-20 11:00:00",
     "updated_at": "2024-09-20 11:00:00"},
]
FILLS = [{"order_id": "PB0001", "code": "600519.SH", "name": "贵州茅台", "side": "buy",
          "price": 1650.0, "qty": 100, "amount": 165000.0, "fee": 16.5,
          "ts": "2024-09-20 10:00:00", "reason": "desktop-manual"}]


# --------------------------------------------------------------------------- 替身
class ImmediateTasks(object):
    """同步任务执行器：立即调用 ``fn`` 并把结果交给回调（主线程内）。"""

    def __init__(self):
        self.names = []
        self.busy = 0

    def run(self, fn, *args, on_done=None, on_error=None, name="", **kwargs):
        self.names.append(name or getattr(fn, "__name__", "task"))
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:                  # noqa: BLE001 - 与 TaskRunner 行为一致
            if on_error is not None:
                on_error(exc)
            else:
                raise
        else:
            if on_done is not None:
                on_done(result)
        return len(self.names)

    def shutdown(self):
        pass


class FakeToast(object):
    def __init__(self):
        self.messages = []

    def show(self, text, kind="info", timeout=None):
        self.messages.append((str(text), str(kind)))

    def texts(self):
        return [item[0] for item in self.messages]

    def kinds(self):
        return [item[1] for item in self.messages]


class FakeServices(object):
    """固定样例数据的服务替身（不联网、不落盘）。"""

    def __init__(self):
        self.calls = []
        self.deleted = []
        self.created = []
        self.submitted = []
        self.cancelled = []
        self.resets = 0
        self.backtests = []
        self.orders_data = [dict(row) for row in ORDERS]
        self.fills_data = [dict(row) for row in FILLS]

    # 策略
    def strategies(self):
        self.calls.append("strategies")
        return [dict(item) for item in STRATEGIES]

    def strategy(self, strategy_id):
        self.calls.append("strategy")
        for item in STRATEGIES:
            if item["id"] == strategy_id:
                return dict(item)
        raise KeyError("策略不存在：%s" % strategy_id)

    def create_strategy(self, payload):
        self.calls.append("create_strategy")
        self.created.append(dict(payload))
        item = _strategy("us_new01", payload.get("name") or "新策略", "user",
                         payload.get("params") or {}, SCHEMA)
        return item

    def delete_strategy(self, strategy_id):
        self.calls.append("delete_strategy")
        self.deleted.append(strategy_id)
        return {"id": strategy_id, "deleted": True}

    # 回测
    def run_backtest(self, strategy_id, params=None):
        self.calls.append("run_backtest")
        self.backtests.append((strategy_id, dict(params or {})))
        result = dict(BACKTEST_RESULT)
        result["metrics"] = dict(METRICS)
        result["request"] = dict(BACKTEST_RESULT["request"])
        result["request"]["symbols"] = list((params or {}).get("symbols") or ["600519.SH"])
        result["request"]["initial_cash"] = float((params or {}).get("cash") or 1000000.0)
        return result

    # 行情/自选
    def watchlist(self):
        self.calls.append("watchlist")
        return ["600519.SH", "000858.SZ"]

    # 账户与交易
    def portfolio_overview(self):
        self.calls.append("portfolio_overview")
        return dict(ACCOUNT)

    def portfolio_mode(self):
        return "paper"

    def holdings(self):
        self.calls.append("holdings")
        return [dict(row) for row in HOLDINGS]

    def orders(self, limit=100):
        self.calls.append("orders")
        return [dict(row) for row in self.orders_data]

    def fills(self, limit=100):
        self.calls.append("fills")
        return [dict(row) for row in self.fills_data]

    def submit_order(self, payload):
        self.calls.append("submit_order")
        self.submitted.append(dict(payload))
        order = {"order_id": "PB0003", "code": payload.get("code"), "name": "贵州茅台",
                 "side": payload.get("side"), "qty": payload.get("qty"),
                 "price": payload.get("price"), "filled_qty": payload.get("qty"),
                 "avg_price": 1660.0, "fee": 16.6, "status": "filled", "reason": "manual"}
        fill = {"order_id": "PB0003", "code": payload.get("code"), "side": payload.get("side"),
                "price": 1660.0, "qty": payload.get("qty"), "amount": 166000.0, "fee": 16.6,
                "ts": "2024-09-20 14:00:00"}
        return {"order": order, "fill": fill}

    def cancel_order(self, order_id):
        self.calls.append("cancel_order")
        self.cancelled.append(order_id)
        return {"order_id": order_id, "code": "000858.SZ", "status": "cancelled"}

    def reset_paper(self, initial_cash=None):
        self.calls.append("reset_paper")
        self.resets += 1
        return dict(ACCOUNT)


class FakeApp(object):
    def __init__(self, root, services):
        self.root = root
        self.services = services
        self.tasks = ImmediateTasks()
        self.toast = FakeToast()
        self.status_text = ""
        self.selected_view = ""

    def status(self, text):
        self.status_text = str(text)

    def select_view(self, key):
        self.selected_view = key

    def refresh_current(self):
        pass


# --------------------------------------------------------------------------- 工具
def walk(widget):
    yield widget
    for child in widget.winfo_children():
        for item in walk(child):
            yield item


def tree_values(data_table):
    tree = data_table.tree
    return [tree.item(iid, "values") for iid in tree.get_children()]


def column_index(data_table, key):
    return list(data_table.tree["columns"]).index(key)


def count_widgets(view, cls):
    return len([widget for widget in walk(view) if isinstance(widget, cls)])


class ViewTestCase(unittest.TestCase):
    """三个页面共用一套 Tk 根窗口。"""

    root = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except Exception as exc:                      # noqa: BLE001 - 无显示器环境
            raise unittest.SkipTest("Tk 不可用：%s" % exc)
        cls.root.withdraw()
        theme.init(cls.root)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:                         # noqa: BLE001
                pass

    def setUp(self):
        self.services = FakeServices()
        self.app = FakeApp(self.root, self.services)
        self.views = []

    def tearDown(self):
        for view in self.views:
            try:
                view.destroy()
            except Exception:                         # noqa: BLE001
                pass
        try:
            self.root.update_idletasks()
        except Exception:                             # noqa: BLE001
            pass

    def make_view(self, factory):
        view = factory(self.root, self.app)
        view.pack(fill="both", expand=True)
        self.views.append(view)
        view.refresh()
        self.root.update_idletasks()
        return view


# --------------------------------------------------------------------------- 策略管理
class StrategiesViewTest(ViewTestCase):

    def _view(self):
        from quantstudio_desktop.views.strategies import StrategiesView

        return self.make_view(StrategiesView)

    def test_table_rows_and_origin_labels(self):
        view = self._view()
        self.assertFalse(view._degraded, "策略页不应降级：%s" % view._missing)
        rows = tree_values(view._table)
        self.assertEqual(len(rows), len(STRATEGIES))
        index = column_index(view._table, "origin_text")
        self.assertEqual([row[index] for row in rows], ["内置", "本地代码", "自定义"])
        # 参数表随选中项渲染
        self.assertEqual(len(view._params.rows), len(SCHEMA))

    def test_selection_is_kept_after_reload(self):
        view = self._view()
        view._select_row(view._display_rows[2])                # 选中「自定义」策略
        self.assertEqual(view._selected_id, "us_demo01")
        self.assertFalse(view._delete_btn.instate(["disabled"]), "自定义策略应可删除")
        view.reload()
        self.assertEqual(view._selected_id, "us_demo01")
        self.assertEqual(view._detail_title.get().split("（")[0], "我的双均线")
        self.assertEqual(view._table.selected()["id"], "us_demo01")

    def test_delete_button_disabled_for_builtin(self):
        view = self._view()
        view._select_row(view._display_rows[0])
        self.assertEqual(view._selected_id, "st_ma_cross")
        self.assertTrue(view._delete_btn.instate(["disabled"]), "内置策略不可删除")

    def test_open_backtest_sets_pending_flag(self):
        view = self._view()
        view._select_row(view._display_rows[0])
        view._open_backtest()
        self.assertEqual(getattr(self.app, "pending_backtest_strategy", None), "st_ma_cross")
        self.assertEqual(self.app.selected_view, "backtest")

    def test_filter_applies(self):
        view = self._view()
        view._filter.set("自定义")
        view._apply_filter()
        self.assertEqual(len(tree_values(view._table)), 1)
        view._filter.set("代码策略")
        view._apply_filter()
        self.assertEqual(len(tree_values(view._table)), 2)
        view._filter.set("全部")
        view._apply_filter()
        self.assertEqual(len(tree_values(view._table)), 3)

    def test_create_strategy_payload(self):
        view = self._view()
        payload = view._payload_from_form({
            "name": "我的新策略", "template": "st_ma_cross · 双均线趋势策略", "category": "自定义",
            "status": "paused", "freq": "日线", "universe": "自选股", "desc": "试验",
            "params": '{"short_ma": 8, "long_ma": 21}',
        })
        self.assertEqual(payload["template"], "st_ma_cross")
        self.assertEqual(payload["params"], {"short_ma": 8, "long_ma": 21})
        self.assertIsNone(view._payload_from_form({"name": "x", "params": "{}"}))
        self.assertIsNone(view._payload_from_form({"name": "合法名称", "template": "t",
                                                   "params": "{不是 JSON}"}))
        view._on_created({"id": "us_new01", "name": "我的新策略"})
        self.assertEqual(self.services.created, [])           # _on_created 只刷新，不直接调用服务
        self.assertTrue(any("策略已创建" in text for text in self.app.toast.texts()))


# --------------------------------------------------------------------------- 回测分析
class BacktestViewTest(ViewTestCase):

    def _view(self):
        from quantstudio_desktop.views.backtest import BacktestView

        return self.make_view(BacktestView)

    def test_layout_has_16_metric_cards_and_four_charts(self):
        view = self._view()
        self.assertFalse(view._degraded, "回测页不应降级：%s" % view._missing)
        self.assertEqual(count_widgets(view, cards_mod.StatCard), 16)
        self.assertEqual(len(view._charts), 4)
        chart_types = [charts_mod.MultiLineChart, charts_mod.LineChart, charts_mod.BarChart]
        self.assertEqual(count_widgets(view, chart_types[0]), 1)
        self.assertEqual(count_widgets(view, chart_types[1]), 2)
        self.assertEqual(count_widgets(view, chart_types[2]), 1)
        self.assertEqual(len(view._table.rows), len(STRATEGIES))
        self.assertEqual(len(view._symbol_vars), 2)
        self.assertTrue(all(var.get() for var in view._symbol_vars.values()), "标的默认全选")

    def test_params_payload_keys(self):
        view = self._view()
        view._end_var.set("")
        view._symbols_var.set("000001.SZ, 600000.sh")
        params, problem = view._collect_params()
        self.assertEqual(problem, "")
        self.assertEqual(sorted(params.keys()),
                         ["benchmark", "cash", "commission_rate", "end", "flow_fee", "slippage_bps",
                          "slippage_ticks", "start", "symbols"])
        self.assertEqual(params["cash"], 1000000.0)
        self.assertEqual(params["commission_rate"], 2.5 / 10000.0)
        self.assertEqual(params["slippage_bps"], 2.0)
        self.assertEqual(params["flow_fee"], 0.0)
        self.assertEqual(params["benchmark"], "000300.SH")
        self.assertEqual(params["end"], "")
        self.assertEqual(params["symbols"], ["600519.SH", "000858.SZ", "000001.SZ", "600000.SH"])
        # 非法输入本地拦截
        view._cash_var.set("abc")
        self.assertIsNone(view._collect_params()[0])
        view._cash_var.set("1000000")
        view._start_var.set("2024/01/01")
        self.assertIsNone(view._collect_params()[0])
        view._start_var.set("2024-01-01")
        view._end_var.set("2023-01-01")
        self.assertIn("晚于", view._collect_params()[1])

    def test_params_payload_new_fee_fields(self):
        """新增费用字段（流量费 / 跳数滑点）随 params 下发，非法输入本地拦截。"""
        view = self._view()
        view._flow_fee_var.set("1.5")
        view._ticks_var.set("3")
        params, problem = view._collect_params()
        self.assertEqual(problem, "")
        self.assertEqual(params["flow_fee"], 1.5)
        self.assertEqual(params["slippage_ticks"], 3.0)

        view._flow_fee_var.set("abc")
        params, problem = view._collect_params()
        self.assertIsNone(params)
        self.assertIn("流量费", problem)
        view._flow_fee_var.set("1.5")
        view._ticks_var.set("-2")
        params, problem = view._collect_params()
        self.assertIsNone(params)
        self.assertIn("滑点跳数", problem)

        # 恢复默认时两个字段归零
        view._flow_fee_var.set("9")
        view._ticks_var.set("9")
        view._restore_defaults()
        self.assertEqual(view._flow_fee_var.get(), "0")
        self.assertEqual(view._ticks_var.get(), "0")

    def test_pending_strategy_runs_backtest_and_renders(self):
        view = self._view()
        self.app.pending_backtest_strategy = "st_ma_cross"
        view.reload()                                          # 模拟从策略页跳转
        self.root.update_idletasks()
        self.assertIsNone(getattr(self.app, "pending_backtest_strategy", None))
        self.assertEqual(self.services.backtests[0][0], "st_ma_cross")
        self.assertEqual(view._selected_id, "st_ma_cross")
        # 指标卡已写入数值
        self.assertEqual(view._cards["total_return_pct"].raw_value, "+12.34%")
        self.assertEqual(view._cards["trade_count"].raw_value, "4")
        self.assertEqual(view._cards["trading_days"].raw_value, "5")
        self.assertIn("2024-01-03", view._cards["max_drawdown_pct"].sub_label["text"])
        # 交易记录 / 期末持仓
        self.assertEqual(len(view._trades.rows), len(BACKTEST_RESULT["trades"]))
        self.assertEqual(len(view._positions.rows), 1)
        self.assertEqual(view._trades.rows[0]["side_text"], "买入")
        self.assertEqual(view._trades.rows[1]["side_text"], "卖出")
        self.assertIn("回测引擎固定使用前复权", view._warn_var.get())
        self.assertIn("区间 2024-01-02", view._meta_var.get())
        self.assertNotEqual(view._hint_var.get(), "")
        self.assertEqual(len(view._charts["nav"]._series), 2)   # 策略 + 基准两条线
        self.assertEqual(view._charts["nav"]._series[0]["name"], "策略净值")

    def test_run_backtest_via_button(self):
        view = self._view()
        view._table.select_index(1)                            # 选中「本地网格策略」
        self.root.update_idletasks()
        self.assertEqual(view._selected_id, "local_grid")
        view._run_backtest()
        self.root.update_idletasks()
        self.assertEqual(self.services.backtests[0][0], "local_grid")
        self.assertIn("回测引擎固定使用前复权", view._warn_var.get())
        self.assertFalse(view._run_btn.instate(["disabled"]), "运行结束后按钮应恢复")

    def test_error_path_shows_hint_and_toast(self):
        view = self._view()

        def boom(strategy_id, params):
            raise RuntimeError("数据源不可用")

        self.services.run_backtest = boom                      # type: ignore[assignment]
        view._run_backtest()
        self.root.update_idletasks()
        self.assertIn("回测失败", view._hint_var.get())
        self.assertTrue(any("数据源不可用" in text for text in self.app.toast.texts()))


# --------------------------------------------------------------------------- 交易（模拟盘）
class TradeViewTest(ViewTestCase):

    def _view(self):
        from quantstudio_desktop.views.trade import TradeView

        return self.make_view(TradeView)

    def test_account_cards_and_three_tables(self):
        view = self._view()
        self.assertFalse(view._degraded, "交易页不应降级：%s" % view._missing)
        self.assertEqual(count_widgets(view, cards_mod.StatCard), 8)
        self.assertEqual(count_widgets(view, table_mod.DataTable), 3)
        self.assertEqual(view._cards["total_assets"].raw_value, theme.fmt_money(1012345.67))
        self.assertEqual(view._cards["mode"].raw_value, "模拟盘（paper）")
        self.assertEqual(len(view._holdings_table.rows), 1)
        self.assertEqual(len(view._orders_table.rows), 2)
        self.assertEqual(len(view._fills_table.rows), 1)
        self.assertEqual(view._orders_table.rows[0]["side_text"], "买入")
        self.assertEqual(view._orders_table.rows[1]["status_text"], "待成交")
        labels = [widget["text"] for widget in walk(view)
                  if isinstance(widget, tk.Label) and "text" in widget.keys()]
        self.assertTrue(any("不会发送任何真实委托" in str(text) for text in labels),
                        "缺少模拟盘提示条")

    def test_submit_order_payload_and_toast(self):
        view = self._view()
        view._code_var.set("600519.sh")
        view._side_var.set("买入")
        view._qty_var.set("100")
        view._price_var.set("")
        view._submit_order()
        self.root.update_idletasks()
        self.assertEqual(self.services.submitted[0],
                         {"code": "600519.SH", "side": "buy", "qty": 100,
                          "reason": "desktop-manual"})
        self.assertTrue(any("已成交" in text and "费用" in text for text in self.app.toast.texts()),
                        "提交成功后应 toast 成交结果")
        self.assertEqual(len(view._orders_table.rows), 2)       # 快照已刷新（替身数据不变）

    def test_submit_order_local_validation(self):
        view = self._view()
        view._code_var.set("")
        view._submit_order()
        self.assertEqual(self.services.submitted, [])
        view._code_var.set("600519.SH")
        view._qty_var.set("0")
        view._submit_order()
        self.assertEqual(self.services.submitted, [])
        view._qty_var.set("1.5")
        view._submit_order()
        self.assertEqual(self.services.submitted, [])
        view._qty_var.set("200")
        view._price_var.set("-3")
        view._submit_order()
        self.assertEqual(self.services.submitted, [])
        self.assertTrue(any("数量需为正整数" in text for text in self.app.toast.texts()))

    def test_cancel_order_flow(self):
        view = self._view()
        view._cancel_order()                                    # 未选中 → 仅提示
        self.assertEqual(self.services.cancelled, [])
        view._orders_table.select_index(1)                      # 选中的是「待成交」委托
        self.root.update_idletasks()
        self.assertFalse(view._cancel_btn.instate(["disabled"]), "待成交委托应可撤销")

    def test_reset_paper_requires_confirmation(self):
        view = self._view()
        original = __import__("tkinter.messagebox", fromlist=["askyesno"])
        import quantstudio_desktop.views.trade as trade_module

        saved = trade_module.messagebox.askyesno
        trade_module.messagebox.askyesno = lambda *args, **kwargs: False
        try:
            call = getattr(view, "_reset_paper")
            call()
            self.assertEqual(self.services.resets, 0)
            trade_module.messagebox.askyesno = lambda *args, **kwargs: True
            call()
            self.assertEqual(self.services.resets, 1)
        finally:
            trade_module.messagebox.askyesno = saved
        self.assertTrue(any("模拟盘已重置" in text for text in self.app.toast.texts()))
        self.assertIsNotNone(original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
