# -*- coding: utf-8 -*-
"""持仓管理：账户卡片 / 账户操作 / 资产配置 / 权益曲线 / 持仓明细。

与 ``MarketView`` 同一套约定：

* ``build()`` 只建控件（首次显示时调用一次），``reload()`` 只更新数据，不重建控件树；
* 所有服务调用都通过 ``self.load(...)`` 在后台线程执行，主线程不被阻塞；
* 控件库缺失或签名不符时降级为「内联错误 + 文本摘要」，不抛异常、不弹窗。
"""

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView

try:                                    # 与行情看板共用同一个滚动容器
    from .market import ScrollArea
except Exception as exc:                # noqa: BLE001 - 极端情况下退化为不可滚动容器
    ScrollArea = None                   # type: ignore[assignment]
    _SCROLL_ERROR = str(exc)

try:                                    # 控件库由并行子任务提供；未交付时降级运行
    from ..widgets import cards, charts, forms, table

    WIDGETS_ERROR = ""
except Exception as exc:                # noqa: BLE001 - 任何导入问题都只影响观感，不影响页面存活
    cards = charts = forms = table = None           # type: ignore[assignment]
    WIDGETS_ERROR = "控件库不可用：%s" % exc


class _PlainArea(ttk.Frame):
    """``ScrollArea`` 不可用时的兜底容器（无滚动，但有同样的 ``body`` 接口）。"""

    def __init__(self, parent: Any):
        super().__init__(parent, style="TFrame")
        self.body = self


AREA = ScrollArea if callable(ScrollArea) else _PlainArea

ACCOUNT_CARDS: Tuple[Tuple[str, str], ...] = (
    ("total_assets", "总资产"),
    ("market_value", "持仓市值"),
    ("cash", "可用现金"),
    ("day_pnl", "当日盈亏"),
    ("total_pnl", "累计盈亏"),
    ("return", "总收益率"),
    ("positions", "持仓数量"),
    ("mode", "账户模式"),
)
MODE_LABELS = {"manual": "手工记账", "paper": "模拟盘"}
MODE_HINT = ("manual（手工记账）= 你手工录入的真实持仓；"
             "paper（模拟盘）= 虚拟资金的模拟账户，下单不会发送到券商。")
EQUITY_DAYS = 90

HOLDING_COLUMNS: List[Dict[str, Any]] = [
    {"key": "code", "label": "代码", "width": 96, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 104, "anchor": "w", "kind": "text"},
    {"key": "qty", "label": "数量", "width": 84, "anchor": "e", "kind": "int"},
    {"key": "available_qty", "label": "可用", "width": 84, "anchor": "e", "kind": "int"},
    {"key": "cost", "label": "成本", "width": 84, "anchor": "e", "kind": "num", "digits": 3},
    {"key": "price", "label": "现价", "width": 84, "anchor": "e", "kind": "num", "digits": 2},
    {"key": "market_value", "label": "市值", "width": 108, "anchor": "e", "kind": "money", "digits": 2},
    {"key": "day_pnl", "label": "当日盈亏", "width": 102, "anchor": "e", "kind": "money", "digits": 2},
    {"key": "total_pnl", "label": "累计盈亏", "width": 102, "anchor": "e", "kind": "money", "digits": 2},
    {"key": "return_pct", "label": "收益率", "width": 90, "anchor": "e", "kind": "pct", "digits": 2},
]


# --------------------------------------------------------------------------- 工具函数


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


def _money(value: Any, digits: int = 2, unit: str = "元") -> str:
    if value is None or value == "":
        return "—"
    return "%s %s" % (theme.fmt_money(value, digits), unit)


def _signed_money(value: Any, digits: int = 2) -> str:
    """带正负号的金额（None → 「—」）。"""
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return "%s%s 元" % ("+" if number > 0 else "", theme.fmt_money(number, digits))


def _show_chart_note(label: Optional[tk.Label], text: str) -> None:
    """在图表上叠加一行提示（快照不足/失败状态，不弹窗）。"""
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


# --------------------------------------------------------------------------- 页面


class PortfolioView(BaseView):
    """持仓管理页面。"""

    title = "持仓管理"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._pending = 0
        self._widget_error = WIDGETS_ERROR
        self.build_errors: List[str] = []
        self.fallback: Optional[tk.Text] = None
        self._first_widget: Optional[Any] = None
        self._account: Dict[str, Any] = {}
        self._holdings: List[Dict[str, Any]] = []
        self._equity_points: List[Dict[str, Any]] = []
        self._mode = ""
        self._suppress_mode = False
        # 控件引用（构建失败时为 None，渲染前一律判空）
        self.account_cards: Dict[str, Any] = {}
        self.allocation_chart = None
        self.equity_chart = None
        self.equity_note = None
        self.holdings_table = None
        self.holdings_hint = None
        self.mode_hint = None

    # ------------------------------------------------------------------ 构建

    def build(self) -> None:
        self.mode_var = tk.StringVar(value="manual")

        self.scroll = AREA(self)
        self.scroll.pack(fill="both", expand=True)
        body = getattr(self.scroll, "body", self.scroll)

        if self._widget_error:
            self._build_widget_error(body, self._widget_error)
            self._build_fallback_text(body)

        self._section(body, "账户卡片", self._build_account)
        self._section(body, "账户操作", self._build_tools)
        self._section(body, "资产配置", self._build_allocation)
        self._section(body, "权益曲线", self._build_equity)
        self._section(body, "持仓明细", self._build_holdings)

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

    def _build_fallback_text(self, body: Any) -> None:
        self.fallback = tk.Text(body, height=16, bg=theme.COLORS["panel"], fg=theme.COLORS["text"],
                                relief="flat", font=theme.font(10, mono=True), wrap="word",
                                padx=10, pady=8)
        self.fallback.pack(fill="both", expand=True)
        self.fallback.configure(state="disabled")

    # ---- 各区块

    def _build_account(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "账户概览", hint="总资产 = 可用现金 + 持仓市值（服务层已勾稽）")
        title.pack(fill="x", pady=(0, 4))
        grid = cards.CardGrid(body, columns=4, gap=8)
        grid.pack(fill="x", pady=(0, 12))
        self.account_grid = grid
        for key, label in ACCOUNT_CARDS:
            card = cards.StatCard(grid, label, value="—", sub="等待数据…")
            grid.add(card)
            self.account_cards[key] = card
        return title

    def _build_tools(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "账户操作", hint="手工记账与模拟盘共用同一套界面",
                                   action=("刷新", self.reload))
        title.pack(fill="x", pady=(0, 4))
        bar = ttk.Frame(body, style="TFrame")
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text="录入/修改持仓", style="Primary.TButton",
                   command=self._upsert_holding).pack(side="left")
        ttk.Button(bar, text="删除选中持仓", style="Danger.TButton",
                   command=self._delete_holding).pack(side="left", padx=6)
        ttk.Button(bar, text="设置现金", command=self._set_cash).pack(side="left")
        ttk.Label(bar, text="账户模式", style="Muted.TLabel").pack(side="left", padx=(14, 4))
        self.mode_box = ttk.Combobox(bar, state="readonly", width=8, textvariable=self.mode_var,
                                     values=["manual", "paper"])
        self.mode_box.pack(side="left")
        self.mode_box.bind("<<ComboboxSelected>>", self._on_mode_selected)
        self.mode_hint = ttk.Label(bar, text="manual=手工录入的真实持仓 · paper=模拟盘账户",
                                   style="Muted.TLabel")
        self.mode_hint.pack(side="left", padx=(10, 0))
        return title

    def _build_allocation(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "资产配置", hint="现金与各持仓市值占比（来自 overview.allocation）")
        title.pack(fill="x", pady=(0, 4))
        self.allocation_chart = charts.DonutChart(body, height=240)
        self.allocation_chart.pack(fill="x", pady=(0, 12))
        return title

    def _build_equity(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "账户权益曲线",
                                   hint="按自然日快照，不足 %d 天时只显示已有快照" % EQUITY_DAYS)
        title.pack(fill="x", pady=(0, 4))
        self.equity_chart = charts.LineChart(body, height=240)
        self.equity_chart.pack(fill="x", pady=(0, 4))
        self.equity_note = tk.Label(self.equity_chart, text="", bg=theme.COLORS["panel"],
                                    fg=theme.COLORS["text2"], font=theme.font(10), justify="center")
        self.equity_notes = ttk.Label(body, text="", style="Muted.TLabel", wraplength=900, justify="left")
        self.equity_notes.pack(fill="x", pady=(0, 12))
        return title

    def _build_holdings(self, body: Any) -> Any:
        title = cards.SectionTitle(body, "持仓明细", hint="双击/选中行可预填「录入/修改持仓」")
        title.pack(fill="x", pady=(0, 4))
        self.holdings_table = table.DataTable(body, HOLDING_COLUMNS, height=8,
                                              on_select=self._on_holding_select,
                                              on_double_click=self._on_holding_select, sortable=True)
        self.holdings_table.pack(fill="x", pady=(0, 4))
        self.holdings_hint = ttk.Label(body, text="", style="Muted.TLabel")
        self.holdings_hint.pack(fill="x", pady=(0, 12))
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
        self.status("正在加载账户数据…")
        self._clear_fallback()
        self._request(self.services.portfolio_overview, on_done=self._render_account)
        self._request(self.services.holdings, on_done=self._render_holdings)
        self._request(self.services.portfolio_mode, on_done=self._render_mode)
        self._request(self.services.equity, days=EQUITY_DAYS, on_done=self._render_equity)

    # ---- 渲染：账户卡片

    def _render_account(self, overview: Any) -> None:
        data = dict(overview or {})
        self._account = data
        cash = data.get("available_cash")
        if cash in (None, ""):
            cash = data.get("cash")
        frozen = data.get("frozen_cash")
        cash_sub = "可用现金（含冻结 %s）" % _money(frozen) if frozen else "可用于买入的现金"
        rows: Dict[str, Tuple[str, Any, str]] = {
            "total_assets": (_money(data.get("total_assets")), None, "现金 + 持仓市值"),
            "market_value": (_money(data.get("market_value")), None,
                             "共 %s 只持仓" % theme.fmt_num(data.get("positions_count"), 0)),
            "cash": (_money(cash), None, cash_sub),
            "day_pnl": (_signed_money(data.get("day_pnl")), data.get("day_pnl"), "当日浮动盈亏"),
            "total_pnl": (_signed_money(data.get("total_pnl")), data.get("total_pnl"), "相对成本累计"),
            "return": (theme.fmt_pct(data.get("total_return_pct")), data.get("total_return_pct"), "累计收益率"),
            "positions": ("%s 只" % theme.fmt_num(data.get("positions_count"), 0), None, "持仓标的数量"),
            "mode": (self._mode_label(), None, "更新 %s" % (data.get("as_of") or "—")),
        }
        self._render_allocation(data.get("allocation"))
        if not self.account_cards:
            self._fallback_line(
                "账户：总资产 %s · 市值 %s · 现金 %s · 当日 %s · 累计 %s（%s）· 持仓 %s 只 · 模式 %s"
                % (rows["total_assets"][0], rows["market_value"][0], rows["cash"][0],
                   rows["day_pnl"][0], rows["total_pnl"][0], rows["return"][0],
                   rows["positions"][0], rows["mode"][0]))
            return
        for key, (value, color_by, sub) in rows.items():
            card = self.account_cards.get(key)
            if card is not None:
                _card_set(card, value, color_by=color_by, sub=sub)

    def _mode_label(self) -> str:
        return MODE_LABELS.get(self._mode, self._mode or "—")

    # ---- 渲染：资产配置

    def _render_allocation(self, allocation: Any) -> None:
        items: List[Dict[str, Any]] = []
        for row in allocation or []:
            if not isinstance(row, dict):
                continue
            items.append({
                "name": str(row.get("name") or row.get("code") or "-"),
                "value": row.get("value") if row.get("value") is not None else 0.0,
            })
        self._allocation = items
        if self.allocation_chart is None:
            self._fallback_line("资产配置：%s" % "、".join(
                "%s %s" % (item["name"], theme.fmt_money(item["value"], 0)) for item in items) or "无数据")
            return
        try:
            self.allocation_chart.set_data(items, colors=None)
        except Exception as exc:            # noqa: BLE001 - 图表失败不弹窗，只提示
            self._fallback_line("资产配置渲染失败：%s" % exc)

    # ---- 渲染：权益曲线

    def _render_equity(self, points: Any) -> None:
        rows = [item for item in (points or []) if isinstance(item, dict)]
        self._equity_points = rows
        values: List[Any] = []
        dates: List[str] = []
        for row in rows:
            value = row.get("equity")
            if value is None:
                value = row.get("total_assets")
            values.append(value)
            dates.append(str(row.get("date") or ""))
        if self.equity_chart is None:
            text = "，末日值 %s" % values[-1] if values else ""
            self._fallback_line("权益曲线：%d 个快照%s" % (len(rows), text))
        elif len(rows) < 2:
            self._clear_chart(self.equity_chart)
            _set_chart_empty(self.equity_chart, self.equity_note, "历史快照累积中（当前 %d 天）" % len(rows))
        else:
            try:
                self.equity_chart.set_data(values, dates=dates, color=theme.COLORS["brand"],
                                           fill=True, kind="money")
                _clear_chart_empty(self.equity_chart, self.equity_note)
            except Exception as exc:        # noqa: BLE001 - 图表失败不弹窗，只提示
                _set_chart_empty(self.equity_chart, self.equity_note, "权益曲线渲染失败：%s" % exc)
        if self.equity_notes is not None:
            notes = list(getattr(self.services, "last_notes", None) or [])
            self.equity_notes.configure(text="；".join(str(item) for item in notes) if notes else "")

    # ---- 渲染：持仓明细

    def _render_holdings(self, rows: Any) -> None:
        self._holdings = [item for item in (rows or []) if isinstance(item, dict)]
        if self.holdings_table is None:
            self._fallback_line("持仓明细（%d 只）：%s" % (len(self._holdings), "、".join(
                "%s %s 数量 %s 市值 %s" % (item.get("code"), item.get("name") or "",
                                          theme.fmt_num(item.get("qty"), 0),
                                          _money(item.get("market_value"))) for item in self._holdings) or "无"))
        else:
            self.holdings_table.set_rows(self._holdings, key_field="code", preserve_selection=True)
        if self.holdings_hint is not None:
            if self._holdings:
                self.holdings_hint.configure(
                    text="共 %d 只持仓 · 选中一行后可删除或据此修改" % len(self._holdings))
            else:
                self.holdings_hint.configure(text="暂无持仓，点击『录入/修改持仓』添加")

    # ---- 渲染：账户模式

    def _render_mode(self, mode: Any) -> None:
        self._mode = str(mode or "manual")
        self._suppress_mode = True
        try:
            self.mode_var.set(self._mode)
        except tk.TclError:
            return
        finally:
            self._suppress_mode = False
        if self.mode_hint is not None:
            self.mode_hint.configure(text="当前：%s" % self._mode_label())
        card = self.account_cards.get("mode")
        if card is not None:
            as_of = self._account.get("as_of") or "—"
            _card_set(card, self._mode_label(), color_by=None, sub="更新 %s" % as_of)

    def _on_mode_selected(self, _event: Any = None) -> None:
        if self._suppress_mode:
            return
        mode = self.mode_var.get()
        if mode == self._mode:
            return
        question = "切换账户模式为「%s」？\n\n%s" % (MODE_LABELS.get(mode, mode), MODE_HINT)
        if not messagebox.askyesno("切换账户模式", question, parent=self):
            self._suppress_mode = True
            try:
                self.mode_var.set(self._mode or "manual")
            finally:
                self._suppress_mode = False
            return
        self._request(self.services.set_portfolio_mode, mode, on_done=self._after_mode_change)

    def _after_mode_change(self, result: Any) -> None:
        mode = result.get("mode") if isinstance(result, dict) else result
        self._mode = str(mode or self._mode or "manual")
        self._notify("账户模式已切换为 %s。%s" % (self._mode_label(), MODE_HINT), kind="ok")
        self.reload()

    # ---- 操作：录入选/删除/现金

    def _upsert_holding(self) -> None:
        if forms is None:
            self._notify("表单控件不可用，无法录入持仓", kind="error")
            return
        values = self._selected_row() or {}
        fields: List[Dict[str, Any]] = [
            {"name": "code", "label": "代码", "type": "text", "required": True,
             "default": str(values.get("code") or ""),
             "help": "支持 600519 / sh600519 / 600519.SH"},
            {"name": "name", "label": "名称", "type": "text", "default": str(values.get("name") or "")},
            {"name": "qty", "label": "数量", "type": "int", "min": 1, "required": True,
             "default": values.get("qty") or 100},
            {"name": "cost", "label": "成本价", "type": "float", "min": 0.0,
             "default": values.get("cost") or 0.0},
            {"name": "available_qty", "label": "可用数量", "type": "int", "min": 0,
             "default": values.get("available_qty") or 0},
        ]
        payload = self._show_form("录入/修改持仓", fields, values=values or None,
                                  hint="同一代码重复提交即覆盖该标的的持仓记录。")
        if not payload:
            return
        item: Dict[str, Any] = {"code": str(payload.get("code") or "").strip(), "qty": payload.get("qty")}
        for key in ("name", "cost", "available_qty"):
            if payload.get(key) not in (None, ""):
                item[key] = payload.get(key)
        if not item["code"]:
            self._notify("代码不能为空", kind="error")
            return
        self._request(self.services.upsert_holding, item, on_done=self._after_write)

    def _delete_holding(self) -> None:
        code = self._code_of(self._selected_payload())
        if not code:
            self._notify("请先在持仓明细中选中一行", kind="warn")
            return
        if not messagebox.askyesno("删除持仓", "确定删除 %s 的持仓记录？" % code, parent=self):
            return
        self._request(self.services.delete_holding, code, on_done=self._after_write)

    def _set_cash(self) -> None:
        if forms is None:
            self._notify("表单控件不可用，无法设置现金", kind="error")
            return
        current = self._account.get("cash")
        if current in (None, ""):
            current = self._account.get("available_cash")
        payload = self._show_form("设置现金", [{
            "name": "amount", "label": "可用现金（元）", "type": "float", "min": 0.0, "required": True,
            "default": theme.fmt_money(current, 2) if current not in (None, "") else 0.0,
            "help": "写入账户的现金余额（会立即反映到总资产与持仓成本勾稽）",
        }], hint="只影响账户记账，不会产生任何真实资金划转。")
        if not payload:
            return
        amount = payload.get("amount")
        self._request(self.services.set_cash, amount, on_done=self._after_write)

    def _after_write(self, result: Any) -> None:
        self._notify("账户已更新", kind="ok")
        self.reload()

    def _on_holding_select(self, payload: Any = None, *_args: Any) -> None:
        code = self._code_of(payload) or self._code_of(self._selected_payload())
        if code:
            self.status("已选中持仓：%s" % code)

    # ------------------------------------------------------------------ 辅助

    def _selected_payload(self) -> Any:
        if self.holdings_table is None:
            return None
        try:
            return self.holdings_table.selected()
        except Exception:                   # noqa: BLE001
            return None

    def _selected_row(self) -> Dict[str, Any]:
        """当前选中行（``selected()`` 可能返回 dict / 代码 / None）。"""
        payload = self._selected_payload()
        if isinstance(payload, dict):
            return payload
        code = self._code_of(payload)
        if code:
            for row in self._holdings:
                if str(row.get("code") or "") == code:
                    return row
        return {}

    @staticmethod
    def _code_of(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("code") or "")
        if isinstance(payload, (list, tuple)):
            return str(payload[0]) if payload else ""
        return str(payload)

    def _show_form(self, title: str, fields: List[Dict[str, Any]], values: Any = None,
                   hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """``FormDialog`` 兼容层：``on_submit`` 回调与 ``show()`` 返回值两种实现都只提交一次。"""
        state = {"done": False}
        holder: Dict[str, Any] = {}

        def _submit(payload: Any) -> None:
            if state["done"]:
                return
            state["done"] = True
            holder["payload"] = payload or {}

        try:
            dialog = forms.FormDialog(self, title, fields, values=values, on_submit=_submit,
                                      submit_label="保存", hint=hint)
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
        return holder.get("payload")

    def _notify(self, text: str, kind: str = "ok") -> None:
        try:
            self.toast.show(text, kind=kind)
        except Exception:                   # noqa: BLE001
            self.status(text)

    def _clear_chart(self, chart: Any) -> None:
        if chart is None:
            return
        try:
            chart.set_data([])
        except Exception:                   # noqa: BLE001 - 空数据被控件拒绝时保持原图
            pass

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


__all__ = ["PortfolioView"]
