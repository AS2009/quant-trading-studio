# -*- coding: utf-8 -*-
"""工具注册表：``ToolSpec``、JSON Schema 小工具、参数校验、注册装饰器。

所有 ``tools_*.py`` 都按同一套写法注册工具：

.. code-block:: python

    def register(registry):
        @registry.tool("market_quotes", "查询实时行情快照（默认自选池）",
                       obj({"codes": p_list("标的代码，如 ['600519.SH']")}),
                       group="market", read_only=True, open_world=True)
        def market_quotes(ctx, args):
            ...
            return "文本摘要（模型优先读）", {"结构化": "字段"}

约定：

* 工具名 ``<领域>_<动作>``（``market_quotes``、``strategy_write_source``），模型友好；
* ``read_only=True`` 的工具在只读模式下可见；写工具（``read_only=False``）在 ``--read-only`` 时
  从 ``tools/list`` 中消失（客户端看不到 = 模型调不到）；
* ``handler(ctx, args)`` 返回 ``str`` 或 ``(text, structured_dict)``；
* 失败时抛 :class:`ToolError`（带 ``hint`` 告诉模型下一步怎么做），会被转成 ``isError=true`` 的结果。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: handler 的返回类型：纯文本，或 (文本, 结构化数据)
HandlerResult = Any


class ToolError(Exception):
    """工具执行失败：面向模型的可读错误 + 可执行建议。"""

    def __init__(self, message: str, hint: str = "", code: str = "TOOL_ERROR"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def text(self) -> str:
        if self.hint:
            return "%s\n建议：%s" % (self.message, self.hint)
        return self.message


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Any, Dict[str, Any]], HandlerResult]
    title: str = ""
    group: str = ""
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = True
    tags: List[str] = field(default_factory=list)

    def annotations(self) -> Dict[str, Any]:
        return {
            "readOnlyHint": bool(self.read_only),
            "destructiveHint": bool(self.destructive),
            "idempotentHint": bool(self.idempotent),
            "openWorldHint": bool(self.open_world),
        }

    def to_mcp(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations(),
        }
        if self.title:
            payload["title"] = self.title
        return payload


# --------------------------------------------------------------------------- JSON Schema 小工具
def obj(properties: Optional[Dict[str, Any]] = None, required: Sequence[str] = (),
        description: Optional[str] = None, additional: bool = False) -> Dict[str, Any]:
    """构造 ``object`` 类型的 JSON Schema（``additionalProperties`` 默认关闭，挡住拼错的参数名）。"""
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": dict(properties or {}),
        "additionalProperties": bool(additional),
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def p_str(description: str, default: Optional[str] = None, enum: Optional[Sequence[str]] = None,
          pattern: Optional[str] = None, min_length: Optional[int] = None,
          max_length: Optional[int] = None, examples: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "string", "description": description}
    if default is not None:
        prop["default"] = default
    if enum:
        prop["enum"] = list(enum)
    if pattern:
        prop["pattern"] = pattern
    if min_length is not None:
        prop["minLength"] = min_length
    if max_length is not None:
        prop["maxLength"] = max_length
    if examples:
        prop["examples"] = list(examples)
    return prop


def p_int(description: str, default: Optional[int] = None, minimum: Optional[int] = None,
          maximum: Optional[int] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        prop["default"] = default
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    return prop


def p_num(description: str, default: Optional[float] = None, minimum: Optional[float] = None,
          maximum: Optional[float] = None, exclusive_minimum: Optional[float] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "number", "description": description}
    if default is not None:
        prop["default"] = default
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    if exclusive_minimum is not None:
        prop["exclusiveMinimum"] = exclusive_minimum
    return prop


def p_bool(description: str, default: Optional[bool] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        prop["default"] = default
    return prop


def p_list(description: str, items: Optional[Dict[str, Any]] = None, default: Optional[list] = None,
           min_items: Optional[int] = None, max_items: Optional[int] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "array", "description": description,
                            "items": dict(items or {"type": "string"})}
    if default is not None:
        prop["default"] = list(default)
    if min_items is not None:
        prop["minItems"] = min_items
    if max_items is not None:
        prop["maxItems"] = max_items
    return prop


def p_object(description: str, properties: Optional[Dict[str, Any]] = None,
             default: Optional[dict] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "object", "description": description}
    if properties:
        prop["properties"] = dict(properties)
    if default is not None:
        prop["default"] = dict(default)
    return prop


# --------------------------------------------------------------------------- 参数校验（轻量，无第三方依赖）
_TYPE_CHECKS: Dict[str, Tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_args(spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    """校验必需参数与基本类型；不通过时抛 :class:`ToolError`（含可执行建议）。"""
    schema = spec.input_schema or {}
    properties: Dict[str, Any] = schema.get("properties") or {}
    missing = [name for name in (schema.get("required") or []) if args.get(name) is None]
    if missing:
        raise ToolError(
            "缺少必需参数：%s" % ", ".join(missing),
            hint="请按 tools/list 中 %s 的 inputSchema 补齐参数后重试。" % spec.name,
            code="INVALID_ARGS",
        )
    for name, value in args.items():
        prop = properties.get(name)
        if value is None or not prop:
            continue
        expected = prop.get("type")
        checker = _TYPE_CHECKS.get(str(expected)) if expected else None
        if not checker:
            continue
        if expected in ("integer", "number") and isinstance(value, bool):
            raise ToolError("参数 %s 期望 %s，收到布尔值" % (name, expected),
                            hint="请传数字；布尔值请改用 true/false 的其它参数。", code="INVALID_ARGS")
        if not isinstance(value, checker):
            raise ToolError("参数 %s 期望 %s，收到 %s" % (name, expected, type(value).__name__),
                            hint="请参照 inputSchema 修正类型。", code="INVALID_ARGS")
        if expected == "array" and prop.get("items", {}).get("type") == "string":
            bad = [item for item in value if not isinstance(item, str)]
            if bad:
                raise ToolError("参数 %s 里含非字符串元素：%r" % (name, bad[:3]),
                                hint="数组元素必须是字符串。", code="INVALID_ARGS")
        if expected == "string" and prop.get("enum") and value not in prop["enum"]:
            raise ToolError("参数 %s 取值非法：%r" % (name, value),
                            hint="可选值：%s" % ", ".join(prop["enum"]), code="INVALID_ARGS")
        if expected in ("integer", "number"):
            minimum, maximum = prop.get("minimum"), prop.get("maximum")
            if minimum is not None and value < minimum:
                raise ToolError("参数 %s 不能小于 %s（收到 %s）" % (name, minimum, value),
                                code="INVALID_ARGS")
            if maximum is not None and value > maximum:
                raise ToolError("参数 %s 不能大于 %s（收到 %s）" % (name, maximum, value),
                                code="INVALID_ARGS")
    if schema.get("additionalProperties") is False:
        unknown = sorted(name for name in args if name not in properties)
        if unknown:
            allowed = ", ".join(sorted(properties)) or "（该工具无参数）"
            raise ToolError(
                "不认识的参数：%s" % ", ".join(unknown),
                hint="本工具可用参数：%s。请按 tools/list 里的 inputSchema 传参（模型常见的错拼会在这里被挡住）。"
                     % allowed,
                code="INVALID_ARGS",
            )
    return args


# --------------------------------------------------------------------------- 注册表
class Registry:
    """工具集合。写工具是否可见由运行模式决定（``--read-only`` 时隐藏）。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    # -- 注册
    def add(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError("工具名重复：%s" % spec.name)
        self._tools[spec.name] = spec
        return spec

    def tool(self, name: str, description: str, schema: Optional[Dict[str, Any]] = None, **meta: Any):
        """装饰器：把被装饰函数注册成工具。``meta`` 见 :class:`ToolSpec`。"""
        def decorator(fn: Callable[[Any, Dict[str, Any]], HandlerResult]):
            spec = ToolSpec(name=name, description=description, input_schema=schema or obj(),
                            handler=fn, **meta)
            self.add(spec)
            return fn

        return decorator

    # -- 查询
    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def visible(self, writable: bool = True) -> List[ToolSpec]:
        """可用的工具：只读模式下过滤掉写工具。"""
        return [spec for spec in self.all() if writable or spec.read_only]

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for spec in self.all():
            out.setdefault(spec.group or "other", []).append(spec.name)
        return out

    def __len__(self) -> int:
        return len(self._tools)


__all__ = [
    "ToolError",
    "ToolSpec",
    "Registry",
    "validate_args",
    "obj",
    "p_str",
    "p_int",
    "p_num",
    "p_bool",
    "p_list",
    "p_object",
]
