# -*- coding: utf-8 -*-
"""用户持仓账户层：真实持仓（manual）与模拟盘（paper）两种模式。

导出
----
- ``PortfolioService``  持仓查询估值、手工维护、真实权益曲线
- ``MANUAL`` / ``PAPER`` 模式常量
"""

from .service import MANUAL, PAPER, MODES, PortfolioService

__all__ = ["PortfolioService", "MANUAL", "PAPER", "MODES"]
