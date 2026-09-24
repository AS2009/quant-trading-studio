# -*- coding: utf-8 -*-
"""MCP「策略」工具分组测试（``tools_strategy``）。

覆盖：工具注册与注解、清单/详情/源码读取、源码校验（含 smoke 开与关）、
核心写工具（落盘/备份/dry_run/lint 失败仍保留文件/超大拒绝/路径逃逸拒绝）、
删除（confirm 门禁 + 备份）、只读模式、审计日志、用户策略（JSON 参数化）。

约定
----
* 数据目录用临时目录（``Settings(data_dir=...)``），不污染仓库 ``backend/data``；
* 写 happy path 必须真的落到 ``strategies/local/<slug>.py`` 才能验证热加载，因此
  ``setUp``/``addCleanup`` 都会清理该文件，跑完仓库里不留残留；
* 服务层是进程级单例（``get_services``）：这里显式 ``force=True`` 绑到临时数据目录，
  测试结束时还原，避免影响其它测试模块。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio import services as services_module  # noqa: E402
from quantstudio.config import Settings  # noqa: E402
from quantstudio.mcp import tools_strategy  # noqa: E402
from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.registry import Registry, ToolError  # noqa: E402
from quantstudio.services import get_services  # noqa: E402
from quantstudio.strategies import registry as strategy_registry  # noqa: E402

DEMO_ID = "st_mcp_demo"
DEMO_SLUG = "mcp_demo"
DEMO_CLASS = "McpDemoStrategy"

READ_TOOLS = ("strategy_list", "strategy_get", "strategy_read_source", "strategy_lint")
WRITE_TOOLS = ("strategy_write_source", "strategy_delete_source",
               "strategy_user_create", "strategy_user_delete")

#: 最小可运行骨架（结构照抄 docs/strategy-examples.md「示例 C」，只换 id / 类名 / 名字）
_SOURCE_TEMPLATE = '''# -*- coding: utf-8 -*-
"""__NAME__（__ID__）。

信号逻辑
--------
- 买入：今日收盘价高于昨日收盘价且当前空仓
- 卖出：今日收盘价低于昨日收盘价且持仓

参数
----
- position_pct: 买入仓位（默认 0.95）

风险与假设
----------
- 只按收盘价成交，未考虑滑点与流动性。
"""

from typing import Dict, List

from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy


class __CLASS__(BaseStrategy):
    id = "__ID__"
    name = "__NAME__"
    category = "自定义"
    desc = "收盘价高于昨日则买入、低于昨日则清仓的演示策略（MCP 测试用）。"
    universe = "自选股（单标的即可）"
    universe_type = "single"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {"position_pct": 0.95}
    default_symbols = ["600519.SH"]

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {
            "position_pct": {"label": "买入仓位", "type": "float", "default": 0.95,
                             "min": 0.05, "max": 1.0, "step": 0.05, "help": "可用资金比例"},
        }

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
__BODY__
'''

_BODY_OK = '''        orders: List[OrderRequest] = []
        for code, bar in bars.items():
            closes = ctx.history(code, "close", 2)
            if len(closes) < 2:
                continue
            pos = ctx.position(code)
            if pos is None and bar.close > closes[-2]:
                order = self.buy_order(ctx, code, bar.close,
                                       float(self.params["position_pct"]), reason="收盘价上穿")
                if order:
                    orders.append(order)
            elif pos is not None and bar.close < closes[-2]:
                order = self.sell_order(ctx, code, reason="收盘价下穿")
                if order:
                    orders.append(order)
        return orders'''

#: 静态合法但运行期必崩（空仓时 ctx.position(code) 是 None）——用来验证 smoke 开关
_BODY_RUNTIME_ERROR = '''        orders: List[OrderRequest] = []
        for code in bars:
            qty = ctx.position(code).qty                 # 空仓场景 → AttributeError → E_SMOKE
            order = self.sell_order(ctx, code, qty=qty, reason="平仓")
            if order:
                orders.append(order)
        return orders'''

#: 静态违规：策略里出现 print()（模拟「模型写错代码」的常态）
_BODY_FORBIDDEN = '''        print("调仓")                                   # ← 故意违反策略规范（禁止 print）
        orders: List[OrderRequest] = []
        for code, bar in bars.items():
            closes = ctx.history(code, "close", 2)
            if len(closes) < 2:
                continue
            pos = ctx.position(code)
            if pos is None and bar.close > closes[-2]:
                order = self.buy_order(ctx, code, bar.close,
                                       float(self.params["position_pct"]), reason="收盘价上穿")
                if order:
                    orders.append(order)
            elif pos is not None and bar.close < closes[-2]:
                order = self.sell_order(ctx, code, reason="收盘价下穿")
                if order:
                    orders.append(order)
        return orders'''


def demo_source(strategy_id=DEMO_ID, class_name=DEMO_CLASS, name="MCP 写入演示",
                body=_BODY_OK) -> str:
    """按「文件名 ↔ id ↔ 类名」约定生成一份策略源码。"""
    return (_SOURCE_TEMPLATE
            .replace("__ID__", strategy_id)
            .replace("__CLASS__", class_name)
            .replace("__NAME__", name)
            .replace("__BODY__", body))


def error_codes(structured) -> list:
    return [item.get("code") for item in (structured.get("errors") or [])]


class McpStrategyToolsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="qs-mcp-strategy-")
        cls.settings = Settings(data_dir=cls.temp_dir, offline=True)
        cls._saved_services = services_module._services       # 进程级单例：用完还原
        get_services(settings=cls.settings, force=True)

        # 注意：本地策略目录**不能**改成临时目录——策略用相对导入（from ..base import …），
        # 只有落在包内 ``strategies/local/`` 才能被 import、才能跑烟雾回测；而且
        # ``ctx.repo_root`` 也是从这个目录往上推的。因此本用例会在真实目录里短暂创建
        # 一个演示策略文件，由 addCleanup/tearDownClass 负责删除（见 remove_demo_file）。
        cls.registry = Registry()
        tools_strategy.register(cls.registry)
        cls.ctx = ToolContext(settings=cls.settings, read_only=False, actor="test")
        cls.read_only_ctx = ToolContext(settings=cls.settings, read_only=True, actor="test")
        cls.local_dir = cls.ctx.local_dir
        cls.demo_path = os.path.join(cls.local_dir, DEMO_SLUG + ".py")

    @classmethod
    def tearDownClass(cls):
        cls.remove_demo_file()
        cls.purge_temp_containers()
        services_module._services = cls._saved_services
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
    # ------------------------------------------------------------------ 工具
    @classmethod
    def remove_demo_file(cls):
        """删掉演示策略文件，并把进程内注册表 / 模块缓存恢复到「仓库原样」。"""
        if os.path.isfile(cls.demo_path):
            os.remove(cls.demo_path)
        module_name = "quantstudio.strategies.local.%s" % DEMO_SLUG
        sys.modules.pop(module_name, None)
        for key, value in list(strategy_registry.REGISTRY.items()):
            if getattr(value, "__module__", "") == module_name:
                strategy_registry.REGISTRY.pop(key, None)
        if DEMO_ID in strategy_registry.LOCAL_IDS:
            strategy_registry.LOCAL_IDS.remove(DEMO_ID)
        strategy_registry.discover_local(force=True)
    @classmethod
    def purge_temp_containers(cls):
        """``strategy_lint`` 的临时校验目录必须以 ``_lint_tmp_`` 开头且用完即删。"""
        strategies_dir = os.path.dirname(os.path.abspath(strategy_registry.__file__))
        for name in os.listdir(strategies_dir):
            if name.startswith("_lint_tmp_"):
                shutil.rmtree(os.path.join(strategies_dir, name), ignore_errors=True)

    @classmethod
    def local_py_files(cls):
        return sorted(name for name in os.listdir(cls.local_dir)
                      if name.endswith(".py") and os.path.isfile(os.path.join(cls.local_dir, name)))

    def backup_files(self):
        directory = self.ctx.backup_dir
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def setUp(self):
        self.remove_demo_file()
        self.addCleanup(self.remove_demo_file)

    def call(self, name, args, ctx=None):
        spec = self.registry.get(name)
        self.assertIsNotNone(spec, "工具未注册：%s" % name)
        return spec.handler(ctx or self.ctx, args)

    def write_demo(self, source=None, **extra):
        args = {"strategy_id": DEMO_ID, "source": source if source is not None else demo_source()}
        args.update(extra)
        return self.call("strategy_write_source", args)

    def read_audit(self):
        path = os.path.join(self.temp_dir, "mcp-audit.log")
        self.assertTrue(os.path.isfile(path), "缺少审计日志：%s" % path)
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    # ------------------------------------------------------------------ 1) 注册与注解
    def test_01_registry_and_annotations(self):
        names = set(self.registry.names())
        self.assertEqual(names, set(READ_TOOLS) | set(WRITE_TOOLS))
        self.assertEqual(len(names), 8)

        for name in READ_TOOLS:
            spec = self.registry.get(name)
            self.assertTrue(spec.read_only, "%s 应为只读" % name)
            self.assertFalse(spec.destructive)
            self.assertFalse(spec.open_world, "%s 不应访问网络" % name)
            self.assertEqual(spec.group, "strategy")
        for name in WRITE_TOOLS:
            spec = self.registry.get(name)
            self.assertFalse(spec.read_only, "%s 应为写工具" % name)
            self.assertFalse(spec.open_world, "%s 不应访问网络" % name)
            self.assertEqual(spec.group, "strategy")
        self.assertTrue(self.registry.get("strategy_delete_source").destructive)
        self.assertTrue(self.registry.get("strategy_user_delete").destructive)
        self.assertTrue(self.registry.get("strategy_write_source").annotations()["readOnlyHint"] is False)

        writable = {spec.name for spec in self.registry.visible(writable=True)}
        read_only = {spec.name for spec in self.registry.visible(writable=False)}
        self.assertEqual(writable, set(READ_TOOLS) | set(WRITE_TOOLS))
        self.assertEqual(read_only, set(READ_TOOLS))
        self.assertEqual(writable.intersection(WRITE_TOOLS), set(WRITE_TOOLS))   # 可写模式下写工具可见

    # ------------------------------------------------------------------ 2) 清单
    def test_02_strategy_list_builtin_and_structure(self):
        text, data = self.call("strategy_list", {})
        self.assertIn("来源", text)                                    # 文本是给人/模型读的表格
        self.assertIn("st_ma_cross", text)
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data["strategies"], list)
        self.assertEqual(data["origin"], "all")
        self.assertEqual(data["count"], len(data["strategies"]))
        ids = [item["id"] for item in data["strategies"]]
        self.assertIn("st_ma_cross", ids)
        for item in data["strategies"]:
            for field in ("id", "name", "category", "origin", "param_count", "min_bars", "status"):
                self.assertIn(field, item)
        builtin = [item for item in data["strategies"] if item["id"] == "st_ma_cross"][0]
        self.assertEqual(builtin["origin"], "builtin")
        self.assertGreaterEqual(builtin["param_count"], 1)
        self.assertGreaterEqual(builtin["min_bars"], 1)

        _, only_local = self.call("strategy_list", {"origin": "local"})
        self.assertTrue(all(item["origin"] == "local" for item in only_local["strategies"]))
        self.assertIn("st_breakout_atr", [item["id"] for item in only_local["strategies"]])
        self.assertTrue(only_local["strategies"][0]["file"].endswith("breakout_atr.py"))

    # ------------------------------------------------------------------ 3) 详情 + 参数 schema
    def test_03_strategy_get_param_schema(self):
        _, data = self.call("strategy_get", {"strategy_id": "st_ma_cross"})
        self.assertEqual(data["strategy"]["id"], "st_ma_cross")
        schema = data["param_schema"]
        self.assertTrue(schema)
        first = data["params"][0]
        for field in ("name", "label", "type", "default", "min", "max", "help"):
            self.assertIn(field, first)
        self.assertEqual(first["default"], schema[first["name"]]["default"])

        with self.assertRaises(ToolError) as caught:
            self.call("strategy_get", {"strategy_id": "st_not_exist"})
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertIn("st_not_exist", caught.exception.message)

    # ------------------------------------------------------------------ 4) 写入 happy path
    def test_04_write_source_happy_path(self):
        source = demo_source()
        text, data = self.write_demo(source)
        self.assertTrue(data["written"])
        self.assertIs(data["dry_run"], False)
        self.assertEqual(data["bytes"], len(source.encode("utf-8")))
        self.assertEqual(data["bytes"], os.path.getsize(self.demo_path))
        self.assertTrue(os.path.isfile(self.demo_path), "happy path 必须真的写进 local/")
        with open(self.demo_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), source)
        self.assertTrue(data["ok"], "干净骨架不应有 error：%s" % (data["errors"],))
        self.assertEqual(data["errors"], [])
        self.assertIn("已写入", text)
        self.assertIn("校验：通过", text)
        self.assertIn("backtest_run", text)                                # 下一步建议
        self.assertIn(DEMO_ID, data["loaded"])

        # 热加载 + 清单可见
        _, listing = self.call("strategy_list", {"origin": "local"})
        row = [item for item in listing["strategies"] if item["id"] == DEMO_ID]
        self.assertTrue(row, "discover_local 后 strategy_list 应能看到 %s" % DEMO_ID)
        self.assertEqual(row[0]["origin"], "local")
        self.assertEqual(row[0]["min_bars"], 61)
        self.assertEqual(row[0]["file"], data["path"])

        # 已落盘文件也能被 strategy_lint 校验
        _, report = self.call("strategy_lint", {"strategy_id": DEMO_ID})
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["smoke"], True)

        # 源码可回读，且路径脱敏为相对路径
        _, read = self.call("strategy_read_source", {"strategy_id": DEMO_ID})
        self.assertEqual(read["source"], source)
        self.assertEqual(read["origin"], "local")
        self.assertFalse(os.path.isabs(read["path"]))
        self.assertIn("strategies/", read["path"])

    # ------------------------------------------------------------------ 5) lint 失败仍落盘
    def test_05_write_source_keeps_file_when_lint_fails(self):
        text, data = self.write_demo(demo_source(body=_BODY_FORBIDDEN))
        self.assertTrue(data["written"], "lint 有 error 也要落盘（模型可继续迭代）")
        self.assertFalse(data["ok"])
        self.assertIn("E_FORBIDDEN_CALL", error_codes(data))
        self.assertIn("E_FORBIDDEN_CALL", text)
        self.assertTrue(os.path.isfile(self.demo_path))
        self.assertIn("下一步", text)
        self.assertIn("strategy_write_source", text)                        # 有 error 时的下一步建议
        self.assertTrue(data["stdout"], "被兜住的 print 输出应作为证据返回")
        self.assertTrue(data["stdout"].strip().startswith("调仓"))

        # 文件名 / id 不一致：同样照写，但如实报 E_ID_FILE_MISMATCH
        _, mismatch = self.write_demo(demo_source(strategy_id="st_other_name"))
        self.assertTrue(mismatch["written"])
        self.assertFalse(mismatch["ok"])
        self.assertIn("E_ID_FILE_MISMATCH", error_codes(mismatch))

    # ------------------------------------------------------------------ 6) 路径逃逸
    def test_06_write_source_rejects_unsafe_ids(self):
        before = self.local_py_files()
        for bad in ("../evil", "a/b", "/tmp/x.py", "st_ok.py", "st_UPPER", ""):
            with self.assertRaises(ToolError) as caught:
                self.call("strategy_write_source", {"strategy_id": bad, "source": demo_source()})
            self.assertIn(caught.exception.code, ("INVALID_ID", "UNSAFE_PATH"))
            self.assertTrue(caught.exception.hint)

        # 删除同样过护栏
        with self.assertRaises(ToolError):
            self.call("strategy_delete_source", {"strategy_id": "../evil", "confirm": True})

        self.assertEqual(self.local_py_files(), before)
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "..", "evil.py")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.local_dir), "evil.py")))
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "x.py")))

    # ------------------------------------------------------------------ 7) 备份
    def test_07_write_source_backs_up_previous_content(self):
        first = demo_source()
        self.write_demo(first)
        second = demo_source() + "\n# 第二次写入：内容变了\n"
        _, data = self.write_demo(second)

        backup = data["backup"]
        self.assertTrue(backup, "覆盖已存在文件必须产生备份")
        self.assertTrue(os.path.isfile(backup))
        self.assertEqual(os.path.dirname(os.path.abspath(backup)),
                         os.path.abspath(self.ctx.backup_dir))
        self.assertIn(DEMO_SLUG + ".py", os.path.basename(backup))
        self.assertNotEqual(os.path.basename(backup), DEMO_SLUG + ".py")     # 带时间戳，不覆盖旧备份
        with open(backup, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), first, "备份内容必须等于覆盖前的旧内容")
        with open(self.demo_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), second)

    # ------------------------------------------------------------------ 8) dry_run
    def test_08_write_source_dry_run_does_not_touch_disk(self):
        first = demo_source()
        self.write_demo(first)
        backups_before = self.backup_files()

        _, data = self.write_demo(demo_source() + "\n# 只校验，不落盘\n", dry_run=True)
        self.assertFalse(data["written"])
        self.assertIs(data["dry_run"], True)
        self.assertTrue(data["ok"], data["errors"])
        self.assertEqual(data["backup"], "")
        with open(self.demo_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), first, "dry_run 不能改动文件")
        self.assertEqual(self.backup_files(), backups_before, "dry_run 不应产生备份")

        # 目标文件不存在时，dry_run 也不创建
        self.remove_demo_file()
        _, data = self.write_demo(dry_run=True)
        self.assertFalse(data["written"])
        self.assertFalse(os.path.exists(self.demo_path))

    # ------------------------------------------------------------------ 9) 只读模式
    def test_09_read_only_mode_blocks_all_writes(self):
        cases = [
            ("strategy_write_source", {"strategy_id": DEMO_ID, "source": demo_source()}),
            ("strategy_delete_source", {"strategy_id": DEMO_ID, "confirm": True}),
            ("strategy_user_create", {"name": "只读模式测试", "template": "st_ma_cross"}),
            ("strategy_user_delete", {"strategy_id": "us_00000000"}),
        ]
        for name, args in cases:
            with self.assertRaises(ToolError) as caught:
                self.call(name, args, ctx=self.read_only_ctx)
            self.assertEqual(caught.exception.code, "READ_ONLY", name)
            self.assertIn("只读模式", caught.exception.message)

        self.assertFalse(os.path.exists(self.demo_path))
        visible = {spec.name for spec in self.registry.visible(writable=False)}
        self.assertFalse(visible.intersection(WRITE_TOOLS))
        # 只读上下文里的只读工具照常可用
        _, data = self.call("strategy_list", {}, ctx=self.read_only_ctx)
        self.assertTrue(data["strategies"])

    # ------------------------------------------------------------------ 10) 超大源码
    def test_10_write_source_rejects_oversize(self):
        huge = demo_source() + "\n# " + "x" * (128 * 1024)
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_write_source", {"strategy_id": DEMO_ID, "source": huge})
        self.assertEqual(caught.exception.code, "SOURCE_TOO_LARGE")
        self.assertFalse(os.path.exists(self.demo_path), "超长源码不能落盘")

    # ------------------------------------------------------------------ 11) 删除
    def test_11_delete_source_requires_confirm(self):
        self.write_demo()
        self.assertTrue(os.path.isfile(self.demo_path))

        for args in ({"strategy_id": DEMO_ID}, {"strategy_id": DEMO_ID, "confirm": False}):
            with self.assertRaises(ToolError) as caught:
                self.call("strategy_delete_source", args)
            self.assertEqual(caught.exception.code, "CONFIRM_REQUIRED")
            self.assertEqual(caught.exception.hint, "确认无误请传 confirm=true")
            self.assertTrue(os.path.isfile(self.demo_path), "未确认时不能删文件")

        text, data = self.call("strategy_delete_source", {"strategy_id": DEMO_ID, "confirm": True})
        self.assertTrue(data["deleted"])
        self.assertFalse(os.path.exists(self.demo_path), "confirm=true 后文件应被删除")
        self.assertTrue(data["backup"] and os.path.isfile(data["backup"]))
        with open(data["backup"], "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), demo_source())
        self.assertIn("已删除", text)

        # 再删同一个 id：文件已不在，报可读错误
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_delete_source", {"strategy_id": DEMO_ID, "confirm": True})
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        # 内置策略没有 local 文件
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_delete_source", {"strategy_id": "st_ma_cross", "confirm": True})
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertIn("内置策略", caught.exception.message)

        # 删除后本地列表不再包含它（进程内注册表也被清理）
        _, listing = self.call("strategy_list", {"origin": "local"})
        self.assertNotIn(DEMO_ID, [item["id"] for item in listing["strategies"]])

    # ------------------------------------------------------------------ 12) 审计
    def test_12_audit_log_records_writes(self):
        self.write_demo()
        entries = self.read_audit()
        writes = [entry for entry in entries if entry.get("tool") == "strategy_write_source"]
        self.assertTrue(writes)
        entry = writes[-1]
        self.assertIs(entry.get("ok"), True)
        self.assertEqual(entry.get("strategy_id"), DEMO_ID)
        self.assertGreater(entry.get("bytes"), 0)
        self.assertIs(entry.get("lint_ok"), True)
        self.assertIn("args", entry)
        self.assertNotIn(demo_source(), json.dumps(entry, ensure_ascii=False))   # 源码不进审计原文

        self.call("strategy_delete_source", {"strategy_id": DEMO_ID, "confirm": True})
        entries = self.read_audit()
        self.assertTrue([item for item in entries if item.get("tool") == "strategy_delete_source"])

    # ------------------------------------------------------------------ 13) 读源码
    def test_13_read_source_builtin_and_unknown(self):
        text, data = self.call("strategy_read_source", {"strategy_id": "st_ma_cross"})
        self.assertTrue(data["source"].strip())
        self.assertIn("class MaCrossStrategy", data["source"])
        self.assertEqual(data["lines"], len(data["source"].splitlines()))
        self.assertEqual(data["origin"], "builtin")
        self.assertFalse(os.path.isabs(data["path"]), "路径必须脱敏成相对路径：%s" % data["path"])
        self.assertIn("strategies/ma_cross.py", data["path"].replace(os.sep, "/"))
        self.assertIn("class MaCrossStrategy", text)

        with self.assertRaises(ToolError) as caught:
            self.call("strategy_read_source", {"strategy_id": "st_no_such_strategy"})
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertIn("st_no_such_strategy", caught.exception.message)
        self.assertTrue(caught.exception.hint)

    # ------------------------------------------------------------------ 14) 源码模式 lint + smoke
    def test_14_lint_source_is_not_persisted_and_smoke_flag(self):
        before = self.local_py_files()
        text, data = self.call("strategy_lint", {"source": demo_source(strategy_id="st_mcp_smoke",
                                                                      class_name="McpSmokeStrategy")})
        self.assertFalse(data["written"])
        self.assertTrue(data["ok"], data["errors"])
        self.assertEqual(data["target_id"], "st_mcp_smoke")
        self.assertEqual(self.local_py_files(), before, "源码模式不能落盘")
        strategies_dir = os.path.dirname(os.path.abspath(strategy_registry.__file__))
        self.assertFalse([n for n in os.listdir(strategies_dir) if n.startswith("_lint_tmp_")],
                         "临时校验目录必须清理干净")

        broken = demo_source(strategy_id="st_mcp_smoke", class_name="McpSmokeStrategy",
                             body=_BODY_RUNTIME_ERROR)
        _, with_smoke = self.call("strategy_lint", {"source": broken})
        self.assertFalse(with_smoke["ok"])
        self.assertIn("E_SMOKE", error_codes(with_smoke))

        _, no_smoke = self.call("strategy_lint", {"source": broken, "smoke": False})
        self.assertIs(no_smoke["smoke"], False)
        self.assertNotIn("E_SMOKE", error_codes(no_smoke))
        self.assertTrue(no_smoke["ok"], no_smoke["errors"])

        # 两样都不给 → 明确报错
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_lint", {})
        self.assertEqual(caught.exception.code, "INVALID_ARGS")

        # 语法错误：临时文件照样清理
        _, syntax = self.call("strategy_lint", {"source": "class Broken(:\n    pass\n"})
        self.assertFalse(syntax["ok"])
        self.assertIn("E_SYNTAX", error_codes(syntax))

    # ------------------------------------------------------------------ 15) 用户策略
    def test_15_user_strategy_create_and_delete(self):
        text, data = self.call("strategy_user_create", {
            "name": "MCP 测试用户策略", "template": "st_ma_cross",
            "params": {"short_ma": 10, "long_ma": 30}, "category": "趋势跟踪",
        })
        new_id = data["strategy_id"]
        self.assertTrue(new_id.startswith("us_"), new_id)
        self.assertIn("已新建", text)

        _, listing = self.call("strategy_list", {"origin": "user"})
        row = [item for item in listing["strategies"] if item["id"] == new_id]
        self.assertTrue(row, "新建的用户策略应出现在 strategy_list")
        self.assertEqual(row[0]["origin"], "user")
        self.assertEqual(row[0]["params"]["short_ma"], 10)

        _, detail = self.call("strategy_get", {"strategy_id": new_id})
        self.assertEqual(detail["strategy"]["id"], new_id)

        # 非法参数键 → VALIDATION（模型可照 hint 修）
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_user_create", {"name": "MCP 非法参数策略", "template": "st_ma_cross",
                                               "params": {"no_such_param": 1}})
        self.assertEqual(caught.exception.code, "VALIDATION")
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_user_create", {"name": "MCP 未知模板", "template": "st_nope"})
        self.assertEqual(caught.exception.code, "VALIDATION")

        _, deleted = self.call("strategy_user_delete", {"strategy_id": new_id})
        self.assertTrue(deleted["deleted"])
        _, listing = self.call("strategy_list", {"origin": "user"})
        self.assertNotIn(new_id, [item["id"] for item in listing["strategies"]])

        # 内置策略不可删
        with self.assertRaises(ToolError) as caught:
            self.call("strategy_user_delete", {"strategy_id": "st_ma_cross"})
        self.assertEqual(caught.exception.code, "FORBIDDEN")

    # ------------------------------------------------------------------ 16) 参数校验入口
    def test_16_missing_required_args(self):
        from quantstudio.mcp.registry import validate_args

        spec = self.registry.get("strategy_write_source")
        with self.assertRaises(ToolError) as caught:
            validate_args(spec, {"strategy_id": DEMO_ID})
        self.assertEqual(caught.exception.code, "INVALID_ARGS")
        self.assertIn("source", caught.exception.message)

        with self.assertRaises(ToolError):
            validate_args(self.registry.get("strategy_list"), {"origin": "nope"})


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
