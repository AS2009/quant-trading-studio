# -*- coding: utf-8 -*-
"""MCP 服务器命令行入口：装配 + stdio 主循环 + 自检（供 CI 与打包产物验证）。

用法::

    python scripts/mcp_server.py                 # stdio 服务器（默认可读写）
    python scripts/mcp_server.py --read-only     # 只读模式：写工具从 tools/list 消失
    python scripts/mcp_server.py --selftest      # 自检：握手 → tools/list → 调一个只读工具
    python -m quantstudio.mcp --selftest         # 等价写法

自检报告写到 ``%TEMP%/quantstudio_mcp_selftest.txt``：MCP 客户端的进程没有终端，
CI 与排障都靠这个文件看结果。
"""

import argparse
import json
import os
import sys
import tempfile
import traceback
from typing import Any, Dict, List, Optional

from .context import ToolContext
from .protocol import DEFAULT_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from .server import Server
from .tools import build_registry

REPORT_FILENAME = "quantstudio_mcp_selftest.txt"
#: ``build_registry()`` 里注册的工具总数（--read-only 只隐藏写工具；新增工具组时同步更新）
EXPECTED_TOOL_COUNT = 41


# --------------------------------------------------------------------------- 装配
def build_context(*, read_only: bool = False, settings: Any = None, actor: str = "mcp",
                  provider: Any = None) -> ToolContext:
    return ToolContext(settings=settings, read_only=read_only, actor=actor, provider=provider)


def build_server(*, read_only: bool = False, settings: Any = None, actor: str = "mcp",
                 with_resources: bool = True, with_prompts: bool = True, provider: Any = None,
                 stderr: Any = None) -> Server:
    """装配一个可用的 MCP 服务器（工具 + 资源 + 提示词）。"""
    context = build_context(read_only=read_only, settings=settings, actor=actor, provider=provider)
    resources = None
    prompts = None
    if with_resources:
        from .resources import DocsResources

        resources = DocsResources(context)
    if with_prompts:
        from .prompts import BuiltinPrompts

        prompts = BuiltinPrompts(context)
    return Server(build_registry(), context=context, resources=resources, prompts=prompts,
                  stderr=stderr)


# --------------------------------------------------------------------------- 自检
def report_path() -> str:
    return os.path.join(tempfile.gettempdir(), REPORT_FILENAME)


class _Report:
    """同时写终端与报告文件（打包成 GUI 产物时终端可能不存在）。"""

    def __init__(self, path: str):
        self.path = path
        self.lines: List[str] = []
        try:
            self._fh = open(path, "w", encoding="utf-8")
        except OSError:
            self._fh = None

    def line(self, text: str = "") -> None:
        self.lines.append(text)
        try:
            print(text)
        except Exception:                       # noqa: BLE001 - 无 stdout 时只写文件
            pass
        if self._fh is not None:
            try:
                self._fh.write(text + "\n")
                self._fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass


def selftest(read_only: bool = False) -> int:
    report = _Report(report_path())
    failures: List[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        report.line("  %s %s%s" % ("✓" if condition else "✗", label,
                                   ("  —— %s" % detail) if detail and not condition else ""))
        if not condition:
            failures.append(label)

    report.line("=" * 72)
    report.line("QuantTrading Studio · MCP 服务器自检")
    report.line("=" * 72)
    report.line("Python      : %s" % sys.version.split()[0])
    report.line("协议版本    : %s（支持 %s）" % (DEFAULT_PROTOCOL_VERSION, ", ".join(SUPPORTED_PROTOCOL_VERSIONS)))
    report.line("模式        : %s" % ("只读（--read-only）" if read_only else "可读写"))
    report.line("")

    try:
        server = build_server(read_only=read_only, actor="selftest", stderr=sys.stderr)
    except Exception as exc:                    # noqa: BLE001
        report.line("装配失败：%s" % exc)
        report.line(traceback.format_exc())
        report.close()
        return 1

    # 1) 握手（刻意用一个较旧的协议版本，验证版本回显）
    report.line("1) 握手 initialize")
    reply = server.handle_raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26",
                                          "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1"}}})
    result = (reply or {}).get("result") or {}
    check(reply is not None and "result" in reply, "initialize 有响应")
    check(result.get("protocolVersion") == "2025-03-26", "协议版本回显客户端值",
          str(result.get("protocolVersion")))
    check(bool((result.get("capabilities") or {}).get("tools")), "声明 tools 能力")
    check(bool((result.get("serverInfo") or {}).get("name")), "返回 serverInfo.name")
    check(bool(result.get("instructions")), "返回 instructions（给模型的用法说明）")

    report.line("")
    report.line("2) 通知与 ping")
    check(server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None,
          "notifications/initialized 不需要响应")
    pong = server.handle_raw({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    check((pong or {}).get("result") == {}, "ping 返回空对象")

    report.line("")
    report.line("3) 工具清单 tools/list")
    listing = server.handle_raw({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}) or {}
    tools = ((listing.get("result") or {}).get("tools")) or []
    report.line("  工具数      : %d" % len(tools))
    groups: Dict[str, int] = {}
    for item in tools:
        groups[item["name"].split("_")[0]] = groups.get(item["name"].split("_")[0], 0) + 1
    report.line("  分组        : %s" % ", ".join("%s×%d" % (k, v) for k, v in sorted(groups.items())))
    if read_only:                               # 只读模式会隐藏写工具，按注册表推算可见数
        expected_tools = len(server.registry) - len(
            [spec for spec in server.registry.all() if not spec.read_only])
    else:
        expected_tools = EXPECTED_TOOL_COUNT
    check(len(tools) == expected_tools, "工具数量为 %d" % expected_tools, "当前 %d 个" % len(tools))
    check(all(item.get("name") and item.get("description") and item.get("inputSchema") for item in tools),
          "每个工具都有 name/description/inputSchema")
    check(all("annotations" in item for item in tools), "每个工具都带 annotations（只读/破坏性提示）")
    if read_only:
        bad = [item["name"] for item in tools if not (item.get("annotations") or {}).get("readOnlyHint")]
        check(not bad, "只读模式下不应出现写工具", ", ".join(bad[:5]))

    report.line("")
    report.line("4) 调用只读工具 tools/call")
    call = server.handle_raw({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "strategy_list", "arguments": {}}}) or {}
    call_result = call.get("result") or {}
    text = ((call_result.get("content") or [{}])[0]).get("text", "")
    check(call_result.get("isError") is False, "strategy_list 调用成功",
          str(call.get("error") or call_result)[:200])
    check(bool(text.strip()), "返回了文本摘要")
    check(isinstance(call_result.get("structuredContent"), dict), "返回了 structuredContent")
    report.line("  摘要首行    : %s" % (text.strip().splitlines()[0] if text.strip() else "(空)"))

    report.line("")
    report.line("5) 错误路径")
    unknown = server.handle_raw({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": "no_such_tool", "arguments": {}}}) or {}
    check((unknown.get("error") or {}).get("code") == -32602, "未知工具返回 -32602")
    bad_method = server.handle_raw({"jsonrpc": "2.0", "id": 6, "method": "no/such"}) or {}
    check((bad_method.get("error") or {}).get("code") == -32601, "未知方法返回 -32601")
    bad_json = server.handle_line("{not json")
    check((bad_json or {}).get("error", {}).get("code") == -32700, "非法 JSON 返回 -32700")
    bad_args = server.handle_raw({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                  "params": {"name": "market_kline", "arguments": {"days": "很多"}}}) or {}
    check(((bad_args.get("result") or {}).get("isError") is True), "参数类型错误返回 isError（不崩）")

    report.line("")
    report.line("6) 资源与提示词")
    res_list = server.handle_raw({"jsonrpc": "2.0", "id": 8, "method": "resources/list"}) or {}
    resources = ((res_list.get("result") or {}).get("resources")) or []
    report.line("  资源数      : %d" % len(resources))
    check(len(resources) >= 1, "至少提供一个资源")
    if resources:
        first = resources[0].get("uri")
        read = server.handle_raw({"jsonrpc": "2.0", "id": 9, "method": "resources/read",
                                  "params": {"uri": first}}) or {}
        contents = ((read.get("result") or {}).get("contents")) or []
        check(bool(contents and contents[0].get("text")), "resources/read 返回正文（%s）" % first)
    prompt_list = server.handle_raw({"jsonrpc": "2.0", "id": 10, "method": "prompts/list"}) or {}
    prompts = ((prompt_list.get("result") or {}).get("prompts")) or []
    report.line("  提示词数    : %d" % len(prompts))
    check(len(prompts) >= 1, "至少提供一个提示词")
    if prompts:
        got = server.handle_raw({"jsonrpc": "2.0", "id": 11, "method": "prompts/get",
                                 "params": {"name": prompts[0]["name"], "arguments": {}}}) or {}
        messages = ((got.get("result") or {}).get("messages")) or []
        check(bool(messages), "prompts/get 返回消息（%s）" % prompts[0]["name"])

    report.line("")
    report.line("=" * 72)
    if failures:
        report.line("MCP 自检未通过（%d 项失败）：" % len(failures))
        for item in failures:
            report.line("  - %s" % item)
        report.line("[报告文件] %s" % report.path)
        report.close()
        return 1
    report.line("MCP 自检通过 ✅（工具 %d 个，资源 %d 个，提示词 %d 个）"
                % (len(tools), len(resources), len(prompts)))
    report.line("[报告文件] %s" % report.path)
    report.close()
    return 0


# --------------------------------------------------------------------------- 入口
def main(argv: Optional[List[str]] = None) -> int:
    # Windows 管道/控制台默认不是 UTF-8：协议消息里有中文（策略名、来源说明），
    # 先固定编码，否则写一行 JSON 就可能抛 UnicodeEncodeError。
    from .. import console

    console.force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python scripts/mcp_server.py",
        description="QuantTrading Studio 的 MCP 服务器（stdio，供大模型调用）")
    parser.add_argument("--read-only", action="store_true",
                        help="只读模式：不注册写工具（策略/持仓/模拟盘写操作全部禁用）")
    parser.add_argument("--selftest", action="store_true",
                        help="自检：握手 → 工具清单 → 调用一个只读工具，退出码 0/1")
    parser.add_argument("--no-resources", action="store_true", help="不提供 resources（文档）")
    parser.add_argument("--no-prompts", action="store_true", help="不提供 prompts（提示词模板）")
    parser.add_argument("--actor", default="mcp", help="审计日志里的调用者标识（默认 mcp）")
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print("quantstudio-mcp %s（协议 %s）" % (__version__, DEFAULT_PROTOCOL_VERSION))
        return 0

    if args.selftest:
        return selftest(read_only=args.read_only)

    try:
        server = build_server(read_only=args.read_only, actor=args.actor,
                              with_resources=not args.no_resources,
                              with_prompts=not args.no_prompts)
    except Exception as exc:                    # noqa: BLE001 - 启动失败要给人看得懂的提示
        print("MCP 服务器启动失败：%s" % exc, file=sys.stderr)
        traceback.print_exc()
        return 1
    return server.serve()


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(main())
