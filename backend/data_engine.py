# -*- coding: utf-8 -*-
"""示例数据引擎:确定性生成行情 / 策略 / 回测 / 持仓演示数据。

重要说明
--------
本工程为「界面 + 示例数据演示」形态,所有行情、策略绩效、回测结果、
持仓数据均为随机生成的演示数据(固定种子,每次启动一致),不代表任何
真实市场行情、历史回测或投资业绩,请勿用于真实投资决策。

口径说明
--------
- 所有金额:人民币(元/万元/亿元,按字段单位标注)
- 成交额 = 总市值 x 换手率;成交量(万手) = 成交额 / 成交均价
- 两市成交额 = 沪市成交额 + 深市成交额(不做重复加总)
- 持仓市值 = 数量 x 现价;累计盈亏 = (现价 - 成本价) x 数量
- 总资产 = 现金 + 全部持仓市值(账目勾稽)
- K 线末根 = 当前交易日:收盘价与行情现价一致,成交量与行情成交量一致
- 权益曲线末点 = 当前总资产(卡片与曲线自洽)
- 回测净值基准日 = 1.0;最大回撤 = max(1 - 净值/历史峰值)
- 年化收益 = (1 + 日均收益)^252 - 1;夏普 = 日均收益/日收益标准差 x sqrt(252)
- 随机种子固定,且跨进程稳定(不使用受 PYTHONHASHSEED 影响的内置 hash),
  因此每次启动生成的数据完全一致
"""

import json
import math
import os
import random
import sys
import zlib
from datetime import date, timedelta

_SEED = 20260921

# 数据时点(演示):行情 / K 线 / 持仓对应交易日
AS_OF = "2026-09-21 15:00"
LAST_TRADE_DAY = date(2026, 9, 21)
# 回测区间(上一交易日为区间末日)
BACKTEST_START = date(2023, 1, 3)
BACKTEST_END = date(2026, 9, 18)

# K 线参数边界
DEFAULT_KLINE_DAYS = 120
MIN_KLINE_DAYS = 1
MAX_KLINE_DAYS = 920
# K 线缓存条数上限(每个「标的+天数」一条,避免脚本遍历 days 造成内存膨胀)
KLINE_CACHE_MAX = 64

# 统一基准(所有策略对比同一条基准净值曲线)
BENCHMARK_TARGET = 0.12
BENCHMARK_SIGMA = 0.0110
BENCHMARK_SALT = 100

# 示例股票池(虚构标的,避免与真实行情混淆)
STOCKS = [
    {"code": "999001", "name": "云帆科技", "industry": "半导体"},
    {"code": "999002", "name": "远航智能", "industry": "人工智能"},
    {"code": "999003", "name": "蓝海生物", "industry": "医药生物"},
    {"code": "999004", "name": "鼎新制造", "industry": "高端装备"},
    {"code": "999005", "name": "皓月能源", "industry": "新能源"},
    {"code": "999006", "name": "金穗农业", "industry": "农林牧渔"},
    {"code": "999007", "name": "青云软件", "industry": "计算机"},
    {"code": "999008", "name": "星河通信", "industry": "通信"},
    {"code": "999009", "name": "恒信消费", "industry": "食品饮料"},
    {"code": "999010", "name": "中辰环保", "industry": "环保"},
]

# 策略类别(新建策略时校验)
CATEGORY_CHOICES = ["趋势跟踪", "动量", "震荡市", "稳健", "均值回归", "自定义"]
STATUS_CHOICES = ["running", "paused"]
FREQ_CHOICES = ["日线", "周度", "月度", "盘中"]

# 示例策略定义(参数均为演示值,可二次开发接入真实策略)
STRATEGIES = [
    {
        "id": "st_ma_cross",
        "name": "双均线趋势策略",
        "category": "趋势跟踪",
        "desc": "MA20 上穿 MA60 产生买入信号,下穿产生卖出信号,适合单边趋势行情。",
        "params": {"short_ma": 20, "long_ma": 60, "position_pct": 0.95},
        "status": "running",
        "universe": "沪深300成分股(演示)",
        "freq": "日线",
    },
    {
        "id": "st_momentum",
        "name": "动量轮动策略",
        "category": "动量",
        "desc": "按过去 20 个交易日涨幅对候选池排序,持有动量最强的 3 只标的,每月调仓。",
        "params": {"lookback": 20, "top_n": 3, "rebalance": "monthly"},
        "status": "running",
        "universe": "宽基ETF(演示)",
        "freq": "月度",
    },
    {
        "id": "st_grid",
        "name": "网格交易策略",
        "category": "震荡市",
        "desc": "在波动区间内按固定步长分档低买高卖,赚取震荡波段收益,标的与区间可配置。",
        "params": {"grid_pct": 0.03, "price_low": 8.0, "price_high": 15.0, "levels": 10},
        "status": "running",
        "universe": "指定标的(演示)",
        "freq": "盘中",
    },
    {
        "id": "st_lowvol",
        "name": "低波动率优选策略",
        "category": "稳健",
        "desc": "从候选池中挑选过去 60 日波动率最低的 5 只股票等权持有,追求低回撤。",
        "params": {"lookback": 60, "top_n": 5, "weight": "equal"},
        "status": "paused",
        "universe": "中证800成分股(演示)",
        "freq": "周度",
    },
    {
        "id": "st_rsi",
        "name": "RSI 均值回归策略",
        "category": "均值回归",
        "desc": "RSI(14) 低于 30 超卖买入,高于 70 超买卖出,适用于震荡行情。",
        "params": {"rsi_period": 14, "buy_th": 30, "sell_th": 70},
        "status": "running",
        "universe": "自选股票池(演示)",
        "freq": "日线",
    },
    {
        "id": "st_turtle",
        "name": "海龟突破策略",
        "category": "趋势跟踪",
        "desc": "价格突破 20 日高点开仓,跌破 10 日低点止损离场,经典趋势跟踪框架。",
        "params": {"entry_high": 20, "exit_low": 10, "atr_mult": 2.0},
        "status": "paused",
        "universe": "商品+股票(演示)",
        "freq": "日线",
    },
]

# 每个内置策略的 (目标累计收益, 日收益波动率)
BUILTIN_PROFILES = [
    (0.48, 0.0090),
    (0.36, 0.0095),
    (0.22, 0.0065),
    (0.18, 0.0060),
    (0.28, 0.0075),
    (0.42, 0.0095),
]

# 每只样例持仓:数量 + 相对现价的浮动收益率(成本价由现价反推,保证演示账户盈亏处于合理区间)
HOLDINGS = [
    {"code": "999001", "qty": 12000, "ret": -0.18},
    {"code": "999002", "qty": 8000, "ret": 0.26},
    {"code": "999003", "qty": 15000, "ret": -0.12},
    {"code": "999005", "qty": 6000, "ret": -0.24},
    {"code": "999007", "qty": 20000, "ret": 0.35},
    {"code": "999010", "qty": 10000, "ret": 0.08},
]
CASH = 284560.00


def trading_days(start: date, end: date):
    """返回 [start, end] 区间内的工作日序列(简化,不含法定节假日)。"""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def stable_seed(*parts) -> int:
    """跨进程稳定的整数种子。

    不使用内置 hash():字符串哈希受 PYTHONHASHSEED 随机化影响,
    会导致同一份演示数据在不同进程/不同次启动之间发生变化。
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return zlib.crc32(raw) % 1000000


class DataEngine:
    """示例数据引擎:所有生成函数可重复调用且结果确定。

    参数
    ----
    store_path: 自定义策略的持久化文件路径(为 None 时仅保存在内存中)
    """

    def __init__(self, store_path=None):
        self._seed = _SEED
        self._store_path = store_path
        self._quotes_cache = None
        self._kline_cache = {}
        self._backtest_cache = {}
        self._benchmark_cache = None
        self._strategies = [dict(s, builtin=True) for s in STRATEGIES]
        self._load_user_strategies()

    # ---------------- 通用 ----------------
    def as_of(self) -> str:
        return AS_OF

    def _rng(self, salt: int = 0) -> random.Random:
        return random.Random(self._seed + salt)

    # ---------------- 行情看板 ----------------
    def market_overview(self):
        rng = self._rng(1)
        indices = []
        # (名称, 代码, 基准点位, 基准成交额 亿元)
        base = [
            ("上证指数", "000001", 3318.42, 5100.0),
            ("深证成指", "399001", 10624.35, 6400.0),
            ("创业板指", "399006", 2147.88, 2500.0),
            ("科创50", "000688", 1023.51, 620.0),
        ]
        for name, code, base_pt, base_amount in base:
            chg = rng.uniform(-1.2, 1.6)
            pt = round(base_pt * (1 + chg / 100.0), 2)
            amount = round(base_amount * (1 + rng.uniform(-0.16, 0.20)), 0)  # 亿元
            indices.append({
                "code": code, "name": name, "point": pt,
                "change_pct": round(chg, 2), "amount_yi": amount,
            })
        breadth = {
            "up": rng.randint(2600, 3600),
            "down": rng.randint(1400, 2400),
            "flat": rng.randint(80, 400),
        }
        sh_amount = next(i["amount_yi"] for i in indices if i["code"] == "000001")
        sz_amount = next(i["amount_yi"] for i in indices if i["code"] == "399001")
        return {
            "indices": indices,
            "breadth": breadth,
            # 两市成交额 = 沪市 + 深市(不重复累加创业板/科创等子板块成交额)
            "total_amount_yi": round(sh_amount + sz_amount, 0),
            "main_net_inflow_yi": round(rng.uniform(-180, 260), 2),
            "north_net_inflow_yi": round(rng.uniform(-60, 120), 2),
        }

    def market_sectors(self):
        rng = self._rng(2)
        names = ["半导体", "新能源", "人工智能", "医药生物", "高端装备",
                 "食品饮料", "计算机", "通信", "环保", "农林牧渔",
                 "房地产", "银行", "证券", "有色金属", "军工"]
        sectors = []
        for i, name in enumerate(names):
            chg = rng.uniform(-3.2, 4.5)
            inflow = rng.uniform(-60, 90)
            sectors.append({
                "name": name, "change_pct": round(chg, 2),
                "net_inflow_yi": round(inflow, 2),
                "leading_stock": STOCKS[i % len(STOCKS)]["name"],
            })
        sectors.sort(key=lambda s: s["change_pct"], reverse=True)
        return sectors

    def _quotes(self):
        """生成并缓存自选股行情(持仓 / K 线会引用同一份行情以保证一致)。"""
        if self._quotes_cache is not None:
            return self._quotes_cache
        rng = self._rng(3)
        quotes = []
        for i, s in enumerate(STOCKS):
            prev_close = round(rng.uniform(5.0, 60.0), 2)
            # 前三只给更强涨幅,让看板有层次
            if i < 3:
                chg = rng.uniform(1.2, 5.8)
            elif i == 9:
                chg = rng.uniform(-5.5, -1.0)
            else:
                chg = rng.uniform(-2.8, 3.5)
            price = round(prev_close * (1 + chg / 100.0), 2)
            market_cap_yi = round(rng.uniform(60, 1200), 2)
            turnover_pct = round(rng.uniform(0.8, 8.5), 2)
            # 口径:成交额 = 总市值 x 换手率;成交量(万手) = 成交额 / 成交均价
            amount_yi = round(market_cap_yi * turnover_pct / 100.0, 2)
            volume_wan = max(1, int(round(amount_yi * 1e8 / (price * 100) / 1e4)))
            quotes.append({
                "code": s["code"], "name": s["name"], "industry": s["industry"],
                "price": price, "prev_close": prev_close,
                "change_pct": round(chg, 2),
                "volume_wan": volume_wan, "amount_yi": amount_yi,
                "turnover_pct": turnover_pct,
                "pe_ttm": round(rng.uniform(12, 85), 2),
                "pb": round(rng.uniform(1.2, 9.5), 2),
                "market_cap_yi": market_cap_yi,
            })
        self._quotes_cache = quotes
        return quotes

    def market_quotes(self):
        return self._quotes()

    def quote(self, code: str):
        """按代码取单只标的行情(不存在返回 None)。"""
        code = str(code or "").strip()
        return next((q for q in self._quotes() if q["code"] == code), None)

    def market_kline(self, code: str = "999001", days: int = DEFAULT_KLINE_DAYS):
        """生成某标的最近 N 个交易日的 K 线。

        - K 线数量严格等于 days(已由调用方夹取到 [MIN_KLINE_DAYS, MAX_KLINE_DAYS]);
        - 末根为当前交易日:收盘价 = 行情现价,成交量 = 行情成交量;
        - 未知标的返回 None(由接口层返回 404)。
        """
        code = str(code or "").strip()
        days = int(days)
        if days < MIN_KLINE_DAYS:
            days = MIN_KLINE_DAYS
        if days > MAX_KLINE_DAYS:
            days = MAX_KLINE_DAYS
        quote = self.quote(code)
        if quote is None:
            return None
        key = (code, days)
        if key in self._kline_cache:
            return self._kline_cache[key]

        # 稳定的随机种子(不含内置 hash,保证跨进程一致)
        rng = self._rng(stable_seed("kline", code, days))
        prev = quote["price"]
        # 从后往前倒推生成,保证最后一根 K 线收盘价与行情一致
        closes = [prev]
        for _ in range(days - 1):
            ret = rng.gauss(0.0004, 0.022)
            closes.append(max(3.0, closes[-1] / (1 + ret)))
        closes.reverse()

        # 取足够长的日历窗口,确保恰好能截出 days 个工作日
        start = LAST_TRADE_DAY - timedelta(days=int(days * 1.5) + 15)
        day_list = trading_days(start, LAST_TRADE_DAY)[-days:]

        base_vol = quote["volume_wan"]
        klines = []
        for i, d in enumerate(day_list):
            c = closes[i]
            o = round(c * (1 + rng.uniform(-0.02, 0.02)), 2)
            h = round(max(o, c) * (1 + rng.uniform(0.001, 0.03)), 2)
            l = round(min(o, c) * (1 - rng.uniform(0.001, 0.03)), 2)
            v = max(1, int(round(base_vol * rng.uniform(0.45, 1.75))))
            klines.append({
                "date": d.isoformat(), "open": o, "close": round(c, 2),
                "high": h, "low": l, "volume_wan": v,
            })
        # 末根(当前交易日)成交量与行情保持一致
        klines[-1]["volume_wan"] = base_vol
        if len(self._kline_cache) >= KLINE_CACHE_MAX:
            self._kline_cache.pop(next(iter(self._kline_cache)))
        self._kline_cache[key] = klines
        return klines

    # ---------------- 策略管理 ----------------
    def strategies(self):
        return [dict(s) for s in self._strategies]

    def strategy_detail(self, strategy_id: str):
        for s in self._strategies:
            if s["id"] == strategy_id:
                return dict(s)
        return None

    def add_strategy(self, payload: dict):
        """新增自定义策略(校验失败抛 ValueError,由接口层返回 400)。"""
        if not isinstance(payload, dict):
            raise ValueError("请求体需为 JSON 对象")

        name = str(payload.get("name") or "").strip()
        if not 2 <= len(name) <= 24:
            raise ValueError("策略名称长度需为 2-24 个字符")
        if any(s["name"] == name for s in self._strategies):
            raise ValueError("已存在同名策略")

        category = str(payload.get("category") or "自定义").strip()
        if category not in CATEGORY_CHOICES:
            raise ValueError("策略类别需为:" + " / ".join(CATEGORY_CHOICES))

        status = str(payload.get("status") or "paused").strip()
        if status not in STATUS_CHOICES:
            raise ValueError("策略状态需为 running / paused")

        freq = str(payload.get("freq") or "日线").strip()
        if freq not in FREQ_CHOICES:
            raise ValueError("运行频率需为:" + " / ".join(FREQ_CHOICES))

        desc = str(payload.get("desc") or "").strip() or "%s(自定义策略,信号逻辑待补充)。" % name
        if len(desc) > 200:
            raise ValueError("策略描述不能超过 200 个字符")

        universe = str(payload.get("universe") or "").strip() or "自定义标的池(演示)"
        if len(universe) > 40:
            raise ValueError("标的池说明不能超过 40 个字符")

        params = payload.get("params") or {}
        params = self._validate_params(params)

        item = {
            "id": self._next_user_id(),
            "name": name,
            "category": category,
            "desc": desc,
            "params": params,
            "status": status,
            "universe": universe,
            "freq": freq,
            "builtin": False,
        }
        self._strategies.append(item)
        self._save_user_strategies()
        return dict(item)

    def delete_strategy(self, strategy_id: str):
        """删除自定义策略。返回 True=已删除 / None=不存在;内置策略抛 ValueError。"""
        for i, s in enumerate(self._strategies):
            if s["id"] == strategy_id:
                if s.get("builtin"):
                    raise ValueError("内置示例策略不可删除")
                self._strategies.pop(i)
                self._backtest_cache.pop(strategy_id, None)
                self._save_user_strategies()
                return True
        return None

    @staticmethod
    def _validate_params(params):
        if not isinstance(params, dict):
            raise ValueError("参数需为 JSON 对象,例如 {\"lookback\": 20}")
        if len(params) > 10:
            raise ValueError("参数个数不能超过 10 个")
        cleaned = {}
        for k, v in params.items():
            key = str(k).strip()
            if not key or len(key) > 24:
                raise ValueError("参数名需为 1-24 个字符")
            if isinstance(v, bool):
                cleaned[key] = v
            elif isinstance(v, (int, float)):
                # NaN / Infinity 不是合法 JSON,写盘后会毒化 /api/strategies 响应
                if isinstance(v, float) and not math.isfinite(v):
                    raise ValueError("参数 %s 不能为 NaN / Infinity" % key)
                cleaned[key] = v
            elif isinstance(v, str):
                if len(v) > 24:
                    raise ValueError("参数 %s 的文本值不能超过 24 个字符" % key)
                cleaned[key] = v
            else:
                raise ValueError("参数 %s 仅支持数字或短文本" % key)
        return cleaned

    def _next_user_id(self) -> str:
        used = 0
        for s in self._strategies:
            sid = s["id"]
            if sid.startswith("st_user_"):
                tail = sid[len("st_user_"):]
                if tail.isdigit():
                    used = max(used, int(tail))
        return "st_user_%d" % (used + 1)

    # ---- 自定义策略持久化(读写失败不影响接口可用性) ----
    def _load_user_strategies(self):
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, list):
                raise ValueError("文件内容不是 JSON 数组")
        except (OSError, ValueError) as exc:
            # 读取失败:先把损坏文件挪到 .bak 再返回,避免后续保存把它静默覆盖
            print("[warn] 读取自定义策略文件失败(%s):%s;已备份为 %s.bak"
                  % (self._store_path, exc, self._store_path), file=sys.stderr)
            try:
                os.replace(self._store_path, self._store_path + ".bak")
            except OSError:
                pass
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "")
            if not sid or any(s["id"] == sid for s in self._strategies):
                continue
            self._strategies.append({
                "id": sid,
                "name": str(item.get("name") or sid),
                "category": str(item.get("category") or "自定义"),
                "desc": str(item.get("desc") or ""),
                "params": item.get("params") if isinstance(item.get("params"), dict) else {},
                "status": item.get("status") if item.get("status") in STATUS_CHOICES else "paused",
                "universe": str(item.get("universe") or "自定义标的池(演示)"),
                "freq": str(item.get("freq") or "日线"),
                "builtin": False,
            })

    def _save_user_strategies(self) -> bool:
        if not self._store_path:
            return False
        payload = [s for s in self._strategies if not s.get("builtin")]
        tmp = self._store_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                # allow_nan=False:保证写出的始终是合法 JSON(前端可直接 JSON.parse)
                json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(tmp, self._store_path)
            return True
        except (OSError, ValueError) as exc:
            print("[warn] 保存自定义策略失败(%s):%s" % (self._store_path, exc), file=sys.stderr)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    # ---------------- 回测结果 ----------------
    def _equity(self, target_total_return, sigma, salt):
        """生成净值曲线:总收益精确等于预设目标,形状与回撤由随机序列决定。

        流程:生成中性日收益序列(gauss) -> 等比缩放因子使复利恰好达到目标
        (1 + target_total_return),从而月度收益复利乘积与末净值严格自洽。

        返回 (dates, navs, drawdowns, daily_ret, max_dd):
        - dates[0] 为回测基准日(起点前一日),navs[0] = 1.0,drawdowns[0] = 0;
        - daily_ret[0] = 0.0,后接各交易日真实日收益;
        - 三序列长度一致。
        """
        rng = self._rng(salt)
        days = trading_days(BACKTEST_START, BACKTEST_END)
        n = len(days)
        raw = [rng.gauss(0.0004, sigma) for _ in range(n)]
        f = 1.0
        for r in raw:
            f *= (1 + r)
        target = 1.0 + target_total_return
        scale = (target / f) ** (1.0 / n)
        rets_adj = [(1.0 + r) * scale - 1.0 for r in raw]

        nav, peak, max_dd = 1.0, 1.0, 0.0
        navs, dds, rets = [1.0], [0.0], [0.0]
        for r in rets_adj:
            nav *= (1 + r)
            peak = max(peak, nav)
            dd = (1 - nav / peak) * 100
            max_dd = max(max_dd, dd)
            navs.append(round(nav, 6))
            dds.append(round(dd, 4))
            rets.append(r)
        base_day = (days[0] - timedelta(days=1)).isoformat()
        return [base_day] + [d.isoformat() for d in days], navs, dds, rets, max_dd

    def _benchmark(self):
        """所有策略共用的基准净值曲线(仅生成一次)。"""
        if self._benchmark_cache is None:
            self._benchmark_cache = self._equity(
                BENCHMARK_TARGET, BENCHMARK_SIGMA, BENCHMARK_SALT
            )[1]
        return self._benchmark_cache

    def _profile_for(self, strategy_id: str, name: str = ""):
        """自定义策略的目标收益 / 波动率:按策略 id + 名称稳定派生。

        名称也参与派生,这样删除后重建的同号策略不会复用上一条策略的曲线。
        """
        rng = self._rng(stable_seed("profile", strategy_id, name))
        return round(rng.uniform(0.10, 0.52), 2), round(rng.uniform(0.0055, 0.0105), 4)

    @staticmethod
    def _metrics(daily_ret, navs):
        n = len(daily_ret)
        mean = sum(daily_ret) / n if n else 0.0
        if n > 1:
            var = sum((r - mean) ** 2 for r in daily_ret) / (n - 1)
            std = math.sqrt(var)
        else:
            std = 0.0
        total_ret = navs[-1] / navs[0] - 1
        annual = (1 + mean) ** 252 - 1
        sharpe = mean / std * math.sqrt(252) if std > 0 else 0.0
        return {
            "total_return_pct": round(total_ret * 100, 2),
            "annual_return_pct": round(annual * 100, 2),
            "max_drawdown_pct": 0.0,  # 由调用方回填
            "sharpe": round(sharpe, 2),
            "win_rate_pct": 0.0,
            "trade_count": 0,
            "profit_loss_ratio": 0.0,
        }

    def backtest(self, strategy_id: str):
        if strategy_id in self._backtest_cache:
            return self._backtest_cache[strategy_id]
        detail = self.strategy_detail(strategy_id)
        if detail is None:
            return None
        idx = next(i for i, s in enumerate(self._strategies) if s["id"] == strategy_id)
        # 内置策略用预设风格(演示不同风格,总收益精确等于目标);
        # 自定义策略按 id 稳定派生,无需手工维护 profile 列表
        if idx < len(BUILTIN_PROFILES):
            profile = BUILTIN_PROFILES[idx]
            salt = 200 + idx
        else:
            profile = self._profile_for(strategy_id, detail["name"])
            salt = 1000 + stable_seed("backtest", strategy_id)

        base_nav = self._benchmark()
        strat_days, strat_nav, dds, strat_ret, max_dd = self._equity(profile[0], profile[1], salt)
        # daily_ret[0] 为基准日占位 0,统计与月度聚合均跳过首点
        strat_ret_real = strat_ret[1:]

        # 月度收益(按 YYYY-MM 复利聚合,跳过基准日占位点)
        monthly = {}
        for d, r in zip(strat_days[1:], strat_ret_real):
            m = d[:7]
            monthly[m] = monthly.get(m, 1.0) * (1 + r)
        month_ret = [{"month": m, "ret_pct": round((v - 1) * 100, 2)} for m, v in sorted(monthly.items())]

        # 绩效指标
        metrics = self._metrics(strat_ret_real, strat_nav)
        metrics["max_drawdown_pct"] = round(max_dd, 2)

        # 模拟交易记录:间隔 8-24 个交易日开仓,持有 5-20 个交易日平仓
        # 种子:内置策略用下标(保持预设风格),自定义策略按 id 派生(不随列表顺序/增删变化)
        trade_salt = (300 + idx) if idx < len(BUILTIN_PROFILES) else (300000 + stable_seed("trades", strategy_id, detail["name"]))
        rng = self._rng(trade_salt)
        trades, i = [], 1
        ref_prices = [round(p, 2) for p in self._gen_trade_prices(rng, len(strat_days))]
        while i < len(strat_days) - 40:
            hold = rng.randint(5, 20)
            exit_i = min(i + hold, len(strat_days) - 1)
            buy_p, sell_p = ref_prices[i], ref_prices[exit_i]
            qty = max(100, int(20000 / buy_p // 100 * 100))
            pnl = (sell_p - buy_p) * qty
            ret_pct = (sell_p / buy_p - 1) * 100 if buy_p else 0
            trades.append({
                "date": strat_days[i], "side": "buy", "price": buy_p, "qty": qty,
                "pnl": None, "ret_pct": None,
            })
            trades.append({
                "date": strat_days[exit_i], "side": "sell", "price": sell_p, "qty": qty,
                "pnl": round(pnl, 2), "ret_pct": round(ret_pct, 2),
            })
            i = exit_i + rng.randint(8, 24)
        wins = [t for t in trades if t["side"] == "sell" and (t["pnl"] or 0) > 0]
        sells = [t for t in trades if t["side"] == "sell"]
        loss = [t for t in sells if (t["pnl"] or 0) <= 0]
        win_avg = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        loss_avg = sum(t["pnl"] for t in loss) / len(loss) if loss else 0
        metrics["win_rate_pct"] = round(len(wins) / len(sells) * 100, 2) if sells else 0.0
        metrics["trade_count"] = len(sells)
        metrics["profit_loss_ratio"] = round(abs(win_avg / loss_avg), 2) if loss_avg else 0.0

        result = {
            "strategy": detail,
            "range": {"start": BACKTEST_START.isoformat(), "end": BACKTEST_END.isoformat()},
            "nav": [
                {"date": d, "strategy": s, "benchmark": b}
                for d, s, b in zip(strat_days, strat_nav, base_nav)
            ],
            "drawdown": [{"date": d, "dd_pct": v} for d, v in zip(strat_days, dds)],
            "monthly": month_ret,
            "metrics": metrics,
            "trades": trades[-30:],
            "trade_total": metrics["trade_count"],
        }
        self._backtest_cache[strategy_id] = result
        return result

    @staticmethod
    def _gen_trade_prices(rng, n):
        """生成一组平滑的参考价格,供模拟交易使用。"""
        prices, p = [], 20.0
        for _ in range(n):
            p = max(4.0, p * (1 + rng.gauss(0.00035, 0.018)))
            prices.append(p)
        return prices

    # ---------------- 持仓管理 ----------------
    def holdings(self):
        quotes = {q["code"]: q for q in self._quotes()}
        rows = []
        for h in HOLDINGS:
            q = quotes[h["code"]]
            # 成本价由「现价 / (1 + 预设浮动收益率)」反推,保证演示账户盈亏处于合理区间
            cost = round(q["price"] / (1 + h["ret"]), 2)
            market_val = round(h["qty"] * q["price"], 2)
            cost_val = round(h["qty"] * cost, 2)
            day_pnl = round((q["price"] - q["prev_close"]) * h["qty"], 2)
            total_pnl = round(market_val - cost_val, 2)
            rows.append({
                "code": h["code"], "name": q["name"], "industry": q["industry"],
                "qty": h["qty"], "cost": cost, "price": q["price"],
                "market_value": market_val, "day_pnl": day_pnl,
                "total_pnl": total_pnl,
                "return_pct": round((q["price"] / cost - 1) * 100, 2),
            })
        return rows

    def portfolio_overview(self):
        rows = self.holdings()
        total_mv = round(sum(r["market_value"] for r in rows), 2)
        total_assets = round(total_mv + CASH, 2)
        total_day_pnl = round(sum(r["day_pnl"] for r in rows), 2)
        total_pnl = round(sum(r["total_pnl"] for r in rows), 2)
        cost_total = round(sum(r["qty"] * r["cost"] for r in rows), 2)
        return {
            "total_assets": total_assets,
            "market_value": total_mv,
            "cash": CASH,
            "day_pnl": total_day_pnl,
            "total_pnl": total_pnl,
            "total_return_pct": round(total_pnl / cost_total * 100, 2) if cost_total else 0.0,
            "positions_count": len(rows),
            "allocation": [
                {"name": "现金", "value": CASH},
            ] + [{"name": r["name"], "value": r["market_value"]} for r in rows],
        }

    def portfolio_equity_curve(self):
        """账户权益曲线(演示):近 3 个月逐日模拟,末点 = 当前总资产。"""
        rng = self._rng(400)
        days = trading_days(date(2026, 6, 1), LAST_TRADE_DAY)
        total_assets = self.portfolio_overview()["total_assets"]
        navs, nav = [], 1.0
        for _ in days:
            nav *= (1 + rng.gauss(0.0008, 0.011))
            navs.append(nav)
        # 归一化:曲线末点与账户总资产卡片保持自洽
        last = navs[-1] or 1.0
        rows = [
            {"date": d.isoformat(), "equity": round(total_assets * v / last, 2)}
            for d, v in zip(days, navs)
        ]
        if rows:
            rows[-1]["equity"] = round(total_assets, 2)
        return rows
