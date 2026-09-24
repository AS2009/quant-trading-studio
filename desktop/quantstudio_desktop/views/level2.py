# -*- coding: utf-8 -*-
"""盘口 / L2 页面：五档快照 · 逐笔成交 · 资金流分档（公开源近似口径）。

数据边界（页面上也必须如实标注，见 ``DISCLAIMER``）：

* 五档 / 外盘 / 内盘：公开快照接口（腾讯 / 新浪），是**快照**不是推送；
* 逐笔方向：第三方「盘口方向标记」（``buy`` / ``sell`` / ``neutral``），
  **不是**交易所 Level-2 的主动买卖判定；
* 资金流：由逐笔按单笔成交额分档自算（≥100 万 超大单 / 20–100 万 大单 / 5–20 万 中单 / <5 万 小单）；
* 十档 / 逐笔委托 / 委托队列：没有公开来源，需付费授权（页面只展示能力位，不逆向任何客户端）。

约定（与 ``views/__init__.BaseView`` 一致）：

* ``build()`` 只建控件（首次显示时调用一次），``reload()`` 只发请求并更新已有控件，
  刷新后滚动位置与表格选中行都保留；
* 三个基础服务调用（``level2.orderbook`` / ``level2.ticks`` / ``level2.capital_flow``）全部走
  ``self.tasks.run(...)``，主线程不被网络阻塞；
* 下面另有 4 个**按需触发**的 L2 工具区块：大单追踪（``level2.big_orders``）、
  资金流分时（``level2.flow_series``）、封板状态（``level2.seal_status``）、
  自选池扫描 / 资金流排行（``level2.scan`` / ``level2.flow_rank``）；点按钮才发请求，
  进行中的工具只禁用**自己的**按钮，其它区块照常可用（排行 10 只可能 2–4 分钟）；
* 自动刷新（3 秒）**只**刷新上面三个基础接口，绝不自动触发这 4 个工具：扫描约 1 秒/只、
  排行约 10–25 秒/只，自动跑会打爆数据源（见 ``_auto_tick`` 的注释与页面灰字）；
* 失败 / 离线 / 无数据一律画**页内内联空状态**（表格中间灰字 + 顶部提示条 + 图表内文案），
  不弹模态框。
"""

import re
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView
# 复用代码规范化 / 滚动容器 / 图表内联空状态，避免两页行为分叉
from .market import (ScrollArea, _clear_chart_empty, _set_chart_empty,
                     normalize_code_text)

try:                                    # 控件库由并行子任务提供；未交付时降级运行
    from ..widgets import cards, charts, table

    WIDGETS_ERROR = ""
except Exception as exc:                # noqa: BLE001 - 任何导入问题都只影响观感，不影响页面存活
    cards = charts = table = None                   # type: ignore[assignment]
    WIDGETS_ERROR = "控件库不可用：%s" % exc

# --------------------------------------------------------------------------- 常量

LEVELS = 5                             # 免费源快照档数（付费源可为 10 档，界面按数据实际档数画）
TICK_LIMIT = 60                        # 逐笔最多展示 60 行
TICK_REQUEST_LIMIT = 60                # 请求逐笔条数（契约：ticks(code, limit=60)）
FLOW_LIMIT = 2000                      # 资金流样本笔数（契约默认）
AUTO_REFRESH_MS = 3000                 # 自动刷新间隔（默认关）

DISCLAIMER = ("五档为公开源快照，逐笔方向为第三方盘口标记（非交易所 Level-2）；"
              "十档/逐笔委托/委托队列需付费授权")

METRIC_CARDS: Tuple[Tuple[str, str], ...] = (
    ("price", "现价"),
    ("change_pct", "涨跌幅"),
    ("imbalance", "委比"),
    ("diff", "委差（手）"),
    ("outer", "外盘（手）"),
    ("inner", "内盘（手）"),
)

BUCKET_ORDER: Tuple[str, ...] = ("super_big", "big", "mid", "small")
BUCKET_LABELS: Dict[str, str] = {"super_big": "超大单", "big": "大单", "mid": "中单", "small": "小单"}

SIDE_LABELS: Dict[str, str] = {"buy": "买", "sell": "卖", "neutral": "中性",
                               "b": "买", "s": "卖", "m": "中性"}


#: 五档「档位」列的着色映射（卖绿买红，见 ``table.DataTable`` 的 badge_colors）
SLOT_COLORS: Dict[str, str] = dict(
    [("卖%d" % index, "down") for index in range(1, LEVELS + 1)] +
    [("买%d" % index, "up") for index in range(1, LEVELS + 1)]
)

LEVEL_COLUMNS: List[Dict[str, Any]] = [
    {"key": "slot", "label": "档位", "width": 72, "anchor": "center", "kind": "badge",
     "badge_colors": SLOT_COLORS},
    {"key": "price", "label": "价格", "width": 96, "anchor": "e", "kind": "text"},
    {"key": "volume", "label": "手数", "width": 96, "anchor": "e", "kind": "text"},
    {"key": "amount_wan", "label": "金额(万元)", "width": 118, "anchor": "e", "kind": "text"},
]

TICK_COLUMNS: List[Dict[str, Any]] = [
    {"key": "time", "label": "时间", "width": 104, "anchor": "w", "kind": "text"},
    {"key": "price", "label": "价格", "width": 96, "anchor": "e", "kind": "text"},
    {"key": "volume", "label": "手数", "width": 88, "anchor": "e", "kind": "text"},
    {"key": "amount_wan", "label": "金额(万元)", "width": 118, "anchor": "e", "kind": "text"},
    {"key": "side", "label": "方向", "width": 76, "anchor": "center", "kind": "badge",
     "badge_colors": {"买": "up", "卖": "down", "中性": "flat"}},
]

FLOW_COLUMNS: List[Dict[str, Any]] = [
    {"key": "label", "label": "档位", "width": 84, "anchor": "w", "kind": "text"},
    {"key": "buy_wan", "label": "买入(万元)", "width": 118, "anchor": "e", "kind": "text"},
    {"key": "sell_wan", "label": "卖出(万元)", "width": 118, "anchor": "e", "kind": "text"},
    {"key": "net_wan", "label": "净额(万元)", "width": 118, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "buy_pct", "label": "买入占比", "width": 96, "anchor": "e", "kind": "text"},
    {"key": "count", "label": "笔数", "width": 76, "anchor": "e", "kind": "text"},
]

FLOW_NOTE = ("口径：净额 = 买入额 − 卖出额，按单笔成交额分档自算"
             "（≥100 万 超大单 / 20–100 万 大单 / 5–20 万 中单 / <5 万 小单）；"
             "主力 = 超大单 + 大单")

# ---- L2 工具（大单追踪 / 资金流分时 / 封板状态 / 扫描与排行）

AUTO_REFRESH_SCOPE = "自动刷新（3 秒）只刷新盘口 / 逐笔 / 资金流分档，不跑下方工具"

BIG_ORDER_THRESHOLDS: Tuple[Tuple[str, float], ...] = (
    ("20 万", 200000.0),
    ("50 万", 500000.0),
    ("100 万", 1000000.0),             # 默认：与「超大单」分档同口径
    ("200 万", 2000000.0),
)
DEFAULT_BIG_THRESHOLD = 1000000.0
BIG_ORDER_LIMIT = 50                   # 契约默认：最多展示 50 笔
BIG_ORDER_CARDS: Tuple[Tuple[str, str], ...] = (
    ("count", "大单笔数"),
    ("buy", "买入额（万元）"),
    ("sell", "卖出额（万元）"),
    ("net", "净额（万元）"),
)
BIG_ORDER_NOTE = ("口径：样本 = 拉取到的逐笔（约最近 4000 笔），单笔金额 ≥ 阈值即计入（时间倒序）；"
                  "方向为第三方盘口标记，非交易所 Level-2 主动买卖判定")

FLOW_SERIES_LIMIT = FLOW_LIMIT         # 与「资金流分档」同一样本口径（2000 笔）
SERIES_CARDS: Tuple[Tuple[str, str], ...] = (
    ("main", "主力净额（万元）"),
    ("main_pct", "主力净额占比"),
    ("minutes", "样本分钟数"),
)
FLOW_SERIES_NOTE = ("口径：累计净额 = 每分钟（主动买 − 主动卖）按时间累加，单位万元；"
                    "主力 = 超大单 + 大单；曲线颜色按区间净额方向红涨绿跌")

SEAL_BADGE_KINDS: Dict[str, str] = {"limit_up": "up", "limit_down": "down",
                                    "normal": "flat", "unknown": "flat"}
SEAL_KEYS_LEFT: Tuple[str, ...] = ("现价", "昨收", "涨停价", "跌停价")
SEAL_KEYS_RIGHT: Tuple[str, ...] = ("距涨停 %", "封单量（手）", "封单额（万元）", "封成比 %")
SEAL_NOTE = ("只按当前快照判断此刻是否封板；开板次数需盘中多次采样。"
             "「距涨停 %」未封板时是到涨停价的幅度，封板后是相对封板价的偏差（≈0）")

TOOL_CODES_MAX = 10                    # 扫描 / 排行最多标的数（服务端同样是 10）
SCAN_LIMIT = TOOL_CODES_MAX
RANK_LIMIT = 1000                      # 排行每只标的的逐笔样本（契约默认）
RANK_TOP = TOOL_CODES_MAX
TOOL_CODES_NOTE = "标的 = 上方「标的」输入 +（勾选时）自选池，去重后最多 %d 只" % TOOL_CODES_MAX
TOOL_LATENCY_HINT = ("耗时提示：大单 / 分时约 3–10 秒；扫描约 1 秒/只；资金流排行约 10–25 秒/只"
                     "（%d 只可能 2–4 分钟，进行中可继续操作其它区块）" % TOOL_CODES_MAX)
RANK_IDLE_TEXT = "点击「资金流排行」后在后台计算：%d 只最多约 2–4 分钟" % TOOL_CODES_MAX
RANK_RUNNING_TEXT = "资金流排行进行中：最多约 2–4 分钟（%d 只 × 10–25 秒/只），可继续操作其它区块" \
                    % TOOL_CODES_MAX
SCAN_NOTE = "按委比降序的单次快照排序；挂单骤增 / 大单撤单等突变需要两次以上采样，本工具不承诺"
RANK_NOTE = "主力净额 = 超大单 + 大单净额（按单笔成交额分档自算，样本约最近 4000 笔），按主力净额降序"

# 大单表：只有「方向」一列着色 → 行色即买红卖绿（价格 / 金额列一律格式化成文本，避免抢行色）
BIG_ORDER_COLUMNS: List[Dict[str, Any]] = [
    {"key": "time", "label": "时间", "width": 104, "anchor": "w", "kind": "text"},
    {"key": "price", "label": "价格", "width": 96, "anchor": "e", "kind": "text"},
    {"key": "volume", "label": "手数", "width": 88, "anchor": "e", "kind": "text"},
    {"key": "amount_wan", "label": "金额(万元)", "width": 118, "anchor": "e", "kind": "text"},
    {"key": "side", "label": "方向", "width": 76, "anchor": "center", "kind": "badge",
     "badge_colors": {"买": "up", "卖": "down", "中性": "flat"}},
    {"key": "bucket", "label": "档位", "width": 88, "anchor": "center", "kind": "text"},
]

# 扫描表：只有「涨跌幅」是着色列（红涨绿跌），其余列格式化成文本；
# 列宽合计 ~780px，普通窗口（≥900px 宽）不需要横向滚动
SCAN_COLUMNS: List[Dict[str, Any]] = [
    {"key": "code", "label": "代码", "width": 96, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 92, "anchor": "w", "kind": "text"},
    {"key": "price", "label": "现价", "width": 80, "anchor": "e", "kind": "text"},
    {"key": "change_pct", "label": "涨跌幅", "width": 80, "anchor": "e", "kind": "pct", "digits": 2},
    {"key": "imbalance_pct", "label": "委比", "width": 80, "anchor": "e", "kind": "text"},
    {"key": "diff", "label": "委差(手)", "width": 88, "anchor": "e", "kind": "text"},
    {"key": "volume_ratio", "label": "量比", "width": 68, "anchor": "e", "kind": "text"},
    {"key": "seal", "label": "封板", "width": 76, "anchor": "center", "kind": "text"},
    {"key": "distance_pct", "label": "距涨停%", "width": 84, "anchor": "e", "kind": "text"},
]

# 排行表：只有「主力净额(万元)」是着色列（红涨绿跌）
RANK_COLUMNS: List[Dict[str, Any]] = [
    {"key": "code", "label": "代码", "width": 110, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 110, "anchor": "w", "kind": "text"},
    {"key": "main_wan", "label": "主力净额(万元)", "width": 132, "anchor": "e", "kind": "num",
     "digits": 2},
    {"key": "main_pct", "label": "主力净额占比", "width": 118, "anchor": "e", "kind": "text"},
    {"key": "tick_count", "label": "样本笔数", "width": 100, "anchor": "e", "kind": "text"},
]

_CODE_SUFFIX = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


# --------------------------------------------------------------------------- 工具函数


def _as_float(value: Any) -> Optional[float]:
    """宽松转数字（None / 空 / 非数字 / NaN / inf → None）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _wan(value: Any, digits: int = 2) -> str:
    """元 → ``万元`` 文本（缺失显示「—」）。"""
    number = _as_float(value)
    if number is None:
        return "—"
    return theme.fmt_money(number / 10000.0, digits)


def _wan_number(value: Any) -> Optional[float]:
    """元 → 万元（数值，供表格按数值着色）；缺失返回 None（表格显示「—」）。"""
    number = _as_float(value)
    return None if number is None else number / 10000.0


def _change_pct(price: Any, prev_close: Any) -> Optional[float]:
    """涨跌幅（%）；缺昨收时返回 None（界面显示「—」）。"""
    current = _as_float(price)
    previous = _as_float(prev_close)
    if current is None or not previous:
        return None
    return (current - previous) / previous * 100.0


def _side_label(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return SIDE_LABELS.get(text.lower(), text or "—")


def _capability_text(caps: Any) -> str:
    """能力位 → 一句话（让界面如实说明「哪些能拿到、哪些要付费授权」）。"""
    data = dict(caps or {})
    if not data:
        return ""
    parts: List[str] = []
    if data.get("orderbook"):
        parts.append("%s 档盘口" % (data.get("orderbook_levels") or LEVELS))
    if data.get("ticks"):
        parts.append("逐笔成交")
    if data.get("orders"):
        parts.append("逐笔委托")
    if data.get("queue"):
        parts.append("委托队列")
    if not data.get("orders") and not data.get("queue"):
        parts.append("无逐笔委托 / 委托队列（需付费授权）")
    if data.get("import"):
        parts.append("本地导入通道可用")
    return " · ".join(str(item) for item in parts)


def _short(text: Any, limit: int = 120) -> str:
    """内联提示文案截断（卡片副标题空间有限，不靠裁剪隐藏布局缺陷）。"""
    value = str(text if text is not None else "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _card_set(card: Any, value: str, color_by: Any = None, sub: Optional[str] = None) -> None:
    """``StatCard.set`` 的容错封装：颜色/副标题参数不被支持时逐级退化。"""
    try:
        card.set(value, color_by=color_by, sub=sub)
        return
    except Exception:                     # noqa: BLE001 - 控件实现差异，退化重试
        pass
    try:
        card.set(value, sub=sub)
    except Exception:                     # noqa: BLE001
        card.set(value)


def _pct_text(value: Any, digits: int = 2) -> str:
    """比例 → ``12.30%``（不带正负号；缺失「—」）：封成比 / 占比这类非涨跌语义用。"""
    number = _as_float(value)
    if number is None:
        return "—"
    return "%s%%" % theme.fmt_num(number, digits)


def _threshold_label(value: Any) -> str:
    """阈值（元）→ 下拉里的标签（20 万 / 50 万 / 100 万 / 200 万）；表外数值按「x 万」显示。"""
    number = _as_float(value)
    if number is None:
        return "—"
    for label, candidate in BIG_ORDER_THRESHOLDS:
        if abs(candidate - number) < 1.0:
            return label
    return "%s 万" % theme.fmt_num(number / 10000.0, 0)


def _sort_desc(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """按数值字段降序（缺字段排最后）：服务端已排过一次，这里保证界面顺序稳定可断言。"""
    def sort_key(item: Dict[str, Any]) -> float:
        number = _as_float(item.get(key))
        return float("-inf") if number is None else number

    return sorted(items, key=sort_key, reverse=True)


def _failures_text(failures: Any, shown: int = 3) -> str:
    """``failures`` → 一行内联提示（最多列出 ``shown`` 只，其余折叠成数量）。"""
    rows = [item for item in (failures or []) if isinstance(item, dict)]
    if not rows:
        return ""
    parts = ["%s：%s" % (item.get("code") or "?", _short(item.get("error"), 60))
             for item in rows[:shown]]
    tail = "，等 %d 只" % len(rows) if len(rows) > shown else ""
    return "⚠ 失败 %d 只：%s%s" % (len(rows), "；".join(parts), tail)


def _codes_text(codes: Any, limit: int = TOOL_CODES_MAX) -> str:
    """标的列表 → 一行文本（超出 ``limit`` 只折叠成数量）。"""
    rows = [str(item) for item in (codes or []) if str(item).strip()]
    if not rows:
        return "—"
    if len(rows) > limit:
        return "、".join(rows[:limit]) + "，等 %d 只" % len(rows)
    return "、".join(rows)


# --------------------------------------------------------------------------- 页面


class Level2View(BaseView):
    """盘口 / L2 页面：五档 + 逐笔 + 资金流分档。"""

    id = "level2"
    title = "盘口 / L2"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._pending = 0                       # 未完成的请求数（测试与状态栏用）
        self._widget_error = WIDGETS_ERROR
        self.build_errors: List[str] = []
        self.fallback: Optional[tk.Text] = None
        self._first_widget: Optional[Any] = None
        self._code = ""                         # 最近一次查询的规范化代码
        self._orderbook_rows: List[Dict[str, Any]] = []
        self._tick_rows: List[Dict[str, Any]] = []
        self._flow_rows: List[Dict[str, Any]] = []
        self._auto_job: Optional[str] = None    # 自动刷新的 after id（关 = None）
        # 控件引用（构建失败时为 None，渲染前一律判空）
        self.metric_cards: Dict[str, Any] = {}
        self.flow_cards: Dict[str, Any] = {}
        self.level_table = None
        self.tick_table = None
        self.flow_table = None
        self.notice_label = None
        self.status_label = None
        # ---- L2 工具区块（大单追踪 / 资金流分时 / 封板状态 / 扫描与排行）
        self.big_rows: List[Dict[str, Any]] = []
        self.series_values: List[Any] = []
        self.series_labels: List[str] = []
        self.scan_rows: List[Dict[str, Any]] = []
        self.rank_rows: List[Dict[str, Any]] = []
        self.seal_state = "unknown"
        self.big_cards: Dict[str, Any] = {}
        self.series_cards: Dict[str, Any] = {}
        self.series_bucket_cards: Dict[str, Any] = {}
        self.big_box = None
        self.big_button = None
        self.series_button = None
        self.seal_button = None
        self.scan_button = None
        self.rank_button = None
        self.include_watch_check = None
        self.big_title = None
        self.big_table = None
        self.big_note = None
        self.flow_chart = None
        self.series_chart_note = None
        self.series_title = None
        self.series_note = None
        self.seal_badge = None
        self.seal_title = None
        self.seal_kv_left = None
        self.seal_kv_right = None
        self.seal_note = None
        self.tool_note = None
        self.tool_codes_label = None
        self.scan_title = None
        self.scan_table = None
        self.scan_failures = None
        self.scan_note = None
        self.rank_title = None
        self.rank_status = None
        self.rank_table = None
        self.rank_failures = None
        self.rank_note = None

    # ------------------------------------------------------------------ 构建

    def build(self) -> None:
        self.code_var = tk.StringVar(value="")
        self.auto_var = tk.StringVar(value="0")             # 「自动刷新（3 秒）」默认关
        self.big_threshold_var = tk.StringVar(value=_threshold_label(DEFAULT_BIG_THRESHOLD))
        self.include_watch_var = tk.StringVar(value="1")    # 扫描 / 排行默认带上自选池

        self.scroll = ScrollArea(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body

        # 离线 / 缓存提示条（默认不显示，meta.offline / stale 时 pack 到最上方）
        self.notice_label = tk.Label(body, text="", bg=theme.COLORS["warn"], fg=theme.COLORS["bg"],
                                    font=theme.font(10, "bold"), anchor="w", justify="left",
                                    padx=10, pady=6)

        if self._widget_error:
            self._build_widget_error(body, self._widget_error)
            self._build_fallback_text(body)

        self._section(body, "查询", self._build_toolbar)
        self._section(body, "指标", self._build_metrics)
        self._section(body, "五档", self._build_levels)
        self._section(body, "逐笔", self._build_ticks)
        self._section(body, "资金流", self._build_flow)
        self._section(body, "大单追踪", self._build_big_orders)
        self._section(body, "资金流分时", self._build_flow_series)
        self._section(body, "封板状态", self._build_seal)
        self._section(body, "工具：扫描与排行", self._build_tools)

        try:                                # 页面销毁时停掉自动刷新，避免回调打到已销毁控件
            self.bind("<Destroy>", self._on_destroy, add="+")
        except tk.TclError:
            pass
        self._show_idle()

    def _section(self, body: Any, label: str, builder: Any) -> None:
        """构建一个区块：失败只影响本区块（内联提示），不影响其它区块。"""
        try:
            widget = builder(body)
        except Exception as exc:            # noqa: BLE001
            self.build_errors.append("%s：%s" % (label, exc))
            try:
                self._build_widget_error(body, "%s 区块构建失败：%s" % (label, exc))
            except Exception:               # noqa: BLE001
                pass
            return
        if self._first_widget is None and widget is not None:
            self._first_widget = widget

    def _build_widget_error(self, body: Any, message: str) -> None:
        frame = ttk.Frame(body, style="Card.TFrame", padding=12)
        frame.pack(fill="x", pady=(0, 8))
        ttk.Label(frame, text="界面控件不可用：%s" % message, style="Card.TLabel",
                  wraplength=780, justify="left").pack(anchor="w")
        ttk.Label(frame, text="数据仍会正常加载，可先用文本摘要查看（控件修好后自动恢复完整界面）。",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(4, 0))
        if self._first_widget is None:
            self._first_widget = frame

    def _build_fallback_text(self, body: Any) -> None:
        self.fallback = tk.Text(body, height=18, bg=theme.COLORS["panel"], fg=theme.COLORS["text"],
                                relief="flat", font=theme.font(10, mono=True), wrap="word",
                                padx=10, pady=8)
        self.fallback.pack(fill="both", expand=True)
        self.fallback.configure(state="disabled")

    # ---- 各区块

    def _build_toolbar(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "盘口 / L2", hint="公开源近似的五档 · 逐笔 · 资金流分档")
        title.pack(fill="x", pady=(0, 4))

        # 数据能力边界：显著位置的一行灰字（合规要求，不可省略）
        ttk.Label(body, text=DISCLAIMER, style="Muted.TLabel", wraplength=860,
                  justify="left").pack(fill="x", pady=(0, 8))

        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="标的", style="Muted.TLabel").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.code_var, width=16)
        entry.pack(side="left", padx=(4, 8))
        entry.bind("<Return>", lambda _event: self.reload())
        self.code_entry = entry

        self.query_button = ttk.Button(bar, text="查询", style="Primary.TButton", command=self.reload)
        self.query_button.pack(side="left")

        self.auto_check = ttk.Checkbutton(bar, text="自动刷新（3 秒）", variable=self.auto_var,
                                          onvalue="1", offvalue="0", style="TCheckbutton",
                                          command=self._on_auto_toggle)
        self.auto_check.pack(side="left", padx=(12, 0))
        # 自动刷新的作用范围写在开关旁边（避免用户以为批量工具也在自动跑）
        ttk.Label(bar, text="（%s）" % AUTO_REFRESH_SCOPE, style="Muted.TLabel").pack(side="left",
                                                                                     padx=(4, 0))

        self.status_label = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                      justify="left")
        self.status_label.pack(fill="x", pady=(6, 12))
        return title

    def _build_metrics(self, body: Any) -> Any:
        grid = cards.CardGrid(body, columns=3, gap=8)
        grid.pack(fill="x", pady=(0, 12))
        self.metric_grid = grid
        for key, label in METRIC_CARDS:
            card = cards.StatCard(grid, label, value="—", sub="等待查询…")
            grid.add(card)
            self.metric_cards[key] = card
        return grid

    def _build_levels(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "五档盘口", hint="行序：卖 5 → 卖 1 → 买 1 → 买 5（卖绿买红）")
        heading.pack(fill="x", pady=(0, 4))
        self.level_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                     justify="left")
        self.level_title.pack(fill="x", pady=(0, 4))
        self.level_table = table.DataTable(body, LEVEL_COLUMNS, height=11, sortable=False)
        self.level_table.pack(fill="x", pady=(0, 12))
        return heading

    def _build_ticks(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "逐笔成交",
                                     hint="最多展示最近 %d 笔；方向为第三方盘口标记" % TICK_LIMIT)
        heading.pack(fill="x", pady=(0, 4))
        self.tick_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                    justify="left")
        self.tick_title.pack(fill="x", pady=(0, 4))
        self.tick_table = table.DataTable(body, TICK_COLUMNS, height=14, sortable=False)
        self.tick_table.pack(fill="x", pady=(0, 12))
        return heading

    def _build_flow(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "资金流（按单笔成交额分档）",
                                     hint="超大单 / 大单 / 中单 / 小单 的净额与买入占比")
        heading.pack(fill="x", pady=(0, 4))
        grid = cards.CardGrid(body, columns=2, gap=8)
        grid.pack(fill="x", pady=(0, 8))
        self.flow_grid = grid
        for key, label in (("main", "主力净额（万元）"), ("main_pct", "主力净额占比")):
            card = cards.StatCard(grid, label, value="—", sub="等待查询…")
            grid.add(card)
            self.flow_cards[key] = card
        self.flow_table = table.DataTable(body, FLOW_COLUMNS, height=5, sortable=False)
        self.flow_table.pack(fill="x")
        self.flow_note = ttk.Label(body, text=FLOW_NOTE, style="Muted.TLabel", wraplength=860,
                                   justify="left")
        self.flow_note.pack(fill="x", pady=(4, 12))
        return heading


    def _build_big_orders(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "大单追踪",
                                     hint="逐笔里单笔金额 ≥ 阈值的大单（时间倒序，最多展示 %d 笔）"
                                          % BIG_ORDER_LIMIT)
        heading.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="阈值", style="Muted.TLabel").pack(side="left")
        self.big_box = ttk.Combobox(bar, state="readonly", width=8,
                                    textvariable=self.big_threshold_var,
                                    values=[label for label, _value in BIG_ORDER_THRESHOLDS])
        self.big_box.pack(side="left", padx=(4, 8))
        self.big_button = ttk.Button(bar, text="查大单", command=self.query_big_orders)
        self.big_button.pack(side="left")
        ttk.Label(bar, text="约 3–10 秒（取逐笔样本）", style="Muted.TLabel").pack(side="left",
                                                                                 padx=(8, 0))

        self.big_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860, justify="left")
        self.big_title.pack(fill="x", pady=(6, 4))
        self.big_grid = cards.CardGrid(body, columns=4, gap=8)
        self.big_grid.pack(fill="x", pady=(0, 8))
        for key, label in BIG_ORDER_CARDS:
            card = cards.StatCard(self.big_grid, label, value="—", sub="等待查询…")
            self.big_grid.add(card)
            self.big_cards[key] = card
        self.big_table = table.DataTable(body, BIG_ORDER_COLUMNS, height=8, sortable=False)
        self.big_table.pack(fill="x")
        self.big_note = ttk.Label(body, text=BIG_ORDER_NOTE, style="Muted.TLabel", wraplength=860,
                                  justify="left")
        self.big_note.pack(fill="x", pady=(4, 12))
        return heading

    def _build_flow_series(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "资金流分时",
                                     hint="逐笔按分钟聚合的累计净额曲线（x = 分钟，y = 万元）")
        heading.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        self.series_button = ttk.Button(bar, text="查分时", command=self.query_flow_series)
        self.series_button.pack(side="left")
        ttk.Label(bar, text="约 3–10 秒（逐笔样本最多 %d 笔）" % FLOW_SERIES_LIMIT,
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))

        self.series_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                      justify="left")
        self.series_title.pack(fill="x", pady=(6, 4))
        self.flow_chart = charts.LineChart(body, height=220, empty_text="等待查询：点「查分时」")
        self.flow_chart.pack(fill="x", pady=(0, 8))
        # 图表内联提示（空状态 / 失败的叠加文案，与行情页同一套处理）
        self.series_chart_note = tk.Label(self.flow_chart, text="", bg=theme.COLORS["panel"],
                                         fg=theme.COLORS["text2"], font=theme.font(10),
                                         justify="center")
        self.series_grid = cards.CardGrid(body, columns=3, gap=8)
        self.series_grid.pack(fill="x", pady=(0, 8))
        for key, label in SERIES_CARDS:
            card = cards.StatCard(self.series_grid, label, value="—", sub="等待查询…")
            self.series_grid.add(card)
            self.series_cards[key] = card
        self.bucket_grid = cards.CardGrid(body, columns=4, gap=8)
        self.bucket_grid.pack(fill="x", pady=(0, 8))
        for key in BUCKET_ORDER:
            card = cards.StatCard(self.bucket_grid, "%s净额（万元）" % BUCKET_LABELS[key],
                                  value="—", sub="等待查询…")
            self.bucket_grid.add(card)
            self.series_bucket_cards[key] = card
        self.series_note = ttk.Label(body, text=FLOW_SERIES_NOTE, style="Muted.TLabel",
                                     wraplength=860, justify="left")
        self.series_note.pack(fill="x", pady=(0, 12))
        return heading

    def _build_seal(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "封板状态",
                                     hint="涨停 / 跌停 / 未封板 + 封单量、封单额、封成比（单次快照）")
        heading.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        self.seal_button = ttk.Button(bar, text="查封板", command=self.query_seal)
        self.seal_button.pack(side="left")
        ttk.Label(bar, text="约 1–3 秒（单只快照）", style="Muted.TLabel").pack(side="left",
                                                                              padx=(8, 0))

        card = ttk.Frame(body, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(6, 4))
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        self.seal_badge = cards.Badge(top, "未查询", kind="flat")
        self.seal_badge.pack(side="left")
        self.seal_title = ttk.Label(top, text="", style="CardMuted.TLabel", anchor="w",
                                    justify="left")
        self.seal_title.pack(side="left", padx=(8, 0))
        columns = ttk.Frame(card, style="Card.TFrame")
        columns.pack(fill="x", pady=(8, 0))
        left = ttk.Frame(columns, style="Card.TFrame")
        left.pack(side="left", fill="x", expand=True)
        right = ttk.Frame(columns, style="Card.TFrame")
        right.pack(side="left", fill="x", expand=True)
        self.seal_kv_left = cards.KeyValueTable(left, [(key, None) for key in SEAL_KEYS_LEFT])
        self.seal_kv_left.pack(fill="x")
        self.seal_kv_right = cards.KeyValueTable(right, [(key, None) for key in SEAL_KEYS_RIGHT])
        self.seal_kv_right.pack(fill="x")
        self.seal_note = ttk.Label(body, text=SEAL_NOTE, style="Muted.TLabel", wraplength=860,
                                   justify="left")
        self.seal_note.pack(fill="x", pady=(0, 12))
        return heading

    def _build_tools(self, body: Any) -> Any:
        heading = cards.SectionTitle(body, "工具：扫描与排行",
                                     hint="批量标的 = 上方输入 +（勾选时）自选池，去重后最多 %d 只"
                                          % TOOL_CODES_MAX)
        heading.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        self.include_watch_check = ttk.Checkbutton(bar, text="含自选池",
                                                   variable=self.include_watch_var,
                                                   onvalue="1", offvalue="0",
                                                   style="TCheckbutton")
        self.include_watch_check.pack(side="left")
        self.scan_button = ttk.Button(bar, text="扫描自选池", command=self.query_scan)
        self.scan_button.pack(side="left", padx=(12, 6))
        self.rank_button = ttk.Button(bar, text="资金流排行", command=self.query_flow_rank)
        self.rank_button.pack(side="left")
        ttk.Label(bar, text="两个工具可同时跑，互不阻塞", style="Muted.TLabel").pack(side="left",
                                                                                  padx=(8, 0))
        # 耗时提示：排行 10 只可能 2–4 分钟，先说清楚再让用户点
        self.tool_note = ttk.Label(body, text=TOOL_LATENCY_HINT, style="Muted.TLabel",
                                   wraplength=860, justify="left")
        self.tool_note.pack(fill="x", pady=(6, 2))
        self.tool_codes_label = ttk.Label(body, text=TOOL_CODES_NOTE, style="Muted.TLabel",
                                          wraplength=860, justify="left")
        self.tool_codes_label.pack(fill="x", pady=(0, 8))

        self.scan_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                    justify="left")
        self.scan_title.pack(fill="x", pady=(0, 4))
        self.scan_table = table.DataTable(body, SCAN_COLUMNS, height=8, sortable=False)
        self.scan_table.pack(fill="x")
        self.scan_failures = tk.Label(body, text="", bg=theme.COLORS["bg"], fg=theme.COLORS["warn"],
                                      font=theme.font(9), anchor="w", justify="left", wraplength=860)
        self.scan_failures.pack(fill="x")
        self.scan_note = ttk.Label(body, text=SCAN_NOTE, style="Muted.TLabel", wraplength=860,
                                   justify="left")
        self.scan_note.pack(fill="x", pady=(2, 12))

        self.rank_title = ttk.Label(body, text="", style="Muted.TLabel", wraplength=860,
                                    justify="left")
        self.rank_title.pack(fill="x", pady=(0, 4))
        self.rank_status = ttk.Label(body, text=RANK_IDLE_TEXT, style="Muted.TLabel", wraplength=860,
                                     justify="left")
        self.rank_status.pack(fill="x", pady=(0, 4))
        self.rank_table = table.DataTable(body, RANK_COLUMNS, height=8, sortable=False)
        self.rank_table.pack(fill="x")
        self.rank_failures = tk.Label(body, text="", bg=theme.COLORS["bg"], fg=theme.COLORS["warn"],
                                      font=theme.font(9), anchor="w", justify="left", wraplength=860)
        self.rank_failures.pack(fill="x")
        self.rank_note = ttk.Label(body, text=RANK_NOTE, style="Muted.TLabel", wraplength=860,
                                   justify="left")
        self.rank_note.pack(fill="x", pady=(2, 0))
        return heading
    # ------------------------------------------------------------------ 数据加载

    def _request(self, fn: Any, *args: Any, on_done: Any = None, on_error: Any = None,
                 **kwargs: Any) -> int:
        """统一的后台请求：计数 + 回调保护（渲染异常只提示，不打断主循环）。"""
        self._pending += 1

        def _done(payload: Any) -> None:
            self._pending = max(0, self._pending - 1)
            try:
                if on_done is not None:
                    on_done(payload)
            except Exception as exc:        # noqa: BLE001
                self.toast_error(exc)

        def _fail(exc: BaseException) -> None:
            self._pending = max(0, self._pending - 1)
            try:
                if on_error is not None:
                    on_error(exc)
                else:
                    self.toast_error(exc)
            except Exception as inner:      # noqa: BLE001
                try:
                    self.toast_error(inner)
                except Exception:           # noqa: BLE001 - 提示失败也不能逃出 Tk 回调
                    pass

        try:
            return self.load(fn, *args, on_done=_done, on_error=_fail, **kwargs)
        except Exception as exc:            # noqa: BLE001 - 任务队列不可用（例如窗口正在销毁）
            self._pending = max(0, self._pending - 1)
            _fail(exc)
            return -1

    # ---- 服务入口（契约：services.level2.* 返回冻结的 JSON 结构）

    def _level2(self) -> Any:
        """``services.level2`` 命名空间；未接入时给出可读原因（画成内联空状态，不弹窗）。"""
        namespace = getattr(self.services, "level2", None)
        if namespace is None:
            raise RuntimeError("服务层尚未提供 level2（需要 services/level2_service.py）")
        return namespace

    def _orderbook(self, code: str) -> Any:
        return self._level2().orderbook(code)

    def _ticks(self, code: str) -> Any:
        return self._level2().ticks(code, limit=TICK_REQUEST_LIMIT)

    def _capital_flow(self, code: str) -> Any:
        return self._level2().capital_flow(code, limit=FLOW_LIMIT)

    # ---- 服务入口：L2 工具（同样走 self.tasks.run，见各 query_* 方法）

    def _big_orders(self, code: str, threshold: float) -> Any:
        return self._level2().big_orders(code, threshold=threshold, limit=BIG_ORDER_LIMIT)

    def _flow_series(self, code: str) -> Any:
        return self._level2().flow_series(code, limit=FLOW_SERIES_LIMIT)

    def _seal_status(self, code: str) -> Any:
        return self._level2().seal_status(code)

    def _scan(self, codes: List[str]) -> Any:
        return self._level2().scan(codes, limit=SCAN_LIMIT)

    def _flow_rank(self, codes: List[str]) -> Any:
        return self._level2().flow_rank(codes, limit=RANK_LIMIT, top=RANK_TOP)

    # ---- 刷新

    def reload(self) -> None:
        """查询当前输入的标的；代码为空时只画引导文案，不发任何请求。"""
        code = self._normalize(self._current_code())
        if not code:
            self._show_idle()
            self.status("盘口 / L2：请输入标的代码后点「查询」")
            return
        self._code = code
        self._set_status("正在查询 %s 的盘口 / 逐笔 / 资金流…" % code)
        self.status("正在查询 %s 的盘口数据…" % code)
        self._request(self._orderbook, code, on_done=self._render_orderbook,
                      on_error=lambda exc: self._on_orderbook_error(exc, code))
        self._request(self._ticks, code, on_done=self._render_ticks,
                      on_error=lambda exc: self._on_block_error("逐笔", exc, self.tick_table))
        self._request(self._capital_flow, code, on_done=self._render_flow,
                      on_error=lambda exc: self._on_block_error("资金流", exc, self.flow_table))

    # ---- 工具区块：入口（主线程只读控件 + 起后台任务，网络耗时全在 self.tasks.run 里）

    def _selected_threshold(self) -> float:
        """阈值下拉 → 元（认不出的标签用默认 100 万）。"""
        label = ""
        try:
            label = str(self.big_threshold_var.get() or "")
        except tk.TclError:
            label = ""
        for name, value in BIG_ORDER_THRESHOLDS:
            if name == label:
                return value
        return DEFAULT_BIG_THRESHOLD

    def _include_watchlist(self) -> bool:
        try:
            return str(self.include_watch_var.get()) == "1"
        except tk.TclError:
            return False

    def _resolve_codes(self, entered: str, include_watch: bool) -> Tuple[List[str], List[str]]:
        """标的列表 = 输入框 +（可选）自选池，去重后最多 ``TOOL_CODES_MAX`` 只 → (codes, 备注)。

        **只在后台线程调用**：会读 ``services.watchlist()``（文件 IO），且不碰任何 Tk 控件。
        """
        codes: List[str] = []
        notes: List[str] = []
        code = self._normalize(entered)
        if code:
            codes.append(code)
        if include_watch:
            loader = getattr(self.services, "watchlist", None)
            if not callable(loader):
                notes.append("当前服务未提供自选池（services.watchlist）")
            else:
                try:
                    watch = list(loader() or [])
                except Exception as exc:        # noqa: BLE001 - 自选池读不到不影响手输标的
                    watch = []
                    notes.append("自选池读取失败：%s" % exc)
                for item in watch:
                    normalized = self._normalize(item)
                    if normalized and normalized not in codes:
                        codes.append(normalized)
            if code and len(codes) == 1:
                notes.append("自选池为空或与输入重复")
        if len(codes) > TOOL_CODES_MAX:
            notes.append("标的超过 %d 只，本次只取前 %d 只" % (TOOL_CODES_MAX, TOOL_CODES_MAX))
            codes = codes[:TOOL_CODES_MAX]
        return codes, notes

    def _scan_task(self, entered: str, include_watch: bool) -> Any:
        """后台线程：解析标的 → ``level2.scan``（把解析结果一并带回主线程显示）。"""
        codes, notes = self._resolve_codes(entered, include_watch)
        if not codes:
            raise RuntimeError("没有可用标的：请先在「标的」输入代码，或勾选「含自选池」")
        data = dict(self._scan(codes) or {})
        data["codes"] = codes
        data["resolve_notes"] = notes
        return data

    def _rank_task(self, entered: str, include_watch: bool) -> Any:
        """后台线程：解析标的 → ``level2.flow_rank``（10 只可能 2–4 分钟）。"""
        codes, notes = self._resolve_codes(entered, include_watch)
        if not codes:
            raise RuntimeError("没有可用标的：请先在「标的」输入代码，或勾选「含自选池」")
        data = dict(self._flow_rank(codes) or {})
        data["codes"] = codes
        data["resolve_notes"] = notes
        return data

    def query_big_orders(self) -> None:
        """大单追踪：当前标的 + 阈值下拉（默认 100 万）。"""
        code = self._normalize(self._current_code())
        if not code:
            self._tool_hint("大单追踪", self.big_table, self.big_title,
                            "大单追踪：请先在上方「标的」输入代码后点「查大单」")
            return
        threshold = self._selected_threshold()
        self._tool_loading(self.big_table, "正在拉取大单（约 3–10 秒）…")
        self._set_inline(self.big_title,
                         "%s · 阈值 %s · 查询中…" % (code, _threshold_label(threshold)))
        self.status("正在查询 %s 的 %s 以上大单…" % (code, _threshold_label(threshold)))
        self._tool_run(self.big_button, self._big_orders, code, threshold,
                       on_done=self._render_big_orders,
                       on_error=lambda exc: self._on_tool_error("大单追踪", exc, self.big_table,
                                                               self.big_title))

    def query_flow_series(self) -> None:
        """资金流分时：当前标的的分钟累计净额曲线。"""
        code = self._normalize(self._current_code())
        if not code:
            _set_chart_empty(self.flow_chart, self.series_chart_note,
                             "资金流分时：请先在上方输入标的代码")
            self._set_inline(self.series_title, "资金流分时：请先在上方输入标的代码")
            self._set_status("资金流分时：请输入标的代码")
            return
        _set_chart_empty(self.flow_chart, self.series_chart_note,
                         "正在拉取逐笔并聚合分钟…（约 3–10 秒）")
        self._set_inline(self.series_title, "%s · 查询中…" % code)
        self.status("正在查询 %s 的资金流分时…" % code)
        self._tool_run(self.series_button, self._flow_series, code,
                       on_done=self._render_flow_series,
                       on_error=self._on_series_error)

    def query_seal(self) -> None:
        """封板状态：当前标的的单次快照判断。"""
        code = self._normalize(self._current_code())
        if not code:
            self._seal_set("未查询", "flat", "封板状态：请先在上方输入标的代码",
                           self._seal_values({}))
            self._set_status("封板状态：请输入标的代码")
            return
        self._seal_set("查询中…", "flat", "%s · 查询中…" % code, self._seal_values({}))
        self.status("正在查询 %s 的封板状态…" % code)
        self._tool_run(self.seal_button, self._seal_status, code,
                       on_done=self._render_seal,
                       on_error=self._on_seal_error)

    def query_scan(self) -> None:
        """扫描自选池：当前输入 +（可选）自选池，≈1 秒/只。"""
        entered = self._current_code()
        include = self._include_watchlist()
        self._tool_loading(self.scan_table, "正在扫描标的快照…（约 1 秒/只）")
        self._set_inline(self.scan_failures, "")
        self._set_inline(self.scan_title, "扫描自选池：标的解析中…")
        self.status("正在扫描盘口（约 1 秒/只）…")
        self._tool_run(self.scan_button, self._scan_task, entered, include,
                       on_done=self._render_scan,
                       on_error=lambda exc: self._on_tool_error("扫描自选池", exc, self.scan_table,
                                                               self.scan_title))

    def query_flow_rank(self) -> None:
        """资金流排行：当前输入 +（可选）自选池，可能 2–4 分钟，**不阻塞其它区块**。"""
        entered = self._current_code()
        include = self._include_watchlist()
        self._tool_loading(self.rank_table, "正在计算资金流排行…（10 只最多约 2–4 分钟）")
        self._set_inline(self.rank_failures, "")
        self._set_inline(self.rank_title, "资金流排行：标的解析中…")
        self._set_inline(self.rank_status, RANK_RUNNING_TEXT)
        self.status(RANK_RUNNING_TEXT)
        self._tool_run(self.rank_button, self._rank_task, entered, include,
                       on_done=self._render_rank,
                       on_error=lambda exc: self._on_tool_error("资金流排行", exc, self.rank_table,
                                                               self.rank_title))

    # ---- 工具区块：通用状态封装（内联，不弹窗）

    def _set_inline(self, widget: Any, text: Any) -> None:
        """给某个 Label 换文案（控件缺失或已销毁都安静跳过）。"""
        if widget is None:
            return
        try:
            widget.configure(text=str(text))
        except tk.TclError:
            return

    def _set_note_text(self, widget: Any, note: Any, fallback: str) -> None:
        """灰字口径说明：优先用服务返回的 note（服务端口径是权威），否则用本地常量。"""
        text = str(note).strip() if note else ""
        self._set_inline(widget, text or fallback)

    def _set_button_enabled(self, button: Any, enabled: bool) -> None:
        """只禁用/恢复触发按钮本身（其它区块必须照常可用）。"""
        if button is None:
            return
        try:
            button.configure(state=("normal" if enabled else "disabled"))
        except tk.TclError:
            return

    def _tool_loading(self, widget: Any, text: str) -> None:
        """区块加载中：表格画内联「加载中」文案并清掉旧行（不弹窗、不锁其它区块）。"""
        if widget is None:
            return
        try:
            widget.set_empty_text(text)
            widget.clear()
        except Exception:                   # noqa: BLE001 - 控件实现差异，不影响页面存活
            pass

    def _tool_hint(self, name: str, widget: Any, title_widget: Any, text: str) -> None:
        """缺少输入时的引导（页内灰字，不是错误、不弹窗、不发请求）。"""
        self._tool_loading(widget, text)
        self._set_inline(title_widget, text)
        self._set_status(text)
        self.status(text)

    def _tool_run(self, button: Any, fn: Any, *args: Any, on_done: Any = None,
                  on_error: Any = None) -> int:
        """工具请求：只禁用触发按钮，完成后恢复；失败也恢复（其它区块全程可用）。"""
        self._set_button_enabled(button, False)

        def _done(payload: Any) -> None:
            try:
                if on_done is not None:
                    on_done(payload)
            finally:
                self._set_button_enabled(button, True)

        def _fail(exc: BaseException) -> None:
            try:
                if on_error is not None:
                    on_error(exc)
            finally:
                self._set_button_enabled(button, True)

        return self._request(fn, *args, on_done=_done, on_error=_fail)

    def _on_tool_error(self, name: str, exc: BaseException, widget: Any = None,
                       title_widget: Any = None) -> None:
        """工具失败：标题行 + 表格中间灰字 + 状态栏 + toast（全在页内，不弹窗）。"""
        text = "%s加载失败：%s" % (name, exc)
        self._set_inline(title_widget, text)
        self._block_error(name, exc, widget)

    # ---- 渲染：盘口

    def _render_orderbook(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        summary = dict(data.get("summary") or {})
        price = _as_float(data.get("price"))
        prev_close = _as_float(data.get("prev_close"))
        change = (price - prev_close) if (price is not None and prev_close is not None) else None
        change_pct = _change_pct(price, prev_close)
        bid_volume = _as_float(summary.get("bid_volume"))
        ask_volume = _as_float(summary.get("ask_volume"))
        diff = (bid_volume - ask_volume) if (bid_volume is not None and ask_volume is not None) else None
        imbalance = summary.get("imbalance_pct")
        name = data.get("name") or ""
        code = data.get("code") or self._code

        values: Dict[str, Tuple[str, Any, str]] = {
            "price": (theme.fmt_num(price, 2), change_pct,
                      "昨收 %s · 来源 %s" % (theme.fmt_num(prev_close, 2), data.get("source") or "—")),
            "change_pct": (theme.fmt_pct(change_pct), change_pct,
                           "涨跌额 %s" % theme.fmt_num(change, 2)),
            "imbalance": (theme.fmt_pct(imbalance), _as_float(imbalance),
                          "委买 %s 手 / 委卖 %s 手" % (theme.fmt_num(bid_volume, 0),
                                                       theme.fmt_num(ask_volume, 0))),
            "diff": (theme.fmt_num(diff, 0), diff, "委差 = 委买 − 委卖（手）"),
            "outer": (theme.fmt_num(data.get("outer_volume"), 0), None, "公开源口径的主动买手数"),
            "inner": (theme.fmt_num(data.get("inner_volume"), 0), None, "公开源口径的主动卖手数"),
        }
        for key, (value, color_by, sub) in values.items():
            card = self.metric_cards.get(key)
            if card is not None:
                _card_set(card, value, color_by=color_by, sub=_short(sub))

        rows = self._level_rows(data)
        self._orderbook_rows = rows
        if self.level_table is not None:
            self.level_table.set_empty_text("%s：五档数据为空" % self._empty_hint(meta))
            self.level_table.set_rows(rows, key_field="slot")
        if self.level_title is not None:
            parts = ["%s %s" % (name, code) if name else code]
            parts.append("来源 %s" % (data.get("source") or "—"))
            if data.get("ts"):
                parts.append("时点 %s" % data.get("ts"))
            parts.append("%s 档" % (len(rows) and max(len(data.get("bids") or []),
                                                      len(data.get("asks") or [])) or 0))
            caps = _capability_text(data.get("capabilities"))
            if caps:
                parts.append("能力：%s" % caps)
            try:
                self.level_title.configure(text=" · ".join(str(item) for item in parts))
            except tk.TclError:
                pass
        if not rows:
            self._fallback_line("五档：%s 无数据（来源 %s）" % (code, data.get("source") or "—"))
        self._set_status("五档 %d 行 · 现价 %s · 委比 %s" % (len(rows), theme.fmt_num(price, 2),
                                                            theme.fmt_pct(imbalance)))

    def _level_rows(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """卖 5 → 卖 1 → 买 1 → 买 5（档数不足时按实际的卖 N…卖 1 / 买 1…买 N）。"""
        asks = [item for item in (data.get("asks") or []) if isinstance(item, dict)][:LEVELS]
        bids = [item for item in (data.get("bids") or []) if isinstance(item, dict)][:LEVELS]
        rows: List[Dict[str, Any]] = []
        for offset, level in enumerate(reversed(asks)):
            rows.append(self._level_row("卖%d" % (len(asks) - offset), level))
        for offset, level in enumerate(bids):
            rows.append(self._level_row("买%d" % (offset + 1), level))
        return rows

    @staticmethod
    def _level_row(slot: str, level: Any) -> Dict[str, Any]:
        item = dict(level or {})
        price = _as_float(item.get("price"))
        volume = _as_float(item.get("volume"))
        return {
            "slot": slot,
            "price": theme.fmt_num(price, 2) if price is not None else "—",
            "volume": theme.fmt_num(volume, 0) if volume is not None else "—",
            "amount_wan": _wan(item.get("amount"), 2),
        }

    def _on_orderbook_error(self, exc: BaseException, code: str) -> None:
        text = "加载 %s 的盘口失败：%s" % (code, exc)
        for key, label in METRIC_CARDS:
            card = self.metric_cards.get(key)
            if card is not None:
                _card_set(card, "—", color_by="flat", sub=_short("加载失败：%s" % exc, 40))
        if self.level_title is not None:
            try:
                self.level_title.configure(text=text)
            except tk.TclError:
                pass
        self._fallback_line(text)
        self._block_error("五档", exc, self.level_table)

    def _render_ticks(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)][:TICK_LIMIT]
        rows: List[Dict[str, Any]] = []
        for item in items:
            rows.append({
                "time": str(item.get("time") or "—"),
                "price": theme.fmt_num(item.get("price"), 2),
                "volume": theme.fmt_num(item.get("volume"), 0),
                "amount_wan": _wan(item.get("amount"), 2),
                "side": _side_label(item.get("side")),
            })
        self._tick_rows = rows
        if self.tick_table is not None:
            self.tick_table.set_empty_text("%s：暂无逐笔成交数据" % self._empty_hint(meta))
            self.tick_table.set_rows(rows, key_field="time")
        stats = dict(data.get("stats") or {})
        if self.tick_title is not None:
            shown = data.get("shown") if data.get("shown") is not None else len(rows)
            parts = ["展示 %d / 共 %s 笔" % (len(rows), theme.fmt_num(
                data.get("count") if data.get("count") is not None else len(rows), 0)),
                "买 %s / 卖 %s（手）" % (theme.fmt_num(stats.get("buy_volume"), 0),
                                        theme.fmt_num(stats.get("sell_volume"), 0)),
                "净额 %s 万元" % _wan(stats.get("net_amount"), 2)]
            if data.get("note"):
                parts.append(str(data.get("note")))
            try:
                self.tick_title.configure(text=" · ".join(str(item) for item in parts))
            except tk.TclError:
                pass
        if not rows:
            self._fallback_line("逐笔：%s 无数据" % (data.get("code") or self._code))
        self._set_status("逐笔 %d 行（共 %s 笔）" % (len(rows), theme.fmt_num(shown, 0)))

    def _render_flow(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        buckets = dict(data.get("buckets") or {})
        sample = _as_float(data.get("amount_total")) or 0.0
        ticks = _as_float(data.get("tick_count")) or 0.0
        rows: List[Dict[str, Any]] = []
        if sample > 0 or ticks > 0:             # 没有逐笔样本时不留一排 0 假装有数据
            for key in BUCKET_ORDER:
                row = dict(buckets.get(key) or {})
                rows.append({
                    "label": row.get("label") or BUCKET_LABELS[key],
                    "buy_wan": _wan(row.get("buy"), 2),
                    "sell_wan": _wan(row.get("sell"), 2),
                    "net_wan": _wan_number(row.get("net")),
                    "buy_pct": theme.fmt_pct(row.get("buy_pct")),
                    "count": theme.fmt_num(row.get("count"), 0),
                })
        self._flow_rows = rows
        if self.flow_table is not None:
            self.flow_table.set_empty_text("%s：暂无资金流数据（需要逐笔样本）" % self._empty_hint(meta))
            self.flow_table.set_rows(rows, key_field="label")
        main_card = self.flow_cards.get("main")
        pct_card = self.flow_cards.get("main_pct")
        if not rows:
            for card in (main_card, pct_card):
                if card is not None:
                    _card_set(card, "—", color_by="flat",
                              sub=_short("无逐笔样本：资金流由逐笔成交额分档自算"))
        else:
            if main_card is not None:
                _card_set(main_card, _wan(data.get("main_net"), 2), color_by=data.get("main_net"),
                          sub=_short("超大单 + 大单净额 · 样本成交额 %s 万元"
                                     % _wan(data.get("amount_total"), 2)))
            if pct_card is not None:
                _card_set(pct_card, theme.fmt_pct(data.get("main_net_pct")),
                          color_by=data.get("main_net_pct"),
                          sub=_short("主力净额 / 样本成交额 · 逐笔 %s 笔"
                                     % theme.fmt_num(data.get("tick_count"), 0)))
        if not rows:
            self._fallback_line("资金流：%s 无逐笔样本" % (data.get("code") or self._code))
            self._set_status("资金流：%s 暂无可用的逐笔样本" % (data.get("code") or self._code))
            return
        self._set_status("资金流：主力净额 %s 万元（占比 %s）"
                         % (_wan(data.get("main_net"), 2),
                            theme.fmt_pct(data.get("main_net_pct"))))

    # ---- 渲染：大单追踪

    def _render_big_orders(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)][:BIG_ORDER_LIMIT]
        rows: List[Dict[str, Any]] = []
        for item in items:
            bucket = str(item.get("bucket") or "")
            rows.append({
                "time": str(item.get("time") or "—"),
                "price": theme.fmt_num(item.get("price"), 2),
                "volume": theme.fmt_num(item.get("volume"), 0),
                "amount_wan": _wan(item.get("amount"), 2),
                "side": _side_label(item.get("side")),
                "bucket": str(item.get("bucket_label") or BUCKET_LABELS.get(bucket, bucket) or "—"),
            })
        self.big_rows = rows
        summary = dict(data.get("summary") or {})
        threshold = _as_float(data.get("threshold"))
        if threshold is None:
            threshold = DEFAULT_BIG_THRESHOLD
        code = data.get("code") or self._code or "—"
        name = data.get("name") or ""
        sample = data.get("tick_sample")
        if self.big_table is not None:
            self.big_table.set_empty_text(
                "%s：%s 在 %s 以上没有成交（样本 %s 笔，可换更低阈值再查）"
                % (self._empty_hint(meta), code, _threshold_label(threshold),
                   theme.fmt_num(sample, 0)))
            self.big_table.set_rows(rows, key_field="time")
        self._set_note_text(self.big_note, data.get("note"), BIG_ORDER_NOTE)
        parts = ["%s %s" % (name, code) if name else code,
                 "阈值 %s" % _threshold_label(threshold),
                 "样本 %s 笔" % theme.fmt_num(sample, 0),
                 "命中 %s 笔 / 展示 %d 笔" % (theme.fmt_num(data.get("count"), 0), len(rows))]
        biggest = summary.get("biggest")
        if isinstance(biggest, dict):
            parts.append("最大单笔 %s 万元（%s %s）"
                         % (_wan(biggest.get("amount"), 2), str(biggest.get("time") or "—"),
                            _side_label(biggest.get("side"))))
        self._set_inline(self.big_title, " · ".join(parts))

        if not summary:
            for key, _label in BIG_ORDER_CARDS:
                card = self.big_cards.get(key)
                if card is not None:
                    _card_set(card, "—", color_by="flat",
                              sub=_short("%s：无大单统计" % self._empty_hint(meta)))
            self._fallback_line("大单追踪：%s 无数据（%s）" % (code, self._empty_hint(meta)))
            return
        buy_pct = _as_float(summary.get("buy_amount_pct"))
        sell_pct = None if buy_pct is None else max(0.0, 100.0 - buy_pct)
        values = {
            "count": (theme.fmt_num(summary.get("count"), 0), None,
                      "买 %s 笔 / 卖 %s 笔" % (theme.fmt_num(summary.get("buy_count"), 0),
                                               theme.fmt_num(summary.get("sell_count"), 0))),
            "buy": (_wan(summary.get("buy_amount"), 2), "up",
                    "占大单总额 %s" % theme.fmt_pct(buy_pct)),
            "sell": (_wan(summary.get("sell_amount"), 2), "down",
                     "占大单总额 %s" % theme.fmt_pct(sell_pct)),
            "net": (_wan(summary.get("net_amount"), 2), summary.get("net_amount"),
                    "占样本成交额 %s" % theme.fmt_pct(summary.get("amount_share_pct"))),
        }
        for key, (value, color_by, sub) in values.items():
            card = self.big_cards.get(key)
            if card is not None:
                _card_set(card, value, color_by=color_by, sub=_short(sub))
        if not rows:
            self._fallback_line("大单追踪：%s 无 ≥ %s 的成交" % (code, _threshold_label(threshold)))

    # ---- 渲染：资金流分时

    def _render_flow_series(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        series = [item for item in (data.get("series") or []) if isinstance(item, dict)]
        values: List[Any] = []
        labels: List[str] = []
        for item in series:
            number = _as_float(item.get("cum_net"))
            values.append(None if number is None else number / 10000.0)   # 元 → 万元
            labels.append(str(item.get("time") or ""))
        self.series_values = values
        self.series_labels = labels
        main_net = _as_float(data.get("main_net"))
        color = theme.COLORS["brand"]
        if main_net is not None and main_net != 0:
            color = theme.COLORS["up"] if main_net > 0 else theme.COLORS["down"]   # 红涨绿跌
        code = data.get("code") or self._code or "—"
        name = data.get("name") or ""
        minutes = data.get("minutes")
        sample = data.get("tick_sample")
        points = len([value for value in values if value is not None])
        if points >= 2:
            try:
                if self.flow_chart is not None:
                    self.flow_chart.set_data(values, dates=labels, color=color, fill=True,
                                             kind="money")
                _clear_chart_empty(self.flow_chart, self.series_chart_note)
            except Exception as exc:        # noqa: BLE001 - 图表失败只提示，不让页面崩
                _set_chart_empty(self.flow_chart, self.series_chart_note,
                                 "资金流分时渲染失败：%s" % _short(exc, 60))
                self._fallback_line("资金流分时渲染失败：%s" % exc)
        else:
            _set_chart_empty(self.flow_chart, self.series_chart_note,
                             "%s：分钟序列不足（需要 ≥ 2 分钟；样本 %s 笔）"
                             % (self._empty_hint(meta), theme.fmt_num(sample, 0)))
        parts = ["%s %s" % (name, code) if name else code,
                 "分钟 %s 个" % theme.fmt_num(minutes, 0),
                 "样本 %s 笔" % theme.fmt_num(sample, 0),
                 "主力净额 %s 万元（%s）" % (_wan(main_net, 2), theme.fmt_pct(data.get("main_net_pct"))),
                 "最新累计净额 %s 万元" % (_wan(values[-1] * 10000.0 if values[-1] is not None else None, 2)
                                          if values else "—")]
        self._set_inline(self.series_title, " · ".join(parts))
        self._set_note_text(self.series_note, data.get("note"), FLOW_SERIES_NOTE)

        has_sample = bool(series) or bool(_as_float(sample))
        if not has_sample:
            for card in list(self.series_cards.values()) + list(self.series_bucket_cards.values()):
                _card_set(card, "—", color_by="flat",
                          sub=_short("%s：无逐笔样本" % self._empty_hint(meta)))
            self._fallback_line("资金流分时：%s 无逐笔样本" % code)
            return
        values_by_key = {
            "main": (_wan(main_net, 2), main_net,
                     "超大单 + 大单净额 · 成交额 %s 万元" % _wan(data.get("amount_total"), 2)),
            "main_pct": (theme.fmt_pct(data.get("main_net_pct")), data.get("main_net_pct"),
                         "主力净额 / 样本成交额"),
            "minutes": (theme.fmt_num(minutes, 0), None,
                        "逐笔样本 %s 笔" % theme.fmt_num(sample, 0)),
        }
        for key, (value, color_by, sub) in values_by_key.items():
            card = self.series_cards.get(key)
            if card is not None:
                _card_set(card, value, color_by=color_by, sub=_short(sub))
        buckets = dict(data.get("buckets") or {})
        for key in BUCKET_ORDER:
            card = self.series_bucket_cards.get(key)
            if card is None:
                continue
            row = dict(buckets.get(key) or {})
            if not row:
                _card_set(card, "—", color_by="flat", sub=_short("无该档数据"))
                continue
            _card_set(card, _wan(row.get("net"), 2), color_by=row.get("net"),
                      sub=_short("买 %s 万 / 卖 %s 万"
                                 % (_wan(row.get("buy"), 2), _wan(row.get("sell"), 2))))

    def _on_series_error(self, exc: BaseException) -> None:
        """资金流分时失败：图表内文案 + 卡片置灰 + toast（与其它区块互不拖累）。"""
        text = "资金流分时加载失败：%s" % exc
        _set_chart_empty(self.flow_chart, self.series_chart_note, _short(text, 80))
        self._set_inline(self.series_title, text)
        for card in list(self.series_cards.values()) + list(self.series_bucket_cards.values()):
            _card_set(card, "—", color_by="flat", sub=_short("加载失败：%s" % exc, 40))
        self._fallback_line(text)
        self._set_status(text)
        self.toast_error(exc)

    # ---- 渲染：封板状态

    @staticmethod
    def _seal_values(seal: Any, price: Any = None, prev_close: Any = None) -> Dict[str, Any]:
        """封板字段 → 卡片的键值文案（缺失 / 数据不足一律「—」，不用 0 假装已知）。"""
        row = dict(seal or {})
        known = str(row.get("state") or "unknown") != "unknown"
        return {
            "现价": theme.fmt_num(price or None, 2),
            "昨收": theme.fmt_num(prev_close or None, 2),
            "涨停价": theme.fmt_num(row.get("limit_up_price"), 2) if known else "—",
            "跌停价": theme.fmt_num(row.get("limit_down_price"), 2) if known else "—",
            "距涨停 %": theme.fmt_pct(row.get("distance_pct")) if known else "—",
            "封单量（手）": theme.fmt_num(row.get("seal_volume"), 0) if known else "—",
            "封单额（万元）": _wan(row.get("seal_amount"), 2) if known else "—",
            "封成比 %": _pct_text(row.get("seal_ratio")) if known else "—",
        }

    def _seal_set(self, label: str, kind: str, title: str, values: Dict[str, Any]) -> None:
        if self.seal_badge is not None:
            try:
                self.seal_badge.set(label, kind=kind)
            except Exception:               # noqa: BLE001 - 徽标实现差异，不影响数据展示
                pass
        self._set_inline(self.seal_title, title)
        for table_ in (self.seal_kv_left, self.seal_kv_right):
            if table_ is None:
                continue
            for key, value in values.items():
                try:
                    table_.set_value(key, value)
                except Exception:           # noqa: BLE001
                    continue

    def _render_seal(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        seal = dict(data.get("seal") or {})
        state = str(seal.get("state") or "unknown")
        self.seal_state = state
        label = str(seal.get("label") or "数据不足")
        code = data.get("code") or self._code or "—"
        name = data.get("name") or ""
        self._seal_set(label, SEAL_BADGE_KINDS.get(state, "flat"),
                       "%s %s · %s" % (name, code, data.get("seal_text") or label) if name
                       else "%s · %s" % (code, data.get("seal_text") or label),
                       self._seal_values(seal, data.get("price"), data.get("prev_close")))
        self._set_note_text(self.seal_note, data.get("note"), SEAL_NOTE)
        if state == "unknown":
            self._fallback_line("封板状态：%s 数据不足" % code)
        elif state == "normal":
            self._fallback_line("封板状态：%s 未封板，距涨停 %s"
                                % (code, theme.fmt_pct(seal.get("distance_pct"))))
        else:
            self._fallback_line("封板状态：%s %s，封单 %s 万元（封成比 %s）"
                                % (code, label, _wan(seal.get("seal_amount"), 2),
                                   _pct_text(seal.get("seal_ratio"))))

    def _on_seal_error(self, exc: BaseException) -> None:
        text = "封板状态加载失败：%s" % exc
        self._seal_set("加载失败", "flat", text, self._seal_values({}))
        self._fallback_line(text)
        self._block_error("封板状态", exc, None)

    # ---- 渲染：扫描 / 排行

    def _codes_line(self, codes: List[str], notes: Any) -> str:
        text = "本次标的（%d 只）：%s" % (len(codes), _codes_text(codes))
        extra = [str(item) for item in (notes or []) if str(item).strip()]
        return "%s · %s" % (text, "；".join(extra)) if extra else text

    def _render_scan(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        codes = [str(item) for item in (data.get("codes") or []) if str(item).strip()]
        if codes:
            self._set_inline(self.tool_codes_label, self._codes_line(codes,
                                                                     data.get("resolve_notes")))
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)][:SCAN_LIMIT]
        rows: List[Dict[str, Any]] = []
        for item in _sort_desc(items, "imbalance_pct"):      # 按委比降序（服务端已排，这里保证稳定）
            bid = _as_float(item.get("bid_volume"))
            ask = _as_float(item.get("ask_volume"))
            diff = (bid - ask) if (bid is not None and ask is not None) else None
            rows.append({
                "code": str(item.get("code") or "—"),
                "name": str(item.get("name") or ""),
                "price": theme.fmt_num(item.get("price"), 2),
                "change_pct": item.get("change_pct"),        # 唯一着色列：红涨绿跌
                "imbalance_pct": theme.fmt_pct(item.get("imbalance_pct")),
                "diff": theme.fmt_num(diff, 0) if diff is not None else "—",
                "volume_ratio": theme.fmt_num(item.get("volume_ratio"), 2),
                "seal": str(item.get("seal_label") or "—"),
                "distance_pct": theme.fmt_pct(item.get("distance_pct")),
            })
        self.scan_rows = rows
        if self.scan_table is not None:
            self.scan_table.set_empty_text("%s：扫描无结果（%s）"
                                           % (self._empty_hint(meta), _codes_text(codes)))
            self.scan_table.set_rows(rows, key_field="code")
        self._set_inline(self.scan_failures, _failures_text(data.get("failures")))
        self._set_note_text(self.scan_note, data.get("note"), SCAN_NOTE)
        self._set_inline(self.scan_title,
                         "扫描：请求 %s 只 · 成功 %d 只 · 失败 %d 只 · 按委比降序"
                         % (theme.fmt_num(data.get("requested"), 0), len(rows),
                            len(list(data.get("failures") or []))))
        if not rows:
            self._fallback_line("扫描自选池：%s 无结果" % _codes_text(codes))

    def _render_rank(self, payload: Any) -> None:
        data = dict(payload or {})
        meta = dict(data.get("meta") or {})
        self._render_meta(meta)
        codes = [str(item) for item in (data.get("codes") or []) if str(item).strip()]
        if codes:
            self._set_inline(self.tool_codes_label, self._codes_line(codes,
                                                                     data.get("resolve_notes")))
        items = [item for item in (data.get("items") or []) if isinstance(item, dict)][:RANK_TOP]
        rows: List[Dict[str, Any]] = []
        for item in _sort_desc(items, "main_net"):           # 按主力净额降序
            rows.append({
                "code": str(item.get("code") or "—"),
                "name": str(item.get("name") or ""),
                "main_wan": _wan_number(item.get("main_net")),   # 唯一着色列：红涨绿跌
                "main_pct": theme.fmt_pct(item.get("main_net_pct")),
                "tick_count": theme.fmt_num(item.get("tick_count"), 0),
            })
        self.rank_rows = rows
        failures = list(data.get("failures") or [])
        if self.rank_table is not None:
            self.rank_table.set_empty_text("%s：排行无结果（%s）"
                                           % (self._empty_hint(meta), _codes_text(codes)))
            self.rank_table.set_rows(rows, key_field="code")
        self._set_inline(self.rank_failures, _failures_text(failures))
        self._set_note_text(self.rank_note, data.get("note"), RANK_NOTE)
        self._set_inline(self.rank_title,
                         "资金流排行：请求 %s 只 · 成功 %d 只 · 失败 %d 只 · 按主力净额降序"
                         % (theme.fmt_num(data.get("requested"), 0), len(rows), len(failures)))
        if rows:
            self._set_inline(self.rank_status,
                             "已完成：成功 %d / 请求 %s 只 · 主力净额最高 %s %s 万元 · 用时视标的多寡"
                             % (len(rows), theme.fmt_num(data.get("requested"), 0),
                                rows[0]["code"], theme.fmt_num(rows[0]["main_wan"], 2)))
        else:
            self._set_inline(self.rank_status,
                             "资金流排行：%s 无可用结果（%s）"
                             % (_codes_text(codes), self._empty_hint(meta)))
            self._fallback_line("资金流排行：%s 无结果" % _codes_text(codes))

    # ---- 渲染：提示与空状态

    def _render_meta(self, meta: Dict[str, Any]) -> None:
        info = dict(meta or {})
        parts: List[str] = []
        if info.get("offline"):
            parts.append("当前为离线/演示数据（来源：%s），不可用于交易决策"
                         % (info.get("source") or "本地"))
        if info.get("stale"):
            parts.append("数据为缓存快照")
        notes = [str(item) for item in (info.get("notes") or []) if str(item).strip()]
        if notes:
            parts.append("；".join(notes))
        self._set_notice("⚠ " + " · ".join(parts) if parts else "")

    def _empty_hint(self, meta: Dict[str, Any]) -> str:
        """空状态前缀：离线/缓存时把数据来源状态一并说明，避免「空」被误读成「没有成交」。"""
        if meta.get("offline"):
            return "离线/演示数据"
        if meta.get("stale"):
            return "缓存快照"
        return "无数据"

    def _set_notice(self, text: str) -> None:
        if self.notice_label is None:
            return
        try:
            if text:
                if not self.notice_label.winfo_manager():
                    try:
                        self.notice_label.pack(fill="x", pady=(0, 8), before=self._first_widget)
                    except tk.TclError:
                        self.notice_label.pack(fill="x", pady=(0, 8))
                self.notice_label.configure(text=text)
            else:
                self.notice_label.configure(text="")
                self.notice_label.pack_forget()
        except tk.TclError:
            return

    def _set_status(self, text: str) -> None:
        if self.status_label is None:
            return
        try:
            self.status_label.configure(text=text)
        except tk.TclError:
            return

    def _show_idle(self) -> None:
        """未查询 / 代码为空时的页内引导（不是错误，也不弹窗）。"""
        hint = "输入标的代码（例如 600519 / sh600519 / 600519.SH）后点「查询」"
        for key, _label in METRIC_CARDS:
            card = self.metric_cards.get(key)
            if card is not None:
                _card_set(card, "—", color_by="flat", sub=hint)
        for key, _label in (("main", "主力净额（万元）"), ("main_pct", "主力净额占比")):
            card = self.flow_cards.get(key)
            if card is not None:
                _card_set(card, "—", color_by="flat", sub=hint)
        if self.level_table is not None:
            self.level_table.set_empty_text(hint)
            self.level_table.clear()
        if self.tick_table is not None:
            self.tick_table.set_empty_text("逐笔成交：%s" % hint)
            self.tick_table.clear()
        if self.flow_table is not None:
            self.flow_table.set_empty_text("资金流：%s" % hint)
            self.flow_table.clear()
        if self.level_title is not None:
            try:
                self.level_title.configure(text=hint)
            except tk.TclError:
                pass
        if self.tick_title is not None:
            try:
                self.tick_title.configure(text="")
            except tk.TclError:
                pass
        self._set_notice("")
        self._set_status(hint)
        if self.flow_note is not None:
            try:
                self.flow_note.configure(text=FLOW_NOTE)
            except tk.TclError:
                pass
        # 重新开始查询前先清空表格空状态文案，避免上一次的失败原因残留
        self._orderbook_rows, self._tick_rows, self._flow_rows = [], [], []
        # ---- 4 个工具区块：回到「未查询」引导态（只改控件，绝不发请求）
        for table_, text in ((self.big_table, "大单追踪：%s" % hint),
                             (self.scan_table, "扫描自选池：%s" % hint),
                             (self.rank_table, "资金流排行：%s" % hint)):
            if table_ is not None:
                table_.set_empty_text(text)
                table_.clear()
        for key, _label in BIG_ORDER_CARDS:
            card = self.big_cards.get(key)
            if card is not None:
                _card_set(card, "—", color_by="flat", sub=hint)
        for card in list(self.series_cards.values()) + list(self.series_bucket_cards.values()):
            _card_set(card, "—", color_by="flat", sub=hint)
        _set_chart_empty(self.flow_chart, self.series_chart_note, "资金流分时：%s" % hint)
        self._seal_set("未查询", "flat", "封板状态：%s" % hint, self._seal_values({}))
        self._set_inline(self.big_title, hint)
        self._set_inline(self.series_title, "")
        self._set_inline(self.scan_title, "")
        self._set_inline(self.rank_title, "")
        self._set_inline(self.scan_failures, "")
        self._set_inline(self.rank_failures, "")
        self._set_inline(self.tool_codes_label, TOOL_CODES_NOTE)
        self._set_inline(self.rank_status, RANK_IDLE_TEXT)
        self._set_note_text(self.big_note, None, BIG_ORDER_NOTE)
        self._set_note_text(self.series_note, None, FLOW_SERIES_NOTE)
        self._set_note_text(self.seal_note, None, SEAL_NOTE)
        self._set_note_text(self.scan_note, None, SCAN_NOTE)
        self._set_note_text(self.rank_note, None, RANK_NOTE)
        self.big_rows, self.scan_rows, self.rank_rows = [], [], []
        self.series_values, self.series_labels = [], []
        self.seal_state = "unknown"

    def _on_block_error(self, name: str, exc: BaseException, widget: Any) -> None:
        text = "%s加载失败：%s" % (name, exc)
        self._fallback_line(text)
        self._block_error(name, exc, widget)

    def _block_error(self, name: str, exc: BaseException, widget: Any) -> None:
        """把一个区块画成内联失败状态（表格中间灰字 + 状态栏 + toast，不弹窗）。"""
        text = "%s加载失败：%s" % (name, exc)
        if widget is not None:
            try:
                widget.set_empty_text(text)
                widget.clear()
            except Exception:               # noqa: BLE001 - 控件实现差异，不影响页面存活
                pass
        self._set_status(text)
        self.toast_error(exc)

    # ------------------------------------------------------------------ 自动刷新

    def _auto_on(self) -> bool:
        try:
            return str(self.auto_var.get()) == "1"
        except tk.TclError:
            return False

    def _on_auto_toggle(self) -> None:
        if self._auto_on():
            self._schedule_auto()
            self.status("自动刷新已开启（每 %.0f 秒）" % (AUTO_REFRESH_MS / 1000.0))
        else:
            self._cancel_auto()
            self.status("自动刷新已关闭")

    def _schedule_auto(self) -> None:
        self._cancel_auto()
        if not self._auto_on():
            return
        try:
            self._auto_job = self.after(AUTO_REFRESH_MS, self._auto_tick)
        except tk.TclError:
            self._auto_job = None

    def _cancel_auto(self) -> None:
        job, self._auto_job = self._auto_job, None
        if job is None:
            return
        try:
            self.after_cancel(job)
        except (tk.TclError, ValueError):
            return

    def _auto_tick(self) -> None:
        """定时回调：开关关闭或页面不在前台时**不发请求**，只保持定时器。

        **只调 ``reload()``（= 盘口 / 逐笔 / 资金流分档）**，绝不触发下面 4 个工具：
        扫描 ≈1 秒/只、排行 ≈10–25 秒/只，3 秒一次会把数据源打爆（工具只能手动点按钮）。
        """
        self._auto_job = None
        if not self._auto_on():
            return
        try:
            visible = bool(self.winfo_manager())
        except tk.TclError:
            return
        if visible:
            self.reload()
        self._schedule_auto()

    def _on_destroy(self, event: Any = None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._cancel_auto()

    # ------------------------------------------------------------------ 辅助

    def _current_code(self) -> str:
        var = getattr(self, "code_var", None)
        try:
            return (var.get() if var is not None else "") or ""
        except tk.TclError:
            return ""

    @staticmethod
    def _normalize(value: Any) -> str:
        """``600519`` / ``sh600519`` / ``600519.SH`` → ``600519.SH``；已有后缀形式原样通过。"""
        text = str(value if value is not None else "").strip()
        if not text:
            return ""
        normalized = normalize_code_text(text)
        if normalized:
            return normalized
        upper = text.upper()
        return upper if _CODE_SUFFIX.match(upper) else ""

    def _fallback_line(self, text: str) -> None:
        """控件库缺失时的文本摘要（正常模式下什么都不做）。"""
        if self.fallback is None:
            return
        try:
            self.fallback.configure(state="normal")
            self.fallback.insert("end", "%s\n" % text)
            self.fallback.see("end")
            self.fallback.configure(state="disabled")
        except tk.TclError:
            return


__all__ = ["Level2View", "DISCLAIMER", "LEVEL_COLUMNS", "TICK_COLUMNS", "FLOW_COLUMNS",
           "SLOT_COLORS", "METRIC_CARDS", "BUCKET_ORDER", "TICK_LIMIT", "AUTO_REFRESH_MS",
           "BIG_ORDER_COLUMNS", "SCAN_COLUMNS", "RANK_COLUMNS", "BIG_ORDER_THRESHOLDS",
           "DEFAULT_BIG_THRESHOLD", "BIG_ORDER_LIMIT", "BIG_ORDER_NOTE", "SEAL_NOTE",
           "TOOL_CODES_MAX", "AUTO_REFRESH_SCOPE", "TOOL_LATENCY_HINT", "RANK_RUNNING_TEXT"]
