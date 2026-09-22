# -*- coding: utf-8 -*-
"""交易（模拟盘）页面：账户卡 + 下单 + 持仓 / 委托 / 成交。

- 明确提示：本页所有委托只在本地模拟撮合，不会发送真实委托、不连接券商；
- 账户 / 持仓 / 委托 / 成交一次性在后台线程取快照，主线程只做渲染；
- 下单前本地校验（代码非空、数量为正整数、价格为正数或留空=市价）。
"""

import importlib
import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView

BANNER_TEXT = "本页为模拟盘：所有委托仅在本地撮合成交，不会发送任何真实委托、不会连接券商"
SIDES = ["买入", "卖出"]
SIDE_MAP = {"买入": "buy", "卖出": "sell"}
SIDE_TEXT = {"buy": "买入", "sell": "卖出"}
STATUS_TEXT = {"new": "待成交", "filled": "已成交", "partial": "部分成交",
               "rejected": "已拒单", "cancelled": "已撤单"}
CANCELLABLE = ("new", "partial")

HOLDING_COLUMNS = [
    {"key": "code", "label": "代码", "width": 96, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 96, "anchor": "w", "kind": "text"},
    {"key": "qty", "label": "数量", "width": 76, "anchor": "e", "kind": "int"},
    {"key": "available_qty", "label": "可用", "width": 76, "anchor": "e", "kind": "int"},
    {"key": "cost", "label": "成本", "width": 80, "anchor": "e", "kind": "money"},
    {"key": "price", "label": "现价", "width": 80, "anchor": "e", "kind": "money"},
    {"key": "market_value", "label": "市值", "width": 106, "anchor": "e", "kind": "money"},
    {"key": "total_pnl", "label": "浮盈", "width": 96, "anchor": "e", "kind": "money"},
    {"key": "return_pct", "label": "收益率", "width": 80, "anchor": "e", "kind": "pct"},
]

ORDER_COLUMNS = [
    {"key": "created_at", "label": "时间", "width": 128, "anchor": "w", "kind": "text"},
    {"key": "code", "label": "代码", "width": 92, "anchor": "w", "kind": "code"},
    {"key": "side_text", "label": "方向", "width": 60, "anchor": "center", "kind": "badge"},
    {"key": "qty", "label": "数量", "width": 70, "anchor": "e", "kind": "int"},
    {"key": "price", "label": "价格", "width": 76, "anchor": "e", "kind": "money"},
    {"key": "filled_qty", "label": "已成交", "width": 70, "anchor": "e", "kind": "int"},
    {"key": "avg_price", "label": "均价", "width": 76, "anchor": "e", "kind": "money"},
    {"key": "fee", "label": "费用", "width": 76, "anchor": "e", "kind": "money"},
    {"key": "status_text", "label": "状态", "width": 74, "anchor": "center", "kind": "badge"},
    {"key": "reason", "label": "原因", "width": 210, "anchor": "w", "kind": "text"},
]

FILL_COLUMNS = [
    {"key": "ts", "label": "时间", "width": 128, "anchor": "w", "kind": "text"},
    {"key": "code", "label": "代码", "width": 92, "anchor": "w", "kind": "code"},
    {"key": "side_text", "label": "方向", "width": 60, "anchor": "center", "kind": "badge"},
    {"key": "price", "label": "价格", "width": 80, "anchor": "e", "kind": "money"},
    {"key": "qty", "label": "数量", "width": 70, "anchor": "e", "kind": "int"},
    {"key": "amount", "label": "金额", "width": 108, "anchor": "e", "kind": "money"},
    {"key": "fee", "label": "费用", "width": 80, "anchor": "e", "kind": "money"},
]

# (key, 标题, 类型, 说明, 着色方式)
ACCOUNT_SPECS: List[Tuple[str, str, str, str, str]] = [
    ("total_assets", "总资产", "money", "现金 + 持仓市值", "none"),
    ("market_value", "持仓市值", "money", "按最新价估值", "none"),
    ("available_cash", "可用现金", "money", "可买入额度", "none"),
    ("day_pnl", "当日盈亏", "money", "逐日盯市", "auto"),
    ("total_pnl", "累计盈亏", "money", "含已实现 + 浮动", "auto"),
    ("total_return_pct", "总收益率", "pct", "相对初始资金", "auto"),
    ("positions_count", "持仓数", "int", "持有标的数量", "none"),
    ("mode", "模式", "text", "本地模拟撮合", "none"),
]


def load_widgets(*names: str) -> Tuple[Dict[str, Any], List[str]]:
    """惰性导入 ``..widgets`` 下的子模块；缺失时值为 None 并记录原因。"""
    modules: Dict[str, Any] = {}
    missing: List[str] = []
    for name in names:
        try:
            modules[name] = importlib.import_module("..widgets." + name, __package__)
        except Exception as exc:                      # noqa: BLE001 - 控件缺失不能拖垮页面
            modules[name] = None
            missing.append("%s（%s）" % (name, exc))
    return modules, missing


def widget_class(modules: Dict[str, Any], module: str, name: str) -> Any:
    mod = modules.get(module)
    return getattr(mod, name, None) if mod is not None else None


def _text(value: Any, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


class TradeView(BaseView):
    """模拟盘交易页（下单 / 持仓 / 委托 / 成交）。"""

    title = "交易（模拟盘）"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._mods: Dict[str, Any] = {}
        self._missing: List[str] = []
        self._degraded = False
        self._cards: Dict[str, Any] = {}
        self._watchlist: List[str] = []
        self._holdings: List[Dict[str, Any]] = []
        self._orders: List[Dict[str, Any]] = []
        self._fills: List[Dict[str, Any]] = []
        self._selected_order_id = ""
        self._busy = False

        self._account_note = tk.StringVar(master=self, value="正在加载账户…")
        self._order_note = tk.StringVar(master=self, value="数量需为 100 股整数倍；价格留空 = 市价")
        self._code_var = tk.StringVar(master=self, value="")
        self._side_var = tk.StringVar(master=self, value=SIDES[0])
        self._qty_var = tk.StringVar(master=self, value="100")
        self._price_var = tk.StringVar(master=self, value="")

    # ------------------------------------------------------------------ 构建
    def build(self) -> None:
        self._mods, self._missing = load_widgets("cards", "table")
        if self._mods.get("cards") is None or self._mods.get("table") is None:
            self._degraded = True
            self._build_inline_error(
                "交易页面缺少控件模块：%s\n请确认 quantstudio_desktop/widgets/ 下的 "
                "cards.py 与 table.py 已就位。" % "、".join(self._missing))
            return
        try:
            self._build_ui()
        except Exception as exc:                      # noqa: BLE001
            self._degraded = True
            self._build_inline_error("交易页面构建失败：%s" % exc)

    def _build_inline_error(self, message: str) -> None:
        box = ttk.Frame(self, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=message, style="Card.TLabel", wraplength=760,
                  justify="left").pack(anchor="w")

    def _place(self, widget: Any, **pack_options: Any) -> Any:
        try:
            if not widget.winfo_manager():
                widget.pack(**pack_options)
        except Exception:                             # noqa: BLE001
            pass
        return widget

    def _grid_add(self, grid: Any, card: Any, index: int) -> None:
        adder = getattr(grid, "add", None)
        if callable(adder):
            try:
                adder(card)
                return
            except Exception:                         # noqa: BLE001
                pass
        try:
            card.grid(row=index // 4, column=index % 4, sticky="nsew", padx=4, pady=4)
        except Exception:                             # noqa: BLE001
            self._place(card, fill="x")

    def _build_ui(self) -> None:
        cards = self._mods["cards"]
        table_mod = self._mods["table"]
        SectionTitle = widget_class(self._mods, "cards", "SectionTitle")
        CardGrid = widget_class(self._mods, "cards", "CardGrid")
        StatCard = widget_class(self._mods, "cards", "StatCard")
        Badge = widget_class(self._mods, "cards", "Badge")

        # ---- 顶部醒目提示条
        banner = tk.Frame(self, bg=theme.COLORS["panel2"], highlightthickness=1,
                          highlightbackground=theme.COLORS["warn"])
        banner.pack(fill="x", pady=(0, 8))
        text = tk.Label(banner, text="⚠ " + BANNER_TEXT, bg=theme.COLORS["panel2"],
                        fg=theme.COLORS["warn"], font=theme.font(10, "bold"), anchor="w",
                        padx=10, pady=7, justify="left")
        text.pack(side="left", fill="x", expand=True)
        if Badge is not None:
            try:
                badge = Badge(banner, "模拟盘", kind="warn")
                self._place(badge, side="right", padx=10)
            except Exception:                         # noqa: BLE001
                pass

        # ---- 账户卡
        if CardGrid is not None and StatCard is not None:
            grid = CardGrid(self, columns=4, gap=8)
            self._place(grid, fill="x", pady=(0, 10))
            for index, (key, title, _kind, sub, _color) in enumerate(ACCOUNT_SPECS):
                try:
                    card = StatCard(grid, title, value="—", sub=sub)
                except Exception as exc:              # noqa: BLE001
                    self.status("账户卡创建失败：%s" % exc)
                    break
                self._cards[key] = card
                self._grid_add(grid, card, index)
        ttk.Label(self, textvariable=self._account_note, style="Muted.TLabel").pack(
            anchor="w", pady=(0, 6))

        # ---- 下单区
        if SectionTitle is not None:
            self._place(SectionTitle(self, "下单", hint="市价单按最新价 ± 滑点成交；限价不成交会挂单"),
                        fill="x")
        form = ttk.Frame(self, style="Card.TFrame", padding=(12, 10))
        self._place(form, fill="x", pady=(4, 10))
        ttk.Label(form, text="代码", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        self._code_box = ttk.Combobox(form, textvariable=self._code_var, values=[], width=14)
        self._code_box.grid(row=0, column=1, sticky="w", padx=(6, 14))
        self._code_box.bind("<KeyRelease>", self._on_code_key)
        ttk.Label(form, text="方向", style="CardMuted.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Combobox(form, textvariable=self._side_var, values=SIDES, state="readonly",
                     width=8).grid(row=0, column=3, sticky="w", padx=(6, 14))
        ttk.Label(form, text="数量", style="CardMuted.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self._qty_var, width=10).grid(row=0, column=5, sticky="w",
                                                                   padx=(6, 14))
        ttk.Label(form, text="价格", style="CardMuted.TLabel").grid(row=0, column=6, sticky="w")
        ttk.Entry(form, textvariable=self._price_var, width=10).grid(row=0, column=7, sticky="w",
                                                                     padx=(6, 14))
        self._submit_btn = ttk.Button(form, text="提交委托", style="Primary.TButton",
                                      command=self._submit_order)
        self._submit_btn.grid(row=0, column=8, sticky="w")
        ttk.Button(form, text="刷新", command=self.refresh).grid(row=0, column=9, sticky="w",
                                                                 padx=(8, 0))
        ttk.Label(form, textvariable=self._order_note, style="CardMuted.TLabel").grid(
            row=1, column=0, columnspan=10, sticky="w", pady=(6, 0))

        # ---- 持仓 / 委托 / 成交
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        holdings_tab = ttk.Frame(notebook, style="TFrame", padding=(6, 8))
        self._holdings_table = table_mod.DataTable(holdings_tab, HOLDING_COLUMNS, height=9,
                                                   sortable=True)
        self._place(self._holdings_table, fill="both", expand=True)
        notebook.add(holdings_tab, text="持仓")

        orders_tab = ttk.Frame(notebook, style="TFrame", padding=(6, 8))
        order_bar = ttk.Frame(orders_tab, style="TFrame")
        order_bar.pack(side="bottom", fill="x", pady=(6, 0))
        self._orders_table = table_mod.DataTable(orders_tab, ORDER_COLUMNS, height=9,
                                                 on_select=self._on_order_select, sortable=True)
        self._place(self._orders_table, fill="both", expand=True)
        self._cancel_btn = ttk.Button(order_bar, text="撤销选中委托", style="Danger.TButton",
                                      command=self._cancel_order)
        self._cancel_btn.pack(side="left")
        self._cancel_btn.configure(state="disabled")
        ttk.Label(order_bar, text="仅「待成交 / 部分成交」的委托可撤销；撤单后资金/持仓立即刷新",
                  style="Muted.TLabel").pack(side="left", padx=10)
        notebook.add(orders_tab, text="委托")

        fills_tab = ttk.Frame(notebook, style="TFrame", padding=(6, 8))
        self._fills_table = table_mod.DataTable(fills_tab, FILL_COLUMNS, height=9, sortable=True)
        self._place(self._fills_table, fill="both", expand=True)
        notebook.add(fills_tab, text="成交")

        bottom = ttk.Frame(self, style="TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="重置模拟盘", style="Danger.TButton",
                   command=self._reset_paper).pack(side="left")
        ttk.Label(bottom, text="重置会清空持仓 / 委托 / 成交记录，并恢复初始资金",
                  style="Muted.TLabel").pack(side="left", padx=10)

    # ------------------------------------------------------------------ 数据
    def reload(self) -> None:
        if self._degraded:
            return
        self.status("正在刷新模拟盘账户与持仓…")
        self.load(self._snapshot, on_done=self._on_snapshot, on_error=self._on_snapshot_error,
                  name="paper-snapshot")

    def _snapshot(self) -> Dict[str, Any]:
        """一次性取账户/持仓/委托/成交/自选池快照（后台线程执行）。"""
        data: Dict[str, Any] = {"errors": []}
        account_fn = getattr(self.services, "portfolio_overview", None)
        calls = [
            ("account", (lambda: account_fn()) if callable(account_fn) else (lambda: {})),
            ("holdings", lambda: self.services.holdings()),
            ("orders", lambda: self.services.orders(200)),
            ("fills", lambda: self.services.fills(200)),
        ]
        for key, fn in calls:
            try:
                data[key] = fn()
            except Exception as exc:                  # noqa: BLE001 - 单项失败不拖垮整页
                data[key] = None
                data["errors"].append("%s 读取失败：%s" % (key, exc))
        try:
            data["watchlist"] = self.services.watchlist()
        except Exception as exc:                      # noqa: BLE001
            data["watchlist"] = []
            data["errors"].append("自选池读取失败：%s" % exc)
        mode_fn = getattr(self.services, "portfolio_mode", None)
        try:
            data["mode"] = mode_fn() if callable(mode_fn) else "paper"
        except Exception as exc:                      # noqa: BLE001
            data["mode"] = "paper"
            data["errors"].append("模式读取失败：%s" % exc)
        return data

    def _on_snapshot_error(self, exc: BaseException) -> None:
        self.toast_error(exc)
        self._account_note.set("账户加载失败：%s" % exc)

    def _on_snapshot(self, data: Any) -> None:
        data = data if isinstance(data, dict) else {}
        errors = data.get("errors") or []
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        holdings = data.get("holdings")
        orders = data.get("orders")
        fills = data.get("fills")
        self._holdings = [row for row in holdings if isinstance(row, dict)] \
            if isinstance(holdings, list) else []
        self._orders = [row for row in orders if isinstance(row, dict)] \
            if isinstance(orders, list) else []
        self._fills = [row for row in fills if isinstance(row, dict)] \
            if isinstance(fills, list) else []
        self._watchlist = [str(code) for code in (data.get("watchlist") or []) if str(code).strip()]
        try:
            self._code_box.configure(values=self._watchlist[:20])
        except Exception:                             # noqa: BLE001
            pass

        self._render_account(account, data.get("mode"))
        self._render_holdings()
        self._render_orders()
        self._render_fills()
        self._cancel_btn.configure(state="disabled")
        self._selected_order_id = ""

        if errors:
            self._account_note.set("部分数据读取失败：%s" % "；".join(str(item) for item in errors))
            for item in errors:
                self._notify(str(item), kind="warn")
        else:
            note = ("数据时点：%s ｜ 持仓 %d 只 ｜ 委托 %d 条 ｜ 成交 %d 条"
                    % (_text(account.get("as_of")), len(self._holdings),
                       len(self._orders), len(self._fills)))
            mode_text = _text(data.get("mode"), "paper")
            if mode_text != "paper":
                note += (" ｜ 注意：当前账户模式为 %s：账户与持仓来自持仓账本，"
                         "委托与成交来自本地模拟盘通道" % mode_text)
            self._account_note.set(note)
        self.status("模拟盘已刷新：总资产 %s 元"
                    % theme.fmt_money(account.get("total_assets")))

    def _render_account(self, account: Dict[str, Any], mode: Any) -> None:
        for key, _title, kind, sub, color in ACCOUNT_SPECS:
            card = self._cards.get(key)
            if card is None:
                continue
            if key == "mode":
                value = "模拟盘（%s）" % _text(mode, "paper")
                color_by = None
            else:
                raw = account.get(key)
                value = self._format_value(kind, raw)
                number = _number(raw)
                color_by = number if (color == "auto" and number is not None) else None
            try:
                card.set(value, color_by=color_by, sub=sub)
            except Exception:                         # noqa: BLE001
                continue

    @staticmethod
    def _format_value(kind: str, raw: Any) -> str:
        if raw is None or raw == "":
            return "—"
        if kind == "pct":
            return theme.fmt_pct(raw)
        if kind == "money":
            return theme.fmt_money(raw)
        if kind == "int":
            number = _number(raw)
            return "—" if number is None else str(int(number))
        return _text(raw)

    def _render_holdings(self) -> None:
        try:
            self._holdings_table.set_rows(self._holdings, key_field="code")
        except Exception as exc:                      # noqa: BLE001
            self.status("持仓渲染失败：%s" % exc)

    def _render_orders(self) -> None:
        rows = []
        for row in self._orders:
            item = dict(row)
            item["side_text"] = SIDE_TEXT.get(str(row.get("side")), _text(row.get("side")))
            item["status_text"] = STATUS_TEXT.get(str(row.get("status")), _text(row.get("status")))
            rows.append(item)
        try:
            self._orders_table.set_rows(rows, key_field="order_id", preserve_selection=False)
        except Exception as exc:                      # noqa: BLE001
            self.status("委托渲染失败：%s" % exc)

    def _render_fills(self) -> None:
        rows = []
        for row in self._fills:
            item = dict(row)
            item["side_text"] = SIDE_TEXT.get(str(row.get("side")), _text(row.get("side")))
            rows.append(item)
        try:
            self._fills_table.set_rows(rows, key_field=None)
        except Exception as exc:                      # noqa: BLE001
            self.status("成交渲染失败：%s" % exc)

    # ------------------------------------------------------------------ 下单
    def _notify(self, text: str, kind: str = "info") -> None:
        try:
            self.toast.show(text, kind=kind)
        except Exception:                             # noqa: BLE001
            self.status(text)

    def _on_code_key(self, _event: Any = None) -> None:
        prefix = (self._code_var.get() or "").strip().upper()
        values = [code for code in self._watchlist if not prefix or prefix in code.upper()][:20]
        try:
            self._code_box.configure(values=values or self._watchlist[:20])
        except Exception:                             # noqa: BLE001
            pass

    def _order_payload(self) -> Tuple[Optional[Dict[str, Any]], str]:
        code = (self._code_var.get() or "").strip().upper()
        if not code:
            return None, "请填写委托代码，例如 600519.SH"
        if not re.match(r"^[0-9A-Za-z.\-]{4,16}$", code):
            return None, "代码格式不正确：%r" % code
        side = SIDE_MAP.get((self._side_var.get() or "").strip())
        if side is None:
            return None, "方向只能是买入 / 卖出"
        qty_text = (self._qty_var.get() or "").strip()
        try:
            qty = int(qty_text)
        except (TypeError, ValueError):
            return None, "数量需为正整数（当前 %r）" % qty_text
        if qty <= 0:
            return None, "数量需为正整数（当前 %r）" % qty_text
        payload: Dict[str, Any] = {"code": code, "side": side, "qty": qty, "reason": "desktop-manual"}
        price_text = (self._price_var.get() or "").strip()
        if price_text:
            price = _number(price_text)
            if price is None or price <= 0:
                return None, "价格需为正数或留空（当前 %r）" % price_text
            payload["price"] = float(price)
        return payload, ""

    def _submit_order(self) -> None:
        if self._degraded or self._busy:
            return
        payload, problem = self._order_payload()
        if payload is None:
            self._order_note.set(problem)
            self.toast_error(ValueError(problem))
            return
        if int(payload["qty"]) % 100:
            self._order_note.set("提示：数量 %d 不是 100 的整数倍，模拟盘会拒单" % payload["qty"])
        else:
            self._order_note.set("正在提交：%s %s %s 股"
                                 % (SIDE_TEXT.get(payload["side"]), payload["code"], payload["qty"]))
        self._set_busy(True)
        self.load(self.services.submit_order, payload, on_done=self._on_submitted,
                  on_error=self._on_submit_error, name="submit-order")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self._submit_btn.configure(state="disabled" if busy else "normal")
        except Exception:                             # noqa: BLE001
            pass

    def _on_submit_error(self, exc: BaseException) -> None:
        self._set_busy(False)
        self._order_note.set("委托失败：%s" % exc)
        self.toast_error(exc)
        self.reload()

    def _on_submitted(self, result: Any) -> None:
        self._set_busy(False)
        data = result if isinstance(result, dict) else {}
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        fill = data.get("fill") if isinstance(data.get("fill"), dict) else {}
        code = _text(order.get("code"), (self._code_var.get() or "").strip().upper())
        side_text = SIDE_TEXT.get(str(order.get("side")), _text(order.get("side")))
        if fill:
            message = "已成交：%s %s %s 股 @ %s，费用 %s 元" % (
                side_text, code, _text(fill.get("qty")), theme.fmt_money(fill.get("price")),
                theme.fmt_money(fill.get("fee")))
            self._order_note.set(message)
            self._notify(message, kind="ok")
            self._price_var.set("")
        elif str(order.get("status")) == "new":
            message = "已挂单（未成交）：%s %s %s 股，%s" % (
                side_text, code, _text(order.get("qty")), _text(order.get("reason")))
            self._order_note.set(message)
            self._notify(message, kind="warn")
        else:
            message = "委托已提交：%s %s 状态 %s" % (
                side_text, code, _text(order.get("status_text") or order.get("status")))
            self._order_note.set(message)
            self._notify(message, kind="ok")
        self.reload()

    # ------------------------------------------------------------------ 撤单 / 重置
    def _on_order_select(self, *_args: Any) -> None:
        order = self._selected_order()
        state = "normal" if order and str(order.get("status")) in CANCELLABLE else "disabled"
        try:
            self._cancel_btn.configure(state=state)
        except Exception:                             # noqa: BLE001
            pass

    def _selected_order(self) -> Optional[Dict[str, Any]]:
        value = None
        try:
            value = self._orders_table.selected()
        except Exception:                             # noqa: BLE001
            value = None
        by_id = {str(row.get("order_id") or ""): row for row in self._orders}
        if isinstance(value, dict):
            key = str(value.get("order_id") or "")
            return by_id.get(key) or (value if value.get("order_id") else None)
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    key = str(item.get("order_id") or "")
                    if key:
                        return by_id.get(key) or item
            return None
        if value is not None:
            key = str(value)
            if key in by_id:
                return by_id[key]
            try:
                index = int(key)
            except (TypeError, ValueError):
                index = -1
            if 0 <= index < len(self._orders):
                return self._orders[index]
        return by_id.get(self._selected_order_id)

    def _cancel_order(self) -> None:
        if self._degraded:
            return
        order = self._selected_order()
        if order is None:
            self._notify("请先在「委托」页签选择一条委托", kind="warn")
            return
        status = str(order.get("status"))
        if status not in CANCELLABLE:
            self._notify("该委托状态为「%s」，无法撤单" % STATUS_TEXT.get(status, status), kind="warn")
            return
        order_id = str(order.get("order_id") or "")
        if not order_id:
            self._notify("该委托缺少 order_id，无法撤单", kind="error")
            return
        try:
            confirmed = messagebox.askyesno(
                "撤销委托",
                "确定撤销委托 %s %s %s 股？\n（模拟盘撤销后不可恢复）"
                % (SIDE_TEXT.get(str(order.get("side")), _text(order.get("side"))),
                   _text(order.get("code")), _text(order.get("qty"))), parent=self)
        except Exception as exc:                      # noqa: BLE001
            self.toast_error(exc)
            return
        if not confirmed:
            return
        self.status("正在撤单：%s …" % order_id)
        self.load(self.services.cancel_order, order_id, on_done=self._on_cancelled,
                  on_error=self.toast_error, name="cancel-order")

    def _on_cancelled(self, order: Any) -> None:
        order = order if isinstance(order, dict) else {}
        self._notify("已撤销委托：%s %s（状态 %s）"
                     % (_text(order.get("code")), _text(order.get("order_id")),
                        STATUS_TEXT.get(str(order.get("status")), _text(order.get("status")))),
                     kind="ok")
        self.reload()

    def _reset_paper(self) -> None:
        if self._degraded:
            return
        try:
            confirmed = messagebox.askyesno(
                "重置模拟盘",
                "将清空模拟盘账户的持仓、委托与成交记录，并恢复初始资金。\n确定继续？",
                parent=self)
        except Exception as exc:                      # noqa: BLE001
            self.toast_error(exc)
            return
        if not confirmed:
            return
        self.status("正在重置模拟盘…")
        self.load(self.services.reset_paper, on_done=self._on_reset,
                  on_error=self.toast_error, name="reset-paper")

    def _on_reset(self, account: Any) -> None:
        account = account if isinstance(account, dict) else {}
        self._notify("模拟盘已重置：总资产 %s 元" % theme.fmt_money(account.get("total_assets")),
                     kind="ok")
        self.reload()


__all__ = ["TradeView"]
