# -*- coding: utf-8 -*-
"""用户自定义策略的持久化与构造。

存储格式（JSON 文件，默认 ``settings.user_strategies_path``）
----------------------------------------------------------
```json
{
  "version": 1,
  "items": [
    {
      "id": "us_1a2b3c4d",
      "name": "我的双均线",
      "category": "趋势跟踪",
      "desc": "MA10/MA30 更灵敏的版本",
      "params": {"short_ma": 10, "long_ma": 30, "position_pct": 0.8},
      "status": "running",
      "universe": "自选股",
      "freq": "日线",
      "template": "st_ma_cross"
    }
  ]
}
```

设计要点
--------
- **用户策略 = 内置模板 + 一组参数**（``template`` 指向内置策略 id，默认 ``st_ma_cross``）：
  注册表按模板类构造实例，``params`` 必须落在模板 ``param_schema`` 之内（非法键直接拒绝）。
- **原子写**：先写同目录临时文件 + ``os.replace``，避免进程中断产生半个 JSON。
- **读写失败不崩接口**：``load_user_specs`` 读取失败一律返回 ``[]``；
  ``add`` / ``delete`` 的 IO 失败转成可读的 :class:`ValidationError`（接口层可回 400/500）。
- 内置策略（``registry.is_builtin``）**不可删除**，重名 / 非法分类 / NaN / Infinity 一律拒绝。
"""

import json
import os
import re
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..core.errors import ValidationError
from ..core.models import StrategySpec
from . import registry

__all__ = [
    "CATEGORY_CHOICES", "STATUS_CHOICES", "FREQ_CHOICES", "DEFAULT_TEMPLATE",
    "load_user_items", "load_user_specs", "save_user_items", "save_user_specs",
    "add_user_strategy", "delete_user_strategy", "find_user_item", "create_from_user",
]

CATEGORY_CHOICES: Tuple[str, ...] = ("趋势跟踪", "动量", "震荡市", "稳健", "均值回归", "自定义")
STATUS_CHOICES: Tuple[str, ...] = ("running", "paused")
FREQ_CHOICES: Tuple[str, ...] = ("日线", "周度", "月度", "盘中")
DEFAULT_TEMPLATE = "st_ma_cross"

_NAME_MIN, _NAME_MAX = 2, 24
_UNIVERSE_MAX = 60
_DESC_MAX = 200
_ITEM_KEYS = ("id", "name", "category", "desc", "params", "status", "universe", "freq", "template")


# --------------------------------------------------------------------------- 读写
def _read_raw(path: str) -> List[Dict[str, Any]]:
    """读取原始条目（失败返回空列表，绝不抛异常）。"""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("items")
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip():
            items.append({key: entry.get(key) for key in _ITEM_KEYS if key in entry})
    return items


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """原子写 JSON（临时文件 + os.replace），失败抛 ValidationError。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False)
    tmp_path = ""
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".user_strategies_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    except (OSError, ValueError) as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise ValidationError("保存用户策略失败：%s" % exc, field="path")


def _to_spec(item: Dict[str, Any]) -> StrategySpec:
    """JSON 条目 → StrategySpec（模板信息不出现在 spec 中，用 template 字段单独保留）。"""
    template = str(item.get("template") or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    cls = registry.try_get_class(template)
    schema = cls.param_schema() if cls is not None else {}
    min_bars = int(cls.min_bars) if cls is not None else 60
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    return StrategySpec(
        id=str(item.get("id") or "").strip(),
        name=str(item.get("name") or "").strip(),
        category=str(item.get("category") or "自定义").strip() or "自定义",
        desc=str(item.get("desc") or "").strip(),
        params={key: params[key] for key in schema if key in params},
        status=str(item.get("status") or "paused").strip() or "paused",
        universe=str(item.get("universe") or "").strip(),
        universe_type=getattr(cls, "universe_type", "multi") if cls is not None else "multi",
        freq=str(item.get("freq") or "日线").strip() or "日线",
        builtin=False,
        origin="user",          # 界面/接口新建的模板实例；内置=builtin，local/=local
        param_schema=dict(schema),
        min_bars=min_bars,
        version=str(getattr(cls, "version", "1.0")) if cls is not None else "1.0",
    )


def load_user_items(path: str) -> List[Dict[str, Any]]:
    """读取**原始条目**（含 ``template`` 字段，可直接回写）。读取失败返回 ``[]``。"""
    return _read_raw(path)


def load_user_specs(path: str) -> List[StrategySpec]:
    """读取用户策略为 ``StrategySpec`` 列表（``builtin=False``，含模板 param_schema）。"""
    specs: List[StrategySpec] = []
    for item in _read_raw(path):
        try:
            specs.append(_to_spec(item))
        except Exception:
            continue
    return specs


def _normalize_items(items: Sequence[Union[Dict[str, Any], StrategySpec]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, StrategySpec):
            out.append({
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "desc": item.desc,
                "params": dict(item.params or {}),
                "status": item.status,
                "universe": item.universe,
                "freq": item.freq,
                "template": DEFAULT_TEMPLATE,
            })
        elif isinstance(item, dict):
            out.append({key: item.get(key) for key in _ITEM_KEYS if key in item})
    return out


def save_user_items(path: str, items: Sequence[Dict[str, Any]]) -> None:
    """原子保存原始条目。"""
    _atomic_write_json(path, {"version": 1, "items": _normalize_items(items)})


def save_user_specs(path: str, items: Sequence[Union[Dict[str, Any], StrategySpec]]) -> None:
    """原子保存（接受 ``StrategySpec`` 或原始条目；StrategySpec 会写回默认模板 id）。"""
    _atomic_write_json(path, {"version": 1, "items": _normalize_items(items)})


def find_user_item(path: str, strategy_id: str) -> Optional[Dict[str, Any]]:
    """按 id 查原始条目（不存在返回 None）。"""
    key = str(strategy_id or "").strip()
    for item in _read_raw(path):
        if str(item.get("id") or "").strip() == key:
            return item
    return None


# --------------------------------------------------------------------------- 校验
def _clean_text(payload: Dict[str, Any], key: str, max_len: int, default: str = "") -> str:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError("字段 %s 需为字符串" % key, field=key)
    text = value.strip()
    if len(text) > max_len:
        raise ValidationError("字段 %s 长度不能超过 %d 个字符" % (key, max_len), field=key)
    return text


def _validate_name(name: str, existing: Sequence[Dict[str, Any]]) -> str:
    if not name:
        raise ValidationError("策略名称不能为空", field="name")
    if len(name) < _NAME_MIN or len(name) > _NAME_MAX:
        raise ValidationError(
            "策略名称需为 %d~%d 个字符（当前 %d 个）" % (_NAME_MIN, _NAME_MAX, len(name)), field="name"
        )
    lowered = name.lower()
    for item in existing:
        other = str(item.get("name") or "").strip()
        if other and (other == name or other.lower() == lowered):
            raise ValidationError("策略名称「%s」已存在，请换一个名字" % name, field="name")
    return name


def _new_id(used: Sequence[str]) -> str:
    taken = {str(item.get("id") or "").strip() for item in used}
    for _ in range(50):
        candidate = "us_" + uuid.uuid4().hex[:8]
        if candidate not in taken and not registry.is_builtin(candidate):
            return candidate
    return "us_" + uuid.uuid4().hex


def add_user_strategy(path: str, payload: Dict[str, Any]) -> StrategySpec:
    """新增用户策略（校验 + 原子持久化），返回新建的 ``StrategySpec``。

    校验项：name 2~24 字且不重名；category 在 :data:`CATEGORY_CHOICES`；
    template 必须是已注册的内置策略 id（默认 ``st_ma_cross``）；
    params 必须是该模板 ``param_schema`` 覆盖的合法键与合法取值（类型 / 范围 / 非 NaN）；
    status ∈ running/paused；freq ∈ FREQ_CHOICES。
    """
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象", field="payload")

    items = _read_raw(path)
    name = _validate_name(_clean_text(payload, "name", 64), items)

    category = _clean_text(payload, "category", 16, "自定义") or "自定义"
    if category not in CATEGORY_CHOICES:
        raise ValidationError(
            "策略类别只能是 %s，当前 %r" % ("/".join(CATEGORY_CHOICES), category), field="category"
        )

    status = _clean_text(payload, "status", 16, "paused") or "paused"
    if status not in STATUS_CHOICES:
        raise ValidationError("策略状态只能是 %s" % "/".join(STATUS_CHOICES), field="status")

    freq = _clean_text(payload, "freq", 16, "日线") or "日线"
    if freq not in FREQ_CHOICES:
        raise ValidationError("策略频率只能是 %s" % "/".join(FREQ_CHOICES), field="freq")

    template = _clean_text(payload, "template", 64, DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE
    cls = registry.try_get_class(template)
    if cls is None:
        raise ValidationError(
            "模板策略不存在：%s（可选：%s）" % (template, ", ".join(registry.list_specs_ids())), field="template"
        )

    raw_params = payload.get("params")
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise ValidationError("params 必须是对象（dict），键取自模板参数表", field="params")
    params = cls.validate_params(raw_params)          # 未知键 / 类型 / 范围 / NaN 一律抛 ValidationError

    desc = _clean_text(payload, "desc", _DESC_MAX, str(cls.desc or ""))
    universe = _clean_text(payload, "universe", _UNIVERSE_MAX, str(cls.universe or ""))

    forced_id = _clean_text(payload, "id", 64)
    used_ids = [str(item.get("id") or "") for item in items]
    if forced_id and not registry.is_builtin(forced_id) and forced_id not in used_ids:
        strategy_id = forced_id
    else:
        strategy_id = _new_id(items)

    item = {
        "id": strategy_id,
        "name": name,
        "category": category,
        "desc": desc,
        "params": params,
        "status": status,
        "universe": universe,
        "freq": freq,
        "template": template,
    }
    items.append(item)
    save_user_items(path, items)
    return _to_spec(item)


def delete_user_strategy(path: str, strategy_id: str) -> bool:
    """删除用户策略：内置策略不可删（ValidationError），不存在返回 False，成功返回 True。"""
    key = str(strategy_id or "").strip()
    if not key:
        raise ValidationError("strategy_id 不能为空", field="strategy_id")
    if registry.is_builtin(key):
        raise ValidationError("内置策略不可删除：%s" % key, field="strategy_id")
    items = _read_raw(path)
    remain = [item for item in items if str(item.get("id") or "").strip() != key]
    if len(remain) == len(items):
        return False
    save_user_items(path, remain)
    return True


def create_from_user(item: Union[Dict[str, Any], StrategySpec],
                     symbols: Optional[Sequence[str]] = None):
    """按用户条目（原始 dict 或 StrategySpec）构造策略实例。

    用户策略只是「内置模板 + 参数」：这里解析 ``template``（默认 ``st_ma_cross``）后
    调用 :func:`quantstudio.strategies.registry.create`，spec 保留用户策略元信息。
    """
    if isinstance(item, StrategySpec):
        spec = item
        template = spec.id if spec.id in registry.REGISTRY else DEFAULT_TEMPLATE
        return registry.create_from_spec(spec, symbols=symbols, template=template)
    if not isinstance(item, dict):
        raise ValidationError("用户策略条目必须是 dict 或 StrategySpec", field="item")
    spec = _to_spec(item)
    template = str(item.get("template") or DEFAULT_TEMPLATE).strip() or DEFAULT_TEMPLATE
    return registry.create_from_spec(spec, symbols=symbols, template=template)


def sanitize_filename(text: str) -> str:
    """把策略名清理成安全文件名（接口层如需导出时使用）。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(text or "").strip())
    return cleaned[:64] or "strategy"
