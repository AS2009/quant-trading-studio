# -*- coding: utf-8 -*-
"""回测**请求级参数**的回归测试：``fill_mode``（成交价模型）与 ``risk_free_rate``（无风险利率）。

背景（修复前的真问题）
----------------------
``BacktestEngine`` 有 ``fill_mode`` / ``risk_free_rate`` 两个构造参数，``broker`` 与
``metrics`` 也都会用，但 **服务层构造引擎时从不传**（``BacktestService.engine`` 只传
provider + calendar），而 ``BacktestService`` 又是进程级单例、引擎只构造一次 —— 结果：

- README 说「默认市价单以当日收盘价成交（可按需改为开盘价模型）」，实际**够不到**；
- ``risk_free_rate`` 恒为 0，夏普/索提诺分子永远按 rf=0 算。

修复后两者都是**请求级**参数（写进引擎实例，已进入缓存 key），本文件把行为钉死。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio.backtest.engine import BacktestEngine          # noqa: E402
from quantstudio.config import Settings                          # noqa: E402
from quantstudio.core.errors import ValidationError              # noqa: E402
from quantstudio.core.models import Bar                          # noqa: E402
from quantstudio.services.backtest_service import (              # noqa: E402
    FILL_MODES,
    BacktestService,
)
from quantstudio.strategies.base import BaseStrategy             # noqa: E402


def weekday_series(start: str, count: int) -> List[str]:
    anchor = date.fromisoformat(start)
    days: List[str] = []
    while len(days) < count:
        if anchor.weekday() < 5:
            days.append(anchor.isoformat())
        anchor += timedelta(days=1)
    return days


class _FixedProvider:
    """确定性行情桩：每天线性上涨，便于断言成交价落在开/收哪一侧。"""

    name = "fixed"

    def __init__(self, days: List[str]):
        self.days = days

    def kline(self, symbol, days=250, freq="day", adjust="qfq", start=None, end=None):
        bars: List[Bar] = []
        for index, day in enumerate(self.days):
            close = 100.0 + index
            bars.append(Bar(date=day, open=close - 1.0, high=close + 1.0, low=close - 2.0,
                            close=close, volume_wan=1000.0, amount_yi=1.0,
                            change_pct=0.0, turnover_pct=1.0))
        if start or end:
            bars = [bar for bar in bars
                    if (not start or bar.date >= start) and (not end or bar.date <= end)]
        return bars[-int(days):] if days and int(days) > 0 else bars

    def latest_quotes(self, symbols):
        return []

    def index_quotes(self, symbols):
        return []

    def sectors(self, limit=20):
        return []

    def breadth(self):
        return None

    def resolve_name(self, symbol):
        return symbol

    def health(self):
        return {"ok": True, "detail": "fixed", "latency_ms": 0}


class _BuyOnceStrategy(BaseStrategy):
    """在第 3 个 bar 买入一次，之后不动 —— 用来观察成交价模型。"""

    id = "fixed_test"
    name = "买入一次"
    category = "自定义"
    desc = "测试用：第 3 个 bar 买入一次"
    min_bars = 2
    version = "test"
    default_params: Dict[str, object] = {}

    @classmethod
    def param_schema(cls):
        return {}

    def __init__(self, params=None, symbols=None, spec=None):
        super().__init__(params=params, symbols=symbols, spec=spec)
        self.day = 0

    def on_start(self, ctx):
        pass

    def on_bar(self, ctx, bars):
        from quantstudio.core.models import OrderRequest
        self.day += 1
        code = self.symbols[0] if self.symbols else ""
        if self.day == 3 and not ctx.position(code):
            return [OrderRequest(code=code, side="buy", qty=100, reason="test")]
        return []

    def on_finish(self, ctx):
        pass


def _register_probe_strategy():
    """把探针策略注册进全局注册表，返回恢复函数（测试后必须还原，避免影响其它用例）。"""
    from quantstudio.strategies import registry
    previous = registry.REGISTRY.get(_BuyOnceStrategy.id)
    previous_order = list(registry.BUILTIN_ORDER)
    registry.register_builtin(_BuyOnceStrategy)

    def restore():
        registry.BUILTIN_ORDER[:] = previous_order
        if previous is None:
            registry.REGISTRY.pop(_BuyOnceStrategy.id, None)
        else:
            registry.REGISTRY[_BuyOnceStrategy.id] = previous

    return restore


class TestFillModeAndRiskFreeRate(unittest.TestCase):
    """引擎层：fill_mode 决定成交价取开盘还是收盘。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-fillmode-")
        self.days = weekday_series("2024-01-01", 40)
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        from quantstudio.core.models import BacktestRequest
        self.request = BacktestRequest(strategy_id="fixed_test", symbols=["600519.SH"],
                                       start=self.days[0], end=self.days[-1],
                                       initial_cash=1_000_000.0, benchmark="")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, fill_mode: str, risk_free_rate: float = 0.0):
        provider = _FixedProvider(self.days)
        engine = BacktestEngine(provider, settings=self.settings, fill_mode=fill_mode,
                                risk_free_rate=risk_free_rate)
        return engine.run(self.request, _BuyOnceStrategy())

    def test_close_mode_fills_at_close(self):
        result = self._run("close")
        self.assertTrue(result.trades)
        trade = result.trades[0]
        # 第 3 个 bar：close = 102，open = 101；滑点 2bp 使买入价略高
        self.assertAlmostEqual(trade.price, 102.0 * 1.0002, places=2)
        self.assertGreater(trade.price, 101.9, "收盘价模型不应成交在开盘价附近")

    def test_open_mode_fills_at_open(self):
        result = self._run("open")
        self.assertTrue(result.trades)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.price, 101.0 * 1.0002, places=2)
        self.assertLess(trade.price, 102.0, "开盘价模型不应成交在收盘价附近")

    def test_two_modes_differ(self):
        self.assertNotEqual(self._run("close").trades[0].price,
                            self._run("open").trades[0].price)

    def test_risk_free_rate_lowers_sharpe(self):
        """有正收益时，抬高无风险利率必然压低夏普（分子变小）。"""
        plain = self._run("close", 0.0)
        with_rf = self._run("close", 0.05)
        if plain.metrics.annual_return_pct > 0:
            self.assertLess(with_rf.metrics.sharpe, plain.metrics.sharpe)
        else:
            self.assertGreater(with_rf.metrics.sharpe, plain.metrics.sharpe)

    def test_invalid_fill_mode_rejected_by_broker(self):
        from quantstudio.backtest.broker import SimulatedBroker
        from quantstudio.core import costs
        with self.assertRaises(ValidationError):
            SimulatedBroker(costs.normalize_fee(None), fill_mode="vwap")


class TestServiceRequestLevelParams(unittest.TestCase):
    """服务层：两个参数必须真的能传进去，且**不跨请求残留**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-svc-params-")
        self.days = weekday_series("2024-01-01", 120)
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        self.provider = _FixedProvider(self.days)
        self.service = BacktestService(provider=self.provider, settings=self.settings)
        self._restore = _register_probe_strategy()

    def tearDown(self):
        self._restore()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fill_modes_are_configurable(self):
        for mode in FILL_MODES:
            data = self.service.run("fixed_test", {
                "symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
                "cash": 1_000_000, "fill_mode": mode,
            })
            self.assertTrue(data["trades"], "每个模式都应产出成交")
            self.assertIn("metrics", data)

    def test_open_and_close_produce_different_prices(self):
        base = {"symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
                "cash": 1_000_000}
        close_run = self.service.run("fixed_test", dict(base, fill_mode="close"))
        open_run = self.service.run("fixed_test", dict(base, fill_mode="open"))
        self.assertNotEqual(close_run["trades"][0]["price"], open_run["trades"][0]["price"],
                            "请求级 fill_mode 必须生效（修复前引擎永远用构造时的 close）")

    def test_settings_do_not_leak_between_requests(self):
        """引擎按请求新建：跑过 open 之后再跑默认，必须回到 close 的行为。"""
        base = {"symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
                "cash": 1_000_000}
        first = self.service.run("fixed_test", dict(base))
        self.service.run("fixed_test", dict(base, fill_mode="open"))
        again = self.service.run("fixed_test", dict(base))
        self.assertEqual(first["trades"][0]["price"], again["trades"][0]["price"],
                         "上一次请求的 fill_mode 残留到了下一次")

    def test_shared_engine_does_not_carry_request_settings(self):
        """服务层的默认引擎不能被某个请求改掉（并发下会互相覆盖，故按请求新建）。"""
        self.service.run("fixed_test", {
            "symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
            "cash": 1_000_000, "fill_mode": "open", "risk_free_rate": 0.05,
        })
        self.assertEqual(self.service.engine.fill_mode, "close")
        self.assertEqual(self.service.engine.risk_free_rate, 0.0)

    def test_risk_free_rate_affects_sharpe_only(self):
        base = {"symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
                "cash": 1_000_000}
        zero = self.service.run("fixed_test", dict(base, risk_free_rate=0))
        high = self.service.run("fixed_test", dict(base, risk_free_rate=0.1))
        self.assertNotEqual(zero["metrics"]["sharpe"], high["metrics"]["sharpe"],
                            "risk_free_rate 必须进入夏普计算")
        self.assertAlmostEqual(zero["metrics"]["total_return_pct"],
                               high["metrics"]["total_return_pct"], places=6,
                               msg="无风险利率不应改变收益率")

    def test_invalid_values_raise_400_style_errors(self):
        base = {"symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1]}
        with self.assertRaises(ValidationError):
            self.service.run("fixed_test", dict(base, fill_mode="vwap"))
        with self.assertRaises(ValidationError):
            self.service.run("fixed_test", dict(base, risk_free_rate="2"))
        with self.assertRaises(ValidationError):
            self.service.run("fixed_test", dict(base, risk_free_rate="-0.5"))

    def test_defaults_match_legacy_behaviour(self):
        """不传参数 = 旧的收盘价 + rf=0 口径（默认值不能悄悄改变历史结果）。"""
        base = {"symbols": ["600519.SH"], "start": self.days[0], "end": self.days[-1],
                "cash": 1_000_000}
        implicit = self.service.run("fixed_test", dict(base))
        explicit = self.service.run("fixed_test", dict(base, fill_mode="close",
                                                       risk_free_rate=0))
        self.assertEqual(implicit["metrics"], explicit["metrics"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
