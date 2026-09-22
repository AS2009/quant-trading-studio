# -*- coding: utf-8 -*-
"""Sample Provider：把旧的确定性「示例数据引擎」包装成新的 :class:`DataProvider`。

数据来源
--------
``backend/data_engine.py``（**保留不删**）里的 ``DataEngine``：
- 优先 ``from data_engine import DataEngine``（``backend/`` 在 ``sys.path`` 上的常见情形）；
- 若失败则用 ``importlib`` 从 ``config.BASE_DIR/data_engine.py`` 动态加载，
  保证 ``quantstudio`` 包在任意工作目录下都能独立导入。

映射规则
--------
- 旧引擎的虚构代码 ``999001`` 这类 → 规范代码 ``999001.SH``（9 开头推断为沪市），名称保留；
- 旧引擎的指数代码 ``000001/399001/399006/000688`` → ``000001.SH/399001.SZ/399006.SZ/000688.SH``；
- 不在示例股票池里的代码（例如真实自选股 600519.SH）：用「稳定种子 + 随机数」合成一份
  确定性演示行情（同一进程/不同进程结果一致），名称优先取指数表，否则标注为 ``演示·<代码>``，
  这样**离线时前端仍有完整界面**。
- 所有产出的 ``source`` 一律是 ``"sample"``，``health()`` 明确说明这是演示数据。


K 线一致性（重要，回测依赖）
---------------------------
- 每个代码只生成**一条固定长度的确定性序列**（``_series``，种子只依赖代码），日期区间固定为
  ``[SAMPLE_SERIES_START(2023-01-03), 今天]`` 的工作日 → ``kline(days=N)`` 永远是这条序列的
  最后 N 根，因此**同一日期在任何 ``days`` 下价格完全相同**（不同 days 不再产生不同的历史价格）；
- 序列末根收盘价 = 该标的的示例快照价，成交量/成交额也取末根，保证「K 线末根 ↔ 行情快照」自洽；
- 指数用点位量级（:data:`INDEX_BASE_POINTS`），个股用 6-88 元量级；
- 跨天启动（新进程）时序列末尾会追加一根 bar，整条序列按同一比例缩放（约 ±2%），
  历史价格的相对形状不变。

注意：示例数据不是真实行情，固定时点为 ``DataEngine.as_of()``，绝不能用于投资决策。
"""

import datetime
import importlib
import math
import os
import random
import sys
import zlib
from typing import Any, Dict, List, Optional, Sequence

from ..config import BASE_DIR, Settings, get_settings
from ..core.errors import ProviderUnavailable, SymbolNotFound
from ..core.models import Bar, IndexQuote, MarketBreadth, Quote, SectorQuote
from . import symbols as sym

__all__ = ["SampleProvider", "load_data_engine"]

#: 旧引擎的指数代码 → 本项目规范代码
ENGINE_INDEX_CODES = {
    "000001": "000001.SH",
    "399001": "399001.SZ",
    "399006": "399006.SZ",
    "000688": "000688.SH",
}

#: 示例 K 线序列的固定起点（**不早于 2023-01-01**，保证默认回测区间 2023-01-03 起可用）
SAMPLE_SERIES_START = "2023-01-03"
#: 指数示例点位基准（避免指数用「个股价格量级」）
INDEX_BASE_POINTS = {
    "000001.SH": 3900.0,
    "000016.SH": 2900.0,
    "000300.SH": 4500.0,
    "000688.SH": 1650.0,
    "000852.SH": 7300.0,
    "000905.SH": 6800.0,
    "399001.SZ": 13700.0,
    "399005.SZ": 8500.0,
    "399006.SZ": 3400.0,
    "399673.SZ": 6500.0,
}

def load_data_engine():
    """返回旧引擎的 ``DataEngine`` 类（import 失败时按文件路径动态加载）。"""
    try:
        module = importlib.import_module("data_engine")
        engine_cls = getattr(module, "DataEngine", None)
        if engine_cls is not None:
            return engine_cls
    except ImportError:
        pass
    path = os.path.join(BASE_DIR, "data_engine.py")
    if not os.path.isfile(path):
        raise ProviderUnavailable("找不到示例数据引擎 data_engine.py：%s" % path)
    spec = importlib.util.spec_from_file_location("quantstudio_sample_data_engine", path)
    if spec is None or spec.loader is None:
        raise ProviderUnavailable("无法加载示例数据引擎：%s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("quantstudio_sample_data_engine", module)
    spec.loader.exec_module(module)
    return module.DataEngine


def _stable_seed(*parts: Any) -> int:
    """跨进程稳定种子（不使用受 PYTHONHASHSEED 影响的内置 hash）。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return zlib.crc32(raw) % 1000000


class SampleProvider:
    """离线兜底的示例数据源（确定性、非真实行情）。"""

    name = "sample"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings if settings is not None else get_settings()
        engine_cls = load_data_engine()
        self._engine = engine_cls()
        self.as_of = str(self._engine.as_of())
        #: 规范化代码 → 旧引擎原始行情字典
        self._quotes: Dict[str, Dict[str, Any]] = {}
        #: 规范化的指数代码 → 旧引擎指数字典
        #: 规范化的指数代码 → 旧引擎指数字典
        self._indices: Dict[str, Dict[str, Any]] = {}
        #: 代码 → 固定长度确定性 K 线序列（进程内缓存；任意 days 都是它的后缀）
        self._series_cache: Dict[str, List[Bar]] = {}
        #: 序列交易日（[2023-01-03, 今天]，进程内固定）
        self._series_dates_cache: Optional[List[str]] = None
        self._load_catalog()
    # ================================================================== 目录
    def _load_catalog(self) -> None:
        for row in self._engine.market_quotes() or []:
            code = self._normalize(row.get("code"))
            if code:
                self._quotes[code] = dict(row)
                self._quotes.setdefault(code.split(".", 1)[0], dict(row))
        overview = self._engine.market_overview() or {}
        for row in overview.get("indices") or []:
            raw = str(row.get("code") or "").strip()
            code = ENGINE_INDEX_CODES.get(raw) or self._normalize(raw)
            if code:
                self._indices[code] = dict(row)

    @staticmethod
    def _normalize(code: Any) -> str:
        try:
            return sym.normalize(code)
        except SymbolNotFound:
            return ""

    def _lookup(self, code: str) -> Optional[Dict[str, Any]]:
        row = self._quotes.get(code)
        if row is None:
            row = self._quotes.get(code.split(".", 1)[0])
        return row

    def _demo_name(self, code: str) -> str:
        return sym.INDEX_NAMES.get(code) or ("演示·" + code.split(".", 1)[0])

    # ================================================================== 快照
    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """示例行情（真实标的用确定性合成值，示例股票池用旧引擎数据）。"""
        out: List[Quote] = []
        for raw in symbols or []:
            code = self._normalize(raw)
            if not code or any(item.code == code for item in out):
                continue
            row = self._lookup(code)
            if row is not None:
                out.append(self._from_engine_quote(code, row))
            else:
                out.append(self._synth_quote(code))
        return out

    def _from_engine_quote(self, code: str, row: Dict[str, Any]) -> Quote:
        """旧引擎行情 → Quote（现价沿用引擎，开高低/量额取序列末根，保证与 K 线自洽）。"""
        price = float(row.get("price") or 0.0)
        prev_close = float(row.get("prev_close") or 0.0)
        series = self._series(code)
        last = series[-1] if series else None
        return Quote(
            code=code,
            name=str(row.get("name") or self._demo_name(code)),
            price=price,
            prev_close=prev_close,
            open=last.open if last else price,
            high=last.high if last else price,
            low=last.low if last else price,
            change=round(price - prev_close, 4),
            change_pct=float(row.get("change_pct") or 0.0),
            volume_wan=last.volume_wan if last else float(row.get("volume_wan") or 0.0),
            amount_yi=last.amount_yi if last else float(row.get("amount_yi") or 0.0),
            turnover_pct=float(row.get("turnover_pct") or 0.0),
            pe_ttm=float(row["pe_ttm"]) if row.get("pe_ttm") is not None else None,
            pb=float(row["pb"]) if row.get("pb") is not None else None,
            market_cap_yi=float(row["market_cap_yi"]) if row.get("market_cap_yi") is not None else None,
            ts=self.as_of,
            source=self.name,
        )

    # ------------------------------------------------------------------ 固定序列
    def _series_dates(self) -> List[str]:
        """示例序列的交易日：``[SAMPLE_SERIES_START, 今天]`` 的工作日（进程内固定）。"""
        if self._series_dates_cache is None:
            try:
                cursor = datetime.date(*[int(part) for part in SAMPLE_SERIES_START.split("-")])
            except (TypeError, ValueError):      # pragma: no cover - 常量写错才会发生
                cursor = datetime.date(2023, 1, 3)
            today = datetime.date.today()
            dates: List[str] = []
            while cursor <= today:
                if cursor.weekday() < 5:
                    dates.append(cursor.isoformat())
                cursor += datetime.timedelta(days=1)
            # 超过 settings.max_kline_days 时只保留最近的一段（起点仍不早于 2023-01-01）
            try:
                max_days = int(getattr(self.settings, "max_kline_days", 0) or 0)
            except (TypeError, ValueError):
                max_days = 0
            if max_days > 0 and len(dates) > max_days:
                dates = dates[-max_days:]
            self._series_dates_cache = dates
        return self._series_dates_cache

    def _series(self, code: str) -> List[Bar]:
        """固定长度的确定性 K 线序列（**种子只依赖代码**）。

        一致性保证（核心验收点）：
        - 序列长度与日期集合只由 ``SAMPLE_SERIES_START``、今天、``max_kline_days`` 决定，
          与调用方的 ``days`` **无关** → ``kline(days=N)`` 永远是本序列的最后 N 根，
          同一日期在任何 ``days`` 下价格完全相同；
        - 末根收盘价 = 该标的的示例快照价（``_series_target``），量额也用末根，
          保证「K 线末根 ↔ 行情快照」自洽；
        - 指数用点位量级（:data:`INDEX_BASE_POINTS`），不会出现「指数 12.34 点」。

        注意：跨天启动（新进程、日期 +1）时序列会追加一根 bar，整条序列按
        ``target / raw[-1]`` 做同一比例缩放（约 ±2%），历史价格的**相对形状**不变。
        """
        cached = self._series_cache.get(code)
        if cached is not None:
            return cached
        dates = self._series_dates()
        target = self._series_target(code)
        is_index = sym.kind_of(code) == "index"
        rng = random.Random(_stable_seed("sample-series", code))
        sigma = 0.010 if is_index else 0.020
        raw: List[float] = []
        level = 1.0
        for _ in dates:
            level *= math.exp(rng.gauss(0.0003, sigma))
            raw.append(level)
        scale = (target / raw[-1]) if raw and raw[-1] else 1.0
        base_volume = self._base_volume(code)
        base_amount_yi = self._base_amount_yi(code)

        bars: List[Bar] = []
        for day, value in zip(dates, raw):
            close = value * scale
            open_price = close * (1 + rng.uniform(-0.012, 0.012) * (0.5 if is_index else 1.0))
            high = max(open_price, close) * (1 + rng.uniform(0.001, 0.012 if is_index else 0.025))
            low = min(open_price, close) * (1 - rng.uniform(0.001, 0.012 if is_index else 0.025))
            volume = max(1.0, base_volume * rng.uniform(0.5, 1.6))
            bars.append(Bar(
                date=day,
                open=round(open_price, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume_wan=round(volume, 2),
                amount_yi=(round(base_amount_yi * volume / base_volume, 4) if is_index
                           else round(volume * 1e4 * 100 * close / 1e8, 4)),
                change_pct=0.0, turnover_pct=0.0,
            ))
        # 末根收盘价严格等于快照价（缩放已保证，这里再对齐一次四舍五入误差）
        if bars:
            bars[-1].close = round(target, 2)
        for index, bar in enumerate(bars):
            prev = bars[index - 1].close if index else 0.0
            bar.change_pct = round((bar.close / prev - 1) * 100, 4) if prev else 0.0
        self._series_cache[code] = bars
        return bars

    def _series_target(self, code: str) -> float:
        """序列末根收盘价（= 示例快照价）：指数/股票池用旧引擎值，其它代码按稳定种子合成。"""
        index_row = self._indices.get(code)
        if index_row is not None:
            point = float(index_row.get("point") or 0.0)
            if point > 0:
                return point
        row = self._lookup(code)
        if row is not None:
            price = float(row.get("price") or 0.0)
            if price > 0:
                return price
        return self._synth_base_price(code)

    def _synth_base_price(self, code: str) -> float:
        """示例标的的基准价（指数用点位量级，个股用 6-88 元量级）。"""
        rng = random.Random(_stable_seed("sample-base", code))
        if sym.kind_of(code) == "index":
            base = INDEX_BASE_POINTS.get(code) or rng.uniform(2500.0, 6000.0)
            return round(base * (1 + rng.uniform(-0.02, 0.02)), 2)
        return round(rng.uniform(6.0, 88.0), 2)

    def _base_amount_yi(self, code: str) -> float:
        """示例成交额基数（亿元）：指数用「两市成交额」量级，个股用市值×换手率的量级。"""
        rng = random.Random(_stable_seed("sample-amount", code))
        if sym.kind_of(code) == "index":
            return round(rng.uniform(2500.0, 8000.0), 2)
        return round(rng.uniform(2.0, 60.0), 2)

    def _base_volume(self, code: str) -> float:
        """示例日成交量基数（万手）。"""
        rng = random.Random(_stable_seed("sample-volume", code))
        if sym.kind_of(code) == "index":
            return round(rng.uniform(1e4, 6e4), 2)
        return round(rng.uniform(20.0, 120.0), 2)

    def _synth_quote(self, code: str) -> Quote:
        """为示例股票池之外的代码合成确定性演示行情（与固定序列末根完全自洽）。"""
        series = self._series(code)
        last = series[-1] if series else None
        prev = series[-2] if len(series) > 1 else None
        price = last.close if last else self._synth_base_price(code)
        prev_close = prev.close if prev else price
        rng = random.Random(_stable_seed("sample-quote", code))
        turnover_pct = round(rng.uniform(0.6, 6.5), 2)
        market_cap_yi = round(rng.uniform(80, 2400), 2)
        return Quote(
            code=code,
            name=self._demo_name(code),
            price=price,
            prev_close=prev_close,
            open=last.open if last else price,
            high=last.high if last else price,
            low=last.low if last else price,
            change=round(price - prev_close, 4),
            change_pct=round((price / prev_close - 1) * 100, 4) if prev_close else 0.0,
            volume_wan=last.volume_wan if last else 0.0,
            amount_yi=last.amount_yi if last else 0.0,
            turnover_pct=turnover_pct,
            pe_ttm=round(rng.uniform(10, 70), 2),
            pb=round(rng.uniform(1.0, 8.0), 2),
            market_cap_yi=market_cap_yi,
            ts=self.as_of,
            source=self.name,
        )

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照（旧引擎的 4 个指数优先，其它代码按固定序列合成点位）。

        点位数/昨收/量额一律取自固定序列末两根，保证「指数快照 ↔ 指数 K 线」自洽。
        """
        out: List[IndexQuote] = []
        for raw in symbols or []:
            code = self._normalize(raw)
            if not code or any(item.code == code for item in out):
                continue
            row = self._indices.get(code)
            if row is not None:
                series = self._series(code)
                last = series[-1] if series else None
                prev = series[-2] if len(series) > 1 else None
                if last is None:
                    continue
                prev_close = prev.close if prev else last.close
                out.append(IndexQuote(
                    code=code,
                    name=str(row.get("name") or self._demo_name(code)),
                    point=last.close,
                    prev_close=prev_close,
                    change=round(last.close - prev_close, 2),
                    change_pct=round((last.close / prev_close - 1) * 100, 4) if prev_close else 0.0,
                    amount_yi=last.amount_yi,
                    volume_wan=last.volume_wan,
                    source=self.name,
                ))
                continue
            quote = self._synth_quote(code)
            out.append(IndexQuote(
                code=code, name=quote.name, point=quote.price, prev_close=quote.prev_close,
                change=quote.change, change_pct=quote.change_pct,
                amount_yi=quote.amount_yi, volume_wan=quote.volume_wan,
                source=self.name,
            ))
        return out

    # ================================================================== K 线
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        """示例 K 线：永远返回固定序列的最后 ``days`` 根（任意 ``days`` 下同日价格一致）。

        ``freq=week/month`` 由日线序列按自然周 / 自然月聚合得到，同样与该序列自洽。
        """
        code = self._normalize(symbol)
        if not code:
            raise ProviderUnavailable("示例数据源无法识别标的：%r" % (symbol,))
        try:
            limit = max(1, int(days))
        except (TypeError, ValueError):
            limit = 250
        series = self._series(code)
        if not series:
            raise ProviderUnavailable("示例数据源暂无 %s 的 K 线" % code)
        freq_key = str(freq or "day").strip().lower()
        if freq_key in ("week", "weekly", "周", "周线"):
            series = self._aggregate(series, "week")
        elif freq_key in ("month", "monthly", "月", "月线"):
            series = self._aggregate(series, "month")
        return series[-limit:]

    @staticmethod
    def _aggregate(bars: List[Bar], freq: str) -> List[Bar]:
        """日线 → 周线 / 月线（开=首、高低=极值、收=末、量额=求和）。"""
        out: List[Bar] = []
        current: Optional[Bar] = None
        bucket = ""
        for bar in bars:
            try:
                parts = [int(part) for part in str(bar.date)[:10].split("-")]
                day = datetime.date(parts[0], parts[1], parts[2])
            except (TypeError, ValueError, IndexError):
                continue
            key = ("%d-W%02d" % day.isocalendar()[:2]) if freq == "week" else str(bar.date)[:7]
            if current is None or key != bucket:
                if current is not None:
                    out.append(current)
                current = Bar(
                    date=bar.date, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                    volume_wan=bar.volume_wan, amount_yi=bar.amount_yi,
                    change_pct=bar.change_pct, turnover_pct=bar.turnover_pct,
                )
                bucket = key
            else:
                current.high = max(current.high, bar.high)
                current.low = min(current.low, bar.low)
                current.close = bar.close
                current.date = bar.date
                current.volume_wan = round(current.volume_wan + bar.volume_wan, 4)
                current.amount_yi = round(current.amount_yi + bar.amount_yi, 6)
                current.change_pct = bar.change_pct
                current.turnover_pct = bar.turnover_pct
        if current is not None:
            out.append(current)
        return out

    # ================================================================== 板块
    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """由旧引擎的板块数组合成（``change_pct`` / ``net_inflow_yi`` / 领涨股均来自引擎）。"""
        try:
            top = max(1, int(limit))
        except (TypeError, ValueError):
            top = 20
        rows = self._engine.market_sectors() or []
        out: List[SectorQuote] = []
        for index, row in enumerate(rows):
            name = str(row.get("name") or "")
            if not name:
                continue
            rng = random.Random(_stable_seed("sample-sector", name))
            up = rng.randint(12, 90)
            down = rng.randint(5, 80)
            out.append(SectorQuote(
                code="SMP%02d" % (index + 1),       # 演示板块代码（非东财 BK 代码）
                name=name,
                change_pct=float(row.get("change_pct") or 0.0),
                net_inflow_yi=float(row.get("net_inflow_yi") or 0.0),
                up_count=up,
                down_count=down,
                leading_stock=str(row.get("leading_stock") or ""),
                leading_code="",
            ))
        out.sort(key=lambda item: item.change_pct, reverse=True)
        return out[:top] if top < len(out) else out

    # ================================================================== 广度
    def breadth(self) -> MarketBreadth:
        """由旧引擎的 ``market_overview()`` 合成市场广度（含两市成交额与主力净流入）。"""
        overview = self._engine.market_overview() or {}
        raw = overview.get("breadth") or {}
        up = int(raw.get("up") or 0)
        down = int(raw.get("down") or 0)
        flat = int(raw.get("flat") or 0)
        return MarketBreadth(
            up=up,
            down=down,
            flat=flat,
            limit_up=0,
            limit_down=0,
            total=up + down + flat,
            total_amount_yi=float(overview.get("total_amount_yi") or 0.0),
            main_net_inflow_yi=overview.get("main_net_inflow_yi"),
            north_net_inflow_yi=overview.get("north_net_inflow_yi"),
            source=self.name,
        )

    # ================================================================== 名称 / 自检
    def resolve_name(self, symbol: str) -> Optional[str]:
        code = self._normalize(symbol)
        if not code:
            return None
        row = self._lookup(code)
        if row is not None and row.get("name"):
            return str(row["name"])
        index_row = self._indices.get(code)
        if index_row is not None and index_row.get("name"):
            return str(index_row["name"])
        return sym.INDEX_NAMES.get(code)

    def health(self) -> Dict[str, Any]:
        """自检：明确提示这是示例（演示）数据，不是真实行情。"""
        stocks = len({key for key in self._quotes if "." in key})
        return {
            "ok": True,
            "detail": (
                "示例（演示）数据：由 backend/data_engine.py 确定性生成，非真实行情，"
                "仅用于离线兜底与界面演示；数据时点固定为 %s，演示标的 %d 只"
                % (self.as_of, stocks)
            ),
            "latency_ms": 0,
            "source": self.name,
            "as_of": self.as_of,
            "demo": True,
        }
