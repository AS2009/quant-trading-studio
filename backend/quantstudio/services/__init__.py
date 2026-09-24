# -*- coding: utf-8 -*-
"""业务服务层聚合入口。

``Services`` 惰性构造 provider / 回测引擎 / 模拟盘 broker / 持仓账户，
保证「导入期不访问网络」；``get_services()`` 为进程级单例，便于接口层复用。

测试可通过 ``Services(provider=FakeProvider(), engine=FakeEngine(), ...)``
或 ``create_app(services=...)`` 注入替身。
"""

import threading
from typing import Any, Optional

from ..config import Settings, get_settings
from ..core.models import DataMeta
from .backtest_service import BacktestService
from .common import provider_meta
from .level2_service import Level2Service
from .market_service import MarketService
from .portfolio_service import PortfolioService
from .strategy_service import StrategyService

__all__ = ["Services", "get_services", "MarketService", "Level2Service", "StrategyService",
           "BacktestService", "PortfolioService"]


class Services:
    """所有业务服务的聚合（组合而非继承，便于替换任意一层）。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        provider: Any = None,
        engine: Any = None,
        broker: Any = None,
        portfolio_service: Any = None,
        strategies_module: Any = None,
        user_module: Any = None,
    ):
        self.settings = settings or get_settings()
        self.market = MarketService(provider=provider, settings=self.settings)
        self.level2 = Level2Service(provider=provider, settings=self.settings)
        self.strategies = StrategyService(
            settings=self.settings,
            strategies_module=strategies_module,
            user_module=user_module,
        )
        self.backtest = BacktestService(
            provider=provider,
            settings=self.settings,
            engine=engine,
            strategies_module=strategies_module,
            user_module=user_module,
        )
        self.portfolio = PortfolioService(
            provider=provider,
            settings=self.settings,
            broker=broker,
            backend=portfolio_service,
        )

    # ------------------------------------------------------------------ 便捷访问

    @property
    def provider(self):
        """行情数据源（惰性构造，全服务共享同一实例）。"""
        return self.market.provider

    def meta(self) -> DataMeta:
        """当前数据源元信息（接口层统一信封使用）。"""
        return provider_meta(self.provider)


_services: Optional[Services] = None
_lock = threading.RLock()


def get_services(
    settings: Optional[Settings] = None,
    force: bool = False,
    **kwargs: Any
) -> Services:
    """进程级单例；``force=True`` 或首次调用时构造（可用于测试注入）。"""
    global _services
    with _lock:
        if _services is None or force:
            _services = Services(settings=settings, **kwargs)
        return _services
