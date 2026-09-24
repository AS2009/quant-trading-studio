# -*- coding: utf-8 -*-
"""``tools_level2``（盘口 / 逐笔 / 资金流）工具分组的回归测试。

全部用**离线假 Provider** 注入（不联网），数据目录指向临时目录：

* 覆盖三个工具的注册 / JSON Schema / 只读标注 / group=level2；
* 覆盖成功路径（structuredContent 含契约字段）、文本截断与口径说明；
* 覆盖缺 code、非法 code、数据源不支持、数据源抛错 → ``isError`` + 可读 hint；
* 覆盖服务层短 TTL 缓存命中（meta.notes 注明）与 HTTP 路由（信封 / 400 / 502）。
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 注意：``quantstudio.api`` 依赖 Flask，精简环境（如本机系统 Python 3.9）可能没装，
# 因此按需导入：拿不到就跳过 HTTP 用例，其余（服务层 / 工具层）用例照常跑。
try:
    from quantstudio.api import create_app  # noqa: E402
except ModuleNotFoundError:                    # pragma: no cover - 取决于运行环境
    create_app = None
from quantstudio.config import Settings  # noqa: E402
from quantstudio.core.errors import DataSourceError  # noqa: E402
from quantstudio.core.models import (  # noqa: E402
    CapitalFlow,
    DataMeta,
    OrderBook,
    OrderBookLevel,
    Tick,
)
from quantstudio.mcp import tools_level2  # noqa: E402
from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.registry import ToolError  # noqa: E402
from quantstudio.mcp.server import Server  # noqa: E402
from quantstudio.mcp.tools import TOOL_MODULES, build_registry  # noqa: E402
from quantstudio.services import Level2Service, Services  # noqa: E402
from quantstudio.services.common import cache_clear, normalize_code  # noqa: E402

# --------------------------------------------------------------------------- 常量与工具清单

CODE = "600519.SH"
NAME = "贵州茅台"

LEVEL2_TOOLS = ("capital_flow", "level2_orderbook", "level2_ticks")
CONTRACT_META_KEYS = ("source", "stale", "offline", "as_of", "notes")

#: 逐笔假数据的池子上限（与真实源「约 4000 笔」对齐）
TICK_POOL = 4000


# --------------------------------------------------------------------------- 替身数据

def make_orderbook(code=CODE, source="tencent"):
    """固定五档盘口（买一 1236.90 / 卖一 1237.00，单位手）。"""
    bids = [
        OrderBookLevel(price=round(1236.90 - index * 0.05, 3), volume=10 + index,
                       amount=round((1236.90 - index * 0.05) * (10 + index) * 100.0, 2))
        for index in range(5)
    ]
    asks = [
        OrderBookLevel(price=round(1237.00 + index * 0.05, 3), volume=5 + index,
                       amount=round((1237.00 + index * 0.05) * (5 + index) * 100.0, 2))
        for index in range(5)
    ]
    return OrderBook(
        code=code,
        name=NAME,
        price=1236.95,
        prev_close=1240.00,
        ts="2026-09-24 10:00:00",
        source=source,
        levels=5,
        bids=bids,
        asks=asks,
        outer_volume=1234,
        inner_volume=1001,
    )


def make_ticks(count):
    """确定性逐笔：买 / 卖 / 中性 轮转，时间升序。"""
    ticks = []
    for index in range(int(count)):
        price = round(1236.0 + (index % 5) * 0.1, 3)
        volume = 10 + (index % 20)
        ticks.append(Tick(
            time="10:%02d:%02d" % (index // 60, index % 60),
            price=price,
            volume=volume,
            amount=round(price * volume * 100.0, 2),
            side=("buy", "sell", "neutral")[index % 3],
            change=0.1,
        ))
    return ticks


def make_capital_flow():
    """确定性资金流（分档口径与 data.level2 一致：净额 = 买入 - 卖出）。"""
    rows = {
        "super_big": (12000000.0, 3000000.0, 9),
        "big": (8000000.0, 5000000.0, 21),
        "mid": (4000000.0, 4200000.0, 80),
        "small": (1200000.0, 1500000.0, 320),
    }
    buckets = {
        key: {"buy": buy, "sell": sell, "net": round(buy - sell, 2), "count": count}
        for key, (buy, sell, count) in rows.items()
    }
    buy_amount = sum(item[0] for item in rows.values())
    sell_amount = sum(item[1] for item in rows.values())
    total = buy_amount + sell_amount
    main_net = buckets["super_big"]["net"] + buckets["big"]["net"]
    net_amount = round(buy_amount - sell_amount, 2)
    return CapitalFlow(
        code=CODE,
        name=NAME,
        ts="2026-09-24 10:00:00",
        source="tencent",
        amount_total=round(total, 2),
        buy_amount=round(buy_amount, 2),
        sell_amount=round(sell_amount, 2),
        net_amount=net_amount,
        main_net=round(main_net, 2),
        main_net_pct=round(main_net / total * 100.0, 2),
        net_pct=round(net_amount / total * 100.0, 2),
        buckets=buckets,
        tick_count=2000,
    )


class FakeLevel2Provider:
    """离线替身：只认识 600519.SH，支持盘口 / 逐笔 / 资金流三件套。"""

    name = "fake"
    ORDERBOOK_LEVELS = 5
    LEVEL2_IMPORT = False

    def __init__(self):
        self.last_meta = DataMeta(
            source="tencent",
            stale=False,
            offline=False,
            as_of="2026-09-24 10:00:00",
            latency_ms=2,
            notes=["测试替身"],
        )
        self.calls = {"orderbook": 0, "ticks": 0, "capital_flow": 0}

    def _check(self, code):
        code = normalize_code(code)
        if code != CODE:
            raise DataSourceError("未找到标的 %s 的盘口级数据" % code)
        return code

    def orderbook(self, code):
        self._check(code)
        self.calls["orderbook"] += 1
        return make_orderbook()

    def ticks(self, code, limit=600):
        self._check(code)
        self.calls["ticks"] += 1
        return make_ticks(min(int(limit), TICK_POOL))

    def capital_flow(self, code, limit=2000):
        self._check(code)
        self.calls["capital_flow"] += 1
        return make_capital_flow()

    def resolve_name(self, code):
        try:
            return NAME if normalize_code(code) == CODE else ""
        except Exception:                             # noqa: BLE001
            return ""

    def describe(self):
        return {"name": self.name, "sources": ["tencent"], "available": ["tencent"]}


class NoCapabilityProvider:
    """没有任何盘口级能力的替身（capabilities 全 False）。"""

    name = "sample"

    def __init__(self):
        self.last_meta = DataMeta(source="sample", offline=True,
                                  as_of="2026-09-24 09:00:00", notes=["离线示例数据"])

    def kline(self, symbol, days=60, freq="day", adjust="qfq"):
        return []


class BrokenLevel2Provider:
    """声明有盘口能力、但取数一律抛 DataSourceError 的替身。"""

    name = "broken"
    ORDERBOOK_LEVELS = 5

    def __init__(self):
        self.last_meta = DataMeta(source="broken", as_of="2026-09-24 10:00:00")

    def orderbook(self, code):
        raise DataSourceError("模拟盘口接口超时")

    def ticks(self, code, limit=600):
        raise DataSourceError("模拟逐笔接口超时")

    def capital_flow(self, code, limit=2000):
        raise DataSourceError("模拟资金流接口超时")


# --------------------------------------------------------------------------- 测试用例


class McpToolsLevel2TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="qs-mcp-level2-")
        cls.settings = Settings(data_dir=cls.temp_dir, offline=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        cache_clear()                                 # 服务层短缓存是进程级的，逐用例清空
        self.provider = FakeLevel2Provider()
        self.services = Services(settings=self.settings, provider=self.provider)
        self.registry = build_registry(modules=("tools_level2",))
        self.ctx = self.make_ctx(self.services)
        self.server = Server(self.registry, context=self.ctx, stderr=io.StringIO())

    # ------------------------------------------------------------------ 小工具
    def make_ctx(self, services, read_only=False):
        ctx = ToolContext(self.settings, read_only=read_only)
        ctx._services = services                        # 整体注入替身
        return ctx

    def call(self, name, args=None, ctx=None, services=None):
        registry = self.registry
        if services is not None:
            registry = build_registry(modules=("tools_level2",))
        context = ctx or (self.make_ctx(services) if services is not None else self.ctx)
        server = Server(registry, context=context, stderr=io.StringIO())
        return server.dispatch("tools/call", {"name": name, "arguments": dict(args or {})})

    def text_of(self, result):
        return "\n".join(block.get("text", "") for block in (result.get("content") or []))

    # ------------------------------------------------------------------ ① 注册与 schema
    def test_01_registry_and_schemas(self):
        self.assertEqual(self.registry.names(), sorted(LEVEL2_TOOLS))
        self.assertIn("tools_level2", TOOL_MODULES)
        for name in LEVEL2_TOOLS:
            spec = self.registry.get(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.group, "level2", name)
            self.assertTrue(spec.title.strip(), name)
            self.assertTrue(spec.description.strip(), name)
            self.assertTrue(spec.read_only, name)
            self.assertFalse(spec.destructive, name)
            self.assertTrue(spec.idempotent, name)
            self.assertTrue(spec.open_world, name)
            self.assertEqual(spec.input_schema.get("type"), "object", name)
            self.assertFalse(spec.input_schema.get("additionalProperties"), name)
            self.assertEqual(spec.input_schema.get("required"), ["code"], name)
            annotations = spec.to_mcp()["annotations"]
            self.assertTrue(annotations["readOnlyHint"], name)
            self.assertFalse(annotations["destructiveHint"], name)
        groups = self.registry.groups()
        self.assertEqual(sorted(groups["level2"]), sorted(LEVEL2_TOOLS))

        ticks_schema = self.registry.get("level2_ticks").input_schema["properties"]["limit"]
        self.assertEqual(ticks_schema["default"], 60)
        self.assertEqual((ticks_schema["minimum"], ticks_schema["maximum"]), (1, 500))
        self.assertEqual(tools_level2.DEFAULT_TICKS_LIMIT, 60)
        self.assertEqual(tools_level2.TEXT_TICKS_LIMIT, 60)

        flow_schema = self.registry.get("capital_flow").input_schema["properties"]["limit"]
        self.assertEqual(flow_schema["default"], 2000)
        self.assertEqual((flow_schema["minimum"], flow_schema["maximum"]), (1, 4000))
        self.assertEqual(self.registry.get("level2_orderbook").input_schema["properties"].keys(), {"code"})

        full = build_registry()                         # 工具总数 33 → 36（含本组 3 个）
        self.assertEqual(len(full), 36)
        self.assertTrue(set(LEVEL2_TOOLS) <= set(full.names()))

    # ------------------------------------------------------------------ ② 盘口成功路径
    def test_02_orderbook_success(self):
        result = self.call("level2_orderbook", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        for key in ("code", "name", "price", "prev_close", "ts", "source", "levels",
                    "outer_volume", "inner_volume", "summary", "bids", "asks",
                    "capabilities", "meta"):
            self.assertIn(key, data, key)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assertEqual(len(data["bids"]), 5)
        self.assertEqual(len(data["asks"]), 5)
        for level in data["bids"] + data["asks"]:
            self.assertEqual(set(level), {"price", "volume", "amount"})
        summary = data["summary"]
        for key in ("bid_volume", "ask_volume", "bid_amount", "ask_amount",
                    "imbalance_pct", "ratio", "spread", "mid"):
            self.assertIn(key, summary, key)
        self.assertAlmostEqual(summary["spread"], 0.10, places=3)
        caps = data["capabilities"]
        self.assertTrue(caps["orderbook"])
        self.assertEqual(caps["orderbook_levels"], 5)
        self.assertTrue(caps["ticks"])
        self.assertFalse(caps["orders"])
        self.assertFalse(caps["queue"])
        self.assertFalse(caps["import"])
        for key in CONTRACT_META_KEYS:
            self.assertIn(key, data["meta"], key)
        self.assertTrue(data["meta"]["offline"])        # 测试 Settings(offline=True)

        text = self.text_of(result)
        self.assertIn("五档盘口", text)
        self.assertIn("委比", text)
        self.assertIn("卖5", text)                      # 卖五在前
        self.assertIn("买1", text)
        self.assertIn("十档行情", text)                 # 能力边界说明
        self.assertIn("金额(万元)", text)
        self.assertIn("买 5 档 / 卖 5 档", text)

    # ------------------------------------------------------------------ ③ 逐笔成功路径与文本截断
    def test_03_ticks_success_and_truncation(self):
        result = self.call("level2_ticks", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        for key in ("code", "name", "count", "shown", "items", "stats", "pages", "note", "meta"):
            self.assertIn(key, data, key)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assertEqual(data["count"], 60)
        self.assertEqual(data["shown"], 60)
        self.assertEqual(len(data["items"]), 60)
        for item in data["items"]:
            self.assertEqual(set(item), {"time", "price", "volume", "amount", "side", "change"})
        for key in ("count", "buy_volume", "sell_volume", "neutral_volume", "buy_amount",
                    "sell_amount", "buy_volume_pct", "sell_volume_pct", "net_amount"):
            self.assertIn(key, data["stats"], key)
        self.assertEqual(data["stats"]["count"], 60)
        self.assertIn("约覆盖最近 4000 笔", data["note"])
        self.assertIn("第三方", data["note"])
        for key in CONTRACT_META_KEYS:
            self.assertIn(key, data["meta"], key)
        text = self.text_of(result)
        self.assertIn("逐笔成交", text)
        self.assertIn("多空统计", text)
        self.assertIn("约覆盖最近 4000 笔", text)

        larger = self.call("level2_ticks", {"code": CODE, "limit": 120})
        self.assertFalse(larger["isError"], self.text_of(larger))
        self.assertEqual(len(larger["structuredContent"]["items"]), 120)
        self.assertIn("文本只展示最近 60 条", self.text_of(larger))
        self.assertIn("完整 120 条", self.text_of(larger))

    # ------------------------------------------------------------------ ④ 资金流成功路径
    def test_04_capital_flow_success(self):
        result = self.call("capital_flow", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        for key in ("code", "name", "ts", "source", "amount_total", "buy_amount", "sell_amount",
                    "net_amount", "main_net", "main_net_pct", "net_pct", "tick_count",
                    "buckets", "meta"):
            self.assertIn(key, data, key)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assertEqual(data["tick_count"], 2000)
        self.assertEqual(self.provider.calls["capital_flow"], 1)
        for key in ("super_big", "big", "mid", "small"):
            row = data["buckets"][key]
            for field in ("label", "buy", "sell", "net", "count", "buy_pct"):
                self.assertIn(field, row, "%s.%s" % (key, field))
        self.assertEqual(data["buckets"]["super_big"]["label"], "超大单")
        self.assertAlmostEqual(data["main_net"], 12000000.0, places=2)
        text = self.text_of(result)
        self.assertIn("超大单", text)
        self.assertIn("主力净额", text)
        self.assertIn("约覆盖最近 4000 笔", text)

    # ------------------------------------------------------------------ ⑤ 缺少 / 非法 code
    def test_05_missing_and_invalid_code(self):
        for name in LEVEL2_TOOLS:
            result = self.call(name, {})
            self.assertTrue(result["isError"], name)
            error = result["structuredContent"]["error"]
            self.assertEqual(error["code"], "INVALID_ARGS", name)
            self.assertIn("code", self.text_of(result))
            spec = self.registry.get(name)
            with self.assertRaises(ToolError) as raised:
                spec.handler(self.ctx, {})
            self.assertEqual(raised.exception.code, "INVALID_ARGS", name)
            self.assertTrue(raised.exception.hint, name)

        bad = self.call("level2_orderbook", {"code": "abc"})
        self.assertTrue(bad["isError"])
        self.assertEqual(bad["structuredContent"]["error"]["code"], "VALIDATION")
        self.assertTrue(self.text_of(bad).strip())

    # ------------------------------------------------------------------ ⑥ 数据源不可用 / 抛错
    def test_06_source_errors_are_readable(self):
        # ① 数据源没有盘口级能力 → 业务异常（不是 500），工具转 isError + hint
        unsupported = Services(settings=self.settings, provider=NoCapabilityProvider())
        for name, args in (("level2_orderbook", {"code": CODE}),
                           ("level2_ticks", {"code": CODE}),
                           ("capital_flow", {"code": CODE})):
            result = self.call(name, args, services=unsupported)
            self.assertTrue(result["isError"], name)
            self.assertEqual(result["structuredContent"]["error"]["code"], "SERVICE", name)
            text = self.text_of(result)
            self.assertIn("不支持", text, name)
            self.assertIn("system_status", text, name)

        # ② 数据源抛 DataSourceError → 同样 isError，hint 可读
        broken = Services(settings=self.settings, provider=BrokenLevel2Provider())
        for name, args in (("level2_orderbook", {"code": CODE}),
                           ("level2_ticks", {"code": CODE}),
                           ("capital_flow", {"code": CODE})):
            result = self.call(name, args, services=broken)
            self.assertTrue(result["isError"], name)
            error = result["structuredContent"]["error"]
            self.assertEqual(error["code"], "SERVICE", name)
            self.assertIn("模拟", error["message"], name)
            self.assertIn("建议：", self.text_of(result), name)
            self.assertIn("system_status", self.text_of(result), name)

        # ③ 未知标的（替身抛 DataSourceError）→ isError 而不是协议错误
        unknown = self.call("level2_orderbook", {"code": "000001.SZ"})
        self.assertTrue(unknown["isError"])
        self.assertIn("000001.SZ", self.text_of(unknown))

    # ------------------------------------------------------------------ ⑦ 短 TTL 缓存
    def test_07_short_ttl_cache_and_meta(self):
        service = self.services.level2
        self.assertEqual(service.ttl_orderbook, 2.0)
        self.assertEqual(service.ttl_ticks, 10.0)

        first = self.call("level2_orderbook", {"code": CODE})
        second = self.call("level2_orderbook", {"code": CODE})
        self.assertFalse(first["isError"])
        self.assertFalse(second["isError"])
        self.assertEqual(self.provider.calls["orderbook"], 1)   # 第二次命中 2 秒缓存
        notes = second["structuredContent"]["meta"]["notes"]
        self.assertTrue(any("缓存" in str(item) for item in notes), notes)
        self.assertFalse(any("缓存" in str(item) for item in
                             first["structuredContent"]["meta"]["notes"]))

        # ttl=0 关闭缓存：每次调用都取数
        cache_clear("level2:orderbook:%s" % CODE)
        standalone = Level2Service(provider=self.provider, settings=self.settings,
                                   ttl_orderbook=0, ttl_ticks=0)
        standalone.orderbook(CODE)
        standalone.orderbook(CODE)
        self.assertEqual(self.provider.calls["orderbook"], 3)

    # ------------------------------------------------------------------ ⑧ HTTP 路由
    @unittest.skipIf(create_app is None, "缺少 Flask（quantstudio.api 不可用）")
    def test_08_http_routes(self):
        app = create_app(settings=self.settings, services=self.services)
        client = app.test_client()

        book = client.get("/api/level2/%s/orderbook" % CODE)
        self.assertEqual(book.status_code, 200)
        payload = book.get_json()
        for key in ("data", "as_of", "meta"):
            self.assertIn(key, payload)
        self.assertIn("capabilities", payload["data"])
        self.assertEqual(payload["meta"]["as_of"], payload["data"]["meta"]["as_of"])
        self.assertIn("notes", payload["meta"])

        ticks = client.get("/api/level2/%s/ticks?limit=10" % CODE)
        self.assertEqual(ticks.status_code, 200)
        self.assertEqual(ticks.get_json()["data"]["count"], 10)

        flow = client.get("/api/level2/%s/flow?limit=100" % CODE)
        self.assertEqual(flow.status_code, 200)
        self.assertIn("main_net", flow.get_json()["data"])

        bad_limit = client.get("/api/level2/%s/ticks?limit=abc" % CODE)
        self.assertEqual(bad_limit.status_code, 400)
        self.assertIn("error", bad_limit.get_json())
        bad_code = client.get("/api/level2/abc/orderbook")
        self.assertEqual(bad_code.status_code, 400)

        unsupported = create_app(
            settings=self.settings,
            services=Services(settings=self.settings, provider=NoCapabilityProvider()),
        )
        unavailable = unsupported.test_client().get("/api/level2/%s/orderbook" % CODE)
        self.assertEqual(unavailable.status_code, 502)
        self.assertIn("error", unavailable.get_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
