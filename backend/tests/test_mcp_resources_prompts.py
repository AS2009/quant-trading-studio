# -*- coding: utf-8 -*-
"""MCP 资源（文档）与提示词（模板）测试：单元 + 走完整服务器的 JSON-RPC 集成。

不依赖任何 ``tools_*.py``：服务器只挂一个空的 ``Registry()``，所以这一组测试
可以独立验证 resources/prompts 的实现；不访问网络，数据目录用默认配置（只读上下文）。
"""

import io
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.prompts import BuiltinPrompts  # noqa: E402
from quantstudio.mcp.registry import Registry  # noqa: E402
from quantstudio.mcp.resources import DOCS_URI_PREFIX, DocsResources  # noqa: E402
from quantstudio.mcp.server import Server  # noqa: E402

#: 必须是白名单之外 / 不存在 / 非文档 scheme 的 uri（含路径穿越尝试）
INVALID_URIS = (
    "quantstudio://docs/../../etc/passwd",
    "file:///etc/passwd",
    "quantstudio://docs/nope.md",
    "quantstudio://docs/strategy-spec.md/../../README.md",
    "quantstudio://strategies/dual_ma",
)


def _request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _result(reply):
    return (reply or {}).get("result") or {}


class DocsResourcesTest(unittest.TestCase):
    """文档资源的白名单、读取与模板声明。"""

    @classmethod
    def setUpClass(cls):
        cls.context = ToolContext(read_only=True)
        cls.resources = DocsResources(cls.context)

    def test_list_shape(self):
        items = self.resources.list()
        self.assertGreaterEqual(len(items), 6, "至少应暴露 6 个文档资源")
        for item in items:
            self.assertIsInstance(item.get("uri"), str)
            self.assertTrue(item["uri"].startswith(DOCS_URI_PREFIX))
            self.assertTrue(item.get("name"), item)
            self.assertTrue(item.get("title"), item)
            self.assertTrue(item.get("description"), item)
            self.assertTrue(item.get("mimeType"), item)
        # 规范要求的核心文档必须在列表里
        uris = [item["uri"] for item in items]
        self.assertIn(DOCS_URI_PREFIX + "strategy-spec.md", uris)
        self.assertIn(DOCS_URI_PREFIX + "strategy-examples.md", uris)

    def test_read_rejects_non_whitelisted_uris(self):
        for uri in INVALID_URIS:
            self.assertIsNone(self.resources.read(uri), "不该能读：%s" % uri)
        self.assertIsNone(self.resources.read(None))
        self.assertIsNone(self.resources.read(""))

    def test_read_every_listed_resource(self):
        """服务器声明的东西必须真能读到：逐个 read()，断言非空文本与 mimeType。"""
        items = self.resources.list()
        self.assertTrue(items)
        for item in items:
            payload = self.resources.read(item["uri"])
            self.assertIsNotNone(payload, "资源读不到：%s" % item["uri"])
            contents = payload.get("contents") or []
            self.assertEqual(len(contents), 1, item["uri"])
            entry = contents[0]
            self.assertEqual(entry.get("uri"), item["uri"])
            text = entry.get("text")
            self.assertIsInstance(text, str, item["uri"])
            self.assertTrue(text.strip(), "资源正文为空：%s" % item["uri"])
            expected_mime = "application/json" if item["uri"].endswith(".json") else "text/markdown"
            self.assertEqual(entry.get("mimeType"), expected_mime, item["uri"])

    def test_templates_declares_strategy_source(self):
        templates = self.resources.templates()
        self.assertTrue(templates)
        template = templates[0]
        self.assertEqual(template.get("uriTemplate"), "quantstudio://strategies/{strategy_id}")
        self.assertTrue(template.get("name"))
        self.assertIn("策略", template.get("description", ""))
        # 模板 uri 不是白名单资源，read() 必须返回 None（读取走 strategy_read_source 工具）
        self.assertIsNone(self.resources.read("quantstudio://strategies/dual_ma"))


class BuiltinPromptsTest(unittest.TestCase):
    """提示词清单与缺失参数时的降级行为。"""

    @classmethod
    def setUpClass(cls):
        cls.prompts = BuiltinPrompts(ToolContext(read_only=True))

    def test_list_shape(self):
        items = self.prompts.list()
        self.assertEqual(len(items), 3)
        names = [item["name"] for item in items]
        self.assertEqual(names, ["write_strategy", "review_backtest", "daily_watch"])
        for item in items:
            self.assertTrue(item.get("title"), item)
            self.assertTrue(item.get("description"), item)
            arguments = item.get("arguments")
            self.assertIsInstance(arguments, list, item["name"])
            for arg in arguments:
                self.assertTrue(arg.get("name"), item["name"])
                self.assertTrue(arg.get("description"), item["name"])
                self.assertIsInstance(arg.get("required"), bool, item["name"])
        # 必需参数声明要准确
        by_name = {item["name"]: item for item in items}
        write_args = {arg["name"]: arg for arg in by_name["write_strategy"]["arguments"]}
        self.assertTrue(write_args["idea"]["required"])
        self.assertFalse(write_args["universe"]["required"])
        review_args = {arg["name"]: arg for arg in by_name["review_backtest"]["arguments"]}
        self.assertTrue(review_args["strategy_id"]["required"])
        self.assertFalse(review_args["symbol"]["required"])
        watch_args = {arg["name"]: arg for arg in by_name["daily_watch"]["arguments"]}
        self.assertFalse(watch_args["symbols"]["required"])

    def test_get_write_strategy_uses_arguments(self):
        payload = self.prompts.get("write_strategy", {"idea": "双均线", "universe": "沪深300"})
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("description"))
        messages = payload.get("messages") or []
        self.assertTrue(messages)
        text = messages[0]["content"]["text"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"]["type"], "text")
        self.assertIn("双均线", text)
        self.assertIn("沪深300", text)
        # 工作流里的关键工具与资源 uri 必须出现
        for token in ("strategy_read_source", "strategy_write_source", "strategy_lint",
                      "backtest_run", "quantstudio://docs/strategy-spec.md",
                      "quantstudio://docs/strategy-examples.md"):
            self.assertIn(token, text)
        # 提示词里引用的资源 uri 必须在资源白名单里（避免提示词指向不存在的文档）
        listed = {item["uri"] for item in DocsResources(self.prompts.context).list()}
        for uri in ("quantstudio://docs/strategy-spec.md", "quantstudio://docs/strategy-examples.md"):
            self.assertIn(uri, listed)

    def test_get_with_missing_or_odd_arguments(self):
        for name in ("write_strategy", "review_backtest", "daily_watch"):
            payload = self.prompts.get(name, {})
            messages = payload.get("messages") or []
            self.assertTrue(messages, name)
            self.assertIn("role", messages[0])
            self.assertTrue(messages[0]["content"]["text"].strip(), name)
        # 无关键必须被忽略；非字符串参数也要能渲染
        payload = self.prompts.get("review_backtest", {"strategy_id": "dual_ma",
                                                       "symbol": "600519.SH",
                                                       "unused.pdf": "x"})
        text = payload["messages"][0]["content"]["text"]
        self.assertIn("dual_ma", text)
        self.assertIn("600519.SH", text)
        self.assertNotIn("unused.pdf", text)
        payload = self.prompts.get("daily_watch", {"symbols": ["600519.SH", "300750.SZ"]})
        self.assertIn("600519.SH", payload["messages"][0]["content"]["text"])

    def test_get_unknown_prompt_returns_none(self):
        self.assertIsNone(self.prompts.get("nope", {}))
        self.assertIsNone(self.prompts.get("write_strategy".upper(), {"idea": "x"}))


class ServerIntegrationTest(unittest.TestCase):
    """走完整服务器协议路径：resources/* 与 prompts/* 四个方法。"""

    def setUp(self):
        self.context = ToolContext(read_only=True)
        self.resources = DocsResources(self.context)
        self.prompts = BuiltinPrompts(self.context)
        self.server = Server(Registry(), context=self.context, resources=self.resources,
                             prompts=self.prompts, stderr=io.StringIO())

    def test_initialize_declares_capabilities(self):
        reply = self.server.handle_raw(_request(1, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "unittest", "version": "1"},
        }))
        self.assertEqual(reply["jsonrpc"], "2.0")
        self.assertEqual(reply["id"], 1)
        result = _result(reply)
        caps = result.get("capabilities") or {}
        self.assertIn("resources", caps)
        self.assertIn("prompts", caps)
        self.assertTrue(result.get("serverInfo", {}).get("name"))

    def test_resources_list_and_read_over_protocol(self):
        reply = self.server.handle_raw(_request(2, "resources/list", {}))
        self.assertEqual(reply["jsonrpc"], "2.0")
        self.assertEqual(reply["id"], 2)
        items = _result(reply).get("resources") or []
        self.assertGreaterEqual(len(items), 6)
        uri = items[0]["uri"]
        read = self.server.handle_raw(_request(3, "resources/read", {"uri": uri}))
        contents = _result(read).get("contents") or []
        self.assertTrue(contents)
        self.assertEqual(contents[0]["uri"], uri)
        self.assertTrue(contents[0]["text"].strip())
        self.assertEqual(contents[0]["mimeType"], "text/markdown")

    def test_resources_read_unknown_uri_is_invalid_params(self):
        for index, uri in enumerate(INVALID_URIS):
            reply = self.server.handle_raw(_request(10 + index, "resources/read", {"uri": uri}))
            self.assertNotIn("result", reply, uri)
            self.assertEqual(reply["error"]["code"], -32602, uri)
        # uri 缺失/类型不对同样是 -32602
        bad = self.server.handle_raw(_request(20, "resources/read", {}))
        self.assertEqual(bad["error"]["code"], -32602)

    def test_resources_templates_list(self):
        reply = self.server.handle_raw(_request(21, "resources/templates/list"))
        templates = _result(reply).get("resourceTemplates") or []
        self.assertEqual(templates[0]["uriTemplate"], "quantstudio://strategies/{strategy_id}")

    def test_prompts_list_and_get_over_protocol(self):
        reply = self.server.handle_raw(_request(30, "prompts/list", {}))
        items = _result(reply).get("prompts") or []
        self.assertEqual(len(items), 3)
        names = [item["name"] for item in items]
        self.assertIn("write_strategy", names)
        got = self.server.handle_raw(_request(31, "prompts/get", {
            "name": "write_strategy", "arguments": {"idea": "双均线"},
        }))
        payload = _result(got)
        self.assertTrue(payload.get("messages"))
        self.assertIn("双均线", payload["messages"][0]["content"]["text"])
        unknown = self.server.handle_raw(_request(32, "prompts/get", {"name": "nope", "arguments": {}}))
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_all_declared_resources_are_readable_over_protocol(self):
        """资源全部可读性检查：声明了却读不到 = 服务器在骗模型。"""
        listing = self.server.handle_raw(_request(40, "resources/list", {}))
        for item in _result(listing).get("resources") or []:
            reply = self.server.handle_raw(_request(41, "resources/read", {"uri": item["uri"]}))
            self.assertNotIn("error", reply, item["uri"])
            contents = _result(reply).get("contents") or []
            self.assertTrue(contents and contents[0]["text"].strip(), item["uri"])

    def test_daily_watch_requires_sources_and_disclaimer(self):
        text = self.prompts.get("daily_watch", {})["messages"][0]["content"]["text"]
        self.assertIn("market_overview", text)
        self.assertIn("market_quotes", text)
        self.assertIn("market_kline", text)
        self.assertIn("60", text)
        self.assertIn("不构成投资建议", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
