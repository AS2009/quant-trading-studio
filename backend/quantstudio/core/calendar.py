# -*- coding: utf-8 -*-
"""交易日历：工作日 + 可扩充的节假日表。

真实使用时请补齐 A 股法定节假日（或从数据源拉取交易日历）；本模块提供两种模式：
1) `TradingCalendar()`            —— 仅剔除周末（默认，安全）
2) `TradingCalendar.from_file()`  —— 从 data/holidays.json 读取节假日表
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Set

_DATE_FMT = "%Y-%m-%d"


def parse_date(value) -> date:
    """把 str/date/datetime 统一成 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip().replace("/", "-")
        if not text:
            raise ValueError("日期不能为空")
        return datetime.strptime(text[:10], _DATE_FMT).date()
    raise ValueError("无法解析日期: %r" % (value,))


def fmt(value) -> str:
    return parse_date(value).strftime(_DATE_FMT)


class TradingCalendar:
    """交易日判定与区间生成。"""

    def __init__(self, holidays: Optional[Iterable[str]] = None):
        self._holidays: Set[str] = set()
        if holidays:
            for item in holidays:
                self._holidays.add(fmt(item))

    @classmethod
    def from_file(cls, path: str) -> "TradingCalendar":
        """从 JSON 文件读取节假日（["2026-01-01", ...] 或 {"holidays": [...]}）。"""
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if isinstance(raw, dict):
            raw = raw.get("holidays") or []
        return cls(raw if isinstance(raw, list) else [])

    def is_trading_day(self, day) -> bool:
        d = parse_date(day)
        return d.weekday() < 5 and fmt(d) not in self._holidays

    def trading_days(self, start, end) -> List[str]:
        """[start, end] 区间内的交易日（升序）。"""
        s, e = parse_date(start), parse_date(end)
        if s > e:
            s, e = e, s
        out: List[str] = []
        cur = s
        while cur <= e:
            if self.is_trading_day(cur):
                out.append(fmt(cur))
            cur += timedelta(days=1)
        return out

    def last_trading_day(self, as_of=None) -> str:
        d = parse_date(as_of) if as_of else date.today()
        for _ in range(30):
            if self.is_trading_day(d):
                return fmt(d)
            d -= timedelta(days=1)
        return fmt(d)

    def is_closed(self, as_of=None) -> bool:
        """当前是否已收盘（用于决定缓存策略；15:00 之后视为当日收盘）。"""
        now = datetime.now()
        if as_of is not None:
            now = datetime.combine(parse_date(as_of), now.time())
        if not self.is_trading_day(now.date()):
            return True
        return (now.hour, now.minute) >= (15, 0)

    def shift(self, day, n: int) -> str:
        """交易日偏移：n>0 向后，n<0 向前。"""
        d = parse_date(day)
        step = 1 if n >= 0 else -1
        remain = abs(n)
        while remain:
            d += timedelta(days=step)
            if self.is_trading_day(d):
                remain -= 1
        return fmt(d)
