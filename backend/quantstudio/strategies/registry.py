# -*- coding: utf-8 -*-
"""策略注册表：内置策略 + 用户自定义策略（参数覆盖内置模板）。

用法
----
```python
from quantstudio.strategies import list_specs, create

for spec in list_specs():          # -> List[StrategySpec]（确定性顺序：内置在前）
    print(spec.id, spec.name, spec.params)

strategy = create("st_ma_cross", params={"short_ma": 10}, symbols=["600519.SH"])
result = BacktestEngine(provider).run(request, strategy)
```

用户自定义策略
--------------
用户策略不新增代码，只是「内置模板 + 一组参数」。调用方从 ``user.py`` 读取条目后，可用
:func:`create` 传入 ``spec=用户 StrategySpec``（见 ``user.create_from_user``）：
策略实例按 ``spec.params`` 覆盖模板默认参数，最终 ``spec`` 会带上用户策略的 id / name / 分类等元信息。
"""

import importlib
import importlib.util
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Type

from ..core.errors import ValidationError
from ..core.models import StrategySpec
from .base import BaseStrategy
from .grid import GridStrategy
from .low_vol import LowVolStrategy
from .ma_cross import MaCrossStrategy
from .momentum import MomentumStrategy
from .rsi_reversion import RsiReversionStrategy
from .turtle import TurtleStrategy

__all__ = [
    "REGISTRY", "BUILTIN_ORDER", "LOCAL_IDS", "LOCAL_DIR", "register", "register_builtin",
    "list_specs", "list_specs_ids", "get_class", "try_get_class", "create", "create_from_spec",
    "describe", "is_builtin", "is_local", "discover_local", "local_status", "spec_from_dict",
    "with_origin",
]

# id -> 策略类（内置策略在导入本模块时注册）
REGISTRY: Dict[str, Type[BaseStrategy]] = {}

# 内置策略展示顺序（前端策略列表按此排序）
BUILTIN_ORDER: List[str] = [
    "st_ma_cross", "st_momentum", "st_grid", "st_lowvol", "st_rsi", "st_turtle",
]


def register(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
    """注册策略类（可用作装饰器）；同一 id 重复注册为不同类会抛 ValidationError。"""
    if not isinstance(cls, type) or not issubclass(cls, BaseStrategy):
        raise ValidationError("register 只接受 BaseStrategy 的子类，当前为 %r" % (cls,))
    strategy_id = str(getattr(cls, "id", "") or "").strip()
    if not strategy_id:
        raise ValidationError("策略类 %s 未定义 id，无法注册" % cls.__name__)
    if not str(getattr(cls, "name", "") or "").strip():
        raise ValidationError("策略类 %s 未定义 name，无法注册" % cls.__name__)
    exist = REGISTRY.get(strategy_id)
    if exist is not None and exist is not cls:
        raise ValidationError("策略 id 冲突：%s 已由 %s 注册" % (strategy_id, exist.__name__))
    cls.validate_params({})          # 默认参数必须自洽，否则拒绝注册
    REGISTRY[strategy_id] = cls
    return cls


def register_builtin(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
    """内置策略注册（保留钩子，便于测试中替换）。"""
    registered = register(cls)
    if registered.id not in BUILTIN_ORDER:
        BUILTIN_ORDER.append(registered.id)
    return registered


for _cls in (MaCrossStrategy, MomentumStrategy, GridStrategy,
             LowVolStrategy, RsiReversionStrategy, TurtleStrategy):
    register_builtin(_cls)


# ========================================================================== 本地策略自动发现
# 运行期约定：``strategies/local/<slug>.py`` 会被本模块按文件名排序导入并注册，
# 单个文件失败只在 LOCAL_ERRORS 中记录原因并跳过 —— 任何情况下都不允许影响应用启动。
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local")
_LOCAL_PACKAGE = __name__.rsplit(".", 1)[0] + ".local"      # quantstudio.strategies.local
LOCAL_IDS: List[str] = []
LOCAL_ERRORS: List[Dict[str, str]] = []
_local_discovered = False


def _module_strategy_classes(module: Any) -> List[Type[BaseStrategy]]:
    """取模块内**自己定义**的策略类（不收集 import 进来的类，避免误注册）。"""
    found: List[Type[BaseStrategy]] = []
    for value in vars(module).values():
        if (isinstance(value, type) and issubclass(value, BaseStrategy) and value is not BaseStrategy
                and getattr(value, "__module__", "") == getattr(module, "__name__", None)):
            found.append(value)
    return found


def _import_local_module(slug: str, filename: str):
    """导入 ``local/<slug>.py``：先刷新导入缓存，缓存仍视而不见时按文件路径直接加载。

    背景：``FileFinder`` 会按**目录 mtime** 缓存目录列表，粒度粗或同一秒内新增文件时会读到旧列表，
    于是刚写入的策略文件 ``import_module`` 报 ``ModuleNotFoundError``（Windows CI 上实测出现，
    用户运行中新增策略也可能踩到）。因此这里显式 ``invalidate_caches()``，并保留按路径加载的兜底。
    """
    module_name = "%s.%s" % (_LOCAL_PACKAGE, slug)
    importlib.invalidate_caches()
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", "") != module_name:
            raise                                  # 缺依赖等其它导入错误照旧上抛
    path = os.path.join(LOCAL_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("无法定位模块 %s（%s）" % (module_name, path), name=module_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def discover_local(force: bool = False) -> List[str]:
    """导入并注册 ``strategies/local/*.py``（幂等）。返回成功加载的策略 id 列表。"""
    global _local_discovered
    if _local_discovered and not force:
        return list(LOCAL_IDS)
    if force:
        LOCAL_IDS.clear()
        LOCAL_ERRORS.clear()
    _local_discovered = True
    if not os.path.isdir(LOCAL_DIR):
        return list(LOCAL_IDS)
    for filename in sorted(os.listdir(LOCAL_DIR)):
        if not filename.endswith(".py") or filename.startswith("_") or filename == "__init__.py":
            continue          # `_template.py` 之类的下划线文件按约定不加载
        slug = filename[:-3]
        try:
            module = _import_local_module(slug, filename)
        except Exception as exc:                      # noqa: BLE001 - 任何异常都只记录
            LOCAL_ERRORS.append({"file": filename, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        for cls in _module_strategy_classes(module):
            strategy_id = str(getattr(cls, "id", "") or "").strip()
            if not strategy_id:
                LOCAL_ERRORS.append({"file": filename, "error": "策略类 %s 未定义 id" % cls.__name__})
                continue
            exist = REGISTRY.get(strategy_id)
            if exist is not None and exist is not cls:
                same_definition = (getattr(exist, "__module__", "") == getattr(cls, "__module__", "")
                                   and exist.__name__ == cls.__name__)
                if same_definition:
                    # 模块被重新加载（lint 的热重载、开发期改文件）：用新类对象替换陈旧引用
                    REGISTRY[strategy_id] = cls
                else:
                    LOCAL_ERRORS.append({
                        "file": filename,
                        "error": "策略 id 冲突：%s 已由 %s 注册" % (strategy_id, exist.__module__),
                    })
                    continue
            if exist is None:
                try:
                    register(cls)
                except ValidationError as exc:
                    LOCAL_ERRORS.append({"file": filename, "error": str(exc)})
                    continue
            if strategy_id not in LOCAL_IDS:
                LOCAL_IDS.append(strategy_id)
    return list(LOCAL_IDS)


def local_status() -> Dict[str, Any]:
    """本地策略目录状态（``/api/system/status`` 与 ``lint`` 都会用到）。"""
    discover_local()
    return {"dir": LOCAL_DIR, "loaded": list(LOCAL_IDS), "errors": list(LOCAL_ERRORS)}


def is_builtin(strategy_id: str) -> bool:
    """是否为本工程内置策略 id（内置策略不可被用户删除）。"""
    return str(strategy_id) in BUILTIN_ORDER


def is_local(strategy_id: str) -> bool:
    """是否为 ``strategies/local/`` 下的本地代码策略。"""
    discover_local()
    return str(strategy_id or "").strip() in LOCAL_IDS


def get_class(strategy_id: str) -> Type[BaseStrategy]:
    """按 id 取策略类；不存在抛 ValidationError（消息可直接给前端）。"""
    discover_local()
    key = str(strategy_id or "").strip()
    if not key:
        raise ValidationError("strategy_id 不能为空", field="strategy_id")
    cls = REGISTRY.get(key)
    if cls is None:
        raise ValidationError(
            "未知策略 id：%s（可用：%s）" % (key, ", ".join(list_specs_ids()) or "无"),
            field="strategy_id",
        )
    return cls


def try_get_class(strategy_id: str) -> Optional[Type[BaseStrategy]]:
    """按 id 取策略类；不存在返回 None。"""
    discover_local()
    return REGISTRY.get(str(strategy_id or "").strip())


def list_specs_ids() -> List[str]:
    """已注册的全部策略 id（内置按 BUILTIN_ORDER → 本地代码策略 → 其余按字典序）。"""
    discover_local()
    extra = sorted(key for key in REGISTRY.keys() if key not in BUILTIN_ORDER and key not in LOCAL_IDS)
    return [key for key in BUILTIN_ORDER if key in REGISTRY] + [key for key in LOCAL_IDS if key in REGISTRY] + extra


def list_specs() -> List[StrategySpec]:
    """全部策略的元数据（内置 → 本地 → 用户模板，每次返回全新对象，顺序固定）。"""
    discover_local()
    return [with_origin(REGISTRY[key].meta()) for key in list_specs_ids()]


def with_origin(spec: StrategySpec) -> StrategySpec:
    """按注册来源补正 ``origin`` / ``builtin``（本地代码策略不是内置策略，删除方式是移除文件）。"""
    if spec.id in LOCAL_IDS:
        spec.origin = "local"
        spec.builtin = False
    return spec


def describe() -> List[Dict[str, Any]]:
    """策略列表的字典形式（API 直接返回）。"""
    return [spec.to_dict() for spec in list_specs()]


def spec_from_dict(payload: Dict[str, Any], cls: Type[BaseStrategy]) -> StrategySpec:
    """把「用户策略条目（dict）」转成 StrategySpec（供接口层直接透传 dict 时使用）。"""
    if not isinstance(payload, dict):
        raise ValidationError("spec 必须是 StrategySpec 或 dict，当前为 %r" % type(payload).__name__, field="spec")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    return StrategySpec(
        id=str(payload.get("id") or "").strip() or cls.id,
        name=str(payload.get("name") or "").strip() or cls.name,
        category=str(payload.get("category") or cls.category).strip() or cls.category,
        desc=str(payload.get("desc") or "").strip(),
        params=dict(params),
        status=str(payload.get("status") or "paused").strip() or "paused",
        universe=str(payload.get("universe") or cls.universe).strip(),
        universe_type=str(payload.get("universe_type") or cls.universe_type).strip() or cls.universe_type,
        freq=str(payload.get("freq") or cls.freq).strip() or cls.freq,
        builtin=False,
        origin="user",         # 界面新建的模板实例（不是内置代码，也不是 local/ 代码）
        param_schema=cls.param_schema(),
        min_bars=int(cls.min_bars),
        version=str(cls.version),
    )


def create(
    strategy_id: str,
    params: Optional[Dict[str, Any]] = None,
    symbols: Optional[Sequence[str]] = None,
    spec: Optional[Any] = None,
) -> BaseStrategy:
    """构造策略实例。

    - ``strategy_id``：内置模板 id（用户策略请传 ``spec.template`` 指向的内置 id）；
    - ``params``：覆盖参数（优先级最高，缺失项取模板默认值）；
    - ``symbols``：标的池（为空则用策略建议池，回测引擎还会再兜底到 settings.default_watchlist）；
    - ``spec``：``StrategySpec`` 或用户策略条目 ``dict``（接口层可直接透传），
      其 ``params`` 作为覆盖基线，实例元数据沿用该 spec。
    """
    cls = get_class(strategy_id)
    if isinstance(spec, dict):
        spec = spec_from_dict(spec, cls)
    if spec is not None and not isinstance(spec, StrategySpec):
        raise ValidationError("spec 必须是 StrategySpec 或 dict，当前为 %r" % type(spec).__name__, field="spec")
    merged: Dict[str, Any] = {}
    if spec is not None:
        merged.update(spec.params or {})
    merged.update(params or {})
    instance = cls(params=merged, symbols=symbols, spec=spec)
    instance.spec = with_origin(instance.spec)
    return instance


def create_from_spec(spec: Any, symbols: Optional[Sequence[str]] = None,
                     template: Optional[str] = None) -> BaseStrategy:
    """按「用户策略 spec（StrategySpec 或 dict）+ 模板 id」构造实例。"""
    strategy_id = str(template or "").strip()
    payload = spec
    if isinstance(payload, dict):
        if not strategy_id:
            strategy_id = str(payload.get("template") or "").strip()
        if not strategy_id:
            raise ValidationError("无法确定内置模板：请在 template 中指定内置策略 id", field="template")
        cls = get_class(strategy_id)
        return create(strategy_id, params=dict(payload.get("params") or {}), symbols=symbols,
                      spec=spec_from_dict(payload, cls))
    if not isinstance(payload, StrategySpec):
        raise ValidationError("spec 必须是 StrategySpec 或 dict", field="spec")
    if not strategy_id:
        strategy_id = payload.id if payload.id in REGISTRY else ""
    if not strategy_id:
        raise ValidationError("无法确定内置模板：请在 template 中指定内置策略 id", field="template")
    return create(strategy_id, params=dict(payload.params or {}), symbols=symbols, spec=payload)


def discover_and_specs() -> List[StrategySpec]:      # 便捷入口（等价于 list_specs()）
    """发现本地策略并返回全部策略元数据。"""
    return list_specs()
