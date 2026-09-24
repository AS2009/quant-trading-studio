# -*- coding: utf-8 -*-
"""工具运行上下文：服务层入口、运行模式、数据目录、审计与写保护。

每个工具 handler 的第一个参数都是 :class:`ToolContext`，它统一提供：

* ``ctx.services()`` / ``ctx.market`` / ``ctx.strategies_service`` / ``ctx.backtest`` / ``ctx.portfolio``：
  与 Web、桌面版**完全同一套**业务服务（``quantstudio.services``），MCP 只做参数映射与摘要；
* ``ctx.read_only``：``--read-only`` 启动时为真，写工具会被隐藏且拒绝执行；
* ``ctx.local_dir`` / ``ctx.data_dir`` / ``ctx.backup_dir``：允许写入的目录与备份目录；
* ``ctx.audit``：写操作审计（每次写都留痕）；
* ``ctx.write_guard(tool, args)``：写操作的统一入口检查（只读模式直接拒绝）。
"""

import os
from typing import Any, Optional

from ..config import get_settings
from .registry import ToolError
from .safety import AUDIT_FILENAME, BACKUP_DIRNAME, AuditLog

#: 只读模式下写工具被拒绝时的提示（模型读到就知道为什么）
READ_ONLY_HINT = ("当前以只读模式（--read-only）启动。请让用户去掉该参数后重启 MCP 服务器，"
                  "或改用只读工具完成查询。")


class ToolContext:
    def __init__(self, settings: Any = None, *, read_only: bool = False, actor: str = "mcp",
                 provider: Any = None):
        self.settings = settings or get_settings()
        self.read_only = bool(read_only)
        self.actor = actor
        self._provider = provider
        self._services: Any = None
        self.data_dir = getattr(self.settings, "data_dir", "") or ""
        self.backup_dir = os.path.join(self.data_dir, BACKUP_DIRNAME) if self.data_dir else ""
        self.audit = AuditLog(os.path.join(self.data_dir, AUDIT_FILENAME) if self.data_dir else "",
                              actor=actor)

    # ------------------------------------------------------------------ 服务层（惰性构造，测试可注入替身）
    def services(self) -> Any:
        """服务层（惰性构造）。

        注入 ``provider`` 时**直接构造** ``Services``，而不是走进程级单例 ``get_services()``：
        后者在同进程已经被创建过（例如 Web 版 ``create_app()``）时会**静默忽略**注入参数，
        测试里就会表现为「以为用了假数据源，其实在联网」。
        """
        if self._services is None:
            if self._provider is not None:
                from ..services import Services

                self._services = Services(settings=self.settings, provider=self._provider)
            else:
                from ..services import get_services

                self._services = get_services(settings=self.settings)
        return self._services

    @property
    def market(self) -> Any:
        return self.services().market

    @property
    def strategies_service(self) -> Any:
        return self.services().strategies

    @property
    def backtest(self) -> Any:
        return self.services().backtest

    @property
    def portfolio(self) -> Any:
        return self.services().portfolio

    # ------------------------------------------------------------------ 目录
    @property
    def local_dir(self) -> str:
        """本地策略目录（``strategies/local``，大模型写的代码策略放在这里）。

        注意：这个目录必须在包内（策略用 ``from ..base import ...`` 相对导入，
        校验器要靠 import 才能跑烟雾回测），所以**不要**为「测试隔离」把它指到临时目录。
        """
        from ..strategies.registry import LOCAL_DIR

        return LOCAL_DIR

    @property
    def user_strategies_path(self) -> str:
        return getattr(self.settings, "user_strategies_path", "")

    @property
    def repo_root(self) -> str:
        """仓库根目录（``docs/``、``README.md`` 都在这里；文档资源从这里读）。"""
        from ..strategies.registry import LOCAL_DIR

        # 源码布局：``<repo>/backend/quantstudio/strategies/local``（往上 4 层才是仓库根）；
        # PyInstaller 打包后：``<_MEIPASS>/quantstudio/strategies/local``（往上 3 层就是包根）。
        # 直接按固定层数算会在打包后算到「包根的父目录」，导致 docs/ 找不到 —— 所以改成
        # 向上找「含 docs/ 的目录」；找不到再退回源码布局的仓库根。
        path = LOCAL_DIR
        for _ in range(3):
            path = os.path.dirname(path)
            if os.path.isdir(os.path.join(path, "docs")):
                return path
        return os.path.dirname(path)

    # ------------------------------------------------------------------ 写保护
    def write_guard(self, tool: str, args: Optional[dict] = None) -> None:
        """写工具的统一入口检查：只读模式直接拒绝。"""
        if self.read_only:
            self.audit.record(tool, args, ok=False, detail="read-only 模式拒绝写操作")
            raise ToolError("该操作会修改本地数据，但当前 MCP 服务器运行在只读模式。",
                            hint=READ_ONLY_HINT, code="READ_ONLY")

    def record(self, tool: str, args: Optional[dict] = None, ok: bool = True,
               detail: str = "", **extra: Any) -> None:
        self.audit.record(tool, args, ok=ok, detail=detail, extra=extra or None)

    # ------------------------------------------------------------------ 异常与取值
    def call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """调用服务层并统一把异常转成可读的 :class:`ToolError`。"""
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:                              # noqa: BLE001
            from ..core.errors import ValidationError

            if isinstance(exc, ValidationError):
                raise ToolError("参数不合法：%s" % exc, hint="请检查参数取值范围后重试。",
                                code="VALIDATION") from exc
            raise ToolError("调用服务失败：%s: %s" % (type(exc).__name__, exc),
                            hint="可先用 system_status 检查数据源与运行环境；若为行情接口限流，稍后重试。",
                            code="SERVICE") from exc

    def jsonable(self, value: Any) -> Any:
        """把 dataclass / 模型对象转成纯 dict/list（复用服务层的转换器）。"""
        from ..services.common import to_dict, to_dict_list

        if isinstance(value, (list, tuple)):
            return to_dict_list(list(value))
        if isinstance(value, dict):
            return value
        return to_dict(value)


__all__ = ["ToolContext", "READ_ONLY_HINT"]
