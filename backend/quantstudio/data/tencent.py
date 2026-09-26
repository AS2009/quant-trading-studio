# -*- coding: utf-8 -*-
"""腾讯行情 Provider：**真实历史 K 线**（乘法前复权 / 后复权日周月线）+ 行业板块 + 实时快照。

为什么需要它
------------
东方财富（push2 / push2his）在部分网络环境下会被整体阻断（实测 `RemoteDisconnected`），
届时 K 线与板块会全部降级到示例数据，真实回测就会落空。腾讯这三组接口在上述环境下仍可用，
是目前唯一能提供**真实历史 K 线**的公开源，因此作为 composite 的第二个真实源。

接口与字段（均已实测）
----------------------
1. 历史 K 线::

       GET https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
           ?param={symbol},{period},,{end},{count},{adjust}

   - ``symbol`` 与新浪同款：``sh600519`` / ``sz300750`` / ``sh000001``（复用 ``to_sina_symbol``）；
   - ``period``：``day`` / ``week`` / ``month``；``adjust``：``qfq`` / ``hfq`` / 空表示不复权。
     **本模块的 ``qfq`` 用的不是腾讯的 qfq**（腾讯 qfq 是减法复权，见下面「复权口径」一节），
     而是 hfq 序列缩放到最新真实价（真正的乘法前复权）；
   - 响应 ``data[symbol]`` 里键名依次为 ``qfqday`` / ``hfqday`` / ``day``（周/月同理：
     ``qfqweek`` / ``week`` …）。**指数即使请求 qfq 也返回 ``day``**，所以解析时按
     ``{adjust}{period}`` → ``{period}`` 的顺序取键。
   - 每根数组字段顺序是 ``[日期, 开, 收, 高, 低, 成交量(手)]``（**开-收-高-低**，不是 OHLC！），
     实测样例：``["2026-09-21","1259.00","1252.57","1259.95","1250.80","25017"]``
     → open=1259.00 / close=1252.57 / high=1259.95 / low=1250.80 / volume=25017 手。
   - 单次请求上限实测 800 根（``count=800`` 返回 801 根含边界，``count>=900`` 会被服务端截到
     ~640 根），因此实现为「每次请求 ``min(剩余, 800)`` 根 + 用 ``end`` 往前翻页」，
     最多翻 :data:`TencentProvider.MAX_REQUESTS` 次（默认 8 → 约 6400 根）后截断。
     **实测可回溯到 2001-08-27**（600519：8 页共 6012 根，第 8 页只剩 412 根，
     说明服务端再往前就没有了），即约 25 年日线；需要更多请提高该常量或改走 CSV 数据源。
   - K 线不返回成交额 → ``amount_yi=0``；``change_pct`` 由相邻收盘自算（合并后整体计算，首根为 0）；
     ``volume_wan = 手 / 1e4``（个股 / ETF / 沪深指数在腾讯口径下**统一是手**）。
   - ``data[symbol].qt[symbol]`` 里同时带实时快照，解析时会顺手记住名称。

复权口径（**重要**，别改回腾讯 qfq）
-----------------------------------
- 腾讯的 ``qfq`` 是**减法复权**：老价格 = 当下价 - 历史累计复权额，深历史会被减到趋近 0
  甚至负数。实测 600519.SH 取 5000 根（2005-11-21 → 2026-09-24）：``adjust="qfq"`` 首根收盘
  **-304.77**、**2516 天非正价**、单日涨跌幅最坏 **-1147%** —— 完全不可用。注意 1200 根以内
  看着正常（减法项还小），所以短区间不暴露，只有长历史才会炸。
- 腾讯的 ``hfq`` 是**乘法复权**，同一段日历干净：首根 88.14、末根 8764.86、最大单日 9.87%、
  非正价 0 天（同样是 600519 的 5000 根）。
- 所以本模块的 ``adjust="qfq"`` = **hfq 序列整体缩放到最新真实价**（真正的乘法前复权）：
  翻页取数用 ``hfq`` 前缀，再额外请求一次最新**不复权**收盘 ``raw_last``
  （``adjust="none"`` + ``days=1``，同 symbol / freq，共 1 次额外请求），令
  ``k = raw_last / hfq_last_close``，把该序列里所有价格字段（开 / 收 / 高 / 低）乘 ``k`` 后再交给
  :meth:`TencentProvider._build_bars`：``change_pct`` 由缩放后的相邻收盘自动重算，
  **百分比与 hfq 完全一致**（乘正常数不改变相邻比值），``volume`` 不受影响。结果保证
  **最后一根收盘 == raw_last**（今日真实价，现金 / 手数模拟才与现实同量级）、**所有价格 > 0**、
  **单日涨跌幅与 hfq 逐项一致**。
- 换成这套口径后实测（同一支 600519、同样 5000 根）：``qfq`` 首根 **12.44**、末根 **1237.00**
  （= 今日真实价）、最大单日 **9.87%**、非正价 **0** 天 —— 百分比与 hfq 完全一致。
- ``adjust="hfq"`` 语义不变（直接返回后复权原值）；``adjust="none"`` 语义不变（不复权原值），
  但 ``none`` 有**除权假跳**（实测 600519 最大单日 **-56.69%** 其实是当天除权），
  直接拿来回测会把除权当成暴跌，**不建议**。
- 指数（如 ``sh000001``）没有复权键，响应给的是 ``day``：此时**不做缩放**（k=1）原样返回；
  ``raw_last`` 取不到（网络 / 响应异常 / 非正价）或 ``hfq_last_close`` 非正时同样跳过缩放，
  保证数据始终可用（只是退回未缩放的 hfq 原值）。

2. 行业板块::

       GET https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank
           ?board_type=hy&sort_type=price&direct=down&offset=0&count={count}
       Header: Referer: https://gu.qq.com/

   - 响应 ``data.rank_list[]``，用的字段：``code``(板块代码) ``name``(名称) ``zdf``(涨跌幅%)
     ``zljlr``(主力净流入，**万元** → /1e4 得亿元) ``zgb``(形如 ``"112/122"``，该板块
     上涨家数/下跌家数) ``lzg``(领涨股 ``{code,name,zdf,zxj}``)；``data.total`` 为板块总数
     （实测 31 个行业板块）。
   - 该接口的 ``sort_type`` 只支持价格等字段（``zdf`` / ``zljlr`` 排序实测返回空），
     所以**先取全部板块再按 zdf 降序本地排序**，不能只取前 N 条再排序（否则是「价格前十」的子集）。

3. 实时快照（GBK）::

       GET https://qt.gtimg.cn/q=sh000001,sz399001,sh600519
       Header: Referer: https://gu.qq.com/

   - 每行 ``v_sh600519="1~贵州茅台~600519~1252.57~…";``，``~`` 分隔，实测索引：
     1=名称 2=代码 3=最新价 4=昨收 5=今开 6=成交量(手) 30=时间(YYYYMMDDHHMMSS) 31=涨跌额
     32=涨跌幅% 33=最高 34=最低 **35=``价/量(手)/额(元)`` 组合串** 37=成交额(万元) 38=换手率%。
     成交额取索引 35 的第三段（元），缺失时退化为索引 37（万元）；换手率取索引 38。
   - 腾讯口径下成交量统一为 **手**（个股 25017 手 = 新浪 2501689 股 / 100），故 ``volume_wan = 手/1e4``。
   - 未返回 / 字段不足的标的跳过并记入 ``self.last_skipped``（与新浪一致）。

4. 五档盘口与逐笔成交（``qt.gtimg.cn`` / ``stock.gtimg.cn``）::

       GET https://qt.gtimg.cn/q=sh600519                       # 五档盘口（GBK）
       GET https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600519&p=0

   - 盘口：``~`` 分隔字段 9-18 为买一~买五（价 / 量，**手**）、19-28 为卖一~卖五、
     7 / 8 为外盘 / 内盘（手）、30 为时间；解析见
     :func:`quantstudio.data.level2.parse_tencent_orderbook`，解析失败或价格为 0
     抛 ``ProviderUnavailable``；
   - 逐笔：响应 ``v_detail_data_sh600519=[p,"序号/时间/价格/涨跌/手数/金额/方向|…"]``，
     ``p`` 从 0 开始、**越大的页越晚**（每页实测 70 条，空页表示到底，全天约 60 页）；
     方向 ``B`` / ``S`` / ``M`` 是第三方「盘口方向标记」，**不是**交易所 L2 的主动买卖。

市场广度口径（**重要**）
------------------------
- 涨跌家数 = **31 个行业板块 ``zgb`` 求和**（实测合计约 9400 家，含新三板等，**大于沪深 A 股
  实际上市公司家数**，仅作市场情绪近似）；
- 两市成交额 = 上证指数 + 深证成指快照的成交额（索引 35 → 元 → /1e8 亿元）；
- 主力净流入 = 行业板块 ``zljlr`` 求和（万元 → 亿元）；
- 腾讯不提供涨跌停家数 → ``limit_up`` / ``limit_down`` 恒为 0。
以上口径会写入 :attr:`TencentProvider.last_notes`，由 Composite 透传到响应 ``meta.notes``。
"""

import datetime
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.errors import (
    DataSourceError,
    ProviderUnavailable,
    SymbolNotFound,
    ValidationError,
)
from ..core.models import Bar, IndexQuote, MarketBreadth, OrderBook, Quote, SectorQuote, Tick
from . import level2
from . import symbols as sym
from .base import BaseHTTPProvider

__all__ = ["TencentProvider"]

#: 快照行：v_sh600519="...";
_SNAPSHOT_LINE_RE = re.compile(r'v_([a-zA-Z]{2}\d{6})="([^"]*)"')
#: 快照时间字段 YYYYMMDDHHMMSS
_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")


class TencentProvider(BaseHTTPProvider):
    """腾讯（web.ifzq / proxy.finance / qt.gtimg）行情源。"""

    name = "tencent"
    referer = "https://gu.qq.com/"
    health_referer = "https://gu.qq.com/"
    health_url = "https://qt.gtimg.cn/q=sh000001"

    KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
    SNAPSHOT_URL = "https://qt.gtimg.cn/q="
    TICKS_URL = "https://stock.gtimg.cn/data/index.php"
    #: 免费源盘口固定 5 档（十档需付费 Level-2，见 ``data/level2.py``）
    ORDERBOOK_LEVELS = 5
    #: 逐笔末页探测上限（全天实测约 60 页；再大视为到底）
    TICKS_MAX_PAGE = 90

    #: 单次 K 线请求最大根数（实测 800；再大服务端会截到约 640）
    BARS_PER_REQUEST = 800
    #: 最多翻页请求次数（8 × 800 = 6400 根 ≈ 25 年；实测 600519 能回到 2001-08-27）
    #: 每翻一页多一次 HTTP 请求（本机实测 3 页约 0.7s），所以只在用户要求长区间时才会走到后面几页。
    MAX_REQUESTS = 8
    #: 单次快照最大标的数
    BATCH_SIZE = 60
    #: 抓满全部行业板块（实测 31 个）所需的最少条数
    BOARD_FETCH_COUNT = 40

    FREQ_PERIOD = {"day": "day", "week": "week", "month": "month"}
    ADJUST_PREFIX = {"qfq": "qfq", "hfq": "hfq", "none": ""}

    def __init__(self, settings=None):
        BaseHTTPProvider.__init__(self, settings)
        #: 最近一次调用被跳过的标的（未返回 / 字段不足）
        self.last_skipped: List[str] = []
        #: 最近一次调用的口径说明（供 Composite 写入 meta.notes）
        self.last_notes: List[str] = []

    # ================================================================== K 线
    def kline(self, symbol: str, days: int = 250, freq: str = "day", adjust: str = "qfq") -> List[Bar]:
        """历史 K 线（按日期升序，最多 ``days`` 根；超长自动往前翻页）。

        ``adjust="qfq"`` 返回**乘法前复权**：先按 ``hfq`` 取数，再整体缩放到最新真实价
        （见模块 docstring「复权口径」），所以最后一根收盘 == 今日真实价、所有价格 > 0、
        单日涨跌幅与 ``hfq`` 版本逐项一致。
        """
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound as exc:
            raise SymbolNotFound("腾讯无法识别标的 %r：%s" % (symbol, exc))
        freq_key = str(freq or "day").strip().lower()
        if freq_key not in self.FREQ_PERIOD:
            raise ValidationError("K 线周期仅支持 day/week/month，当前为 %r" % (freq,), field="freq")
        adjust_key = str(adjust or "qfq").strip().lower()
        if adjust_key not in self.ADJUST_PREFIX:
            raise ValidationError("复权方式仅支持 qfq/hfq/none，当前为 %r" % (adjust,), field="adjust")
        try:
            limit = int(days)
        except (TypeError, ValueError):
            raise ValidationError("days 需为整数，当前为 %r" % (days,), field="days")
        if limit <= 0:
            raise ValidationError("days 需为正整数，当前为 %d" % limit, field="days")

        tx_symbol = self._code_to_sina(code)
        period = self.FREQ_PERIOD[freq_key]
        # 腾讯的 qfq 是减法复权（深历史出负价），所以 qfq 也按 hfq 取数，
        # 拿到序列后再整体缩放到最新真实价（见模块 docstring「复权口径」）。
        scale_to_latest = adjust_key == "qfq"
        prefix = self.ADJUST_PREFIX["hfq"] if scale_to_latest else self.ADJUST_PREFIX[adjust_key]
        hfq_key = "%s%s" % (self.ADJUST_PREFIX["hfq"], period)

        merged: Dict[str, List[str]] = {}
        end = ""
        hit_key = ""
        for _attempt in range(self.MAX_REQUESTS):
            want = min(limit - len(merged), self.BARS_PER_REQUEST)
            if want <= 0:
                break
            param = "%s,%s,,%s,%d,%s" % (tx_symbol, period, end, want, prefix)
            payload = self._get_json(self.KLINE_URL, params={"param": param})
            node = self._node(payload, tx_symbol)
            page_key, rows = self._kline_section(node, prefix, period)
            if not rows:
                break
            if not hit_key:
                hit_key = page_key
            for row in rows:
                if row and row[0]:
                    merged[str(row[0])] = row
            name = self._qt_name(node.get("qt") or {}, tx_symbol)
            if name:
                self.remember_name(code, name)
            if len(rows) < want or len(merged) >= limit or freq_key != "day":
                break                      # 数据已被服务端截断（或周/月线足够长）
            first_day = min(merged)
            end = self._prev_day(first_day)
            if not end:
                break

        if not merged:
            raise DataSourceError("腾讯未返回 %s 的 K 线数据" % code)
        ordered = [merged[key] for key in sorted(merged)]
        if scale_to_latest and hit_key == hfq_key:
            # 只有真的拿到 hfq 序列才缩放：命中 day（指数）或旧 qfq 键时 k=1 原样返回，
            # 否则会把减法复权的负价一起带出来。
            ordered = self._scale_to_latest(ordered, tx_symbol, period)
        bars = self._build_bars(ordered)
        if not bars:
            raise DataSourceError("腾讯 K 线解析失败：%s" % code)
        return bars[-limit:]

    @classmethod
    def _kline_section(cls, node: Dict[str, Any], prefix: str,
                       period: str) -> Tuple[str, List[List[Any]]]:
        """取 K 线数组，返回 ``(命中的键名, 行列表)``。

        键优先级：``{adjust}{period}``（``hfqday``）→ ``{period}``（``day``：指数即使请求
        复权也只返回无复权键）→ ``hfq`` 前缀时兜底 ``qfq{period}``（极少数响应只给 qfq 键）。

        命中 ``day`` / ``qfq`` 键时调用方**不做缩放**：那两者不是后复权序列。``qfq`` 键还要
        过一道 :meth:`_all_positive`——腾讯的 qfq 是减法复权，深历史会把老价格减成负数，
        遇到这种响应宁可不返回（上层报「未返回 K 线数据」），也不把负数当价格传出去。
        """
        keys = ["%s%s" % (prefix, period), period]
        if prefix == cls.ADJUST_PREFIX["hfq"]:
            keys.append("qfq%s" % period)
        for key in keys:
            rows = node.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            rows = [row for row in rows if isinstance(row, (list, tuple))]
            if key.startswith("qfq") and not cls._all_positive(rows):
                continue
            return key, rows
        return "", []

    @classmethod
    def _all_positive(cls, rows: List[Sequence[Any]]) -> bool:
        """所有行的收盘都 > 0（腾讯 qfq 的减法复权会把老价格打成 0 / 负数）。"""
        if not rows:
            return False
        for row in rows:
            if cls._row_close(row) <= 0:
                return False
        return True

    def _scale_to_latest(self, rows: List[Sequence[Any]],
                         tx_symbol: str, period: str) -> List[List[Any]]:
        """后复权行整体缩放到最新真实价（真正的乘法前复权）。

        缩放系数 ``k = raw_last / hfq_last_close``；``raw_last`` 取不到或末根收盘非正时
        原样返回（k=1，数据仍可用）。只乘价格字段（开 / 收 / 高 / 低），成交量不动，因此
        :meth:`_build_bars` 自算的 ``change_pct`` 与 hfq 版本逐项一致。
        """
        raw_last = self._latest_raw_close(tx_symbol, period)
        base = self._row_close(rows[-1]) if rows else 0.0
        if raw_last <= 0 or base <= 0:
            return list(rows)
        factor = raw_last / base
        if not (factor > 0) or factor == 1.0:
            return list(rows)
        scaled: List[List[Any]] = []
        for row in rows:
            try:
                prices = [float(row[1]) * factor, float(row[2]) * factor,
                          float(row[3]) * factor, float(row[4]) * factor]
            except (TypeError, ValueError, IndexError):
                scaled.append(list(row))       # 异常行原样保留（_build_bars 会跳过）
                continue
            scaled.append([row[0]] + prices + list(row[5:]))
        return scaled

    def _latest_raw_close(self, tx_symbol: str, period: str) -> float:
        """最新**不复权**收盘价（``adjust="none"`` + ``days=1``，1 次额外请求）。

        只认 ``{period}`` 键（``day``）：响应里只有复权键时不用它——qfq 是减法复权、
        hfq 不是真实价。任何异常都返回 0，让调用方跳过缩放（k=1，数据保持可用）。
        """
        try:
            param = "%s,%s,,,%d,%s" % (tx_symbol, period, 1, self.ADJUST_PREFIX["none"])
            payload = self._get_json(self.KLINE_URL, params={"param": param})
            node = self._node(payload, tx_symbol)
            key, rows = self._kline_section(node, self.ADJUST_PREFIX["none"], period)
        except Exception:                      # noqa: BLE001 - 缩放是锦上添花，失败就 k=1
            return 0.0
        if key != period or not rows:
            return 0.0
        newest_date = ""
        newest_close = 0.0
        for row in rows:
            if not row:
                continue
            close = self._row_close(row)
            date = str(row[0])[:10]
            if close > 0 and date >= newest_date:
                newest_date, newest_close = date, close
        return newest_close

    @staticmethod
    def _row_close(row: Sequence[Any]) -> float:
        """K 线行第 3 段是收盘（``[日期, 开, 收, 高, 低, 量]``）；缺失 / 非数字返回 0。"""
        try:
            return float(row[2])
        except (TypeError, ValueError, IndexError):
            return 0.0

    @staticmethod
    def _build_bars(rows: List[Sequence[Any]]) -> List[Bar]:
        """``[日期, 开, 收, 高, 低, 成交量(手)]`` → :class:`Bar`（含相邻收盘自算涨跌幅）。"""
        bars: List[Bar] = []
        prev_close = 0.0
        for row in rows:
            if len(row) < 6:
                continue
            try:
                date = str(row[0])[:10]
                open_price = float(row[1])
                close = float(row[2])
                high = float(row[3])
                low = float(row[4])
                volume_hand = float(row[5])
            except (TypeError, ValueError):
                continue
            change_pct = ((close / prev_close - 1) * 100) if prev_close else 0.0
            bars.append(Bar(
                date=date,
                open=open_price,
                close=close,
                high=high,
                low=low,
                volume_wan=volume_hand / 1e4,     # 手 → 万手（腾讯口径统一为手）
                amount_yi=0.0,                    # K 线接口不返回成交额
                change_pct=round(change_pct, 4),
                turnover_pct=0.0,
            ))
            prev_close = close
        return bars

    @staticmethod
    def _prev_day(day: str) -> str:
        try:
            current = datetime.date(*[int(part) for part in str(day)[:10].split("-")])
        except (TypeError, ValueError):
            return ""
        return (current - datetime.timedelta(days=1)).isoformat()

    # ================================================================== 快照
    def _snapshot(self, codes: Sequence[str]) -> Dict[str, List[str]]:
        """``qt.gtimg.cn`` 快照（GBK），返回 ``{规范化代码: 字段列表}``。"""
        normal = self._normalize_all(codes)
        if not normal:
            return {}
        out: Dict[str, List[str]] = {}
        for start in range(0, len(normal), self.BATCH_SIZE):
            chunk = normal[start:start + self.BATCH_SIZE]
            url = self.SNAPSHOT_URL + ",".join(self._code_to_sina(code) for code in chunk)
            text = self._get_text(url, referer=self.referer)
            for match in _SNAPSHOT_LINE_RE.finditer(text or ""):
                raw_symbol = match.group(1)
                payload = (match.group(2) or "").strip()
                if not payload:
                    continue                        # 未返回 / 停牌无数据
                try:
                    code = sym.normalize(raw_symbol)
                except SymbolNotFound:
                    continue
                fields = payload.split("~")
                if len(fields) < 30:
                    continue
                out[code] = fields
        return out

    def latest_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """批量实时快照（``volume_wan`` = 手/1e4；含换手率、成交额）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._snapshot(codes)
        self.last_skipped = [code for code in codes if code not in rows]
        if not rows:
            raise DataSourceError(
                "腾讯未返回任何有效行情（%d 个标的全部为空：%s）"
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
            raise DataSourceError("腾讯行情全部无法解析（%d 个标的）" % len(codes))
        return out

    def _to_quote(self, code: str, fields: List[str]) -> Optional[Quote]:
        price = self._f(fields, 3)
        prev_close = self._f(fields, 4)
        if price <= 0 and prev_close <= 0:
            return None                      # 停牌 / 退市
        name = self._s(fields, 1)
        if name:
            self.remember_name(code, name)
        change = self._f(fields, 31)
        if not change and prev_close:
            change = price - prev_close
        change_pct = self._f(fields, 32)
        if not change_pct and prev_close:
            change_pct = (price / prev_close - 1) * 100
        return Quote(
            code=code,
            name=name or (sym.INDEX_NAMES.get(code) or ""),
            price=price,
            prev_close=prev_close,
            open=self._f(fields, 5),
            high=self._f(fields, 33),
            low=self._f(fields, 34),
            change=round(change, 4),
            change_pct=round(change_pct, 4),
            volume_wan=self._f(fields, 6) / 1e4,          # 手 → 万手
            amount_yi=self._amount_yuan(fields) / 1e8,    # 元 → 亿元
            turnover_pct=self._f(fields, 38),
            # 以下字段按腾讯行情行的实测位置解析（实测 600519.SH：38 换手率 0.20、
            # 39 PE(TTM) 19.23、45 总市值 15658.15 亿、46 PB 6.23、49 量比 1.26）。
            # 加范围校验，字段位置漂移时宁可为 None 也不写入可疑数值。
            pe_ttm=self._plausible(fields, 39, 0, 10000),
            pb=self._plausible(fields, 46, 0, 1000),
            market_cap_yi=self._plausible(fields, 45, 1, 1e6),
            volume_ratio=self._plausible(fields, 49, 0, 1000),
            ts=self._ts(fields),
            source=self.name,
        )

    def index_quotes(self, symbols: Sequence[str]) -> List[IndexQuote]:
        """指数快照（``qt.gtimg.cn`` 同结构；成交额取 ``价/量/额`` 组合串）。"""
        codes = self._normalize_all(symbols)
        if not codes:
            return []
        rows = self._snapshot(codes)
        out: List[IndexQuote] = []
        for code in codes:
            fields = rows.get(code)
            if not fields:
                continue
            point = self._f(fields, 3)
            prev_close = self._f(fields, 4)
            if point <= 0 and prev_close <= 0:
                continue
            name = self._s(fields, 1)
            if name:
                self.remember_name(code, name)
            change = self._f(fields, 31)
            if not change and prev_close:
                change = point - prev_close
            change_pct = self._f(fields, 32)
            if not change_pct and prev_close:
                change_pct = (point / prev_close - 1) * 100
            out.append(IndexQuote(
                code=code,
                name=name or (sym.INDEX_NAMES.get(code) or ""),
                point=point,
                prev_close=prev_close,
                change=round(change, 4),
                change_pct=round(change_pct, 4),
                amount_yi=self._amount_yuan(fields) / 1e8,
                volume_wan=self._f(fields, 6) / 1e4,
                source=self.name,
            ))
        if not out:
            raise DataSourceError("腾讯未返回任何指数快照")
        return out

    @classmethod
    def _amount_yuan(cls, fields: List[str]) -> float:
        """成交额（元）：优先取索引 35 的 ``价/量(手)/额(元)`` 第三段，退化到索引 37（万元）。"""
        combined = cls._s(fields, 35)
        if "/" in combined:
            parts = combined.split("/")
            if len(parts) >= 3:
                amount = cls._num(parts[2])
                if amount > 0:
                    return amount
        return cls._num(cls._s(fields, 37)) * 1e4      # 万元 → 元

    @classmethod
    def _ts(cls, fields: List[str]) -> str:
        """索引 30 的 ``YYYYMMDDHHMMSS`` → ``YYYY-MM-DD HH:MM:SS``。"""
        raw = cls._s(fields, 30)
        match = _TS_RE.match(raw)
        if not match:
            return raw
        return "%s-%s-%s %s:%s:%s" % match.groups()

    @staticmethod
    def _s(fields: List[str], index: int) -> str:
        if index < 0 or index >= len(fields):
            return ""
        return str(fields[index]).strip()

    @classmethod
    def _f(cls, fields: List[str], index: int) -> float:
        return cls._num(cls._s(fields, index))

    @classmethod
    def _plausible(cls, fields: List[str], index: int, low: float, high: float) -> Optional[float]:
        """解析可选估值字段：缺失、非数字或超出合理区间时返回 None（防止字段漂移写出脏数据）。"""
        raw = cls._s(fields, index)
        if not raw or raw in ("-", "--"):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0 or value < low or value > high:
            return None
        return value

    @staticmethod
    def _qt_name(qt: Dict[str, Any], tx_symbol: str) -> str:
        fields = qt.get(tx_symbol)
        if isinstance(fields, list) and len(fields) > 1:
            return str(fields[1]).strip()
        return ""

    # ============================================================== 盘口 / 逐笔
    def orderbook(self, code: str) -> OrderBook:
        """五档盘口（``qt.gtimg.cn``，GBK；含外盘 / 内盘）。

        解析失败或价格为 0（停牌 / 未返回）时抛 :class:`ProviderUnavailable`，
        由 Composite 降级到新浪 / 缓存 / CSV / 示例。
        """
        try:
            norm = sym.normalize(code)
        except SymbolNotFound as exc:
            raise SymbolNotFound("腾讯无法识别标的 %r：%s" % (code, exc))
        url = self.SNAPSHOT_URL + self._code_to_sina(norm)
        text = self._get_text(url, referer=self.referer, encoding="gbk")
        book = level2.parse_tencent_orderbook(text, code=norm)
        if book is None or book.price <= 0:
            raise ProviderUnavailable(
                "腾讯未返回 %s 的五档盘口（解析失败或价格全 0）" % norm
            )
        if book.name:
            self.remember_name(norm, book.name)
        return book

    def ticks(self, code: str, limit: int = 600) -> List[Tick]:
        """当日逐笔成交（``stock.gtimg.cn/data/index.php?appn=detail``）。

        ``p`` 从 0 开始、**越大的页越晚**（每页约 70 条，空页表示到底）。实现先向后
        探测最后一页（1 / 2 / 4 / … 倍速探测 + 二分回退），再从末页向前翻页收集，
        最后按时间升序返回、条数不超过 ``limit``；全部为空时抛
        :class:`ProviderUnavailable`。方向 ``B`` / ``S`` / ``M`` 是第三方「盘口方向
        标记」，不是交易所 L2 的主动买卖。
        """
        try:
            norm = sym.normalize(code)
        except SymbolNotFound as exc:
            raise SymbolNotFound("腾讯无法识别标的 %r：%s" % (code, exc))
        try:
            top = int(limit)
        except (TypeError, ValueError):
            raise ValidationError("limit 需为整数，当前为 %r" % (limit,), field="limit")
        if top <= 0:
            return []
        tx_symbol = self._code_to_sina(norm)
        last_page = self._last_tick_page(tx_symbol)
        if last_page is None:
            raise ProviderUnavailable("腾讯未返回 %s 的逐笔成交明细" % norm)
        pages: List[List[Tick]] = []
        count = 0
        for page in range(last_page, -1, -1):
            page_ticks = self._tick_page(tx_symbol, page)
            if not page_ticks:
                continue                     # 单页为空：跳过，不阻断更早的页
            pages.append(page_ticks)
            count += len(page_ticks)
            if count >= top:
                break
        merged: List[Tick] = []
        for page_ticks in reversed(pages):
            merged.extend(page_ticks)
        if not merged:
            raise ProviderUnavailable("腾讯未返回 %s 的逐笔成交明细" % norm)
        merged.sort(key=lambda tick: tick.time)
        return merged[-top:]

    def _tick_page(self, tx_symbol: str, page: int) -> List[Tick]:
        """取逐笔某一页（``p`` 从 0 开始，越大的页越晚；空页返回空列表）。"""
        text = self._get_text(self.TICKS_URL, referer=self.referer, params={
            "appn": "detail", "action": "data", "c": tx_symbol, "p": page,
        })
        return level2.parse_tencent_ticks(text, code=tx_symbol)

    def _last_tick_page(self, tx_symbol: str) -> Optional[int]:
        """探测当日最后一页：1 / 2 / 4 / … 倍速找到首个空页，再二分回退。

        全天约 60 页（每页约 70 条 ≈ 最近 4000 笔），倍速 + 二分把探测控制在约 12 次
        请求；第 0 页也为空时返回 ``None``（该标的当日无逐笔）。
        """
        if not self._tick_page(tx_symbol, 0):
            return None
        low, high = 0, None
        page = 1
        while page <= self.TICKS_MAX_PAGE:
            if not self._tick_page(tx_symbol, page):
                high = page
                break
            low = page
            page *= 2
        if high is None:
            high = self.TICKS_MAX_PAGE + 1
        while low + 1 < high:
            mid = (low + high) // 2
            if self._tick_page(tx_symbol, mid):
                low = mid
            else:
                high = mid
        return low

    # ================================================================== 板块
    def sectors(self, limit: int = 20) -> List[SectorQuote]:
        """行业板块（先把全部板块取回，再按 ``zdf`` 降序取前 ``limit`` 个）。"""
        try:
            top = max(1, int(limit))
        except (TypeError, ValueError):
            top = 20
        rows = self._board_rows(max(top, self.BOARD_FETCH_COUNT))
        out: List[SectorQuote] = []
        for row in rows:
            item = self._to_sector(row)
            if item is not None:
                out.append(item)
        if not out:
            raise DataSourceError("腾讯未返回行业板块数据")
        out.sort(key=lambda item: item.change_pct, reverse=True)
        return out[:top] if top < len(out) else out

    def _board_rows(self, count: int) -> List[Dict[str, Any]]:
        payload = self._get_json(self.RANK_URL, params={
            "board_type": "hy",
            "sort_type": "price",
            "direct": "down",
            "offset": 0,
            "count": min(max(count, 1), 100),
        })
        if not isinstance(payload, dict):
            raise DataSourceError("腾讯板块响应格式异常：%r" % (payload,))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError("腾讯板块响应缺少 data（code=%s）" % payload.get("code"))
        rows = [row for row in (data.get("rank_list") or []) if isinstance(row, dict)]
        if not rows:
            raise DataSourceError("腾讯未返回行业板块数据")
        return rows

    def _to_sector(self, row: Dict[str, Any]) -> Optional[SectorQuote]:
        name = str(row.get("name") or "").strip()
        code = str(row.get("code") or "").strip()
        if not name and not code:
            return None
        up_count, down_count = self._split_zgb(row.get("zgb"))
        leading = row.get("lzg") if isinstance(row.get("lzg"), dict) else {}
        leading_code = str(leading.get("code") or "").strip()
        if leading_code:
            try:
                leading_code = sym.normalize(leading_code)      # sh601579 → 601579.SH
            except SymbolNotFound:
                pass
        return SectorQuote(
            code=code,
            name=name,
            change_pct=self._num(row.get("zdf")),
            net_inflow_yi=self._num(row.get("zljlr")) / 1e4,     # 万元 → 亿元
            up_count=up_count,
            down_count=down_count,
            leading_stock=str(leading.get("name") or "").strip(),
            leading_code=leading_code,
        )

    @staticmethod
    def _split_zgb(value: Any):
        """``"112/122"`` → ``(112, 122)``（该板块上涨家数 / 下跌家数）。"""
        text = str(value or "").strip()
        if "/" not in text:
            return 0, 0
        head, _, tail = text.partition("/")
        try:
            return int(float(head)), int(float(tail))
        except ValueError:
            return 0, 0

    # ================================================================== 广度
    def breadth(self) -> MarketBreadth:
        """市场广度与资金（腾讯行业板块口径，见模块 docstring）。"""
        rows = self._board_rows(100)
        up = down = 0
        main_net_inflow_wan = 0.0
        for row in rows:
            part_up, part_down = self._split_zgb(row.get("zgb"))
            up += part_up
            down += part_down
            main_net_inflow_wan += self._num(row.get("zljlr"))

        total_amount_yi = 0.0
        notes: List[str] = []
        try:
            indices = self._snapshot(["000001.SH", "399001.SZ"])
            for code in ("000001.SH", "399001.SZ"):
                fields = indices.get(code)
                if fields:
                    total_amount_yi += self._amount_yuan(fields) / 1e8
        except DataSourceError as exc:          # 指数快照失败不影响家数与净流入
            notes.append("腾讯指数快照不可用：%s" % exc)
        if total_amount_yi <= 0:
            notes.append("两市成交额不可用（腾讯指数快照缺失）")
        notes.append(
            "腾讯行业板块口径（含新三板，合计家数偏大）：涨跌家数=%d 个行业板块 zgb 求和；"
            "两市成交额=上证+深证成指快照成交额；主力净流入=板块 zljlr 求和（万元→亿元）；"
            "腾讯不提供涨跌停家数，limit_up/limit_down 置 0" % len(rows)
        )
        self.last_notes = notes
        return MarketBreadth(
            up=up,
            down=down,
            flat=0,                    # 腾讯只给上涨/下跌家数
            limit_up=0,
            limit_down=0,
            total=up + down,
            total_amount_yi=round(total_amount_yi, 2),
            main_net_inflow_yi=round(main_net_inflow_wan / 1e4, 2),
            north_net_inflow_yi=None,
            source=self.name,
        )

    # ================================================================== 名称
    def resolve_name(self, symbol: str) -> Optional[str]:
        """代码 → 名称（``qt.gtimg.cn`` 快照第 1 字段，带磁盘缓存）。"""
        try:
            code = sym.normalize(symbol)
        except SymbolNotFound:
            return None
        cached = self._cache_get("name", [code])
        if cached:
            return str(cached)
        known = sym.INDEX_NAMES.get(code)
        try:
            rows = self._snapshot([code])
        except DataSourceError:
            return known
        fields = rows.get(code)
        if not fields:
            return known
        name = self._s(fields, 1)
        if not name:
            return known
        self.remember_name(code, name)
        self._cache_set("name", [code], name, self.ttl.name)
        return name

    # ================================================================== 自检
    def _probe(self) -> str:
        rows = self._snapshot(["000001.SH"])
        fields = rows.get("000001.SH")
        if not fields:
            raise DataSourceError("腾讯未返回上证指数快照")
        name = self._s(fields, 1) or "上证指数"
        point = self._f(fields, 3)
        if point <= 0:
            raise DataSourceError("腾讯上证指数点数为 0")
        return "腾讯可用：%s %.2f（时间 %s）" % (name, point, self._ts(fields))

    # ================================================================== 内部
    @staticmethod
    def _node(payload: Any, tx_symbol: str) -> Dict[str, Any]:
        """取 ``data[symbol]`` 节点。"""
        if not isinstance(payload, dict):
            raise DataSourceError("腾讯 K 线响应格式异常：%r" % (payload,))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DataSourceError("腾讯 K 线响应缺少 data（code=%s）" % payload.get("code"))
        node = data.get(tx_symbol)
        if not isinstance(node, dict):
            raise DataSourceError("腾讯 K 线响应中没有 %s（code=%s）" % (tx_symbol, payload.get("code")))
        return node
