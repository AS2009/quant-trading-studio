# -*- coding: utf-8 -*-
"""MCP 回测 / 持仓账本 / 模拟盘工具组测试（unittest，全部离线替身，绝不联网）。

写法对齐 ``backend/tests/test_api.py``：

* ``BASE_DIR`` 插 ``sys.path``；
* 注入假 provider（确定性日线）+ 真实 ``BacktestEngine``（不联网、不依赖仓库数据）、
  假 broker（模拟盘通道替身）、假持仓后端（自有账本替身）；
* 数据目录用 ``tempfile`` 临时目录（MCP 审计日志写在里面），``Settings(offline=True)``；
* 通过 ``ToolContext._services`` 注入 ``Services`` 替身（``ToolContext.services()`` 只接受 provider 注入）。
"""

import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta

import inspect

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.backtest.engine import BacktestEngine  # noqa: E402
from quantstudio.config import Settings  # noqa: E402
from quantstudio.core.calendar import TradingCalendar  # noqa: E402
from quantstudio.core.models import Account, Bar, Fill, Order, Position  # noqa: E402
from quantstudio.mcp import tools_backtest, tools_portfolio  # noqa: E402
from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.registry import ToolError  # noqa: E402
from quantstudio.mcp.server import Server  # noqa: E402
from quantstudio.mcp.tools import build_registry  # noqa: E402
from quantstudio.services import Services  # noqa: E402
from quantstudio.services.common import cache_clear, normalize_code  # noqa: E402

# --------------------------------------------------------------------------- 工具清单

BACKTEST_TOOLS = ("backtest_run", "backtest_cache_info", "backtest_cache_clear")
PORTFOLIO_TOOLS = (
    "portfolio_overview",
    "portfolio_holdings",
    "portfolio_equity",
    "portfolio_upsert_holding",
    "portfolio_delete_holding",
    "portfolio_set_cash",
    "portfolio_set_mode",
)
PAPER_TOOLS = (
    "paper_account",
    "paper_orders",
    "paper_fills",
    "paper_submit_order",
    "paper_cancel_order",
    "paper_reset",
)
WRITE_TOOLS = (
    "backtest_cache_clear",
    "portfolio_upsert_holding",
    "portfolio_delete_holding",
    "portfolio_set_cash",
    "portfolio_set_mode",
    "paper_submit_order",
    "paper_cancel_order",
    "paper_reset",
)
READ_TOOLS = tuple(
    name for name in (BACKTEST_TOOLS + PORTFOLIO_TOOLS + PAPER_TOOLS) if name not in WRITE_TOOLS
)

#: 只读模式测试用的最小合法参数（handler 第一行就会被 write_guard 拦下）
MINIMAL_WRITE_ARGS = {
    "backtest_cache_clear": {},
    "portfolio_upsert_holding": {"code": "600519.SH", "quantity": 100, "cost": 10.0},
    "portfolio_delete_holding": {"code": "600519.SH"},
    "portfolio_set_cash": {"amount": 1000.0},
    "portfolio_set_mode": {"mode": "paper"},
    "paper_submit_order": {"code": "600519.SH", "side": "buy", "quantity": 100},
    "paper_cancel_order": {"order_id": "O1"},
    "paper_reset": {},
}

BENCH_CODE = "000300.SH"
STOCK_CODE = "600519.SH"


# --------------------------------------------------------------------------- 替身数据


def weekdays_ending(end: str, days: int):
    """生成 days 个工作日，最后一个为 end（升序）。"""
    y, m, d = (int(part) for part in end.split("-"))
    cur = date(y, m, d)
    out = []
    while len(out) < days:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur -= timedelta(days=1)
    return list(reversed(out))


class FakeProvider:
    """确定性日线替身：只认识 600519.SH / 300750.SZ / 000300.SH，未知标的返回空列表。"""

    name = "fake"
    last_meta = None

    def __init__(self, days=420, end="2024-06-28"):
        self.dates = weekdays_ending(end, days)
        self.calls = []

    def _close(self, code, index):
        seed = sum(ord(ch) for ch in code) % 23
        wave = 6.0 * math.sin(2.0 * math.pi * (index + seed) / 47.0)
        fast = 2.0 * math.sin(2.0 * math.pi * index / 13.0)
        return max(30.0 + seed + wave + fast + 0.05 * index, 5.0)

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        code = normalize_code(symbol)
        if code not in (STOCK_CODE, "300750.SZ", BENCH_CODE):
            return []
        self.calls.append(code)
        bars = []
        prev = 0.0
        for index, day in enumerate(self.dates):
            close = self._close(code, index)
            open_ = prev or close
            bars.append(
                Bar(
                    date=day,
                    open=round(open_, 4),
                    high=round(max(open_, close) * 1.01, 4),
                    low=round(min(open_, close) * 0.99, 4),
                    close=round(close, 4),
                    volume_wan=1000.0,
                    amount_yi=2.0,
                    change_pct=round((close / prev - 1.0) * 100.0, 4) if prev else 0.0,
                    turnover_pct=1.2,
                )
            )
            prev = close
        return bars[-int(days):] if days and int(days) > 0 else bars

    def resolve_name(self, symbol):
        return {STOCK_CODE: "贵州茅台", "300750.SZ": "宁德时代", BENCH_CODE: "沪深300"}.get(
            normalize_code(symbol)
        )


class FakeBroker:
    """模拟盘通道替身：限价 < 90 挂单（可撤），否则立即成交；不联网。"""

    name = "paper"
    is_live = False

    def __init__(self, cash=100000.0, positions=None):
        self._orders = []
        self._fills = []
        self._cash = float(cash)
        self._positions = list(positions or [])
        self._seq = 0

    def submit(self, order):
        self._seq += 1
        order_id = "O%d" % self._seq
        pending = order.price is not None and order.price < 90
        result = Order(
            order_id=order_id,
            code=order.code,
            name="示例",
            side=order.side,
            qty=order.qty,
            price=order.price,
            filled_qty=0 if pending else order.qty,
            avg_price=0.0 if pending else (order.price or 100.0),
            fee=0.0 if pending else 5.0,
            status="new" if pending else "filled",
            created_at=order.ts,
            updated_at=order.ts,
        )
        self._orders.insert(0, result)
        if not pending:
            self._fills.insert(
                0,
                Fill(
                    order_id=order_id,
                    code=order.code,
                    name="示例",
                    side=order.side,
                    price=result.avg_price,
                    qty=order.qty,
                    amount=result.avg_price * order.qty,
                    fee=5.0,
                    ts=order.ts,
                    reason="test",
                ),
            )
        return result

    def cancel(self, order_id):
        for order in self._orders:
            if order.order_id != order_id:
                continue
            if order.status in ("filled", "rejected", "cancelled"):
                return order
            order.status = "cancelled"
            return order
        return None

    def orders(self, limit=100):
        return self._orders[: int(limit)]

    def fills(self, limit=100):
        return self._fills[: int(limit)]

    def positions(self):
        return list(self._positions)

    def account(self):
        market_value = sum(float(getattr(item, "market_value", 0.0) or 0.0) for item in self._positions)
        return Account(
            total_assets=market_value + self._cash,
            market_value=market_value,
            cash=self._cash,
            available_cash=self._cash,
            frozen_cash=0.0,
            day_pnl=0.0,
            total_pnl=0.0,
            total_return_pct=0.0,
            positions_count=len(self._positions),
            allocation=[{"name": "现金", "value": self._cash}],
            as_of="2026-09-21 15:00:00",
        )

    def reset(self, initial_cash=None):
        if initial_cash is not None:
            self._cash = float(initial_cash)
        self._orders = []
        self._fills = []
        return self.account()


class FakePortfolioBackend:
    """自有持仓账本替身（``PortfolioService`` 包装对象），字段名沿用服务层：qty/available_qty。"""

    def __init__(self, holdings=None, cash=50000.0):
        self.mode = "manual"
        self._cash = float(cash)
        if holdings is None:
            holdings = [
                Position(
                    code=STOCK_CODE,
                    name="贵州茅台",
                    qty=100,
                    available_qty=100,
                    cost=1500.0,
                    price=1600.0,
                    market_value=160000.0,
                    cost_value=150000.0,
                    day_pnl=100.0,
                    total_pnl=10000.0,
                    return_pct=6.67,
                    industry="白酒",
                )
            ]
        self._holdings = list(holdings)

    def holdings(self):
        return list(self._holdings)

    def account(self):
        market_value = sum(float(getattr(item, "market_value", 0.0) or 0.0) for item in self._holdings)
        return Account(
            total_assets=market_value + self._cash,
            market_value=market_value,
            cash=self._cash,
            available_cash=self._cash,
            frozen_cash=0.0,
            day_pnl=100.0,
            total_pnl=10000.0,
            total_return_pct=6.7,
            positions_count=len(self._holdings),
            allocation=[{"name": "现金", "value": self._cash}],
            as_of="2026-09-21 15:00:00",
        )

    def upsert_holding(self, payload):
        assert isinstance(payload, dict), "PortfolioService 应传入 dict"
        code = payload["code"]
        item = Position(
            code=code,
            name=str(payload.get("name") or ""),
            qty=int(payload.get("qty") or 0),
            available_qty=int(payload.get("available_qty") or 0),
            cost=float(payload.get("cost") or 0.0),
            price=float(payload.get("price") or 0.0),
            market_value=float(payload.get("qty") or 0) * float(payload.get("price") or 0.0),
        )
        self._holdings = [row for row in self._holdings if row.code != code]
        self._holdings.append(item)
        return item

    def delete_holding(self, code):
        existed = any(row.code == code for row in self._holdings)
        self._holdings = [row for row in self._holdings if row.code != code]
        return existed

    def set_cash(self, amount):
        self._cash = float(amount)
        return self.account()

    def set_mode(self, mode):
        self.mode = mode
        return mode

    def equity_curve(self, days=90):
        if not self._holdings:                      # 空账本没有权益快照
            return {"items": [], "notes": ["账本暂无权益快照"], "count": 0}
        # 与真实后端一致：dict + items + total_assets（服务层会归一化成 (points, notes)）
        return {
            "items": [
                {"date": "2026-09-18", "total_assets": 200000.0},
                {"date": "2026-09-19", "total_assets": 201000.0},
                {"date": "2026-09-21", "total_assets": 210000.0},
            ],
            "notes": ["fake 权益说明"],
            "count": 3,
        }


# --------------------------------------------------------------------------- 测试用例


class McpToolsPortfolioTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="qs-mcp-tools-")
        cls.settings = Settings(data_dir=cls.temp_dir, offline=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        cache_clear("portfolio:holdings:manual")
        cache_clear("portfolio:holdings:paper")
        self.provider = FakeProvider()
        self.engine = BacktestEngine(self.provider, calendar=TradingCalendar(), settings=self.settings)
        self.broker = FakeBroker()
        self.backend = FakePortfolioBackend()
        self.services = Services(
            settings=self.settings,
            provider=self.provider,
            engine=self.engine,
            broker=self.broker,
            portfolio_service=self.backend,
        )
        self.registry = build_registry(modules=("tools_backtest", "tools_portfolio"))
        self.ctx = self.make_ctx(self.services)
        self.server = Server(self.registry, context=self.ctx, stderr=io.StringIO())

    # ------------------------------------------------------------------ 小工具
    def make_ctx(self, services, read_only=False):
        ctx = ToolContext(self.settings, read_only=read_only)
        ctx._services = services                        # 整体注入替身（services() 只支持 provider 注入）
        return ctx

    def call(self, name, args=None, ctx=None):
        server = self.server if ctx is None else Server(self.registry, context=ctx, stderr=io.StringIO())
        return server.dispatch("tools/call", {"name": name, "arguments": dict(args or {})})

    def text_of(self, result):
        return "\n".join(block.get("text", "") for block in (result.get("content") or []))

    def audit_lines(self):
        path = os.path.join(self.settings.data_dir, "mcp-audit.log")
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    # ------------------------------------------------------------------ ① 注册与 schema
    def test_01_registry_and_schemas(self):
        expected = sorted(BACKTEST_TOOLS + PORTFOLIO_TOOLS + PAPER_TOOLS)
        self.assertEqual(self.registry.names(), expected)
        self.assertEqual(len(WRITE_TOOLS) + len(READ_TOOLS), len(expected))
        for name in expected:
            spec = self.registry.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.input_schema.get("type"), "object", name)
            self.assertFalse(spec.input_schema.get("additionalProperties"), name)
            self.assertTrue(spec.description.strip(), name)
            self.assertEqual(spec.read_only, name not in WRITE_TOOLS, name)
        self.assertEqual(self.registry.get("paper_reset").destructive, True)
        self.assertEqual(self.registry.get("portfolio_delete_holding").destructive, True)
        self.assertEqual(self.registry.get("backtest_cache_clear").destructive, False)
        self.assertIn("耗时", self.registry.get("backtest_run").description)
        self.assertIn("不触达真实券商", self.registry.get("paper_account").description)
        groups = self.registry.groups()
        self.assertEqual(sorted(groups["backtest"]), sorted(BACKTEST_TOOLS))
        self.assertEqual(sorted(groups["portfolio"]), sorted(PORTFOLIO_TOOLS + PAPER_TOOLS))

    # ------------------------------------------------------------------ ② 只读工具（空账本）
    def test_02_read_tools_empty_ledger(self):
        empty_services = Services(
            settings=self.settings,
            provider=self.provider,
            engine=self.engine,
            broker=FakeBroker(cash=0.0),
            portfolio_service=FakePortfolioBackend(holdings=[], cash=0.0),
        )
        ctx = self.make_ctx(empty_services)
        expected_keys = {
            "backtest_cache_info": ("size", "capacity", "keys"),
            "portfolio_overview": ("total_assets", "market_value", "cash", "day_pnl",
                                   "total_pnl", "total_return_pct", "positions_count", "mode"),
            "portfolio_holdings": ("mode", "count", "items"),
            "portfolio_equity": ("days", "count", "points", "notes"),
            "paper_account": ("mode", "total_assets", "cash", "paper_account"),
            "paper_orders": ("limit", "shown", "total", "orders"),
            "paper_fills": ("limit", "shown", "total", "fills"),
        }
        for name, keys in expected_keys.items():
            result = self.call(name, {}, ctx=ctx)
            self.assertFalse(result["isError"], "%s: %s" % (name, self.text_of(result)))
            data = result["structuredContent"]
            for key in keys:
                self.assertIn(key, data, "%s.%s" % (name, key))
            self.assertTrue(self.text_of(result).strip(), name)
        self.assertIn("没有持仓", self.text_of(self.call("portfolio_holdings", {}, ctx=ctx)))
        self.assertIn("没有委托", self.text_of(self.call("paper_orders", {}, ctx=ctx)))
        self.assertIn("没有成交", self.text_of(self.call("paper_fills", {}, ctx=ctx)))
        self.assertIn("不触达真实券商", self.text_of(self.call("paper_account", {}, ctx=ctx)))
        equity = self.call("portfolio_equity", {"days": 30}, ctx=ctx)
        self.assertEqual(equity["structuredContent"]["days"], 30)
        self.assertEqual(equity["structuredContent"]["count"], 0)

    # ------------------------------------------------------------------ ③ 只读模式
    def test_03_write_tools_hidden_and_rejected_in_read_only(self):
        visible = set(spec.name for spec in self.registry.visible(writable=False))
        self.assertTrue(set(READ_TOOLS) <= visible)
        for name in WRITE_TOOLS:
            self.assertNotIn(name, visible)
        ro_ctx = self.make_ctx(self.services, read_only=True)
        for name in WRITE_TOOLS:
            spec = self.registry.get(name)
            with self.assertRaises(ToolError) as raised:
                spec.handler(ro_ctx, dict(MINIMAL_WRITE_ARGS[name]))
            self.assertEqual(raised.exception.code, "READ_ONLY", name)
            result = self.call(name, dict(MINIMAL_WRITE_ARGS[name]), ctx=ro_ctx)
            self.assertTrue(result["isError"], name)
        rejected = [
            row for row in self.audit_lines()
            if row.get("tool") in WRITE_TOOLS and row.get("ok") is False
        ]
        self.assertGreaterEqual(len(rejected), len(WRITE_TOOLS), "只读拒绝也应记审计")
        # 硬要求：写 handler 的第一条语句就是 ctx.write_guard(...)
        for name in WRITE_TOOLS:
            module = tools_backtest if name.startswith("backtest_") else tools_portfolio
            body = [line.strip() for line in inspect.getsource(getattr(module, "_" + name)).splitlines()
                    if line.strip()]
            self.assertTrue(body[1].startswith("ctx.write_guard("),
                            "%s 首行不是 write_guard：%s" % (name, body[1]))
        # 只读工具在只读模式下仍可用
        for name in ("backtest_cache_info", "portfolio_overview", "portfolio_holdings",
                     "portfolio_equity", "paper_account", "paper_orders", "paper_fills"):
            self.assertFalse(self.call(name, {}, ctx=ro_ctx)["isError"], name)

    # ------------------------------------------------------------------ ④ 回测
    def test_04_backtest_run_summary_and_series(self):
        args = {
            "strategy_id": "st_ma_cross",
            "symbols": [STOCK_CODE],
            "start": "2024-01-02",
            "end": "2024-06-28",
            "initial_cash": 1000000,
            "params": {"short_ma": 5, "long_ma": 20},
        }
        first = self.call("backtest_run", args)
        self.assertFalse(first["isError"], self.text_of(first))
        data = first["structuredContent"]
        for key in ("strategy", "range", "request", "metrics", "trade_total", "positions", "warnings"):
            self.assertIn(key, data)
        self.assertNotIn("nav", data)
        self.assertNotIn("series", data)
        self.assertNotIn("trades", data)
        self.assertFalse(data["series_included"])
        self.assertFalse(data["trades_included"])
        metrics = data["metrics"]
        self.assertGreater(metrics["trading_days"], 60)
        self.assertEqual(data["range"]["start"], "2024-01-02")
        text = self.text_of(first)
        self.assertIn("累计收益", text)
        self.assertIn("最大回撤", text)
        self.assertIn("个交易日", text)
        calls_after_first = len(self.provider.calls)
        self.assertGreater(calls_after_first, 0)

        second = self.call("backtest_run", dict(args, include_series=True, include_trades=True))
        self.assertFalse(second["isError"], self.text_of(second))
        data2 = second["structuredContent"]
        series = data2["series"]
        # 引擎在区间前多插一个基准点：nav 长度 = 区间交易日数 + 1
        self.assertEqual(len(series["nav"]), metrics["trading_days"] + 1)
        self.assertEqual(series["nav_points"], len(series["nav"]))
        self.assertLessEqual(len(series["nav"]), tools_backtest.SERIES_LIMIT)
        self.assertEqual(len(series["drawdown"]), len(series["nav"]))
        self.assertIn("trades", data2)
        self.assertEqual(data2["trades_shown"], len(data2["trades"]))
        self.assertEqual(len(data2["trades"]), min(data2["trades_total"], tools_backtest.TRADES_LIMIT))
        text2 = self.text_of(second)
        self.assertIn("净值：返回", text2)
        self.assertIn("明细序列", text2)
        # 同参数（include_* 只影响展示）应命中服务层回测缓存，不再取数
        self.assertEqual(len(self.provider.calls), calls_after_first)

    # ------------------------------------------------------------------ ④b 费用参数透传

    def test_04b_backtest_fee_options(self):
        """新费用参数（流量费 / 跳数滑点 / tick 价位 / 手数 / 最低佣金）能从工具 schema 透传到引擎。"""
        spec = self.registry.get("backtest_run")
        properties = spec.input_schema["properties"]
        for key in ("commission_min", "flow_fee", "slippage_ticks", "tick_size", "lot_size"):
            self.assertIn(key, properties, "工具 schema 应暴露 %s" % key)
            self.assertIn("minimum", properties[key], "%s 应带下限" % key)

        args = {
            "strategy_id": "st_ma_cross",
            "symbols": [STOCK_CODE],
            "start": "2024-01-02",
            "end": "2024-06-28",
            "initial_cash": 1000000,
            "params": {"short_ma": 5, "long_ma": 20},
        }
        base = self.call("backtest_run", args)
        self.assertFalse(base["isError"], self.text_of(base))
        expensive = self.call("backtest_run", dict(args, flow_fee=50.0, slippage_ticks=2.0))
        self.assertFalse(expensive["isError"], self.text_of(expensive))

        base_fee = base["structuredContent"]["metrics"]["total_fee"]
        high_fee = expensive["structuredContent"]["metrics"]["total_fee"]
        self.assertGreater(high_fee, base_fee, "流量费与跳数滑点应推高总费用")
        fee = expensive["structuredContent"]["request"]["fee"]
        self.assertAlmostEqual(fee["flow_fee"], 50.0)
        self.assertAlmostEqual(fee["slippage_ticks"], 2.0)
        self.assertEqual(int(fee["lot_size"]), 100)

        # 缺省时不下发这些键（交给 settings / FeeConfig 默认值）
        self.assertNotIn("flow_fee", tools_backtest._run_options(args))


    # ------------------------------------------------------------------ ⑤ 模拟盘闭环 + 审计
    def test_05_paper_order_closed_loop_and_audit(self):
        before = len(self.audit_lines())

        cash = self.call("portfolio_set_cash", {"amount": 500000})
        self.assertFalse(cash["isError"], self.text_of(cash))
        self.assertEqual(cash["structuredContent"]["cash"], 500000.0)

        pending = self.call(
            "paper_submit_order",
            {"code": STOCK_CODE, "side": "buy", "quantity": 100, "price": 50.0},
        )
        self.assertFalse(pending["isError"], self.text_of(pending))
        pending_order = pending["structuredContent"]["order"]
        self.assertEqual(pending_order["status"], "new")          # 限价 50 < 90 → 假 broker 挂单
        self.assertIsNone(pending["structuredContent"]["fill"])
        order_id = pending_order["order_id"]
        self.assertIn(order_id, self.text_of(pending))

        filled = self.call("paper_submit_order", {"code": STOCK_CODE, "side": "buy", "quantity": 100})
        self.assertFalse(filled["isError"], self.text_of(filled))
        self.assertEqual(filled["structuredContent"]["order"]["status"], "filled")
        self.assertIsNotNone(filled["structuredContent"]["fill"])
        receipt = self.text_of(filled)
        self.assertIn("委托号", receipt)
        self.assertIn("成交", receipt)
        self.assertIn("不触达真实券商", receipt)

        orders = self.call("paper_orders", {"limit": 20})
        self.assertFalse(orders["isError"])
        self.assertIn(order_id, [row["order_id"] for row in orders["structuredContent"]["orders"]])
        self.assertGreaterEqual(orders["structuredContent"]["total"], 2)
        self.assertIn("共", self.text_of(orders))

        fills = self.call("paper_fills", {"limit": 20})
        self.assertFalse(fills["isError"])
        self.assertTrue(fills["structuredContent"]["fills"])

        cancelled = self.call("paper_cancel_order", {"order_id": order_id})
        self.assertFalse(cancelled["isError"], self.text_of(cancelled))
        self.assertEqual(cancelled["structuredContent"]["status"], "cancelled")
        self.assertIn("已撤销", self.text_of(cancelled))

        reset = self.call("paper_reset", {"initial_cash": 200000})
        self.assertFalse(reset["isError"], self.text_of(reset))
        self.assertEqual(reset["structuredContent"]["cash"], 200000.0)
        self.assertIn("清空本地模拟盘账本", self.text_of(reset))

        rows = self.audit_lines()[before:]
        tools = [row.get("tool") for row in rows]
        for name in ("portfolio_set_cash", "paper_submit_order", "paper_cancel_order", "paper_reset"):
            self.assertIn(name, tools, "审计缺少 %s" % name)
        succeeded = [row for row in rows if row.get("ok") is True]
        self.assertEqual(len(succeeded), 5)                       # set_cash + 2×下单 + 撤单 + reset
        self.assertEqual(len([row for row in tools if row == "paper_submit_order"]), 2)

    # ------------------------------------------------------------------ ⑥ 参数校验
    def test_06_paper_submit_argument_validation(self):
        cases = [
            {"code": STOCK_CODE, "quantity": 100},                        # 缺 side
            {"code": STOCK_CODE, "side": "buy", "quantity": 0},           # quantity=0
            {"code": STOCK_CODE, "side": "hold", "quantity": 100},        # 枚举外
        ]
        for args in cases:
            result = self.call("paper_submit_order", args)
            self.assertTrue(result["isError"], args)
            error = result["structuredContent"]["error"]
            self.assertIn(error["code"], ("INVALID_ARGS", "VALIDATION"), args)
            self.assertTrue(self.text_of(result).strip())
        self.assertFalse(self.call("paper_orders", {})["isError"])

    # ------------------------------------------------------------------ ⑦ 空缓存清理
    def test_07_backtest_cache_clear_on_empty_cache(self):
        services = Services(
            settings=self.settings,
            provider=self.provider,
            engine=self.engine,
            broker=FakeBroker(),
            portfolio_service=FakePortfolioBackend(),
        )
        ctx = self.make_ctx(services)
        result = self.call("backtest_cache_clear", {}, ctx=ctx)
        self.assertFalse(result["isError"], self.text_of(result))
        self.assertTrue(result["structuredContent"]["cleared"])
        self.assertEqual(result["structuredContent"]["size"], 0)
        self.assertIn("不删除", self.text_of(result))
        again = self.call("backtest_cache_clear", {}, ctx=ctx)
        self.assertFalse(again["isError"], self.text_of(again))

    # ------------------------------------------------------------------ ⑧ 持仓账本写工具
    def test_08_portfolio_write_tools(self):
        upsert = self.call(
            "portfolio_upsert_holding",
            {"code": "300750.SZ", "quantity": 200, "cost": 250.5, "available": 100, "note": "手工加仓"},
        )
        self.assertFalse(upsert["isError"], self.text_of(upsert))
        holding = upsert["structuredContent"]
        self.assertEqual(holding["code"], "300750.SZ")
        self.assertEqual(holding["qty"], 200)
        self.assertEqual(holding["available_qty"], 100)
        self.assertIn("300750.SZ", self.text_of(upsert))

        holdings = self.call("portfolio_holdings", {})
        self.assertFalse(holdings["isError"])
        self.assertEqual(holdings["structuredContent"]["count"], 2)   # 初始 1 只 + 新增 1 只
        self.assertIn("300750.SZ", self.text_of(holdings))

        deleted = self.call("portfolio_delete_holding", {"code": "300750.SZ"})
        self.assertFalse(deleted["isError"], self.text_of(deleted))
        self.assertTrue(deleted["structuredContent"]["deleted"])
        self.assertIn("不可撤销", self.text_of(deleted))
        self.assertEqual(self.call("portfolio_holdings", {})["structuredContent"]["count"], 1)

        cash = self.call("portfolio_set_cash", {"amount": 12345})
        self.assertFalse(cash["isError"], self.text_of(cash))
        self.assertEqual(cash["structuredContent"]["cash"], 12345.0)

        mode = self.call("portfolio_set_mode", {"mode": "paper"})
        self.assertFalse(mode["isError"], self.text_of(mode))
        self.assertEqual(mode["structuredContent"]["mode"], "paper")
        overview = self.call("portfolio_overview", {})
        self.assertFalse(overview["isError"], self.text_of(overview))
        self.assertEqual(overview["structuredContent"]["mode"], "paper")

        equity = self.call("portfolio_equity", {"days": 30})
        self.assertFalse(equity["isError"], self.text_of(equity))
        self.assertEqual(equity["structuredContent"]["points"][-1]["equity"], 210000.0)
        self.assertIn("权益曲线", self.text_of(equity))
        self.assertIn("fake 权益说明", self.text_of(equity))

        back = self.call("portfolio_set_mode", {"mode": "manual"})
        self.assertEqual(back["structuredContent"]["mode"], "manual")

        fractional = self.call(
            "portfolio_upsert_holding", {"code": "300750.SZ", "quantity": 100.5, "cost": 10}
        )
        self.assertTrue(fractional["isError"], "非整数股数应被拒绝")
        missing = self.call("portfolio_upsert_holding", {"code": "300750.SZ"})
        self.assertTrue(missing["isError"])

    # ------------------------------------------------------------------ ⑨ 模块常量与工具清单一致性
    def test_09_module_constants(self):
        self.assertEqual(tools_portfolio.DEFAULT_LIMIT, 20)
        self.assertEqual(tools_portfolio.MAX_LIMIT, 200)
        self.assertLessEqual(tools_backtest.SERIES_LIMIT, 300)
        self.assertLessEqual(tools_backtest.TRADES_LIMIT, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
