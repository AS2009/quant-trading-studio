# -*- coding: utf-8 -*-
"""抽象接口（Protocol）：数据源、交易通道、策略。

实现方只需“结构匹配”，无需显式继承；`runtime_checkable` 便于测试断言。
依赖方向：本模块只依赖 core.models / core.errors，**不得**反向依赖 data/backtest 等实现层。
"""

from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from .models import (
    Account,
    Bar,
    Fill,
    IndexQuote,
    MarketBreadth,
    Order,
    OrderRequest,
    Position,
    Quote,
    SectorQuote,
    StrategySpec,
    Trade,
)

# =========================================================================== 数据源


@runtime_checkable
class DataProvider(Protocol):
    """行情数据源。所有方法**必须**是只读的，且失败时抛 DataSourceError 子类。"""

    name: str  # sina / eastmoney / csv / sample / composite

    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照。symbols 为规范化代码（600519.SH）；不支持的类型可跳过。"""
        ...

    def kline(
        self,
        symbol: str,
        days: int = 250,
        freq: str = "day",
        adjust: str = "qfq",
    ) -> List[Bar]:
        """历史 K 线，按日期升序。freq: day/week/month；adjust: qfq/hfq/none。"""
        ...

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照。"""
        ...

    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """板块行情，按涨跌幅降序。"""
        ...

    def breadth(self) -> MarketBreadth:
        """市场广度与资金。"""
        ...

    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（未知返回 None）。"""
        ...

    def health(self) -> Dict[str, Any]:
        """自检：{'ok': bool, 'detail': str, 'latency_ms': int}。"""
        ...


# =========================================================================== 交易通道


@runtime_checkable
class Broker(Protocol):
    """交易通道（模拟盘与真实券商共用同一接口）。"""

    name: str
    is_live: bool  # True 表示会真的发出委托（默认实现必须为 False）

    def submit(self, order: OrderRequest) -> Order:
        """提交委托，返回委托状态（可能已成交，也可能被拒绝）。"""
        ...

    def cancel(self, order_id: str) -> Order:
        ...

    def orders(self, limit: int = 100) -> List[Order]:
        ...

    def fills(self, limit: int = 100) -> List[Fill]:
        ...

    def positions(self) -> List[Position]:
        ...

    def account(self) -> Account:
        ...


# =========================================================================== 策略


@runtime_checkable
class Strategy(Protocol):
    """策略：以「逐 bar 事件」方式驱动，回测与模拟盘共用。"""

    spec: StrategySpec

    def on_start(self, ctx: "StrategyContext") -> None:
        ...

    def on_bar(self, ctx: "StrategyContext", bars: Dict[str, Bar]) -> List[OrderRequest]:
        """收到当日各标的 bar；返回委托列表（可空）。"""
        ...

    def on_finish(self, ctx: "StrategyContext") -> None:
        ...


@runtime_checkable
class StrategyContext(Protocol):
    """策略运行时上下文：由回测引擎或模拟盘注入。

    策略只通过它读取历史、持仓、资金，避免直接触碰账户对象。
    """

    today: str

    def history(self, symbol: str, field: str = "close", n: int = 60) -> List[float]:
        """取截至今日（含）的最近 n 个值。"""
        ...

    def position(self, symbol: str) -> Optional[Position]:
        ...

    def positions(self) -> List[Position]:
        ...

    def cash(self) -> float:
        ...

    def total_assets(self) -> float:
        ...

    def bars_since(self, symbol: str, n: int) -> List[Bar]:
        ...

    def log(self, message: str) -> None:
        """回测过程中的信息，会收集到结果 warnings/logs。"""


# =========================================================================== 结果承载


@runtime_checkable
class StrategyRunSummary(Protocol):
    """一次策略运行的摘要（回测/模拟盘共用）。"""

    trades: List[Trade]
    warnings: List[str]
