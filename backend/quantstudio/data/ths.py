# -*- coding: utf-8 -*-
"""同花顺（10jqka）Provider：**只用公开网页 JSONP 接口**，提供实时分时快照 + 日线（前复权）。

数据边界（重要）
----------------
- 只请求 ``d.10jqka.com.cn`` 的**公开网页行情接口**（与现有新浪 / 腾讯 / 东财源同一性质）；
- **不含**同花顺付费 Level-2（十档盘口 / 逐笔委托 / 委托队列 / 主力资金）的任何内容，
  也**不涉及**其客户端协议、鉴权、加密，或任何逆向手段——需要 L2 请走 ``docs/level2.md``
  的「授权源接入位 / 本地导入通道」；
- 这些网页接口是非公开文档的私有 JSONP，**格式随时可能变化**，因此本模块所有解析失败
  一律降级：单条标的解析不出就跳过，整批失败抛 :class:`DataSourceError`,
  ``kline`` 拿不到就返回 ``[]``，由 :class:`~quantstudio.data.composite.CompositeProvider`
  继续走降级链（腾讯 / 东财 / 缓存 / CSV / 示例）。

接口与字段（2026-09-24 本机实测，网络受限环境可能不可达）
--------------------------------------------------------
1. 实时分时（JSONP）::

       GET https://d.10jqka.com.cn/v6/time/hs_600519/last.js
       Referer: https://stockpage.10jqka.com.cn/

   代码统一是 ``hs_`` + 6 位数字：``600519.SH → hs_600519``、``000001.SZ → hs_000001``、
   ``399001.SZ → hs_399001``（见 :func:`to_ths_code`）。响应形如::

       quotebridge_v6_time_hs_600519_last({"hs_600519":{
           "name":"\\u8d35\\u5dde\\u8305\\u53f0","pre":"1251.24","date":"20260924",
           "marketType":"HS_stock_sh",
           "data":"0930,1250.01,22911433,1250.010,18329;...;1530,1237.00,742200,1237.961,600.00"}})

   - 外层是 ``回调名({...})``，必须剥掉；内层是 JSON（**中文名是 ``\\uXXXX`` 转义**，
     JSON 解析即还原）；
   - ``name`` 标的名称、``pre`` **昨收**、``date`` 交易日 ``YYYYMMDD``（与 ``dates[0]`` 一致）；
   - ``data`` 是 ``;`` 分隔的分钟点，每点 ``时间(HHMM),价格,成交额(元),均价,成交量(股)``；
     实测约 268 个点（沪市个股含 15:05–15:30 盘后固定价格交易段）；
   - ``marketType`` 形如 ``HS_stock_sh`` / ``HS_stock_sz``（实测值）用于**校验代码市场**：
     同花顺的 ``hs_`` 是「沪深个股 + 深市指数」同一命名空间，``000001.SH``（上证指数）会落到
     ``hs_000001``＝**平安银行**，因此市场不符时本模块直接跳过该标的，绝不用错数据。

2. 日线（JSONP，**已前复权**）::

       GET https://d.10jqka.com.cn/v6/line/hs_600519/01/last.js     # 最近 140 个交易日
       GET https://d.10jqka.com.cn/v6/line/hs_600519/01/<year>.js   # 按年明细（翻页用）

   响应是**扁平** JSON（没有 ``hs_600519`` 那一层）::

       quotebridge_v6_line_hs_600519_01_last({"name":"\\u8d35\\u5dde\\u8305\\u53f0","total":"6012",
           "num":140,"start":"20010827","today":"20260924","marketType":"HS_stock_sh",
           "year":{"2001":86,...,"2026":178},
           "data":"20260924,1250.01,1256.13,1231.05,1237.00,3123935,3867310900.00,0.250,,600.00,742200;..."})

   每个日线行是 ``日期,开,高,低,收,成交量(股),成交额(元),换手率%,?,?,?``：
   **顺序是 开-高-低-收**（注意与腾讯的 开-收-高-低 不同，实测 2026-09-24 的
   ``1250.01,1256.13,1231.05,1237.00`` 与分时点的最高 1256.13 / 最低 1231.05 对齐）；
   成交量单位是**股**、成交额是**元**（实测 600519 2026-09-21：2501689 股 = 腾讯 25017 手；
   2026-02-22 的 2212.69 与腾讯 ``fqkline`` 的 qfq 2212.766 一致 → 本源按**前复权**使用）；
   第 8 位是换手率 %，末三位属融资 / 龙虎榜类字段（本模块不用）。
   ``year`` 是「年份 → 该年交易日数」索引、``total`` 是总根数、``start`` 是最早上线日。
   **指数的日线实测会滞后一个交易日**（``today`` 是 20260924 而最后一根是 20260923 的那根），
   所以当日开 / 高 / 低取不到时按 0 处理（见下）。

口径
----
- :meth:`ThsProvider.latest_quotes`：**现价**取分时接口最后一个点；**昨收**优先取分时的
  ``pre``，其次按「日线最后一根（其日期与分时日期不一致时）或日线倒数第二根（一致时）」的收盘；
  **开 / 高 / 低 / 成交量 / 成交额 / 换手率**取日线里与当日匹配的那一根（指数日线滞后时当日无根：
  开高低置 0，量额退化为分时点求和）；``change`` / ``change_pct`` 由现价与昨收自算；
  任何字段缺失都留 0，``source="ths"``；``volume_wan = 股 / 100 / 1e4``（项目统一「万手」口径，
  与新浪的个股 / 深市指数换算一致）、``amount_yi = 元 / 1e8``；
- :meth:`ThsProvider.kline`：**只支持** ``freq="day"`` 且 ``adjust="qfq"``，其它参数**返回 ``[]``**
  让降级链去用别家（腾讯 / 东财有周月线与后复权）；先取 ``last.js``（约 140 根），不够 ``days``
  时按 ``start`` 逐年往前抓 ``<year>.js``（最多 :data:`ThsProvider.MAX_YEAR_REQUESTS` 次）；
  某一年抓失败就停在那里，**把已收集到的真实根数返回**（原因写入 :attr:`ThsProvider.last_notes`），
  一根都没解析出则返回 ``[]``；
- :meth:`ThsProvider.resolve_name`：名称来自分时接口（``\\uXXXX`` 由 JSON 还原），
  带磁盘缓存，TTL 用 ``cache_ttl.name``，与新浪 / 腾讯一致。
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.errors import (
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
)
from ..core.models import Bar, Quote
from . import symbols as sym
from .base import BaseHTTPProvider

__all__ = ["ThsProvider", "to_ths_code"]

#: 日线行日期 ``YYYYMMDD``
_DAY_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
#: 分时点时间 ``HHMM``
_CLOCK_RE = re.compile(r"^(\d{2})(\d{2})$")


def to_ths_code(code: str) -> str:
    """项目代码 → 同花顺代码：``600519.SH → hs_600519``（无法识别时抛 SymbolNotFound）。"""
    return "hs_" + sym.digits_of(code)


def _jsonp_body(text: str) -> str:
    """剥掉 JSONP 外层回调 ``回调名({...})``（拿到 ``{...}`` 本体；没有括号则原样返回）。"""
    body = (text or "").strip()
    start = body.find("(")
    end = body.rfind(")")
    if 0 <= start < end:
        return body[start + 1:end].strip()
    return body


def _unescape(text: Any) -> str:
    """还原 ``\\uXXXX`` 转义（JSON 解析通常已还原；这里兜住非 JSON 路径与二次转义）。"""
    value = str(text if text is not None else "").strip()
    if "\\u" not in value and "\\x" not in value:
        return value
    try:
        restored = json.loads('"%s"' % value.replace('"', '\\"'))
    except ValueError:
        return value
    return str(restored)


def _number(value: Any, default: float = 0.0) -> float:
    """数据源的 ``""`` / ``"-"`` / ``None`` / 非数字 → default。"""
    if value is None:
        return default
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in ("-", "--", "null", "None"):
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _iso_date(value: Any) -> str:
    """``20260924`` → ``2026-09-24``（识别不了返回空串）。"""
    match = _DAY_RE.match(str(value or "").strip())
    if not match:
        return ""
    return "%s-%s-%s" % match.groups()


def _clock(value: Any) -> str:
    """``1530`` → ``15:30:00``（识别不了返回空串）。"""
    match = _CLOCK_RE.match(str(value or "").strip())
    if not match:
        return ""
    return "%s:%s:00" % match.groups()


def _points(data: Any) -> List[List[str]]:
    """分时 ``data`` 字符串 → ``[[时间, 价格, 成交额, 均价, 成交量], ...]``。"""
    out: List[List[str]] = []
    for chunk in str(data or "").split(";"):
        fields = [part.strip() for part in chunk.split(",")]
        if len(fields) >= 2 and fields[0]:
            out.append(fields)
    return out


class ThsProvider(BaseHTTPProvider):
    """同花顺公开网页接口（实时分时 + 前复权日线）。"""

    name = "ths"
    referer = "https://stockpage.10jqka.com.cn/"
    health_referer = "https://stockpage.10jqka.com.cn/"

    TIME_URL = "https://d.10jqka.com.cn/v6/time/%s/last.js"
    LINE_URL = "https://d.10jqka.com.cn/v6/line/%s/01/last.js"
    LINE_YEAR_URL = "https://d.10jqka.com.cn/v6/line/%s/01/%s.js"

    health_url = TIME_URL % "hs_600519"

    #: ``<year>.js`` 最多抓几次（10 年 ≈ 2400 根；与腾讯 K 线的翻页上限同量级）
    MAX_YEAR_REQUESTS = 10

    #: 启用该 Provider 作为 kline 降级源时需要的前复权周期
    KLINE_FREQ = "day"
    KLINE_ADJUST = "qfq"

    def __init__(self, settings=None):
        BaseHTTPProvider.__init__(self, settings)
        #: 最近一次调用被跳过的标的（市场不符 / 无数据 / 解析失败）
        self.last_skipped: List[str] = []
        #: 最近一次调用的口径说明（供 Composite 写入 meta.notes）
        self.last_notes: List[str] = []

    # ================================================================== 抓取
    def _jsonp(self, url: str) -> Any:
        """GET 一次 JSONP 接口并剥壳解析；失败抛 :class:`DataSourceError`。"""
        text = self._get_text(url, referer=self.referer)
        body = _jsonp_body(text)
        if not body:
            raise DataSourceError("%s 返回空响应：%s" % (self.name, url))
        try:
            return json.loads(body)
        except ValueError as exc:
            raise DataSourceError(
                "%s 响应不是合法 JSON（%s）：%s"
                % (self.name, exc, body[:120].replace("\n", " "))
            )

    def _time_node(self, code: str) -> Dict[str, Any]:
        """取分时接口的节点（含名称 / 昨收 / 当日分钟点）。"""
        ths_code = to_ths_code(code)
        payload = self._jsonp(self.TIME_URL % ths_code)
        if not isinstance(payload, dict):
            raise DataSourceError("%s 分时响应格式异常：%r" % (self.name, str(payload)[:120]))
        node = payload.get(ths_code)
        if not isinstance(node, dict):
            node = next((item for item in payload.values() if isinstance(item, dict)), None)
        if not isinstance(node, dict):
            raise DataSourceError("%s 分时响应缺少 %s 节点" % (self.name, ths_code))
        self._check_market(code, node.get("marketType"))
        return node

    def _daily_node(self, code: str, year: str = "last") -> Dict[str, Any]:
        """取日线节点（``year="last"`` 是最近 140 个交易日，否则某年明细）。"""
        ths_code = to_ths_code(code)
        url = self.LINE_URL % ths_code if year == "last" else self.LINE_YEAR_URL % (ths_code, year)
        payload = self._jsonp(url)
        if not isinstance(payload, dict):
            raise DataSourceError("%s 日线响应格式异常：%r" % (self.name, str(payload)[:120]))
        self._check_market(code, payload.get("marketType"))
        return payload

    def _check_market(self, code: str, market_type: Any) -> None:
        """校验 ``marketType`` 的市场后缀（``HS_stock_sh`` → ``SH``）与请求代码一致。

        同花顺的 ``hs_`` 命名空间里沪深个股 + 深市指数混在一起（``hs_000001`` 是**平安银行**，
        不是上证指数），市场不符时宁可不给数据，也不能把别的标的当成用户要的那个。
        """
        try:
            expected = sym.market_of(code)
        except SymbolNotFound:
            return
        tail = str(market_type or "").strip().lower().rsplit("_", 1)[-1].upper()
        if tail in sym.MARKETS and tail != expected:
            raise DataSourceError(
                "%s 的 %s 落在市场 %s，与请求的 %s 不符（hs_ 命名空间不含沪市指数，"
                "如 000001.SH 需改用其它数据源）" % (self.name, code, tail, expected)
            )

    # ================================================================== 快照
    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照（现价取分时最后一点，昨收 / 开高低 / 量额取日线，字段缺失留 0）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        out: List[Quote] = []
        skipped: List[str] = []
        notes: List[str] = []
        for code in codes:
            try:
                quote = self._snapshot(code, notes)
            except ProviderUnavailable:
                raise
            except DataSourceError as exc:
                skipped.append(code)
                notes.append("%s 同花顺快照失败：%s" % (code, exc))
                continue
            if quote is None:
                skipped.append(code)
                continue
            out.append(quote)
        self.last_skipped = skipped
        self.last_notes = notes
        if not out:
            reason = "；".join(notes[:2])
            raise DataSourceError(
                "同花顺未返回任何有效行情（%d 个标的全部失败：%s）%s"
                % (len(codes), ", ".join(codes[:5]), ("；原因：" + reason) if reason else "")
            )
        return out

    def _snapshot(self, code: str, notes: List[str]) -> Optional[Quote]:
        """单个标的的快照：分时决定现价与昨收，日线补开高低 / 量额 / 换手率。"""
        node = self._time_node(code)
        name = _unescape(node.get("name"))
        if name:
            self.remember_name(code, name)
        points = _points(node.get("data"))
        last_point = points[-1] if points else []
        price = _number(last_point[1]) if len(last_point) > 1 else 0.0
        prev_close = _number(node.get("pre"))
        quote_date = str(node.get("date") or "").strip()
        if not quote_date:
            dates = node.get("dates")
            quote_date = str(dates[0]).strip() if isinstance(dates, list) and dates else ""

        rows = self._recent_rows(code, notes)
        current = previous = None
        if rows:
            last_row = rows[-1]
            if quote_date and str(last_row[0]).strip() != quote_date:
                previous = last_row          # 日线还没更新到当日（指数实测滞后一天）
                notes.append(
                    "%s 同花顺日线未更新到 %s（最后一根 %s），当日开/高/低按 0 处理"
                    % (code, quote_date, str(last_row[0]).strip())
                )
            else:
                current = last_row
                previous = rows[-2] if len(rows) >= 2 else None

        open_price = _number(current[1]) if current is not None and len(current) > 1 else 0.0
        high = _number(current[2]) if current is not None and len(current) > 2 else 0.0
        low = _number(current[3]) if current is not None and len(current) > 3 else 0.0
        close = _number(current[4]) if current is not None and len(current) > 4 else 0.0
        volume = _number(current[5]) if current is not None and len(current) > 5 else 0.0
        amount = _number(current[6]) if current is not None and len(current) > 6 else 0.0
        turnover = _number(current[7]) if current is not None and len(current) > 7 else 0.0

        if not prev_close and previous is not None and len(previous) > 4:
            prev_close = _number(previous[4])
        if price <= 0:
            price = close                     # 分时缺失（盘前 / 停牌）时退化为日线收盘
        if volume <= 0:                       # 日线无当日根：量额由分时点求和
            volume = sum(_number(point[4]) for point in points if len(point) > 4)
            amount = sum(_number(point[2]) for point in points if len(point) > 2)
        if price <= 0 and prev_close <= 0:
            return None                       # 停牌 / 退市：不产出 0 价记录

        change = round(price - prev_close, 4) if prev_close else 0.0
        return Quote(
            code=code,
            name=name or (sym.INDEX_NAMES.get(code) or ""),
            price=price,
            prev_close=prev_close,
            open=open_price,
            high=high,
            low=low,
            change=change,
            change_pct=round(change / prev_close * 100, 4) if prev_close else 0.0,
            volume_wan=volume / 100.0 / 1e4,          # 股 → 手 → 万手
            amount_yi=amount / 1e8,                   # 元 → 亿元
            turnover_pct=turnover,                    # 日线第 8 位（%）
            ts=self._ts(quote_date, last_point),
            source=self.name,
        )

    def _recent_rows(self, code: str, notes: List[str]) -> List[List[str]]:
        """最近两三个交易日的日线行（升序）；日线不可用时只记笔记，不影响分时快照。"""
        try:
            node = self._daily_node(code)
        except ProviderUnavailable:
            raise
        except DataSourceError as exc:
            notes.append("%s 同花顺日线不可用（昨收取分时 pre）：%s" % (code, exc))
            return []
        return self._parse_daily(node.get("data"))[-2:]

    @staticmethod
    def _ts(quote_date: str, last_point: Sequence[str]) -> str:
        """``20260924`` + ``1530`` → ``2026-09-24 15:30:00``。"""
        day = _iso_date(quote_date)
        clock = _clock(last_point[0]) if len(last_point) > 0 else ""
        if day and clock:
            return "%s %s" % (day, clock)
        return day or clock

    # ================================================================== K 线
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        """前复权日线（最近 ``days`` 根，升序）；其它周期 / 复权方式返回 ``[]`` 让降级链接手。"""
        freq_key = str(freq or "day").strip().lower()
        adjust_key = str(adjust or "qfq").strip().lower()
        if freq_key != self.KLINE_FREQ or adjust_key != self.KLINE_ADJUST:
            return []
        try:
            limit = int(days)
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            return []

        rows, notes = self._daily_rows(code, limit)
        self.last_notes = notes
        if not rows:
            return []
        bars = self._build_bars(rows)
        return bars[-limit:]

    def _daily_rows(self, code: str, limit: int) -> Tuple[List[List[str]], List[str]]:
        """收集最近 ``limit`` 根日线（升序）：``last.js`` + 逐年 ``<year>.js`` 往前翻页。

        **尽力而为**：某一年抓失败就停在那儿，返回已收集到的真实根数（原因写进 notes），
        由上层决定是否降级；一根都没有则返回空列表（此时不抛异常）。
        """
        notes: List[str] = []
        try:
            node = self._daily_node(code)
        except ProviderUnavailable:
            raise
        except DataSourceError as exc:
            notes.append("%s 同花顺日线不可用：%s" % (code, exc))
            return [], notes

        collected: Dict[str, List[str]] = {}
        self._merge_rows(collected, self._parse_daily(node.get("data")))
        start = str(node.get("start") or "").strip()
        try:
            start_year = int(start[:4])
        except (TypeError, ValueError):
            start_year = 0
        attempts = 0
        while collected and len(collected) < limit and attempts < self.MAX_YEAR_REQUESTS:
            oldest_year = int(min(collected)[:4])
            year = oldest_year - 1
            if year < 1990 or (start_year and year < start_year):
                break                       # 已到该标的最早年份：历史就这么多
            attempts += 1
            try:
                year_node = self._daily_node(code, str(year))
            except ProviderUnavailable:
                raise
            except DataSourceError as exc:
                notes.append(
                    "%s 同花顺 %d 年日线抓取失败（已收集 %d 根，提前返回）：%s"
                    % (code, year, len(collected), exc)
                )
                break
            added = self._merge_rows(collected, self._parse_daily(year_node.get("data")))
            if not added:                   # 该年无数据：往前再走一年也不会更早，直接停
                notes.append("%s 同花顺 %d 年日线为空（已收集 %d 根）" % (code, year, len(collected)))
                break
        if collected and len(collected) < limit:
            notes.append(
                "%s 同花顺只提供 %d 根日线（请求 %d 根）" % (code, len(collected), limit)
            )
        return [collected[key] for key in sorted(collected)], notes

    @staticmethod
    def _merge_rows(collected: Dict[str, List[str]], rows: List[List[str]]) -> int:
        """合并日线行（按日期去重），返回新增根数。"""
        added = 0
        for row in rows:
            date = str(row[0]).strip()
            if date and date not in collected:
                collected[date] = row
                added += 1
        return added

    @staticmethod
    def _parse_daily(data: Any) -> List[List[str]]:
        """日线 ``data`` 字符串 → ``[[日期, 开, 高, 低, 收, 量, 额, 换手, ...], ...]``。"""
        out: List[List[str]] = []
        for chunk in str(data or "").split(";"):
            fields = [part.strip() for part in chunk.split(",")]
            if len(fields) >= 6 and _DAY_RE.match(fields[0]):
                out.append(fields)
        return out

    @staticmethod
    def _build_bars(rows: Sequence[Sequence[str]]) -> List[Bar]:
        """``[日期, 开, 高, 低, 收, 量(股), 额(元), 换手率%]`` → :class:`Bar`（相邻收盘自算涨跌幅）。"""
        bars: List[Bar] = []
        prev_close = 0.0
        for row in rows:
            date = _iso_date(row[0])
            open_price = _number(row[1]) if len(row) > 1 else 0.0
            high = _number(row[2]) if len(row) > 2 else 0.0
            low = _number(row[3]) if len(row) > 3 else 0.0
            close = _number(row[4]) if len(row) > 4 else 0.0
            if not date or close <= 0:
                continue
            change_pct = ((close / prev_close - 1) * 100) if prev_close else 0.0
            bars.append(Bar(
                date=date,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume_wan=_number(row[5]) / 100.0 / 1e4 if len(row) > 5 else 0.0,
                amount_yi=_number(row[6]) / 1e8 if len(row) > 6 else 0.0,
                change_pct=round(change_pct, 4),
                turnover_pct=_number(row[7]) if len(row) > 7 else 0.0,
            ))
            prev_close = close
        return bars

    # ================================================================== 名称与自检
    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（分时接口的 ``name``，``\\uXXXX`` 由 JSON 还原；带磁盘缓存）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            return None
        cached = self._cache_get("name", [code])
        if cached:
            return str(cached)
        known = sym.INDEX_NAMES.get(code)
        try:
            node = self._time_node(code)
        except DataSourceError:
            return known
        name = _unescape(node.get("name"))
        if not name:
            return known
        self.remember_name(code, name)
        self._cache_set("name", [code], name, self.ttl.name)
        return name

    def _probe(self) -> str:
        """自检：取贵州茅台的分时快照（离线由基类拦住，不会发起请求）。"""
        node = self._time_node("600519.SH")
        points = _points(node.get("data"))
        last_point = points[-1] if points else []
        price = _number(last_point[1]) if len(last_point) > 1 else 0.0
        if price <= 0:
            raise DataSourceError("同花顺 600519 分时现价为 0")
        node_date = str(node.get("date") or "").strip()
        return "同花顺可用：%s %.2f（时间 %s）" % (
            _unescape(node.get("name")) or "贵州茅台",
            price,
            self._ts(node_date, last_point) or "-",
        )
