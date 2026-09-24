# -*- coding: utf-8 -*-
"""MCP 的 stdio 传输与 JSON-RPC 2.0 编解码（纯标准库实现）。

MCP（Model Context Protocol）在 stdio 上的传输形式是 **换行分隔的 JSON-RPC 2.0**：
一行一条消息、消息内不含裸换行、UTF-8 编码。本模块只负责「这一层的正确性」：

* 编解码：``encode``/``decode``（``ensure_ascii=False``，中文不被转义）；
* 分帧：``read_message``/``write_message``（忽略空行、兼容 ``\\r\\n``、永不把裸换行写进去）；
* 报文形状校验：``validate_request``（jsonrpc 版本、method、id、params 类型）；
* 标准错误码与响应构造：``require_error_code`` 之外的 ``response``/``error_response``/``notification``。

**协议版本**：MCP 用日期字符串声明版本（如 ``2025-06-18``）。服务器在 ``initialize`` 时：
客户端请求的版本若在支持列表内则回显它，否则回自己的最高版本（规范允许并提示客户端降级），
这样新老客户端都能连上。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

JSONRPC_VERSION = "2.0"

#: 本服务器支持协商的协议版本（由新到旧）。新增版本时同时更新 ``docs/mcp.md``。
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: Tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "quant-trading-studio"


class ErrorCode:
    """JSON-RPC 2.0 标准错误码（MCP 沿用同一套）。"""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """协议层错误：会被 ``server`` 转成 JSON-RPC error 响应。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__("%s (%d)" % (message, code))
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------- 报文构造
def encode(message: Dict[str, Any]) -> str:
    """把消息编码成「一行 JSON」（不含换行符），供 stdio 写出。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode(line: str) -> Any:
    """把一行文本解码成 JSON 对象；失败抛 :class:`ProtocolError`（-32700）。"""
    try:
        return json.loads(line)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.PARSE_ERROR, "JSON 解析失败：%s" % exc)


def response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def notification(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def is_notification(message: Dict[str, Any]) -> bool:
    """没有 ``id`` 的消息是通知（不需要响应）。"""
    return isinstance(message, dict) and "id" not in message


# --------------------------------------------------------------------------- 校验
def validate_request(message: Any) -> Dict[str, Any]:
    """校验一条入站消息；不合法时抛 :class:`ProtocolError`。"""
    if not isinstance(message, dict):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "消息必须是 JSON 对象")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "jsonrpc 字段必须是 \"2.0\"")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "缺少 method 字段")
    request_id = message.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "id 必须是字符串或数字")
    params = message.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, "params 必须是对象或数组")
    return message


def params_dict(message: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``params`` 并统一成 dict（缺省为 ``{}``；数组视为非法）。"""
    params = message.get("params")
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise ProtocolError(ErrorCode.INVALID_PARAMS, "params 必须是 JSON 对象")


# --------------------------------------------------------------------------- stdio 分帧
def read_message(stream) -> Optional[Any]:
    """从 ``stream`` 读一行并解码；EOF 时返回 ``None``，空行会被跳过。"""
    while True:
        line = stream.readline()
        if not line:
            return None                     # EOF
        line = line.strip()
        if line:
            return decode(line)


def write_message(stream, message: Dict[str, Any]) -> None:
    """把消息写成一行并 flush（必须立即刷出，客户端在等）。"""
    stream.write(encode(message) + "\n")
    try:
        stream.flush()
    except Exception:                       # noqa: BLE001 - 管道已关闭时忽略
        pass


def negotiate_version(requested: Optional[str]) -> str:
    """版本协商：支持就回显客户端的，否则回本服务器最高版本。"""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


__all__ = [
    "JSONRPC_VERSION",
    "DEFAULT_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SERVER_NAME",
    "ErrorCode",
    "ProtocolError",
    "encode",
    "decode",
    "response",
    "error_response",
    "notification",
    "is_notification",
    "validate_request",
    "params_dict",
    "read_message",
    "write_message",
    "negotiate_version",
]
