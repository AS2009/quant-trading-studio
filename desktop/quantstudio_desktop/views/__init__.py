# -*- coding: utf-8 -*-
"""页面契约与注册表。

每个页面是一个 ``tkinter.ttk.Frame`` 子类，只依赖 ``app`` 提供的四样东西：
``app.services``（服务桥接）、``app.tasks``（后台任务）、``app.toast``（提示）、``app.status``（状态栏）。
**耗时操作必须走 ``self.tasks.run(...)``**，否则界面会卡死。

新增页面：在本文件的 ``VIEW_SPECS`` 里加一行，并在 ``views/`` 下实现对应类即可；
打包时需把模块名加入 ``desktop/build/quantstudio.spec`` 的 ``hiddenimports``（PyInstaller 静态分析看不到动态导入）。
"""

import importlib
import tkinter.ttk as ttk
from typing import Any, Dict, List, Tuple

# (key, 侧栏标题, 模块名, 类名)
VIEW_SPECS: List[Tuple[str, str, str, str]] = [
    ("market", "行情看板", "quantstudio_desktop.views.market", "MarketView"),
    ("strategies", "策略管理", "quantstudio_desktop.views.strategies", "StrategiesView"),
    ("backtest", "回测分析", "quantstudio_desktop.views.backtest", "BacktestView"),
    ("portfolio", "持仓管理", "quantstudio_desktop.views.portfolio", "PortfolioView"),
    ("trade", "交易（模拟盘）", "quantstudio_desktop.views.trade", "TradeView"),
]


class BaseView(ttk.Frame):
    """页面基类。子类至少实现 ``build()``；数据加载写在 ``reload()`` 里。"""

    title = "页面"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.services = app.services
        self.tasks = app.tasks
        self.toast = app.toast
        self._built = False

    # ---- 生命周期 ----
    def ensure_built(self) -> None:
        if not self._built:
            self.build()
            self._built = True

    def build(self) -> None:                       # pragma: no cover - 子类实现
        raise NotImplementedError("%s 必须实现 build()" % type(self).__name__)

    def refresh(self, force: bool = False) -> None:
        """切换到本页或点击刷新时调用（首次会先构建控件）。"""
        self.ensure_built()
        self.reload()

    def reload(self) -> None:
        """加载数据并渲染。默认什么都不做，子类覆盖。"""

    # ---- 便捷封装 ----
    def load(self, fn, *args, on_done=None, on_error=None, **kwargs) -> int:
        """把服务调用丢到后台线程；``on_done`` 在主线程执行。"""
        return self.tasks.run(fn, *args, on_done=on_done, on_error=on_error, **kwargs)

    def toast_error(self, exc: BaseException) -> None:
        self.toast.show("操作失败：%s" % exc, kind="error")

    def status(self, text: str) -> None:
        self.app.status(text)


def load_view_class(module_name: str, class_name: str):
    """动态导入页面类（打包后同样可用，见 spec 的 hiddenimports）。"""
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


__all__ = ["BaseView", "VIEW_SPECS", "load_view_class"]
