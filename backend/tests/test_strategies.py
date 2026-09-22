# -*- coding: utf-8 -*-
"""策略库单元测试（仅标准库 unittest）。

覆盖：6 个内置策略能否在确定性行情上跑通并产生交易、参数校验能拦截非法值、
param_schema / meta 完整性、注册表 API、用户自定义策略的持久化与构造。

行情来自本文件内的 FakeProvider（500 个交易日、7 个标的 + 1 个基准，完全确定），
不依赖网络与 ``quantstudio.data``。
"""

import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio.backtest import BacktestEngine  # noqa: E402
from quantstudio.core.errors import ValidationError  # noqa: E402
from quantstudio.core.models import BacktestRequest, Bar, StrategySpec  # noqa: E402
from quantstudio.strategies import (  # noqa: E402
    CATEGORY_CHOICES,
    REGISTRY,
    add_user_strategy,
    create,
    create_from_user,
    delete_user_strategy,
    describe,
    get_class,
    list_specs,
    load_user_items,
    load_user_specs,
)
from quantstudio.strategies.base import BaseStrategy  # noqa: E402

BUILTIN_IDS = ["st_ma_cross", "st_momentum", "st_grid", "st_lowvol", "st_rsi", "st_turtle"]

SYMBOLS = [
    "600519.SH", "000858.SZ", "300750.SZ", "600036.SH",
    "601318.SH", "600900.SH", "600276.SH", "300059.SZ",
]

# （基准价、漂移、主周期、主振幅、次周期、次振幅）：0 号强趋势（海龟突破）、1 号宽幅震荡（网格/RSI）
_PROFILES = [
    (60.0, 0.60, 62.0, 0.15, 19.0, 0.04),
    (30.0, 0.05, 55.0, 0.13, 17.0, 0.05),
    (18.0, -0.30, 70.0, 0.12, 23.0, 0.04),
    (12.0, 0.35, 58.0, 0.16, 13.0, 0.05),
    (25.0, 0.00, 66.0, 0.14, 21.0, 0.04),
    (40.0, -0.15, 61.0, 0.11, 15.0, 0.05),
    (22.0, 0.25, 52.0, 0.17, 11.0, 0.05),
    (35.0, 0.10, 64.0, 0.12, 23.0, 0.06),
]

BENCH_CODE = "000300.SH"


def trading_dates(start: str, days: int) -> List[str]:
    y, m, d = (int(part) for part in start.split("-"))
    cur = date(y, m, d)
    out: List[str] = []
    while len(out) < days:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


class FakeProvider:
    """确定性行情源（DataProvider 结构匹配；不访问网络）。"""

    name = "fake"

    def __init__(self, codes: Optional[List[str]] = None, days: int = 500, start: str = "2023-01-02"):
        self.codes = list(codes or SYMBOLS)
        for code in (BENCH_CODE,):
            if code not in self.codes:
                self.codes.append(code)
        self.days = int(days)
        self.dates = trading_dates(start, self.days)

    def series(self, symbol: str) -> List[Bar]:
        if symbol not in self.codes:
            return []
        index = self.codes.index(symbol)
        base, drift, t1, a1, t2, a2 = _PROFILES[index % len(_PROFILES)]
        bars: List[Bar] = []
        prev_close = 0.0
        for i, day in enumerate(self.dates):
            wave = a1 * math.sin(2.0 * math.pi * (i + 7.0 * index) / t1)
            fast = a2 * math.sin(2.0 * math.pi * (i + 3.0 * index) / t2)
            close = max(base * (1.0 + drift * (float(i) / self.days - 0.5) + wave + fast), 1.0)
            open_ = prev_close if prev_close > 0 else close
            change = (close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
            bars.append(Bar(
                date=day, open=round(open_, 4), high=round(max(open_, close) * 1.006, 4),
                low=round(min(open_, close) * 0.994, 4), close=round(close, 4),
                volume_wan=1000.0, amount_yi=2.0, change_pct=round(change, 4), turnover_pct=1.0,
            ))
            prev_close = close
        return bars

    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        bars = self.series(symbol)
        return bars[-int(days):] if days and int(days) > 0 else bars

    def latest_quotes(self, symbols):
        return []

    def index_quotes(self, symbols):
        return []

    def sectors(self, limit: int = 20):
        return []

    def breadth(self):
        return None

    def resolve_name(self, symbol: str) -> Optional[str]:
        return "测试-%s" % symbol

    def health(self):
        return {"ok": True, "detail": "fake", "latency_ms": 0}


REQUEST_KWARGS = {
    "start": "2020-01-01",
    "end": "2030-01-01",
    "initial_cash": 1_000_000.0,
    "benchmark": BENCH_CODE,
}


class TestRegistry(unittest.TestCase):

    def test_six_builtin_strategies_registered(self):
        """内置 6 个策略必须完整注册（strategies/local/ 下的本地策略不计入内置集合）。"""
        self.assertEqual(sorted(key for key in REGISTRY if key in BUILTIN_IDS), sorted(BUILTIN_IDS))
        builtin_specs = [spec for spec in list_specs() if spec.origin == "builtin"]
        self.assertEqual([spec.id for spec in builtin_specs], BUILTIN_IDS)

    def test_spec_fields_complete(self):
        for spec in list_specs():
            self.assertTrue(spec.id)
            self.assertTrue(spec.name)
            self.assertTrue(spec.category)
            self.assertTrue(spec.desc)
            self.assertTrue(spec.freq)
            self.assertGreaterEqual(spec.min_bars, 2)
            self.assertTrue(spec.version)
            # origin 与 builtin 必须自洽：内置策略 builtin=True；本地代码策略 builtin=False
            self.assertIn(spec.origin, ("builtin", "local"))
            self.assertEqual(spec.builtin, spec.origin == "builtin", "%s 的 origin/builtin 不一致" % spec.id)
            self.assertTrue(spec.param_schema, "%s 缺少 param_schema" % spec.id)
            self.assertTrue(spec.params, "%s 缺少默认参数" % spec.id)
            for key, meta in spec.param_schema.items():
                self.assertIn("label", meta, "%s.%s 缺少 label" % (spec.id, key))
                self.assertIn("type", meta, "%s.%s 缺少 type" % (spec.id, key))
                self.assertIn("default", meta, "%s.%s 缺少 default" % (spec.id, key))
                self.assertIn("help", meta, "%s.%s 缺少 help" % (spec.id, key))
                self.assertIn(meta["type"], ("int", "float", "str", "bool"))
                self.assertIn(key, spec.params, "%s 默认参数缺少 %s" % (spec.id, key))
                if meta["type"] in ("int", "float"):
                    self.assertIn("min", meta)
                    self.assertIn("max", meta)
                    self.assertIn("step", meta)

    def test_names_and_categories(self):
        expected = {
            "st_ma_cross": ("双均线趋势策略", "趋势跟踪"),
            "st_momentum": ("动量轮动策略", "动量"),
            "st_grid": ("网格交易策略", "震荡市"),
            "st_lowvol": ("低波动率优选策略", "稳健"),
            "st_rsi": ("RSI 均值回归策略", "均值回归"),
            "st_turtle": ("海龟突破策略", "趋势跟踪"),
        }
        for spec in list_specs():
            if spec.origin != "builtin":
                continue          # 本地策略的名称/分类由各自文件定义，不在本用例断言范围
            self.assertEqual((spec.name, spec.category), expected[spec.id])

    def test_default_params_match_documented_defaults(self):
        defaults = {spec.id: spec.params for spec in list_specs() if spec.origin == "builtin"}
        self.assertEqual(defaults["st_ma_cross"]["short_ma"], 20)
        self.assertEqual(defaults["st_ma_cross"]["long_ma"], 60)
        self.assertEqual(defaults["st_ma_cross"]["position_pct"], 0.95)
        self.assertEqual(defaults["st_momentum"]["lookback"], 20)
        self.assertEqual(defaults["st_momentum"]["top_n"], 3)
        self.assertEqual(defaults["st_momentum"]["rebalance_days"], 20)
        self.assertEqual(defaults["st_grid"]["grid_pct"], 0.03)
        self.assertEqual(defaults["st_grid"]["levels"], 10)
        self.assertEqual(defaults["st_grid"]["base_qty"], 1000)
        self.assertEqual(defaults["st_lowvol"]["lookback"], 60)
        self.assertEqual(defaults["st_lowvol"]["top_n"], 5)
        self.assertEqual(defaults["st_lowvol"]["rebalance_days"], 5)
        self.assertEqual(defaults["st_rsi"]["rsi_period"], 14)
        self.assertEqual(defaults["st_rsi"]["buy_th"], 30)
        self.assertEqual(defaults["st_rsi"]["sell_th"], 70)
        self.assertEqual(defaults["st_turtle"]["entry_high"], 20)
        self.assertEqual(defaults["st_turtle"]["exit_low"], 10)
        self.assertEqual(defaults["st_turtle"]["atr_mult"], 2.0)

    def test_get_class_and_create(self):
        cls = get_class("st_ma_cross")
        self.assertTrue(issubclass(cls, BaseStrategy))
        with self.assertRaises(ValidationError):
            get_class("st_not_exist")
        strategy = create("st_ma_cross", params={"short_ma": 10, "long_ma": 30}, symbols=["600519.SH"])
        self.assertEqual(strategy.params["short_ma"], 10)
        self.assertEqual(strategy.params["position_pct"], 0.95)      # 未覆盖的取默认值
        self.assertEqual(strategy.symbols, ["600519.SH"])
        self.assertEqual(strategy.spec.id, "st_ma_cross")
        self.assertIsInstance(strategy.spec, StrategySpec)

    def test_describe_payload(self):
        payload = describe()
        builtin = [item for item in payload if item.get("origin") == "builtin"]
        self.assertEqual(len(builtin), 6)          # 内置 6 个；local/ 下的本地策略另行计入
        self.assertGreaterEqual(len(payload), 6)
        for item in payload:
            self.assertIsInstance(item, dict)
            self.assertIn("param_schema", item)
            self.assertTrue(item["id"])
            self.assertIn("origin", item)

    def test_register_requires_base_strategy(self):
        with self.assertRaises(ValidationError):
            from quantstudio.strategies import register

            register(object)


class TestParamValidation(unittest.TestCase):

    def test_defaults_filled(self):
        for spec in list_specs():
            cls = get_class(spec.id)
            self.assertEqual(cls.validate_params({}), spec.params)
            self.assertEqual(cls.validate_params(None), spec.params)

    def test_negative_and_zero_values_rejected(self):
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"short_ma": -1})
        with self.assertRaises(ValidationError):
            get_class("st_momentum").validate_params({"top_n": 0})
        with self.assertRaises(ValidationError):
            get_class("st_rsi").validate_params({"rsi_period": -14})
        with self.assertRaises(ValidationError):
            get_class("st_grid").validate_params({"base_qty": -100})

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"position_pct": 1.5})
        with self.assertRaises(ValidationError):
            get_class("st_lowvol").validate_params({"top_n": 999})

    def test_wrong_type_rejected(self):
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"short_ma": "abc"})
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"short_ma": [20]})
        with self.assertRaises(ValidationError):
            get_class("st_momentum").validate_params({"top_n": 2.5})       # 需要整数
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"short_ma": True})   # 布尔不算数字

    def test_nan_infinity_rejected(self):
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"position_pct": float("nan")})
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"position_pct": float("inf")})

    def test_unknown_param_rejected(self):
        with self.assertRaises(ValidationError) as err:
            get_class("st_ma_cross").validate_params({"nope": 1})
        self.assertIn("nope", str(err.exception))

    def test_cross_field_rules(self):
        with self.assertRaises(ValidationError):
            get_class("st_ma_cross").validate_params({"short_ma": 80, "long_ma": 60})
        with self.assertRaises(ValidationError):
            get_class("st_rsi").validate_params({"buy_th": 80, "sell_th": 70})

    def test_numeric_string_accepted(self):
        params = get_class("st_ma_cross").validate_params({"short_ma": "10"})
        self.assertEqual(params["short_ma"], 10)

    def test_constructor_rejects_bad_params(self):
        with self.assertRaises(ValidationError):
            create("st_momentum", params={"top_n": -3})

    def test_json_round_trip_of_params(self):
        for spec in list_specs():
            text = json.dumps(spec.to_dict(), ensure_ascii=False, allow_nan=False)
            back = json.loads(text)
            self.assertEqual(back["id"], spec.id)
            self.assertEqual(back["param_schema"], spec.param_schema)


class TestIndicatorHelpers(unittest.TestCase):

    def test_ma_ema_std_pct_high_low(self):
        values = [float(i) for i in range(1, 11)]           # 1..10
        self.assertAlmostEqual(BaseStrategy.ma(values, 5), 8.0, places=9)
        self.assertIsNone(BaseStrategy.ma(values, 11))                       # 长度不足
        self.assertIsNone(BaseStrategy.ma(values, 0))
        ema = BaseStrategy.ema(values, 3)
        self.assertIsNotNone(ema)
        self.assertGreater(ema, 8.0)
        self.assertAlmostEqual(BaseStrategy.std([2.0, 4.0, 6.0], 3), 2.0, places=9)
        self.assertIsNone(BaseStrategy.std([1.0], 1))
        self.assertAlmostEqual(BaseStrategy.pct_change(values, 1), 10.0 / 9.0 - 1.0, places=9)
        self.assertAlmostEqual(BaseStrategy.highest(values, 3), 10.0, places=9)
        self.assertAlmostEqual(BaseStrategy.lowest(values, 3), 8.0, places=9)

    def test_rsi_bounds_and_insufficient(self):
        rising = [float(i) for i in range(1, 40)]
        self.assertAlmostEqual(BaseStrategy.rsi(rising, 14), 100.0, places=6)
        falling = [float(40 - i) for i in range(1, 40)]
        self.assertAlmostEqual(BaseStrategy.rsi(falling, 14), 0.0, places=6)
        self.assertIsNone(BaseStrategy.rsi([1.0, 2.0], 14))

    def test_atr(self):
        bars = [Bar(date="d%d" % i, open=10.0, high=11.0 + i, low=9.0 + i, close=10.0 + i) for i in range(20)]
        value = BaseStrategy.atr(bars, 14)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0.0)
        self.assertIsNone(BaseStrategy.atr(bars[:5], 14))


class TestStrategiesRunRealBacktest(unittest.TestCase):
    """每个内置策略跑一次完整回测：必须产生交易且无异常。"""

    def _run(self, strategy_id: str, symbols: List[str], params: Optional[Dict] = None):
        provider = FakeProvider()
        strategy = create(strategy_id, params=params, symbols=symbols)
        request = BacktestRequest(strategy_id=strategy_id, symbols=list(symbols), **REQUEST_KWARGS)
        result = BacktestEngine(provider).run(request, strategy)
        self.assertEqual(len(result.nav), len(result.equity))
        self.assertAlmostEqual(result.nav[0]["strategy"], 1.0, places=6)
        self.assertEqual(result.metrics.trading_days, len(result.equity) - 1)
        for key, value in result.metrics.to_dict().items():
            if isinstance(value, float):
                self.assertFalse(math.isnan(value), "%s: %s 为 NaN" % (strategy_id, key))
                self.assertFalse(math.isinf(value), "%s: %s 为 Infinity" % (strategy_id, key))
        json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)
        return result, strategy

    def test_ma_cross(self):
        result, strategy = self._run("st_ma_cross", ["600519.SH"])
        self.assertGreater(len(result.trades), 0, "双均线策略应产生交易")
        self.assertTrue(any(t.side == "sell" for t in result.trades), "应出现金叉买入后的死叉卖出")
        self.assertIn("上穿", "".join(t.reason for t in result.trades))

    def test_momentum(self):
        result, _ = self._run("st_momentum", SYMBOLS[:6])
        self.assertGreater(len(result.trades), 0)
        self.assertTrue(any(t.side == "sell" for t in result.trades), "动量轮动应出现调出卖出")
        self.assertLessEqual(len(result.positions), 3, "持仓不应超过 top_n")
        self.assertTrue(any("动量排名" in t.reason for t in result.trades))

    def test_grid(self):
        result, strategy = self._run("st_grid", ["000858.SZ"])
        self.assertGreater(len(result.trades), 0, "网格策略应产生交易")
        self.assertTrue(any(t.side == "buy" for t in result.trades))
        self.assertTrue(any("网格" in t.reason for t in result.trades))

    def test_low_vol(self):
        result, _ = self._run("st_lowvol", SYMBOLS, params={"top_n": 3})
        self.assertGreater(len(result.trades), 0)
        self.assertTrue(any(t.side == "sell" for t in result.trades), "调仓应产生卖出")
        self.assertLessEqual(len(result.positions), 3)

    def test_rsi(self):
        result, _ = self._run("st_rsi", ["600519.SH", "000858.SZ"])
        self.assertGreater(len(result.trades), 0, "RSI 策略应产生交易")
        self.assertTrue(any("RSI" in t.reason for t in result.trades))

    def test_turtle(self):
        result, _ = self._run("st_turtle", ["600519.SH"])
        self.assertGreater(len(result.trades), 0, "海龟策略应产生突破交易")
        self.assertTrue(any("突破" in t.reason for t in result.trades))

    def test_ma_cross_params_override_takes_effect(self):
        fast, _ = self._run("st_ma_cross", ["600519.SH"], params={"short_ma": 5, "long_ma": 20, "position_pct": 0.5})
        slow, _ = self._run("st_ma_cross", ["600519.SH"], params={"short_ma": 20, "long_ma": 60, "position_pct": 0.5})
        self.assertNotEqual([t.date for t in fast.trades], [t.date for t in slow.trades])

    def test_multi_symbol_strategies_are_deterministic(self):
        first, _ = self._run("st_momentum", SYMBOLS[:6])
        second, _ = self._run("st_momentum", SYMBOLS[:6])
        self.assertEqual([t.to_dict() for t in first.trades], [t.to_dict() for t in second.trades])
        self.assertEqual(first.metrics.to_dict(), second.metrics.to_dict())

    def test_default_symbols_used_when_missing(self):
        strategy = create("st_momentum")
        self.assertTrue(strategy.symbols, "缺少 symbols 时应使用策略建议池")
        provider = FakeProvider(codes=list(strategy.symbols) + [BENCH_CODE])
        request = BacktestRequest(strategy_id="st_momentum", **REQUEST_KWARGS)
        result = BacktestEngine(provider).run(request, strategy)
        self.assertGreater(len(result.equity), 0)
        traded = set(t.code for t in result.trades)
        self.assertTrue(traded.issubset(set(strategy.symbols)), "成交标的必须来自策略标的池")


class TestUserStrategies(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("PI_SCRATCH_DIR") or tempfile.gettempdir()
        self.dir = tempfile.mkdtemp(prefix="qs_users_", dir=base)
        self.path = os.path.join(self.dir, "user_strategies.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _payload(self, **overrides):
        payload = {
            "name": "我的双均线",
            "category": "趋势跟踪",
            "desc": "MA10/MA30 更灵敏的版本",
            "params": {"short_ma": 10, "long_ma": 30, "position_pct": 0.8},
            "status": "running",
            "universe": "自选股",
            "freq": "日线",
            "template": "st_ma_cross",
        }
        payload.update(overrides)
        return payload

    def test_add_load_delete_round_trip(self):
        spec = add_user_strategy(self.path, self._payload())
        self.assertFalse(spec.builtin)
        self.assertEqual(spec.name, "我的双均线")
        self.assertEqual(spec.params["short_ma"], 10)
        self.assertTrue(spec.id.startswith("us_"))
        self.assertTrue(os.path.exists(self.path))

        items = load_user_items(self.path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["template"], "st_ma_cross")

        specs = load_user_specs(self.path)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].id, spec.id)
        self.assertEqual(specs[0].param_schema["short_ma"]["default"], 20)

        self.assertTrue(delete_user_strategy(self.path, spec.id))
        self.assertEqual(load_user_specs(self.path), [])

    def test_name_rules(self):
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(name="短"))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(name="这个策略名称实在是太长了超过二十四个字符一定会被拒绝的"))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(name=123))
        add_user_strategy(self.path, self._payload())
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(name="我的双均线"))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(name="我的双均线", id="us_other"))

    def test_category_status_freq_rules(self):
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(category="玄学"))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(status="flying"))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(freq="分钟线"))
        spec = add_user_strategy(self.path, self._payload(category="自定义", status="paused", freq="周度"))
        self.assertEqual(spec.category, "自定义")
        self.assertEqual(spec.status, "paused")
        self.assertIn(spec.category, CATEGORY_CHOICES)

    def test_params_rules(self):
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(params={"unknown_key": 1}))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(params={"short_ma": -5}))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(params={"short_ma": "abc"}))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(params={"position_pct": float("nan")}))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(params=[1, 2, 3]))
        with self.assertRaises(ValidationError):
            add_user_strategy(self.path, self._payload(template="st_not_exist"))
        spec = add_user_strategy(self.path, self._payload(params={}))
        self.assertEqual(spec.params["long_ma"], 60, "缺省参数应取模板默认值")

    def test_builtin_cannot_be_deleted(self):
        with self.assertRaises(ValidationError):
            delete_user_strategy(self.path, "st_ma_cross")
        self.assertFalse(delete_user_strategy(self.path, "us_not_exists"))

    def test_load_tolerates_broken_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json ")
        self.assertEqual(load_user_specs(self.path), [])
        self.assertEqual(load_user_items(self.path), [])
        # 损坏的文件不应阻塞新增（会被覆盖为合法 JSON）
        spec = add_user_strategy(self.path, self._payload())
        self.assertEqual(len(load_user_specs(self.path)), 1)
        self.assertEqual(load_user_items(self.path)[0]["id"], spec.id)

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_user_specs(os.path.join(self.dir, "nope.json")), [])

    def test_create_from_user_runs_backtest(self):
        spec = add_user_strategy(self.path, self._payload())
        item = load_user_items(self.path)[0]
        strategy = create_from_user(item, symbols=["600519.SH"])
        self.assertEqual(strategy.params["short_ma"], 10)
        self.assertEqual(strategy.spec.id, spec.id)
        self.assertFalse(strategy.spec.builtin)
        request = BacktestRequest(strategy_id=spec.id, symbols=["600519.SH"], **REQUEST_KWARGS)
        result = BacktestEngine(FakeProvider()).run(request, strategy)
        self.assertGreater(len(result.trades), 0)
        self.assertEqual(result.strategy.id, spec.id)
        self.assertEqual(result.strategy.name, "我的双均线")

    def test_atomic_write_leaves_no_temp_files(self):
        add_user_strategy(self.path, self._payload())
        leftovers = [name for name in os.listdir(self.dir) if name.startswith(".user_strategies_")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
