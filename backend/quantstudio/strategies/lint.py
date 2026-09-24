# -*- coding: utf-8 -*-
"""策略规范校验器（lint）：让「照着文档写出来的策略」可被机器验收。

命令行
------
```bash
python -m quantstudio.strategies.lint                    # 校验内置策略 + local/ 下全部本地策略
python -m quantstudio.strategies.lint --path local/x.py  # 校验单个文件（无需先注册）
python -m quantstudio.strategies.lint --json             # 机器可读结果（CI / AI 自检）
python -m quantstudio.strategies.lint --no-smoke         # 跳过烟雾回测（更快）
```

三层校验
--------
1. **源码层（AST）**：编码/头注释、Python 3.9 语法、文件↔id↔类名一致、模块 docstring、
   必填元数据、禁用 import 与禁用调用（网络/文件/随机/时间/输出）、``on_bar`` 签名；
2. **运行期层**：``meta()`` 可序列化、类别/频率/标的池类型在词表内、``param_schema`` 与
   ``default_params`` 完全一致、非法参数必须抛 ``ValidationError``、``min_bars`` 足够；
3. **行为层（烟雾回测）**：用确定性合成行情走一遍 ``on_bar``（空仓/持仓两种场景），
   并做**无未来函数校验**——把第 N 根之后的行情放大 10 倍，前 N 根的委托必须完全一致。

退出码：0 = 无 error（warning 不影响）；1 = 存在 error；2 = 用法错误。
"""

import argparse
import ast
import importlib
import importlib.util
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from ..core.errors import ValidationError
from ..core.models import Bar, OrderRequest, Position
from .. import console

# --------------------------------------------------------------------------- 规范常量（与 docs/strategy-spec.md 保持一致）
ID_RE = re.compile(r"^st_[a-z0-9_]{2,36}$")
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
CATEGORY_CHOICES = ["趋势跟踪", "动量", "震荡市", "稳健", "均值回归", "统计套利", "事件驱动", "自定义"]
FREQ_CHOICES = ["日线", "周度", "月度", "盘中"]
UNIVERSE_TYPES = ["single", "multi", "index"]
PARAM_TYPES = ["int", "float", "bool", "str"]
REQUIRED_CLASS_ATTRS = [
    "id", "name", "category", "desc", "universe", "universe_type",
    "freq", "min_bars", "version", "default_params",
]
FORBIDDEN_MODULES = {
    "requests", "urllib", "urllib2", "urllib3", "http", "httpx", "aiohttp", "socket", "ssl",
    "subprocess", "multiprocessing", "threading", "asyncio", "concurrent", "signal",
    "os", "sys", "shutil", "pathlib", "tempfile", "glob", "io", "pickle", "shelve", "sqlite3",
    "ctypes", "platform", "getpass", "webbrowser", "logging",
}
WARN_MODULES = {"time", "datetime", "random", "secrets", "csv", "json", "re", "uuid", "statistics"}
ALLOWED_MODULES = {
    "math", "typing", "dataclasses", "collections", "itertools", "functools", "decimal",
    "fractions", "enum", "abc", "copy", "operator", "bisect", "heapq", "string", "textwrap",
}
FORBIDDEN_CALLS = {"print", "open", "eval", "exec", "compile", "__import__", "input", "exit", "quit", "breakpoint"}
FORBIDDEN_ATTRS = {"time", "now", "today", "utcnow", "system", "popen", "environ", "getenv"}

# 静态推断「指标窗口」用的参数名特征（用于校验 min_bars 是否足够）
WINDOW_HINTS = ("ma", "days", "period", "lookback", "window", "n")


class Issue:
    """一条校验结果。level: error / warning。"""

    __slots__ = ("level", "code", "message", "line")

    def __init__(self, level: str, code: str, message: str, line: int = 0):
        self.level = level
        self.code = code
        self.message = message
        self.line = int(line or 0)

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "line": self.line}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Issue(%s, %s, %s, line=%s)" % (self.level, self.code, self.message, self.line)


def _err(code: str, message: str, line: int = 0) -> Issue:
    return Issue("error", code, message, line)


def _warn(code: str, message: str, line: int = 0) -> Issue:
    return Issue("warning", code, message, line)


# --------------------------------------------------------------------------- 1) 源码层（AST）
def _class_attr_literal(node: ast.ClassDef, name: str) -> Optional[ast.AST]:
    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return item.value
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == name:
            return item.value
    return None


def _method(node: ast.ClassDef, name: str) -> Optional[ast.FunctionDef]:
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == name:
            return item
    return None


def lint_source(path: str, source: Optional[str] = None, origin: str = "local") -> Tuple[List[Issue], Optional[ast.Module]]:
    """对源码做静态规范检查；返回 (问题列表, AST)。"""
    issues: List[Issue] = []
    filename = os.path.basename(path)
    if source is None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            return [_err("E_FILE", "无法读取文件：%s" % exc)], None
        except UnicodeDecodeError as exc:
            return [_err("E_ENCODING", "文件必须是 UTF-8 编码：%s" % exc)], None

    lines = source.splitlines()
    if lines and lines[0].strip() != "# -*- coding: utf-8 -*-":
        issues.append(_warn("W_HEADER", "建议首行为 `# -*- coding: utf-8 -*-`（仓库统一约定）", 1))
    try:
        tree = ast.parse(source, filename=path, feature_version=(3, 9))
    except SyntaxError as exc:
        return issues + [_err("E_SYNTAX", "Python 语法错误（需兼容 3.9）：%s" % exc.msg, exc.lineno or 0)], None

    module_doc = ast.get_docstring(tree) or ""
    if not module_doc.strip():
        issues.append(_err("E_DOCSTRING", "缺少模块 docstring（需写明「信号逻辑/参数/风险与假设」）", 1))
    elif "信号逻辑" not in module_doc:
        issues.append(_warn("W_DOCSTRING", "模块 docstring 建议包含「信号逻辑」小节（便于他人与 AI 理解）", 1))

    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if not classes:
        issues.append(_err("E_CLASS", "未定义策略类（需继承 BaseStrategy）", 1))
        return issues, tree
    if len(classes) > 1:
        issues.append(_warn("W_MULTI_CLASS", "文件内有多个类定义（%s），只有本模块定义的 BaseStrategy 子类会被注册"
                            % ", ".join(cls.name for cls in classes)))

    strategy_classes = [cls for cls in classes if any(
        "BaseStrategy" in (ast.unparse(base) if hasattr(ast, "unparse") else "") for base in cls.bases
    )]
    if not strategy_classes:
        issues.append(_err("E_BASE", "没有类继承 BaseStrategy", classes[0].lineno))
        return issues, tree

    expected_class = "".join(part.capitalize() for part in filename[:-3].split("_")) + "Strategy"
    expected_id = "st_" + filename[:-3].lower()

    for cls in strategy_classes:
        line = cls.lineno
        for attr in REQUIRED_CLASS_ATTRS:
            if _class_attr_literal(cls, attr) is None:
                issues.append(_err("E_META_MISSING", "类 %s 缺少类属性 %s" % (cls.name, attr), line))

        id_node = _class_attr_literal(cls, "id")
        strategy_id = None
        if isinstance(id_node, ast.Constant) and isinstance(id_node.value, str):
            strategy_id = id_node.value
            if not ID_RE.match(strategy_id):
                issues.append(_err("E_ID_FORMAT",
                                   "id=%r 不符合命名规范：必须为 st_ + 小写 snake_case（字母/数字/下划线，3-36 字符）"
                                   % strategy_id, id_node.lineno))
            if origin == "local" and strategy_id != expected_id:
                issues.append(_err("E_ID_FILE_MISMATCH",
                                   "id 必须与文件名一致：文件 %s → id 应为 %r，当前 %r"
                                   % (filename, expected_id, strategy_id), id_node.lineno))
        elif id_node is not None:
            issues.append(_err("E_ID_FORMAT", "id 必须是字符串字面量（便于静态校验与前端展示）", id_node.lineno))

        if origin == "local" and cls.name != expected_class:
            issues.append(_err("E_CLASS_NAME",
                               "类名必须由文件名转 PascalCase 并加 Strategy 后缀：应为 %s，当前 %s"
                               % (expected_class, cls.name), line))

        schema = _method(cls, "param_schema")
        if schema is None:
            issues.append(_err("E_SCHEMA", "类 %s 必须实现 param_schema()" % cls.name, line))
        on_bar = _method(cls, "on_bar")
        if on_bar is None:
            issues.append(_err("E_ON_BAR", "类 %s 必须实现 on_bar(self, ctx, bars)" % cls.name, line))
        elif not on_bar.args.args or len(on_bar.args.args) != 3:
            issues.append(_err("E_ON_BAR_SIG", "on_bar 签名必须为 (self, ctx, bars)", on_bar.lineno))

        if _class_attr_literal(cls, "default_symbols") is None:
            issues.append(_warn("W_DEFAULT_SYMBOLS", "建议提供 default_symbols（未选标的时的兜底池）", line))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                issues.extend(_module_issue(root, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level == 0:
                issues.extend(_module_issue(root, node.lineno))
            elif origin == "local" and node.level == 2 and root in ("core", "config", "compat"):
                issues.append(_err(
                    "E_RELATIVE_IMPORT",
                    "相对导入层级不对：local/ 下的策略访问 quantstudio.%s 需要三级（`from ...%s... import ...`）"
                    % (root, root), node.lineno))
        elif isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            if name in FORBIDDEN_CALLS:
                issues.append(_err("E_FORBIDDEN_CALL",
                                   "禁止调用 %s()（策略必须是纯计算；日志请用 ctx.log）" % name, node.lineno))
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS and isinstance(node.value, ast.Name) and node.value.id in ("time", "datetime", "os", "random"):
                issues.append(_err("E_FORBIDDEN_ATTR",
                                   "禁止访问 %s.%s（破坏可复现性；请让引擎提供数据）" % (node.value.id, node.attr),
                                   node.lineno))
            elif node.attr.startswith("_") and isinstance(node.value, ast.Name) and node.value.id in ("ctx", "context"):
                issues.append(_err(
                    "E_PRIVATE_ACCESS",
                    "禁止访问 ctx.%s（上下文只暴露 history/bars_since/position(s)/cash/total_assets/price/bar/log；"
                    "访问内部字段等同于偷看未来数据）" % node.attr, node.lineno))
    return issues, tree


def _module_issue(root: str, line: int) -> List[Issue]:
    if not root:
        return []
    if root in FORBIDDEN_MODULES:
        return [_err("E_IMPORT", "禁止导入 %s（策略必须只依赖标准库计算能力与 quantstudio 内部模块）" % root, line)]
    if root in WARN_MODULES:
        return [_warn("W_IMPORT", "不建议导入 %s（可能引入随机性/时间依赖；若确有必要请在 docstring 说明）" % root, line)]
    if root in ALLOWED_MODULES or root == "quantstudio":
        return []
    return [_warn("W_IMPORT_UNKNOWN", "非白名单模块 %s：请确认它是标准库且无副作用" % root, line)]


# --------------------------------------------------------------------------- 2) 运行期层
def lint_class(cls, origin: str = "local") -> List[Issue]:
    """对已导入的策略类做运行期校验（元数据 / 参数 schema / 参数校验行为 / min_bars）。"""
    issues: List[Issue] = []
    try:
        spec = cls.meta()
    except Exception as exc:  # noqa: BLE001
        return [_err("E_META", "meta() 调用失败：%s: %s" % (type(exc).__name__, exc))]
    try:
        json.dumps(spec.to_dict(), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        issues.append(_err("E_META_JSON", "meta() 无法安全序列化为 JSON（禁止 NaN/Infinity）：%s" % exc))

    if not ID_RE.match(spec.id or ""):
        issues.append(_err("E_ID_FORMAT", "id=%r 不符合命名规范" % spec.id))
    if not 4 <= len(spec.name or "") <= 20:
        issues.append(_err("E_NAME_LEN", "name 长度需为 4-20 字（当前 %d 字）：%r" % (len(spec.name or ""), spec.name)))
    if not (spec.desc or "").strip():
        issues.append(_err("E_DESC", "desc 不能为空"))
    elif len(spec.desc) > 120:
        issues.append(_err("E_DESC_LEN", "desc 需 ≤ 120 字（当前 %d 字）" % len(spec.desc)))
    if spec.category not in CATEGORY_CHOICES:
        issues.append(_err("E_CATEGORY", "category=%r 不在词表内：%s" % (spec.category, "/".join(CATEGORY_CHOICES))))
    if spec.freq not in FREQ_CHOICES:
        issues.append(_err("E_FREQ", "freq=%r 不在词表内：%s" % (spec.freq, "/".join(FREQ_CHOICES))))
    if spec.universe_type not in UNIVERSE_TYPES:
        issues.append(_err("E_UNIVERSE_TYPE", "universe_type=%r 必须为 %s" % (spec.universe_type, "/".join(UNIVERSE_TYPES))))
    if not VERSION_RE.match(str(spec.version or "")):
        issues.append(_err("E_VERSION", "version=%r 需形如 1.0 或 1.0.0" % spec.version))

    schema = spec.param_schema or {}
    # 注意：spec.params 是 validate_params({}) 的结果（由 schema 的 default 填充），
    # 因此「schema 与默认值是否一致」必须比对类属性 default_params，否则永远相等、校验形同虚设。
    defaults = dict(getattr(cls, "default_params", None) or {})
    if set(schema.keys()) != set(defaults.keys()):
        only_schema = sorted(set(schema) - set(defaults))
        only_defaults = sorted(set(defaults) - set(schema))
        issues.append(_err("E_SCHEMA_KEYS",
                           "param_schema 与 default_params 的键必须完全一致（仅在 schema：%s；仅在 defaults：%s）"
                           % (", ".join(only_schema) or "-", ", ".join(only_defaults) or "-")))
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            issues.append(_err("E_SCHEMA_TYPE", "参数 %s 的 schema 必须是 dict" % key))
            continue
        for field in ("label", "type", "default", "help"):
            if field not in meta:
                issues.append(_err("E_SCHEMA_FIELD", "参数 %s 缺少 schema 字段 %s" % (key, field)))
        ptype = str(meta.get("type") or "").lower()
        if ptype not in PARAM_TYPES:
            issues.append(_err("E_SCHEMA_PTYPE", "参数 %s 的 type=%r 必须是 %s" % (key, meta.get("type"), "/".join(PARAM_TYPES))))
            continue
        if ptype in ("int", "float"):
            low, high = meta.get("min"), meta.get("max")
            if low is None or high is None:
                issues.append(_err("E_SCHEMA_RANGE", "数字参数 %s 必须声明 min 与 max" % key))
            elif not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or low >= high:
                issues.append(_err("E_SCHEMA_RANGE", "参数 %s 的 min(%r) 必须小于 max(%r)" % (key, low, high)))
            default = meta.get("default")
            if not isinstance(default, (int, float)) or isinstance(default, bool):
                issues.append(_err("E_SCHEMA_DEFAULT", "参数 %s 的 default 必须是数字" % key))
            elif isinstance(low, (int, float)) and isinstance(high, (int, float)) and not (low <= default <= high):
                issues.append(_err("E_SCHEMA_DEFAULT", "参数 %s 的 default=%r 不在 [%r, %r] 内" % (key, default, low, high)))
            step = meta.get("step")
            if step is not None and (not isinstance(step, (int, float)) or step <= 0):
                issues.append(_err("E_SCHEMA_STEP", "参数 %s 的 step 必须为正数" % key))
            if ptype == "int" and isinstance(default, float) and default != int(default):
                issues.append(_err("E_SCHEMA_DEFAULT", "参数 %s 声明为 int，default 却带小数" % key))
        if ptype == "str" and not meta.get("choices"):
            issues.append(_err("E_SCHEMA_CHOICES", "字符串参数 %s 必须提供 choices 白名单" % key))
        if key not in defaults:
            continue
        if meta.get("default") != defaults.get(key):
            issues.append(_err("E_SCHEMA_DEFAULT_MISMATCH",
                               "参数 %s 的 schema.default=%r 与 default_params 中的 %r 不一致"
                               % (key, meta.get("default"), defaults.get(key))))

    try:
        validated = cls.validate_params({})
    except Exception as exc:  # noqa: BLE001
        issues.append(_err("E_VALIDATE", "validate_params({}) 失败：%s: %s" % (type(exc).__name__, exc)))
        validated = {}
    if validated and defaults and validated != defaults:
        issues.append(_warn("W_DEFAULTS", "validate_params({}) 结果与 default_params 不完全一致：%r vs %r"
                            % (validated, defaults)))
    if not defaults:
        issues.append(_err("E_DEFAULT_PARAMS", "default_params 不能为空（参数默认值要在这里声明）"))

    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        for bad, label in (
            ({"__不存在的参数__": 1}, "未知参数"),
            ({key: (meta.get("min") - 1) if isinstance(meta.get("min"), (int, float)) and not isinstance(meta.get("min"), bool) else None},
             "低于 min"),
            ({key: (meta.get("max") + 1) if isinstance(meta.get("max"), (int, float)) and not isinstance(meta.get("max"), bool) else None},
             "高于 max"),
        ):
            if list(bad.values())[0] is None:
                continue
            try:
                cls.validate_params(dict(bad))
            except ValidationError:
                continue
            except Exception as exc:  # noqa: BLE001
                issues.append(_warn("W_VALIDATE_EXC", "参数 %s 的%s用例抛出了非 ValidationError 异常：%s" % (key, label, exc)))
                continue
            issues.append(_err("E_VALIDATE_MISS", "参数校验未拦截「%s」用例：%r 应被拒绝" % (label, bad)))

    window = _longest_window(defaults, schema)
    if int(spec.min_bars) < window + 1:
        issues.append(_err("E_MIN_BARS", "min_bars=%s 必须 ≥ 最长指标窗口 + 1（推断窗口 %s → 至少 %s）"
                           % (spec.min_bars, window, window + 1)))
    elif int(spec.min_bars) < 60:
        issues.append(_warn("W_MIN_BARS", "min_bars=%s 偏小：回测引擎至少预热 60 根，建议 ≥ 60" % spec.min_bars))
    return issues


def _longest_window(defaults: Dict[str, Any], schema: Dict[str, Any]) -> int:
    """从参数默认值里推断「最长指标窗口」（用于 min_bars 校验）。"""
    longest = 0
    for key, value in (defaults or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        meta = schema.get(key) or {}
        if str(meta.get("type") or "").lower() not in ("int", "float"):
            continue
        lowered = key.lower()
        if any(hint in lowered for hint in WINDOW_HINTS) or lowered in ("top_n", "levels"):
            longest = max(longest, int(value))
    return longest


# --------------------------------------------------------------------------- 3) 行为层（烟雾回测）
class _FakeContext:
    """最小可用的 StrategyContext 实现：只暴露「截至今日」的数据，用于烟雾回测与无未来函数校验。"""

    def __init__(self, series: Dict[str, List[Bar]], index: int, cash: float = 1_000_000.0,
                 positions: Optional[Dict[str, Position]] = None):
        self._series = series
        self._i = index
        self._cash = cash
        self._positions = dict(positions or {})
        self.logs: List[str] = []
        first = next(iter(series.values()))
        self.today = first[index].date

    def history(self, symbol: str, field: str = "close", n: int = 60) -> List[float]:
        bars = self._series.get(symbol) or []
        end = self._i + 1
        n = max(1, int(n))
        window = bars[max(0, end - n):end]
        out: List[float] = []
        for bar in window:
            value = getattr(bar, field, None)
            out.append(float(value) if isinstance(value, (int, float)) else 0.0)
        return out

    def bars_since(self, symbol: str, n: int) -> List[Bar]:
        bars = self._series.get(symbol) or []
        end = self._i + 1
        return bars[max(0, end - max(1, int(n))):end]

    def position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def positions(self) -> List[Position]:
        return list(self._positions.values())

    def cash(self) -> float:
        return self._cash

    def total_assets(self) -> float:
        return self._cash + sum(float(p.market_value or 0.0) for p in self._positions.values())

    def price(self, symbol: str) -> float:
        bars = self._series.get(symbol) or []
        return float(bars[self._i].close) if bars else 0.0

    def bar(self, symbol: str) -> Optional[Bar]:
        bars = self._series.get(symbol) or []
        return bars[self._i] if bars else None

    def log(self, message: str) -> None:
        self.logs.append(str(message))


def _synthetic_series(codes: List[str], bars_count: int = 260, scale_after: Optional[int] = None,
                      factor: float = 10.0) -> Dict[str, List[Bar]]:
    """确定性合成行情（无随机数）：趋势 + 周期波动，便于复现与「放大未来」对比。"""
    out: Dict[str, List[Bar]] = {}
    for offset, code in enumerate(codes):
        bars: List[Bar] = []
        for i in range(bars_count):
            base = 100.0 * (1.0 + 0.35 * math.sin((i + offset * 7) / 17.0) + 0.0009 * i)
            if scale_after is not None and i >= scale_after:
                base *= factor
            open_ = base * (1 + 0.002 * math.sin(i / 5.0))
            high = max(open_, base) * 1.006
            low = min(open_, base) * 0.994
            bars.append(Bar(date="2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                            open=round(open_, 2), high=round(high, 2), low=round(low, 2),
                            close=round(base, 2), volume_wan=1000.0 + i, amount_yi=1.0 + i / 100.0))
        out[code] = bars
    return out


def smoke_run(cls, bars_count: int = 260, warmup: int = 61) -> List[Issue]:
    """用合成行情走一遍 on_bar：查异常、查返回类型、查无未来函数。"""
    issues: List[Issue] = []
    codes = _codes_for(cls)
    series = _synthetic_series(codes, bars_count=bars_count)

    for scenario, with_position in (("空仓", False), ("持仓", True)):
        try:
            strategy = cls()
            positions = {}
            if with_position:
                first = codes[0]
                price = series[first][warmup].close
                positions[first] = Position(code=first, name=first, qty=100, available_qty=100,
                                            cost=round(price * 0.9, 2), price=price)
            collected = _walk(strategy, series, codes, warmup, positions)
        except Exception as exc:  # noqa: BLE001
            issues.append(_err("E_SMOKE", "烟雾回测（%s）抛出异常：%s: %s" % (scenario, type(exc).__name__, exc)))
            continue
        for i, orders in collected:
            if not isinstance(orders, list):
                issues.append(_err("E_ON_BAR_RETURN", "on_bar 必须返回 List[OrderRequest]，第 %d 根返回了 %s"
                                   % (i, type(orders).__name__)))
                break
            for order in orders:
                if not isinstance(order, OrderRequest):
                    issues.append(_err("E_ON_BAR_RETURN", "on_bar 返回了非 OrderRequest 对象：%r（请用 buy_order/sell_order 构造）"
                                       % (order,)))
                    break

    cutoff = warmup + 60
    try:
        plain = _walk(cls(), _synthetic_series(codes, bars_count=bars_count), codes, warmup)
        scaled = _walk(cls(), _synthetic_series(codes, bars_count=bars_count, scale_after=cutoff), codes, warmup)
        if _prefix_signature(plain, cutoff) != _prefix_signature(scaled, cutoff):
            issues.append(_err("E_LOOKAHEAD",
                               "疑似使用了未来函数：把第 %d 根之后的行情放大 10 倍后，之前的委托发生了变化"
                               "（只能用 ctx.history/bars_since 取「截至今日」的数据）" % cutoff))
    except Exception as exc:  # noqa: BLE001
        issues.append(_warn("W_LOOKAHEAD_SKIP", "无未来函数校验未执行：%s: %s" % (type(exc).__name__, exc)))
    return issues


def _codes_for(cls) -> List[str]:
    codes = [str(code) for code in (getattr(cls, "default_symbols", None) or []) if str(code).strip()]
    return codes[:3] or ["600519.SH"]


def _walk(strategy, series: Dict[str, List[Bar]], codes: List[str], warmup: int,
          positions: Optional[Dict[str, Position]] = None) -> List[Tuple[int, List[OrderRequest]]]:
    """逐 bar 调用策略，返回 [(bar_index, orders)]。"""
    out: List[Tuple[int, List[OrderRequest]]] = []
    length = min(len(series[code]) for code in codes)
    try:
        strategy.on_start(_FakeContext(series, warmup, positions=positions))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("on_start 失败: %s" % exc)
    for i in range(warmup, length):
        ctx = _FakeContext(series, i, positions=positions)
        bars = {code: series[code][i] for code in codes}
        orders = strategy.on_bar(ctx, bars)
        out.append((i, orders if isinstance(orders, list) else []))
    return out


def _prefix_signature(walked: List[Tuple[int, List[OrderRequest]]], cutoff: int) -> List[Tuple[int, str, str, int]]:
    signature: List[Tuple[int, str, str, int]] = []
    for index, orders in walked:
        if index >= cutoff:
            break
        for order in orders:
            signature.append((index, str(getattr(order, "code", "")), str(getattr(order, "side", "")),
                              int(getattr(order, "qty", 0) or 0)))
    return signature


# --------------------------------------------------------------------------- 组合校验与 CLI
def lint_path(path: str, origin: str = "local", smoke: bool = True, registry: Any = None) -> Dict[str, Any]:
    """校验单个策略文件（静态 + 导入 + 运行期 + 烟雾回测）。"""
    issues, _tree = lint_source(path, origin=origin)
    if any(issue.level == "error" and issue.code in ("E_SYNTAX", "E_FILE", "E_ENCODING") for issue in issues):
        return _report(path, issues, None)
    module = _load_module(path, refresh=(origin == "local"))
    if module is None:
        issues.append(_err("E_IMPORT", "无法导入该模块（依赖/包路径错误？）"))
        return _report(path, issues, None)
    classes = [value for value in vars(module).values()
               if isinstance(value, type) and getattr(value, "__module__", "") == module.__name__
               and getattr(value, "id", "") and hasattr(value, "on_bar")]
    if not classes:
        issues.append(_err("E_CLASS", "模块内没有任何 BaseStrategy 子类（本模块定义的类）"))
        return _report(path, issues, None)
    for cls in classes:
        issues.extend(lint_class(cls, origin=origin))
        if registry is not None:
            strategy_id = str(getattr(cls, "id", ""))
            exist = registry.REGISTRY.get(strategy_id)
            if exist is not None and exist is not cls:
                same_definition = (getattr(exist, "__module__", "") == getattr(cls, "__module__", "")
                                   and exist.__name__ == cls.__name__)
                if same_definition:
                    # 同一文件被重新加载（本地策略热重载）：把注册表指向新类对象，避免陈旧引用
                    registry.REGISTRY[strategy_id] = cls
                else:
                    issues.append(_err("E_ID_CONFLICT",
                                       "策略 id %s 已被 %s 占用（换个 id 或文件名）" % (strategy_id, exist.__module__)))
        if smoke:
            issues.extend(smoke_run(cls))
    return _report(path, issues, [cls.__name__ for cls in classes])


_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))            # .../quantstudio/strategies
_PACKAGE_ROOT = os.path.dirname(_PACKAGE_DIR)                        # .../quantstudio


def _package_module_name(path: str) -> Optional[str]:
    """把包内文件路径映射为可导入的模块名（策略文件使用相对导入，必须按包导入）。"""
    root = os.path.dirname(_PACKAGE_ROOT)                            # .../backend
    rel = os.path.relpath(os.path.abspath(path), root)
    if rel.startswith("..") or not rel.endswith(".py"):
        return None
    parts = rel[:-3].split(os.sep)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or parts[0] != "quantstudio":
        return None
    return ".".join(parts)


def _load_module(path: str, refresh: bool = False):
    """优先按包导入（支持相对导入）；包外文件退化为按路径加载。

    ``refresh=True`` 时会重新执行模块（本地策略改完立即校验）；内置模块保持缓存，
    避免重复注册出「同 id 不同类对象」的假冲突。
    """
    name = _package_module_name(path)
    if name:
        try:
            if refresh and name in sys.modules:
                return importlib.reload(sys.modules[name])
            return importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 导入失败在调用处统一报告
            return None
    fallback = "quantstudio_strategy_lint_" + re.sub(r"\W+", "_", os.path.basename(path))
    try:
        spec = importlib.util.spec_from_file_location(fallback, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[fallback] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        sys.modules.pop(fallback, None)
        return None


def _report(path: str, issues: List[Issue], classes: Optional[List[str]]) -> Dict[str, Any]:
    return {
        "path": path,
        "classes": classes or [],
        "errors": [issue.to_dict() for issue in issues if issue.level == "error"],
        "warnings": [issue.to_dict() for issue in issues if issue.level == "warning"],
        "ok": not any(issue.level == "error" for issue in issues),
    }


def lint_all(smoke: bool = True) -> Dict[str, Any]:
    """校验内置策略 + ``local/`` 下全部本地策略；返回汇总报告。"""
    from . import registry as registry_module

    registry_module.discover_local(force=True)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    targets: List[Tuple[str, str]] = []
    for filename in sorted(os.listdir(package_dir)):
        if filename.endswith(".py") and filename not in (
                "__init__.py", "base.py", "registry.py", "lint.py", "user.py"):
            targets.append((os.path.join(package_dir, filename), "builtin"))
    local_dir = registry_module.LOCAL_DIR
    for filename in sorted(os.listdir(local_dir)) if os.path.isdir(local_dir) else []:
        if filename.endswith(".py") and not filename.startswith("_") and filename != "__init__.py":
            targets.append((os.path.join(local_dir, filename), "local"))

    reports = [lint_path(path, origin=origin, smoke=smoke, registry=registry_module) for path, origin in targets]
    errors = sum(len(report["errors"]) for report in reports)
    warnings = sum(len(report["warnings"]) for report in reports)
    return {
        "reports": reports,
        "errors": errors,
        "warnings": warnings,
        "ok": errors == 0,
        "local": registry_module.local_status(),
    }


def _print_text(report: Dict[str, Any]) -> None:
    for item in report.get("reports", [report]):
        mark = "OK  " if item["ok"] else "FAIL"
        rel = os.path.relpath(item["path"], os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        print("%s %s%s" % (mark, rel, ("  [%s]" % ", ".join(item["classes"])) if item.get("classes") else ""))
        for issue in item.get("errors", []):
            print("   ✗ %-18s %s%s" % (issue["code"], issue["message"], " (line %s)" % issue["line"] if issue["line"] else ""))
        for issue in item.get("warnings", []):
            print("   ! %-18s %s%s" % (issue["code"], issue["message"], " (line %s)" % issue["line"] if issue["line"] else ""))
    local = report.get("local") or {}
    if local:
        print("local/ 目录：已加载 %s%s" % (", ".join(local.get("loaded") or []) or "（无）",
                                        "；失败 %d 个" % len(local.get("errors") or []) if local.get("errors") else ""))
        for item in local.get("errors") or []:
            print("   ✗ %s：%s" % (item.get("file"), item.get("error")))
    if "errors" in report:
        print("\n总计：%d 个 error，%d 个 warning" % (report["errors"], report["warnings"]))


def main(argv: Optional[List[str]] = None) -> int:
    console.force_utf8_output()             # Windows 控制台非 UTF-8 时，打印中文不再崩
    parser = argparse.ArgumentParser(prog="python -m quantstudio.strategies.lint",
                                     description="策略规范校验（源码 + 运行期 + 烟雾回测）")
    parser.add_argument("--path", action="append", default=[], help="校验单个策略文件（可多次指定）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI / AI 自检用）")
    parser.add_argument("--no-smoke", action="store_true", help="跳过烟雾回测与无未来函数校验（更快）")
    parser.add_argument("--all", action="store_true", help="校验全部（默认行为）")
    args = parser.parse_args(argv)

    smoke = not args.no_smoke
    if args.path:
        from . import registry as registry_module
        reports = []
        for path in args.path:
            origin = "local" if os.path.basename(os.path.dirname(os.path.abspath(path))) == "local" else "builtin"
            reports.append(lint_path(os.path.abspath(path), origin=origin, smoke=smoke, registry=registry_module))
        result = {
            "reports": reports,
            "errors": sum(len(report["errors"]) for report in reports),
            "warnings": sum(len(report["warnings"]) for report in reports),
            "ok": all(report["ok"] for report in reports),
        }
    else:
        result = lint_all(smoke=smoke)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
