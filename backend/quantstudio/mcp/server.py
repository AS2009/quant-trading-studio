# -*- coding: utf-8 -*-
"""MCP 服务器：握手、能力声明、方法分发、stdio 主循环。

职责边界：本模块只做「协议与分发」，业务逻辑全在 ``quantstudio.services``，
工具定义全在 ``tools_*.py``，资源与提示词在 ``resources.py`` / ``prompts.py``。

三条硬规矩（写新代码时别破坏）：

1. **stdout 只允许出现协议消息**；所有日志、异常、调试输出一律走 ``self.log()`` → stderr。
2. **工具执行失败不算协议错误**：返回 ``isError=true`` 的结果（模型能读到原因并自行修正）；
   只有报文本身不合法、方法不存在、参数结构错误才返回 JSON-RPC ``error``。
3. **只读模式隐藏写工具**：``tools/list`` 只列出 ``visible()`` 的工具，模型看不到就调不到。
"""

import sys
import traceback
from typing import Any, Dict, List, Optional

from . import protocol

# 工具执行期间把 stdout 改道（策略/回测代码是本进程执行的，print 会污染协议流）
import contextlib
import io
from .context import ToolContext
from .registry import Registry, ToolError, validate_args

#: 服务端给模型的总说明（客户端会把它放进系统提示）
INSTRUCTIONS = """本服务器提供 A 股量化研究能力（真实行情、策略编写与校验、历史回测、持仓账本、模拟盘）。

典型用法：
1. 先看 `system_status` / `strategy_list` 了解数据源与现有策略；
2. 写策略：`strategy_read_source` 读现有代码 → 改好后 `strategy_write_source` 落盘（会自动跑校验并返回问题清单）
   → `strategy_lint` 复查 → `backtest_run` 用真实历史数据验证；
3. 看盘：`market_overview` / `market_quotes` / `market_kline`；
4. 账户：`portfolio_*`（自有持仓账本）与 `paper_*`（模拟盘）。

注意：所有委托都只进本地模拟盘，**不会**触达任何券商；重结果（净值序列、成交流水）请按需开启参数，
避免一次性灌满上下文。"""


class Server:
    def __init__(self, registry: Registry, *, context: ToolContext,
                 resources: Any = None, prompts: Any = None,
                 name: str = protocol.SERVER_NAME, version: str = "",
                 instructions: str = INSTRUCTIONS, stderr: Any = None):
        self.registry = registry
        self.context = context
        self.resources = resources
        self.prompts = prompts
        self.name = name
        self.version = version or _core_version()
        self.instructions = instructions
        self._stderr = stderr
        self.initialized = False
        self.client_info: Dict[str, Any] = {}
        self.protocol_version = protocol.DEFAULT_PROTOCOL_VERSION
        self.log(f"[mcp] {name} v{self.version} 启动；工具 {len(registry)} 个；"
                 f"模式：{'只读' if context.read_only else '可读写'}")

    # ------------------------------------------------------------------ 日志（只走 stderr）
    def log(self, text: str) -> None:
        stream = self._stderr if self._stderr is not None else sys.stderr
        if stream is None:
            return
        try:
            stream.write(str(text).rstrip() + "\n")
            stream.flush()
        except Exception:                       # noqa: BLE001 - 日志失败不能影响协议
            pass

    # ------------------------------------------------------------------ 能力
    def capabilities(self) -> Dict[str, Any]:
        caps: Dict[str, Any] = {"tools": {"listChanged": False}}
        if self.resources is not None:
            caps["resources"] = {"subscribe": False, "listChanged": False}
        if self.prompts is not None:
            caps["prompts"] = {"listChanged": False}
        return caps

    def server_info(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version}

    def writable(self) -> bool:
        return not self.context.read_only

    # ------------------------------------------------------------------ 方法实现
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.protocol_version = protocol.negotiate_version(params.get("protocolVersion"))
        self.client_info = params.get("clientInfo") or {}
        self.initialized = True
        self.log("[mcp] 客户端：%s %s（协议 %s）" % (
            self.client_info.get("name", "?"), self.client_info.get("version", "?"),
            params.get("protocolVersion", "?")))
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities(),
            "serverInfo": self.server_info(),
            "instructions": self.instructions,
        }

    def _tools_list(self) -> Dict[str, Any]:
        tools = [spec.to_mcp() for spec in self.registry.visible(writable=self.writable())]
        return {"tools": tools}

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "tools/call 需要字符串参数 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "tools/call 的 arguments 必须是对象")
        spec = self.registry.get(name)
        if spec is None:
            raise protocol.ProtocolError(
                protocol.ErrorCode.INVALID_PARAMS,
                "未知工具：%s" % name,
                data={"available": self.registry.names()},
            )
        if spec.read_only is False and not self.writable():
            return _error_result("工具 %s 在当前只读模式下不可用（--read-only 启动）。" % name,
                                 hint="请让用户去掉 --read-only 重启，或改用只读工具。")
        # 策略/回测代码是**本进程执行**的，一句 print 就会污染 stdout（协议流）——
        # 这里把工具执行期间的 stdout 改道，事后写进 stderr，保证客户端只读到 JSON-RPC 消息。
        leaked = io.StringIO()
        try:
            with contextlib.redirect_stdout(leaked):
                validate_args(spec, arguments)
                result = spec.handler(self.context, arguments)
        except ToolError as exc:
            self.log("[mcp] 工具 %s 失败：%s" % (name, exc.text().replace("\n", " ")[:300]))
            return _error_result(exc.text(), code=exc.code)
        except protocol.ProtocolError:
            raise
        except Exception as exc:                # noqa: BLE001 - 兜底：不让一个工具掀翻服务器
            self.log("[mcp] 工具 %s 内部异常：\n%s" % (name, traceback.format_exc()))
            return _error_result("工具 %s 执行时发生内部错误：%s: %s" % (name, type(exc).__name__, exc),
                                 hint="可先用 system_status 检查环境；若持续失败请把该错误反馈给用户。")
        finally:
            text = leaked.getvalue().strip()
            if text:
                self.log("[mcp] 工具 %s 往 stdout 写了内容（已改道 stderr，避免污染协议）：%s"
                         % (name, text[:500]))
        return _ok_result(result)

    def _resources_list(self) -> Dict[str, Any]:
        return {"resources": list(self.resources.list() or [])}

    def _resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "resources/read 需要字符串参数 uri")
        payload = self.resources.read(uri)
        if payload is None:
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "未知资源：%s" % uri,
                                         data={"available": [item.get("uri") for item in self.resources.list()]})
        return payload

    def _prompts_list(self) -> Dict[str, Any]:
        return {"prompts": list(self.prompts.list() or [])}

    def _prompts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "prompts/get 需要字符串参数 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "prompts/get 的 arguments 必须是对象")
        payload = self.prompts.get(name, arguments)
        if payload is None:
            raise protocol.ProtocolError(protocol.ErrorCode.INVALID_PARAMS,
                                         "未知提示词：%s" % name,
                                         data={"available": [item.get("name") for item in self.prompts.list()]})
        return payload

    # ------------------------------------------------------------------ 分发
    def dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._tools_list()
        if method == "tools/call":
            return self._tools_call(params)
        if method == "resources/list":
            if self.resources is None:
                raise _no_capability("resources")
            return self._resources_list()
        if method == "resources/read":
            if self.resources is None:
                raise _no_capability("resources")
            return self._resources_read(params)
        if method == "resources/templates/list":
            if self.resources is None:
                raise _no_capability("resources")
            templates = getattr(self.resources, "templates", None)
            return {"resourceTemplates": list(templates() if callable(templates) else (templates or []))}
        if method == "prompts/list":
            if self.prompts is None:
                raise _no_capability("prompts")
            return self._prompts_list()
        if method == "prompts/get":
            if self.prompts is None:
                raise _no_capability("prompts")
            return self._prompts_get(params)
        if method == "logging/setLevel":
            return {}                            # 接受并忽略（我们只往 stderr 打日志）
        raise protocol.ProtocolError(protocol.ErrorCode.METHOD_NOT_FOUND,
                                     "不支持的方法：%s" % method)

    def handle_raw(self, message: Any) -> Optional[Dict[str, Any]]:
        """处理一条已解码的消息；返回 ``None`` 表示这是通知、无需响应。"""
        if isinstance(message, list):
            # JSON-RPC 批量（2024-11-05 客户端可能这么发）；逐条处理并回数组
            replies = [item for item in (self.handle_raw(entry) for entry in message) if item is not None]
            return replies or None
        try:
            protocol.validate_request(message)
        except protocol.ProtocolError as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            return protocol.error_response(request_id, exc.code, exc.message)
        method = message["method"]
        request_id = message.get("id")
        is_notification = protocol.is_notification(message)
        if method.startswith("notifications/") or is_notification:
            if method == "notifications/initialized":
                self.initialized = True
            return None                          # 通知一律不响应（未知通知也静默忽略）
        try:
            params = protocol.params_dict(message)
            result = self.dispatch(method, params)
        except protocol.ProtocolError as exc:
            return protocol.error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:                 # noqa: BLE001
            self.log("[mcp] 方法 %s 异常：\n%s" % (method, traceback.format_exc()))
            return protocol.error_response(request_id, protocol.ErrorCode.INTERNAL_ERROR,
                                           "服务器内部错误：%s: %s" % (type(exc).__name__, exc))
        if result is None:
            return None
        return protocol.response(request_id, result)

    def handle_line(self, line: str) -> Optional[Dict[str, Any]]:
        """处理一行文本（含解析错误处理），便于测试与自检直接调用。"""
        try:
            message = protocol.decode(line)
        except protocol.ProtocolError as exc:
            return protocol.error_response(None, exc.code, exc.message)
        return self.handle_raw(message)

    # ------------------------------------------------------------------ 主循环
    def serve(self, stdin: Any = None, stdout: Any = None) -> int:
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        while True:
            try:
                message = protocol.read_message(stdin)
            except protocol.ProtocolError as exc:
                protocol.write_message(stdout, protocol.error_response(None, exc.code, exc.message))
                continue
            if message is None:
                self.log("[mcp] 输入结束（EOF），退出")
                return 0
            started_ok = True
            try:
                reply = self.handle_raw(message)
            except Exception:                    # noqa: BLE001 - 极端情况兜底
                started_ok = False
                self.log("[mcp] 处理消息异常：\n%s" % traceback.format_exc())
                reply = protocol.error_response(None, protocol.ErrorCode.INTERNAL_ERROR, "服务器内部错误")
            if reply is not None:
                try:
                    protocol.write_message(stdout, reply)
                except Exception:                # noqa: BLE001 - 管道断了就结束
                    self.log("[mcp] 写响应失败（客户端可能已断开）")
                    return 1
            if not started_ok and getattr(self.context, "fail_fast", False):
                return 1


# --------------------------------------------------------------------------- 结果整形
def _ok_result(result: Any) -> Dict[str, Any]:
    text, structured = _split(result)
    payload: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        payload["structuredContent"] = structured
    return payload


def _error_result(text: str, *, hint: str = "", code: str = "TOOL_ERROR") -> Dict[str, Any]:
    if hint:
        text = "%s\n建议：%s" % (text, hint)
    return {"content": [{"type": "text", "text": text}], "isError": True,
            "structuredContent": {"error": {"code": code, "message": text}}}


def _split(result: Any):
    """handler 返回值 → (文本, 结构化或 None)。"""
    if isinstance(result, tuple) and len(result) == 2:
        text, structured = result
        return _as_text(text), structured
    if isinstance(result, str):
        return result, None
    if isinstance(result, dict):
        import json
        return json.dumps(result, ensure_ascii=False, indent=2), result
    return _as_text(result), None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    import json
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _no_capability(name: str) -> protocol.ProtocolError:
    return protocol.ProtocolError(protocol.ErrorCode.METHOD_NOT_FOUND,
                                  "本服务器未启用 %s 能力" % name)


def _core_version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:                            # noqa: BLE001
        return "0"


__all__ = ["Server", "INSTRUCTIONS"]
