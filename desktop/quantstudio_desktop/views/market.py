# -*- coding: utf-8 -*-
"""行情看板：指数卡片 / 市场概况 / 板块涨幅榜 / 自选股 / 个股 K 线。

约定（与 ``views/__init__.BaseView`` 一致）：

* ``build()`` 只建控件（首次显示时调用一次）；
* ``reload()`` 只发数据请求并更新已有控件的文本/表格/图表，不重建控件树，
  因此刷新后滚动位置与表格选中行都能保留；
* 所有服务调用都通过 ``self.load(...)`` 进入后台线程，主线程不会被网络阻塞；
* 控件库（``widgets.charts/table/cards/forms``）缺失或签名不符时，页面降级为
  「内联错误 + 文本摘要」，既不抛异常也不弹窗。
"""

import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView

try:                                    # 控件库由并行子任务提供；未交付时降级运行
    from ..widgets import cards, charts, forms, table

    WIDGETS_ERROR = ""
except Exception as exc:                # noqa: BLE001 - 任何导入问题都只影响观感，不影响页面存活
    cards = charts = forms = table = None           # type: ignore[assignment]
    WIDGETS_ERROR = "控件库不可用：%s" % exc

# --------------------------------------------------------------------------- 常量

INDEX_DEFAULTS: Tuple[Tuple[str, str], ...] = (      # 卡片占位标题（settings.index_symbols 的默认四项）
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("科创50", "000688.SH"),
)
BREADTH_CARDS: Tuple[Tuple[str, str], ...] = (
    ("up", "上涨家数"),
    ("down", "下跌家数"),
    ("flat", "平盘家数"),
    ("amount", "两市成交额"),
    ("inflow", "主力净流入"),
)
FREQ_CHOICES: Tuple[Tuple[str, str], ...] = (("日线", "day"), ("周线", "week"), ("月线", "month"))
ADJUST_CHOICES: Tuple[Tuple[str, str], ...] = (("前复权", "qfq"), ("后复权", "hfq"), ("不复权", "none"))
DAYS_CHOICES: Tuple[str, ...] = ("60", "120", "250", "500")
SECTOR_LIMIT = 12

WATCH_COLUMNS: List[Dict[str, Any]] = [
    {"key": "code", "label": "代码", "width": 96, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 104, "anchor": "w", "kind": "text"},
    {"key": "price", "label": "现价", "width": 82, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "change_pct", "label": "涨跌幅", "width": 84, "anchor": "e", "kind": "pct", "digits": 2},
    {"key": "amount_yi", "label": "成交额(亿)", "width": 96, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "turnover_pct", "label": "换手率", "width": 82, "anchor": "e", "kind": "pct", "digits": 2},
    {"key": "pe_ttm", "label": "PE(TTM)", "width": 84, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "pb", "label": "PB", "width": 72, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "market_cap_yi", "label": "总市值(亿)", "width": 100, "anchor": "e", "kind": "num", "digits": 2},
]

_CODE_SUFFIX = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_CODE_PREFIX = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_CODE_PLAIN = re.compile(r"^\d{6}$")


# --------------------------------------------------------------------------- 工具函数


def normalize_code_text(value: Any) -> str:
    """``600519`` / ``sh600519`` / ``600519.SH`` → ``600519.SH``；无法识别返回空串。"""
    text = str(value if value is not None else "").strip().upper().replace(" ", "")
    match = _CODE_SUFFIX.match(text)
    if match:
        return "%s.%s" % (match.group(1), match.group(2))
    match = _CODE_PREFIX.match(text)
    if match:
        return "%s.%s" % (match.group(2), match.group(1))
    if _CODE_PLAIN.match(text):
        if text[0] in ("0", "3"):
            market = "SZ"
        elif text[0] in ("4", "8"):
            market = "BJ"
        else:
            market = "SH"
        return "%s.%s" % (text, market)
    return ""


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


def _set_card_title(card: Any, text: str) -> None:
    """卡片标题的「尽力而为」更新：契约没给标题接口，能找到标题 Label 就更新。"""
    for name in ("title_label", "_title_label", "title_lbl", "_title"):
        label = getattr(card, name, None)
        if label is not None and hasattr(label, "configure"):
            try:
                label.configure(text=text)
                return
            except Exception:             # noqa: BLE001
                continue


def _money_yi(value: Any, digits: int = 2) -> str:
    """亿元金额 → ``1,234.56 亿``（None / 空 → 「—」）。"""
    if value is None or value == "":
        return "—"
    return "%s 亿" % theme.fmt_money(value, digits)


def _count(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return "%s 家" % theme.fmt_num(value, 0)


def _show_chart_note(label: Optional[tk.Label], text: str) -> None:
    """在图表上叠加一行提示（空数据/失败状态，不弹窗）。"""
    if label is None:
        return
    try:
        label.configure(text=text)
        label.place(relx=0.5, rely=0.5, anchor="center")
    except tk.TclError:
        pass


def _hide_chart_note(label: Optional[tk.Label]) -> None:
    if label is None:
        return
    try:
        label.place_forget()
    except tk.TclError:
        pass


def _set_chart_empty(chart: Any, label: Optional[tk.Label], text: str) -> None:
    """把空状态/错误写到图表上：优先用控件内置空状态，其次退化为叠加标签（都不弹窗）。"""
    if chart is not None and hasattr(chart, "set_empty"):
        try:
            chart.set_empty(text)
            return
        except Exception:                     # noqa: BLE001 - 控件实现差异，退化到叠加标签
            pass
    _show_chart_note(label, text)


def _clear_chart_empty(chart: Any, label: Optional[tk.Label]) -> None:
    """数据恢复正常后清掉空状态。"""
    if chart is not None and hasattr(chart, "set_empty"):
        try:
            chart.set_empty("暂无数据")
        except Exception:                     # noqa: BLE001
            pass
    _hide_chart_note(label)


# --------------------------------------------------------------------------- 滚动容器


class ScrollArea(ttk.Frame):
    """可滚动容器：``tk.Canvas`` + 内层 Frame + 垂直滚动条（Tk 8.5 兼容，无 8.6 专有项）。"""

    def __init__(self, parent: Any, bg: Optional[str] = None):
        super().__init__(parent, style="TFrame")
        self.canvas = tk.Canvas(self, bg=bg or theme.COLORS["bg"], highlightthickness=0, bd=0,
                                takefocus=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = ttk.Frame(self.canvas, style="TFrame")
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.body.bind("<Configure>", self._sync_region)       # 内容变化 → 更新 scrollregion
        self.canvas.bind("<Configure>", self._sync_width)      # 容器变化 → 内层跟随宽度
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    # ---- 内部
    def _sync_region(self, _event: Any = None) -> None:
        try:
            box = self.canvas.bbox("all")
        except tk.TclError:
            return
        self.canvas.configure(scrollregion=box or (0, 0, 0, 0))

    def _sync_width(self, event: Any) -> None:
        try:
            self.canvas.itemconfigure(self.window, width=event.width)
        except tk.TclError:
            return
        self._sync_region()

    def _bind_wheel(self, _event: Any = None) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.canvas.bind_all(sequence, self._on_wheel)
            except tk.TclError:
                continue

    def _unbind_wheel(self, _event: Any = None) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.canvas.unbind_all(sequence)
            except tk.TclError:
                continue

    def _on_wheel(self, event: Any) -> None:
        number = getattr(event, "num", 0)
        if number == 4:
            step = -1
        elif number == 5:
            step = 1
        else:
            step = -1 if getattr(event, "delta", 0) > 0 else 1
        try:
            self.canvas.yview_scroll(step, "units")
        except tk.TclError:
            return

    def scroll_to_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except tk.TclError:
            return


# --------------------------------------------------------------------------- 页面


class MarketView(BaseView):
    """行情看板页面。"""

    title = "行情看板"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._pending = 0                       # 未完成的请求数（测试与状态栏用）
        self._widget_error = WIDGETS_ERROR
        self.build_errors: List[str] = []
        self.fallback: Optional[tk.Text] = None
        self._first_widget: Optional[Any] = None
        self._indices: List[Dict[str, Any]] = []
        self._sectors: List[Dict[str, Any]] = []
        self._quotes: List[Dict[str, Any]] = []
        self._names: Dict[str, str] = {}
        self._kline_bars: List[Dict[str, Any]] = []
        # 控件引用（构建失败时为 None，渲染前一律判空）
        self.index_cards: List[Any] = []
        self.breadth_cards: Dict[str, Any] = {}
        self.sector_chart = None
        self.quote_table = None
        self.candle_chart = None
        self.watch_hint = None
        self.kline_note = None

    # ------------------------------------------------------------------ 构建

    def build(self) -> None:
        self.code_var = tk.StringVar(value="")
        self.freq_var = tk.StringVar(value=FREQ_CHOICES[0][0])
        self.adjust_var = tk.StringVar(value=ADJUST_CHOICES[0][0])
        self.days_var = tk.StringVar(value="250")

        self.scroll = ScrollArea(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body

        # 数据源提示条（默认不显示，offline/stale 时 pack 到最上方）
        self.notice_label = tk.Label(body, text="", bg=theme.COLORS["warn"], fg=theme.COLORS["bg"],
                                     font=theme.font(10, "bold"), anchor="w", justify="left",
                                     padx=10, pady=6)

        if self._widget_error:
            self._build_widget_error(body, self._widget_error)
            self._build_fallback_text(body)

        self._section(body, "主要指数", self._build_indexes)
        self._section(body, "市场概况", self._build_breadth)
        self._section(body, "板块涨幅榜", self._build_sectors)
        self._section(body, "自选股", self._build_watchlist)
        self._section(body, "个股K线", self._build_kline)

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
        self.fallback = tk.Text(body, height=16, bg=theme.COLORS["panel"], fg=theme.COLORS["text"],
                                relief="flat", font=theme.font(10, mono=True), wrap="word",
                                padx=10, pady=8)
        self.fallback.pack(fill="both", expand=True)
        self.fallback.configure(state="disabled")

    # ---- 各区块

    def _build_indexes(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "主要指数", hint="点位 / 涨跌幅 / 成交额（亿元）")
        title.pack(fill="x", pady=(0, 4))
        grid = cards.CardGrid(body, columns=4, gap=8)
        grid.pack(fill="x", pady=(0, 12))
        self.index_grid = grid
        for name, code in INDEX_DEFAULTS:
            card = cards.StatCard(grid, "%s · %s" % (name, code), value="—", sub="等待数据…")
            grid.add(card)
            self.index_cards.append(card)
        return title

    def _build_breadth(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "市场概况", hint="涨跌家数 / 两市成交额 / 资金净流入")
        title.pack(fill="x", pady=(0, 4))
        grid = cards.CardGrid(body, columns=5, gap=8)
        grid.pack(fill="x", pady=(0, 12))
        self.breadth_grid = grid
        for key, label in BREADTH_CARDS:
            card = cards.StatCard(grid, label, value="—", sub="")
            grid.add(card)
            self.breadth_cards[key] = card
        return title

    def _build_sectors(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "板块涨幅榜", hint="按涨幅降序，取前 %d 个" % SECTOR_LIMIT)
        title.pack(fill="x", pady=(0, 4))
        self.sector_chart = charts.BarChart(body, height=240)
        self.sector_chart.pack(fill="x", pady=(0, 12))
        return title

    def _build_watchlist(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "自选股", hint="选中行即加载 K 线，双击同样触发")
        title.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        ttk.Button(bar, text="添加标的", style="Primary.TButton",
                   command=self._add_watchlist).pack(side="left")
        ttk.Button(bar, text="删除选中", style="Danger.TButton",
                   command=self._remove_watchlist).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left")
        self.watch_hint = ttk.Label(bar, text="", style="Muted.TLabel")
        self.watch_hint.pack(side="left", padx=(10, 0))

        self.quote_table = table.DataTable(body, WATCH_COLUMNS, height=8,
                                           on_select=self._on_watch_select,
                                           on_double_click=self._on_watch_select, sortable=True)
        self.quote_table.pack(fill="x", pady=(4, 12))
        return title

    def _build_kline(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "个股 K 线", hint="数据来自 kline(code, days, freq, adjust)")
        title.pack(fill="x", pady=(0, 4))

        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="标的", style="Muted.TLabel").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.code_var, width=14)
        entry.pack(side="left", padx=(4, 12))
        entry.bind("<Return>", lambda _event: self._load_kline())
        self.code_entry = entry

        ttk.Label(bar, text="周期", style="Muted.TLabel").pack(side="left")
        self.freq_box = ttk.Combobox(bar, state="readonly", width=6, textvariable=self.freq_var,
                                     values=[label for label, _value in FREQ_CHOICES])
        self.freq_box.pack(side="left", padx=(4, 12))
        self.freq_box.bind("<<ComboboxSelected>>", lambda _event: self._load_kline())

        ttk.Label(bar, text="复权", style="Muted.TLabel").pack(side="left")
        self.adjust_box = ttk.Combobox(bar, state="readonly", width=8, textvariable=self.adjust_var,
                                       values=[label for label, _value in ADJUST_CHOICES])
        self.adjust_box.pack(side="left", padx=(4, 12))
        self.adjust_box.bind("<<ComboboxSelected>>", lambda _event: self._load_kline())

        ttk.Label(bar, text="天数", style="Muted.TLabel").pack(side="left")
        self.days_box = ttk.Combobox(bar, state="readonly", width=6, textvariable=self.days_var,
                                     values=list(DAYS_CHOICES))
        self.days_box.pack(side="left", padx=(4, 12))
        self.days_box.bind("<<ComboboxSelected>>", lambda _event: self._load_kline())

        ttk.Button(bar, text="刷新K线", style="Primary.TButton",
                   command=self._load_kline).pack(side="left")

        self.kline_title = ttk.Label(body, text="", style="Muted.TLabel")
        self.kline_title.pack(fill="x", pady=(8, 0))
        self.kline_meta = ttk.Label(body, text="", style="Muted.TLabel")
        self.kline_meta.pack(fill="x", pady=(2, 4))

        self.candle_chart = charts.CandleChart(body, height=320)
        self.candle_chart.pack(fill="x", pady=(0, 12))
        self.kline_note = tk.Label(self.candle_chart, text="", bg=theme.COLORS["panel"],
                                   fg=theme.COLORS["text2"], font=theme.font(10), justify="center")
        return title

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

    def reload(self) -> None:
        self.status("正在加载行情数据…")
        self._clear_fallback()
        self._request(self.services.provider_meta, on_done=self._render_provider,
                      on_error=self._on_provider_error)
        self._request(self.services.overview, on_done=self._render_overview)
        self._request(self.services.sectors, limit=SECTOR_LIMIT, on_done=self._render_sectors)
        self._request(self.services.quotes, on_done=self._render_quotes)
        if self.candle_chart is not None and self._current_code():
            self._load_kline()

    # ---- 渲染：数据源提示

    def _render_provider(self, meta: Any) -> None:
        info = dict(meta or {})
        source = info.get("source") or "未知来源"
        parts: List[str] = []
        if info.get("offline"):
            parts.append("当前为离线/演示数据，不可用于交易决策（来源：%s）" % source)
        if info.get("stale"):
            parts.append("数据为缓存快照")
        self._set_notice("⚠ " + " · ".join(parts) if parts else "")
        as_of = info.get("as_of") or ""
        self.status("数据源：%s%s" % (source, (" · 时点 %s" % as_of) if as_of else ""))
        notes = info.get("notes") or []
        if notes:
            self._fallback_line("数据源说明：%s" % "；".join(str(item) for item in notes))

    def _on_provider_error(self, exc: BaseException) -> None:
        self._set_notice("⚠ 数据源状态读取失败：%s" % exc)
        self.toast_error(exc)

    def _set_notice(self, text: str) -> None:
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

    # ---- 渲染：指数 + 市场概况

    def _render_overview(self, overview: Any) -> None:
        data = dict(overview or {})
        self._render_indices(list(data.get("indices") or []))
        self._render_breadth(data)

    def _render_indices(self, indices: List[Dict[str, Any]]) -> None:
        self._indices = [item for item in indices if isinstance(item, dict)]
        if not self.index_cards:
            self._fallback_line("指数：%s" % "、".join(
                "%s %s(%s)" % (item.get("name") or "", theme.fmt_num(item.get("point"), 2),
                               theme.fmt_pct(item.get("change_pct"))) for item in self._indices) or "无数据")
            return
        for position, card in enumerate(self.index_cards):
            if position < len(self._indices):
                item = self._indices[position]
                name = item.get("name") or item.get("code") or "指数"
                code = item.get("code") or ""
                _set_card_title(card, "%s · %s" % (name, code) if code else name)
                pct = item.get("change_pct")
                sub = "涨跌幅 %s · 成交额 %s" % (theme.fmt_pct(pct), _money_yi(item.get("amount_yi"), 0))
                _card_set(card, theme.fmt_num(item.get("point"), 2), color_by=pct, sub=sub)
            else:
                name, code = INDEX_DEFAULTS[position] if position < len(INDEX_DEFAULTS) else ("指数", "")
                _set_card_title(card, "%s · %s" % (name, code) if code else name)
                _card_set(card, "—", color_by=None, sub="暂无数据")

    def _render_breadth(self, overview: Dict[str, Any]) -> None:
        breadth = dict(overview.get("breadth") or {})
        up = breadth.get("up")
        down = breadth.get("down")
        flat = breadth.get("flat")
        total_yi = overview.get("total_amount_yi")
        if total_yi in (None, ""):
            total_yi = breadth.get("total_amount_yi")
        main_in = overview.get("main_net_inflow_yi")
        if main_in is None:
            main_in = breadth.get("main_net_inflow_yi")
        north_in = overview.get("north_net_inflow_yi")
        if north_in is None:
            north_in = breadth.get("north_net_inflow_yi")

        amount_value = "—"
        amount_sub = "合计 —"
        if total_yi not in (None, ""):
            try:
                amount_value = "%s 万亿" % theme.fmt_num(float(total_yi) / 10000.0, 2)
                amount_sub = "合计 %s 亿" % theme.fmt_money(total_yi, 1)
            except (TypeError, ValueError):
                amount_value, amount_sub = "—", "合计 —"

        items: Dict[str, Tuple[str, Any, str]] = {
            "up": (_count(up), 1 if (up or 0) else 0,
                   "涨停 %s · 占比 %s" % (theme.fmt_num(breadth.get("limit_up"), 0),
                                          theme.fmt_pct(_ratio(up, breadth.get("total")), 1))),
            "down": (_count(down), -1 if (down or 0) else 0,
                     "跌停 %s · 占比 %s" % (theme.fmt_num(breadth.get("limit_down"), 0),
                                            theme.fmt_pct(_ratio(down, breadth.get("total")), 1))),
            "flat": (_count(flat), 0, "平盘家数"),
            "amount": (amount_value, None, amount_sub),
            "inflow": (_money_yi(main_in), main_in, "北向净流入：%s" % _money_yi(north_in)),
        }
        if not self.breadth_cards:
            self._fallback_line("市场概况：上涨 %s / 下跌 %s / 平盘 %s · 成交额 %s"
                                " · 主力净流入 %s · 北向 %s"
                                % (_count(up), _count(down), _count(flat), amount_value,
                                   _money_yi(main_in), _money_yi(north_in)))
            return
        for key, (value, color_by, sub) in items.items():
            card = self.breadth_cards.get(key)
            if card is not None:
                _card_set(card, value, color_by=color_by, sub=sub)

    # ---- 渲染：板块

    def _render_sectors(self, rows: Any) -> None:
        self._sectors = [item for item in (rows or []) if isinstance(item, dict)][:SECTOR_LIMIT]
        if self.sector_chart is None:
            self._fallback_line("板块涨幅榜：%s" % "、".join(
                "%s %s" % (item.get("name") or item.get("code") or "-",
                           theme.fmt_pct(item.get("change_pct"))) for item in self._sectors) or "无数据")
            return
        labels = [str(item.get("name") or item.get("code") or "-") for item in self._sectors]
        values = [item.get("change_pct") or 0.0 for item in self._sectors]
        # 服务层已按涨幅降序返回，这里保持原顺序传给图表
        self.sector_chart.set_data(labels, values, horizontal=True, kind="pct")

    # ---- 渲染：自选股

    def _render_quotes(self, rows: Any) -> None:
        self._quotes = [item for item in (rows or []) if isinstance(item, dict)]
        for item in self._quotes:
            code = str(item.get("code") or "")
            if code:
                self._names[code] = str(item.get("name") or "")
        if self.quote_table is None:
            self._fallback_line("自选股（%d 只）：%s" % (len(self._quotes), "、".join(
                "%s %s %s" % (item.get("code"), item.get("name") or "",
                              theme.fmt_num(item.get("price"), 2)) for item in self._quotes) or "无"))
        else:
            self.quote_table.set_rows(self._quotes, key_field="code", preserve_selection=True)
        if self.watch_hint is not None:
            if self._quotes:
                self.watch_hint.configure(text="共 %d 只 · 选中行加载 K 线" % len(self._quotes))
            else:
                self.watch_hint.configure(
                    text="自选股为空：点击『添加标的』录入（支持 600519 / sh600519 / 600519.SH）")
        # 未选标的时默认跟随第一条自选股
        if self.candle_chart is not None and not self._current_code() and self._quotes:
            first = str(self._quotes[0].get("code") or "")
            if first:
                self._set_code(first, load=True)

    def _add_watchlist(self) -> None:
        if forms is None:
            self._notify("表单控件不可用，无法添加标的", kind="error")
            return
        payload = self._show_form("添加自选标的", [{
            "name": "code", "label": "标的代码", "type": "text", "default": "", "required": True,
            "help": "支持 600519 / sh600519 / 600519.SH 三种写法",
        }])
        if not payload:
            return
        code = normalize_code_text(payload.get("code"))
        if not code:
            self._notify("标的代码格式非法：%s" % (payload.get("code") or ""), kind="error")
            return
        self._request(self.services.add_watchlist, code, on_done=self._after_watch_change)

    def _remove_watchlist(self) -> None:
        code = self._code_of(self._selected_payload())
        if not code:
            self._notify("请先在自选股表格中选中一行", kind="warn")
            return
        if not messagebox.askyesno("删除自选", "确定从自选股中删除 %s？" % code, parent=self):
            return
        self._request(self.services.remove_watchlist, code, on_done=self._after_watch_change)

    def _after_watch_change(self, _codes: Any) -> None:
        self._notify("自选股已更新", kind="ok")
        self.reload()

    def _on_watch_select(self, payload: Any = None, *_args: Any) -> None:
        code = self._code_of(payload) or self._code_of(self._selected_payload())
        if not code:
            return
        self._set_code(code, load=True)

    # ---- 渲染：K 线

    def _load_kline(self) -> None:
        if self.candle_chart is None:
            return
        code = normalize_code_text(self._current_code())
        if not code:
            self._show_kline_note("请输入标的代码（例如 600519.SH），或先选中一条自选股")
            return
        days = self._current_days()
        freq = dict(FREQ_CHOICES).get(self.freq_var.get(), "day") if hasattr(self, "freq_var") else "day"
        adjust = dict(ADJUST_CHOICES).get(self.adjust_var.get(), "qfq") if hasattr(self, "adjust_var") else "qfq"
        self._show_kline_note("正在加载 %s 的 K 线…" % code)
        self._request(self.services.kline, code, days=days, freq=freq, adjust=adjust,
                      on_done=lambda bars: self._render_kline(bars, code, days, freq, adjust),
                      on_error=lambda exc: self._on_kline_error(exc, code))

    def _render_kline(self, bars: Any, code: str, days: int, freq: str, adjust: str) -> None:
        rows = [item for item in (bars or []) if isinstance(item, dict)]
        self._kline_bars = rows
        name = self._names.get(code, "")
        if self.kline_title is not None:
            self.kline_title.configure(text="%s%s · 近 %d 日" % (("%s " % name) if name else "", code, days))
        if self.kline_meta is not None:
            freq_label = dict((value, label) for label, value in FREQ_CHOICES).get(freq, freq)
            adjust_label = dict((value, label) for label, value in ADJUST_CHOICES).get(adjust, adjust)
            self.kline_meta.configure(text="周期 %s · %s · 共 %d 根%s"
                                           % (freq_label, adjust_label, len(rows),
                                              (" · 最新 %s" % rows[-1].get("date")) if rows else ""))
        if not rows:
            self._clear_chart(self.candle_chart)
            self._show_kline_note("无 K 线数据：%s（换一个标的或周期试试）" % code)
            return
        try:
            self.candle_chart.set_data(rows, volumes=True, ma_periods=(5, 20, 60))
        except Exception as exc:            # noqa: BLE001 - 渲染失败也只提示，不弹窗
            self._show_kline_note("K 线渲染失败：%s" % exc)
            return
        _clear_chart_empty(self.candle_chart, self.kline_note)

    def _on_kline_error(self, exc: BaseException, code: str) -> None:
        self._clear_chart(self.candle_chart)
        self._show_kline_note("加载 %s 的 K 线失败：%s" % (code, exc))
        self._fallback_line("K 线失败：%s" % exc)
        self.toast_error(exc)

    def _show_kline_note(self, text: str) -> None:
        _set_chart_empty(self.candle_chart, self.kline_note, text)
        if self.kline_meta is not None:
            self.kline_meta.configure(text=text)

    def _clear_chart(self, chart: Any) -> None:
        if chart is None:
            return
        try:
            chart.set_data([])
        except Exception:                   # noqa: BLE001 - 空数据被控件拒绝时保持原图
            pass

    # ------------------------------------------------------------------ 辅助

    def _current_code(self) -> str:
        var = getattr(self, "code_var", None)
        return (var.get() if var is not None else "") or ""

    def _current_days(self) -> int:
        var = getattr(self, "days_var", None)
        try:
            value = int((var.get() if var is not None else "") or 250)
        except (TypeError, ValueError):
            return 250
        return value if value > 0 else 250

    def _set_code(self, code: str, load: bool = False) -> None:
        var = getattr(self, "code_var", None)
        if var is not None:
            try:
                var.set(code)
            except tk.TclError:
                return
        if load:
            self._load_kline()

    def _selected_payload(self) -> Any:
        if self.quote_table is None:
            return None
        try:
            return self.quote_table.selected()
        except Exception:                   # noqa: BLE001
            return None

    @staticmethod
    def _code_of(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("code") or "")
        if isinstance(payload, (list, tuple)):
            return str(payload[0]) if payload else ""
        return str(payload)

    def _show_form(self, title: str, fields: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """``FormDialog`` 兼容层：``on_submit`` 回调与 ``show()`` 返回值两种实现都只提交一次。"""
        state = {"done": False}
        result_holder: Dict[str, Any] = {}

        def _submit(payload: Any) -> None:
            if state["done"]:
                return
            state["done"] = True
            result_holder["payload"] = payload or {}

        try:
            dialog = forms.FormDialog(self, title, fields, on_submit=_submit, submit_label="保存")
        except Exception as exc:            # noqa: BLE001
            self.toast_error(exc)
            return None
        try:
            returned = dialog.show()
        except Exception as exc:            # noqa: BLE001
            self.toast_error(exc)
            return None
        if isinstance(returned, dict) and not state["done"]:
            _submit(returned)
        return result_holder.get("payload")

    def _notify(self, text: str, kind: str = "ok") -> None:
        try:
            self.toast.show(text, kind=kind)
        except Exception:                   # noqa: BLE001
            self.status(text)

    def _clear_fallback(self) -> None:
        if self.fallback is None:
            return
        try:
            self.fallback.configure(state="normal")
            self.fallback.delete("1.0", "end")
            self.fallback.configure(state="disabled")
        except tk.TclError:
            return

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


def _ratio(part: Any, total: Any) -> Optional[float]:
    """占比（%）；total 缺失或为 0 时返回 None（theme.fmt_pct(None) → 「—」）。"""
    try:
        numerator = float(part)
        denominator = float(total)
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    return numerator / denominator * 100.0


__all__ = ["MarketView", "ScrollArea", "normalize_code_text"]
