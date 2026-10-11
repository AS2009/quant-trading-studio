# -*- coding: utf-8 -*-
"""掘金（gm.api）策略转换器的回归测试。

覆盖三件事：
1. **判定准确性** —— 分析器只能认平台能力，不能把 `print` / `len` / `x.append` 当阻断项
   （早期版本就因为误报把「布林线均值回归」误判为 blocked）；
2. **产物诚实性** —— blocked 的策略必须**显式报错**而不是静默产出 0 笔交易；
3. **转换正确性** —— 已转换的布林线策略，指标口径必须与掘金原版（pandas 样本标准差）逐值一致。
"""

import ast
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio.core.models import Bar, StrategySpec            # noqa: E402
from quantstudio.strategies import gm_compat as G                # noqa: E402

#: 只用本项目**有**的能力：应当判为可翻译
FIDELITY_SRC = '''
from gm.api import *
from datetime import datetime

def init(context):
    context.symbol = 'SHSE.600004'
    context.n = 20

def on_bar(context, bars):
    data = context.data(symbol=context.symbol, frequency='1d', count=context.n, fields='close')
    closes = list(data['close'].values)
    pos = get_position()
    if len(closes) < context.n:
        return
    if not pos:
        order_volume(symbol=context.symbol, volume=100, side=OrderSide_Buy,
                     order_type=OrderType_Market, position_effect=PositionEffect_Open)
    else:
        order_volume(symbol=context.symbol, volume=100, side=OrderSide_Sell,
                     order_type=OrderType_Market, position_effect=PositionEffect_Close)
'''

#: 依赖本项目没有的数据能力：必须判为 blocked
BLOCKED_SRC = '''
from gm.api import *

def init(context):
    context.num = 10

def algo(context):
    stock300 = stk_get_index_constituents(index='SHSE.000300')
    fin = stk_get_daily_mktvalue_pt(symbols=list(stock300), fields='tot_mv')
    order_target_percent(symbol='SHSE.600000', percent=0.1)
'''

#: 混入 pandas / numpy 与一个**不支持的平台能力**：语义有损但可翻
APPROX_SRC = '''
from gm.api import *
import pandas as pd
import numpy as np

def init(context):
    context.symbol = 'SHSE.600000'

def on_bar(context, bars):
    df = context.data(symbol=context.symbol, frequency='1d', count=30, fields='close')
    mean = df['close'].rolling(26).mean().values[-1]
    std = np.std(df['close'].values)
    price = current(symbols=context.symbol)[0]['price']
    print(mean, std, price, len(df))
'''


class AnalyzerPrecisionTest(unittest.TestCase):
    """分析器只认平台能力，不能被内置函数/属性链干扰。"""

    def _usage(self, source):
        return G.analyze("inline.py", source)

    def test_builtins_are_not_findings(self):
        """`print` / `len` / `list` / `filter` / `str` 不是平台能力，不得进 findings。"""
        usage = self._usage(APPROX_SRC)
        names = {item["name"] for item in usage.findings()}
        for builtin in ("print", "len", "list", "filter", "str", "max", "min", "set", "range"):
            self.assertNotIn(builtin, names, "内置函数 %s 不应被当成平台能力" % builtin)

    def test_attribute_chains_are_not_findings(self):
        """`x.append(...)` / `context.now.strftime(...)` 不是 gm.api 顶层函数。"""
        usage = self._usage('''
from gm.api import *
def on_bar(context, bars):
    rows = []
    rows.append(1)
    text = context.now.strftime('%Y-%m-%d')
    print(text)
''')
        names = {item["name"] for item in usage.findings()}
        for noise in ("append", "strftime", "rows.append", "context.now.strftime"):
            self.assertNotIn(noise, names)

    def test_fidelity_when_only_supported_api_used(self):
        usage = self._usage(FIDELITY_SRC)
        self.assertEqual(usage.level(), G.FIDELITY, usage.findings())

    def test_blocked_when_data_capability_missing(self):
        usage = self._usage(BLOCKED_SRC)
        self.assertEqual(usage.level(), G.BLOCKED)
        blocked = {item["name"] for item in usage.findings() if item["level"] == G.BLOCKED}
        self.assertIn("stk_get_index_constituents", blocked)
        self.assertIn("stk_get_daily_mktvalue_pt", blocked)

    def test_approximate_for_pandas_numpy(self):
        usage = self._usage(APPROX_SRC)
        self.assertEqual(usage.level(), G.APPROXIMATE)

    def test_context_method_call_is_recognized(self):
        """`context.data(...)` 必须被识别为 context 成员（否则会漏判取数方式）。"""
        usage = self._usage(FIDELITY_SRC)
        self.assertIn("context.data", usage.calls)

    def test_syntax_error_is_reported_not_raised(self):
        usage = self._usage("def broken(:\n    pass\n")
        self.assertTrue(usage.syntax_error)
        self.assertEqual(usage.level(), G.BLOCKED)
        self.assertIn("语法错误", G.render_report(usage))

    def test_params_guessed_from_init_assignments(self):
        usage = self._usage(FIDELITY_SRC)
        params = G._guess_params(usage)
        self.assertEqual(params.get("n"), 20)
        self.assertEqual(params.get("symbol"), "SHSE.600004")


class SlugStabilityTest(unittest.TestCase):
    """slug 必须稳定且可读（它同时是文件名与策略 id）。"""

    def test_chinese_names_get_readable_pinyin(self):
        self.assertEqual(G._slug("风格轮动(股票)"), "feng_ge_lun_dong_gu_piao")
        self.assertEqual(G._slug("小市值策略"), "xiao_shi_zhi_ce_lue")

    def test_slug_is_deterministic_across_calls(self):
        for name in ("风格轮动(股票)", "布林线均值回归", "完全未知的策略名XYZ"):
            self.assertEqual(G._slug(name), G._slug(name))

    def test_brief_truncates_on_word_boundary(self):
        slug = G._slug("布林线均值回归", brief=12)
        self.assertLessEqual(len(slug), 12)
        self.assertFalse(slug.endswith("_"))
        self.assertNotIn("__", slug)

    def test_unknown_chars_fall_back_without_crashing(self):
        slug = G._slug("策略αβγ")
        self.assertTrue(slug)
        self.assertTrue(all(char.isalnum() or char == "_" for char in slug))


class CliEndToEndTest(unittest.TestCase):
    """`scripts/import_strategy.py` 的端到端行为（不联网、不依赖真实源文件）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-gm-cli-")
        self.src_dir = os.path.join(self.tmp, "src")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.src_dir, exist_ok=True)
        # 两个同名策略（中文名不同但 slug 相同）+ 一个 blocked
        for name, body in (("风格轮动(股票)", FIDELITY_SRC), ("风格轮动(股票)(算法交易)", FIDELITY_SRC),
                           ("多因子选股", BLOCKED_SRC)):
            with open(os.path.join(self.src_dir, "%s.py" % name), "w", encoding="utf-8") as handle:
                handle.write(body)
        self._import_cli()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _import_cli():
        """按路径加载 scripts/import_strategy.py（它不是包，只能按文件加载）。"""
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "scripts", "import_strategy.py")
        spec = importlib.util.spec_from_file_location("qs_import_strategy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _run_cli(module, argv):
        """跑 CLI 并把 stdout/stderr 都吞掉（避免测试日志里全是报告与提示）。"""
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = module.main(argv)
        return code, buffer.getvalue()

    def test_batch_conversion_does_not_overwrite_duplicate_slugs(self):
        """同名策略必须各留一份产物 —— 早期版本会互相覆盖，9 个文件只剩 8 个。"""
        cli = self._import_cli()
        code, _out = self._run_cli(cli, ["--dir", self.src_dir, "--all", "--out", self.out_dir,
                                         "--quiet"])
        produced = sorted(name for name in os.listdir(self.out_dir) if name.endswith(".py"))
        self.assertEqual(len(produced), 3, "3 个源文件应产出 3 个产物，实际 %s" % produced)
        self.assertIn("feng_ge_lun_dong_gu_piao.py", produced)
        self.assertIn("feng_ge_lun_dong_gu_piao_2.py", produced)
        self.assertEqual(code, 1, "存在 blocked 文件时退出码应为 1")

    def test_batch_writes_report_and_summary(self):
        cli = self._import_cli()
        self._run_cli(cli, ["--dir", self.src_dir, "--all", "--out", self.out_dir, "--quiet"])
        report = os.path.join(self.out_dir, "CONVERSION-REPORT.md")
        self.assertTrue(os.path.exists(report))
        with open(report, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("多因子选股", text)
        self.assertIn("⛔", text)

    def test_all_supported_files_exit_zero(self):
        cli = self._import_cli()
        only_ok = os.path.join(self.tmp, "ok")
        os.makedirs(only_ok, exist_ok=True)
        with open(os.path.join(only_ok, "good.py"), "w", encoding="utf-8") as handle:
            handle.write(FIDELITY_SRC)
        code, _out = self._run_cli(cli, ["--dir", only_ok, "--all", "--out", self.out_dir,
                                         "--quiet"])
        self.assertEqual(code, 0, "全部可转换时应返回 0")

    def test_dry_run_writes_nothing(self):
        cli = self._import_cli()
        empty_out = os.path.join(self.tmp, "dry")
        code, _out = self._run_cli(cli, [os.path.join(self.src_dir, "多因子选股.py"),
                                         "--out", empty_out, "--dry-run", "--quiet"])
        self.assertEqual(code, 1, "blocked 的 dry-run 仍应报 1")
        self.assertFalse(os.path.exists(empty_out), "dry-run 不得创建输出目录")

    def test_missing_source_returns_usage_error(self):
        cli = self._import_cli()
        code, _out = self._run_cli(cli, ["--out", self.out_dir, "--quiet"])
        self.assertEqual(code, 2)

    def test_catalog_lists_levels(self):
        cli = self._import_cli()
        code, out = self._run_cli(cli, ["--list"])
        self.assertEqual(code, 0)
        for level in ("fidelity", "approximate", "blocked"):
            self.assertIn(level, out)


class ConvertOutputTest(unittest.TestCase):
    """产物必须语法合法、诚实、且不会被自动加载。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qts-gm-")
        self.src_fidelity = os.path.join(self.tmp, "fidelity_strategy.py")
        self.src_blocked = os.path.join(self.tmp, "blocked_strategy.py")
        with open(self.src_fidelity, "w", encoding="utf-8") as handle:
            handle.write(FIDELITY_SRC)
        with open(self.src_blocked, "w", encoding="utf-8") as handle:
            handle.write(BLOCKED_SRC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generated_file_is_valid_python(self):
        target, _report, _level = G.convert(self.src_fidelity, self.tmp)
        with open(target, "r", encoding="utf-8") as handle:
            ast.parse(handle.read())            # 语法不合法会直接抛

    def test_conversion_is_idempotent(self):
        """同一源文件反复转换：文件名稳定；除「生成时间」外内容一致且同步更新判定。"""
        first, _r1, _l1 = G.convert(self.src_fidelity, self.tmp)
        second, _r2, _l2 = G.convert(self.src_fidelity, self.tmp)
        self.assertEqual(first, second, "同一源文件必须产出同一个文件名")

        def without_stamp(path):
            with open(path, "r", encoding="utf-8") as handle:
                return "\n".join(line for line in handle.read().splitlines()
                                 if not line.startswith("- 生成时间："))

        self.assertEqual(without_stamp(first), without_stamp(second))

    def test_blocked_output_raises_instead_of_silently_trading(self):
        """blocked 的策略 on_bar 必须抛 NotImplementedError —— 静默 0 笔交易更危险。"""
        target, _report, level = G.convert(self.src_blocked, self.tmp)
        self.assertEqual(level, G.BLOCKED)
        with open(target, "r", encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn("_unsupported()", code)
        # 真的实例化并调一次 on_bar，确认它吵
        namespace = {}
        exec(compile(code.replace("from ...core.models", "from quantstudio.core.models")
                     .replace("from ..base", "from quantstudio.strategies.base"),
                     target, "exec"), namespace)
        cls = next(value for value in namespace.values()
                   if isinstance(value, type) and issubclass(value, G_BaseStrategy())
                   and value is not G_BaseStrategy())
        strategy = cls({})
        with self.assertRaises(NotImplementedError) as ctx:
            strategy.on_bar(_DummyCtx(), {})
        self.assertIn("不支持", str(ctx.exception))

    def test_report_lists_blockers_and_target(self):
        target, report, level = G.convert(self.src_blocked, self.tmp)
        self.assertEqual(level, G.BLOCKED)
        self.assertIn("stk_get_index_constituents", report)
        self.assertIn("⛔", report)
        self.assertIn(target, report)

    def test_report_marks_approximate_loss(self):
        path = os.path.join(self.tmp, "approx.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(APPROX_SRC)
        _target, report, level = G.convert(path, self.tmp)
        self.assertEqual(level, G.APPROXIMATE)
        self.assertIn("语义有损", report)
        self.assertIn("pandas", report)


def G_BaseStrategy():
    from quantstudio.strategies.base import BaseStrategy
    return BaseStrategy


class _DummyCtx:
    """给 blocked 策略用的最小上下文（on_bar 会先抛错，不会真的用到）。"""

    today = "2024-01-02"

    def history(self, code, field="close", n=60):
        return []

    def position(self, code):
        return None

    def positions(self):
        return []

    def cash(self):
        return 0.0

    def total_assets(self):
        return 0.0

    def bars_since(self, code, n):
        return []

    def log(self, message):
        pass


class ConvertedBollStrategyTest(unittest.TestCase):
    """已转换的布林线策略：口径必须与掘金原版（pandas 样本标准差）一致。"""

    def setUp(self):
        from quantstudio.strategies.local.gm_boll_reversion import GmBollReversionStrategy
        self.cls = GmBollReversionStrategy

    def test_metadata_and_lint_shape(self):
        spec = self.cls.meta()
        self.assertTrue(spec.id.startswith("st_"))
        self.assertEqual(spec.id, "st_gm_boll_reversion")
        self.assertTrue(spec.desc)
        self.assertGreaterEqual(spec.min_bars, 60, "引擎预热 60 根，min_bars 不应低于它")
        schema = self.cls.param_schema()
        self.assertEqual(set(schema.keys()), set(self.cls.default_params.keys()),
                         "param_schema 必须与 default_params 键完全一致")
        for key, meta in schema.items():
            self.assertEqual(meta.get("default"), self.cls.default_params[key],
                             "参数 %s 的 schema 默认值与 default_params 不一致" % key)

    def test_sample_std_matches_pandas_ddof1(self):
        """样本标准差（ddof=1）必须与手算一致，且**不等于**总体标准差（ddof=0）。"""
        values = [10.0, 12.0, 11.0, 13.0, 9.0, 14.0]
        n = 6
        mean = sum(values) / n
        expected = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5
        population = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
        got = self.cls._sample_std(values, n)
        self.assertAlmostEqual(got, expected, places=9)
        self.assertNotAlmostEqual(got, population, places=4,
                                  msg="必须用样本口径（ddof=1），否则布林轨道会变窄")

    def test_sample_std_needs_enough_data(self):
        self.assertIsNone(self.cls._sample_std([1.0], 2))
        self.assertIsNone(self.cls._sample_std([1.0, 2.0], 1))

    def test_bands_ordering_and_scale(self):
        closes = [10.0 + (index % 5) for index in range(40)]
        strategy = self.cls({})
        upper, mid, lower = strategy._bands(closes)
        self.assertGreater(upper, mid)
        self.assertGreater(mid, lower)
        self.assertAlmostEqual(mid, sum(closes[-26:]) / 26, places=9)

    def test_bands_none_when_insufficient(self):
        self.assertIsNone(self.cls({})._bands([1.0, 2.0, 3.0]))

    def test_buy_volume_must_be_a_round_lot(self):
        from quantstudio.core.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.cls.check_params({"buy_volume": 150, "ma_period": 26, "std_period": 26,
                                   "std_range": 1.0})
        self.cls.check_params({"buy_volume": 200, "ma_period": 26, "std_period": 26,
                               "std_range": 1.0})

    def test_cross_signal_generates_buy(self):
        """构造「昨收在下轨之上、今收跌破下轨」的序列，应产生一笔买入委托。"""
        strategy = self.cls({"buy_volume": 200})
        # 先平稳震荡抬高标准差，最后一根明显跌破下轨
        base = [20.0, 21.0, 19.5, 20.5, 19.0, 21.5, 20.0, 20.2, 19.8, 20.1] * 4
        closes = base + [11.0]
        ctx = _SignalCtx(closes)
        orders = strategy.on_bar(ctx, {"600004.SH": Bar(date="2024-06-03", open=11.0, high=11.2,
                                                       low=10.8, close=11.0)})
        self.assertEqual(len(orders), 1, "跌破布林下轨应产生买入委托")
        self.assertEqual(orders[0].side, "buy")
        self.assertEqual(orders[0].qty, 200)

    def test_no_signal_inside_bands(self):
        strategy = self.cls({})
        closes = [20.0, 21.0, 19.5, 20.5, 19.0, 21.5, 20.0, 20.2, 19.8, 20.1] * 4 + [20.0]
        orders = strategy.on_bar(_SignalCtx(closes),
                                 {"600004.SH": Bar(date="2024-06-03", open=20.0, high=20.2,
                                                   low=19.8, close=20.0)})
        self.assertEqual(orders, [], "价格在带内不应交易")


class _SignalCtx:
    """给信号测试用的最小 ctx：只提供 history 与 position。"""

    today = "2024-06-03"

    def __init__(self, closes):
        self._closes = list(closes)

    def history(self, code, field="close", n=60):
        return self._closes[-int(n):]

    def bars_since(self, code, n):
        return []

    def position(self, code):
        return None

    def positions(self):
        return []

    def cash(self):
        return 1_000_000.0

    def total_assets(self):
        return 1_000_000.0

    def log(self, message):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
