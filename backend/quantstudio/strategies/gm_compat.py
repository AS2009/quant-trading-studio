# -*- coding: utf-8 -*-
"""掘金量化（gm.api）策略 → 本项目策略 的转换与诊断。

为什么需要它
------------
外部平台（掘金 `gm.api`、聚宽、米筐…）的策略用的是**平台自己的运行时**：全局函数 +
`context` 对象 + pandas，而本项目是 `BaseStrategy` 子类 + `ctx` 上下文 + 零依赖。

两者不是「改个 import」的关系，缺的是**数据能力**：
掘金能取指数成分股、全市场市值、财务报表、分钟线、股指期货，本项目的数据层只有
快照 / 日周月 K 线 / 指数 / 板块 / 广度。所以转换必须**如实分级**，不能把取不到数据的
策略假装转成功——那种「转换」产出的回测结果是假的，比不转换更危险。

三级判定
--------
- ``fidelity``   可**忠实**翻译：用到的 gm.api 全有等价物，且语义已核对；
- ``approximate`` 可翻译但**语义有损**：例如分钟线→日线、目标仓位→按当时现金下单；
- ``blocked``    不可翻译：依赖本项目数据层没有的能力（成分股 / 市值 / 基本面 / 期货…）。

产出的策略一律经过 `scripts/check_strategies.py` 校验；本模块只负责「翻译 + 说清代价」。
"""

import ast
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# =========================================================================== 能力目录

FIDELITY = "fidelity"
APPROXIMATE = "approximate"
BLOCKED = "blocked"

#: gm.api 模块级函数/常量 → (级别, 本项目对应物说明)
GM_MODULE_API: Dict[str, Tuple[str, str]] = {
    # ---- 运行时骨架
    "init": (FIDELITY, "`init(context)` → `on_start(ctx)` + `__init__` 里的参数"),
    "on_bar": (FIDELITY, "`on_bar(context, bars)` → `on_bar(ctx, bars)`（签名几乎一致）"),
    "on_order_status": (APPROXIMATE, "本项目无委托回报回调；成交明细由回测结果统一给出"),
    "on_backtest_finished": (APPROXIMATE, "本项目无该回调；绩效由回测引擎统一计算"),
    "run": (APPROXIMATE, "掘金的回测入口；本项目用 `scripts/run_backtest.py` 或网页回测"),
    "subscribe": (APPROXIMATE, "订阅 → 本项目由回测请求的 symbols 决定标的池与频率"),
    "schedule": (APPROXIMATE, "定时任务 → 落到 `on_bar` 里按日期/月份判断触发"),
    # ---- 取数
    "history_n": (FIDELITY, "`ctx.history(code, field, n)`（升序，含当日，无未来函数）"),
    "history": (FIDELITY, "`ctx.history` / `ctx.bars_since`"),
    "current": (APPROXIMATE, "实时用 `provider.latest_quotes`；回测里用当日 bar 收盘价"),
    "get_previous_n_trading_dates": (FIDELITY, "`ctx` 无法直接取，但可用回测日历 / `ctx.today`"),
    "get_symbols": (BLOCKED, "本项目数据层没有「全市场证券列表 + 停牌/ST 过滤」能力"),
    "stk_get_index_constituents": (BLOCKED, "本项目数据层**没有指数成分股**能力"),
    "stk_get_daily_mktvalue_pt": (BLOCKED, "本项目数据层**没有市值**时间序列（快照只有单日 f20）"),
    "stk_get_fundamentals_balance_pt": (BLOCKED, "本项目数据层**没有财务报表**能力"),
    "get_instruments": (BLOCKED, "没有合约/证券主数据接口"),
    "get_history_instruments": (BLOCKED, "同上"),
    "get_trading_dates": (APPROXIMATE, "本项目用 `core/calendar.TradingCalendar`（仅排周末，不含节假日）"),
    # ---- 账户与持仓
    "get_position": (FIDELITY, "`ctx.positions()` / `ctx.position(code)`"),
    "get_cash": (FIDELITY, "`ctx.cash()` / `ctx.total_assets()`"),
    # ---- 下单
    "order_volume": (FIDELITY, "`self.buy_order` / `self.sell_order`（数量语义）"),
    "order_target_volume": (APPROXIMATE, "「调到目标股数」需自己算差额；本项目下单是增量语义"),
    "order_target_percent": (APPROXIMATE, "「调到目标仓位」→ 按当时可用现金/持仓折算增量下单"),
    "order_close_all": (APPROXIMATE, "→ 逐个持仓 `self.sell_order(ctx, code)`"),
    "order_percent": (APPROXIMATE, "→ `self.buy_order(ctx, code, price, pct)`"),
    "algo_order": (BLOCKED, "算法交易（ATS-SMART 拆单）是券商侧能力，本项目不模拟拆单"),
    # ---- 常量（直接映射）
    "OrderSide_Buy": (FIDELITY, "→ `OrderRequest(side=\"buy\")`"),
    "OrderSide_Sell": (FIDELITY, "→ `side=\"sell\"`"),
    "OrderType_Market": (FIDELITY, "→ 市价单（本项目按当日收盘价撮合）"),
    "OrderType_Limit": (APPROXIMATE, "本项目限价单只能挂单、不做后台撮合；转换时按市价处理"),
    "PositionSide_Long": (FIDELITY, "本项目只做多"),
    "PositionEffect_Open": (FIDELITY, "买入开仓"),
    "PositionEffect_Close": (FIDELITY, "卖出平仓"),
    "ADJUST_PREV": (FIDELITY, "本项目回测固定乘法前复权（qfq）"),
    "ADJUST_POST": (APPROXIMATE, "本项目回测固定 qfq；后复权需改数据层"),
    "ADJUST_NONE": (APPROXIMATE, "本项目回测固定 qfq"),
    "MODE_BACKTEST": (APPROXIMATE, "回测模式概念 → 本项目「回测 vs 模拟盘」"),
    "MODE_LIVE": (APPROXIMATE, "本项目默认只做模拟盘"),
    # ---- 期货
    "get_continuous_contracts": (BLOCKED, "本项目不支持股指期货/主力合约"),
}

#: `context.<attr>` 属性 → (级别, 说明)
GM_CONTEXT_ATTRS: Dict[str, Tuple[str, str]] = {
    "now": (FIDELITY, "→ `ctx.today`（回测里是 `YYYY-MM-DD`；掘金是 datetime）"),
    "data": (FIDELITY, "`context.data(...)` → `ctx.history(code, field, n)` 或 `ctx.bars_since`"),
    "mode": (APPROXIMATE, "→ 本项目没有 `mode` 概念"),
    "backtest_start_time": (APPROXIMATE, "→ 回测请求的 `start`"),
    "backtest_end_time": (APPROXIMATE, "→ 回测请求的 `end`"),
    "symbol": (FIDELITY, "→ `self.symbols[0]` 或策略参数"),
    "symbols": (FIDELITY, "→ `self.symbols`"),
}

#: 第三方依赖 → 处理方式
THIRD_PARTY: Dict[str, Tuple[str, str]] = {
    "pandas": (APPROXIMATE, "逐行改写为纯 Python（列表推导 / 内置统计）"),
    "numpy": (APPROXIMATE, "逐行改写为纯 Python（`math` / 生成式）"),
    "sklearn": (BLOCKED, "本项目零第三方依赖；需引入 scikit-learn 或手写等价模型"),
    "talib": (BLOCKED, "本项目不依赖 TA-Lib；指标请改用 `BaseStrategy` 内置实现"),
    "scipy": (BLOCKED, "同上"),
    "statsmodels": (BLOCKED, "同上"),
}

#: 本项目 **内置指标** 与掘金/pandas 常见写法的对照（供人工核对语义）
INDICATOR_NOTES: Dict[str, str] = {
    "boll": (
        "**语义差异**：掘金示例常用 `close.rolling(n).std()`（pandas 默认 **样本**标准差 ddof=1），"
        "本项目 `BaseStrategy.boll()` 用**总体**标准差（ddof=0，与通达信一致）。"
        "同参数下轨道宽度不同：样本口径更宽（n=26 时约 2% 差距）。"
        "要逐值对齐必须手写样本标准差，而不是直接用 `boll()`。"
    ),
    "macd": (
        "口径一致：掘金示例的 `DIF=EMA(S,12)-EMA(S,26)`、`DEA=EMA(DIF,9)`、`MACD=(DIF-DEA)*2` "
        "与本项目 `BaseStrategy.macd()` 完全相同（EMA 均为 `alpha=2/(span+1)`、`adjust=False`）。"
    ),
    "atr": "本项目 `atr()` 用 Wilder 平滑，与国内软件一致。",
}

ORDER_SIDE = {"buy": "OrderSide_Buy", "sell": "OrderSide_Sell"}


def _entry(level: str, note: str) -> Dict[str, str]:
    return {"level": level, "note": note}


def _context_lookup(name: str) -> Tuple[str, str]:
    """`context.now` / `context.data` → (级别, 说明)。

    未登记成员（`context.n` / `context.holding_num` …）是**策略自己的变量**，不是平台能力，
    因此算 fidelity —— 把它降级成 `approximate` 会让本来能忠实转换的策略被误判。
    它们会被提成策略参数（见 `_guess_params`）。
    """
    if name.startswith("context."):
        attr = name.split(".", 1)[1]
        for key in (attr, "context.%s" % attr):
            if key in GM_CONTEXT_ATTRS:
                return GM_CONTEXT_ATTRS[key]
        return (FIDELITY, "策略自己的 `context` 变量 → 转换后成为策略参数 / 实例属性")
    level, note = GM_MODULE_API.get(name, (BLOCKED, "未登记的 gm.api 函数（无法确认语义）"))
    return level, note


# =========================================================================== 源码分析


class GmApiUsage:
    """一次源码扫描的结果。"""

    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.tree: Optional[ast.AST] = None
        self.syntax_error = ""
        self.calls: Dict[str, List[int]] = {}        # 函数名 → 行号
        self.constants: Dict[str, int] = {}
        self.context_attrs: Dict[str, List[int]] = {}
        self.imports: List[str] = []
        self.functions: List[str] = []
        self.has_schedule = False
        self.has_subscribe = False
        self.uses_pandas = False
        self.uses_numpy = False
        self.init_assignments: Dict[str, str] = {}   # context.xxx = <字面量> → 参数初值

    @property
    def title(self) -> str:
        base = os.path.splitext(os.path.basename(self.path))[0]
        return re.sub(r"\(\d+\)$", "", base).strip()

    def findings(self) -> List[Dict[str, Any]]:
        """按级别汇总「用到但本项目没有/不等价」的项。"""
        out: List[Dict[str, Any]] = []
        for name, lines in sorted(self.calls.items()):
            level, note = _context_lookup(name)
            out.append({"kind": "context" if name.startswith("context.") else "function",
                        "name": name, "lines": lines, "level": level, "note": note})
        for name, lines in sorted(self.context_attrs.items()):
            level, note = _context_lookup("context.%s" % name)
            if level == FIDELITY:
                continue
            out.append({"kind": "context", "name": "context.%s" % name, "lines": lines,
                        "level": level, "note": note})
        for module in sorted(set(self.imports)):
            if module in THIRD_PARTY:
                level, note = THIRD_PARTY[module]
                out.append({"kind": "import", "name": module, "lines": [],
                            "level": level, "note": note})
        return out

    def level(self) -> str:
        """整体判定：有 BLOCKED 就是 blocked，否则有 APPROXIMATE 就是 approximate。

        语法错误直接算 blocked —— 连 AST 都没有，谈不上「可忠实翻译」。
        """
        if self.syntax_error:
            return BLOCKED
        levels = {item["level"] for item in self.findings()}
        if BLOCKED in levels:
            return BLOCKED
        if APPROXIMATE in levels:
            return APPROXIMATE
        return FIDELITY


def _module_name(node: ast.AST) -> str:
    """`a.b.c` → `"a.b.c"`；非点号表达式返回空串。"""
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def analyze(path: str, source: Optional[str] = None) -> GmApiUsage:
    """解析一份 gm.api 策略源码，收集它用到的平台能力。"""
    if source is None:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            source = handle.read()
    usage = GmApiUsage(path, source)
    try:
        usage.tree = ast.parse(source)
    except SyntaxError as exc:
        usage.syntax_error = "第 %s 行语法错误：%s" % (exc.lineno, exc.msg)
        return usage

    for node in ast.walk(usage.tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    usage.imports.append(node.module.split(".")[0])
            else:
                for alias in node.names:
                    usage.imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.FunctionDef):
            usage.functions.append(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                # 只认 gm.api 的**顶层函数**（`history_n(...)`）；`print` / `len` / `list`
                # 这些内置函数不是平台能力，报成阻断项会把能转的策略误判成不能转。
                if func.id in GM_MODULE_API:
                    name = func.id
            elif isinstance(func, ast.Attribute):
                # `context.method(...)`：只认直接挂在 `context` 上的一层
                if isinstance(func.value, ast.Name) and func.value.id == "context":
                    name = "context.%s" % func.attr
                elif _module_name(func.value) == "context":
                    name = "context.%s" % func.attr
            if name:
                usage.calls.setdefault(name, []).append(node.lineno)
                if name == "schedule":
                    usage.has_schedule = True
                if name == "subscribe":
                    usage.has_subscribe = True
        elif isinstance(node, ast.Name):
            if node.id in GM_MODULE_API and node.id not in usage.calls:
                usage.constants.setdefault(node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            # 只统计**直接**的 `context.x` 读取；`context.now.strftime` 里的 `strftime`
            # 或 `foo.bar` 之类与平台能力无关，不能混进来。
            if isinstance(node.value, ast.Name) and node.value.id == "context":
                usage.context_attrs.setdefault(node.attr, []).append(getattr(node, "lineno", 0))
    return usage


# =========================================================================== 报告


def render_report(usage: GmApiUsage, target: str = "") -> str:
    """生成可读的诊断报告（Markdown）。"""
    findings = usage.findings()
    level = usage.level()
    badge = {FIDELITY: "✅ 可忠实翻译",
             APPROXIMATE: "⚠️ 可翻译但语义有损",
             BLOCKED: "⛔ 依赖本项目缺失的数据能力"}[level]
    blocked = [f for f in findings if f["level"] == BLOCKED]
    approx = [f for f in findings if f["level"] == APPROXIMATE]

    lines = [
        "# 掘金策略转换诊断：%s" % usage.title,
        "",
        "- 源文件：`%s`" % usage.path,
        "- 整体判定：**%s**" % badge,
        "- 触发方式：%s" % ("`schedule`（定时任务）" if usage.has_schedule else
                            ("`subscribe` + `on_bar`" if usage.has_subscribe else "未识别")),
        "- 第三方依赖：%s" % (", ".join(sorted(set(usage.imports))) or "无"),
    ]
    if target:
        lines.append("- 生成产物：`%s`" % target)
    if usage.syntax_error:
        lines += ["", "> ❌ %s" % usage.syntax_error]
        return "\n".join(lines) + "\n"

    if blocked:
        lines += ["", "## ⛔ 阻断项（本项目数据层没有这些能力）", ""]
        for item in blocked:
            lines.append("- **`%s`**（%s）—— %s"
                         % (item["name"], item["kind"],
                            item["note"] + ("；行 %s" % _fmt_lines(item["lines"])
                                            if item["lines"] else "")))
    if approx:
        lines += ["", "## ⚠️ 语义有损项（转换会继续，但结果不等于原策略）", ""]
        for item in approx:
            lines.append("- **`%s`**（%s）—— %s%s"
                         % (item["name"], item["kind"], item["note"],
                            "；行 %s" % _fmt_lines(item["lines"]) if item["lines"] else ""))

    lines += ["", "## 平台能力对照", ""]
    lines.append("| gm.api | 本项目 |")
    lines.append("|---|---|")
    for name in sorted(set(usage.calls) | set(usage.constants)):
        _level, note = GM_MODULE_API.get(name, (BLOCKED, "未登记"))
        lines.append("| `%s` | %s |" % (name, note))

    notes = [INDICATOR_NOTES[key] for key in ("boll", "macd")
             if key in usage.source.lower()]
    if notes:
        lines += ["", "## 指标口径核对", ""]
        for note in notes:
            lines.append("- %s" % note)

    lines += ["", "## 结论", ""]
    if level == FIDELITY:
        lines.append("可以直接转换，转换后建议跑 `scripts/check_strategies.py` 校验并做一次回测对照。")
    elif level == APPROXIMATE:
        lines.append("可以转换，但**上面每一项语义有损都会改变回测结果**；"
                     "转换产物里的 `TODO(转换)` 注释标出了需要你确认的位置。")
    else:
        lines.append("**不能忠实转换**：上面每个 ⛔ 项都需要先在数据层补能力"
                     "（或换一个不依赖它的标的池/因子）。转换器仍会产出骨架，"
                     "但取数部分会写成显式的 `NotImplementedError`，避免产出「看起来能跑、结果是假的」策略。")
    return "\n".join(lines) + "\n"


def _fmt_lines(lines: Sequence[int]) -> str:
    uniq = sorted({int(x) for x in lines if x})
    if not uniq:
        return ""
    if len(uniq) <= 6:
        return ", ".join(str(x) for x in uniq)
    return "%d, %d, … 共 %d 处" % (uniq[0], uniq[1], len(uniq))


# =========================================================================== 生成器

_HEADER = '''# -*- coding: utf-8 -*-
"""{name}

由 `scripts/import_strategy.py` 从掘金（gm.api）策略**转换**而来。

- 源文件：`{source}`
- 转换判定：**{badge}**
- 生成时间：{stamp}

转换说明（务必先读）
--------------------
{reasons}

参数
----
{params_doc}
"""
'''


#: 常见策略名汉字的拼音转写（只为一件事：让**文件名可读**）。
#: 覆盖不到的字回退到 sha1 短哈希（稳定、幂等）；表里没有的字不会导致失败。
PINYIN: Dict[str, str] = {
    "风": "feng", "格": "ge", "轮": "lun", "动": "dong", "策": "ce", "略": "lue",
    "多": "duo", "因": "yin", "子": "zi", "选": "xuan", "股": "gu", "票": "piao",
    "小": "xiao", "市": "shi", "值": "zhi", "机": "ji", "器": "qi", "学": "xue",
    "习": "xi", "布": "bu", "林": "lin", "线": "xian", "均": "jun", "回": "hui",
    "归": "gui", "日": "ri", "内": "nei", "转": "zhuan", "交": "jiao", "易": "yi",
    "指": "zhi", "数": "shu", "增": "zeng", "强": "qiang", "对": "dui", "冲": "chong",
    "期": "qi", "货": "huo", "算": "suan", "法": "fa", "价": "jia", "量": "liang",
    "趋": "qu", "势": "shi", "跟": "gen", "踪": "zong", "波": "bo", "率": "lv",
    "稳": "wen", "健": "jian", "网": "wang", "海": "hai", "龟": "gui", "突": "tu",
    "破": "po", "通": "tong", "道": "dao", "止": "zhi", "损": "sun", "动": "dong",
    "量": "liang", "轮": "lun", "动": "dong", "低": "di", "波": "bo", "优": "you",
}


def _slug(text: str, brief: int = 0) -> str:
    """中文名 → **稳定**的 ASCII slug（拼音转写优先）。

    文件 slug ↔ 策略 id 必须一致且可复现（见 docs/strategy-spec.md）：同一份源文件反复转换
    必须得到同一个文件名，否则每次转换都新增文件、策略列表里堆一堆近似重复项。
    因此纯中文名走拼音转写，转写不了才退到 sha1（**不能**用 `hash()`，它每个进程都带随机种子）。
    """
    import hashlib

    ascii_part = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    if ascii_part:
        slug = ascii_part
    else:
        parts = [PINYIN.get(char, "") for char in text if "\u4e00" <= char <= "\u9fff"]
        slug = "_".join(part for part in parts if part)
        if not slug:
            slug = "gm_%s" % hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"_+", "_", slug).strip("_")
    if brief and len(slug) > brief:
        cut = slug[:brief]
        slug = cut.rsplit("_", 1)[0] if "_" in cut else cut
    return slug or "gm_strategy"


def build_skeleton(usage: GmApiUsage, slug: str, name: str,
                   reasons: Sequence[str], params: Dict[str, Any]) -> str:
    """生成**诚实**的策略骨架：能翻的部分给出可用实现，不能翻的部分显式报错。"""
    import datetime as _dt

    level = usage.level()
    badge = {FIDELITY: "✅ 可忠实翻译",
             APPROXIMATE: "⚠️ 语义有损（结果不等于原策略）",
             BLOCKED: "⛔ 数据能力缺失，取数处会显式报错"}[level]
    params_doc = "\n".join("- `%s`：默认 `%r`" % (k, v) for k, v in params.items()) or "- （无）"
    header = _HEADER.format(
        name=name, source=os.path.basename(usage.path), badge=badge,
        stamp=_dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        reasons="\n".join("- " + item for item in reasons) or "- （无需特殊说明）",
        params_doc=params_doc,
    )

    schema_lines = []
    for key, value in params.items():
        ptype = "int" if isinstance(value, int) and not isinstance(value, bool) else (
            "float" if isinstance(value, float) else "str")
        schema_lines.append(
            '            "%s": {"label": "%s", "type": "%s", "default": %r},'
            % (key, key, ptype, value))
    schema_body = "\n".join(schema_lines) or "            # 无参数"

    blocked = [f for f in usage.findings() if f["level"] == BLOCKED]
    blocked_block = ""
    if blocked:
        lines = "\n".join("            %r," % ("%s：%s" % (f["name"], f["note"]))
                          for f in blocked)
        blocked_block = (
            "\n    def _unsupported(self) -> None:\n"
            '        """源策略依赖本项目数据层没有的能力 —— 显式报错，而不是静默算错。"""\n'
            "        detail = \"\\n\".join([\n"
            + lines + "\n"
            "        ])\n"
            "        raise NotImplementedError(\n"
            '            "该策略依赖以下本项目暂不支持的能力，无法忠实回测：\\n%s" % detail)\n'
        )

    # blocked 的产物**必须吵**：如果 on_bar 只 `return []`，把它挪进 local/ 后回测会
    # 「成功」并给出 0 笔交易——比报错更危险（用户会以为策略没信号，而不是没数据）。
    if level == BLOCKED:
        on_bar_body = (
            "        # 源策略依赖的数据能力本项目没有；补不上就直接报错，别让它静默产出 0 笔交易。\n"
            "        # 补上能力（或换掉依赖它的标的池/因子）后，再把源策略的 algo() 主体搬进来。\n"
            "        self._unsupported()\n"
            "        return []"
        )
    else:
        on_bar_body = (
            '        """TODO(转换)：把源策略的 `algo(context)` / `on_bar(context, bars)` 主体搬到这里。\n'
            "\n"
            "        可用的等价物：\n"
            '        - `ctx.history(code, "close", n)`：截至当日的最近 n 个收盘（升序，无未来函数）\n'
            "        - `ctx.bars_since(code, n)`：最近 n 根完整 bar（算 ATR/KDJ 用）\n"
            "        - `ctx.position(code)` / `ctx.positions()` / `ctx.cash()` / `ctx.total_assets()`\n"
            "        - `self.buy_order(ctx, code, price, pct, reason=...)` / `self.sell_order(ctx, code, reason=...)`\n"
            "        - 指标：`self.ma/ema/std/boll/macd/kdj/atr/rsi/highest/lowest/cross/...`\n"
            '        """\n'
            '        self.log("策略尚未完成转换：请实现 on_bar（见上方 TODO）")\n'
            "        return []"
        )

    return header + '''
from typing import Dict, List

from ...core.models import Bar, OrderRequest
from ..base import BaseStrategy


class {cls}(BaseStrategy):
    id = "{sid}"
    name = "{name}"
    category = "自定义"
    desc = "由掘金策略转换而来（{badge_short}）"
    universe = "由回测请求决定"
    universe_type = "multi"
    freq = "日线"
    min_bars = 61
    version = "1.0"
    default_params = {params_repr}
    default_symbols: List[str] = []

    @classmethod
    def param_schema(cls) -> Dict[str, Dict[str, object]]:
        return {{
{schema_body}
        }}
{blocked_block}
    def on_start(self, ctx) -> None:
        super().on_start(ctx)

    def on_bar(self, ctx, bars: Dict[str, Bar]) -> List[OrderRequest]:
{on_bar_body}
'''.format(
        cls="".join(part.capitalize() for part in slug.split("_")) + "Strategy",
        sid="st_%s" % slug,
        name=name,
        badge_short=badge,
        params_repr=repr(dict(params)),
        schema_body=schema_body,
        blocked_block=blocked_block,
        on_bar_body=on_bar_body,
    )


def convert(path: str, out_dir: str, slug: str = "", name: str = "") -> Tuple[str, str, str]:
    """转换一份策略。

    返回 ``(生成的 .py 路径, 诊断报告 Markdown, 整体判定)``。
    """
    usage = analyze(path)
    title = name or usage.title
    # slug 兼作文件名与策略 id（`st_<slug>`），太长会难以输入，截到 28 字符
    final_slug = slug or _slug(title, brief=28)
    filename = "%s.py" % final_slug
    reasons: List[str] = []
    level = usage.level()
    if level == BLOCKED:
        reasons.append("源策略依赖本项目缺失的数据能力，取数处已写成显式 `NotImplementedError`。")
    elif level == APPROXIMATE:
        reasons.append("存在语义有损的转换，**回测结果不等于原平台**。")
    for item in usage.findings():
        if item["level"] != FIDELITY:
            reasons.append("`%s`：%s" % (item["name"], item["note"]))

    params = _guess_params(usage)
    code = build_skeleton(usage, final_slug, title, reasons, params)
    target = os.path.join(out_dir, filename)
    os.makedirs(out_dir, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(code)
    return target, render_report(usage, target=target), level


def _guess_params(usage: GmApiUsage) -> Dict[str, Any]:
    """从 `init()` 里的 `context.xxx = <字面量>` 猜出策略参数默认值。

    只排除与「回测元信息」相关的成员（`context.now` / `mode` / `backtest_*`）——它们不是
    策略参数；而 `context.symbol` / `context.n` / `context.holding_num` 这些恰恰是原策略的
    可调参数，必须提出来（`symbol` 在 `GM_CONTEXT_ATTRS` 里登记为「等价物」，
    但在这里它是**策略自己的配置**，不该被过滤掉）。
    """
    params: Dict[str, Any] = {}
    if not usage.tree:
        return params
    skip = {"now", "mode", "backtest_start_time", "backtest_end_time"}
    for node in ast.walk(usage.tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Attribute):
            continue
        target = node.targets[0]
        if not (isinstance(target.value, ast.Name) and target.value.id == "context"):
            continue
        key = target.attr
        if key in skip:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, (int, float, str, bool)):
            params[key] = value
    return params
