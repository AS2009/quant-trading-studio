# -*- coding: utf-8 -*-
"""策略工具分组：清单 / 详情 / 源码读写 / 校验 / 用户策略。

本组是「让大模型改策略」的核心：模型先 ``strategy_read_source`` 学现有写法，
再 ``strategy_write_source`` 落盘（自动备份 + 原子写 + 热加载 + 跑校验），
``strategy_lint`` 可反复自检，坏了还能 ``strategy_delete_source`` 回滚。

写路径的安全边界（全部复用 :mod:`quantstudio.mcp.safety`，本模块不另造黑名单）：

* **路径护栏**：id 必须匹配 ``^st_[a-z0-9_]{2,36}$``，再经 ``safe_child`` 落到
  ``strategies/local/<slug>.py``（``slug`` = id 去掉 ``st_`` 前缀，与 ``local/`` 目录约定一致：
  ``breakout_atr.py`` ↔ ``st_breakout_atr`` ↔ ``BreakoutAtrStrategy``）；
* **大小上限**：``check_source_size``（128KB），超限拒绝落盘；
* **先备份后覆盖**：``backup_file`` → ``<data>/strategy-backups/<文件名>.<时间戳>``；
* **原子写**：``atomic_write_text``（同目录临时文件 + ``os.replace``）；
* **代码正确性**：由 ``quantstudio.strategies.lint``（静态 + 运行期 + 烟雾回测）判定；
  lint 有 error **仍然落盘**（模型可以照着 code 继续迭代），只在返回值里如实汇报。

工具一览（名字即 MCP 工具名）::

    strategy_list           列出策略（文本表格 + 完整结构化列表）
    strategy_get            单个策略详情 + 参数 schema
    strategy_read_source    读策略源码（local 文件 / 包内内置策略）
    strategy_lint           校验候选源码（临时文件，不落盘）或已有策略文件
    strategy_write_source   写 local/<slug>.py（核心写工具）
    strategy_delete_source  删 local/<slug>.py（需 confirm=true）
    strategy_user_create    新建用户策略（内置模板 + 参数，JSON）
    strategy_user_delete    删除用户策略（JSON 条目）

只读工具 ``read_only=True``；写工具 ``read_only=False`` 并按语义标 ``destructive`` / ``idempotent``；
全部 ``open_world=False``（只读写本机文件与本地数据目录，不访问网络）。
"""

import ast
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from ..strategies import lint as lint_mod
from ..strategies import registry as strategy_registry
from .registry import ToolError, obj, p_bool, p_object, p_str
from .safety import (
    GuardError,
    atomic_write_text,
    backup_file,
    byte_size,
    check_source_size,
    safe_child,
)

#: 工具分组名（``registry.groups()`` / 文档用）
GROUP = "strategy"

#: 本地策略 id 规范（与 ``strategies/lint.py`` 的 ``ID_RE`` 保持一致）
ID_RE = re.compile(r"^st_[a-z0-9_]{2,36}$")

#: ``strategies/local/`` 对应的包名（热加载时用来失效 ``sys.modules`` 缓存）
_LOCAL_PACKAGE = "quantstudio.strategies.local"

_ID_HINT = "id 必须是 st_ + 小写 snake_case（字母/数字/下划线，3-36 字符），例如 st_mcp_demo。"
_LIST_HINT = "可先用 strategy_list 查看全部可用策略 id。"
_NEXT_LIST = ("下一步：strategy_get 看参数 schema；读现有代码用 strategy_read_source；"
              "改代码用 strategy_write_source。")


# =========================================================================== 路径与缓存
def _text_path(ctx: Any, path: Any) -> str:
    """把绝对路径脱敏成相对 ``ctx.repo_root`` 的相对路径（仓库外只给文件名）。"""
    target = os.path.abspath(str(path or ""))
    try:
        rel = os.path.relpath(target, os.path.abspath(ctx.repo_root))
    except (OSError, ValueError):
        return os.path.basename(target)
    if rel.startswith(".."):
        return os.path.basename(target)
    return rel.replace(os.sep, "/")


def _guard_local_path(ctx: Any, name: str) -> str:
    """在 ``ctx.local_dir`` 下解析单层文件名；越界一律转成 :class:`ToolError`。"""
    try:
        return safe_child(ctx.local_dir, name, suffix=".py")
    except GuardError as exc:
        raise ToolError("策略文件路径不合法：%s" % exc,
                        hint="strategy_id 只能是 %s 这种形式，脚本不会写到 strategies/local/ 之外。" % _ID_HINT,
                        code="UNSAFE_PATH") from exc


def _strategy_path(ctx: Any, strategy_id: Any) -> str:
    """``strategy_id`` → ``local/<slug>.py``（先校验 id 规范，再走安全护栏）。"""
    key = str(strategy_id or "").strip()
    if not ID_RE.match(key):
        raise ToolError("strategy_id 非法：%r" % (strategy_id,),
                        hint=_ID_HINT + " 不要带路径分隔符或 .py 后缀。",
                        code="INVALID_ID")
    # ① 按「id + .py」过一次护栏（挡住任何形式的路径穿越），
    # ② 实际落盘用 slug（id 去掉 st_ 前缀）：local/ 目录的命名约定是
    #    breakout_atr.py ↔ st_breakout_atr，lint 会校验文件名与 id 一致。
    _guard_local_path(ctx, key + ".py")
    slug = key[3:]
    return _guard_local_path(ctx, slug + ".py")


def _existing_local_path(ctx: Any, strategy_id: Any) -> Optional[str]:
    """已存在的本地策略文件路径；不存在返回 ``None``（不做 id 格式校验）。"""
    key = str(strategy_id or "").strip()
    if not ID_RE.match(key):
        return None
    try:
        path = _guard_local_path(ctx, key[3:] + ".py")
    except ToolError:
        return None
    return path if os.path.isfile(path) else None


def _local_module_name(slug: str) -> str:
    return "%s.%s" % (_LOCAL_PACKAGE, slug)


def _forget_local_module(slug: str) -> None:
    """丢弃 ``sys.modules`` 里的旧模块，让 ``discover_local`` 真正重新读文件。

    ``discover_local(force=True)`` 只做「重新扫描 + 注册」：模块已缓存时它拿到的是旧类
    （改完文件不生效），删了文件时又仍能拿到旧类（删完还在列表里）。写/删之前按模块名
    精确失效，是「改完立刻生效」的必要一步。
    """
    sys.modules.pop(_local_module_name(slug), None)


def _forget_local_strategy(key: str, slug: str) -> None:
    """删除文件后清理进程内注册表，让 ``strategy_list`` 立刻不再显示它。"""
    module_name = _local_module_name(slug)
    sys.modules.pop(module_name, None)
    cls = strategy_registry.REGISTRY.get(key)
    if cls is not None and getattr(cls, "__module__", "") == module_name:
        strategy_registry.REGISTRY.pop(key, None)
        if key in strategy_registry.LOCAL_IDS:
            strategy_registry.LOCAL_IDS.remove(key)
    strategy_registry.discover_local(force=True)


# =========================================================================== 服务层调用
def _rethrow(exc: BaseException) -> None:
    raise exc


def _service_call(ctx: Any, fn: Any, *args: Any) -> Any:
    """调用服务层：NotFound / Forbidden / ValidationError 转成模型能照做的 :class:`ToolError`。"""
    from ..core.errors import ValidationError
    from ..services.common import Forbidden, NotFound

    try:
        return fn(*args)
    except NotFound as exc:
        raise ToolError(str(exc), hint=_LIST_HINT, code="NOT_FOUND") from exc
    except Forbidden as exc:
        raise ToolError(str(exc),
                        hint="内置策略不可删除；只想换参数请用 strategy_user_create 建模板实例。",
                        code="FORBIDDEN") from exc
    except ValidationError as exc:
        raise ToolError("参数不合法：%s" % exc,
                        hint="请按 strategy_get 给出的 param_schema 修正后重试。",
                        code="VALIDATION") from exc
    except Exception as exc:                      # noqa: BLE001 - 其余交给 ctx.call 统一转换
        ctx.call(_rethrow, exc)


# =========================================================================== lint 封装
def _lint_call(path: str, origin: str, smoke: bool, registry: Any = None) -> Tuple[Dict[str, Any], str]:
    """跑一次 lint（静态 + 运行期 + 烟雾回测），并兜住被校验代码的 stdout。

    MCP 服务器的 stdout 只能有协议消息，而策略代码里可能自带 ``print()``（lint 也会报
    ``E_FORBIDDEN_CALL``）：这里把校验期间的 stdout 收进缓冲区，作为证据回给模型。
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            report = lint_mod.lint_path(path, origin=origin, smoke=bool(smoke), registry=registry)
    except Exception as exc:                      # noqa: BLE001 - 校验器自身崩了也要给结果
        report = {
            "path": path,
            "classes": [],
            "ok": False,
            "errors": [{"level": "error", "code": "E_LINT_CRASH", "line": 0,
                        "message": "校验器内部错误：%s: %s" % (type(exc).__name__, exc)}],
            "warnings": [],
        }
    return report, buffer.getvalue()


def _snake(name: str) -> str:
    """``McpDemo`` → ``mcp_demo``（推导 local 文件名用）。"""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or ""))
    return re.sub(r"[^a-z0-9_]+", "", text.lower()).strip("_")


def _base_name(node: Any) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _class_id_literal(node: ast.ClassDef) -> Optional[str]:
    """取类体里 ``id = "st_xxx"`` 的字面量（合法才返回）。"""
    for item in node.body:
        target: Any = None
        value: Any = None
        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            target, value = item.targets[0].id, item.value
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            target, value = item.target.id, item.value
        if target == "id" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            if ID_RE.match(value.value):
                return value.value
    return None


def _slug_from_source(source: str) -> str:
    """从候选源码推断它应该叫什么文件名（优先类属性 id，其次类名 PascalCase → snake）。"""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    strategies = [node for node in classes
                  if any(_base_name(base) == "BaseStrategy" for base in node.bases)]
    for node in (strategies or classes):
        literal = _class_id_literal(node)
        if literal:
            return literal[3:]
        name = node.name[:-8] if node.name.endswith("Strategy") else node.name
        slug = _snake(name)
        if slug:
            return slug
    return ""


def _lint_candidate(slug: str, source: str, smoke: bool) -> Tuple[Dict[str, Any], str, bool]:
    """把候选源码写进**包内**临时目录校验，随后彻底清理（不落盘、不留残渣）。

    必须写在包内（``quantstudio/strategies/_lint_tmp_*/<slug>.py``）：local 策略用的是
    ``from ..base import ...`` / ``from ...core import ...`` 相对导入，只有按包导入才解析得到；
    目录名以 ``_`` 开头且不是 ``local/`` 下的文件，``discover_local`` 与 ``lint_all`` 都会跳过。

    返回 ``(报告, 被兜住的 stdout, 是否退化成系统临时目录)``。
    """
    strategies_dir = os.path.dirname(os.path.abspath(lint_mod.__file__))
    container = ""
    degraded = False
    temp_pkg = ""
    try:
        try:
            container = tempfile.mkdtemp(prefix="_lint_tmp_%d_" % os.getpid(), dir=strategies_dir)
            temp_pkg = os.path.basename(container)
        except OSError:                          # 只读安装（例如打包产物）：退化到系统临时目录
            container = tempfile.mkdtemp(prefix="qs-lint-")
            degraded = True
        path = safe_child(container, slug + ".py", suffix=".py")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source)
        report, captured = _lint_call(path, "local", smoke, registry=None)
        return report, captured, degraded
    finally:
        if temp_pkg:
            for name in list(sys.modules):
                if name == temp_pkg or name.startswith(temp_pkg + "."):
                    sys.modules.pop(name, None)
        if container:
            shutil.rmtree(container, ignore_errors=True)


def _conflict_issue(key: str, slug: str) -> Optional[Dict[str, Any]]:
    """源码校验时提前发现 id 冲突（同名不同类的策略注册会失败）。"""
    cls = strategy_registry.REGISTRY.get(key)
    if cls is None:
        return None
    module_name = _local_module_name(slug)
    if getattr(cls, "__module__", "") == module_name:
        return None                              # 就是它自己那份 local 文件，改代码不算冲突
    return {
        "level": "error", "code": "E_ID_CONFLICT", "line": 0,
        "message": "策略 id %s 已被 %s 占用（换个 id 或文件名）" % (key, getattr(cls, "__module__", "?")),
    }


# =========================================================================== 文本整形
def _report_state(report: Dict[str, Any]) -> str:
    errors = len(report.get("errors") or [])
    warnings = len(report.get("warnings") or [])
    if report.get("ok"):
        return "通过 ✅（0 个 error / %d 个 warning）" % warnings
    return "未通过 ✗（%d 个 error / %d 个 warning）" % (errors, warnings)


def _report_lines(report: Dict[str, Any], captured: str = "") -> List[str]:
    """把 lint 报告排版成逐条清单（code / message / line）。"""
    lines: List[str] = []
    for issue in report.get("errors") or []:
        lines.append("  ✗ %-20s %s%s" % (issue.get("code"), issue.get("message"),
                                        (" (line %s)" % issue.get("line")) if issue.get("line") else ""))
    for issue in report.get("warnings") or []:
        lines.append("  ! %-20s %s%s" % (issue.get("code"), issue.get("message"),
                                        (" (line %s)" % issue.get("line")) if issue.get("line") else ""))
    text = (captured or "").strip()
    if text:
        head = text.splitlines()[0][:120]
        lines.append("  ! %-20s 校验期间代码向 stdout 写了 %d 行（MCP 要求 stdout 只有协议消息）：%s"
                     % ("stdout", len(text.splitlines()), head))
    return lines


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in str(text))


def _pad(text: Any, width: int) -> str:
    value = str(text if text is not None else "")
    if _display_width(value) > width:
        value = value[:max(1, width - 1)] + "…"
    return value + " " * max(0, width - _display_width(value))


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], min(_display_width(cell), 40))
    out = ["  ".join(_pad(header, widths[index]) for index, header in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * width for width in widths))
    for row in rows:
        out.append("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(row)).rstrip())
    return out


def _param_table(schema: Dict[str, Any]) -> List[str]:
    rows: List[List[Any]] = []
    for name, meta in (schema or {}).items():
        data = meta if isinstance(meta, dict) else {}
        if str(data.get("type") or "").lower() in ("int", "float"):
            span = "%s ~ %s" % (data.get("min"), data.get("max"))
        elif data.get("choices"):
            span = "/".join(str(item) for item in data.get("choices"))
        else:
            span = "-"
        rows.append([name, data.get("label") or name, data.get("type") or "-",
                     data.get("default"), span, data.get("help") or ""])
    if not rows:
        return ["  （该策略没有可调参数）"]
    return _table(["参数", "名称", "类型", "默认值", "取值范围", "说明"], rows)


def _write_next_step(report: Dict[str, Any]) -> str:
    if report.get("ok"):
        return ("下一步：可用 backtest_run（strategy_id=…）跑真实历史数据验证；"
                "要接着改就先 strategy_read_source 再写一次。")
    return "下一步：按上面的 code 逐条修正后，重新调用 strategy_write_source 覆盖写入（会自动备份）。"


# =========================================================================== 工具注册
def register(registry: Any) -> None:
    """把「策略」工具组的 8 个工具注册进 ``registry``（``tools.py`` 的 ``TOOL_MODULES`` 调用）。"""

    # ------------------------------------------------------------------ 只读：清单
    @registry.tool(
        "strategy_list",
        "列出全部策略（内置/本地代码/用户），含参数个数、最少预热 bar 与运行状态；"
        "写代码前先用它确认 id 是否已被占用。",
        obj({"origin": p_str("来源过滤：all=全部（默认）、builtin=内置、local=strategies/local 下的代码策略、"
                             "user=用户参数化策略", default="all", enum=["all", "builtin", "local", "user"])}),
        title="策略清单", group=GROUP, read_only=True, open_world=False, idempotent=True,
    )
    def strategy_list(ctx, args):
        origin = str(args.get("origin") or "all").strip().lower() or "all"
        rows = ctx.jsonable(ctx.call(ctx.strategies_service.list_strategies) or [])
        local = strategy_registry.local_status()
        local_ids = list(local.get("loaded") or [])

        items: List[Dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["origin"] = str(data.get("origin") or ("builtin" if data.get("builtin") else "user"))
            data["param_count"] = len(data.get("param_schema") or {})
            data["min_bars"] = int(data.get("min_bars") or 0)
            data["status"] = str(data.get("status") or "paused")
            if data["origin"] == "local" or data.get("id") in local_ids:
                path = _existing_local_path(ctx, data.get("id"))
                data["file"] = _text_path(ctx, path) if path else ""
            items.append(data)
        if origin != "all":
            items = [item for item in items if item["origin"] == origin]

        labels = {"builtin": "内置", "local": "本地代码", "user": "用户"}
        counts: Dict[str, int] = {}
        for item in items:
            counts[item["origin"]] = counts.get(item["origin"], 0) + 1
        summary = "、".join("%s %d" % (labels.get(key, key), value)
                           for key, value in sorted(counts.items())) or "无"

        lines = ["策略 %d 个（%s）%s" % (len(items), summary,
                                      "" if origin == "all" else "；过滤 origin=%s" % origin)]
        lines.extend(_table(
            ["id", "名称", "类别", "来源", "参数", "最少bar", "状态"],
            [[item.get("id"), item.get("name"), item.get("category"), labels.get(item["origin"], item["origin"]),
              item.get("param_count"), item.get("min_bars"), item.get("status")] for item in items],
        ))
        errors = local.get("errors") or []
        if errors:
            lines.append("strategies/local/ 有 %d 个文件未能加载：" % len(errors))
            for entry in errors[:5]:
                lines.append("  ✗ %s：%s" % (entry.get("file"), entry.get("error")))
        lines.append(_NEXT_LIST)

        structured = {
            "count": len(items),
            "total": len(rows),
            "origin": origin,
            "by_origin": counts,
            "strategies": items,
            "local": local,
            "next_step": _NEXT_LIST,
        }
        return "\n".join(lines), structured

    # ------------------------------------------------------------------ 只读：详情
    @registry.tool(
        "strategy_get",
        "查询单个策略的完整定义与参数 schema（参数名/类型/默认值/范围/说明）。",
        obj({"strategy_id": p_str("策略 id，例如 st_ma_cross 或 strategies/local 下的 st_xxx")},
            required=["strategy_id"]),
        title="策略详情", group=GROUP, read_only=True, open_world=False, idempotent=True,
    )
    def strategy_get(ctx, args):
        key = str(args.get("strategy_id") or "").strip()
        data = dict(_service_call(ctx, ctx.strategies_service.get_strategy, key) or {})
        schema = data.get("param_schema") or {}
        params: List[Dict[str, Any]] = []
        for name in schema:
            meta = schema[name] if isinstance(schema[name], dict) else {}
            params.append({
                "name": name,
                "label": meta.get("label"),
                "type": meta.get("type"),
                "default": meta.get("default"),
                "min": meta.get("min"),
                "max": meta.get("max"),
                "step": meta.get("step"),
                "choices": meta.get("choices"),
                "help": meta.get("help"),
            })

        labels = {"builtin": "内置", "local": "本地代码", "user": "用户模板实例"}
        origin = str(data.get("origin") or "builtin")
        lines = ["%s（%s）· %s" % (data.get("name") or key, key, labels.get(origin, origin)),
                 "类别：%s ｜ 频率：%s ｜ 标的池：%s ｜ 最少 bar：%s ｜ 版本：%s"
                 % (data.get("category"), data.get("freq"), data.get("universe_type"),
                    data.get("min_bars"), data.get("version")),
                 "说明：%s" % (data.get("desc") or "（无）")]
        local_path = _existing_local_path(ctx, key)
        if local_path:
            lines.append("源码文件：%s" % _text_path(ctx, local_path))
        lines.append("参数（%d 个，默认值）：" % len(params))
        lines.extend(_param_table(schema))
        if origin == "local" or local_path:
            lines.append("下一步：strategy_read_source 读源码 → strategy_write_source 改完即生效；"
                         "backtest_run 跑真实历史验证。")
        elif origin == "user":
            lines.append("下一步：用户策略没有独立源码（模板 %s + 参数）；"
                         "backtest_run 可直接用 id=%s 回测。" % (data.get("template") or "内置模板", key))
        else:
            lines.append("下一步：想改参数就 strategy_user_create（template=%s + params）；"
                         "想看代码用 strategy_read_source。" % key)
        return "\n".join(lines), {
            "strategy": data,
            "param_schema": schema,
            "params": params,
            "file": _text_path(ctx, local_path) if local_path else "",
        }

    # ------------------------------------------------------------------ 只读：读源码
    @registry.tool(
        "strategy_read_source",
        "读取策略的 Python 源码（local 策略读文件；内置/用户策略读其模板类的源文件），"
        "用于在现有风格上修改；返回值同时给出源码文本、行数与仓库内相对路径。",
        obj({"strategy_id": p_str("策略 id，例如 st_breakout_atr（local）或 st_ma_cross（内置）")},
            required=["strategy_id"]),
        title="读取策略源码", group=GROUP, read_only=True, open_world=False, idempotent=True,
    )
    def strategy_read_source(ctx, args):
        key = str(args.get("strategy_id") or "").strip()
        local_path = _existing_local_path(ctx, key)
        origin = "local" if local_path else "builtin"
        template = ""
        note = ""
        cls = None
        if local_path:
            path = local_path
        else:
            cls = strategy_registry.try_get_class(key)
            if cls is None:
                data = dict(_service_call(ctx, ctx.strategies_service.get_strategy, key) or {})
                origin = str(data.get("origin") or "user")
                template = str(data.get("template") or "")
                cls = strategy_registry.try_get_class(template)
                if cls is None:
                    raise ToolError("策略 %s 没有可读的源码（来源 %s，模板 %r 未注册）。" % (key, origin, template),
                                    hint="用户策略是「内置模板 + 参数」的 JSON 条目，没有独立代码；"
                                         "可用 strategy_get 看它的参数，或读内置模板的源码。",
                                    code="NO_SOURCE")
                note = ("用户策略 %s 本身没有源码（= 模板 %s + 参数），下面是模板类的源码。"
                        % (key, template))
            path = inspect_source_file(cls)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            raise ToolError("读取策略源码失败：%s" % exc,
                            hint="请确认文件存在且当前用户可读。", code="E_FILE") from exc

        lines_count = len(source.splitlines())
        rel = _text_path(ctx, path)
        header = "策略 %s（来源 %s）源码：%s（%d 行）" % (key, origin, rel, lines_count)
        if note:
            header = note + "\n" + header
        text = "%s\n%s\n%s" % (header, "-" * 68, source.rstrip("\n"))
        return text, {
            "strategy_id": key,
            "origin": origin,
            "template": template,
            "path": rel,
            "lines": lines_count,
            "bytes": byte_size(source),
            "source": source,
        }

    # ------------------------------------------------------------------ 只读：校验
    @registry.tool(
        "strategy_lint",
        "校验策略代码（静态规范 + 运行期元数据/参数 + 烟雾回测与无未来函数）。"
        "传 source 校验候选源码（临时文件，不落盘）；传 strategy_id 校验已有文件。"
        "返回 {ok, errors[], warnings[]}，每条含 code/message/line。",
        obj({
            "strategy_id": p_str("要校验的已有策略 id（与 source 二选一；local 文件或内置策略）"),
            "source": p_str("要校验的候选源码（给了就校验这段，不落盘）"),
            "smoke": p_bool("是否跑烟雾回测（真的用合成行情执行一次策略逻辑，默认 true；"
                            "false 只做静态 + 运行期检查，更快）", default=True),
        }),
        title="校验策略", group=GROUP, read_only=True, open_world=False, idempotent=True,
    )
    def strategy_lint(ctx, args):
        source = args.get("source")
        key = str(args.get("strategy_id") or "").strip()
        smoke = args.get("smoke")
        smoke = True if smoke is None else bool(smoke)

        if not isinstance(source, str) or not source.strip():
            if not key:
                raise ToolError("需要 strategy_id 或 source 二者之一。",
                                hint="校验已有策略传 strategy_id；校验还没落盘的新代码传 source。",
                                code="INVALID_ARGS")
            report, captured = _lint_existing(ctx, key, smoke)
            lines = ["策略校验：%s（%s；smoke=%s）—— %s"
                     % (key, _lint_target_text(ctx, key), "true" if smoke else "false", _report_state(report))]
            lines.extend(_report_lines(report, captured))
            lines.append("说明：errors 为 0 即 ok=true；warning 不影响落盘。")
            lines.append(_write_next_step(report))
            return "\n".join(lines), {"ok": bool(report.get("ok")),
                                      "errors": report.get("errors") or [],
                                      "warnings": report.get("warnings") or [],
                                      "path": _text_path(ctx, report.get("path")),
                                      "classes": report.get("classes") or [],
                                      "smoke": smoke,
                                      "stdout": captured}

        if key and not ID_RE.match(key):
            raise ToolError("strategy_id 非法：%r" % key, hint=_ID_HINT, code="INVALID_ID")
        try:
            check_source_size(source)
        except GuardError as exc:
            raise ToolError("源码过大：%s" % exc, hint="请只提交策略代码本身。",
                            code="SOURCE_TOO_LARGE") from exc

        slug = key[3:] if key else (_slug_from_source(source) or "strategy_preview")
        report, captured, degraded = _lint_candidate(slug, source, smoke)
        if report.get("classes"):
            issue = _conflict_issue("st_" + slug, slug)
            if issue is not None:
                report["errors"] = [issue] + list(report.get("errors") or [])
                report["ok"] = False

        target = _text_path(ctx, _guard_local_path(ctx, slug + ".py"))
        lines = ["源码校验：目标文件 %s（临时文件已删除，未落盘；smoke=%s）—— %s"
                 % (target, "true" if smoke else "false", _report_state(report))]
        if degraded:
            lines.append("  提示：包目录不可写，已改用系统临时目录校验；相对导入可能被判为 E_IMPORT。")
        lines.extend(_report_lines(report, captured))
        if report.get("classes"):
            lines.append("类：%s" % ", ".join(report.get("classes") or []))
        lines.append(_write_next_step(report))
        return "\n".join(lines), {"ok": bool(report.get("ok")),
                                  "errors": report.get("errors") or [],
                                  "warnings": report.get("warnings") or [],
                                  "path": target,
                                  "target_id": "st_" + slug,
                                  "classes": report.get("classes") or [],
                                  "smoke": smoke,
                                  "written": False,
                                  "stdout": captured}

    # ------------------------------------------------------------------ 写：落盘源码
    @registry.tool(
        "strategy_write_source",
        "把策略源码写入 strategies/local/<slug>.py 并立即热加载、跑校验（覆盖前自动备份）。"
        "校验有 error 也会照写（文件保留，方便继续迭代），返回值里给问题清单与下一步；"
        "只有 id 非法 / 路径越界 / 超过 128KB 才拒绝落盘。dry_run=true 只校验不写盘。",
        obj({
            "strategy_id": p_str("策略 id：st_ + 小写 snake_case（如 st_mcp_demo）；文件名 = 去掉 st_ 前缀",
                                 pattern="^st_[a-z0-9_]{2,36}$"),
            "source": p_str("完整策略源码（UTF-8，≤128KB；文件名 / id / 类名三者必须一致）"),
            "dry_run": p_bool("只校验不落盘（默认 false；不会写文件也不会产生备份）", default=False),
        }, required=["strategy_id", "source"]),
        title="写入策略源码", group=GROUP, read_only=False, destructive=False,
        idempotent=False, open_world=False,
    )
    def strategy_write_source(ctx, args):
        ctx.write_guard("strategy_write_source", args)
        key = str(args.get("strategy_id") or "").strip()
        source = args.get("source")
        dry_run = bool(args.get("dry_run") or False)

        if not isinstance(source, str) or not source.strip():
            ctx.record("strategy_write_source", args, ok=False, detail="拒绝：source 为空")
            raise ToolError("source 不能为空。", hint="请把完整策略源码作为 source 传入。", code="INVALID_ARGS")
        try:
            path = _strategy_path(ctx, key)
        except ToolError as exc:
            ctx.record("strategy_write_source", args, ok=False, detail="拒绝：%s" % exc.message)
            raise
        try:
            check_source_size(source)
        except GuardError as exc:
            ctx.record("strategy_write_source", args, ok=False, detail="拒绝：%s" % exc)
            raise ToolError("源码过大，拒绝落盘：%s" % exc,
                            hint="单个策略文件上限 128KB；请精简代码（别把数据或大段注释塞进来）。",
                            code="SOURCE_TOO_LARGE") from exc

        rel = _text_path(ctx, path)
        size = byte_size(source)
        lines = len(source.splitlines())
        existed = os.path.isfile(path)
        slug = key[3:]

        backup = ""
        loaded: List[str] = []
        if dry_run:
            report, captured, degraded = _lint_candidate(slug, source, True)
            if report.get("classes"):
                issue = _conflict_issue(key, slug)
                if issue is not None:
                    report["errors"] = [issue] + list(report.get("errors") or [])
                    report["ok"] = False
            text = ["仅校验（dry_run=true）：未写盘、未备份；目标文件 %s%s"
                    % (rel, "（已存在，覆盖前会先备份）" if existed else ""),
                    "校验：%s" % _report_state(report)]
            if degraded:
                text.append("  提示：包目录不可写，已改用系统临时目录校验；相对导入可能被判为 E_IMPORT。")
            text.extend(_report_lines(report, captured))
            next_step = ("下一步：确认无误后去掉 dry_run（或传 false）再调用一次即可落盘。"
                         if report.get("ok") else
                         "下一步：按上面的 code 修正后重新调用 strategy_write_source（可继续带 dry_run）。")
            text.append(next_step)
            ctx.record("strategy_write_source", args, ok=True, detail="dry_run 仅校验",
                       strategy_id=key, dry_run=True, bytes=size,
                       lint_ok=bool(report.get("ok")),
                       errors=len(report.get("errors") or []),
                       warnings=len(report.get("warnings") or []), backup="")
            return "\n".join(text), {
                "strategy_id": key, "path": rel, "written": False, "dry_run": True,
                "existed": existed, "bytes": size, "lines": lines, "backup": "",
                "ok": bool(report.get("ok")),
                "errors": report.get("errors") or [], "warnings": report.get("warnings") or [],
                "classes": report.get("classes") or [], "loaded": [], "stdout": captured,
                "next_step": next_step,
            }

        if existed:
            backup = backup_file(path, ctx.backup_dir) or ""
        atomic_write_text(path, source)
        _forget_local_module(slug)
        loaded = strategy_registry.discover_local(force=True)
        local = strategy_registry.local_status()
        report, captured = _lint_call(path, "local", True, registry=strategy_registry)

        text = ["已写入 %s（%s 字节 / %d 行；id=%s，来源 local）"
                % (rel, size, lines, key),
                "备份：%s" % (backup if backup else "（新文件，无需备份）"),
                "校验：%s" % _report_state(report)]
        text.extend(_report_lines(report, captured))
        if key in loaded:
            text.append("热加载：成功（strategy_list 现在能看到 %s）" % key)
        else:
            fails = [item for item in (local.get("errors") or []) if item.get("file", "").startswith(slug)]
            text.append("热加载：未生效——%s" % ("；".join("%s：%s" % (item.get("file"), item.get("error"))
                                                    for item in fails) or "discover_local 未返回该 id"))
        text.append(_write_next_step(report))
        next_step = text[-1]

        ctx.record("strategy_write_source", args, ok=True,
                   detail="写入 %s（lint %s）" % (rel, "通过" if report.get("ok") else "有 error"),
                   strategy_id=key, bytes=size, lines=lines, dry_run=False,
                   lint_ok=bool(report.get("ok")),
                   errors=len(report.get("errors") or []),
                   warnings=len(report.get("warnings") or []),
                   backup=backup, existed=existed, loaded=key in loaded)
        return "\n".join(text), {
            "strategy_id": key, "path": rel, "written": True, "dry_run": False,
            "existed": existed, "bytes": size, "lines": lines, "backup": backup,
            "ok": bool(report.get("ok")),
            "errors": report.get("errors") or [], "warnings": report.get("warnings") or [],
            "classes": report.get("classes") or [], "loaded": loaded, "stdout": captured,
            "next_step": next_step,
        }

    # ------------------------------------------------------------------ 写：删源码
    @registry.tool(
        "strategy_delete_source",
        "删除 strategies/local/<slug>.py（只删本地代码策略；删除前先备份到 <data>/strategy-backups/）。"
        "必须显式传 confirm=true，否则只报错不动作。",
        obj({
            "strategy_id": p_str("要删除的本地策略 id（st_ 开头；内置/用户策略不能用本工具删）"),
            "confirm": p_bool("确认删除：必须传 true；缺省或 false 一律拒绝"),
        }, required=["strategy_id"]),
        title="删除策略源码", group=GROUP, read_only=False, destructive=True,
        idempotent=False, open_world=False,
    )
    def strategy_delete_source(ctx, args):
        ctx.write_guard("strategy_delete_source", args)
        key = str(args.get("strategy_id") or "").strip()
        if not bool(args.get("confirm")):
            ctx.record("strategy_delete_source", args, ok=False, detail="拒绝：confirm 非 true")
            raise ToolError("删除本地策略文件是不可撤销的操作（会先备份一份）。",
                            hint="确认无误请传 confirm=true", code="CONFIRM_REQUIRED")
        try:
            path = _strategy_path(ctx, key)
        except ToolError as exc:
            ctx.record("strategy_delete_source", args, ok=False, detail="拒绝：%s" % exc.message)
            raise
        rel = _text_path(ctx, path)
        if not os.path.isfile(path):
            ctx.record("strategy_delete_source", args, ok=False, detail="文件不存在：%s" % rel)
            if strategy_registry.is_builtin(key):
                raise ToolError("%s 是内置策略，没有 local/ 源码文件，不能删除。" % key,
                                hint="内置策略不可删除；只想换参数请用 strategy_user_create。",
                                code="NOT_FOUND")
            raise ToolError("strategies/local 下不存在策略文件：%s" % rel,
                            hint=_LIST_HINT + " 本工具只删 local/ 下的代码策略。",
                            code="NOT_FOUND")

        backup = backup_file(path, ctx.backup_dir) or ""
        try:
            os.remove(path)
        except OSError as exc:
            ctx.record("strategy_delete_source", args, ok=False, detail="删除失败：%s" % exc)
            raise ToolError("删除策略文件失败：%s" % exc,
                            hint="请确认文件权限后重试（备份已保留，可手工恢复）。",
                            code="E_FILE") from exc
        _forget_local_strategy(key, key[3:])
        loaded = list(strategy_registry.LOCAL_IDS)
        text = ["已删除本地策略：%s（id=%s）" % (rel, key),
                "备份：%s" % (backup or "（未生成）"),
                "local/ 当前已加载：%s" % ("、".join(loaded) or "（无）"),
                "下一步：strategy_list 复查；若要恢复，把备份文件复制回 %s 并再写一次。" % rel]
        ctx.record("strategy_delete_source", args, ok=True, detail="删除 %s" % rel,
                   strategy_id=key, backup=backup, deleted=True)
        return "\n".join(text), {"strategy_id": key, "deleted": True, "file": rel,
                                 "backup": backup, "loaded": loaded}

    # ------------------------------------------------------------------ 写：用户策略
    @registry.tool(
        "strategy_user_create",
        "新建「用户策略」：内置模板 + 一组参数（JSON 条目，不是代码）。"
        "参数键必须是模板 param_schema 里的键，否则报 VALIDATION。",
        obj({
            "name": p_str("策略名称（2-24 字，不能与现有策略重名）"),
            "template": p_str("内置模板 id，例如 st_ma_cross / st_momentum（见 strategy_list）"),
            "params": p_object("模板参数覆盖，键取自模板 param_schema，如 {\"short_ma\": 10, \"long_ma\": 30}"),
            "category": p_str("类别，取自词表：趋势跟踪/动量/震荡市/稳健/均值回归/自定义"),
            "status": p_str("运行状态：running / paused", enum=["running", "paused"]),
            "freq": p_str("频率：日线 / 周度 / 月度 / 盘中"),
            "universe": p_str("标的池说明（人类可读文本）"),
            "desc": p_str("策略说明（≤200 字）"),
        }, required=["name", "template"]),
        title="新建用户策略", group=GROUP, read_only=False, destructive=False,
        idempotent=False, open_world=False,
    )
    def strategy_user_create(ctx, args):
        ctx.write_guard("strategy_user_create", args)
        payload: Dict[str, Any] = {}
        for field in ("name", "template", "params", "category", "status", "freq", "universe", "desc"):
            value = args.get(field)
            if value is None:
                continue
            if field == "params" and not isinstance(value, dict):
                ctx.record("strategy_user_create", args, ok=False, detail="拒绝：params 不是对象")
                raise ToolError("params 必须是 JSON 对象（键取自模板 param_schema）。",
                                hint="例如 {\"short_ma\": 10, \"long_ma\": 30}；不需要参数就不传。",
                                code="INVALID_ARGS")
            payload[field] = value

        created = dict(_service_call(ctx, ctx.strategies_service.create_strategy, payload) or {})
        new_id = str(created.get("id") or "")
        text = ["已新建用户策略：%s（id=%s，模板=%s）"
                % (created.get("name"), new_id, created.get("template") or payload.get("template")),
                "参数：%s" % (created.get("params") or {}),
                "存储：data/user_strategies.json（用户策略 = 模板 + 参数，改代码请用 strategy_write_source）",
                "下一步：backtest_run（strategy_id=%s）跑真实历史验证；"
                "strategy_get 复查参数 schema。" % new_id]
        ctx.record("strategy_user_create", args, ok=True, detail="新建用户策略 %s" % new_id,
                   strategy_id=new_id, template=created.get("template") or payload.get("template"))
        return "\n".join(text), {"strategy": created, "strategy_id": new_id}

    # ------------------------------------------------------------------ 写：删用户策略
    @registry.tool(
        "strategy_user_delete",
        "删除用户策略（data/user_strategies.json 里的条目；删除前备份该 JSON）。内置策略不可删。",
        obj({"strategy_id": p_str("用户策略 id（strategy_list 里来源为「用户」的 us_xxx）")},
            required=["strategy_id"]),
        title="删除用户策略", group=GROUP, read_only=False, destructive=True,
        idempotent=False, open_world=False,
    )
    def strategy_user_delete(ctx, args):
        ctx.write_guard("strategy_user_delete", args)
        key = str(args.get("strategy_id") or "").strip()
        path = str(getattr(ctx.settings, "user_strategies_path", "") or "")
        backup = backup_file(path, ctx.backup_dir) or "" if path else ""
        result = dict(_service_call(ctx, ctx.strategies_service.delete_strategy, key) or {})
        text = ["已删除用户策略：%s" % (result.get("id") or key),
                "备份：%s" % (backup or "（无 user_strategies.json，未生成）"),
                "下一步：strategy_list 复查（内置策略与 local/ 代码策略不受影响）。"]
        ctx.record("strategy_user_delete", args, ok=True, detail="删除用户策略 %s" % key,
                   strategy_id=key, backup=backup, deleted=bool(result.get("deleted")))
        return "\n".join(text), {"strategy_id": key, "deleted": bool(result.get("deleted")),
                                 "backup": backup}


# =========================================================================== 内部小工具
def inspect_source_file(cls: Any) -> str:
    """取策略类的源文件路径（内置/用户模板策略用）；取不到抛可读 :class:`ToolError`。"""
    import inspect

    try:
        path = inspect.getsourcefile(cls)
    except (TypeError, OSError) as exc:           # pragma: no cover - 极端情况
        path = None
        detail = "%s: %s" % (type(exc).__name__, exc)
    else:
        detail = ""
    if not path:
        raise ToolError("无法定位策略类 %s 的源文件%s。" % (getattr(cls, "__name__", cls),
                                                        ("（%s）" % detail) if detail else ""),
                        hint="请改用 strategy_list 确认 id，或让用户检查安装是否完整。",
                        code="NO_SOURCE")
    return path


def _lint_existing(ctx: Any, key: str, smoke: bool) -> Tuple[Dict[str, Any], str]:
    """校验已有策略：local 文件用 origin=local（含文件名↔id↔类名一致性），内置用 origin=builtin。"""
    local_path = _existing_local_path(ctx, key)
    if local_path:
        return _lint_call(local_path, "local", smoke, registry=strategy_registry)
    cls = strategy_registry.try_get_class(key)
    if cls is None:
        data = dict(_service_call(ctx, ctx.strategies_service.get_strategy, key) or {})
        raise ToolError("策略 %s 没有可校验的源码文件（来源 %s）。" % (key, data.get("origin") or "user"),
                        hint="用户策略是模板 + 参数：用 strategy_get 检查参数；改代码请用 strategy_write_source 写 local 策略。",
                        code="NO_SOURCE")
    path = inspect_source_file(cls)
    return _lint_call(path, "builtin", smoke, registry=strategy_registry)


def _lint_target_text(ctx: Any, key: str) -> str:
    local_path = _existing_local_path(ctx, key)
    if local_path:
        return _text_path(ctx, local_path)
    cls = strategy_registry.try_get_class(key)
    if cls is None:
        return "非代码策略"
    return "%s 的源文件" % getattr(cls, "__name__", key)


__all__ = ["register", "GROUP"]
