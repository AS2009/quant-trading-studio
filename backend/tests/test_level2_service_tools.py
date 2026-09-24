# -*- coding: utf-8 -*-
"""L2 工具的服务层与 HTTP 契约测试（离线，全部替身数据源）。

覆盖五个工具：大单追踪 / 资金流分时 / 封板状态 / 盘口扫描 / 资金流排行 ——
既验证服务层 JSON 形状与排序规则，也验证 HTTP 路由（缺 Flask 时自动跳过）。
"""

import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from quantstudio.api import create_app
except ModuleNotFoundError:                        # pragma: no cover - 取决于运行环境
    create_app = None

from quantstudio.config import Settings  # noqa: E402
from quantstudio.core.errors import DataSourceError  # noqa: E402
from quantstudio.core.models import OrderBook, OrderBookLevel, Quote, Tick  # noqa: E402
from quantstudio.services import Services  # noqa: E402
from quantstudio.services.level2_service import Level2Service  # noqa: E402

CODE = "600519.SH"
OTHER = "000001.SZ"


def _ticks(scale=1.0):
    return [
        Tick(time="09:30:05", price=100.0, volume=1500, amount=1_500_000.0 * scale, side="buy"),
        Tick(time="09:30:40", price=100.1, volume=300, amount=300_300.0 * scale, side="sell"),
        Tick(time="09:31:02", price=100.2, volume=2500, amount=2_505_000.0 * scale, side="buy"),
        Tick(time="09:31:20", price=99.9, volume=100, amount=99_900.0 * scale, side="neutral"),
    ]


class StubProvider(object):
    """确定性数据源：盘口涨停样本 + 四笔逐笔；``broken`` 时全部报错。"""

    name = "stub"
    ORDERBOOK_LEVELS = 5

    def __init__(self, broken=False):
        self.broken = broken
        self.calls = []

    def orderbook(self, code):
        self.calls.append(("orderbook", code))
        if self.broken:
            raise DataSourceError("模拟盘口故障")
        if code == OTHER:
            return OrderBook(code=code, name="平安银行", price=11.0, prev_close=11.0,
                             bids=[OrderBookLevel(price=11.0, volume=100, amount=110_000.0)],
                             asks=[OrderBookLevel(price=11.01, volume=900, amount=990_900.0)], levels=1)
        # 涨停：昨收 100 → 涨停价 110，买一封单 20000 手
        return OrderBook(code=code, name="贵州茅台", price=110.0, prev_close=100.0,
                         bids=[OrderBookLevel(price=110.0, volume=20_000, amount=220_000_000.0),
                               OrderBookLevel(price=109.9, volume=100, amount=1_099_000.0)],
                         asks=[], levels=2, outer_volume=13_918, inner_volume=17_321)

    def ticks(self, code, limit=600):
        self.calls.append(("ticks", code, limit))
        if self.broken:
            raise DataSourceError("模拟逐笔故障")
        rows = _ticks(0.5 if code == OTHER else 1.0)
        return rows[-int(limit):] if limit and int(limit) > 0 else []

    def latest_quotes(self, symbols):
        self.calls.append(("latest_quotes", tuple(symbols)))
        if self.broken:
            raise DataSourceError("模拟快照故障")
        out = []
        for code in symbols:
            if code == OTHER:
                out.append(Quote(code=code, name="平安银行", price=11.0, prev_close=11.0,
                                 change_pct=0.0, volume_ratio=1.2))
            else:
                out.append(Quote(code=code, name="贵州茅台", price=110.0, prev_close=100.0,
                                 change_pct=10.0, amount_yi=5.0, volume_ratio=2.5))
        return out

    def resolve_name(self, symbol):
        return {"600519.SH": "贵州茅台", "000001.SZ": "平安银行"}.get(symbol)

    def health(self):
        return {"ok": True, "detail": "替身", "latency_ms": 0, "source": self.name}


class Level2ToolsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-l2tools-")
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        self.provider = StubProvider()
        self.service = Level2Service(provider=self.provider, settings=self.settings,
                                    ttl_orderbook=0, ttl_ticks=0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 大单 / 分时

    def test_big_orders_contract_and_order(self):
        payload = self.service.big_orders(CODE, threshold=1_000_000, limit=5)
        self.assertEqual(payload["code"], CODE)
        self.assertEqual(payload["count"], 2, "两笔 ≥ 100 万")
        self.assertEqual([row["time"] for row in payload["items"]], ["09:31:02", "09:30:05"])
        self.assertAlmostEqual(payload["summary"]["net_amount"], 4_005_000.0, places=2)
        self.assertEqual(payload["summary"]["biggest"]["time"], "09:31:02")
        self.assertEqual(payload["tick_sample"], 4)
        for key in ("meta", "capabilities", "note"):
            self.assertIn(key, payload)
        self.assertIn("第三方盘口标记", payload["note"])
        # sides 过滤：只看主动买
        buys = self.service.big_orders(CODE, threshold=1_000_000, sides=["buy"])
        self.assertEqual(buys["count"], 2)
        sells = self.service.big_orders(CODE, threshold=1_000_000, sides=["sell"])
        self.assertEqual(sells["count"], 0)
        self.assertEqual(sells["shown"], 0)

    def test_big_orders_sides_accepts_bare_string(self):
        """``sides="buy"``（裸字符串）必须等同 ``sides=["buy"]``，不能静默返回 0 行。"""
        bare = self.service.big_orders(CODE, threshold=1_000_000, sides="buy")
        listed = self.service.big_orders(CODE, threshold=1_000_000, sides=["buy"])
        self.assertEqual(bare["count"], listed["count"])
        self.assertEqual(bare["shown"], listed["shown"])
        self.assertEqual(bare["count"], 2, "两笔 ≥ 100 万且方向为买")
        # 大小写 / 空白 / 空串都要归一化
        loose = self.service.big_orders(CODE, threshold=1_000_000, sides=" BUY ")
        self.assertEqual(loose["count"], 2)
        self.assertEqual(self.service.big_orders(CODE, threshold=1_000_000, sides="")["count"], 2)


    def test_flow_series_contract(self):
        payload = self.service.flow_series(CODE, limit=100)
        self.assertEqual(payload["code"], CODE)
        self.assertEqual(payload["minutes"], 2)
        self.assertEqual([row["time"] for row in payload["series"]], ["09:30", "09:31"])
        self.assertAlmostEqual(payload["series"][-1]["cum_net"], 3_704_700.0, places=2)
        self.assertAlmostEqual(payload["main_net"], 3_704_700.0, places=2)   # 超大单 +4,005,000 与大单 −300,300
        self.assertEqual(sorted(payload["buckets"].keys()), ["big", "mid", "small", "super_big"])

    # ------------------------------------------------------------------ 封板 / 扫描 / 排行

    def test_seal_status_contract(self):
        payload = self.service.seal_status(CODE)
        seal = payload["seal"]
        self.assertEqual(seal["state"], "limit_up")
        self.assertAlmostEqual(seal["limit_up_price"], 110.0, places=2)
        self.assertEqual(seal["seal_volume"], 20_000)
        self.assertAlmostEqual(seal["seal_amount"], 220_000_000.0, places=2)
        # 封成比 = 2.2 亿 / 快照成交额 5 亿 = 44%
        self.assertAlmostEqual(seal["seal_ratio"], 44.0, places=2)
        self.assertIn("涨停", payload["seal_text"])
        self.assertIn("开板次数", payload["note"])

    def test_scan_sorted_by_imbalance(self):
        payload = self.service.scan([CODE, OTHER])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["requested"], 2)
        rows = {row["code"]: row for row in payload["items"]}
        self.assertGreater(rows[CODE]["imbalance_pct"], rows[OTHER]["imbalance_pct"])
        self.assertEqual(payload["items"][0]["code"], CODE, "按委比降序")
        self.assertEqual(rows[CODE]["seal_state"], "limit_up")
        self.assertEqual(payload["failures"], [])

    def test_flow_rank_sorted_by_main_net(self):
        payload = self.service.flow_rank([OTHER, CODE], limit=100)
        self.assertEqual([row["code"] for row in payload["items"]], [CODE, OTHER], "主力净额降序")
        self.assertAlmostEqual(payload["items"][0]["main_net"], 3_704_700.0, places=2)
        self.assertGreater(payload["items"][0]["main_net_pct"], 0)
        self.assertEqual(payload["failures"], [])

    # ------------------------------------------------------------------ 边界与容错

    def test_empty_codes_reports_business_error(self):
        for method, args in ((self.service.scan, ([],)), (self.service.flow_rank, ([],))):
            with self.assertRaises(DataSourceError) as err:
                method(*args)
            self.assertIn("至少一个标的", str(err.exception))

    def test_invalid_codes_are_skipped_not_fatal(self):
        payload = self.service.scan(["not-a-code", CODE])
        self.assertEqual(payload["count"], 1, "非法代码跳过，合法代码照常返回")
        self.assertEqual(payload["requested"], 1)
        self.assertIn("not-a-code", "".join(item.get("code", "") for item in payload["failures"]))

    def test_provider_failure_maps_to_business_error(self):
        broken = Level2Service(provider=StubProvider(broken=True), settings=self.settings,
                               ttl_orderbook=0, ttl_ticks=0)
        for call in (lambda: broken.big_orders(CODE), lambda: broken.flow_series(CODE),
                     lambda: broken.seal_status(CODE)):
            with self.assertRaises(DataSourceError):
                call()
        # 批量工具逐标的容错：全部失败时 items 为空、failures 记录原因
        payload = broken.scan([CODE])
        self.assertEqual(payload["items"], [])
        self.assertEqual(len(payload["failures"]), 1)
        self.assertIn("故障", payload["failures"][0]["error"])

    def test_limits_are_clamped(self):
        payload = self.service.big_orders(CODE, threshold=1, limit=10_000)
        self.assertLessEqual(payload["shown"], 200)
        series = self.service.flow_series(CODE, limit=10_000_000)
        self.assertTrue(series["series"])
        scan = self.service.scan([CODE] * 20)
        self.assertEqual(scan["requested"], 1, "重复代码去重 + 上限 10 只")


@unittest.skipIf(create_app is None, "缺少 Flask（quantstudio.api 不可用）")
class Level2ToolsHttpTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qs-l2http-")
        self.settings = Settings(data_dir=self.tmp, cache_dir=os.path.join(self.tmp, "cache"))
        self.services = Services(settings=self.settings, provider=StubProvider())
        self.client = create_app(settings=self.settings, services=self.services).test_client()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_endpoints_return_contract(self):
        cases = (
            ("/api/level2/%s/big-orders?threshold=2000000&limit=3" % CODE,
             ("code", "count", "shown", "items", "summary", "threshold")),
            ("/api/level2/%s/flow-series?limit=100" % CODE,
             ("code", "minutes", "series", "buckets", "main_net")),
            ("/api/level2/%s/seal" % CODE, ("code", "price", "prev_close", "seal", "seal_text")),
            ("/api/level2/scan?codes=%s,%s" % (CODE, OTHER), ("count", "requested", "items", "failures")),
            ("/api/level2/flow-rank?codes=%s,%s&limit=100" % (CODE, OTHER),
             ("count", "items", "failures")),
        )
        for path, keys in cases:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_json()
            self.assertIn("data", body, path)
            for key in keys:
                self.assertIn(key, body["data"], "%s 缺少 %s" % (path, key))
            self.assertIn("meta", body["data"])
            self.assertIn("capabilities", body["data"])

    def test_invalid_parameters_return_400(self):
        for path in ("/api/level2/%s/big-orders?threshold=abc" % CODE,
                     "/api/level2/scan?codes=abc",           # 非法代码 → 400（服务层校验）
                     "/api/level2/%s/big-orders?limit=abc" % CODE):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 400, path)
            self.assertIn("error", response.get_json(), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
