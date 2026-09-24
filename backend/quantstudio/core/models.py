# -*- coding: utf-8 -*-
"""领域模型：行情、策略、回测、交易、账户。

约定
----
- 金额单位：元；成交额对外统一换算为 **亿元**（字段名以 `_yi` 结尾），成交量换算为 **万手**（`_wan`）。
- 所有模型提供 `to_dict()`，保证可被 `json.dumps` 直接序列化（不含 NaN/Infinity）。
- 模型是**不可变事实**的载体：数据层只负责填充，不做业务判断。
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- 交易方向

# 买卖方向的规范常量（core.costs 等零业务依赖的模块只从本文件取值，
# 避免 core 包反向依赖 backtest / strategies 包）
SIDE_BUY = "buy"
SIDE_SELL = "sell"

# --------------------------------------------------------------------------- 标的


@dataclass
class Symbol:
    """规范化标的：code 形如 600519.SH / 000001.SH(指数) / 159915.SZ(ETF)。"""

    code: str                      # 600519.SH
    name: str = ""
    market: str = ""               # SH / SZ / BJ
    kind: str = "stock"            # stock / index / etf / fund

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 行情


@dataclass
class Quote:
    """实时快照（来自新浪 / 东方财富）。"""

    code: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0            # 涨跌额
    change_pct: float = 0.0        # 涨跌幅 %
    volume_wan: float = 0.0        # 成交量（万手）
    amount_yi: float = 0.0         # 成交额（亿元）
    turnover_pct: float = 0.0      # 换手率 %
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    market_cap_yi: Optional[float] = None
    volume_ratio: Optional[float] = None
    ts: str = ""                   # 数据时点（数据源给出，YYYY-MM-DD HH:MM:SS）
    source: str = ""               # sina / eastmoney / csv / sample / cache

    @property
    def limit_up(self) -> Optional[float]:
        return round(self.prev_close * 1.1, 2) if self.prev_close else None

    @property
    def limit_down(self) -> Optional[float]:
        return round(self.prev_close * 0.9, 2) if self.prev_close else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IndexQuote:
    """指数快照。"""

    code: str
    name: str = ""
    point: float = 0.0
    prev_close: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    amount_yi: float = 0.0
    volume_wan: float = 0.0
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Bar:
    """K 线（复权后）。"""

    date: str                      # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume_wan: float = 0.0
    amount_yi: float = 0.0
    change_pct: float = 0.0
    turnover_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SectorQuote:
    """板块行情。"""

    code: str
    name: str
    change_pct: float = 0.0
    net_inflow_yi: float = 0.0
    up_count: int = 0
    down_count: int = 0
    leading_stock: str = ""
    leading_code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MarketBreadth:
    """市场广度与资金（涨跌家数口径见 README）。"""

    up: int = 0
    down: int = 0
    flat: int = 0
    limit_up: int = 0
    limit_down: int = 0
    total: int = 0
    total_amount_yi: float = 0.0
    main_net_inflow_yi: Optional[float] = None
    north_net_inflow_yi: Optional[float] = None
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 策略


@dataclass
class StrategySpec:
    """策略元数据（内置或用户自定义）。"""

    id: str
    name: str
    category: str = "自定义"
    desc: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "paused"         # running / paused
    universe: str = ""             # 人类可读说明
    universe_type: str = "multi"   # single / multi / index
    freq: str = "日线"
    builtin: bool = True
    # 策略来源：builtin（随仓库发布）/ local（strategies/local/ 下的代码）/ user（界面新建的模板实例）
    origin: str = "builtin"
    param_schema: Dict[str, Any] = field(default_factory=dict)
    min_bars: int = 60
    version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 回测


@dataclass
class FeeConfig:
    """交易费用（默认按 A 股主流费率，可覆盖）。

    口径的**唯一实现**在 :mod:`quantstudio.core.costs`（``exec_price`` / ``order_cost``）：

    - 佣金 ``max(成交额 × commission_rate, commission_min)``，双边；
    - 印花税 ``成交额 × stamp_duty_rate``，**仅卖出**；
    - 过户费 ``成交额 × transfer_fee_rate``，双边（沪深已统一）；
    - 流量费 ``flow_fee``：**每笔固定**（买卖各收一次），默认 0 元 = 与旧行为一致；
    - 滑点：``slippage_bps``（比例，双边计入成交价）与 ``slippage_ticks × tick_size``
      （按最小变动价位的跳数）**叠加**，默认跳数 0 时退化为纯比例滑点。
    """

    commission_rate: float = 0.00025     # 佣金：双边万分之 2.5
    commission_min: float = 5.0          # 单笔最低 5 元
    stamp_duty_rate: float = 0.0005      # 印花税：仅卖出，2023-08 后为 0.05%
    transfer_fee_rate: float = 0.00001   # 过户费：双边 0.001%（沪深已统一）
    slippage_bps: float = 2.0            # 滑点（基点，双边计入成交价）
    lot_size: int = 100                  # 最小交易单位（股）
    flow_fee: float = 0.0                # 每笔固定流量费（元，买卖各收一次；0 = 与旧行为一致）
    slippage_ticks: float = 0.0          # 滑点（最小变动价位的跳数，与 slippage_bps 叠加）
    tick_size: float = 0.01              # 价格最小变动单位（元，A 股 0.01 元）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestRequest:
    """回测入参。"""

    strategy_id: str
    symbols: List[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    initial_cash: float = 1_000_000.0
    benchmark: str = "000300.SH"
    fee: FeeConfig = field(default_factory=FeeConfig)
    params_override: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["fee"] = self.fee.to_dict()
        return data


@dataclass
class Trade:
    """成交流水（回测/模拟盘共用）。"""

    date: str
    code: str
    name: str
    side: str                      # buy / sell
    price: float
    qty: int
    amount: float = 0.0
    fee: float = 0.0
    pnl: Optional[float] = None
    ret_pct: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestMetrics:
    """绩效指标（全部基于真实逐日权益计算）。"""

    total_return_pct: float = 0.0
    annual_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_start: str = ""
    max_drawdown_end: str = ""
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    volatility_pct: float = 0.0
    win_rate_pct: float = 0.0
    trade_count: int = 0
    profit_loss_ratio: float = 0.0
    turnover_pct: float = 0.0
    total_fee: float = 0.0
    benchmark_return_pct: float = 0.0
    alpha_pct: float = 0.0
    beta: float = 0.0
    start: str = ""
    end: str = ""
    trading_days: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    """回测结果（API 直接返回 data.result）。"""

    strategy: StrategySpec
    request: BacktestRequest
    range: Dict[str, str] = field(default_factory=dict)
    nav: List[Dict[str, Any]] = field(default_factory=list)         # [{date, strategy, benchmark}]
    drawdown: List[Dict[str, Any]] = field(default_factory=list)    # [{date, dd_pct}]
    monthly: List[Dict[str, Any]] = field(default_factory=list)     # [{month, ret_pct}]
    metrics: BacktestMetrics = field(default_factory=BacktestMetrics)
    trades: List[Trade] = field(default_factory=list)
    equity: List[Dict[str, Any]] = field(default_factory=list)      # [{date, equity}]
    positions: List[Dict[str, Any]] = field(default_factory=list)   # 期末持仓
    warnings: List[str] = field(default_factory=list)

    @property
    def trade_total(self) -> int:
        return self.metrics.trade_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.to_dict(),
            "request": self.request.to_dict(),
            "range": dict(self.range),
            "nav": list(self.nav),
            "drawdown": list(self.drawdown),
            "monthly": list(self.monthly),
            "metrics": self.metrics.to_dict(),
            "trades": [t.to_dict() if isinstance(t, Trade) else t for t in self.trades],
            "trade_total": self.trade_total,
            "equity": list(self.equity),
            "positions": list(self.positions),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- 账户与交易


@dataclass
class Position:
    """持仓（T+1：available_qty 为可卖数量）。"""

    code: str
    name: str = ""
    qty: int = 0
    available_qty: int = 0
    cost: float = 0.0              # 摊薄成本价
    price: float = 0.0             # 最新价
    market_value: float = 0.0
    cost_value: float = 0.0
    day_pnl: float = 0.0
    total_pnl: float = 0.0
    return_pct: float = 0.0
    industry: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Account:
    """账户总览。"""

    total_assets: float = 0.0
    market_value: float = 0.0
    cash: float = 0.0
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    day_pnl: float = 0.0
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    positions_count: int = 0
    allocation: List[Dict[str, Any]] = field(default_factory=list)
    as_of: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRequest:
    """委托请求（模拟盘与真实券商适配器共用）。"""

    code: str
    side: str                      # buy / sell
    qty: int
    price: Optional[float] = None  # None = 市价
    reason: str = "manual"
    ts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    """委托状态。"""

    order_id: str
    code: str
    name: str = ""
    side: str = "buy"
    qty: int = 0
    price: Optional[float] = None
    filled_qty: int = 0
    avg_price: float = 0.0
    fee: float = 0.0
    status: str = "new"            # new / filled / partial / rejected / cancelled
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    """成交回报。"""

    order_id: str = ""
    code: str = ""
    name: str = ""
    side: str = "buy"
    price: float = 0.0
    qty: int = 0
    amount: float = 0.0
    fee: float = 0.0
    ts: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DataMeta:
    """数据来源与新鲜度元信息（随每个响应返回，前端据此提示用户）。"""

    source: str = ""               # sina / eastmoney / csv / sample / cache / mixed
    stale: bool = False            # 是否为过期缓存
    offline: bool = False          # 是否已降级到本地数据
    as_of: str = ""                # 数据时点
    latency_ms: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
