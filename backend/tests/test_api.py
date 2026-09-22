# -*- coding: utf-8 -*-
"""接口层集成测试（unittest + Flask test_client）。

全部依赖用离线替身注入（FakeProvider / FakeEngine / FakeBroker / FakePortfolio），
不访问网络；数据目录指向临时目录，不污染仓库。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.api import create_app  # noqa: E402
from quantstudio.config import Settings  # noqa: E402
from quantstudio.core.calendar import TradingCalendar  # noqa: E402
from quantstudio.core.errors import InsufficientData, SymbolNotFound, ValidationError  # noqa: E402
from quantstudio.core.models import (  # noqa: E402
    Account,
    BacktestMetrics,
    BacktestResult,
    Bar,
    DataMeta,
    Fill,
    IndexQuote,
    MarketBreadth,
    Order,
    Position,
    Quote,
    SectorQuote,
    StrategySpec,
    Trade,
)
from quantstudio.services import Services  # noqa: E402
from quantstudio.services.common import normalize_code, write_json_atomic  # noqa: E402

# --------------------------------------------------------------------------- 替身数据

INDEXES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
STOCKS = {
    "600519.SH": "贵州茅台",
    "300750.SZ": "宁德时代",
    "002594.SZ": "比亚迪",
    "600036.SH": "招商银行",
    "601318.SH": "中国平安",
    "000858.SZ": "五粮液",
    "600900.SH": "长江电力",
    "601012.SH": "隆基绿能",
    "600276.SH": "恒瑞医药",
    "300059.SZ": "东方财富",
}


class FakeProvider:
    """离线固定数据源：未知标的抛 SymbolNotFound（与真实 Composite 行为一致）。"""

    name = "fake"

    def __init__(self):
        self.last_meta = DataMeta(
            source="sample",
            stale=False,
            offline=False,
            as_of="2026-09-21 15:00:00",
            latency_ms=1,
            notes=["测试替身"],
        )
        self.calls = {"quotes": 0, "kline": 0}

    def latest_quotes(self, symbols):
        self.calls["quotes"] += 1
        rows = []
        for raw in symbols:
            code = normalize_code(raw)
            if code not in STOCKS:
                raise SymbolNotFound("未找到标的 %s" % code)
            rows.append(
                Quote(
                    code=code,
                    name=STOCKS[code],
                    price=10.0,
                    prev_close=9.5,
                    open=9.6,
                    high=10.2,
                    low=9.4,
                    change=0.5,
                    change_pct=5.26,
                    volume_wan=123.4,
                    amount_yi=12.34,
                    turnover_pct=1.23,
                    pe_ttm=20.0,
                    pb=3.0,
                    market_cap_yi=1000.0,
                    ts="2026-09-21 15:00:00",
                    source="sample",
                )
            )
        return rows

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        code = normalize_code(symbol)
        if code not in STOCKS and code not in INDEXES:
            raise SymbolNotFound("未找到标的 %s" % code)
        self.calls["kline"] += 1
        calendar = TradingCalendar()
        dates = calendar.trading_days("2020-01-01", "2026-09-21")[-int(days):]
        bars = []
        price = 100.0
        for i, day in enumerate(dates):
            price = price * (1.01 if i % 2 else 0.995)
            bars.append(
                Bar(
                    date=day,
                    open=price,
                    high=price * 1.01,
                    low=price * 0.99,
                    close=price,
                    volume_wan=10.0,
                    amount_yi=1.0,
                    change_pct=0.5,
                    turnover_pct=0.8,
                )
            )
        return bars

    def index_quotes(self, symbols):
        rows = []
        for i, code in enumerate(symbols):
            if code not in INDEXES:
                continue
            rows.append(
                IndexQuote(
                    code=code,
                    name=INDEXES[code],
                    point=3000.0 + i,
                    prev_close=2990.0,
                    change=10.0,
                    change_pct=0.33,
                    amount_yi=1000.0,
                    volume_wan=500.0,
                    source="sample",
                )
            )
        return rows

    def sectors(self, limit=20):
        rows = [
            SectorQuote(code="BK001", name="半导体", change_pct=3.5, net_inflow_yi=9.9, up_count=30, down_count=5),
            SectorQuote(code="BK002", name="白酒", change_pct=1.2, net_inflow_yi=2.2, up_count=12, down_count=6),
            SectorQuote(code="BK003", name="银行", change_pct=-0.8, net_inflow_yi=-3.3, up_count=4, down_count=28),
            SectorQuote(code="BK004", name="新能源", change_pct=2.1, net_inflow_yi=5.5, up_count=20, down_count=9),
        ]
        return rows[: int(limit)]

    def breadth(self):
        return MarketBreadth(
            up=3000,
            down=1500,
            flat=200,
            limit_up=50,
            limit_down=5,
            total=4700,
            total_amount_yi=12345.0,
            main_net_inflow_yi=-12.3,
            north_net_inflow_yi=45.6,
            source="sample",
        )

    def resolve_name(self, symbol):
        return STOCKS.get(normalize_code(symbol))

    def health(self):
        return {"ok": True, "detail": "fake provider ok", "latency_ms": 1}

    def describe(self):
        return {"name": "fake", "sources": ["sina", "sample"], "available": ["sample"]}

    def sources(self):
        return ["sina", "sample"]


BUILTIN_SPECS = [
    dict(
        id="st_ma_cross",
        name="双均线趋势策略",
        category="趋势跟踪",
        desc="短均线上穿长均线买入",
        params={"short_ma": 20, "long_ma": 60},
        status="running",
        universe="沪深300",
        universe_type="multi",
        freq="日线",
        builtin=True,
        param_schema={"short_ma": {"type": "int", "default": 20}},
        min_bars=60,
    ),
    dict(
        id="st_insufficient",
        name="数据不足策略",
        category="自定义",
        desc="用于测试 InsufficientData → 400",
        params={},
        status="paused",
        universe="沪深300",
        universe_type="multi",
        freq="日线",
        builtin=True,
        param_schema={},
        min_bars=250,
    ),
]


class FakeStrategy:
    def __init__(self, spec):
        self.spec = spec


class FakeStrategiesModule:
    def __init__(self):
        self.create_calls = []

    def list_specs(self):
        return [StrategySpec(**item) for item in BUILTIN_SPECS]

    def get_class(self, strategy_id):
        return FakeStrategy

    def create(self, strategy_id, params=None, symbols=None, spec=None):
        self.create_calls.append(strategy_id)
        for item in BUILTIN_SPECS:
            if item["id"] == strategy_id:
                data = dict(item)
                merged = dict(data.get("params") or {})
                merged.update(params or {})
                data["params"] = merged
                if spec is not None:  # 用户策略：实例元数据用用户 spec（与 registry.create 一致）
                    if isinstance(spec, dict):
                        spec = StrategySpec(**spec)
                    return FakeStrategy(spec)
                return FakeStrategy(StrategySpec(**data))
        raise KeyError(strategy_id)

    @property
    def REGISTRY(self):
        return {item["id"]: FakeStrategy for item in BUILTIN_SPECS}


class FakeUserStore:
    """用户策略持久化替身（写入临时目录的 user_strategies.json）。"""

    def load_user_specs(self, path):
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_user_specs(self, path, items):
        write_json_atomic(path, items)

    def add_user_strategy(self, path, payload):
        name = str(payload.get("name") or "").strip()
        if not (2 <= len(name) <= 24):
            raise ValidationError("策略名称长度需为 2-24 个字符", field="name")
        items = self.load_user_specs(path)
        spec = StrategySpec(
            id=str(payload.get("id") or "user_%d" % (len(items) + 1)),
            name=name,
            category=str(payload.get("category") or "自定义"),
            desc=str(payload.get("desc") or ""),
            params=dict(payload.get("params") or {}),
            status=str(payload.get("status") or "paused"),
            universe=str(payload.get("universe") or ""),
            universe_type=str(payload.get("universe_type") or "multi"),
            freq=str(payload.get("freq") or "日线"),
            builtin=False,
            param_schema=dict(payload.get("param_schema") or {}),
            min_bars=int(payload.get("min_bars") or 60),
        )
        item = spec.to_dict()
        if any(x.get("id") == item["id"] for x in items):
            raise ValidationError("策略 id 已存在：%s" % item["id"], field="id")
        items.append(item)
        self.save_user_specs(path, items)
        return item

    def delete_user_strategy(self, path, strategy_id):
        items = self.load_user_specs(path)
        left = [x for x in items if x.get("id") != strategy_id]
        if len(left) == len(items):
            return False
        self.save_user_specs(path, left)
        return True


class FakeEngine:
    """离线回测引擎：返回固定结构结果；st_insufficient 抛 InsufficientData。"""

    def __init__(self):
        self.calls = 0

    def run(self, request, strategy):
        self.calls += 1
        if request.strategy_id == "st_insufficient":
            raise InsufficientData("仅 10 根 bar，需要 250 根")
        spec = getattr(strategy, "spec", None) or StrategySpec(id=request.strategy_id, name="替身策略")
        metrics = BacktestMetrics(
            total_return_pct=12.5,
            annual_return_pct=8.0,
            max_drawdown_pct=-6.5,
            max_drawdown_start="2024-03-01",
            max_drawdown_end="2024-04-01",
            sharpe=1.1,
            sortino=1.4,
            calmar=1.2,
            volatility_pct=15.0,
            win_rate_pct=55.0,
            trade_count=7,
            profit_loss_ratio=1.8,
            turnover_pct=120.0,
            total_fee=123.0,
            benchmark_return_pct=5.0,
            alpha_pct=3.0,
            beta=0.9,
            start=request.start or "2024-01-02",
            end=request.end or "2024-06-28",
            trading_days=120,
        )
        code = request.symbols[0] if request.symbols else "600519.SH"
        return BacktestResult(
            strategy=spec,
            request=request,
            range={"start": metrics.start, "end": metrics.end},
            nav=[
                {"date": "2024-01-02", "strategy": 1.0, "benchmark": 1.0},
                {"date": "2024-06-28", "strategy": 1.125, "benchmark": 1.05},
            ],
            drawdown=[{"date": "2024-06-28", "dd_pct": -6.5}],
            monthly=[{"month": "2024-01", "ret_pct": 1.0}],
            metrics=metrics,
            trades=[
                Trade(
                    date="2024-02-01",
                    code=code,
                    name="贵州茅台",
                    side="buy",
                    price=100.0,
                    qty=100,
                    amount=10000.0,
                    fee=5.0,
                    reason="test",
                )
            ],
            equity=[{"date": "2024-06-28", "equity": request.initial_cash * 1.125}],
            positions=[{"code": code, "qty": 100}],
            warnings=["fake engine"],
        )


class FakeBroker:
    """模拟盘替身：价格 < 90 的限价单挂单（可撤），否则立即成交。"""

    name = "paper"
    is_live = False

    def __init__(self):
        self._orders = []
        self._fills = []
        self._cash = 1_000_000.0
        self._positions = [
            Position(
                code="600519.SH",
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
            )
        ]
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
            if order.order_id == order_id:
                if order.status == "filled":
                    raise ValueError("已成交的委托不可撤销")
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
        return Account(
            total_assets=1_160_000.0,
            market_value=160000.0,
            cash=self._cash,
            available_cash=self._cash,
            frozen_cash=0.0,
            day_pnl=100.0,
            total_pnl=10000.0,
            total_return_pct=0.9,
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
    """真实持仓替身（PortfolioService 包装对象）。"""

    def __init__(self):
        self.mode = "manual"
        self._cash = 50000.0
        self._holdings = [
            Position(
                code="600519.SH",
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

    def holdings(self):
        return list(self._holdings)

    def account(self):
        market_value = sum(item.market_value for item in self._holdings)
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
            name=str(payload.get("name") or STOCKS.get(code, "")),
            qty=int(payload.get("qty") or 0),
            available_qty=int(payload.get("available_qty") or 0),
            cost=float(payload.get("cost") or 0.0),
            price=float(payload.get("price") or 0.0),
            market_value=float(payload.get("qty") or 0) * float(payload.get("price") or 0.0),
        )
        self._holdings = [x for x in self._holdings if x.code != code]
        self._holdings.append(item)
        return item

    def delete_holding(self, code):
        existed = any(x.code == code for x in self._holdings)
        self._holdings = [x for x in self._holdings if x.code != code]
        return existed

    def set_cash(self, amount):
        self._cash = float(amount)
        return self.account()

    def set_mode(self, mode):
        self.mode = mode
        return mode

    def equity_curve(self, days=90):
        # 与真实 quantstudio.portfolio.PortfolioService 一致：dict + items + total_assets
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


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="qs-api-test-")
        settings = Settings(data_dir=cls.temp_dir, offline=False)
        cls.provider = FakeProvider()
        cls.engine = FakeEngine()
        cls.broker = FakeBroker()
        cls.backend = FakePortfolioBackend()
        cls.strategies_module = FakeStrategiesModule()
        cls.user_store = FakeUserStore()
        cls.services = Services(
            settings=settings,
            provider=cls.provider,
            engine=cls.engine,
            broker=cls.broker,
            portfolio_service=cls.backend,
            strategies_module=cls.strategies_module,
            user_module=cls.user_store,
        )
        app = create_app(settings=settings, services=cls.services)
        app.config["TESTING"] = True
        cls.app = app
        cls.client = app.test_client()
        cls.settings = settings

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 工具

    def body(self, response):
        text = response.get_data(as_text=True)
        try:
            return json.loads(text)
        except ValueError:
            self.fail("响应不是合法 JSON：%s" % text[:200])

    def check_envelope(self, response, status=200):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        payload = self.body(response)
        self.assertIn("data", payload)
        self.assertIn("as_of", payload)
        self.assertIn("meta", payload)
        meta = payload["meta"]
        for key in ("source", "stale", "offline", "as_of", "latency_ms", "notes"):
            self.assertIn(key, meta)
        datetime.strptime(payload["as_of"], "%Y-%m-%d %H:%M:%S")
        return payload

    def check_json_error(self, response, status):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertIn("application/json", response.content_type or "")
        payload = self.body(response)
        self.assertIn("error", payload)
        self.assertIn("code", payload)
        self.assertEqual(payload["code"], status)
        self.assertTrue(str(payload["error"]).strip())
        return payload

    # ------------------------------------------------------------------ ① GET 全量接口

    def test_01_market_overview(self):
        payload = self.check_envelope(self.client.get("/api/market/overview"))
        data = payload["data"]
        self.assertTrue(data["indices"])
        first = data["indices"][0]
        for key in ("code", "name", "point", "change_pct", "amount_yi"):
            self.assertIn(key, first)
        breadth = data["breadth"]
        for key in ("up", "down", "flat", "limit_up", "limit_down", "total"):
            self.assertIn(key, breadth)
        self.assertEqual(data["total_amount_yi"], 12345.0)
        self.assertEqual(data["main_net_inflow_yi"], -12.3)
        self.assertEqual(data["north_net_inflow_yi"], 45.6)

    def test_02_market_sectors(self):
        payload = self.check_envelope(self.client.get("/api/market/sectors?limit=3"))
        rows = payload["data"]
        self.assertEqual(len(rows), 3)
        pcts = [row["change_pct"] for row in rows]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        for key in ("code", "name", "change_pct", "net_inflow_yi", "up_count", "down_count"):
            self.assertIn(key, rows[0])

    def test_03_market_quotes(self):
        payload = self.check_envelope(self.client.get("/api/market/quotes"))
        rows = payload["data"]
        self.assertEqual(len(rows), len(self.settings.default_watchlist))
        for key in (
            "code",
            "name",
            "price",
            "prev_close",
            "change_pct",
            "change",
            "volume_wan",
            "amount_yi",
            "turnover_pct",
            "pe_ttm",
            "pb",
            "market_cap_yi",
            "ts",
        ):
            self.assertIn(key, rows[0])
        ordered = self.check_envelope(
            self.client.get("/api/market/quotes?codes=300750.SZ,600519")
        )["data"]
        self.assertEqual([row["code"] for row in ordered], ["300750.SZ", "600519.SH"])

    def test_04_market_kline(self):
        payload = self.check_envelope(
            self.client.get("/api/market/kline?code=600519.SH&days=30&freq=day&adjust=qfq")
        )
        rows = payload["data"]
        self.assertEqual(len(rows), 30)
        for key in (
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume_wan",
            "amount_yi",
            "change_pct",
            "turnover_pct",
        ):
            self.assertIn(key, rows[-1])
        self.assertLess(rows[0]["date"], rows[-1]["date"])

    def test_05_watchlist_crud(self):
        payload = self.check_envelope(self.client.get("/api/watchlist"))
        self.assertEqual(payload["data"]["codes"], self.settings.default_watchlist)
        self.assertTrue(payload["data"]["items"])

        created = self.check_envelope(
            self.client.post("/api/watchlist", json={"code": "600000.SH"}), status=201
        )
        self.assertIn("600000.SH", created["data"]["codes"])
        self.assertIn(
            "600000.SH", self.check_envelope(self.client.get("/api/watchlist"))["data"]["codes"]
        )
        removed = self.check_envelope(self.client.delete("/api/watchlist/600000.SH"))
        self.assertNotIn("600000.SH", removed["data"]["codes"])

    def test_06_strategies_list(self):
        payload = self.check_envelope(self.client.get("/api/strategies"))
        rows = payload["data"]
        self.assertTrue(rows)
        first = rows[0]
        for key in ("id", "name", "builtin", "freq", "universe_type", "min_bars", "param_schema"):
            self.assertIn(key, first)
        detail = self.check_envelope(self.client.get("/api/strategies/st_ma_cross"))
        self.assertEqual(detail["data"]["id"], "st_ma_cross")
        self.assertTrue(detail["data"]["builtin"])

    def test_07_backtest_get(self):
        payload = self.check_envelope(self.client.get("/api/backtest/st_ma_cross"))
        data = payload["data"]
        metrics = data["metrics"]
        for key in ("total_return_pct", "annual_return_pct", "max_drawdown_pct", "sharpe", "win_rate_pct", "trade_count"):
            self.assertIn(key, metrics)
        self.assertTrue(data["nav"])
        self.assertIn("trade_total", data)
        self.assertEqual(data["trade_total"], metrics["trade_count"])
        self.assertIn("warnings", data)
        self.assertIn("positions", data)
        self.assertIn("equity", data)

    def test_08_portfolio_get(self):
        overview = self.check_envelope(self.client.get("/api/portfolio/overview"))["data"]
        for key in ("total_assets", "market_value", "cash", "day_pnl", "total_pnl", "positions_count", "allocation"):
            self.assertIn(key, overview)
        holdings = self.check_envelope(self.client.get("/api/portfolio/holdings"))["data"]
        self.assertTrue(holdings)
        for key in ("code", "name", "qty", "cost", "price", "market_value"):
            self.assertIn(key, holdings[0])
        equity = self.check_envelope(self.client.get("/api/portfolio/equity?days=90"))
        self.assertIsInstance(equity["data"], list)
        self.assertIn("date", equity["data"][0])
        self.assertIn("equity", equity["data"][0])
        self.assertEqual(equity["data"][-1]["equity"], 210000.0)  # total_assets → equity 归一化
        self.assertIn("fake 权益说明", equity["meta"]["notes"])

    def test_09_orders_and_fills_get(self):
        orders = self.check_envelope(self.client.get("/api/orders?limit=5"))["data"]
        self.assertIsInstance(orders, list)
        fills = self.check_envelope(self.client.get("/api/fills?limit=5"))["data"]
        self.assertIsInstance(fills, list)

    def test_10_health_and_status(self):
        health = self.body(self.client.get("/api/health"))
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        for key in ("ok", "version", "as_of", "provider", "mode"):
            self.assertIn(key, health)
        self.assertEqual(health["provider"], "fake")
        self.assertIn(health["mode"], ("real", "cache", "offline"))

        payload = self.check_envelope(self.client.get("/api/system/status"))
        data = payload["data"]
        for key in (
            "version",
            "mode",
            "provider",
            "offline",
            "as_of",
            "market",
            "cache",
            "backend",
            "data_dir",
            "benchmark",
            "initial_cash",
            "trade",
            "watchlist_count",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["mode"], "real")
        self.assertFalse(data["offline"])
        self.assertEqual(data["provider"]["active"], "fake")
        self.assertIn("sample", data["provider"]["sources"])
        self.assertIn("is_closed", data["market"])
        self.assertIn("last_trading_day", data["market"])
        self.assertIn("ttl", data["cache"])
        self.assertIn("stats", data["cache"])
        for key in ("numpy", "pandas", "mode"):
            self.assertIn(key, data["backend"])
        self.assertEqual(data["trade"]["mode"], "paper")
        self.assertIsInstance(data["trade"]["brokers"], dict)
        self.assertEqual(data["data_dir"], self.settings.data_dir)
        watchlist_codes = self.check_envelope(self.client.get("/api/watchlist"))["data"]["codes"]
        self.assertEqual(data["watchlist_count"], len(watchlist_codes))

    # ------------------------------------------------------------------ ② days 边界

    def test_11_kline_days_bounds(self):
        cases = {"0": 1, "-1": 1, "1.5": None, "99999": self.settings.max_kline_days}
        for raw, expected in cases.items():
            response = self.client.get("/api/market/kline?code=600519.SH&days=%s" % raw)
            self.assertNotEqual(response.status_code, 500)
            if expected is None:
                self.check_json_error(response, 400)
            else:
                rows = self.check_envelope(response)["data"]
                self.assertEqual(len(rows), expected, "days=%s" % raw)
        bad = self.client.get("/api/market/kline?code=600519.SH&days=abc")
        payload = self.check_json_error(bad, 400)
        self.assertEqual(payload["field"], "days")

    def test_12_kline_invalid_params(self):
        self.check_json_error(self.client.get("/api/market/kline"), 400)
        self.check_json_error(self.client.get("/api/market/kline?code=600519.SH&freq=hour"), 400)
        self.check_json_error(self.client.get("/api/market/kline?code=600519.SH&adjust=xx"), 400)
        self.check_json_error(self.client.get("/api/market/sectors?limit=abc"), 400)

    # ------------------------------------------------------------------ ③ 未知标的 / 非法代码

    def test_13_unknown_symbol_404_json(self):
        quote = self.client.get("/api/market/quotes?codes=999999.SH")
        payload = self.check_json_error(quote, 404)
        self.assertIn("999999.SH", payload["error"])
        kline = self.client.get("/api/market/kline?code=999999.SH")
        self.check_json_error(kline, 404)
        bad_code = self.client.get("/api/market/quotes?codes=abc,600519.SH")
        payload = self.check_json_error(bad_code, 400)
        self.assertEqual(payload["field"], "codes")

    # ------------------------------------------------------------------ ④ 未知路径

    def test_14_unknown_paths(self):
        self.check_json_error(self.client.get("/api/does-not-exist"), 404)
        self.check_json_error(self.client.put("/api/health"), 405)

        missing_static = self.client.get("/definitely-missing.css")
        self.assertEqual(missing_static.status_code, 404)
        self.assertNotIn("application/json", missing_static.content_type or "")

        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("text/html", index.content_type or "")

    # ------------------------------------------------------------------ ⑤ 策略增删

    def test_15_strategy_crud(self):
        created = self.check_envelope(
            self.client.post(
                "/api/strategies",
                json={
                    "id": "user_test1",
                    "name": "我的测试策略",
                    "category": "趋势跟踪",
                    "status": "paused",
                    "freq": "日线",
                    "universe": "沪深300",
                    "universe_type": "multi",
                    "desc": "单元测试创建",
                },
            ),
            status=201,
        )
        self.assertEqual(created["data"]["id"], "user_test1")
        self.assertFalse(created["data"]["builtin"])
        self.assertIn(
            "user_test1",
            [item["id"] for item in self.check_envelope(self.client.get("/api/strategies"))["data"]],
        )
        detail = self.check_envelope(self.client.get("/api/strategies/user_test1"))
        self.assertEqual(detail["data"]["name"], "我的测试策略")

        # 用户策略可直接回测（按默认模板 st_ma_cross 构造）
        user_backtest = self.check_envelope(self.client.get("/api/backtest/user_test1"))
        self.assertTrue(user_backtest["data"]["metrics"])
        self.assertEqual(user_backtest["data"]["strategy"]["id"], "user_test1")
        deleted = self.check_envelope(self.client.delete("/api/strategies/user_test1"))
        self.assertEqual(deleted["data"], {"id": "user_test1", "deleted": True})
        self.check_json_error(self.client.delete("/api/strategies/user_test1"), 404)
        self.check_json_error(self.client.delete("/api/strategies/st_ma_cross"), 403)

        self.check_json_error(self.client.post("/api/strategies", data="{bad json"), 400)
        bad = self.check_json_error(self.client.post("/api/strategies", json={"name": "x"}), 400)
        self.assertTrue(bad["error"])

    # ------------------------------------------------------------------ ⑥ 回测 POST / 缓存 / 错误

    def test_16_backtest_get_with_params_and_cache(self):
        path = (
            "/api/backtest/st_ma_cross?start=2024-01-01&end=2024-06-30&cash=500000"
            "&symbols=600519.SH&adjust=qfq&slippage_bps=3&short_ma=5"
        )
        before = self.engine.calls
        first = self.check_envelope(self.client.get(path))["data"]
        self.assertEqual(self.engine.calls, before + 1)
        self.assertTrue(first["nav"])
        self.assertIn("metrics", first)
        second = self.check_envelope(self.client.get(path))["data"]
        self.assertEqual(self.engine.calls, before + 1, "相同参数应命中缓存")
        self.assertEqual(first["metrics"]["trade_count"], second["metrics"]["trade_count"])

    def test_17_backtest_post(self):
        payload = self.check_envelope(
            self.client.post(
                "/api/backtest/st_ma_cross",
                json={
                    "start": "2024-01-01",
                    "end": "2024-06-30",
                    "cash": 800000,
                    "benchmark": "000300.SH",
                    "symbols": ["600519.SH", "300750.SZ"],
                    "params": {"short_ma": 10},
                },
            )
        )["data"]
        self.assertEqual(payload["request"]["initial_cash"], 800000.0)
        self.assertEqual(payload["request"]["symbols"], ["600519.SH", "300750.SZ"])
        self.assertEqual(payload["request"]["params_override"].get("short_ma"), 10)
        self.assertTrue(payload["metrics"])

        self.check_json_error(self.client.post("/api/backtest/st_ma_cross", data="not-json"), 400)
        self.check_json_error(self.client.get("/api/backtest/no_such_strategy"), 404)
        insufficient = self.check_json_error(self.client.get("/api/backtest/st_insufficient"), 400)
        self.assertIn("数据不足", insufficient["error"])
        self.check_json_error(
            self.client.get("/api/backtest/st_ma_cross?symbols=abc"), 400
        )
        self.check_json_error(
            self.client.get("/api/backtest/st_ma_cross?commission_rate=abc"), 400
        )

    # ------------------------------------------------------------------ ⑦ 持仓与模拟盘

    def test_18_portfolio_writes(self):
        created = self.check_envelope(
            self.client.post(
                "/api/portfolio/holdings",
                json={"code": "300750.SZ", "name": "宁德时代", "qty": 200, "cost": 250.5},
            ),
            status=201,
        )
        self.assertEqual(created["data"]["code"], "300750.SZ")
        self.assertEqual(created["data"]["qty"], 200)

        self.check_json_error(
            self.client.post("/api/portfolio/holdings", json={"code": "300750.SZ", "qty": -5}), 400
        )
        self.check_json_error(self.client.post("/api/portfolio/holdings", json={"qty": 100}), 400)
        self.check_json_error(self.client.post("/api/portfolio/holdings", data="{bad"), 400)

        deleted = self.check_envelope(self.client.delete("/api/portfolio/holdings/300750.SZ"))
        self.assertEqual(deleted["data"], {"code": "300750.SZ", "deleted": True})
        self.check_json_error(self.client.delete("/api/portfolio/holdings/300750.SZ"), 404)

        cash = self.check_envelope(self.client.post("/api/portfolio/cash", json={"amount": 12345}))
        self.assertEqual(cash["data"]["cash"], 12345.0)
        self.check_json_error(self.client.post("/api/portfolio/cash", json={"amount": -1}), 400)
        self.check_json_error(self.client.post("/api/portfolio/cash", json={}), 400)

        mode = self.check_envelope(self.client.post("/api/portfolio/mode", json={"mode": "paper"}))
        self.assertEqual(mode["data"], {"mode": "paper"})
        paper_overview = self.check_envelope(self.client.get("/api/portfolio/overview"))["data"]
        self.assertEqual(paper_overview["total_assets"], 1_160_000.0)
        back = self.check_envelope(self.client.post("/api/portfolio/mode", json={"mode": "manual"}))
        self.assertEqual(back["data"], {"mode": "manual"})
        self.check_json_error(self.client.post("/api/portfolio/mode", json={"mode": "x"}), 400)

    def test_19_orders_flow(self):
        submitted = self.check_envelope(
            self.client.post("/api/orders", json={"code": "600519.SH", "side": "buy", "qty": 100}),
            status=201,
        )
        self.assertEqual(submitted["data"]["order"]["status"], "filled")
        self.assertIsNotNone(submitted["data"]["fill"])
        self.assertEqual(submitted["data"]["fill"]["qty"], 100)

        pending = self.check_envelope(
            self.client.post(
                "/api/orders", json={"code": "600519.SH", "side": "buy", "qty": 100, "price": 50}
            ),
            status=201,
        )["data"]
        self.assertEqual(pending["order"]["status"], "new")
        self.assertIsNone(pending["fill"])

        orders = self.check_envelope(self.client.get("/api/orders?limit=10"))["data"]
        self.assertGreaterEqual(len(orders), 2)
        fills = self.check_envelope(self.client.get("/api/fills?limit=10"))["data"]
        self.assertTrue(fills)

        cancelled = self.check_envelope(
            self.client.delete("/api/orders/%s" % pending["order"]["order_id"])
        )
        self.assertEqual(cancelled["data"]["status"], "cancelled")
        self.check_json_error(self.client.delete("/api/orders/NOPE"), 404)

        self.check_json_error(
            self.client.post("/api/orders", json={"code": "600519.SH", "side": "hold", "qty": 100}), 400
        )
        self.check_json_error(
            self.client.post("/api/orders", json={"code": "600519.SH", "side": "buy", "qty": 0}), 400
        )
        self.check_json_error(self.client.post("/api/orders", data="{bad"), 400)

        reset = self.check_envelope(
            self.client.post("/api/orders/reset", json={"initial_cash": 2000000})
        )["data"]
        self.assertEqual(reset["cash"], 2000000.0)
        empty_reset = self.check_envelope(self.client.post("/api/orders/reset"))
        self.assertIn("cash", empty_reset["data"])
        self.check_json_error(self.client.post("/api/orders/reset", data="{bad"), 400)

    # ------------------------------------------------------------------ ⑧ 所有 /api/ 错误均为 JSON

    def test_20_all_api_errors_are_json(self):
        cases = [
            ("GET", "/api/does-not-exist", None),
            ("GET", "/api/health/extra", None),
            ("POST", "/api/watchlist", "{bad json"),
            ("POST", "/api/orders/reset", "[1,2,3]"),
            ("DELETE", "/api/watchlist/not-a-code", None),
            ("GET", "/api/market/kline?code=600519.SH&days=abc", None),
            ("GET", "/api/market/quotes?codes=bad", None),
            ("GET", "/api/market/quotes?codes=999999.SH", None),
            ("GET", "/api/strategies/nope", None),
            ("DELETE", "/api/strategies/st_ma_cross", None),
            ("GET", "/api/backtest/nope", None),
            ("POST", "/api/backtest/st_ma_cross", "{bad"),
            ("DELETE", "/api/portfolio/holdings/999999.SH", None),
            ("POST", "/api/portfolio/holdings", "{bad"),
            ("POST", "/api/portfolio/mode", json.dumps({"mode": "invalid"})),
            ("POST", "/api/orders", json.dumps({"code": "600519.SH", "side": "buy", "qty": -1})),
            ("DELETE", "/api/orders/nope", None),
        ]
        for method, path, raw in cases:
            client_method = getattr(self.client, method.lower())
            if raw is None:
                response = client_method(path)
            else:
                response = client_method(path, data=raw, content_type="application/json")
            self.assertGreaterEqual(response.status_code, 400, "%s %s" % (method, path))
            self.assertIn(
                "application/json", response.content_type or "", "%s %s" % (method, path)
            )
            payload = self.body(response)
            self.assertIsInstance(payload.get("error"), str, "%s %s" % (method, path))
            self.assertIsInstance(payload.get("code"), int, "%s %s" % (method, path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
