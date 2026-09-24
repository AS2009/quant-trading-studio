# -*- coding: utf-8 -*-
"""MCP 协议层与服务器分发的回归测试（不依赖任何工具分组，也不需要网络）。"""

import io
import json
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 测试隔离：写保护会往「数据目录」写审计日志与备份，绝不能落到用户真实的 backend/data
import tempfile  # noqa: E402

os.environ.setdefault("QUANTSTUDIO_DATA_DIR", tempfile.mkdtemp(prefix="qs-mcp-protocol-"))


from quantstudio.mcp import protocol  # noqa: E402
from quantstudio.mcp.context import ToolContext  # noqa: E402
from quantstudio.mcp.registry import Registry, ToolError, obj, p_str  # noqa: E402
from quantstudio.mcp.server import Server  # noqa: E402


# --------------------------------------------------------------------------- 测试替身
class FakeResources:
    def list(self):
        return [{"uri": "quantstudio://docs/demo.md", "name": "demo", "title": "示例",
                 "description": "测试资源", "mimeType": "text/markdown"}]

    def read(self, uri):
        if uri == "quantstudio://docs/demo.md":
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": "# 示例\n正文"}]}
        return None


class FakePrompts:
    def list(self):
        return [{"name": "demo_prompt", "title": "示例提示词", "description": "测试用",
                 "arguments": [{"name": "topic", "description": "主题", "required": True}]}]

    def get(self, name, arguments):
        if name != "demo_prompt":
            return None
        topic = (arguments or {}).get("topic") or "(未指定)"
        return {"description": "示例", "messages": [
            {"role": "user", "content": {"type": "text", "text": "请处理：%s" % topic}}]}


def build_test_server(read_only=False, resources=True, prompts=True):
    """一个最小服务器：两个工具（一读一写），用于验证分发与开关。"""
    registry = Registry()

    @registry.tool("demo_read", "只读工具", obj({"text": p_str("文本")}),
                   group="demo", read_only=True)
    def demo_read(ctx, args):
        return "读到了：%s" % args.get("text", ""), {"echo": args.get("text", "")}

    @registry.tool("demo_write", "写工具", obj({"text": p_str("文本")}),
                   group="demo", read_only=False, idempotent=False)
    def demo_write(ctx, args):
        ctx.write_guard("demo_write", args)
        return "写好了：%s" % args.get("text", ""), {"written": args.get("text", "")}

    @registry.tool("demo_boom", "故意抛异常", obj(), group="demo")
    def demo_boom(ctx, args):
        raise RuntimeError("模拟内部错误")

    @registry.tool("demo_tool_error", "故意抛 ToolError", obj(), group="demo")
    def demo_tool_error(ctx, args):
        raise ToolError("参数不对", hint="请传 text", code="DEMO")

    context = ToolContext(read_only=read_only)
    return Server(registry, context=context,
                  resources=FakeResources() if resources else None,
                  prompts=FakePrompts() if prompts else None,
                  stderr=io.StringIO())


# --------------------------------------------------------------------------- 协议层
class ProtocolTests(unittest.TestCase):

    def test_encode_is_single_line_and_keeps_chinese(self):
        line = protocol.encode({"jsonrpc": "2.0", "result": {"名称": "贵州茅台"}})
        self.assertNotIn("\n", line)
        self.assertIn("贵州茅台", line, "中文不应被转义成 \\uXXXX")
        self.assertEqual(json.loads(line)["result"]["名称"], "贵州茅台")

    def test_read_message_skips_blank_lines_and_handles_crlf(self):
        stream = io.StringIO('\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\r\n')
        message = protocol.read_message(stream)
        self.assertEqual(message["method"], "ping")
        self.assertIsNone(protocol.read_message(stream), "EOF 应返回 None")

    def test_write_message_appends_exactly_one_newline(self):
        buffer = io.StringIO()
        protocol.write_message(buffer, {"jsonrpc": "2.0", "id": 1, "result": {}})
        text = buffer.getvalue()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)

    def test_decode_error_raises_parse_error(self):
        with self.assertRaises(protocol.ProtocolError) as ctx:
            protocol.decode("{not json")
        self.assertEqual(ctx.exception.code, protocol.ErrorCode.PARSE_ERROR)

    def test_validate_request_rejects_bad_shapes(self):
        cases = [
            ("not a dict", protocol.ErrorCode.INVALID_REQUEST),
            ({"id": 1, "method": "ping"}, protocol.ErrorCode.INVALID_REQUEST),            # 缺 jsonrpc
            ({"jsonrpc": "2.0", "id": 1}, protocol.ErrorCode.INVALID_REQUEST),            # 缺 method
            ({"jsonrpc": "2.0", "id": {}, "method": "ping"}, protocol.ErrorCode.INVALID_REQUEST),
            ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": 3},
             protocol.ErrorCode.INVALID_PARAMS),
        ]
        for message, code in cases:
            with self.assertRaises(protocol.ProtocolError) as ctx:
                protocol.validate_request(message)
            self.assertEqual(ctx.exception.code, code, "message=%r" % (message,))

    def test_negotiate_version(self):
        self.assertEqual(protocol.negotiate_version("2025-03-26"), "2025-03-26")
        self.assertEqual(protocol.negotiate_version("2025-06-18"), "2025-06-18")
        self.assertEqual(protocol.negotiate_version("1999-01-01"), protocol.DEFAULT_PROTOCOL_VERSION)
        self.assertEqual(protocol.negotiate_version(None), protocol.DEFAULT_PROTOCOL_VERSION)

    def test_validate_args_rejects_unknown_property(self):
        """拼错的参数名必须报错（否则模型以为生效了，其实被静默忽略）。"""
        from quantstudio.mcp.registry import ToolSpec, validate_args

        spec = ToolSpec(name="t", description="d", input_schema=obj({"text": p_str("文本")}),
                        handler=lambda ctx, args: "")
        validate_args(spec, {"text": "ok"})                       # 正常参数不报错
        validate_args(spec, {})                                   # 非必需参数可以省
        with self.assertRaises(ToolError) as ctx:
            validate_args(spec, {"text": "ok", "tex": "typo"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGS")
        self.assertIn("tex", ctx.exception.message)
        self.assertIn("text", ctx.exception.hint, "提示里要给出可用参数名，便于模型自我纠正")


# --------------------------------------------------------------------------- 服务器分发
class ServerTests(unittest.TestCase):

    def test_initialize_and_capabilities(self):
        server = build_test_server()
        reply = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2024-11-05",
                                              "clientInfo": {"name": "t", "version": "0"}}})
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("resources", result["capabilities"])
        self.assertIn("prompts", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], protocol.SERVER_NAME)
        self.assertTrue(result["instructions"])

    def test_capabilities_omit_disabled_features(self):
        server = build_test_server(resources=False, prompts=False)
        caps = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})["result"]["capabilities"]
        self.assertNotIn("resources", caps)
        self.assertNotIn("prompts", caps)
        err = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        self.assertEqual(err["error"]["code"], protocol.ErrorCode.METHOD_NOT_FOUND)

    def test_notifications_get_no_response(self):
        server = build_test_server()
        self.assertIsNone(server.handle_raw({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(server.handle_raw({"jsonrpc": "2.0", "method": "notifications/whatever"}))

    def test_ping(self):
        server = build_test_server()
        self.assertEqual(server.handle_raw({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"], {})

    def test_tools_list_includes_annotations(self):
        server = build_test_server()
        tools = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        names = [item["name"] for item in tools]
        self.assertEqual(names, ["demo_boom", "demo_read", "demo_tool_error", "demo_write"])
        for item in tools:
            self.assertEqual(item["inputSchema"]["type"], "object")
            self.assertIn("readOnlyHint", item["annotations"])

    def test_read_only_mode_hides_and_blocks_write_tools(self):
        server = build_test_server(read_only=True)
        names = [item["name"] for item in
                 server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertNotIn("demo_write", names, "只读模式下写工具必须从列表消失")
        blocked = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "demo_write", "arguments": {"text": "x"}}})
        self.assertTrue(blocked["result"]["isError"])
        self.assertIn("只读", blocked["result"]["content"][0]["text"])

    def test_tools_call_success_shapes(self):
        server = build_test_server()
        reply = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "demo_read", "arguments": {"text": "hi"}}})
        result = reply["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"echo": "hi"})
        self.assertEqual(result["content"][0]["type"], "text")

    def test_unknown_tool_and_method_and_bad_json(self):
        server = build_test_server()
        unknown = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(unknown["error"]["code"], protocol.ErrorCode.INVALID_PARAMS)
        self.assertIn("demo_read", unknown["error"]["data"]["available"])
        bad_method = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "no/such"})
        self.assertEqual(bad_method["error"]["code"], protocol.ErrorCode.METHOD_NOT_FOUND)
        bad_json = server.handle_line("{oops")
        self.assertEqual(bad_json["error"]["code"], protocol.ErrorCode.PARSE_ERROR)
        no_args = server.handle_raw({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": 5}})
        self.assertEqual(no_args["error"]["code"], protocol.ErrorCode.INVALID_PARAMS)

    def test_tool_errors_are_results_not_protocol_errors(self):
        server = build_test_server()
        tool_error = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "demo_tool_error", "arguments": {}}})
        self.assertNotIn("error", tool_error)
        self.assertTrue(tool_error["result"]["isError"])
        self.assertIn("建议", tool_error["result"]["content"][0]["text"])
        boom = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "demo_boom", "arguments": {}}})
        self.assertTrue(boom["result"]["isError"], "内部异常也要变成 isError，不能掀翻服务器")
        self.assertIn("内部错误", boom["result"]["content"][0]["text"])

    def test_tool_stdout_is_redirected(self):
        """策略/回测代码是本进程执行的，里面的 print 绝不能污染协议流。"""
        registry = Registry()

        @registry.tool("demo_print", "会打印的工具", obj(), group="demo")
        def demo_print(ctx, args):
            print("工具里的中文输出")            # 模拟策略代码里的 print
            return "done", {"ok": True}

        server = Server(registry, context=ToolContext(), stderr=io.StringIO())
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "demo_print", "arguments": {}}}) + "\n"
        out = io.StringIO()
        self.assertEqual(server.serve(stdin=io.StringIO(request), stdout=out), 0)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "stdout 里只允许出现协议消息：%r" % out.getvalue())
        self.assertFalse(json.loads(lines[0])["result"]["isError"])
        self.assertNotIn("工具里的中文输出", out.getvalue())
        self.assertIn("工具里的中文输出", server._stderr.getvalue(),
                      "被改道的输出要留在 stderr 里，便于排障")
    def test_resources_and_prompts_flow(self):
        server = build_test_server()
        res = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})["result"]
        self.assertEqual(len(res["resources"]), 1)
        read = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                                  "params": {"uri": "quantstudio://docs/demo.md"}})["result"]
        self.assertIn("正文", read["contents"][0]["text"])
        bad = server.handle_raw({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                 "params": {"uri": "quantstudio://docs/nope.md"}})
        self.assertEqual(bad["error"]["code"], protocol.ErrorCode.INVALID_PARAMS)
        prompts = server.handle_raw({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})["result"]
        self.assertEqual(prompts["prompts"][0]["name"], "demo_prompt")
        got = server.handle_raw({"jsonrpc": "2.0", "id": 5, "method": "prompts/get",
                                 "params": {"name": "demo_prompt", "arguments": {"topic": "回测"}}})["result"]
        self.assertIn("回测", got["messages"][0]["content"]["text"])
        missing = server.handle_raw({"jsonrpc": "2.0", "id": 6, "method": "prompts/get",
                                     "params": {"name": "nope"}})
        self.assertEqual(missing["error"]["code"], protocol.ErrorCode.INVALID_PARAMS)

    def test_batch_is_supported(self):
        server = build_test_server()
        replies = server.handle_raw([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(len(replies), 2, "通知不产生响应，批量的其它请求各自有一条")
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[1]["id"], 2)

    # -- 主循环：stdout 纯净性（这是 MCP 服务器最容易犯的致命错误）
    def test_serve_keeps_stdout_protocol_only(self):
        server = build_test_server()
        requests = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "demo_read", "arguments": {"text": "中文"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "demo_boom", "arguments": {}}}),
            "{oops",                                    # 非法 JSON 也要有错误响应且不中断
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
        ]) + "\n"
        out = io.StringIO()
        code = server.serve(stdin=io.StringIO(requests), stdout=out)
        self.assertEqual(code, 0, "EOF 正常结束应返回 0")
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 5, "4 个请求 + 1 个错误响应")
        for line in lines:
            payload = json.loads(line)                  # 每一行都必须是合法 JSON
            self.assertEqual(payload["jsonrpc"], "2.0")
        ids = [json.loads(line).get("id") for line in lines]
        self.assertEqual(ids, [1, 2, 3, None, 4])
        # 中文必须原样出现在协议消息里（UTF-8 直写，不转义）
        self.assertIn("中文", out.getvalue())
        # 日志必须走 stderr，不能混进 stdout
        self.assertNotIn("[mcp]", out.getvalue())
        self.assertIn("[mcp]", server._stderr.getvalue())

    def test_serve_returns_on_eof(self):
        server = build_test_server()
        self.assertEqual(server.serve(stdin=io.StringIO(""), stdout=io.StringIO()), 0)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
