# -*- coding: utf-8 -*-
"""``tools_level2``（盘口 / 逐笔 / 资金流 / L2 工具箱）工具分组的回归测试。

全部用**离线假 Provider** 注入（不联网），数据目录指向临时目录：

* 覆盖 8 个工具的注册 / JSON Schema / 只读标注 / group=level2；
* 覆盖成功路径（structuredContent 含契约字段）、文本截断与口径说明；
* 覆盖 L2 工具箱（大单 / 分时 / 封板 / 扫描 / 排行）：缺省自选池、空自选池报错、
  ``sides`` 与 ``threshold`` / ``limit`` / ``top`` 参数透传；
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
from quantstudio.core.errors import DataSourceError, ProviderUnavailable  # noqa: E402
from quantstudio.core.models import (  # noqa: E402
    CapitalFlow,
    DataMeta,
    OrderBook,
    OrderBookLevel,
    Quote,
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
OTHER = "000001.SZ"
NAME = "贵州茅台"

#: 本组的全部 8 个工具（3 个既有 + 5 个 L2 工具箱）
LEVEL2_TOOLS = ("capital_flow", "l2_big_orders", "l2_flow_rank", "l2_flow_series",
                "l2_scan", "l2_seal_status", "level2_orderbook", "level2_ticks")
#: 需要 code 参数的 6 个工具
CODE_TOOLS = ("capital_flow", "l2_big_orders", "l2_flow_series", "l2_seal_status",
              "level2_orderbook", "level2_ticks")
#: codes 缺省 = 自选池的 2 个批量工具
BATCH_TOOLS = ("l2_flow_rank", "l2_scan")
CONTRACT_META_KEYS = ("source", "stale", "offline", "as_of", "notes")

#: 逐笔假数据的池子上限（与真实源「约 4000 笔」对齐）
TICK_POOL = 4000

#: 契约字段（与 docs/level2.md §3.4 对齐）
BIG_ORDERS_KEYS = ("code", "name", "threshold", "count", "shown", "items", "summary",
                   "tick_sample", "note", "meta", "capabilities")
BIG_ORDERS_ITEM_KEYS = ("time", "price", "volume", "amount", "side", "bucket", "bucket_label")
BIG_ORDERS_SUMMARY_KEYS = ("count", "buy_count", "sell_count", "buy_amount", "sell_amount",
                           "net_amount", "buy_amount_pct", "amount_share_pct", "biggest")
SERIES_KEYS = ("code", "name", "minutes", "tick_sample", "series", "buckets", "main_net",
               "main_net_pct", "amount_total", "note", "meta", "capabilities")
SERIES_ROW_KEYS = ("time", "buy", "sell", "net", "cum_net", "amount", "count")
SEAL_KEYS = ("state", "label", "limit_pct", "limit_pct_text", "limit_up_price",
             "limit_down_price", "distance_pct", "seal_volume", "seal_amount", "seal_ratio",
             "amount_total")
SEAL_TOOL_KEYS = ("code", "name", "price", "prev_close", "seal", "seal_text", "note", "meta",
                  "capabilities")
SCAN_KEYS = ("count", "requested", "items", "failures", "note", "meta", "capabilities")
SCAN_ROW_KEYS = ("code", "name", "price", "change_pct", "bid_volume", "ask_volume",
                 "imbalance_pct", "ratio", "spread", "outer_volume", "inner_volume",
                 "volume_ratio", "seal_state", "seal_label", "seal_amount", "distance_pct")
RANK_KEYS = ("count", "requested", "items", "failures", "note", "meta", "capabilities")
RANK_ROW_KEYS = ("code", "name", "main_net", "main_net_pct", "net_amount", "buy_amount",
                 "sell_amount", "amount_total", "tick_count")


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


def make_limit_up_orderbook(code=CODE):
    """涨停封板盘口：现价 = 涨停价（1240 × 1.1 = 1364.00），买一挂 5000 手封单。"""
    book = make_orderbook(code=code)
    book.price = 1364.00
    book.bids[0].price = 1364.00
    book.bids[0].volume = 5000
    book.bids[0].amount = round(1364.00 * 5000 * 100.0, 2)
    return book


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
    """离线替身：只认识 600519.SH，支持盘口 / 逐笔 / 资金流三件套 + 简单快照。"""

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
        self.calls = {"orderbook": 0, "ticks": 0, "capital_flow": 0, "latest_quotes": 0}

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

    def latest_quotes(self, codes):
        """快照替身：给扫描 / 封板工具补上量比、涨跌幅与当日成交额。"""
        self.calls["latest_quotes"] += 1
        rows = []
        for item in codes or []:
            try:
                if normalize_code(item) != CODE:
                    continue
            except Exception:                     # noqa: BLE001
                continue
            rows.append(Quote(code=CODE, name=NAME, price=1236.95, prev_close=1240.00,
                              change=-3.05, change_pct=-0.25, amount_yi=2.5, volume_ratio=1.5,
                              ts="2026-09-24 10:00:00", source="tencent"))
        return rows

    def resolve_name(self, code):
        try:
            return NAME if normalize_code(code) == CODE else ""
        except Exception:                             # noqa: BLE001
            return ""

    def describe(self):
        return {"name": self.name, "sources": ["tencent"], "available": ["tencent"]}


class LimitUpLevel2Provider(FakeLevel2Provider):
    """涨停替身：盘口封在涨停价，快照给出当日成交额（用于封成比）。"""

    def orderbook(self, code):
        self._check(code)
        self.calls["orderbook"] += 1
        return make_limit_up_orderbook()

    def latest_quotes(self, codes):
        self.calls["latest_quotes"] += 1
        return [Quote(code=CODE, name=NAME, price=1364.00, prev_close=1240.00,
                      change=124.00, change_pct=10.00, amount_yi=3.0, volume_ratio=2.5,
                      ts="2026-09-24 10:00:00", source="tencent")]


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


class FakeMarketService:
    """自选池替身：``watchlist()`` 返回固定代码（记录调用次数）。"""

    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = 0

    def watchlist(self):
        self.calls += 1
        return list(self.codes)


class RecordingLevel2Service:
    """包一层记录调用参数（``sides`` / ``threshold`` / ``limit`` 透传断言用）。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def big_orders(self, code, threshold=None, limit=None, sides=None):
        self.calls.append({"name": "big_orders", "code": code, "threshold": threshold,
                           "limit": limit, "sides": sides})
        return self.inner.big_orders(code, threshold=threshold, limit=limit, sides=sides)

    def flow_series(self, code, limit=None):
        self.calls.append({"name": "flow_series", "code": code, "limit": limit})
        return self.inner.flow_series(code, limit=limit)

    def seal_status(self, code):
        self.calls.append({"name": "seal_status", "code": code})
        return self.inner.seal_status(code)

    def scan(self, codes, limit=10):
        self.calls.append({"name": "scan", "codes": list(codes), "limit": limit})
        return self.inner.scan(codes, limit=limit)

    def flow_rank(self, codes, limit=1000, top=10):
        self.calls.append({"name": "flow_rank", "codes": list(codes), "limit": limit,
                           "top": top})
        return self.inner.flow_rank(codes, limit=limit, top=top)


class ErrorLevel2Service:
    """服务层替身：L2 工具箱接口一律抛指定异常（模拟数据源不可用 / 离线无数据）。"""

    def __init__(self, error):
        self.error = error
        self.calls = []

    def _raise(self, name):
        self.calls.append(name)
        raise self.error

    def big_orders(self, *args, **kwargs):
        self._raise("big_orders")

    def flow_series(self, *args, **kwargs):
        self._raise("flow_series")

    def seal_status(self, *args, **kwargs):
        self._raise("seal_status")

    def scan(self, *args, **kwargs):
        self._raise("scan")

    def flow_rank(self, *args, **kwargs):
        self._raise("flow_rank")


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

    def use_market(self, codes):
        """把自选池替身挂到当前 Services 上（返回替身，便于断言调用次数）。"""
        market = FakeMarketService(codes)
        self.services.market = market
        return market

    def assert_contract(self, data, keys, label=""):
        for key in keys:
            self.assertIn(key, data, "%s%s" % (label, key))

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
            annotations = spec.to_mcp()["annotations"]
            self.assertTrue(annotations["readOnlyHint"], name)
            self.assertFalse(annotations["destructiveHint"], name)
            self.assertTrue(annotations["idempotentHint"], name)
            self.assertTrue(annotations["openWorldHint"], name)
        for name in CODE_TOOLS:
            self.assertEqual(self.registry.get(name).input_schema.get("required"), ["code"], name)
        for name in BATCH_TOOLS:                       # codes 可选（缺省 = 自选池）
            self.assertNotIn("required", self.registry.get(name).input_schema, name)
            self.assertIn("codes", self.registry.get(name).input_schema["properties"], name)
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
        self.assertEqual(self.registry.get("level2_orderbook").input_schema["properties"].keys(),
                         {"code"})

        full = build_registry()                         # 工具总数 36 → 41（本组 3 → 8）
        self.assertEqual(len(full), 41)
        self.assertTrue(set(LEVEL2_TOOLS) <= set(full.names()))

    # ------------------------------------------------------------------ ①b 新增工具 schema
    def test_02_l2_tool_schemas(self):
        props = self.registry.get("l2_big_orders").input_schema["properties"]
        self.assertEqual(set(props), {"code", "threshold", "limit", "sides"})
        self.assertEqual(props["threshold"]["type"], "number")
        self.assertEqual(props["threshold"]["default"], 1000000.0)
        self.assertEqual(props["threshold"]["minimum"], 1.0)
        self.assertEqual((props["limit"]["default"], props["limit"]["minimum"],
                          props["limit"]["maximum"]), (50, 1, 200))
        self.assertEqual(props["sides"]["type"], "array")
        self.assertEqual(props["sides"]["items"]["type"], "string")
        self.assertEqual(tools_level2.DEFAULT_BIG_ORDER_THRESHOLD, 1000000.0)
        self.assertEqual(tools_level2.TEXT_BIG_ORDERS_LIMIT, 30)

        series = self.registry.get("l2_flow_series").input_schema["properties"]
        self.assertEqual(set(series), {"code", "limit"})
        self.assertEqual((series["limit"]["default"], series["limit"]["minimum"],
                          series["limit"]["maximum"]), (2000, 1, 4000))
        self.assertEqual(tools_level2.TEXT_FLOW_MINUTES, 10)

        self.assertEqual(self.registry.get("l2_seal_status").input_schema["properties"].keys(),
                         {"code"})

        scan = self.registry.get("l2_scan").input_schema["properties"]
        self.assertEqual(set(scan), {"codes", "limit"})
        self.assertEqual(scan["codes"]["type"], "array")
        self.assertEqual(scan["codes"]["items"]["type"], "string")
        self.assertEqual(scan["codes"]["maxItems"], 10)
        self.assertEqual((scan["limit"]["default"], scan["limit"]["minimum"],
                          scan["limit"]["maximum"]), (10, 1, 10))
        self.assertEqual(tools_level2.MAX_SCAN_CODES, 10)

        rank = self.registry.get("l2_flow_rank").input_schema["properties"]
        self.assertEqual(set(rank), {"codes", "limit", "top"})
        self.assertEqual((rank["limit"]["default"], rank["limit"]["minimum"],
                          rank["limit"]["maximum"]), (1000, 1, 4000))
        self.assertEqual((rank["top"]["default"], rank["top"]["minimum"], rank["top"]["maximum"]),
                         (10, 1, 10))

    # ------------------------------------------------------------------ ② 盘口成功路径
    def test_03_orderbook_success(self):
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
    def test_04_ticks_success_and_truncation(self):
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
    def test_05_capital_flow_success(self):
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

    # ------------------------------------------------------------------ ⑥ 大单追踪
    def test_06_l2_big_orders_success(self):
        result = self.call("l2_big_orders", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assert_contract(data, BIG_ORDERS_KEYS)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assertEqual(data["threshold"], 1000000.0)
        self.assertEqual(data["tick_sample"], 2000)     # max(50*20, 2000)
        self.assertGreater(data["count"], 0)
        self.assertEqual(data["shown"], len(data["items"]))
        for item in data["items"]:
            self.assertEqual(set(item), set(BIG_ORDERS_ITEM_KEYS))
        self.assert_contract(data["summary"], BIG_ORDERS_SUMMARY_KEYS)
        self.assertTrue(data["summary"]["biggest"])
        self.assertIn("超大单", data["summary"]["biggest"]["bucket_label"])
        times = [row["time"] for row in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))     # 服务层按时间倒序
        self.assertIn("约覆盖最近 4000 笔", data["note"])
        self.assertIn("第三方", data["note"])

        text = self.text_of(result)
        self.assertIn("大单追踪", text)
        self.assertIn("时间倒序", text)
        self.assertIn("统计：大单", text)
        self.assertIn("净额", text)
        self.assertIn("占样本成交额", text)
        self.assertIn("最大单笔", text)
        self.assertIn("约覆盖最近 4000 笔", text)
        self.assertIn("第三方盘口标记", text)
        self.assertIn("金额(万元)", text)
        self.assertIn("档位", text)
        self.assertIn("超大单", text)                   # 100 万门槛 = 超大单分档线
        self.assertIn("文本只列时间倒序前 30 条", text)  # 默认 limit=50 → 文本截断到 30
        self.assertEqual(tools_level2.TEXT_BIG_ORDERS_LIMIT, 30)

        # 更低样本 / 更高门槛：没有命中时文本也要可读
        empty = self.call("l2_big_orders", {"code": CODE, "threshold": 900000000})
        self.assertFalse(empty["isError"], self.text_of(empty))
        self.assertEqual(empty["structuredContent"]["count"], 0)
        self.assertIn("没有单笔 ≥ 门槛的成交", self.text_of(empty))
        self.assertIn("最大单笔：无", self.text_of(empty))

    # ------------------------------------------------------------------ ⑦ sides / 参数透传
    def test_07_l2_big_orders_params_passthrough(self):
        services = Services(settings=self.settings, provider=self.provider)
        recorder = RecordingLevel2Service(services.level2)
        services.level2 = recorder
        services.market = FakeMarketService([CODE])

        result = self.call("l2_big_orders",
                           {"code": CODE, "threshold": 500000, "limit": 5, "sides": ["sell"]},
                           services=services)
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assertEqual(recorder.calls[-1]["code"], CODE)
        self.assertEqual(recorder.calls[-1]["threshold"], 500000)
        self.assertEqual(recorder.calls[-1]["limit"], 5)
        self.assertEqual(recorder.calls[-1]["sides"], ["sell"])   # sides 原样透传
        self.assertEqual(data["threshold"], 500000.0)
        self.assertEqual(len(data["items"]), 5)
        for item in data["items"]:
            self.assertEqual(item["side"], "sell")
        self.assertIn("≥ 50.00 万元", self.text_of(result))

        # 缺省 sides → None（买卖都看）
        defaults = self.call("l2_big_orders", {"code": CODE, "limit": 3}, services=services)
        self.assertFalse(defaults["isError"], self.text_of(defaults))
        self.assertIsNone(recorder.calls[-1]["sides"])
        self.assertEqual(recorder.calls[-1]["threshold"], 1000000.0)

    # ------------------------------------------------------------------ ⑧ 资金流分时
    def test_08_l2_flow_series_success(self):
        result = self.call("l2_flow_series", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assert_contract(data, SERIES_KEYS)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assertEqual(data["tick_sample"], 2000)
        self.assertGreater(data["minutes"], 10)
        self.assertEqual(data["minutes"], len(data["series"]))
        for row in data["series"]:
            self.assertEqual(set(row), set(SERIES_ROW_KEYS))
        for key in ("super_big", "big", "mid", "small"):
            row = data["buckets"][key]
            for field in ("label", "buy", "sell", "net", "count", "buy_pct"):
                self.assertIn(field, row, "%s.%s" % (key, field))
        self.assertIn("main_net", data)
        self.assertIn("main_net_pct", data)

        text = self.text_of(result)
        self.assertIn("资金流分时", text)
        self.assertIn("主力净额", text)
        self.assertIn("四档净额", text)
        self.assertIn("超大单", text)
        self.assertIn("最近 10 分钟", text)              # 只列最近 10 分钟
        self.assertIn("累计净额", text)
        self.assertIn("第三方方向标记", text)
        self.assertIn("文本只列最近 10 分钟", text)
        self.assertIn("完整 %d 分钟序列见结构化 series" % data["minutes"], text)

    # ------------------------------------------------------------------ ⑨ 封板状态
    def test_09_l2_seal_status(self):
        result = self.call("l2_seal_status", {"code": CODE})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assert_contract(data, SEAL_TOOL_KEYS)
        self.assertEqual(data["code"], CODE)
        self.assertEqual(data["name"], NAME)
        self.assert_contract(data["seal"], SEAL_KEYS)
        seal = data["seal"]
        self.assertEqual(seal["state"], "normal")
        self.assertEqual(seal["label"], "未封板")
        self.assertAlmostEqual(seal["limit_pct"], 0.10, places=4)
        self.assertIn("主板 10%", seal["limit_pct_text"])
        self.assertAlmostEqual(seal["limit_up_price"], 1364.00, places=2)
        self.assertAlmostEqual(seal["limit_down_price"], 1116.00, places=2)
        self.assertAlmostEqual(seal["distance_pct"], 10.27, places=2)
        self.assertEqual(seal["seal_volume"], 0)
        self.assertEqual(seal["seal_amount"], 0.0)
        self.assertEqual(seal["seal_ratio"], 0.0)
        self.assertAlmostEqual(seal["amount_total"], 250000000.0, places=2)

        text = self.text_of(result)
        self.assertIn("封板状态", text)
        self.assertIn("状态：未封板", text)
        self.assertIn("涨跌停幅度", text)
        self.assertIn("涨停价 1364.00", text)
        self.assertIn("距涨停", text)
        self.assertIn("封单：0 手", text)
        self.assertIn("封成比", text)
        self.assertIn("仅当前快照", text)
        self.assertIn("不承诺", text)

        # 涨停 + 封单路径（成交量 3 亿元 → 封成比 ≈ 227%）
        # 上面那次调用把封板结果写进了进程级短缓存（key 与标的绑定），先清掉再换数据源
        cache_clear("level2:seal:%s" % CODE)
        limit_up = self.call("l2_seal_status", {"code": CODE},
                             services=Services(settings=self.settings,
                                               provider=LimitUpLevel2Provider()))
        self.assertFalse(limit_up["isError"], self.text_of(limit_up))
        seal = limit_up["structuredContent"]["seal"]
        self.assertEqual(seal["state"], "limit_up")
        self.assertEqual(seal["label"], "涨停")
        self.assertEqual(seal["seal_volume"], 5000)
        self.assertAlmostEqual(seal["seal_amount"], 682000000.0, places=2)
        self.assertAlmostEqual(seal["seal_ratio"], 227.33, places=2)
        sealed_text = self.text_of(limit_up)
        self.assertIn("状态：涨停", sealed_text)
        self.assertIn("已封板", sealed_text)
        self.assertIn("封单：5000 手", sealed_text)
        self.assertIn("227.33%", sealed_text)

    # ------------------------------------------------------------------ ⑩ 盘口扫描
    def test_10_l2_scan_watchlist_and_rows(self):
        market = self.use_market([CODE, OTHER])
        result = self.call("l2_scan", {"limit": 5})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assert_contract(data, SCAN_KEYS)
        self.assertEqual(market.calls, 1)               # 缺省取自选池
        self.assertTrue(data["from_watchlist"])
        self.assertEqual(data["requested"], 2)
        self.assertEqual(data["count"], 1)              # 000001.SZ 让替身抛出 → failures
        self.assertEqual(len(data["failures"]), 1)
        self.assertIn("000001.SZ", data["failures"][0]["code"])
        row = data["items"][0]
        self.assert_contract(row, SCAN_ROW_KEYS, "row.")
        self.assertEqual(row["code"], CODE)
        self.assertEqual(row["name"], NAME)
        self.assertAlmostEqual(row["change_pct"], -0.25, places=2)
        self.assertAlmostEqual(row["volume_ratio"], 1.5, places=2)
        self.assertEqual(row["seal_state"], "normal")

        text = self.text_of(result)
        self.assertIn("盘口异动扫描", text)
        self.assertIn("自选池 2 只", text)
        self.assertIn("代码", text)
        self.assertIn("委比", text)
        self.assertIn("买盘占优", text)                  # 委差方向（委买 > 委卖）
        self.assertIn("量比", text)
        self.assertIn("1.50", text)
        self.assertIn("距涨停 10.27%", text)
        self.assertIn("最多 10 只", text)
        self.assertIn("失败：000001.SZ", text)
        self.assertIn(NAME, text)

        # 显式 codes：不读自选池，且按委比排序
        explicit = self.call("l2_scan", {"codes": [CODE]})
        self.assertFalse(explicit["isError"], self.text_of(explicit))
        self.assertEqual(market.calls, 1)               # 没有再次读自选池
        payload = explicit["structuredContent"]
        self.assertFalse(payload["from_watchlist"])
        self.assertEqual(payload["requested"], 1)
        self.assertIn("指定 1 只", self.text_of(explicit))

    # ------------------------------------------------------------------ ⑪ 扫描 / 排行：空自选池
    def test_11_l2_batch_empty_watchlist(self):
        for name in BATCH_TOOLS:
            self.use_market([])                         # 自选池为空
            result = self.call(name, {})
            self.assertTrue(result["isError"], name)
            error = result["structuredContent"]["error"]
            self.assertEqual(error["code"], "NO_CODES", name)
            text = self.text_of(result)
            self.assertIn("自选池为空", text, name)
            self.assertIn("codes", text, name)
            self.assertIn("建议：", text, name)
            self.assertIn("watchlist_add", text, name)

    # ------------------------------------------------------------------ ⑫ 资金流排行
    def test_12_l2_flow_rank(self):
        rank = self.registry.get("l2_flow_rank")
        self.assertNotIn("required", rank.input_schema)
        market = self.use_market([CODE, OTHER])
        result = self.call("l2_flow_rank", {"limit": 1000, "top": 10})
        self.assertFalse(result["isError"], self.text_of(result))
        data = result["structuredContent"]
        self.assert_contract(data, RANK_KEYS)
        self.assertEqual(market.calls, 1)
        self.assertTrue(data["from_watchlist"])
        self.assertEqual(data["requested"], 2)
        self.assertEqual(data["count"], 1)              # 000001.SZ 逐笔失败 → failures
        self.assertEqual(len(data["failures"]), 1)
        row = data["items"][0]
        self.assert_contract(row, RANK_ROW_KEYS, "row.")
        self.assertEqual(row["code"], CODE)
        self.assertEqual(row["tick_count"], 1000)
        main_nets = [item["main_net"] for item in data["items"]]
        self.assertEqual(main_nets, sorted(main_nets, reverse=True))

        text = self.text_of(result)
        self.assertIn("资金流排行", text)
        self.assertIn("自选池 2 只", text)
        self.assertIn("主力净额", text)
        self.assertIn("占比", text)
        self.assertIn("每只约 10–25 秒，最多 10 只", text)
        self.assertIn("失败：000001.SZ", text)
        self.assertIn(NAME, text)

        # 显式 codes + top：参数透传（top 会夹到 1-10，limit 夹到 1-4000）
        services = Services(settings=self.settings, provider=self.provider)
        recorder = RecordingLevel2Service(services.level2)
        services.level2 = recorder
        services.market = FakeMarketService([CODE])
        explicit = self.call("l2_flow_rank", {"codes": [CODE], "limit": 300, "top": 3},
                             services=services)
        self.assertFalse(explicit["isError"], self.text_of(explicit))
        self.assertEqual(recorder.calls[-1]["codes"], [CODE])
        self.assertEqual(recorder.calls[-1]["limit"], 300)
        self.assertEqual(recorder.calls[-1]["top"], 3)
        self.assertEqual(explicit["structuredContent"]["items"][0]["tick_count"], 300)
        self.assertFalse(explicit["structuredContent"]["from_watchlist"])

        # 扫描的参数透传（codes / limit）
        scanning = self.call("l2_scan", {"codes": [CODE], "limit": 3}, services=services)
        self.assertFalse(scanning["isError"], self.text_of(scanning))
        self.assertEqual(recorder.calls[-1]["name"], "scan")
        self.assertEqual(recorder.calls[-1]["codes"], [CODE])
        self.assertEqual(recorder.calls[-1]["limit"], 3)

    # ------------------------------------------------------------------ ⑬ 缺少 / 非法 code
    def test_13_missing_and_invalid_code(self):
        for name in CODE_TOOLS:
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

        for name in ("l2_big_orders", "l2_flow_series", "l2_seal_status"):
            bad_batch = self.call(name, {"code": "abc"})
            self.assertTrue(bad_batch["isError"], name)
            self.assertTrue(self.text_of(bad_batch).strip(), name)

        # 白名单：不认识的参数被挡住（sides / codes 拼错能及时反馈）
        typo = self.call("l2_big_orders", {"code": CODE, "side": ["buy"]})
        self.assertTrue(typo["isError"])
        self.assertEqual(typo["structuredContent"]["error"]["code"], "INVALID_ARGS")
        self.assertIn("side", self.text_of(typo))

    # ------------------------------------------------------------------ ⑭ 数据源不可用 / 抛错
    def test_14_source_errors_are_readable(self):
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
        unknown = self.call("level2_orderbook", {"code": OTHER})
        self.assertTrue(unknown["isError"])
        self.assertIn(OTHER, self.text_of(unknown))

    # ------------------------------------------------------------------ ⑮ L2 工具箱的服务层异常
    def test_15_l2_tools_service_errors(self):
        cases = ((DataSourceError("模拟 L2 工具接口超时"), "模拟"),
                 (ProviderUnavailable("离线模式下没有取到 600519.SH 的逐笔成交数据"), "离线"))
        for error, marker in cases:
            services = Services(settings=self.settings, provider=self.provider)
            services.level2 = ErrorLevel2Service(error)
            services.market = FakeMarketService([CODE])          # 批量工具缺省自选池
            for name, args in (("l2_big_orders", {"code": CODE}),
                               ("l2_flow_series", {"code": CODE}),
                               ("l2_seal_status", {"code": CODE}),
                               ("l2_scan", {}),
                               ("l2_flow_rank", {})):
                result = self.call(name, args, services=services)
                self.assertTrue(result["isError"], name)
                payload = result["structuredContent"]["error"]
                self.assertEqual(payload["code"], "SERVICE", name)
                self.assertIn(marker, payload["message"], name)
                text = self.text_of(result)
                self.assertIn("建议：", text, name)              # hint 可读
                self.assertIn("system_status", text, name)
                self.assertIn("watchlist_add" if name in BATCH_TOOLS else "标的代码", text, name)

        # 数据源抛错但被批量工具逐只容错 → 不是 isError，失败写在 failures 里
        broken = Services(settings=self.settings, provider=BrokenLevel2Provider())
        broken.market = FakeMarketService([CODE])
        partial = self.call("l2_scan", {}, services=broken)
        self.assertFalse(partial["isError"], self.text_of(partial))
        self.assertEqual(partial["structuredContent"]["count"], 0)
        self.assertEqual(len(partial["structuredContent"]["failures"]), 1)
        self.assertIn("模拟", partial["structuredContent"]["failures"][0]["error"])

    # ------------------------------------------------------------------ ⑯ 短 TTL 缓存
    def test_16_short_ttl_cache_and_meta(self):
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

        # L2 工具箱同样走短缓存（大单 10 秒）
        first_big = self.call("l2_big_orders", {"code": CODE})
        ticks_after_first = self.provider.calls["ticks"]
        second_big = self.call("l2_big_orders", {"code": CODE})
        self.assertFalse(first_big["isError"])
        self.assertFalse(second_big["isError"])
        self.assertEqual(self.provider.calls["ticks"], ticks_after_first)
        self.assertTrue(any("缓存" in str(item) for item in
                            second_big["structuredContent"]["meta"]["notes"]))

        # ttl=0 关闭缓存：每次调用都取数
        cache_clear("level2:orderbook:%s" % CODE)
        standalone = Level2Service(provider=self.provider, settings=self.settings,
                                   ttl_orderbook=0, ttl_ticks=0)
        standalone.orderbook(CODE)
        standalone.orderbook(CODE)
        self.assertEqual(self.provider.calls["orderbook"], 3)

    # ------------------------------------------------------------------ ⑰ HTTP 路由
    @unittest.skipIf(create_app is None, "缺少 Flask（quantstudio.api 不可用）")
    def test_17_http_routes(self):
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
