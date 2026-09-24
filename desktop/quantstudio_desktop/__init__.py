# -*- coding: utf-8 -*-
"""QuantTrading Studio 桌面版（Windows 原生 GUI）。

同一个核心（``backend/quantstudio``）的两个前端之一：
- Web 前端：Flask + 原生 JS（浏览器打开）
- **桌面前端（本包）**：Tkinter/ttk 原生窗口，直接调用服务层，**不需要 Flask、不需要浏览器、不需要端口**

设计要点
--------
- 零新增运行时依赖：只用标准库（tkinter/ttk 随 CPython 提供）+ 项目自身的 quantstudio 包；
- 界面响应性：所有网络/回测等耗时操作都在后台线程执行，结果通过队列回到主线程渲染（见 ``services.TaskRunner``）；
- 深浅一致的深色金融风：ttk 使用 ``clam`` 主题以便在 Windows 上完整着色（见 ``theme``）；
- 可自动化验收：``--selftest``（数据/回测自检）与 ``--selftest-gui``（构建整个窗口与所有页面后销毁），CI 用它验证打包产物。

入口
----
::

    python -m quantstudio_desktop              # 启动图形界面
    python -m quantstudio_desktop --selftest       # 无界面自检（数据源 + 回测 + 报告）
    python -m quantstudio_desktop --selftest-gui   # 无显示器自检（构建窗口与页面）
"""

__version__ = "1.2.0"

from .boot import ensure_core_path, ensure_data_dir, is_frozen, prepare  # noqa: E402,F401

# 导入本包即完成路径与数据目录准备：后续 ``import quantstudio`` 才可用
_BOOT = prepare()
