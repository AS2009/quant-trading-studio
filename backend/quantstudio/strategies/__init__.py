# -*- coding: utf-8 -*-
"""策略库：6 个内置真实策略 + 注册表 + 用户自定义策略（参数覆盖内置模板）。

对外 API
--------
```python
from quantstudio.strategies import list_specs, create, get_class, REGISTRY, describe

list_specs()                                    # List[StrategySpec]（内置 6 个，顺序固定）
create("st_momentum", params={"top_n": 2})      # -> BaseStrategy 实例
get_class("st_ma_cross")                        # -> 策略类
describe()                                      # -> List[dict]（API 直接返回）
```

用户自定义策略（JSON 持久化 + 原子写）::

```python
from quantstudio.strategies import add_user_strategy, load_user_specs, delete_user_strategy, create_from_user

spec = add_user_strategy(path, {"name": "我的双均线", "template": "st_ma_cross",
                                "params": {"short_ma": 10, "long_ma": 30}})
strategy = create_from_user(item, symbols=["600519.SH"])
```

新增内置策略：在 ``strategies/`` 下新建一个文件，继承 ``BaseStrategy``，
用 ``registry.register(cls)`` 注册（或加入 ``registry.BUILTIN_ORDER`` 调整展示顺序）即可。
"""

from .base import BaseStrategy  # noqa: F401
from .registry import (  # noqa: F401
    BUILTIN_ORDER,
    LOCAL_DIR,
    LOCAL_IDS,
    REGISTRY,
    create,
    discover_local,
    is_local,
    list_specs_ids,
    local_status,
    create_from_spec,
    describe,
    get_class,
    is_builtin,
    list_specs,
    register,
    spec_from_dict,
    try_get_class,
)
from .user import (  # noqa: F401
    CATEGORY_CHOICES,
    DEFAULT_TEMPLATE,
    FREQ_CHOICES,
    STATUS_CHOICES,
    add_user_strategy,
    create_from_user,
    delete_user_strategy,
    find_user_item,
    load_user_items,
    load_user_specs,
    save_user_items,
    save_user_specs,
)

__all__ = [
    "BaseStrategy",
    "REGISTRY",
    "BUILTIN_ORDER",
    "register",
    "list_specs",
    "get_class",
    "try_get_class",
    "create",
    "create_from_spec",
    "spec_from_dict",
    "describe",
    "is_builtin",
    # 用户自定义策略
    "CATEGORY_CHOICES",
    "STATUS_CHOICES",
    "FREQ_CHOICES",
    "DEFAULT_TEMPLATE",
    "load_user_items",
    "load_user_specs",
    "save_user_items",
    "save_user_specs",
    "add_user_strategy",
    "delete_user_strategy",
    "find_user_item",
    "create_from_user",
]
