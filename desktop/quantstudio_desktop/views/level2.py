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
* 三个服务调用（``level2.orderbook`` / ``level2.ticks`` / ``level2.capital_flow``）全部走
  ``self.tasks.run(...)``，主线程不被网络阻塞；
* 失败 / 离线 / 无数据一律画**页内内联空状态**（表格中间灰字 + 顶部提示条），不弹模态框。
"""

import re
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView
from .market import ScrollArea, normalize_code_text      # 复用代码规范化与滚动容器，避免行为分叉

try:                                    # 控件库由并行子任务提供；未交付时降级运行
    from ..widgets import cards, table

    WIDGETS_ERROR = ""
except Exception as exc:                # noqa: BLE001 - 任何导入问题都只影响观感，不影响页面存活
    cards = table = None                            # type: ignore[assignment]
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

    # ------------------------------------------------------------------ 构建

    def build(self) -> None:
        self.code_var = tk.StringVar(value="")
        self.auto_var = tk.StringVar(value="0")             # 「自动刷新（3 秒）」默认关

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
        """定时回调：开关关闭或页面不在前台时**不发请求**，只保持定时器。"""
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
           "SLOT_COLORS", "METRIC_CARDS", "BUCKET_ORDER", "TICK_LIMIT", "AUTO_REFRESH_MS"]
