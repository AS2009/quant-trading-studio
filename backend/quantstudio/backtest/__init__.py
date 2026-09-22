# -*- coding: utf-8 -*-
"""回测层：账户记账（portfolio）→ 撮合（broker）→ 指标（metrics）→ 引擎（engine）。

对外主入口
----------
``from quantstudio.backtest import BacktestEngine, SimAccount, SimulatedBroker, compute_metrics``

```python
engine = BacktestEngine(provider)                     # provider: core.interfaces.DataProvider
result = engine.run(
    BacktestRequest(strategy_id="st_ma_cross", symbols=["600519.SH"],
                    start="2023-01-01", end="2026-09-18"),
    strategy,                                          # 任意满足 Strategy 协议的对象
)
result.metrics.total_return_pct, result.nav, result.trades
```

分层依赖：``engine -> broker -> portfolio -> core``，``metrics`` 只依赖 ``core``。
"""

from .broker import DEFAULT_LIMIT_PCT, SimulatedBroker  # noqa: F401
from .engine import BacktestContext, BacktestEngine, LIQUID_FIELDS  # noqa: F401
from .metrics import (  # noqa: F401
    compute_metrics,
    drawdown_series,
    monthly_returns,
    normalize_nav,
)
from .portfolio import SIDE_BUY, SIDE_SELL, SimAccount, SimPosition  # noqa: F401

__all__ = [
    "BacktestEngine",
    "BacktestContext",
    "SimulatedBroker",
    "SimAccount",
    "SimPosition",
    "compute_metrics",
    "monthly_returns",
    "drawdown_series",
    "normalize_nav",
    "DEFAULT_LIMIT_PCT",
    "LIQUID_FIELDS",
    "SIDE_BUY",
    "SIDE_SELL",
]
