# -*- coding: utf-8 -*-
"""CSV Provider：读取用户自有的本地行情文件（离线可用，可作精确数据的补充来源）。

目录结构（``settings.csv_dir``，默认 ``backend/data/csv``）
----------------------------------------------------------
``quotes.csv``
    列：``code,name,price,prev_close,open,high,low,volume,amount,ts``
    （``volume`` 单位 **股**、``amount`` 单位 **元**；可选列 ``change_pct,turnover_pct``）
``kline/{code}.csv`` 或单文件 ``kline_all.csv``（多一列 ``code``）
    列：``date,open,high,low,close,volume,amount``
    （``volume`` 单位 **手**、``amount`` 单位 **元**；可选列 ``change_pct,turnover_pct``）
    ``{code}`` 支持 ``600519.SH.csv`` / ``600519.csv`` / ``sh600519.csv`` 三种命名。
``sectors.csv``
    列：``code,name,change_pct,net_inflow,up_count,down_count,leading_stock``
``breadth.csv``
    两列 ``key,value``，key 取 ``up/down/flat/limit_up/limit_down/total``
    ``total_amount/main_net_inflow/north_net_inflow``。

金额容错
--------
``net_inflow`` / ``total_amount`` / ``main_net_inflow`` 这类金额列没有强制单位：
绝对值 **小于 1e5** 视为「亿元」（手工填 ``35.2`` 很自然），否则视为「元」。

缺失行为
--------
目录不存在或对应文件缺失时，该方法抛 :class:`ProviderUnavailable`，让上层继续降级；
K 线按 ``days`` 截尾返回（``freq=week/month`` 时由日线聚合）。
"""

import csv
import datetime
import os
import time
from typing import Any, Dict, List, Optional, Sequence

from ..config import Settings, get_settings
from ..core.errors import ProviderUnavailable, SymbolNotFound
from ..core.models import Bar, IndexQuote, MarketBreadth, Quote, SectorQuote
from . import symbols as sym

__all__ = ["CsvProvider"]

#: 金额列的单位推断阈值：|v| < 1e5 视为亿元
_YI_THRESHOLD = 1e5


class CsvProvider:
    """本地 CSV 数据源（用户自有数据导入路径）。"""

    name = "csv"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings if settings is not None else get_settings()
        self.csv_dir = getattr(self.settings, "csv_dir", "")
        self.kline_dir = os.path.join(self.csv_dir, "kline") if self.csv_dir else ""
        self.quotes_path = os.path.join(self.csv_dir, "quotes.csv") if self.csv_dir else ""
        self.kline_all_path = os.path.join(self.csv_dir, "kline_all.csv") if self.csv_dir else ""
        self.sectors_path = os.path.join(self.csv_dir, "sectors.csv") if self.csv_dir else ""
        self.breadth_path = os.path.join(self.csv_dir, "breadth.csv") if self.csv_dir else ""

    # ================================================================== 读文件
    def _rows(self, path: str, label: str) -> List[Dict[str, str]]:
        """读 CSV（utf-8-sig 兼容 BOM）；文件缺失抛 ProviderUnavailable。"""
        if not path or not os.path.isfile(path):
            raise ProviderUnavailable("CSV 数据文件不存在：%s（%s）" % (path, label))
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                rows = []
                for raw in reader:
                    if raw is None:
                        continue
                    row = {}
                    for key, value in raw.items():
                        if key is None:
                            continue
                        row[str(key).strip().lower()] = ("" if value is None else str(value).strip())
                    if any(row.values()):
                        rows.append(row)
        except (OSError, ValueError, csv.Error) as exc:
            raise ProviderUnavailable("读取 CSV 失败：%s（%s: %s）" % (path, type(exc).__name__, exc))
        if not rows:
            raise ProviderUnavailable("CSV 文件为空：%s" % path)
        return rows

    # ================================================================== 数值工具
    @staticmethod
    def _f(row: Dict[str, str], key: str, default: float = 0.0) -> float:
        text = (row.get(key) or "").strip().replace(",", "")
        if not text or text in ("-", "--"):
            return default
        try:
            return float(text)
        except ValueError:
            return default

    @staticmethod
    def _i(row: Dict[str, str], key: str, default: int = 0) -> int:
        try:
            return int(float((row.get(key) or "").strip()))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_yi(value: float) -> float:
        """金额列单位推断：|v| < 1e5 视为亿元，否则视为元。"""
        return value if abs(value) < _YI_THRESHOLD else value / 1e8

    # ================================================================== 快照
    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """读取 ``quotes.csv``（``volume`` 股、``amount`` 元）。"""
        rows = self._rows(self.quotes_path, "实时快照")
        wanted = self._wanted(symbols)
        out: List[Quote] = []
        for row in rows:
            code = self._norm(row.get("code"))
            if not code:
                continue
            if wanted and code not in wanted:
                continue
            price = self._f(row, "price")
            prev_close = self._f(row, "prev_close")
            if price <= 0 and prev_close <= 0:
                continue
            change = price - prev_close if prev_close else 0.0
            out.append(Quote(
                code=code,
                name=(row.get("name") or sym.INDEX_NAMES.get(code) or ""),
                price=price,
                prev_close=prev_close,
                open=self._f(row, "open"),
                high=self._f(row, "high"),
                low=self._f(row, "low"),
                change=round(change, 4),
                change_pct=self._f(row, "change_pct", round(change / prev_close * 100, 4) if prev_close else 0.0),
                volume_wan=self._f(row, "volume") / 100.0 / 1e4,     # 股 → 万手
                amount_yi=self._f(row, "amount") / 1e8,              # 元 → 亿元
                turnover_pct=self._f(row, "turnover_pct"),
                ts=(row.get("ts") or ""),
                source=self.name,
            ))
        if not out:
            raise ProviderUnavailable(
                "quotes.csv 中找不到请求的标的：%s" % (", ".join(sorted(wanted)) or "-")
            )
        return out

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """由 ``quotes.csv`` 合成指数快照（``price`` 即点位）。"""
        quotes = self.latest_quotes(symbols)
        return [IndexQuote(
            code=item.code,
            name=item.name,
            point=item.price,
            prev_close=item.prev_close,
            change=item.change,
            change_pct=item.change_pct,
            amount_yi=item.amount_yi,
            volume_wan=item.volume_wan,
            source=self.name,
        ) for item in quotes]

    # ================================================================== K 线
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        """读取 ``kline/{code}.csv`` 或 ``kline_all.csv``（``volume`` 手、``amount`` 元）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            raise ProviderUnavailable("CSV 数据源无法识别标的：%r" % (symbol,))
        try:
            limit = max(1, int(days))
        except (TypeError, ValueError):
            limit = 250

        rows = self._kline_rows(code)
        bars: List[Bar] = []
        for row in rows:
            date = (row.get("date") or "").strip()
            if not date:
                continue
            try:
                bars.append(Bar(
                    date=date[:10],
                    open=self._f(row, "open"),
                    high=self._f(row, "high"),
                    low=self._f(row, "low"),
                    close=self._f(row, "close"),
                    volume_wan=self._f(row, "volume") / 1e4,      # 手 → 万手
                    amount_yi=self._f(row, "amount") / 1e8,       # 元 → 亿元
                    change_pct=self._f(row, "change_pct"),
                    turnover_pct=self._f(row, "turnover_pct"),
                ))
            except Exception:      # 单行坏了不影响整体
                continue
        if not bars:
            raise ProviderUnavailable("CSV 中没有 %s 的 K 线数据" % code)
        bars.sort(key=lambda item: item.date)
        freq_key = str(freq or "day").strip().lower()
        if freq_key in ("week", "weekly", "周", "周线"):
            bars = self._aggregate(bars, 7)
        elif freq_key in ("month", "monthly", "月", "月线"):
            bars = self._aggregate(bars, 30)
        elif freq_key not in ("day", "daily", "日", "日线"):
            raise ProviderUnavailable("CSV 数据源暂不支持周期 %r（仅 day/week/month）" % (freq,))
        return bars[-limit:]

    def _kline_rows(self, code: str) -> List[Dict[str, str]]:
        digits = code.split(".", 1)[0]
        candidates = []
        if self.kline_dir:
            candidates.extend([
                os.path.join(self.kline_dir, code + ".csv"),
                os.path.join(self.kline_dir, digits + ".csv"),
                os.path.join(self.kline_dir, sym.to_sina_symbol(code) + ".csv"),
                os.path.join(self.kline_dir, code.replace(".", "_") + ".csv"),
            ])
        for path in candidates:
            if path and os.path.isfile(path):
                return self._rows(path, "K 线 %s" % code)
        if self.kline_all_path and os.path.isfile(self.kline_all_path):
            rows = self._rows(self.kline_all_path, "K 线汇总")
            picked = [row for row in rows if self._norm(row.get("code")) == code
                      or (row.get("code") or "").strip() == digits]
            if picked:
                return picked
            raise ProviderUnavailable("kline_all.csv 中没有 %s 的数据" % code)
        raise ProviderUnavailable(
            "未找到 %s 的 K 线 CSV（尝试过 %s）" % (code, "、".join(os.path.basename(p) for p in candidates))
        )

    @staticmethod
    def _aggregate(bars: List[Bar], days_span: int) -> List[Bar]:
        """把日线聚合为周线 / 月线（开=首、高低=极值、收=末、量额=求和）。"""
        out: List[Bar] = []
        bucket_key = ""
        current: Optional[Bar] = None
        for bar in bars:
            try:
                day = bar.date
                parts = [int(x) for x in day.split("-")]
                d = datetime.date(parts[0], parts[1], parts[2])
            except (ValueError, IndexError):
                continue
            key = ("%d-W%02d" % d.isocalendar()[:2]) if days_span == 7 else day[:7]
            if current is None or key != bucket_key:
                if current is not None:
                    out.append(current)
                current = Bar(
                    date=bar.date, open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                    volume_wan=bar.volume_wan, amount_yi=bar.amount_yi,
                    change_pct=bar.change_pct, turnover_pct=bar.turnover_pct,
                )
                bucket_key = key
            else:
                current.high = max(current.high, bar.high)
                current.low = min(current.low if current.low else bar.low, bar.low)
                current.close = bar.close
                current.date = bar.date
                current.volume_wan += bar.volume_wan
                current.amount_yi += bar.amount_yi
                current.change_pct = bar.change_pct
                current.turnover_pct = bar.turnover_pct
        if current is not None:
            out.append(current)
        return out

    # ================================================================== 板块 / 广度
    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """读取 ``sectors.csv``（按涨跌幅降序取前 ``limit`` 个）。"""
        rows = self._rows(self.sectors_path, "板块")
        try:
            top = max(1, int(limit))
        except (TypeError, ValueError):
            top = 20
        out: List[SectorQuote] = []
        for row in rows:
            name = (row.get("name") or "").strip()
            code = self._norm(row.get("code")) or (row.get("code") or "").strip()
            if not name and not code:
                continue
            out.append(SectorQuote(
                code=code,
                name=name,
                change_pct=self._f(row, "change_pct"),
                net_inflow_yi=self._to_yi(self._f(row, "net_inflow")),
                up_count=self._i(row, "up_count"),
                down_count=self._i(row, "down_count"),
                leading_stock=(row.get("leading_stock") or "").strip(),
            ))
        if not out:
            raise ProviderUnavailable("sectors.csv 中没有有效板块数据")
        out.sort(key=lambda item: item.change_pct, reverse=True)
        return out[:top] if top < len(out) else out

    def breadth(self) -> MarketBreadth:
        """读取 ``breadth.csv``（``key,value`` 两列）。"""
        rows = self._rows(self.breadth_path, "市场广度")
        values: Dict[str, str] = {}
        for row in rows:
            key = (row.get("key") or "").strip().lower()
            if key:
                values[key] = row.get("value") or ""
        if not values:
            raise ProviderUnavailable("breadth.csv 中没有有效数据")
        up = self._f(values, "up")
        down = self._f(values, "down")
        flat = self._f(values, "flat")

        def _num(key: str, default: float = 0.0) -> float:
            text = (values.get(key) or "").strip().replace(",", "")
            if not text:
                return default
            try:
                return float(text)
            except ValueError:
                return default

        total = int(_num("total", 0)) or int(up + down + flat)
        return MarketBreadth(
            up=int(up),
            down=int(down),
            flat=int(flat),
            limit_up=int(_num("limit_up", 0)),
            limit_down=int(_num("limit_down", 0)),
            total=total,
            total_amount_yi=round(self._to_yi(_num("total_amount", 0.0)), 2),
            main_net_inflow_yi=(round(self._to_yi(_num("main_net_inflow")), 2)
                                if "main_net_inflow" in values else None),
            north_net_inflow_yi=(round(self._to_yi(_num("north_net_inflow")), 2)
                                 if "north_net_inflow" in values else None),
            source=self.name,
        )

    # ================================================================== 名称 / 自检
    def resolve_name(self, symbol: str) -> Optional[str]:
        code = self._norm(symbol)
        if not code:
            return None
        try:
            rows = self._rows(self.quotes_path, "实时快照")
        except ProviderUnavailable:
            return sym.INDEX_NAMES.get(code)
        for row in rows:
            if self._norm(row.get("code")) == code:
                name = (row.get("name") or "").strip()
                return name or sym.INDEX_NAMES.get(code)
        return sym.INDEX_NAMES.get(code)

    def health(self) -> Dict[str, Any]:
        import time
        started = time.time()
        files = [
            ("quotes.csv", self.quotes_path),
            ("kline_all.csv", self.kline_all_path),
            ("sectors.csv", self.sectors_path),
            ("breadth.csv", self.breadth_path),
        ]
        present = [name for name, path in files if path and os.path.isfile(path)]
        if self.kline_dir and os.path.isdir(self.kline_dir):
            present.extend(
                sorted("kline/" + name for name in os.listdir(self.kline_dir) if name.endswith(".csv"))
            )
        ok = bool(present)
        if ok:
            detail = "本地 CSV 就绪（%s）：%s" % (self.csv_dir, "、".join(present[:6]))
        else:
            detail = "未找到任何 CSV 数据文件（目录 %s）；请按 README 的规范准备数据" % self.csv_dir
        return {
            "ok": ok,
            "detail": detail,
            "latency_ms": int((time.time() - started) * 1000),
            "source": self.name,
        }

    # ================================================================== 内部
    @staticmethod
    def _norm(code: Any) -> str:
        try:
            return sym.normalize(code)
        except SymbolNotFound:
            return ""

    def _wanted(self, symbols_in: Sequence[str]) -> List[str]:
        out = []
        for item in symbols_in or []:
            code = self._norm(item)
            if code and code not in out:
                out.append(code)
        return out
