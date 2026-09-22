# -*- coding: utf-8 -*-
"""策略业务服务：内置策略 + 用户策略的统一管理。

- 内置策略来自 ``quantstudio.strategies``（注册表）
- 用户策略持久化到 ``settings.user_strategies_path``（``data/user_strategies.json``）

对并行模块（``quantstudio.strategies`` / ``quantstudio.strategies.user``）一律惰性导入，
测试时可通过构造函数注入替身。
"""

from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.errors import ValidationError
from .common import Forbidden, NotFound, to_dict


def _spec_dict(item: Any, builtin: bool) -> Dict[str, Any]:
    """把 StrategySpec/用户条目转成接口字典。

    ``builtin`` 参数只作为「未知来源时的兜底」：若 spec 自带 ``origin``，则以 origin 为准，
    这样 ``strategies/local/`` 下的本地代码策略不会被误标为内置（界面据此区分徽标与删除按钮）。
    """
    data = to_dict(item)
    origin = data.get("origin") or ("builtin" if builtin else "user")
    if origin == "builtin" and data.get("builtin") is False:
        origin = "user"          # 显式声明非内置时以 builtin 标志为准（兼容历史数据文件）
    data["origin"] = origin
    data["builtin"] = origin == "builtin"
    return data


class StrategyService:
    """策略列表 / 详情 / 新建 / 删除。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        strategies_module: Any = None,
        user_module: Any = None,
    ):
        self.settings = settings or get_settings()
        self._strategies = strategies_module
        self._user = user_module

    # ------------------------------------------------------------------ 惰性依赖

    @property
    def strategies(self):
        if self._strategies is None:
            from .. import strategies as module

            self._strategies = module
        return self._strategies

    @property
    def user(self):
        if self._user is None:
            from ..strategies import user as module

            self._user = module
        return self._user

    # ------------------------------------------------------------------ 查询

    def _builtin_specs(self) -> List[Dict[str, Any]]:
        return [_spec_dict(item, True) for item in (self.strategies.list_specs() or [])]

    def _user_specs(self) -> List[Dict[str, Any]]:
        path = self.settings.user_strategies_path
        return [_spec_dict(item, False) for item in (self.user.load_user_specs(path) or [])]

    def list_strategies(self) -> List[Dict[str, Any]]:
        """内置在前、用户在后；id 冲突时以内置为准。"""
        result: List[Dict[str, Any]] = []
        seen = set()
        for data in self._builtin_specs():
            if data.get("id") in seen:
                continue
            seen.add(data.get("id"))
            result.append(data)
        for data in self._user_specs():
            if data.get("id") in seen:
                continue
            seen.add(data.get("id"))
            result.append(data)
        return result

    def get_strategy(self, strategy_id: str) -> Dict[str, Any]:
        for data in self.list_strategies():
            if data.get("id") == strategy_id:
                return data
        raise NotFound("策略不存在：%s" % strategy_id)

    # ------------------------------------------------------------------ 变更

    def create_strategy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """新建用户策略；校验失败抛 ValidationError（400）。"""
        if not isinstance(payload, dict):
            raise ValidationError("请求体需为 JSON 对象", field="body")
        item = dict(payload)
        item.pop("builtin", None)
        path = self.settings.user_strategies_path
        try:
            created = self.user.add_user_strategy(path, item)
        except ValidationError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise ValidationError(str(exc) or "策略参数非法", field="body")

        wanted = str(item.get("id") or "")
        if isinstance(created, (list, tuple)):
            candidates = [_spec_dict(x, False) for x in created]
            for data in reversed(candidates):
                if wanted and data.get("id") == wanted:
                    return data
            if candidates:
                return candidates[-1]
            raise ValidationError("策略创建失败：未返回策略定义", field="body")
        return _spec_dict(created, False)

    def delete_strategy(self, strategy_id: str) -> Dict[str, Any]:
        """删除用户策略；内置策略 403、不存在 404。"""
        spec = None
        for data in self.list_strategies():
            if data.get("id") == strategy_id:
                spec = data
                break
        if spec is None:
            raise NotFound("策略不存在：%s" % strategy_id)
        if spec.get("builtin"):
            raise Forbidden("内置策略不可删除：%s" % strategy_id)

        path = self.settings.user_strategies_path
        try:
            self.user.delete_user_strategy(path, strategy_id)
        except NotFound:
            raise
        except (ValueError, TypeError) as exc:
            raise NotFound("策略不存在：%s（%s）" % (strategy_id, exc))
        # 以落盘结果为准，避免上级模块返回语义不一致
        if any(item.get("id") == strategy_id for item in self._user_specs()):
            raise NotFound("策略不存在：%s" % strategy_id)
        return {"id": strategy_id, "deleted": True}
