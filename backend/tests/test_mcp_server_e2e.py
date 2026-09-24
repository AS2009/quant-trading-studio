# -*- coding: utf-8 -*-
"""MCP 端到端测试：像真实客户端那样**用子进程 + 管道**跟服务器对话。

单元测试（``test_mcp_protocol.py``）在进程内验证分发逻辑；这里验证「换了个进程、
换了 stdin/stdout、客户端按行读写」时还能不能跑通——也就是 Claude Desktop / Claude Code
实际使用的那条路径：启动脚本 → 写入请求 → 逐行读响应 → 关闭 stdin → 进程 0 退出。

不联网：数据目录指向临时目录，并置 ``QUANTSTUDIO_OFFLINE=1``。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

SERVER_SCRIPT = os.path.join(REPO_ROOT, "scripts", "mcp_server.py")


class StdioClient:
    """最小 MCP 客户端：一行一条 JSON-RPC，读响应按 id 匹配。"""

    def __init__(self, extra_args=()):
        env = dict(os.environ)
        env["QUANTSTUDIO_DATA_DIR"] = tempfile.mkdtemp(prefix="qs-mcp-e2e-")
        env["QUANTSTUDIO_OFFLINE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, SERVER_SCRIPT, "--actor", "e2e"] + list(extra_args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=REPO_ROOT, env=env, text=True, encoding="utf-8", bufsize=1)
        self._next_id = 0
        self.stderr_text = ""

    # -- 协议交互
    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("服务器提前退出（未等到 id=%s 的响应）\nstderr:\n%s"
                                     % (request_id, self._stderr_so_far()))
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)                  # 每一行都必须是合法 JSON
            if payload.get("id") == request_id:
                return payload

    def read_raw_line(self):
        """直接读一行原始输出（用于测试非法 JSON 的错误响应）。"""
        return self.proc.stdout.readline()

    # -- 收尾
    def _stderr_so_far(self):
        return self.stderr_text

    def close(self):
        """关掉 stdin 让服务器看到 EOF，然后收尾并返回退出码。

        注意：管道**不可 seek**（``seek`` 会抛 ``io.UnsupportedOperation``），所以只能在
        进程结束之后读 stderr；此时进程已退出，读取不会阻塞。
        """
        try:
            self.proc.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        try:
            self.stderr_text = self.proc.stderr.read() or ""
        except Exception:                               # noqa: BLE001
            self.stderr_text = ""
        return self.proc.returncode


class StdioEndToEndTests(unittest.TestCase):
    """真实子进程 + 管道：握手 → 工具清单 → 调用 → 正常退出。"""

    def test_full_client_handshake_and_tool_call(self):
        client = StdioClient()
        try:
            init = client.request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1.0"}})
            result = init["result"]
            self.assertEqual(result["protocolVersion"], "2025-06-18")
            self.assertIn("tools", result["capabilities"])
            self.assertTrue(result["serverInfo"]["name"])

            client.notify("notifications/initialized")

            listing = client.request("tools/list")["result"]["tools"]
            names = [item["name"] for item in listing]
            self.assertGreaterEqual(len(names), 20,
                                    "工具数量应覆盖行情/策略/回测/持仓/模拟盘：%s" % names)
            for expected in ("market_overview", "strategy_list", "strategy_write_source",
                             "backtest_run", "portfolio_overview", "paper_submit_order"):
                self.assertIn(expected, names)
            for item in listing:
                self.assertIn("inputSchema", item)
                self.assertIn("annotations", item)

            call = client.request("tools/call", {"name": "strategy_list", "arguments": {}})
            payload = call["result"]
            self.assertFalse(payload["isError"], payload["content"][0]["text"][:300])
            self.assertTrue(payload["content"][0]["text"].strip())
            self.assertIsInstance(payload["structuredContent"], dict)

            pong = client.request("ping")
            self.assertEqual(pong["result"], {})
        finally:
            code = client.close()
        self.assertEqual(code, 0, "客户端关闭 stdin 后服务器应以 0 退出")
        self.assertIn("[mcp]", client.stderr_text, "启动日志应该在 stderr 上")

    def test_read_only_mode_hides_write_tools(self):
        client = StdioClient(extra_args=("--read-only",))
        try:
            client.request("initialize", {"protocolVersion": "2025-06-18"})
            tools = client.request("tools/list")["result"]["tools"]
            write_like = [item["name"] for item in tools
                          if not item["annotations"].get("readOnlyHint")]
            self.assertEqual(write_like, [], "只读模式不允许任何写工具出现在清单里")
            blocked = client.request("tools/call", {
                "name": "strategy_write_source",
                "arguments": {"strategy_id": "st_x", "source": ""}})
            self.assertTrue(blocked["result"]["isError"])
        finally:
            code = client.close()
        self.assertEqual(code, 0)

    def test_server_survives_garbage_input(self):
        client = StdioClient()
        try:
            client.request("initialize", {"protocolVersion": "2025-06-18"})
            client.proc.stdin.write("这不是 JSON\n")
            client.proc.stdin.flush()
            bad = json.loads(client.read_raw_line().strip())
            self.assertEqual(bad["error"]["code"], -32700)
            pong = client.request("ping")                       # 之后仍能正常工作
            self.assertEqual(pong["result"], {})
        finally:
            code = client.close()
        self.assertEqual(code, 0)


if __name__ == "__main__":       # pragma: no cover
    unittest.main(verbosity=2)
