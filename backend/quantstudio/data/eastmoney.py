# -*- coding: utf-8 -*-
"""东方财富 Provider：实时快照、K 线（日/周/月，前复权）、行业板块、市场广度。

接口（字段已实测，勿随意改动）
------------------------------
- **K 线**：``push2his.eastmoney.com/api/qt/stock/kline/get``
  ``data.klines`` 每行是逗号串：
  ``日期,开,收,高,低,成交量(手),成交额(元),振幅%,涨跌幅%,涨跌额,换手率%``
  → 填 :class:`Bar`（``volume_wan=手/1e4``、``amount_yi=元/1e8``）。
  ``klt``：day=101 / week=102 / month=103；``fqt``：qfq=1 / hfq=2 / none=0。
  ``data.name`` 即标的名称（进程内记住，供 resolve_name 兜底）。
- **批量快照**：``push2.eastmoney.com/api/qt/ulist.np/get``，``fltt=2`` 时已是小数：
  f2 最新价 / f3 涨跌幅% / f4 涨跌额 / f5 成交量(手) / f6 成交额(元) / f8 换手率% /
  f9 PE(动) / f10 量比 / f12 代码 / f13 市场(1沪 0深) / f14 名称 / f15 最高 / f16 最低 /
  f17 今开 / f18 昨收 / f20 总市值(元) / f21 流通市值(元) / f23 市净率。
  指数走同一接口（``1.000001`` 等）。
- **行业板块**：``push2.eastmoney.com/api/qt/clist/get``，
  f12 板块代码 / f14 名称 / f3 涨幅% / f62 主力净流入(元) / f104 上涨家数 /
  f105 下跌家数 / f106 平盘家数 / f128 领涨股名称 / f140 领涨股代码。

市场广度口径（**重要**）
------------------------
- ``up`` / ``down`` / ``flat``：优先取「沪深指数快照的 f104/f105/f106」（更接近真实家数）；
  若该字段缺失，则退化为 **行业板块成分汇总**：把行业板块（``fs=m:90+t:2+f:!50``，
  东财在同一筛选下同时返回一级 / 二级行业板块）的 f104/f105/f106 相加。板块之间存在
  层级重叠，汇总值会高于真实上市公司家数，只能作为市场情绪近似参考。``total`` = 三者之和。
- ``total_amount_yi`` = 上证指数(``1.000001``) + 深证成指(``0.399001``) 的 f6 / 1e8。
- ``main_net_inflow_yi`` = 行业板块 f62 求和 / 1e8。
- ``limit_up`` / ``limit_down``：东财这两个接口不提供，保持 0（需要精确家数请用 CSV 自有数据）。
- 东财 clist 单页最多返回 100 条，``pz`` 传更大值也只返回 100 条，因此广度统计会翻页。
"""

from typing import Any, Dict, List, Optional, Sequence

from ..core.errors import (
    DataSourceError,
    SymbolNotFound,
    ValidationError,
)
from ..core.models import Bar, IndexQuote, MarketBreadth, Quote, SectorQuote
from . import symbols as sym
from .base import BaseHTTPProvider

__all__ = ["EastmoneyProvider"]

#: 行业板块筛选：m:90+t:2 = 行业板块，f:!50 排除已退市成分
BOARD_FILTER = "m:90+t:2+f:!50"


class EastmoneyProvider(BaseHTTPProvider):
    """东方财富（push2 / push2his）行情源。"""

    name = "eastmoney"
    referer = "https://quote.eastmoney.com/"
    health_referer = "https://quote.eastmoney.com/"
    health_url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get"
        "?secids=1.000001&fields=f12,f14,f2,f3&fltt=2&invt=2"
    )

    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    SNAPSHOT_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    BOARD_URL = "https://push2.eastmoney.com/api/qt/clist/get"

    KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
    KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    SNAPSHOT_FIELDS = "f2,f3,f4,f5,f6,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23"
    BOARD_FIELDS = "f2,f3,f12,f14,f62,f104,f105,f106,f128,f140"

    FREQ_KLT = {"day": 101, "week": 102, "month": 103}
    ADJUST_FQT = {"qfq": 1, "hfq": 2, "none": 0}

    #: 东财 clist 单页最多返回 100 条（传更大值也只给 100 条）
    BOARD_PAGE_SIZE = 100
    #: 广度统计最多翻页数（防止无限翻页打爆接口）
    BOARD_MAX_PAGES = 5

    def __init__(self, settings=None):
        BaseHTTPProvider.__init__(self, settings)
        #: 最近一次调用中数据源给出的口径说明（供服务层写进 meta.notes）
        self.last_notes: List[str] = []

    # ================================================================== K 线
    def kline(
        self,
        symbol: str,
        days: int = 250,
        freq: str = "day",
        adjust: str = "qfq",
    ) -> List[Bar]:
        """历史 K 线（按日期升序，最多 ``days`` 根）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound as exc:
            raise SymbolNotFound("东方财富无法识别标的 %r：%s" % (symbol, exc))

        freq_key = str(freq or "day").strip().lower()
        if freq_key not in self.FREQ_KLT:
            raise ValidationError(
                "K 线周期仅支持 day/week/month，当前为 %r" % (freq,), field="freq"
            )
        adjust_key = str(adjust or "qfq").strip().lower()
        if adjust_key not in self.ADJUST_FQT:
            raise ValidationError(
                "复权方式仅支持 qfq/hfq/none，当前为 %r" % (adjust,), field="adjust"
            )
        try:
            limit = int(days)
        except (TypeError, ValueError):
            raise ValidationError("days 需为整数，当前为 %r" % (days,), field="days")
        if limit <= 0:
            raise ValidationError("days 需为正整数，当前为 %d" % limit, field="days")

        payload = self._get_json(self.KLINE_URL, params={
            "secid": self._code_to_secid(code),
            "fields1": self.KLINE_FIELDS1,
            "fields2": self.KLINE_FIELDS2,
            "klt": self.FREQ_KLT[freq_key],
            "fqt": self.ADJUST_FQT[adjust_key],
            "end": "20500101",
            "lmt": limit,
        })
        data = self._data_of(payload)
        rows = data.get("klines") or []
        if not rows:
            raise DataSourceError("东方财富未返回 %s 的 K 线数据" % code)
        name = str(data.get("name") or "").strip()
        if name:
            self.remember_name(code, name)
        bars = [bar for bar in (self._parse_kline_row(row) for row in rows) if bar is not None]
        if not bars:
            raise DataSourceError("东方财富 K 线解析失败：%s" % code)
        bars.sort(key=lambda item: item.date)
        return bars[-limit:]

    @staticmethod
    def _parse_kline_row(row: Any) -> Optional[Bar]:
        """``日期,开,收,高,低,成交量(手),成交额(元),振幅%,涨跌幅%,涨跌额,换手率%``。"""
        text = str(row or "").strip()
        if not text:
            return None
        parts = text.split(",")
        if len(parts) < 7:
            return None
        try:
            return Bar(
                date=parts[0].strip(),
                open=float(parts[1]),
                close=float(parts[2]),
                high=float(parts[3]),
                low=float(parts[4]),
                volume_wan=float(parts[5]) / 1e4,     # 手 → 万手
                amount_yi=float(parts[6]) / 1e8,      # 元 → 亿元
                change_pct=float(parts[8]) if len(parts) > 8 and parts[8] not in ("", "-") else 0.0,
                turnover_pct=float(parts[10]) if len(parts) > 10 and parts[10] not in ("", "-") else 0.0,
            )
        except ValueError:
            return None

    # ================================================================== 快照
    def _snapshot(self, codes: Sequence[str], extra_fields: str = "") -> Dict[str, Dict[str, Any]]:
        """批量快照原始行，返回 ``{规范化代码: 行}``（同时按 6 位代码建索引便于容错）。

        ``extra_fields`` 用于在标准字段之外追加字段（如市场广度需要的 ``f104,f105,f106``），
        不会改变 :data:`SNAPSHOT_FIELDS` 里已实测的字段含义。
        """
        normal = self._normalize_all(codes)
        if not normal:
            return {}
        rows: Dict[str, Dict[str, Any]] = {}
        batch_size = 80                     # ulist 一次不宜超过 100 个 secid
        for start in range(0, len(normal), batch_size):
            chunk = normal[start:start + batch_size]
            payload = self._get_json(self.SNAPSHOT_URL, params={
                "secids": ",".join(self._code_to_secid(code) for code in chunk),
                "fields": self.SNAPSHOT_FIELDS + (("," + extra_fields) if extra_fields else ""),
                "fltt": 2,
                "invt": 2,
            })
            data = self._data_of(payload)
            for row in data.get("diff") or []:
                if not isinstance(row, dict):
                    continue
                norm = self._row_code(row)
                if not norm:
                    continue
                rows[norm] = row
                rows.setdefault(norm.split(".", 1)[0], row)
        if not rows:
            raise DataSourceError("东方财富快照返回空数据（%d 个标的）" % len(normal))
        return rows

    @staticmethod
    def _row_code(row: Dict[str, Any]) -> Optional[str]:
        """行情行 → 规范化代码（f12 代码 + f13 市场，1=沪 0=深/北）。"""
        digits = str(row.get("f12") or "").strip()
        if not digits.isdigit() or len(digits) != 6:
            return None
        if str(row.get("f13")) == "1":
            market = "SH"
        else:
            try:
                market = sym.infer_market(digits)
            except SymbolNotFound:
                market = "SZ"
        return "%s.%s" % (digits, market)

    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照。

        指数代码同样会返回 :class:`Quote`（``price`` 即点位），这样自选列表可以统一渲染；
        需要 :class:`IndexQuote` 请调用 :meth:`index_quotes`。
        """
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._snapshot(codes)
        out: List[Quote] = []
        for code in codes:
            row = rows.get(code) or rows.get(code.split(".", 1)[0])
            if not row:
                continue
            quote = self._to_quote(code, row)
            if quote is not None:
                out.append(quote)
        return out

    def _to_quote(self, code: str, row: Dict[str, Any]) -> Optional[Quote]:
        price = self._num(row.get("f2"), 0.0)
        prev_close = self._num(row.get("f18"), 0.0)
        if price <= 0 and prev_close <= 0:
            return None                      # 停牌 / 退市：跳过，让上层换别的手段
        name = str(row.get("f14") or "").strip()
        if name:
            self.remember_name(code, name)
        change = self._num_or_none(row.get("f4"))
        change_pct = self._num_or_none(row.get("f3"))
        if change is None:
            change = price - prev_close
        if change_pct is None:
            change_pct = (price / prev_close - 1) * 100 if prev_close else 0.0
        market_cap = self._num_or_none(row.get("f20"))
        return Quote(
            code=code,
            name=name or (sym.INDEX_NAMES.get(code) or ""),
            price=price,
            prev_close=prev_close,
            open=self._num(row.get("f17"), 0.0),
            high=self._num(row.get("f15"), 0.0),
            low=self._num(row.get("f16"), 0.0),
            change=change,
            change_pct=change_pct,
            volume_wan=self._num(row.get("f5"), 0.0) / 1e4,     # 手 → 万手
            amount_yi=self._num(row.get("f6"), 0.0) / 1e8,      # 元 → 亿元
            turnover_pct=self._num(row.get("f8"), 0.0),
            pe_ttm=self._num_or_none(row.get("f9")),
            pb=self._num_or_none(row.get("f23")),
            market_cap_yi=(market_cap / 1e8) if market_cap else None,
            volume_ratio=self._num_or_none(row.get("f10")),
            ts="",                       # ulist 不返回时间戳，服务层用 as_of 标注
            source=self.name,
        )

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照（非指数代码也能取到点位，便于降级时复用同一批数据）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._snapshot(codes)
        out: List[IndexQuote] = []
        for code in codes:
            row = rows.get(code) or rows.get(code.split(".", 1)[0])
            if not row:
                continue
            point = self._num(row.get("f2"), 0.0)
            prev_close = self._num(row.get("f18"), 0.0)
            if point <= 0 and prev_close <= 0:
                continue
            name = str(row.get("f14") or "").strip()
            if name:
                self.remember_name(code, name)
            change = self._num_or_none(row.get("f4"))
            change_pct = self._num_or_none(row.get("f3"))
            if change is None:
                change = point - prev_close
            if change_pct is None:
                change_pct = (point / prev_close - 1) * 100 if prev_close else 0.0
            out.append(IndexQuote(
                code=code,
                name=name or (sym.INDEX_NAMES.get(code) or ""),
                point=point,
                prev_close=prev_close,
                change=change,
                change_pct=change_pct,
                amount_yi=self._num(row.get("f6"), 0.0) / 1e8,
                volume_wan=self._num(row.get("f5"), 0.0) / 1e4,
                source=self.name,
            ))
        if not out:
            raise DataSourceError("东方财富未返回任何指数快照")
        return out

    # ================================================================== 板块
    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """行业板块行情（按涨跌幅降序，取前 ``limit`` 个）。"""
        try:
            top = max(1, int(limit))
        except (TypeError, ValueError):
            top = 20
        rows = self._board_rows(pages=1, page_size=min(max(top, 1), self.BOARD_PAGE_SIZE))
        out = [self._to_sector(row) for row in rows]
        out = [item for item in out if item is not None]
        if not out:
            raise DataSourceError("东方财富未返回行业板块数据")
        out.sort(key=lambda item: item.change_pct, reverse=True)
        return out[:top] if top < len(out) else out

    def _board_rows(self, pages: int = 1, page_size: int = BOARD_PAGE_SIZE) -> List[Dict[str, Any]]:
        """翻页取行业板块原始行（东财单页上限 100 条，超过会自动续页）。"""
        rows: List[Dict[str, Any]] = []
        seen = set()
        for page in range(1, max(1, pages) + 1):
            payload = self._get_json(self.BOARD_URL, params={
                "pn": page,
                "pz": min(max(page_size, 1), self.BOARD_PAGE_SIZE),
                "po": 1,          # 按 fid 降序
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": BOARD_FILTER,
                "fields": self.BOARD_FIELDS,
            })
            data = self._data_of(payload)
            chunk = [row for row in (data.get("diff") or []) if isinstance(row, dict)]
            if not chunk:
                break
            for row in chunk:
                key = str(row.get("f12") or id(row))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            total = self._int(data.get("total"))
            if total and len(rows) >= total:
                break
        if not rows:
            raise DataSourceError("东方财富未返回行业板块数据")
        return rows

    def _to_sector(self, row: Dict[str, Any]) -> Optional[SectorQuote]:
        code = str(row.get("f12") or "").strip()
        name = str(row.get("f14") or "").strip()
        if not code and not name:
            return None
        return SectorQuote(
            code=code,
            name=name,
            change_pct=self._num(row.get("f3"), 0.0),
            net_inflow_yi=self._num(row.get("f62"), 0.0) / 1e8,   # 元 → 亿元
            up_count=self._int(row.get("f104")),
            down_count=self._int(row.get("f105")),
            leading_stock=str(row.get("f128") or "").strip(),
            leading_code=str(row.get("f140") or "").strip(),
        )

    # ================================================================== 广度
    def breadth(self) -> MarketBreadth:
        """市场广度与资金（口径见模块 docstring）。"""
        self.last_notes = []

        # 1) 行业板块：f62 求和 → 主力净流入；f104/f105/f106 求和 → 涨跌家数兜底口径
        board_rows = self._board_rows(pages=self.BOARD_MAX_PAGES)
        main_net_inflow_yi = sum(self._num(row.get("f62"), 0.0) for row in board_rows) / 1e8
        board_up = sum(self._int(row.get("f104")) for row in board_rows)
        board_down = sum(self._int(row.get("f105")) for row in board_rows)
        board_flat = sum(self._int(row.get("f106")) for row in board_rows)

        # 2) 指数快照：优先用沪/深指数的 f104/f105/f106（更接近真实家数）+ 两市成交额
        up = down = flat = 0
        total_amount_yi = 0.0
        try:
            index_rows = self._snapshot(["000001.SH", "399001.SZ"], extra_fields="f104,f105,f106")
            sh_row = index_rows.get("000001.SH") or {}
            sz_row = index_rows.get("399001.SZ") or {}
            index_up = self._int(sh_row.get("f104")) + self._int(sz_row.get("f104"))
            index_down = self._int(sh_row.get("f105")) + self._int(sz_row.get("f105"))
            index_flat = self._int(sh_row.get("f106")) + self._int(sz_row.get("f106"))
            if index_up + index_down + index_flat > 0:
                up, down, flat = index_up, index_down, index_flat
            total_amount_yi = (
                self._num(sh_row.get("f6"), 0.0) + self._num(sz_row.get("f6"), 0.0)
            ) / 1e8
        except DataSourceError as exc:
            self.last_notes.append("指数快照不可用：%s" % exc)

        if up + down + flat <= 0:
            up, down, flat = board_up, board_down, board_flat
            self.last_notes.append(
                "涨跌家数为行业板块成分汇总口径（板块层级重叠，非精确上市公司家数）"
            )
        else:
            self.last_notes.append("涨跌家数取自沪深指数快照 f104/f105/f106")
        if total_amount_yi <= 0:
            self.last_notes.append("两市成交额不可用（指数快照缺失）")

        return MarketBreadth(
            up=up,
            down=down,
            flat=flat,
            limit_up=0,          # 东财该接口不提供涨跌停家数
            limit_down=0,
            total=up + down + flat,
            total_amount_yi=round(total_amount_yi, 2),
            main_net_inflow_yi=round(main_net_inflow_yi, 2),
            north_net_inflow_yi=None,      # 北向资金实时数据已停止披露
            source=self.name,
        )

    # ================================================================== 名称
    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（ulist 快照 f14，带磁盘缓存，TTL 用 ``cache_ttl.name``）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            return None
        cached = self._cache_get("name", [code])
        if cached:
            return str(cached)
        rows = self._snapshot([code])
        row = rows.get(code) or rows.get(code.split(".", 1)[0])
        if not row:
            return None
        name = str(row.get("f14") or "").strip()
        if not name:
            return None
        self.remember_name(code, name)
        self._cache_set("name", [code], name, self.ttl.name)
        return name

    # ================================================================== 自检
    def _probe(self) -> str:
        rows = self._snapshot(["000001.SH"])
        row = rows.get("000001.SH") or {}
        name = str(row.get("f14") or "上证指数")
        point = self._num(row.get("f2"), 0.0)
        if point <= 0:
            raise DataSourceError("上证指数快照为空")
        return "东方财富可用：%s %.2f（本次请求 %dms）" % (name, point, self.last_latency_ms)

    # ================================================================== 内部
    @staticmethod
    def _data_of(payload: Any) -> Dict[str, Any]:
        """取东财响应的 ``data``（``rc != 0`` 或缺少 data 视为失败）。"""
        if not isinstance(payload, dict):
            raise DataSourceError("东方财富响应格式异常：%r" % (payload,))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError("东方财富响应缺少 data 字段（rc=%s）" % payload.get("rc"))
        return data
