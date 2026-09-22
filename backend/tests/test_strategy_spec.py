# -*- coding: utf-8 -*-
"""策略规范测试：命名/目录/文件格式约定、本地策略自动发现、lint 校验能力。

对应文档：docs/strategy-spec.md（规范）、docs/strategy-api.md（API）、
docs/strategy-examples.md（示例）、docs/ai-strategy-guide.md（AI 写作指南）。
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantstudio.core.errors import ValidationError  # noqa: E402
from quantstudio.strategies import (  # noqa: E402
    LOCAL_DIR,
    REGISTRY,
    create,
    discover_local,
    is_builtin,
    is_local,
    list_specs,
    local_status,
)
from quantstudio.strategies import lint as lint_module  # noqa: E402
from quantstudio.strategies import registry as registry_module  # noqa: E402

ID_RE = re.compile(r"^st_[a-z0-9_]{2,36}$")
BUILTIN_FILES = ["grid.py", "low_vol.py", "ma_cross.py", "momentum.py", "rsi_reversion.py", "turtle.py"]

# 一个「最小但合规」的本地策略源码模板：{} 处替换即可制造各类违规
FIXTURE_TEMPLATE = '''# -*- coding: utf-8 -*-
"""测试用策略（{strategy_id}）。

信号逻辑
--------
- 收盘价高于上一日时买入，低于上一日时清仓。
"""

from typing import Dict, List

from ..base import BaseStrategy
from ...core.models import Bar, OrderRequest


class {class_name}(BaseStrategy):
    id = "{strategy_id}"
    name = "{name}"
    category = "趋势跟踪"
    desc = "测试夹具策略"
    universe = "自选股"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {{"lookback": 20, "position_pct": 0.95}}
    default_symbols = ["600519.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {{
            "lookback": {{"label": "回看周期", "type": "int", "default": 20, "min": 2, "max": 250, "step": 1, "help": "比较窗口"}},
            "position_pct": {{"label": "仓位", "type": "float", "default": 0.95, "min": 0.05, "max": 1.0, "step": 0.05, "help": "买入资金比例"}},
        }}

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
        orders: List[OrderRequest] = []
        for code, bar in bars.items():
            closes = ctx.history(code, "close", int(self.params["lookback"]))
            if len(closes) < 2:
                continue
            pos = ctx.position(code)
            if pos is None and bar.close > closes[-2]:
                order = self.buy_order(ctx, code, bar.close, float(self.params["position_pct"]), reason="上涨")
                if order:
                    orders.append(order)
            elif pos is not None and bar.close < closes[-2]:
                order = self.sell_order(ctx, code, reason="下跌")
                if order:
                    orders.append(order)
        return orders
'''


def fixture_source(slug="fixture_ok", class_name=None, strategy_id=None, name="测试夹具策略",
                   extra_body="", on_bar_extra=""):
    return FIXTURE_TEMPLATE.format(
        strategy_id=strategy_id or ("st_" + slug),
        class_name=class_name or ("".join(part.capitalize() for part in slug.split("_")) + "Strategy"),
        name=name,
    ).replace("    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:",
              "    %s\n    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:" % extra_body
              ).replace("        return orders", ("        %s\n        return orders" % on_bar_extra) if on_bar_extra else "        return orders")


class LocalFixtureMixin(unittest.TestCase):
    """在 strategies/local/ 下临时写文件做校验，并保证注册表状态可回滚。"""

    def setUp(self):
        self._backup_registry = dict(REGISTRY)
        self._backup_ids = list(registry_module.LOCAL_IDS)
        self._backup_errors = list(registry_module.LOCAL_ERRORS)
        self._paths = []

    def tearDown(self):
        for path in self._paths:
            if os.path.exists(path):
                os.remove(path)
            sys.modules.pop("quantstudio.strategies.local." + os.path.basename(path)[:-3], None)
        REGISTRY.clear()
        REGISTRY.update(self._backup_registry)
        registry_module.LOCAL_IDS[:] = self._backup_ids
        registry_module.LOCAL_ERRORS[:] = self._backup_errors
        registry_module.discover_local(force=False)

    def write_local(self, slug, source):
        path = os.path.join(LOCAL_DIR, slug + ".py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        self._paths.append(path)
        return path


class TestNamingConvention(LocalFixtureMixin):
    """命名规范：文件 ↔ id ↔ 类名必须一一对应（内置策略同样受约束）。"""

    def test_builtin_files_match_ids_and_classes(self):
        package_dir = os.path.dirname(os.path.abspath(registry_module.__file__))
        for filename in BUILTIN_FILES:
            path = os.path.join(package_dir, filename)
            self.assertTrue(os.path.exists(path), "内置策略文件缺失：%s" % filename)
            issues, _tree = lint_module.lint_source(path, origin="builtin")
            self.assertEqual([i for i in issues if i.level == "error"], [],
                             "%s 静态校验未通过" % filename)
        for spec in list_specs():
            if spec.origin == "builtin":
                self.assertTrue(ID_RE.match(spec.id), "内置 id 不符合规范：%s" % spec.id)
                self.assertTrue(is_builtin(spec.id))
                self.assertEqual(spec.origin, "builtin")

    def test_builtin_order_is_stable(self):
        builtin_ids = [spec.id for spec in list_specs() if spec.origin == "builtin"]
        self.assertEqual(builtin_ids, registry_module.BUILTIN_ORDER)


class TestLocalDiscovery(LocalFixtureMixin):
    """目录约定：local/ 自动发现、下划线文件不加载、坏文件不影响应用。"""

    def test_template_is_not_loaded(self):
        self.assertTrue(os.path.exists(os.path.join(LOCAL_DIR, "_template.py")))
        discover_local(force=True)
        self.assertNotIn("st_template", registry_module.LOCAL_IDS)
        for strategy_id in registry_module.LOCAL_IDS:
            self.assertFalse(strategy_id.endswith("template"))

    def test_example_strategy_is_discovered(self):
        discover_local(force=True)
        self.assertIn("st_breakout_atr", registry_module.LOCAL_IDS)
        self.assertTrue(is_local("st_breakout_atr"))
        self.assertFalse(is_builtin("st_breakout_atr"))
        spec = next(s for s in list_specs() if s.id == "st_breakout_atr")
        self.assertEqual(spec.origin, "local")
        self.assertFalse(spec.builtin, "本地代码策略不应被标记为内置（界面不提供删除按钮）")
        strategy = create("st_breakout_atr", symbols=["600519.SH"])
        self.assertEqual(strategy.spec.origin, "local")
        with self.assertRaises(ValidationError):
            create("st_breakout_atr", params={"exit_days": 99})   # 跨参数校验必须生效

    def test_local_priority_and_order(self):
        ids = [spec.id for spec in list_specs()]
        self.assertEqual(ids[:len(registry_module.BUILTIN_ORDER)], registry_module.BUILTIN_ORDER)
        if registry_module.LOCAL_IDS:
            self.assertEqual(ids[len(registry_module.BUILTIN_ORDER)], registry_module.LOCAL_IDS[0])

    def test_broken_file_does_not_crash_discovery(self):
        self.write_local("broken_syntax", "def oops(:\n")
        discover_local(force=True)
        status = local_status()
        self.assertTrue(any(item["file"] == "broken_syntax.py" for item in status["errors"]))
        self.assertGreaterEqual(len(list_specs()), len(registry_module.BUILTIN_ORDER))

    def test_id_conflict_with_builtin_is_reported(self):
        self.write_local("shadow", fixture_source(slug="shadow", strategy_id="st_ma_cross",
                                                  class_name="ShadowStrategy", name="冒名策略"))
        discover_local(force=True)
        status = local_status()
        self.assertTrue(any("冲突" in item["error"] for item in status["errors"]))
        self.assertEqual(REGISTRY["st_ma_cross"].__name__, "MaCrossStrategy", "内置策略不能被本地文件顶替")


class TestLintRules(LocalFixtureMixin):
    """校验要求：各类违规必须被 lint 拦住。"""

    @staticmethod
    def _errors(report):
        """兼容两种报告结构：lint_path（errors 为列表）与 lint_all（errors 为计数）。"""
        errors = report.get("errors")
        if isinstance(errors, int):
            merged = []
            for item in report.get("reports", []):
                merged.extend(item.get("errors", []))
            errors = merged
        return " | ".join(item["code"] + ": " + item["message"] for item in errors or [])

    def test_all_shipped_strategies_pass(self):
        report = lint_module.lint_all(smoke=True)
        self.assertEqual(report["errors"], 0, self._errors(report))
        self.assertTrue(report["ok"])

    def test_valid_fixture_passes(self):
        path = self.write_local("fixture_ok", fixture_source())
        report = lint_module.lint_path(path, origin="local", smoke=True)
        self.assertTrue(report["ok"], self._errors(report))

    def test_id_file_mismatch(self):
        path = self.write_local("bad_id", fixture_source(slug="bad_id", strategy_id="st_other_name",
                                                         class_name="BadIdStrategy"))
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_ID_FILE_MISMATCH", self._errors(report))

    def test_class_name_mismatch(self):
        path = self.write_local("bad_class", fixture_source(slug="bad_class", class_name="WrongName"))
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_CLASS_NAME", self._errors(report))

    def test_forbidden_import_and_calls(self):
        source = fixture_source(slug="bad_import").replace(
            "from typing import Dict, List", "from typing import Dict, List\n\nimport requests")
        path = self.write_local("bad_import", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_IMPORT", self._errors(report))

        source = fixture_source(slug="bad_print").replace('        return orders',
                                                          '        print("偷偷输出")\n        return orders')
        path = self.write_local("bad_print", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_FORBIDDEN_CALL", self._errors(report))

    def test_private_ctx_access_is_rejected(self):
        source = fixture_source(slug="peek", on_bar_extra='future = ctx._series[list(bars)[0]][-1]')
        path = self.write_local("peek", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_PRIVATE_ACCESS", self._errors(report))

    def test_schema_and_defaults_must_agree(self):
        source = fixture_source(slug="bad_schema").replace('"lookback": {"label": "回看周期", "type": "int", "default": 20,',
                                                           '"lookback": {"label": "回看周期", "type": "int", "default": 30,')
        path = self.write_local("bad_schema", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_SCHEMA_DEFAULT_MISMATCH", self._errors(report))

    def test_min_bars_must_cover_longest_window(self):
        # 把回看窗口默认值提到 200（class 默认值与 schema 同步改，避免触发一致性错误），min_bars 仍是 61
        source = (fixture_source(slug="short_bars")
                  .replace('default_params = {"lookback": 20,', 'default_params = {"lookback": 200,')
                  .replace('"lookback": {"label": "回看周期", "type": "int", "default": 20,',
                           '"lookback": {"label": "回看周期", "type": "int", "default": 200,'))
        path = self.write_local("short_bars", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertIn("E_MIN_BARS", self._errors(report))

    def test_min_bars_warning_is_not_an_error(self):
        source = fixture_source(slug="small_bars").replace("min_bars = 61", "min_bars = 21").replace(
            '"lookback": {"label": "回看周期", "type": "int", "default": 20, "min": 2, "max": 250',
            '"lookback": {"label": "回看周期", "type": "int", "default": 20, "min": 2, "max": 250')
        path = self.write_local("small_bars", source)
        report = lint_module.lint_path(path, origin="local", smoke=False)
        self.assertTrue(report["ok"], self._errors(report))
        self.assertTrue(any(item["code"] == "W_MIN_BARS" for item in report["warnings"]))

    def test_on_bar_exception_is_caught_by_smoke_run(self):
        source = fixture_source(slug="boom", on_bar_extra="raise RuntimeError('炸了')")
        path = self.write_local("boom", source)
        report = lint_module.lint_path(path, origin="local", smoke=True)
        self.assertIn("E_SMOKE", self._errors(report))


class TestLintCli(LocalFixtureMixin):
    """命令行接口（CI 与 AI 自检都用它）。"""

    def test_cli_passes_for_shipped_strategies(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = lint_module.main(["--json", "--no-smoke"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["errors"], 0)

    def test_cli_reports_failure_for_bad_file(self):
        import io
        import contextlib
        path = self.write_local("cli_bad", fixture_source(slug="cli_bad", strategy_id="bad-id"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = lint_module.main(["--path", path, "--no-smoke"])
        self.assertEqual(code, 1)
        self.assertIn("E_ID_FORMAT", buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
