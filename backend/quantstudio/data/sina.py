# -*- coding: utf-8 -*-
"""新浪财经 Provider：只做**实时快照**（K 线 / 板块 / 广度一律抛 ProviderUnavailable）。

接口
----
``GET https://hq.sinajs.cn/list=sh600519,sz300750,sh000001``
必须带 ``Referer: https://finance.sina.com.cn``，响应是 **GBK** 编码，每行形如::

    var hq_str_sh600519="名称,今开,昨收,最新,最高,最低,买一价,卖一价,成交量,成交额,
                        买1量,买1价,...,卖5量,卖5价,日期,时间,状态";

字段位置：0 名称 / 1 今开 / 2 昨收 / 3 最新 / 4 最高 / 5 最低 / 6 买一价 / 7 卖一价 /
8 成交量 / 9 成交额 / … / 30 日期 / 31 时间。

成交量单位（已实测对齐东方财富 f5）
-----------------------------------
- 个股 / ETF：**股** → ``volume_wan = 股 / 100 / 1e4``
  （如 sh600519 的 2501689 股 = 东财 25017 手）
- 沪市指数（sh000001 等）：**手** → ``volume_wan = 手 / 1e4``
- 深市指数（sz399001 等）：**股** → ``volume_wan = 股 / 100 / 1e4``
  （如 sz399001 的 63170807053 股 = 东财 631708071 手）
- 成交额恒为 **元** → ``amount_yi = 元 / 1e8``

容错
----
停牌 / 退市 / 未知标的会返回空内容（``var hq_str_xxx="";``）：
这类标的**直接跳过**（不产出 0 价记录）；若整批响应都为空则抛
:class:`DataSourceError`，让 CompositeProvider 继续降级到东财 / 缓存 / CSV / 示例。
"""

import re
from typing import Any, Dict, List, Optional, Sequence

from ..core.errors import (
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
)
from ..core.models import IndexQuote, Quote
from . import symbols as sym
from .base import BaseHTTPProvider

__all__ = ["SinaProvider"]

#: 每条行情行的变量名（sh600519 / sz300750 / bj430047）
_LINE_RE = re.compile(r'hq_str_([a-zA-Z]{2}\d{6})\s*=\s*"([^"]*)"')


class SinaProvider(BaseHTTPProvider):
    """新浪财经实时快照（GBK）。"""

    name = "sina"
    referer = "https://finance.sina.com.cn"
    health_referer = "https://finance.sina.com.cn"
    health_url = "https://hq.sinajs.cn/list=sh000001"

    QUOTE_URL = "https://hq.sinajs.cn/list="
    #: 单次请求最大标的数（过大容易被拒答）
    BATCH_SIZE = 60

    def __init__(self, settings=None):
        BaseHTTPProvider.__init__(self, settings)
        #: 最近一次调用被跳过的标的（停牌 / 退市 / 空响应）
        self.last_skipped: List[str] = []

    # ================================================================== 抓取
    def _fetch_rows(self, codes: Sequence[str]) -> Dict[str, List[str]]:
        """返回 ``{规范化代码: 字段列表}``；整批为空时抛 DataSourceError。"""
        normal = self._normalize_all(codes)
        if not normal:
            return {}
        out: Dict[str, List[str]] = {}
        for start in range(0, len(normal), self.BATCH_SIZE):
            chunk = normal[start:start + self.BATCH_SIZE]
            url = self.QUOTE_URL + ",".join(self._code_to_sina(code) for code in chunk)
            # 新浪响应是 GBK：交给基类按「响应头 charset → utf-8 → gbk」探测解码
            text = self._get_text(url, referer=self.referer)
            for match in _LINE_RE.finditer(text or ""):
                raw_symbol = match.group(1)
                payload = match.group(2)
                try:
                    code = sym.normalize(raw_symbol)
                except SymbolNotFound:
                    continue
                fields = payload.split(",") if payload.strip() else []
                if len(fields) < 10:
                    continue                       # 空内容 / 字段不足：跳过
                out[code] = fields
        return out

    # ================================================================== 快照
    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照（指数代码也会返回 Quote，``price`` 即点位）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._fetch_rows(codes)
        self.last_skipped = [code for code in codes if code not in rows]
        if not rows:
            raise DataSourceError(
                "新浪未返回任何有效行情（%d 个标的全部为空：%s）"
                % (len(codes), ", ".join(codes[:5]))
            )
        out: List[Quote] = []
        for code in codes:
            fields = rows.get(code)
            if not fields:
                continue
            quote = self._to_quote(code, fields)
            if quote is not None:
                out.append(quote)
        if not out:
            raise DataSourceError("新浪行情全部无法解析（%d 个标的）" % len(codes))
        return out

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照（非指数代码同样能取到点位，便于降级链复用）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._fetch_rows(codes)
        out: List[IndexQuote] = []
        for code in codes:
            fields = rows.get(code)
            if not fields:
                continue
            point = self._f(fields, 3)
            prev_close = self._f(fields, 2)
            if point <= 0 and prev_close <= 0:
                continue
            name = self._s(fields, 0)
            if name:
                self.remember_name(code, name)
            out.append(IndexQuote(
                code=code,
                name=name or (sym.INDEX_NAMES.get(code) or ""),
                point=point,
                prev_close=prev_close,
                change=round(point - prev_close, 4) if prev_close else 0.0,
                change_pct=round((point / prev_close - 1) * 100, 4) if prev_close else 0.0,
                amount_yi=self._f(fields, 9) / 1e8,
                volume_wan=self._volume_wan(code, self._f(fields, 8)),
                source=self.name,
            ))
        if not out:
            raise DataSourceError("新浪未返回任何指数快照")
        return out

    def _to_quote(self, code: str, fields: List[str]) -> Optional[Quote]:
        price = self._f(fields, 3)
        prev_close = self._f(fields, 2)
        if price <= 0 and prev_close <= 0:
            return None                      # 停牌 / 退市
        name = self._s(fields, 0)
        if name:
            self.remember_name(code, name)
        change = round(price - prev_close, 4) if prev_close else 0.0
        return Quote(
            code=code,
            name=name or (sym.INDEX_NAMES.get(code) or ""),
            price=price,
            prev_close=prev_close,
            open=self._f(fields, 1),
            high=self._f(fields, 4),
            low=self._f(fields, 5),
            change=change,
            change_pct=round(change / prev_close * 100, 4) if prev_close else 0.0,
            volume_wan=self._volume_wan(code, self._f(fields, 8)),
            amount_yi=self._f(fields, 9) / 1e8,
            turnover_pct=0.0,       # 新浪快照不含换手率（东财接口有）
            ts=self._ts(fields),
            source=self.name,
        )

    # ================================================================== 内部
    def _volume_wan(self, code: str, volume: float) -> float:
        """按标的市场/类型换算成交量（万手）；规则见模块 docstring。"""
        try:
            kind = sym.kind_of(code)
            market = sym.market_of(code)
        except SymbolNotFound:
            kind, market = "stock", "SH"
        if kind == "index":
            # 沪市指数报「手」，深市指数报「股」
            return volume / 1e4 if market == "SH" else volume / 100.0 / 1e4
        # 个股 / ETF 报「股」
        return volume / 100.0 / 1e4

    @staticmethod
    def _s(fields: List[str], index: int) -> str:
        if index < 0 or index >= len(fields):
            return ""
        return str(fields[index]).strip()

    @classmethod
    def _f(cls, fields: List[str], index: int) -> float:
        text = cls._s(fields, index)
        if not text or text in ("-", "--"):
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0

    @classmethod
    def _ts(cls, fields: List[str]) -> str:
        day = cls._s(fields, 30)
        clock = cls._s(fields, 31)
        if day and clock:
            return "%s %s" % (day, clock)
        return day or clock

    # ================================================================== 不支持的能力
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Any]:
        """新浪不提供 K 线接口 → 抛 ProviderUnavailable（让 Composite 兜到东方财富）。"""
        raise ProviderUnavailable(
            "新浪不提供 K 线接口（symbol=%s），请使用东方财富数据源" % symbol
        )

    def sectors(self, limit: int = 20) -> List[Any]:
        """新浪不提供板块接口 → 抛 ProviderUnavailable。"""
        raise ProviderUnavailable("新浪不提供板块行情，请使用东方财富数据源")

    def breadth(self) -> Any:
        """新浪不提供市场广度接口 → 抛 ProviderUnavailable。"""
        raise ProviderUnavailable("新浪不提供市场广度数据，请使用东方财富数据源")

    # ================================================================== 名称与自检
    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（快照第 0 字段，带磁盘缓存，TTL 用 ``cache_ttl.name``）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            return None
        cached = self._cache_get("name", [code])
        if cached:
            return str(cached)
        known = sym.INDEX_NAMES.get(code)
        try:
            rows = self._fetch_rows([code])
        except DataSourceError:
            return known
        fields = rows.get(code)
        if not fields:
            return known
        name = self._s(fields, 0)
        if not name:
            return known
        self.remember_name(code, name)
        self._cache_set("name", [code], name, self.ttl.name)
        return name

    def _probe(self) -> str:
        rows = self._fetch_rows(["000001.SH"])
        fields = rows.get("000001.SH")
        if not fields:
            raise DataSourceError("新浪未返回上证指数快照")
        name = self._s(fields, 0) or "上证指数"
        point = self._f(fields, 3)
        if point <= 0:
            raise DataSourceError("新浪上证指数点数为 0")
        return "新浪可用：%s %.2f（时间 %s）" % (name, point, self._ts(fields))
