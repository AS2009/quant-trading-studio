# -*- coding: utf-8 -*-
"""``tools_market``（行情 / 自选池 / 系统）工具分组的回归测试。

全部依赖用**离线假 Provider** 注入（FakeMarketProvider），数据目录指向临时目录，
绝不联网；同时覆盖工具注册、JSON Schema、文本/结构化结果、参数校验、只读模式与审计。
至少一个用例走完整的 ``quantstudio.mcp.Server`` → ``tools/call`` 分发路径。
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.config import Settings  # noqa: E402
from quantstudio.core.calendar import TradingCalendar  # noqa: E402
from quantstudio.core.errors import SymbolNotFound  # noqa: E402
from quantstudio.core.models import (  # noqa: E402
    Bar,
    DataMeta,
    IndexQuote,
    MarketBreadth,
    Quote,
    SectorQuote,
)
from quantstudio.mcp import tools_market  # noqa: E402
from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.registry import Registry, ToolError  # noqa: E402
from quantstudio.mcp.server import Server  # noqa: E402
from quantstudio.mcp.tools import build_registry  # noqa: E402
from quantstudio.services import Services  # noqa: E402
from quantstudio.services.common import cache_clear, normalize_code  # noqa: E402

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
    "600000.SH": "浦发银行",
}

EXPECTED_TOOLS = (
    "market_kline",
    "market_overview",
    "market_quotes",
    "market_sectors",
    "system_audit",
    "system_status",
    "watchlist_add",
    "watchlist_list",
    "watchlist_remove",
)

_DATE_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} ")


class FakeMarketProvider:
    """离线固定数据源：未知标的抛 SymbolNotFound（与真实 Composite 行为一致）。"""

    name = "fake"

    def __init__(self, source="sina", offline=False):
        self.last_meta = DataMeta(
            source=source,
            stale=False,
            offline=offline,
            as_of="2026-09-18 15:00:00",
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
                    ts="2026-09-18 15:00:00",
                    source=self.last_meta.source,
                )
            )
        return rows

    def kline(self, symbol, days=250, freq="day", adjust="qfq"):
        code = normalize_code(symbol)
        if code not in STOCKS and code not in INDEXES:
            raise SymbolNotFound("未找到标的 %s" % code)
        self.calls["kline"] += 1
        calendar = TradingCalendar()
        dates = calendar.trading_days("2020-01-01", "2026-09-18")[-int(days):]
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
                    volume_wan=10.0 + i,
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
                    source=self.last_meta.source,
                )
            )
        return rows

    def sectors(self, limit=20):
        rows = [
            SectorQuote(code="BK001", name="半导体", change_pct=3.5, net_inflow_yi=9.9,
                        up_count=30, down_count=5),
            SectorQuote(code="BK002", name="白酒", change_pct=1.2, net_inflow_yi=2.2,
                        up_count=12, down_count=6),
            SectorQuote(code="BK003", name="银行", change_pct=-0.8, net_inflow_yi=-3.3,
                        up_count=4, down_count=28),
            SectorQuote(code="BK004", name="新能源", change_pct=2.1, net_inflow_yi=5.5,
                        up_count=20, down_count=9),
        ]
        return rows[: int(limit)]

    def breadth(self):
        return MarketBreadth(
            up=3000, down=1500, flat=200, limit_up=50, limit_down=5, total=4700,
            total_amount_yi=12345.0, main_net_inflow_yi=-12.3, north_net_inflow_yi=45.6,
            source=self.last_meta.source,
        )

    def resolve_name(self, symbol):
        return STOCKS.get(normalize_code(symbol))

    def health(self):
        return {"ok": True, "detail": "fake market provider ok", "latency_ms": 1}

    def describe(self):
        return {"name": self.name, "sources": ["sina", "sample"], "available": ["sina"]}

    def sources(self):
        return ["sina", "sample"]


# --------------------------------------------------------------------------- 测试
class MarketToolsTestCase(unittest.TestCase):
    """工具注册、分发、结果整形与只读/参数行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-mcp-market-")
        self.settings = Settings(data_dir=self.tmp, offline=False)
        self.provider = FakeMarketProvider()
        self.registry = Registry()
        tools_market.register(self.registry)
        cache_clear()                            # 服务层 TTL 缓存跨用例共享，先清干净

    def tearDown(self):
        cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 工具
    def make_context(self, read_only=False):
        ctx = ToolContext(settings=self.settings, read_only=read_only, provider=self.provider)
        # ToolContext.services() 走进程级单例且不传 force：若同进程其它测试已构造过
        # services（例如 test_integration 的 create_app），这里注入的 provider/settings
        # 会被静默忽略。显式替换，保证测试完全离线、数据目录指向临时目录。
        ctx._services = Services(settings=self.settings, provider=self.provider)
        return ctx

    def make_server(self, read_only=False):
        return Server(self.registry, context=self.make_context(read_only=read_only),
                      stderr=io.StringIO())

    def call(self, server, name, arguments=None):
        """走完整的 tools/call 分发路径（含参数校验与结果整形）。"""
        reply = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments or {}}})
        self.assertNotIn("error", reply, "工具调用不应产生协议错误：%r" % (reply,))
        return reply["result"]

    def assert_schema(self, schema, required=()):
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema.get("type"), "object")
        properties = schema.get("properties")
        self.assertIsInstance(properties, dict)
        self.assertEqual(list(schema.get("required") or []), list(required))
        for name in required:
            self.assertIn(name, properties)
        for name, prop in properties.items():
            self.assertIn(prop.get("type"),
                          ("string", "integer", "number", "boolean", "array", "object"), name)
            self.assertTrue(str(prop.get("description") or "").strip(), name)
            if "default" in prop:
                self.assertIsNotNone(prop["default"], name)
            if prop.get("enum"):
                if "default" in prop:
                    self.assertIn(prop["default"], prop["enum"], name)
                for value in prop["enum"]:
                    self.assertIsInstance(value, str, name)
        return properties

    # ------------------------------------------------------------------ ① 注册与 schema
    def test_01_register_names_and_json_schema(self):
        names = [spec.name for spec in self.registry.all()]
        self.assertEqual(names, sorted(EXPECTED_TOOLS))
        self.assertEqual(len(names), 9)

        # 写工具标记与只读模式下可见性
        for write_tool in ("watchlist_add", "watchlist_remove"):
            self.assertFalse(self.registry.get(write_tool).read_only, write_tool)
        visible = [spec.name for spec in self.registry.visible(writable=False)]
        self.assertNotIn("watchlist_add", visible)
        self.assertNotIn("watchlist_remove", visible)
        for read_tool in EXPECTED_TOOLS:
            if read_tool.startswith("watchlist_") and read_tool != "watchlist_list":
                continue
            self.assertIn(read_tool, visible)

        required_map = {
            "market_kline": ["symbol"],
            "watchlist_add": ["code"],
            "watchlist_remove": ["code"],
        }
        for spec in self.registry.all():
            self.assertTrue(spec.description.strip(), spec.name)
            self.assert_schema(spec.input_schema, required_map.get(spec.name, ()))

        # 默认值与边界写在 schema 里，模型能直接看到
        kline = self.registry.get("market_kline").input_schema["properties"]
        self.assertEqual(kline["days"]["default"], 60)
        self.assertEqual(kline["days"]["maximum"], 400)
        self.assertEqual(kline["freq"]["default"], "day")
        self.assertEqual(kline["freq"]["enum"], ["day", "week", "month"])
        self.assertEqual(kline["adjust"]["enum"], ["qfq", "hfq", "none"])
        self.assertEqual(kline["symbol"]["examples"], ["600519.SH", "300750.SZ"])
        self.assertEqual(self.registry.get("market_sectors").input_schema["properties"]["limit"]["default"], 12)
        self.assertEqual(self.registry.get("system_audit").input_schema["properties"]["limit"]["maximum"], 200)
        self.assertEqual(
            self.registry.get("market_quotes").input_schema["properties"]["codes"]["items"],
            {"type": "string"},
        )

        # tools/list（完整服务器路径）里的 inputSchema 必须都是合法 JSON Schema
        listing = self.make_server().handle_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = listing["result"]["tools"]
        self.assertEqual([item["name"] for item in tools], sorted(EXPECTED_TOOLS))
        for item in tools:
            self.assertTrue(item["description"].strip(), item["name"])
            self.assertEqual(item["inputSchema"]["type"], "object")
            self.assertIn("properties", item["inputSchema"])
            annotations = item["annotations"]
            self.assertIn("readOnlyHint", annotations)
            self.assertIn("openWorldHint", annotations)
        # 只读模式下 tools/list 隐藏写工具
        read_only_tools = [item["name"] for item in self.make_server(read_only=True).handle_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(read_only_tools, sorted(set(EXPECTED_TOOLS) - {"watchlist_add", "watchlist_remove"}))

        # tools.py 的 TOOL_MODULES 装配路径也能工作（只装本模块，避免依赖其它分组）
        built = build_registry(modules=("tools_market",))
        self.assertEqual([spec.name for spec in built.all()], sorted(EXPECTED_TOOLS))

    # ------------------------------------------------------------------ ② 假 Provider 全链路（完整服务器）
    def test_02_read_only_tools_via_full_server(self):
        server = self.make_server()
        watchlist = list(self.settings.default_watchlist)

        expected_keys = {
            "market_overview": ("indices", "breadth", "total_amount_yi", "source", "meta"),
            "market_quotes": ("quotes", "codes", "count", "source", "from_watchlist"),
            "market_kline": ("bars", "summary", "symbol", "freq", "adjust", "count", "source"),
            "market_sectors": ("sectors", "limit", "count", "source"),
            "watchlist_list": ("codes", "count"),
            "system_status": ("status", "provider_meta", "source", "offline", "mode"),
        }
        for name in expected_keys:
            result = self.call(server, name, {} if name != "market_kline" else {"symbol": "600519.SH"})
            text = result["content"][0]["text"]
            structured = result["structuredContent"]
            self.assertFalse(result["isError"], name)
            self.assertTrue(text.strip(), name)
            self.assertIsInstance(structured, dict, name)
            for key in expected_keys[name]:
                self.assertIn(key, structured, "%s → %s" % (name, key))

        # market_overview：指数 4 条 + 广度 + 资金
        overview = self.call(server, "market_overview")["structuredContent"]
        self.assertEqual(len(overview["indices"]), 4)
        self.assertEqual(overview["breadth"]["up"], 3000)
        self.assertEqual(overview["total_amount_yi"], 12345.0)
        self.assertEqual(overview["main_net_inflow_yi"], -12.3)
        self.assertEqual(overview["indices"][0]["name"], "上证指数")

        # market_quotes：缺省 = 自选池，顺序与自选池一致，文本带来源标记
        quotes = self.call(server, "market_quotes")
        self.assertTrue(quotes["structuredContent"]["from_watchlist"])
        self.assertEqual(quotes["structuredContent"]["codes"], watchlist)
        self.assertEqual(quotes["structuredContent"]["count"], len(watchlist))
        self.assertEqual(quotes["structuredContent"]["source"], "sina")
        quote_text = quotes["content"][0]["text"]
        self.assertIn("来源=sina", quote_text)
        self.assertIn("600519.SH", quote_text)
        self.assertIn("贵州茅台", quote_text)
        self.assertIn("单位说明", quote_text)

        # market_kline：默认 60 根，文本只给首尾各 5 根
        kline = self.call(server, "market_kline", {"symbol": "600519.SH"})
        self.assertEqual(kline["structuredContent"]["count"], 60)
        self.assertEqual(kline["structuredContent"]["symbol"], "600519.SH")
        self.assertEqual(kline["structuredContent"]["freq"], "day")
        self.assertEqual(kline["structuredContent"]["adjust"], "qfq")
        self.assertEqual(len(kline["structuredContent"]["bars"]), 60)
        summary = kline["structuredContent"]["summary"]
        for key in ("start", "end", "change_pct", "high", "low", "avg_volume_wan"):
            self.assertIn(key, summary)
        kline_text = kline["content"][0]["text"]
        self.assertIn("共 60 根", kline_text)
        self.assertIn("省略", kline_text)
        date_lines = [line for line in kline_text.splitlines() if _DATE_LINE.match(line)]
        self.assertEqual(len(date_lines), 10, "文本只应展示首尾各 5 根")

        # market_sectors：按涨幅降序、limit 生效
        sectors = self.call(server, "market_sectors", {"limit": 3})["structuredContent"]
        self.assertEqual(sectors["count"], 3)
        pcts = [row["change_pct"] for row in sectors["sectors"]]
        self.assertEqual(pcts, sorted(pcts, reverse=True))

        # watchlist_list 与 settings.default_watchlist 一致（首次读取会落盘临时目录）
        listed = self.call(server, "watchlist_list")["structuredContent"]
        self.assertEqual(listed["codes"], watchlist)
        self.assertEqual(listed["count"], len(watchlist))
        self.assertTrue(os.path.isfile(self.settings.watchlist_path))

        # system_status：版本/数据源/离线/日历/缓存都有
        status = self.call(server, "system_status")["structuredContent"]
        self.assertTrue(status["status"]["version"])
        self.assertEqual(status["status"]["provider"]["active"], "fake")
        self.assertEqual(status["provider_meta"]["source"], "sina")
        self.assertFalse(status["offline"])
        self.assertEqual(status["mode"], "real")
        self.assertIn("is_closed", status["status"]["market"])

    def test_03_system_status_offline_flag(self):
        self.provider.last_meta = DataMeta(source="sample", stale=False, offline=True,
                                           as_of="2026-09-18 15:00:00", notes=["离线兜底"])
        result = self.call(self.make_server(), "system_status")
        structured = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertTrue(structured["offline"])
        self.assertEqual(structured["mode"], "offline")
        self.assertEqual(structured["source"], "sample")
        self.assertIn("离线", result["content"][0]["text"])

    # ------------------------------------------------------------------ ③ 参数校验
    def test_04_invalid_args_are_results_not_crashes(self):
        server = self.make_server()
        cases = [
            ("market_kline", {}),
            ("market_kline", {"symbol": "600519.SH", "days": "很多"}),
            ("market_kline", {"symbol": "600519.SH", "days": 99999}),
            ("market_kline", {"symbol": "600519.SH", "days": 0}),
            ("market_kline", {"symbol": "600519.SH", "freq": "hour"}),
            ("market_kline", {"symbol": "600519.SH", "adjust": "xx"}),
            ("market_sectors", {"limit": 100}),
            ("market_quotes", {"codes": "600519.SH"}),
            ("system_audit", {"limit": 999}),
            ("watchlist_add", {}),
        ]
        for name, arguments in cases:
            result = self.call(server, name, arguments)
            self.assertTrue(result["isError"], "%s %r 应返回 isError" % (name, arguments))
            self.assertIn("error", result["structuredContent"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "INVALID_ARGS")
            self.assertTrue(result["content"][0]["text"].strip())

        # 直接调 handler 时非数字 days 也不能崩（schema 校验之外的兜底）
        text, structured = self.registry.get("market_kline").handler(
            self.make_context(), {"symbol": "600519.SH", "days": "很多"})
        self.assertEqual(structured["count"], 60)
        self.assertIn("共 60 根", text)

    def test_05_unknown_symbol_is_tool_error_with_hint(self):
        result = self.call(self.make_server(), "market_quotes", {"codes": ["999999.SH"]})
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("999999.SH", text)
        self.assertIn("建议", text, "错误必须带可执行的下一步建议")
        self.assertIn("market_overview", text)

        kline = self.call(self.make_server(), "market_kline", {"symbol": "999999.SH"})
        self.assertTrue(kline["isError"])
        self.assertIn("999999.SH", kline["content"][0]["text"])

    # ------------------------------------------------------------------ ④ K 线配额
    def test_06_kline_defaults_and_caps(self):
        ctx = self.make_context()
        spec = self.registry.get("market_kline")

        text, structured = spec.handler(ctx, {"symbol": "600519.SH"})
        self.assertEqual(structured["count"], 60)
        self.assertEqual(structured["days"], 60)
        self.assertIn("日线", text)
        self.assertIn("前复权", text)
        self.assertEqual(len(structured["bars"]), 60)
        for key in ("date", "open", "high", "low", "close", "volume_wan", "amount_yi", "change_pct"):
            self.assertIn(key, structured["bars"][0])

        # 大于上限直接夹到 400（handler 层兜底；走服务器会先被 schema 拦下）
        _, capped = spec.handler(ctx, {"symbol": "600519.SH", "days": 9999})
        self.assertEqual(capped["count"], 400)
        _, weekly = spec.handler(ctx, {"symbol": "600519.SH", "days": 12, "freq": "week",
                                       "adjust": "none"})
        self.assertEqual(weekly["freq"], "week")
        self.assertEqual(weekly["adjust"], "none")
        self.assertEqual(weekly["count"], 12)
        twelve_text = spec.handler(ctx, {"symbol": "600519.SH", "days": 12})[0]
        self.assertIn("省略", twelve_text, "12 根时中间 2 根也应省略")
        self.assertEqual(len([line for line in twelve_text.splitlines() if _DATE_LINE.match(line)]), 10)

    # ------------------------------------------------------------------ ⑤ 写工具与只读模式
    def test_07_read_only_mode_blocks_watchlist_writes(self):
        ctx = self.make_context(read_only=True)
        spec = self.registry.get("watchlist_add")
        with self.assertRaises(ToolError) as caught:
            spec.handler(ctx, {"code": "600000.SH"})
        self.assertEqual(caught.exception.code, "READ_ONLY")
        self.assertTrue(caught.exception.hint.strip())

        result = self.call(self.make_server(read_only=True), "watchlist_add", {"code": "600000.SH"})
        self.assertTrue(result["isError"])
        self.assertIn("只读", result["content"][0]["text"])
        self.assertFalse(os.path.exists(self.settings.watchlist_path),
                         "只读模式不应写任何文件")

    def test_08_watchlist_write_and_audit_round_trip(self):
        ctx = self.make_context()
        add = self.registry.get("watchlist_add")
        remove = self.registry.get("watchlist_remove")

        text, structured = add.handler(ctx, {"code": "600000"})       # 简写也应规范化
        self.assertIn("600000.SH", structured["codes"])
        self.assertTrue(structured["changed"])
        self.assertEqual(structured["added"], "600000.SH")
        self.assertIn("已加入自选池", text)
        again = add.handler(ctx, {"code": "600000.SH"})[1]
        self.assertFalse(again["changed"], "重复加入应幂等")
        with open(self.settings.watchlist_path, "r", encoding="utf-8") as fh:
            persisted = json.loads(fh.read())
        self.assertIn("600000.SH", persisted["codes"])

        removed = remove.handler(ctx, {"code": "600000.SH"})[1]
        self.assertTrue(removed["changed"])
        self.assertNotIn("600000.SH", removed["codes"])
        self.assertFalse(remove.handler(ctx, {"code": "600000.SH"})[1]["changed"])

        # 审计：四次成功（加/重复加/移/重复移）+ 一次失败（非法代码）
        bad = self.call(self.make_server(), "watchlist_add", {"code": "not-a-code"})
        self.assertTrue(bad["isError"])
        audit = self.call(self.make_server(), "system_audit", {"limit": 20})
        self.assertFalse(audit["isError"])
        entries = audit["structuredContent"]["entries"]
        self.assertGreaterEqual(len(entries), 4)
        self.assertEqual(entries[-1]["tool"], "watchlist_add")
        self.assertFalse(entries[-1]["ok"])
        self.assertTrue(any(entry["tool"] == "watchlist_remove" and entry["ok"] for entry in entries))
        self.assertIn("写操作审计", audit["content"][0]["text"])

    # ------------------------------------------------------------------ ⑥ 空审计
    def test_09_system_audit_without_file(self):
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mcp-audit.log")))
        result = self.call(self.make_server(), "system_audit")
        self.assertFalse(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["entries"], [])
        self.assertEqual(structured["count"], 0)
        self.assertEqual(structured["limit"], 20)
        self.assertIn("mcp-audit.log", structured["path"])
        self.assertTrue(result["content"][0]["text"].strip())

        # 直接调 handler 也不报错
        text, direct = self.registry.get("system_audit").handler(self.make_context(), {})
        self.assertEqual(direct["entries"], [])
        self.assertTrue(text.strip())

    def test_10_quotes_explicit_codes(self):
        result = self.call(self.make_server(), "market_quotes",
                           {"codes": ["300750.SZ", "600519.SH"]})
        structured = result["structuredContent"]
        self.assertFalse(structured["from_watchlist"])
        self.assertEqual(structured["codes"], ["300750.SZ", "600519.SH"])
        self.assertEqual(structured["count"], 2)
        self.assertTrue(all(row["source"] == "sina" for row in structured["quotes"]))


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
