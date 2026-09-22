# -*- coding: utf-8 -*-
"""可复用控件集合（页面从这里取控件，避免各页重复造轮子）。

| 模块 | 提供 | 说明 |
|---|---|---|
| ``charts`` | ``LineChart`` / ``MultiLineChart`` / ``CandleChart`` / ``BarChart`` / ``DonutChart`` | 纯 ``tk.Canvas`` 绘制，零依赖；数据→几何换算与渲染分离，便于测试 |
| ``table`` | ``DataTable`` | ``ttk.Treeview`` 封装：列定义、排序、按列着色、行选中回调、空状态 |
| ``cards`` | ``StatCard`` / ``CardGrid`` / ``SectionTitle`` / ``Badge`` | 指标卡、卡片容器与徽标 |
| ``toast`` | ``Toast`` | 右下角提示（成功/失败/警告/信息） |
| ``forms`` | ``FormDialog`` / ``Field`` / ``FormSpec`` | 表单弹窗（新建策略、录入持仓、下单、回测参数） |

约定：所有控件都使用 ``theme`` 的配色与字体，不接受外部硬编码颜色。
子模块按需惰性导入，任一控件缺失不影响其它控件使用。
"""

from typing import Any

__all__ = ["charts", "table", "cards", "toast", "forms"]


def __getattr__(name: str) -> Any:
    """懒加载子模块：``widgets.charts`` 等价于 ``from . import charts``。"""
    if name in __all__:
        import importlib

        module = importlib.import_module("." + name, __name__)
        globals()[name] = module
        return module
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
