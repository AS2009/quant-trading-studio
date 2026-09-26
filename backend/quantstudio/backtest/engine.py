# -*- coding: utf-8 -*-
"""回测引擎：逐 bar 事件驱动 + 真实 A 股约束（T+1 / 涨跌停 / 费用 / 滑点）。

执行流程（严格按序，README 照抄）
--------------------------------
1. **标的与基准**：``request.symbols`` → 策略自带标的 → 策略建议池（``strategy.default_symbols``）
   → ``settings.default_watchlist``（取第一个非空的）；基准取 ``request.benchmark``（默认 settings.benchmark）。
   日线一次取足：``交易日数(区间) + 预热 bar 数 + 2``，上限 ``settings.max_kline_days``（默认 1200）；
   预热 bar = ``max(spec.min_bars, 60)``，保证区间首日指标已可用，且**绝不引入未来数据**。
2. **对齐**：以各标的 / 基准交易日的并集为时间轴，逐标的做**前向填充**（停牌 / 缺失日沿用上一根 bar 的收盘价），
   前向填充只用于估值；**当日没有真实 bar 的标的不会成交**（停牌日不产生成交）。
3. **逐日循环**（``range_dates`` 升序）：
   ① ``account.unlock_all()``（T+1 解禁）→ ② 按当日价 ``mark_to_market``（供策略读取当日总资产）
   → ③ ``strategy.on_bar(ctx, bars)`` → ④ ``broker.track_bars`` + 逐笔 ``broker.execute``
   → ⑤ 成交后再次 ``mark_to_market`` → ⑥ 记录 ``{date, equity}``。
4. ``strategy.on_finish(ctx)``。
5. **指标与曲线**：在区间首日之前插入一个基准点（日期 = 区间首日的前一交易日，权益 = 初始资金），
   策略与基准净值都以该点归一化到 **1.0**，再计算 metrics / 月度收益 / 区间回撤 / 净值曲线。
6. 组装 :class:`~quantstudio.core.models.BacktestResult`（equity、期末持仓、warnings 等）。

可复现性
--------
不使用随机数、不依赖集合遍历顺序（所有遍历都按给定顺序或 ``sorted``），
相同 provider 数据 + 相同入参 → **逐字节相同**的结果（含 nav / trades / metrics）。
"""

import bisect
import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import Settings, get_settings
from ..core import costs
from ..core.calendar import TradingCalendar
from ..core.errors import InsufficientData, ValidationError
from ..core.models import (
    BacktestRequest,
    BacktestResult,
    Bar,
    FeeConfig,
    OrderRequest,
    Position,
    StrategySpec,
)
from .broker import SimulatedBroker
from .metrics import compute_metrics, drawdown_series, monthly_returns, normalize_nav
from .portfolio import SimAccount

__all__ = ["BacktestEngine", "BacktestContext", "LIQUID_FIELDS"]

# ctx.history / ctx.bars_since 支持的数值字段（Bar 的数值属性）
LIQUID_FIELDS = (
    "open", "high", "low", "close", "volume_wan", "amount_yi", "change_pct", "turnover_pct",
)


class _Series:
    """单标的的 bar 序列 + 单调游标（只向“今天及以前”推进，天然无未来函数）。"""

    __slots__ = ("code", "bars", "dates", "index")

    def __init__(self, code: str, bars: Sequence[Bar]):
        self.code = code
        self.bars: List[Bar] = list(bars or [])
        self.dates: List[str] = [str(b.date) for b in self.bars]
        self.index = -1

    def __len__(self) -> int:
        return len(self.bars)

    def seek(self, date: str) -> None:
        """把游标推进到 ``date``（含）之前最后一根 bar。"""
        while self.index + 1 < len(self.bars) and self.dates[self.index + 1] <= date:
            self.index += 1

    def bar(self) -> Optional[Bar]:
        return self.bars[self.index] if 0 <= self.index < len(self.bars) else None

    def is_real(self, date: str) -> bool:
        bar = self.bar()
        return bar is not None and str(bar.date) == date

    def close(self) -> float:
        bar = self.bar()
        try:
            return float(bar.close) if bar is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def value_at(self, date: str) -> float:
        """不移动游标的取数（用于区间基准点）：最后一根 ``date`` 及以前的收盘价。"""
        i = bisect.bisect_right(self.dates, date) - 1
        if i < 0:
            return 0.0
        try:
            return float(self.bars[i].close)
        except (TypeError, ValueError):
            return 0.0

    def history(self, field: str, n: int) -> List[float]:
        """截至今日（含）的最近 n 个字段值；严格不包含任何未来 bar。"""
        if field not in LIQUID_FIELDS:
            raise ValidationError(
                "history 不支持字段 %r，可用：%s" % (field, ", ".join(LIQUID_FIELDS)), field="field"
            )
        if self.index < 0 or n <= 0:
            return []
        start = max(0, self.index - int(n) + 1)
        out: List[float] = []
        for bar in self.bars[start:self.index + 1]:
            try:
                out.append(float(getattr(bar, field)))
            except (TypeError, ValueError):
                out.append(0.0)
        return out

    def bars_since(self, n: int) -> List[Bar]:
        """截至今日（含）的最近 n 根 bar。"""
        if self.index < 0 or n <= 0:
            return []
        start = max(0, self.index - int(n) + 1)
        return list(self.bars[start:self.index + 1])


class BacktestContext:
    """回测上下文（实现 ``core.interfaces.StrategyContext`` 协议）。

    **无未来函数保证**：``history`` / ``bars_since`` 都只返回截至 ``today``（含）的最近 n 个值；
    内部用单标的单调游标（:class:`_Series`）切片，任何未来 bar 都不可能进入返回值。
    停牌日（当日无真实 bar）沿用上一根 bar 的收盘价（前向填充），因此不会出现空窗。
    """

    def __init__(self, account: SimAccount, symbols: Sequence[str], fee: Optional[FeeConfig] = None):
        self._account = account
        self._fee = fee if isinstance(fee, FeeConfig) else FeeConfig()
        self.symbols: List[str] = list(symbols)
        self.lot_size = int(getattr(account, "lot_size", 100) or 100)
        self.today = ""
        self.logs: List[str] = []
        self._series: Dict[str, _Series] = {}

    # ---- 引擎内部 ----
    def prepare(self, series: Dict[str, List[Bar]], first_date: str) -> None:
        for code in sorted(series.keys()):
            item = _Series(code, series[code])
            item.seek(first_date)
            self._series[code] = item
        for code in self.symbols:
            self._series.setdefault(code, _Series(code, []))

    def advance(self, date: str) -> None:
        self.today = date
        for code in sorted(self._series.keys()):
            self._series[code].seek(date)

    def has_real_bar(self, symbol: str) -> bool:
        item = self._series.get(symbol)
        return bool(item is not None and item.is_real(self.today))

    def history(self, symbol: str, field: str = "close", n: int = 60) -> List[float]:
        """截至今日（含）的最近 n 个 ``field`` 值（升序，最后一根是今日）。

        ``field`` ∈ open/high/low/close/volume_wan/amount_yi/change_pct/turnover_pct。
        **严禁未来函数**：返回值不包含今日之后的任何 bar。
        """
        item = self._series.get(symbol)
        return item.history(field, n) if item is not None else []

    def bars_since(self, symbol: str, n: int) -> List[Bar]:
        """截至今日（含）的最近 n 根 bar（升序）。"""
        item = self._series.get(symbol)
        return item.bars_since(n) if item is not None else []

    def price(self, symbol: str) -> float:
        """今日（或最近可用）收盘价；无数据返回 0.0。"""
        item = self._series.get(symbol)
        return item.close() if item is not None else 0.0

    def bar(self, symbol: str) -> Optional[Bar]:
        item = self._series.get(symbol)
        return item.bar() if item is not None else None

    def position(self, symbol: str) -> Optional[Position]:
        """当前持仓（快照对象，改动它不会影响账户）。"""
        pos = self._account.positions.get(symbol)
        if pos is None or pos.qty <= 0:
            return None
        return self._to_model(pos)

    def positions(self) -> List[Position]:
        """全部持仓（数量 > 0，按代码升序，保证顺序稳定）。"""
        out: List[Position] = []
        for code in sorted(self._account.positions.keys()):
            pos = self._account.positions[code]
            if pos.qty > 0:
                out.append(self._to_model(pos))
        return out

    def cash(self) -> float:
        return self._account.cash

    def total_assets(self) -> float:
        return self._account.equity()

    def fee_config(self) -> FeeConfig:
        """本轮回测使用的费率配置（策略据此精确反解可买数量，无需自己猜费用）。"""
        return self._fee

    def log(self, message: str) -> None:
        """回测过程中的信息（会汇总到结果 warnings）。"""
        text = str(message or "").strip()
        if text:
            self.logs.append(text)

    # ---- 内部 ----
    def _to_model(self, pos) -> Position:
        price = pos.price if pos.price > 0 else self.price(pos.code)
        model = Position(
            code=pos.code,
            name=pos.name,
            qty=int(pos.qty),
            available_qty=int(pos.available_qty),
            cost=round(pos.cost, 4),
            price=round(price, 2),
            market_value=round(price * pos.qty, 2),
            cost_value=round(pos.cost * pos.qty, 2),
            total_pnl=round(price * pos.qty - pos.cost * pos.qty, 2),
            return_pct=round((price / pos.cost - 1.0) * 100.0, 2) if pos.cost > 0 else 0.0,
        )
        return model


class BacktestEngine:
    """真实回测引擎：provider 只负责给数据，撮合 / 记账 / 指标全部在本层完成。"""

    def __init__(
        self,
        provider,
        calendar: Optional[TradingCalendar] = None,
        settings: Optional[Settings] = None,
        broker: Optional[SimulatedBroker] = None,
        fill_mode: str = "close",
        risk_free_rate: float = 0.0,
    ):
        self.provider = provider
        self.calendar = calendar or TradingCalendar()
        self.settings = settings or get_settings()
        self.broker = broker
        self.fill_mode = fill_mode
        self.risk_free_rate = float(risk_free_rate or 0.0)

    # ------------------------------------------------------------------ 主入口
    def run(self, request: BacktestRequest, strategy) -> BacktestResult:
        """跑一次回测（同一份数据 + 同一入参 → 完全相同的结果）。"""
        if request is None:
            raise ValidationError("回测请求不能为空", field="request")
        spec = self._spec_of(strategy)
        warnings: List[str] = []

        self._apply_overrides(request, strategy, spec, warnings)
        symbols = self._resolve_symbols(request, strategy)
        min_bars = max(int(spec.min_bars or 1), 2)
        warmup = max(min_bars, 60)

        span, days, capped = self._fetch_days(request, warmup)
        if capped:
            warnings.append(
                "区间交易日 %d 天 + 预热 %d 天 超过 settings.max_kline_days=%d，已按上限截取"
                "（可设环境变量 QUANTSTUDIO_MAX_KLINE_DAYS 调大；腾讯日线实测最多约 6400 根）"
                % (span, warmup, self.settings.max_kline_days)
            )

        series: Dict[str, List[Bar]] = {}
        names: Dict[str, str] = {}
        for code in symbols:
            series[code] = self._kline(code, days, warnings)
            names[code] = self._resolve_name(code)
        bench_bars = self._kline(request.benchmark, days, warnings) if request.benchmark else []
        if len(bench_bars) < 2:
            warnings.append("基准 %s 数据不足，基准净值按 1.0 处理" % (request.benchmark or "未指定"))

        # ---- 数据充分性
        for code in symbols:
            bars = series.get(code) or []
            if len(bars) < min_bars:
                raise InsufficientData(
                    "%s 历史数据不足：可用 %d 个 bar，策略 %s(%s) 需要至少 %d 个"
                    "（请扩大 start/end 区间或减小指标周期；当前请求 %s ~ %s）"
                    % (code, len(bars), spec.name or spec.id, spec.id, min_bars,
                       request.start or "最早", request.end or "最新")
                )

        # ---- 时间轴与区间
        range_dates, _all_dates = self._timeline(series, bench_bars, request, warnings)
        if len(range_dates) < 2:
            start_label = request.start or (range_dates[0] if range_dates else "?")
            end_label = request.end or (range_dates[-1] if range_dates else "?")
            raise InsufficientData(
                "回测区间 %s ~ %s 内没有足够的共同交易日（%d 天），无法回测"
                % (start_label, end_label, len(range_dates))
            )
        first_date, last_date = range_dates[0], range_dates[-1]
        base_date = self._base_date(first_date)

        # ---- 账户 / 撮合 / 上下文（费率先归一化一次，account/broker/ctx 共用同一对象）
        fee = costs.normalize_fee(request.fee)
        request.fee = fee
        initial_cash = float(request.initial_cash or self.settings.initial_cash)
        lot_size = int(fee.lot_size or 100)
        account = SimAccount(initial_cash, lot_size=lot_size)
        broker = self.broker or SimulatedBroker(fee, self.calendar, fill_mode=self.fill_mode)
        broker.fee = fee
        broker.lot_size = lot_size
        broker.reset()

        ctx = BacktestContext(account, symbols, fee=fee)
        ctx.prepare({**series, **(dict([(request.benchmark, bench_bars)]) if bench_bars else {})}, first_date)

        strategy.on_start(ctx)

        bench_series = _Series(request.benchmark or "benchmark", bench_bars)
        bench_series.seek(first_date)
        bench_base = bench_series.value_at(base_date) or bench_series.close()
        if bench_base <= 0 and bench_series.bars:
            bench_base = float(bench_series.bars[0].close or 0.0)

        equity_curve: List[Dict[str, Any]] = []
        bench_curve: List[float] = []
        for date in range_dates:
            ctx.advance(date)
            # 支撑回测的基准标的也推进游标（仅供取数，不参与交易）
            if bench_bars:
                bench_series.seek(date)
            bars_today: Dict[str, Bar] = {}
            prices: Dict[str, float] = {}
            for code in symbols:
                bar = ctx.bar(code)
                if bar is not None:
                    bars_today[code] = bar
                    prices[code] = ctx.price(code)

            account.unlock_all()
            account.mark_to_market(prices)

            orders = strategy.on_bar(ctx, bars_today) or []
            broker.track_bars(date, bars_today)
            for order in orders:
                if order is None:
                    continue
                if not isinstance(order, OrderRequest):
                    raise ValidationError("策略返回的委托必须是 OrderRequest，当前为 %r" % type(order).__name__)
                bar = bars_today.get(order.code)
                if bar is None or not ctx.has_real_bar(order.code):
                    # 停牌 / 当日无行情：不成交（broker 会记录原因）
                    broker.execute(order, None, account, names.get(order.code, order.code), date)
                    continue
                broker.execute(order, bar, account, names.get(order.code, order.code), date)

            account.mark_to_market(prices)
            equity_curve.append({"date": date, "equity": round(account.equity(), 2)})
            bench_curve.append(round(bench_series.close(), 4) if bench_bars else 0.0)

        ctx.today = last_date
        strategy.on_finish(ctx)

        # ---- 结果曲线：插入区间前一交易日的基准点（权益 = 初始资金，净值 = 1.0）
        curve_dates = [base_date] + [item["date"] for item in equity_curve]
        equity_values = [round(initial_cash, 2)] + [item["equity"] for item in equity_curve]
        bench_values = [round(bench_base, 4)] + bench_curve
        if not bench_bars or bench_base <= 0:
            bench_values = [round(initial_cash, 2)] + [round(initial_cash, 2) for _ in equity_curve]

        strategy_nav = normalize_nav(equity_values)
        bench_nav = normalize_nav(bench_values)
        nav = [
            {"date": curve_dates[i], "strategy": strategy_nav[i], "benchmark": bench_nav[i]}
            for i in range(len(curve_dates))
        ]

        metrics = compute_metrics(
            dates=curve_dates,
            equity=equity_values,
            benchmark_equity=bench_values,
            trades=account.trades,
            initial_cash=initial_cash,
            total_fee=account.fees_total,
            turnover_value=account.turnover,
            risk_free_rate=self.risk_free_rate,
            warnings=warnings,
        )

        warnings.extend(ctx.logs)
        reject_summary = broker.reject_summary()
        if reject_summary:
            detail = "；".join(
                "%s × %d" % (reason, count) for reason, count in sorted(reject_summary.items())
            )
            warnings.append("共 %d 笔委托未成交：%s" % (len(broker.rejects), detail))

        result = BacktestResult(
            strategy=spec,
            request=request,
            range={"start": first_date, "end": last_date, "base_date": base_date, "benchmark": request.benchmark},
            nav=nav,
            drawdown=drawdown_series(curve_dates, equity_values),
            monthly=monthly_returns(curve_dates, equity_values),
            metrics=metrics,
            trades=list(account.trades),
            equity=[{"date": curve_dates[i], "equity": equity_values[i]} for i in range(len(curve_dates))],
            positions=[pos.to_dict() for pos in ctx.positions()],
            warnings=warnings,
        )
        # 便于调用方复用同一份账户明细（只读用途）
        result.account = account  # type: ignore[attr-defined]
        result.context = ctx      # type: ignore[attr-defined]
        return result

    # ------------------------------------------------------------------ 入参处理
    def _spec_of(self, strategy) -> StrategySpec:
        spec = getattr(strategy, "spec", None)
        if not isinstance(spec, StrategySpec):
            raise ValidationError("策略必须提供 StrategySpec（strategy.spec），当前为 %r" % type(spec).__name__)
        return spec

    def _apply_overrides(self, request: BacktestRequest, strategy, spec: StrategySpec,
                         warnings: List[str]) -> None:
        override = dict(request.params_override or {})
        if not override:
            return
        setter = getattr(strategy, "set_params", None)
        if callable(setter):
            try:
                merged = dict(getattr(strategy, "params", {}) or {})
                merged.update(override)
                params = setter(merged)
                spec.params = dict(params)
            except Exception as exc:  # 参数非法时给出可读提示，而不是让整次回测崩溃
                warnings.append("params_override 未生效（%s），使用策略默认参数" % exc)
        else:
            warnings.append("params_override 已忽略：该策略不支持运行时改参")

    def _resolve_symbols(self, request: BacktestRequest, strategy) -> List[str]:
        candidates = [
            list(request.symbols or []),
            list(getattr(strategy, "symbols", None) or []),
            list(getattr(strategy, "default_symbols", None) or []),
            list(self.settings.default_watchlist or []),
        ]
        for item in candidates:
            codes = [str(code).strip() for code in item if str(code).strip()]
            if codes:
                setter = getattr(strategy, "set_symbols", None)
                if callable(setter):
                    setter(codes)
                return codes
        raise ValidationError("未指定任何标的：请在请求或策略中给出 symbols", field="symbols")

    def _fetch_days(self, request: BacktestRequest, warmup: int) -> Tuple[int, int, bool]:
        """返回 ``(区间交易日数, 请求天数, 是否被 max_kline_days 截断)``。

        ``end`` 留空表示「最近交易日」（界面上的默认提示就是这句），这里必须把它补成
        真实终点再算区间：否则区间交易日数算不出来，会退化成 ``default_kline_days``
        （默认 250 根 ≈ 一年），**把用户填的起始日期整个丢掉** —— 表现就是「不管起点填多早，
        回测都只从最近一年开始」。
        """
        span = 0
        start = str(request.start or "").strip()
        end = str(request.end or "").strip()
        if not end:
            try:
                end = self.calendar.last_trading_day()
            except Exception:                      # noqa: BLE001 - 日历不可用时退回自然日
                end = datetime.date.today().isoformat()
        if start and end:
            try:
                span = len(self.calendar.trading_days(start, end))
            except Exception:
                span = 0
        if span <= 0:
            span = int(self.settings.default_kline_days)
        need = span + warmup + 2
        cap = max(int(self.settings.max_kline_days), warmup + 2)
        days = min(max(need, warmup + 2), cap)
        return span, days, need > cap

    def _kline(self, code: str, days: int, warnings: List[str]) -> List[Bar]:
        if not code:
            return []
        try:
            # 固定使用**乘法前复权**日线：`adjust="qfq"` 的口径是「后复权（hfq）序列整体缩放到
            # 最新真实价」（腾讯的 qfq 是减法复权，深历史会出负价，见 data/tencent.py 的
            # 「复权口径」）。末根收盘 == 今日真实价，现金 / 手数模拟才与现实同量级。
            raw = self.provider.kline(code, days=days, freq="day", adjust="qfq") or []
        except Exception as exc:  # 单个标的数据失败不影响整体（后续按数据不足处理）
            warnings.append("获取 %s 日线失败：%s" % (code, exc))
            return []
        by_date: Dict[str, Bar] = {}
        for bar in raw:
            if bar is None or not getattr(bar, "date", ""):
                continue
            try:
                if float(bar.close or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            by_date[str(bar.date)] = bar      # 同日重复取最后一根
        return [by_date[day] for day in sorted(by_date.keys())]

    def _resolve_name(self, code: str) -> str:
        try:
            name = self.provider.resolve_name(code)
        except Exception:
            name = None
        return str(name).strip() if name else code

    def _timeline(
        self,
        series: Dict[str, List[Bar]],
        bench_bars: Sequence[Bar],
        request: BacktestRequest,
        warnings: List[str],
    ) -> Tuple[List[str], List[str]]:
        all_dates = set()
        for code in sorted(series.keys()):
            for bar in series[code]:
                all_dates.add(str(bar.date))
        for bar in bench_bars:
            all_dates.add(str(bar.date))
        ordered = sorted(all_dates)
        if not ordered:
            raise InsufficientData("未取到任何日线数据，请检查标的代码与数据源")

        start = str(request.start or "").strip()
        end = str(request.end or "").strip()
        range_dates = [
            day for day in ordered
            if (not start or day >= start) and (not end or day <= end)
        ]
        if start and ordered and ordered[0] > start:
            warnings.append("区间起点 %s 早于可用数据起点 %s，已按实际数据回测" % (start, ordered[0]))
        elif not start:
            warnings.append("未指定 start，已按数据源返回的全部区间回测")
        if end and ordered and ordered[-1] < end:
            warnings.append("区间终点 %s 晚于可用数据终点 %s，已按实际数据回测" % (end, ordered[-1]))
        return range_dates, ordered

    def _base_date(self, first_date: str) -> str:
        """区间首日的前一交易日（用作净值基准点）。"""
        try:
            return self.calendar.shift(first_date, -1)
        except Exception:
            return first_date
