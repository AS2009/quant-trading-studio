# -*- coding: utf-8 -*-
"""绩效指标（纯标准库、纯函数）——口径与 README 完全一致。

输入约定
--------
- ``dates`` / ``equity`` / ``benchmark_equity`` 等长且按日期升序；
  **第 0 个点是区间基准点**（回测引擎会插入「区间首日的前一日」，权益 = 初始资金），
  因此 ``交易天数 = len(equity) - 1``，日收益个数 = 交易天数。
- ``equity`` 为**总资产**（现金 + 持仓市值 + 冻结资金），单位元。
- 所有百分比字段均为**百分数**（10.0 表示 +10%）；``risk_free_rate`` 为**年化小数**（0.02 表示 2%）。
- 所有除法都有零分母 / 数据不足保护：返回 0.0，绝不抛异常、绝不产生 NaN / Infinity。

指标口径
--------
====================  ======================================================================
总收益率               ``equity[-1] / equity[0] - 1``
年化收益率             ``(1 + 总收益) ** (252 / 交易天数) - 1``（几何；交易天数 < 252 时按实际天数）
                       交易天数 < 2 或权益出现非正数时返回 0 并写入 ``warnings``
日收益率               ``equity[i] / equity[i-1] - 1``
年化波动率             日收益率**样本标准差（n-1）** × √252
夏普比率               ``(年化收益 - 无风险利率) / 年化波动率``（分母 0 → 0）
索提诺比率             ``(年化收益 - 无风险利率) / (下行日收益标准差 × √252)``
                       下行日收益 = 负的日收益，样本标准差（负收益不足 2 个 → 0）
卡玛比率               ``年化收益 / 最大回撤``（回撤 0 → 0）
最大回撤               ``max(1 - equity / 历史峰值)``，另给回撤起点（峰值日）与终点（谷值日）
胜率                   盈利平仓笔数 / 平仓笔数（平仓 = 卖出且 ``pnl`` 非空）
盈亏比                 平均盈利 / |平均亏损|（缺盈利或缺亏损 → 0）
交易次数（trade_count） 平仓笔数（卖出且 ``pnl`` 非空）
换手率                 总成交额 / 平均总资产（平均总资产 = 逐日权益均值）
基准收益               ``benchmark_equity[-1] / benchmark_equity[0] - 1``
Beta                   ``cov(策略日收益, 基准日收益) / var(基准日收益)``（样本矩 n-1；基准方差 0 → 0）
Alpha                  ``(策略年化 - 无风险) - beta × (基准年化 - 无风险)``
====================  ======================================================================
"""

import math
from typing import Any, Dict, List, Optional, Sequence

from ..core.models import BacktestMetrics, Trade

_DAYS_PER_YEAR = 252.0
# 年化收益的截断范围：避免极短区间 / 极端收益产生 Infinity
_ANNUAL_MIN = -1.0
_ANNUAL_MAX = 1e6


# --------------------------------------------------------------------------- 基础工具
def _finite(value: Any, default: float = 0.0) -> float:
    """把任意输入变成有限浮点数（NaN / Infinity / 非数字 → default）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _is_finite(value: Any) -> bool:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(num) and not math.isinf(num)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct(value: float, digits: int = 2) -> float:
    """小数 → 百分数（四舍五入，保证有限）。"""
    return round(_finite(value) * 100.0, digits)


def _mean(values: Sequence[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    return sum(values) / n


def _stdev(values: Sequence[float]) -> float:
    """样本标准差（分母 n-1）；n < 2 或方差非正返回 0.0。"""
    n = len(values)
    if n < 2:
        return 0.0
    avg = _mean(values)
    var = sum((v - avg) ** 2 for v in values) / (n - 1)
    if var <= 0 or not _is_finite(var):
        return 0.0
    return math.sqrt(var)


def _daily_returns(series: Sequence[float]) -> List[float]:
    """逐日收益率（前值为非正时记 0，避免除零）。"""
    out: List[float] = []
    for i in range(1, len(series)):
        prev = series[i - 1]
        out.append(series[i] / prev - 1.0 if prev > 0 else 0.0)
    return out


def _annualize(total_return: float, trading_days: int) -> Optional[float]:
    """几何年化；数据不足（< 2 个交易日）或终值非正时返回 None。"""
    if trading_days < 2:
        return None
    base = 1.0 + total_return
    if base <= 0:
        return None
    try:
        value = base ** (_DAYS_PER_YEAR / trading_days) - 1.0
    except (OverflowError, ValueError):
        return None
    if not _is_finite(value):
        return None
    return _clamp(value, _ANNUAL_MIN, _ANNUAL_MAX)


def _drawdown(dates: Sequence[str], equity: Sequence[float]):
    """返回 ``(最大回撤小数, 回撤起点日, 回撤终点日, 逐日回撤列表)``。"""
    peak = equity[0] if len(equity) else 0.0
    peak_date = dates[0] if len(dates) else ""
    worst = 0.0
    worst_start = peak_date
    worst_end = peak_date
    series: List[Dict[str, Any]] = []
    for i, value in enumerate(equity):
        day = dates[i] if i < len(dates) else ""
        if value > peak:
            peak = value
            peak_date = day
        dd = (1.0 - value / peak) if peak > 0 else 0.0
        dd = max(dd, 0.0)
        if dd > worst:
            worst = dd
            worst_start = peak_date
            worst_end = day
        series.append({"date": day, "dd_pct": round(dd * 100.0, 2)})
    return worst, worst_start, worst_end, series


# --------------------------------------------------------------------------- 主函数
def compute_metrics(
    dates: Sequence[str],
    equity: Sequence[float],
    benchmark_equity: Optional[Sequence[float]] = None,
    trades: Optional[Sequence[Trade]] = None,
    initial_cash: float = 0.0,
    total_fee: float = 0.0,
    turnover_value: float = 0.0,
    risk_free_rate: float = 0.0,
    warnings: Optional[List[str]] = None,
) -> BacktestMetrics:
    """计算全部绩效指标（口径见模块 docstring）。

    ``warnings``：可选列表，数据不足 / 口径降级等信息会追加进去（由调用方展示）。
    """
    log = warnings if warnings is not None else []
    dates = list(dates or [])
    values = [_finite(v) for v in (equity or [])]
    trades = list(trades or [])
    rf = _finite(risk_free_rate)

    metrics = BacktestMetrics(
        start=dates[0] if dates else "",
        end=dates[-1] if dates else "",
        total_fee=round(_finite(total_fee), 2),
    )
    n = len(values)
    if n < 2:
        log.append("权益序列不足 2 个点，无法计算收益类指标（已全部置 0）")
        return metrics
    if len(dates) != n:
        log.append("日期序列与权益序列长度不一致（%d vs %d），缺失日期以空串填充" % (len(dates), n))

    metrics.trading_days = n - 1
    base = values[0] if values[0] > 0 else _finite(initial_cash)
    if base <= 0:
        log.append("权益基准点非正数，收益类指标按 0 处理")
        base = 0.0
    total_return = (values[-1] / base - 1.0) if base > 0 else 0.0
    metrics.total_return_pct = _pct(total_return)

    annual = _annualize(total_return, metrics.trading_days)
    if annual is None:
        log.append("交易日数 %d 不足 2 天或区间终值非正，年化收益按 0 处理" % metrics.trading_days)
        annual = 0.0
    metrics.annual_return_pct = _pct(annual)

    # ---- 回撤
    max_dd, dd_start, dd_end, _dd_series = _drawdown(dates, values)
    metrics.max_drawdown_pct = _pct(max_dd)
    metrics.max_drawdown_start = dd_start
    metrics.max_drawdown_end = dd_end

    # ---- 波动率 / 夏普 / 索提诺 / 卡玛
    returns = _daily_returns(values)
    volatility = _stdev(returns) * math.sqrt(_DAYS_PER_YEAR)
    metrics.volatility_pct = _pct(volatility)
    excess = annual - rf
    metrics.sharpe = round(excess / volatility, 2) if volatility > 0 else 0.0

    downside = [r for r in returns if r < 0]
    down_vol = _stdev(downside) * math.sqrt(_DAYS_PER_YEAR)
    metrics.sortino = round(excess / down_vol, 2) if down_vol > 0 else 0.0
    metrics.calmar = round(annual / max_dd, 2) if max_dd > 0 else 0.0

    # ---- 交易统计（平仓 = 卖出且 pnl 非空）
    pnls = [
        _finite(t.pnl) for t in trades
        if getattr(t, "side", "") == "sell" and getattr(t, "pnl", None) is not None
    ]
    metrics.trade_count = len(pnls)
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        metrics.win_rate_pct = round(len(wins) / len(pnls) * 100.0, 2)
        if wins and losses:
            metrics.profit_loss_ratio = round(abs(_mean(wins) / _mean(losses)), 2)

    # ---- 换手率（总成交额 / 平均总资产）
    avg_equity = _mean(values)
    metrics.turnover_pct = _pct(_finite(turnover_value) / avg_equity) if avg_equity > 0 else 0.0

    # ---- 基准 / Beta / Alpha
    bench = [_finite(v) for v in (benchmark_equity or [])]
    if len(bench) >= 2 and bench[0] > 0:
        metrics.benchmark_return_pct = _pct(bench[-1] / bench[0] - 1.0)
        k = min(len(bench), n)
        bench_used = bench[:k]
        bench_annual = _annualize(bench_used[-1] / bench_used[0] - 1.0, k - 1)
        if bench_annual is None:
            bench_annual = 0.0
        strat_returns = _daily_returns(values[:k])
        bench_returns = _daily_returns(bench_used)
        pairs = min(len(strat_returns), len(bench_returns))
        if pairs >= 2:
            xs = bench_returns[:pairs]
            ys = strat_returns[:pairs]
            mx, my = _mean(xs), _mean(ys)
            var_x = sum((x - mx) ** 2 for x in xs) / (pairs - 1)
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (pairs - 1)
            metrics.beta = round(cov / var_x, 3) if var_x > 0 else 0.0
        metrics.alpha_pct = _pct(excess - metrics.beta * (bench_annual - rf))
    elif bench:
        log.append("基准权益序列不足 2 个点，基准收益 / Alpha / Beta 按 0 处理")

    return metrics


# --------------------------------------------------------------------------- 序列工具
def monthly_returns(dates: Sequence[str], equity: Sequence[float]) -> List[Dict[str, Any]]:
    """月度收益：按 ``YYYY-MM`` 取**当月最后一个权益值**，逐月复利。

    首月收益相对第 0 个点（区间基准点）；**基准点所在月份不单独成月**（它是基准，不产生收益）。
    返回 ``[{"month": "2024-01", "ret_pct": 3.21}, ...]``（升序，ret_pct 为百分数）。
    """
    dates = list(dates or [])
    values = [_finite(v) for v in (equity or [])]
    n = min(len(dates), len(values))
    if n < 2:
        return []
    out: List[Dict[str, Any]] = []
    prev_value = values[0]        # 区间基准点
    cur_month = str(dates[1] or "")[:7]
    month_last = values[1]
    for i in range(2, n):
        month = str(dates[i] or "")[:7]
        if month != cur_month:
            out.append({
                "month": cur_month,
                "ret_pct": round((month_last / prev_value - 1.0) * 100.0, 2) if prev_value > 0 else 0.0,
            })
            prev_value = month_last
            cur_month = month
        month_last = values[i]
    if cur_month:
        out.append({
            "month": cur_month,
            "ret_pct": round((month_last / prev_value - 1.0) * 100.0, 2) if prev_value > 0 else 0.0,
        })
    return out


def drawdown_series(dates: Sequence[str], equity: Sequence[float]) -> List[Dict[str, Any]]:
    """逐日回撤序列 ``[{"date": ..., "dd_pct": ...}]``（相对历史峰值，百分数，0 ~ 100）。"""
    dates = list(dates or [])
    values = [_finite(v) for v in (equity or [])]
    n = min(len(dates), len(values))
    if n == 0:
        return []
    _worst, _start, _end, series = _drawdown(dates[:n], values[:n])
    return series


def normalize_nav(values: Sequence[float]) -> List[float]:
    """把序列首值归一化为 1.0（首值非正时整体返回 1.0，保证可作图且无 NaN）。"""
    nums = [_finite(v) for v in (values or [])]
    if not nums:
        return []
    base = nums[0]
    if base <= 0:
        return [1.0 for _ in nums]
    return [round(v / base, 6) for v in nums]
