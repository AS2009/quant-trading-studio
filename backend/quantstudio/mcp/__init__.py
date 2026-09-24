# -*- coding: utf-8 -*-
"""``quantstudio.mcp``：MCP（Model Context Protocol）服务器。

把本项目的量化能力暴露给大模型：真实行情、策略编写与校验、历史回测、持仓账本、模拟盘。
实现为**纯标准库**的 stdio 服务器（MCP 的 stdio 传输就是换行分隔的 JSON-RPC 2.0），
因此 `git clone` 之后无需安装任何依赖即可接入 Claude Desktop / Claude Code / Cursor 等客户端。

模块地图：

| 模块 | 作用 |
|---|---|
| ``protocol`` | JSON-RPC 编解码、stdio 分帧、协议版本协商 |
| ``registry`` | ``ToolSpec`` / JSON Schema 小工具 / 参数校验 / 注册装饰器 |
| ``context`` | 工具运行上下文（服务层入口、只读模式、目录、审计） |
| ``safety`` | 写保护：路径护栏、原子写、自动备份、审计日志 |
| ``server`` | 握手、能力声明、方法分发、stdio 主循环 |
| ``tools_*`` | 各领域工具（行情/策略/回测/持仓+模拟盘） |
| ``resources`` / ``prompts`` | 文档资源与提示词模板 |
| ``cli`` | 命令行入口与自检 |
"""

from .context import ToolContext
from .registry import Registry, ToolError, ToolSpec
from .server import Server
from .tools import TOOL_MODULES, build_registry

__version__ = "1.0.0"


def build_server(**kwargs):
    """装配服务器（惰性导入 ``cli``，避免包初始化期的循环导入）。"""
    from .cli import build_server as _build_server

    return _build_server(**kwargs)


def main(argv=None) -> int:
    """命令行入口（等价于 ``python -m quantstudio.mcp``）。"""
    from .cli import main as _main

    return _main(argv)


__all__ = [
    "Registry",
    "ToolSpec",
    "ToolError",
    "ToolContext",
    "Server",
    "TOOL_MODULES",
    "build_registry",
    "build_server",
    "main",
    "__version__",
]
