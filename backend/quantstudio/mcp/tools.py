# -*- coding: utf-8 -*-
"""工具装配：把各 ``tools_*.py`` 分组注册进同一个 :class:`~quantstudio.mcp.registry.Registry`。

新增一组工具时：写一个 ``tools_<领域>.py``，里面实现 ``register(registry)``，
然后把它加到下面的 ``TOOL_MODULES``。
"""

from typing import List, Optional, Tuple

from .registry import Registry, ToolError, p_bool, p_int, p_list, p_num, p_object, p_str, obj  # noqa: F401

#: 工具分组模块（按顺序注册，模块名 → 领域）
TOOL_MODULES: Tuple[str, ...] = (
    "tools_market",        # 行情、自选池、系统状态
    "tools_level2",        # 盘口、逐笔、资金流（只读）
    "tools_strategy",      # 策略清单、源码读写与校验、用户策略
    "tools_backtest",      # 回测与缓存
    "tools_portfolio",     # 持仓账本 + 模拟盘
)


def build_registry(modules: Optional[Tuple[str, ...]] = None) -> Registry:
    """构造注册表：导入每个分组模块并调用它的 ``register(registry)``。"""
    import importlib

    registry = Registry()
    for name in (modules or TOOL_MODULES):
        module = importlib.import_module("." + name, __package__)
        register = getattr(module, "register", None)
        if register is None:
            raise RuntimeError("工具模块 %s 必须实现 register(registry)" % name)
        register(registry)
    return registry


__all__ = ["build_registry", "TOOL_MODULES", "ToolError", "obj", "p_str", "p_int", "p_num",
           "p_bool", "p_list", "p_object"]
