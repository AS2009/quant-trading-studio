# -*- coding: utf-8 -*-
"""回测分析页面：参数表单 + 指标卡 + 净值/回撤/月度/日收益图 + 交易与期末持仓。

- 左侧策略列表单选，右侧上部内嵌参数表单（不用弹窗），下部为滚动结果区；
- 所有服务调用走 ``self.load``（后台线程），运行期间禁用按钮并在状态栏提示；
- 支持从「策略管理」页跳转：``app.pending_backtest_strategy`` 有值时自动选中并跑一次。
"""

import datetime
import importlib
import re
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView

BENCHMARKS = ["000300.SH", "000905.SH", "000001.SH", "399001.SZ", "399006.SZ"]
DEFAULT_SYMBOL_SLOTS = 4
MAX_SYMBOL_CHECKS = 40
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ORIGIN_LABELS = {"builtin": "内置", "local": "本地代码", "user": "自定义"}
STATUS_LABELS = {"running": "运行中", "paused": "已暂停"}

STRATEGY_COLUMNS = [
    {"key": "name", "label": "策略", "width": 132, "anchor": "w", "kind": "text"},
    {"key": "origin_text", "label": "来源", "width": 68, "anchor": "center", "kind": "badge"},
    {"key": "status_text", "label": "状态", "width": 66, "anchor": "center", "kind": "badge"},
    {"key": "min_bars", "label": "最少 bar", "width": 72, "anchor": "center", "kind": "int"},
]

TRADE_COLUMNS = [
    {"key": "date", "label": "日期", "width": 90, "anchor": "w", "kind": "text"},
    {"key": "side_text", "label": "方向", "width": 62, "anchor": "center", "kind": "badge"},
    {"key": "price", "label": "价格", "width": 78, "anchor": "e", "kind": "money"},
    {"key": "qty", "label": "数量", "width": 70, "anchor": "e", "kind": "int"},
    {"key": "amount", "label": "金额", "width": 104, "anchor": "e", "kind": "money"},
    {"key": "fee", "label": "费用", "width": 80, "anchor": "e", "kind": "money"},
    {"key": "pnl", "label": "盈亏", "width": 92, "anchor": "e", "kind": "money"},
    {"key": "ret_pct", "label": "收益率", "width": 82, "anchor": "e", "kind": "pct"},
]

POSITION_COLUMNS = [
    {"key": "code", "label": "代码", "width": 96, "anchor": "w", "kind": "code"},
    {"key": "name", "label": "名称", "width": 96, "anchor": "w", "kind": "text"},
    {"key": "qty", "label": "数量", "width": 76, "anchor": "e", "kind": "int"},
    {"key": "cost", "label": "成本", "width": 82, "anchor": "e", "kind": "money"},
    {"key": "price", "label": "现价", "width": 82, "anchor": "e", "kind": "money"},
    {"key": "market_value", "label": "市值", "width": 106, "anchor": "e", "kind": "money"},
    {"key": "total_pnl", "label": "浮盈", "width": 96, "anchor": "e", "kind": "money"},
]

# (key, 标题, 类型, 说明, 着色方式)
METRIC_SPECS: List[Tuple[str, str, str, str, str]] = [
    ("total_return_pct", "累计收益", "pct", "区间累计", "auto"),
    ("annual_return_pct", "年化收益", "pct", "按交易日折算", "auto"),
    ("max_drawdown_pct", "最大回撤", "pct", "峰值到谷底", "invert"),
    ("sharpe", "夏普比率", "num", "年化，无风险 0", "auto"),
    ("sortino", "索提诺比率", "num", "只罚下行波动", "auto"),
    ("calmar", "卡玛比率", "num", "年化 / 最大回撤", "auto"),
    ("volatility_pct", "年化波动", "pct", "收益标准差", "none"),
    ("win_rate_pct", "胜率", "pct", "已实现盈亏口径", "auto"),
    ("trade_count", "交易次数", "int", "买卖合计", "none"),
    ("profit_loss_ratio", "盈亏比", "num", "平均盈利 / 平均亏损", "auto"),
    ("turnover_pct", "换手率", "pct", "成交额 / 资金", "none"),
    ("benchmark_return_pct", "基准收益", "pct", "同期基准", "auto"),
    ("alpha_pct", "Alpha", "pct", "年化超额", "auto"),
    ("beta", "Beta", "num", "相对基准", "auto"),
    ("total_fee", "总费用", "money", "佣金 + 印花税 + 过户费", "none"),
    ("trading_days", "交易日", "int", "区间内", "none"),
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


def _default_start() -> str:
    today = datetime.date.today()
    try:
        start = today.replace(year=today.year - 2)
    except ValueError:                                # 2 月 29 日
        start = today.replace(year=today.year - 2, day=28)
    return start.strftime("%Y-%m-%d")


class BacktestView(BaseView):
    """策略回测与绩效分析页。"""

    title = "回测分析"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._mods: Dict[str, Any] = {}
        self._missing: List[str] = []
        self._degraded = False
        self._strategies: List[Dict[str, Any]] = []
        self._row_by_id: Dict[str, Dict[str, Any]] = {}
        self._display_rows: List[Dict[str, Any]] = []
        self._watchlist: List[str] = []
        self._symbol_vars: Dict[str, Any] = {}
        self._symbol_widgets: Dict[str, Any] = {}
        self._selected_id = ""
        self._running = False
        self._task_id = 0
        self._suppress_select = False
        self._cards: Dict[str, Any] = {}
        self._charts: Dict[str, Any] = {}

        self._strategy_label = tk.StringVar(master=self, value="（请先在左侧选择策略）")
        self._strategy_hint = tk.StringVar(master=self, value="")
        self._start_var = tk.StringVar(master=self, value=_default_start())
        self._end_var = tk.StringVar(master=self, value="")
        self._cash_var = tk.StringVar(master=self, value="1000000")
        self._bench_var = tk.StringVar(master=self, value=BENCHMARKS[0])
        self._symbols_var = tk.StringVar(master=self, value="")
        self._slippage_var = tk.StringVar(master=self, value="2")
        self._commission_var = tk.StringVar(master=self, value="2.5")
        self._flow_fee_var = tk.StringVar(master=self, value="0")
        self._ticks_var = tk.StringVar(master=self, value="0")
        self._meta_var = tk.StringVar(master=self, value="")
        self._warn_var = tk.StringVar(master=self, value="")
        self._hint_var = tk.StringVar(master=self, value="选择策略并点击「运行回测」")

    # ------------------------------------------------------------------ 构建
    def build(self) -> None:
        self._mods, self._missing = load_widgets("cards", "table", "charts")
        if self._mods.get("cards") is None or self._mods.get("table") is None:
            self._degraded = True
            self._build_inline_error(
                "回测页面缺少控件模块：%s\n请确认 quantstudio_desktop/widgets/ 下的 "
                "cards.py 与 table.py 已就位。" % "、".join(self._missing))
            return
        try:
            self._build_ui()
        except Exception as exc:                      # noqa: BLE001
            self._degraded = True
            self._build_inline_error("回测页面构建失败：%s" % exc)

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

    def _mount(self, widget: Any, row: int = 0, column: int = 0, **grid_options: Any) -> Any:
        try:
            if widget.winfo_manager():
                return widget
        except Exception:                             # noqa: BLE001
            return widget
        try:
            widget.grid(row=row, column=column, **grid_options)
        except Exception:                             # noqa: BLE001
            self._place(widget)
        return widget

    def _build_ui(self) -> None:
        table_mod = self._mods["table"]

        main = ttk.Frame(self, style="TFrame")
        main.pack(fill="both", expand=True)
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=0, minsize=272)
        main.columnconfigure(1, weight=1)

        # ---- 左：策略列表
        left = ttk.Frame(main, style="Card.TFrame", padding=(8, 8))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        ttk.Label(left, text="策略", style="H2.TLabel").pack(anchor="w", pady=(0, 6))
        self._table = table_mod.DataTable(left, STRATEGY_COLUMNS, height=22,
                                          on_select=self._on_pick, sortable=False)
        self._place(self._table, fill="both", expand=True)
        ttk.Label(left, text="单选策略后，右侧参数即时跟随",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(6, 0))

        # ---- 右：滚动区（参数 + 结果）
        inner = self._build_scroll_area(main)
        self._build_params(inner)
        self._build_results(inner)

    def _build_scroll_area(self, parent: Any) -> Any:
        holder = ttk.Frame(parent, style="TFrame")
        holder.grid(row=0, column=1, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(holder, bg=theme.COLORS["bg"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=bar.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        inner = ttk.Frame(self._canvas, style="TFrame")
        self._window = self._canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._window, width=e.width))
        try:                                          # 滚轮：绑在顶层窗口，处理时再判断指针位置
            self.winfo_toplevel().bind("<MouseWheel>", self._on_wheel, add="+")
            self.winfo_toplevel().bind("<Button-4>", self._on_wheel, add="+")
            self.winfo_toplevel().bind("<Button-5>", self._on_wheel, add="+")
        except Exception:                             # noqa: BLE001
            pass
        return inner

    def _on_wheel(self, event: Any) -> None:
        if self._degraded or not hasattr(self, "_canvas"):
            return
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except Exception:                             # noqa: BLE001
            return
        node = widget
        while node is not None and node is not self._canvas:
            node = getattr(node, "master", None)
        if node is None:
            return
        num = getattr(event, "num", None)
        if num == 4:
            step = -1
        elif num == 5:
            step = 1
        else:
            delta = getattr(event, "delta", 0) or 0
            if not delta:
                return
            step = -1 if delta > 0 else 1
        try:
            self._canvas.yview_scroll(step, "units")
        except Exception:                             # noqa: BLE001
            pass

    def _build_params(self, parent: Any) -> None:
        cards = self._mods["cards"]
        SectionTitle = widget_class(self._mods, "cards", "SectionTitle")
        if SectionTitle is not None:
            self._place(SectionTitle(parent, "回测参数", hint="耗时操作在后台执行，不会卡住界面"),
                        fill="x")
        form = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        self._place(form, fill="x", pady=(4, 12))

        ttk.Label(form, text="策略", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(form, textvariable=self._strategy_label, style="Card.TLabel").grid(
            row=0, column=1, columnspan=3, sticky="w", padx=(8, 0))
        ttk.Label(form, textvariable=self._strategy_hint, style="CardMuted.TLabel").grid(
            row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(0, 6))

        ttk.Label(form, text="起始日期", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self._start_var, width=14).grid(
            row=2, column=1, sticky="w", padx=(8, 16), pady=2)
        ttk.Label(form, text="结束日期", style="CardMuted.TLabel").grid(row=2, column=2, sticky="w")
        ttk.Entry(form, textvariable=self._end_var, width=14).grid(
            row=2, column=3, sticky="w", padx=(8, 0), pady=2)
        ttk.Label(form, text="留空 = 最近交易日", style="CardMuted.TLabel").grid(
            row=3, column=3, sticky="w", padx=(8, 0))

        ttk.Label(form, text="初始资金", style="CardMuted.TLabel").grid(row=4, column=0, sticky="w")
        ttk.Entry(form, textvariable=self._cash_var, width=14).grid(
            row=4, column=1, sticky="w", padx=(8, 16), pady=2)
        ttk.Label(form, text="基准", style="CardMuted.TLabel").grid(row=4, column=2, sticky="w")
        ttk.Combobox(form, textvariable=self._bench_var, values=BENCHMARKS, state="readonly",
                     width=12).grid(row=4, column=3, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(form, text="滑点（bp）", style="CardMuted.TLabel").grid(row=5, column=0, sticky="w")
        ttk.Entry(form, textvariable=self._slippage_var, width=14).grid(
            row=5, column=1, sticky="w", padx=(8, 16), pady=2)
        ttk.Label(form, text="佣金（万分之）", style="CardMuted.TLabel").grid(row=5, column=2,
                                                                        sticky="w")
        ttk.Entry(form, textvariable=self._commission_var, width=14).grid(
            row=5, column=3, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(form, text="流量费（元/笔）", style="CardMuted.TLabel").grid(row=6, column=0, sticky="w")
        ttk.Entry(form, textvariable=self._flow_fee_var, width=14).grid(
            row=6, column=1, sticky="w", padx=(8, 16), pady=2)
        ttk.Label(form, text="滑点（跳数）", style="CardMuted.TLabel").grid(row=6, column=2, sticky="w")
        ttk.Entry(form, textvariable=self._ticks_var, width=14).grid(
            row=6, column=3, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(form, text="标的池", style="CardMuted.TLabel").grid(row=7, column=0, sticky="nw",
                                                                    pady=(8, 0))
        head = ttk.Frame(form, style="Card.TFrame")
        head.grid(row=7, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Button(head, text="全选", command=lambda: self._toggle_all(True)).pack(side="left")
        ttk.Button(head, text="全不选", command=lambda: self._toggle_all(False)).pack(side="left",
                                                                                    padx=6)
        ttk.Label(head, text="默认全选自选池；下方可手填补充（逗号分隔）",
                  style="CardMuted.TLabel").pack(side="left", padx=8)
        self._symbol_box = ttk.Frame(form, style="Card.TFrame")
        self._symbol_box.grid(row=8, column=1, columnspan=3, sticky="ew", padx=(8, 0))

        ttk.Label(form, text="手填标的", style="CardMuted.TLabel").grid(row=9, column=0, sticky="w",
                                                                     pady=(6, 0))
        ttk.Entry(form, textvariable=self._symbols_var).grid(row=9, column=1, columnspan=3,
                                                            sticky="ew", padx=(8, 0), pady=(6, 0))
        ttk.Label(form, text="例：600519.SH, 000858.SZ（与勾选合并，代码会自动转大写）",
                  style="CardMuted.TLabel").grid(row=10, column=1, columnspan=3, sticky="w",
                                                padx=(8, 0))

        actions = ttk.Frame(form, style="Card.TFrame")
        actions.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self._run_btn = ttk.Button(actions, text="运行回测", style="Primary.TButton",
                                   command=self._run_backtest)
        self._run_btn.pack(side="left")
        self._reset_btn = ttk.Button(actions, text="恢复默认", command=self._restore_defaults)
        self._reset_btn.pack(side="left", padx=8)

    def _build_results(self, parent: Any) -> None:
        cards = self._mods["cards"]
        SectionTitle = widget_class(self._mods, "cards", "SectionTitle")
        CardGrid = widget_class(self._mods, "cards", "CardGrid")
        StatCard = widget_class(self._mods, "cards", "StatCard")

        if SectionTitle is not None:
            self._place(SectionTitle(parent, "回测结果", hint="指标口径与核心引擎一致"), fill="x")
        meta = ttk.Label(parent, textvariable=self._meta_var, style="Muted.TLabel")
        self._place(meta, fill="x", pady=(4, 0))
        warn = tk.Label(parent, textvariable=self._warn_var, bg=theme.COLORS["bg"],
                        fg=theme.COLORS["warn"], font=theme.font(9), anchor="w", justify="left",
                        wraplength=900)
        self._place(warn, fill="x", pady=(2, 0))
        self._warn_label = warn
        hint = ttk.Label(parent, textvariable=self._hint_var, style="Muted.TLabel")
        self._place(hint, fill="x", pady=(2, 6))

        # ---- 指标卡（16）
        if CardGrid is not None and StatCard is not None:
            self._metric_grid = CardGrid(parent, columns=4, gap=8)
            self._place(self._metric_grid, fill="x", pady=(4, 12))
            for index, (key, title, _kind, sub, _color) in enumerate(METRIC_SPECS):
                try:
                    card = StatCard(self._metric_grid, title, value="—", sub=sub)
                except Exception as exc:              # noqa: BLE001
                    self.status("指标卡创建失败：%s" % exc)
                    break
                self._cards[key] = card
                self._grid_add(self._metric_grid, card, index)
        else:
            self._metric_grid = None

        charts = self._mods.get("charts")
        MultiLineChart = widget_class(self._mods, "charts", "MultiLineChart")
        LineChart = widget_class(self._mods, "charts", "LineChart")
        BarChart = widget_class(self._mods, "charts", "BarChart")

        if charts is None or MultiLineChart is None:
            self._place(ttk.Label(parent, text="图表控件不可用：%s（结果为数值形态展示）"
                                  % "、".join(self._missing), style="Muted.TLabel"), fill="x")
        else:
            self._section(parent, "净值曲线", "策略 vs 基准（归一化）", pady=(0, 4))
            self._charts["nav"] = self._make_chart(MultiLineChart, parent, 260, "净值曲线不可用")
            if LineChart is not None:
                self._section(parent, "每日收益", "策略逐日收益（%）")
                self._charts["daily"] = self._make_chart(LineChart, parent, 200, "收益曲线不可用")
                self._section(parent, "回撤", "取负值显示，越深越差")
                self._charts["dd"] = self._make_chart(LineChart, parent, 240, "回撤曲线不可用")
            if BarChart is not None:
                self._section(parent, "月度收益", "按自然月聚合（%）")
                self._charts["monthly"] = self._make_chart(BarChart, parent, 240, "月度收益不可用")

        # ---- 交易记录 / 期末持仓
        table_mod = self._mods["table"]
        self._section(parent, "交易记录", "含费用与已实现盈亏", pady=(12, 4))
        self._trades = table_mod.DataTable(parent, TRADE_COLUMNS, height=10, sortable=True)
        self._place(self._trades, fill="x")
        self._section(parent, "期末持仓", "回测最后一日的持仓快照", pady=(12, 4))
        self._positions = table_mod.DataTable(parent, POSITION_COLUMNS, height=8, sortable=True)
        self._place(self._positions, fill="x", pady=(0, 12))

    def _section(self, parent: Any, text: str, hint: str = "", pady: Any = (10, 0)) -> Any:
        """小节标题（SectionTitle 缺失时退化为普通标签）。"""
        SectionTitle = widget_class(self._mods, "cards", "SectionTitle")
        if SectionTitle is None:
            return self._place(ttk.Label(parent, text=text, style="Muted.TLabel"), fill="x",
                               pady=pady)
        try:
            widget = SectionTitle(parent, text, hint=hint)
        except Exception:                             # noqa: BLE001
            widget = ttk.Label(parent, text=text, style="Muted.TLabel")
        return self._place(widget, fill="x", pady=pady)

    def _make_chart(self, ChartClass: Any, parent: Any, height: int, error_text: str) -> Any:

        try:
            chart = ChartClass(parent, height=height)
        except Exception as exc:                      # noqa: BLE001
            self.status("%s：%s" % (error_text, exc))
            return None
        self._place(chart, fill="x", pady=(4, 0))
        return chart

    def _grid_add(self, grid: Any, card: Any, index: int) -> None:
        """优先用 CardGrid.add；没有该方法时按 4 列自行排版。"""
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

    # ------------------------------------------------------------------ 数据
    def reload(self) -> None:
        if self._degraded:
            return
        self.status("正在加载策略列表与自选池…")
        self.load(self._snapshot, on_done=self._on_snapshot, on_error=self._on_snapshot_error,
                  name="backtest-config")

    def _snapshot(self) -> Dict[str, Any]:
        strategies = self.services.strategies()
        notes: List[str] = []
        try:
            watchlist = self.services.watchlist()
        except Exception as exc:                      # noqa: BLE001 - 自选池失败不影响策略选择
            watchlist = []
            notes.append("自选池读取失败：%s" % exc)
        return {"strategies": strategies, "watchlist": watchlist, "notes": notes}

    def _on_snapshot_error(self, exc: BaseException) -> None:
        self.toast_error(exc)
        self._hint_var.set("配置加载失败：%s" % exc)

    def _on_snapshot(self, data: Any) -> None:
        data = data if isinstance(data, dict) else {}
        rows = data.get("strategies")
        self._strategies = [item for item in rows if isinstance(item, dict)] \
            if isinstance(rows, list) else []
        self._row_by_id = {str(item.get("id") or ""): item for item in self._strategies}
        self._watchlist = [str(code) for code in (data.get("watchlist") or []) if str(code).strip()]
        for note in data.get("notes") or []:
            self.status(str(note))
        self._render_strategy_table()
        self._render_symbols()
        if not self._selected_id and self._strategies:
            self._select_index(0)
        self.status("回测参数已就绪：%d 个策略 / %d 个自选标的"
                    % (len(self._strategies), len(self._watchlist)))
        pending = getattr(self.app, "pending_backtest_strategy", None)
        if pending:
            try:
                delattr(self.app, "pending_backtest_strategy")
            except Exception:                         # noqa: BLE001
                try:
                    self.app.pending_backtest_strategy = None
                except Exception:                     # noqa: BLE001
                    pass
            self._handle_pending(str(pending))

    def _handle_pending(self, strategy_id: str) -> None:
        index = -1
        for position, item in enumerate(self._display_rows):
            if str(item.get("id") or "") == strategy_id:
                index = position
                break
        if index < 0:
            self._hint_var.set("待回测策略不存在：%s" % strategy_id)
            self.status("跳转失败：找不到策略 %s" % strategy_id)
            return
        self._select_index(index)
        self.status("正在回测（来自策略管理跳转）：%s" % strategy_id)
        self._run_backtest()

    def _select_index(self, index: int) -> None:
        try:
            self._table.select_index(index, notify=False)
        except Exception as exc:                      # noqa: BLE001
            self.status("选中策略失败：%s" % exc)
        if 0 <= index < len(self._display_rows):
            self._apply_selection(self._display_rows[index])

    def _render_strategy_table(self) -> None:
        rows: List[Dict[str, Any]] = []
        for item in self._strategies:
            origin = str(item.get("origin") or ("builtin" if item.get("builtin") else "user"))
            row = dict(item)
            row["origin"] = origin
            row["origin_text"] = ORIGIN_LABELS.get(origin, origin or "—")
            row["status_text"] = STATUS_LABELS.get(str(item.get("status") or ""), "—")
            row["min_bars"] = int(_number(item.get("min_bars"), 0) or 0)
            row["name"] = _text(item.get("name"), _text(item.get("id")))
            rows.append(row)
        self._display_rows = rows
        wanted = self._selected_id
        self._suppress_select = True
        try:
            self._table.set_rows(rows, key_field="id", preserve_selection=True)
        except Exception as exc:                      # noqa: BLE001
            self.status("策略列表渲染失败：%s" % exc)
        finally:
            self._suppress_select = False
        self._restore_pick(wanted)

    def _restore_pick(self, wanted: str) -> None:
        """刷新后按 id 恢复选中项（与左表高亮保持一致）。"""
        if not wanted:
            return
        for index, row in enumerate(self._display_rows):
            if str(row.get("id") or "") == wanted:
                try:
                    self._table.select_index(index, notify=False)
                except Exception:                     # noqa: BLE001
                    pass
                self._apply_selection(self._row_by_id.get(wanted) or row)
                return

    def _on_pick(self, *_args: Any) -> None:
        if self._degraded or self._suppress_select:
            return
        value = None
        try:
            value = self._table.selected()
        except Exception:                             # noqa: BLE001
            value = None
        row = self._row_from_selection(value)
        if row is not None:
            self._apply_selection(row)

    def _row_from_selection(self, value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        if isinstance(value, dict):
            sid = str(value.get("id") or "")
            return self._row_by_id.get(sid) or (value if value.get("id") else None)
        if isinstance(value, (list, tuple)):
            for item in value:
                found = self._row_from_selection(item)
                if found is not None:
                    return found
            return None
        text = str(value)
        if text in self._row_by_id:
            return self._row_by_id[text]
        try:
            index = int(text)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(self._display_rows):
            return self._display_rows[index]
        for row in self._display_rows:
            if str(row.get("name")) == text:
                return row
        return None

    def _apply_selection(self, row: Dict[str, Any]) -> None:
        self._selected_id = str(row.get("id") or "")
        name = _text(row.get("name"), self._selected_id)
        self._strategy_label.set("%s（%s）" % (name, self._selected_id))
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        summary = "类别 %s ｜ 频率 %s ｜ 参数 %d 个 ｜ 最少 %s bar" % (
            _text(row.get("category")), _text(row.get("freq")), len(params),
            _text(row.get("min_bars"), "0"))
        if params:
            summary += "\n默认参数：" + "，".join(
                "%s=%s" % (key, theme.fmt_num(value, 4)) for key, value in list(params.items())[:6])
        self._strategy_hint.set(summary)

    # ------------------------------------------------------------------ 标的池
    def _render_symbols(self) -> None:
        if not hasattr(self, "_symbol_box"):
            return
        codes = self._watchlist[:MAX_SYMBOL_CHECKS]
        if not hasattr(self, "_symbol_note"):
            self._symbol_note = tk.StringVar(master=self, value="")
            note = ttk.Label(self._symbol_box, textvariable=self._symbol_note,
                             style="CardMuted.TLabel")
            try:
                note.grid(row=100, column=0, columnspan=DEFAULT_SYMBOL_SLOTS, sticky="w")
            except Exception:                         # noqa: BLE001
                self._place(note, anchor="w")
        for code in codes:
            if code in self._symbol_widgets:
                continue
            var = tk.BooleanVar(master=self, value=True)
            widget = ttk.Checkbutton(self._symbol_box, text=code, variable=var,
                                     style="TCheckbutton")
            self._symbol_vars[code] = var
            self._symbol_widgets[code] = widget
        for index, code in enumerate(codes):
            widget = self._symbol_widgets.get(code)
            if widget is None:
                continue
            try:
                widget.grid_forget()
                widget.grid(row=index // DEFAULT_SYMBOL_SLOTS, column=index % DEFAULT_SYMBOL_SLOTS,
                            sticky="w", padx=2, pady=1)
            except Exception:                         # noqa: BLE001
                pass
        if not codes:
            self._symbol_note.set("（自选池为空，可在下方手填代码）")
        elif len(self._watchlist) > MAX_SYMBOL_CHECKS:
            self._symbol_note.set("（仅显示前 %d 个，其余请在下方手填）" % MAX_SYMBOL_CHECKS)
        else:
            self._symbol_note.set("")

    def _toggle_all(self, value: bool) -> None:
        for code in self._symbol_vars:
            try:
                self._symbol_vars[code].set(bool(value))
            except Exception:                         # noqa: BLE001
                pass

    def _collect_symbols(self) -> List[str]:
        codes: List[str] = []
        for code in self._watchlist:
            var = self._symbol_vars.get(code)
            if var is not None and var.get():
                codes.append(code)
        manual = self._symbols_var.get() or ""
        for part in re.split(r"[,，;；\s]+", manual):
            code = part.strip().upper()
            if code and code not in codes:
                codes.append(code)
        return codes

    # ------------------------------------------------------------------ 运行
    def _restore_defaults(self) -> None:
        self._start_var.set(_default_start())
        self._end_var.set("")
        self._cash_var.set("1000000")
        self._bench_var.set(BENCHMARKS[0])
        self._symbols_var.set("")
        self._slippage_var.set("2")
        self._commission_var.set("2.5")
        self._flow_fee_var.set("0")
        self._ticks_var.set("0")
        self._toggle_all(True)
        self._hint_var.set("参数已恢复默认")

    def _collect_params(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """把表单组装成服务层 ``params``；返回 (params, 错误信息)。"""
        problems: List[str] = []
        start = (self._start_var.get() or "").strip()
        end = (self._end_var.get() or "").strip()
        for label, text in (("起始日期", start), ("结束日期", end)):
            if text and not _DATE_RE.match(text):
                problems.append("%s 需为 YYYY-MM-DD 格式（当前 %r）" % (label, text))
        if not problems and start and end and start > end:
            problems.append("起始日期不能晚于结束日期")
        cash = _number(self._cash_var.get())
        if cash is None or cash <= 0:
            problems.append("初始资金需为正数（当前 %r）" % self._cash_var.get())
        slippage = _number(self._slippage_var.get())
        if slippage is None or slippage < 0:
            problems.append("滑点需为非负数，单位 bp（当前 %r）" % self._slippage_var.get())
        commission_wan = _number(self._commission_var.get())
        if commission_wan is None or commission_wan < 0:
            problems.append("佣金费率需为非负数，单位万分之（当前 %r）" % self._commission_var.get())
        flow_fee = _number(self._flow_fee_var.get())
        if flow_fee is None or flow_fee < 0:
            problems.append("流量费需为非负数，单位元/笔（当前 %r）" % self._flow_fee_var.get())
        ticks = _number(self._ticks_var.get())
        if ticks is None or ticks < 0:
            problems.append("滑点跳数需为非负数，单位跳（当前 %r）" % self._ticks_var.get())
        if problems:
            return None, "；".join(problems)
        symbols = self._collect_symbols()
        params: Dict[str, Any] = {
            "start": start,
            "end": end,
            "cash": float(cash),
            "benchmark": (self._bench_var.get() or BENCHMARKS[0]).strip() or BENCHMARKS[0],
            "slippage_bps": float(slippage),
            "commission_rate": float(commission_wan) / 10000.0,
            "flow_fee": float(flow_fee),
            "slippage_ticks": float(ticks),
        }
        if symbols:
            params["symbols"] = symbols
        return params, ""

    def _run_backtest(self) -> None:
        if self._degraded or self._running:
            return
        if not self._selected_id:
            self._hint_var.set("请先在左侧选择一个策略")
            self.status("请先选择策略")
            return
        params, problem = self._collect_params()
        if params is None:
            self._hint_var.set("参数有问题：%s" % problem)
            self.toast_error(ValueError(problem))
            return
        self._set_running(True)
        self._hint_var.set("正在回测：%s …（数据量大时需要十几秒）" % self._selected_id)
        self.status("正在运行回测：%s ｜ 参数 %s" % (self._selected_id, params))
        self._task_id = self.load(self.services.run_backtest, self._selected_id, params,
                                  on_done=self._on_result, on_error=self._on_run_error,
                                  name="backtest")

    def _set_running(self, running: bool) -> None:
        self._running = running
        state = "disabled" if running else "normal"
        for button in (getattr(self, "_run_btn", None), getattr(self, "_reset_btn", None)):
            if button is None:
                continue
            try:
                button.configure(state=state)
            except Exception:                         # noqa: BLE001
                pass

    def _on_run_error(self, exc: BaseException) -> None:
        self._set_running(False)
        self.toast_error(exc)
        self._hint_var.set("回测失败：%s" % exc)
        self._meta_var.set("")
        self._warn_var.set("")
        self.status("回测失败：%s" % exc)

    def _on_result(self, data: Any) -> None:
        self._set_running(False)
        if not isinstance(data, dict) or not data:
            self._hint_var.set("回测没有返回结果，请稍后重试")
            return
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        if not metrics and not data.get("nav"):
            self._hint_var.set("回测结果为空：可能区间内没有可用行情")
            self._meta_var.set("")
            self._warn_var.set("")
            return
        try:
            self._render_metrics(metrics)
            self._render_charts(data)
            self._render_trades(data.get("trades"))
            self._render_positions(data.get("positions"))
        except Exception as exc:                      # noqa: BLE001 - 渲染异常不崩页面
            self.toast_error(exc)
            self._hint_var.set("结果渲染失败：%s" % exc)
            return
        warnings = [str(item) for item in (data.get("warnings") or []) if str(item).strip()]
        self._warn_var.set("；".join(warnings))
        self._render_meta(data)
        self._hint_var.set("回测完成：%s" % datetime.datetime.now().strftime("%H:%M:%S"))
        self.status("回测完成：%s ｜ 交易日 %s ｜ 交易 %s 次"
                    % (self._selected_id, metrics.get("trading_days"), metrics.get("trade_count")))

    def _render_meta(self, data: Dict[str, Any]) -> None:
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        span = data.get("range") if isinstance(data.get("range"), dict) else {}
        start = span.get("start") or metrics.get("start") or "—"
        end = span.get("end") or metrics.get("end") or "—"
        strategy = data.get("strategy") if isinstance(data.get("strategy"), dict) else {}
        request = data.get("request") if isinstance(data.get("request"), dict) else {}
        symbols = request.get("symbols") if isinstance(request.get("symbols"), list) else []
        fee = request.get("fee") if isinstance(request.get("fee"), dict) else {}
        flow_fee = _number(fee.get("flow_fee"), 0.0) or 0.0
        ticks = _number(fee.get("slippage_ticks"), 0.0) or 0.0
        fee_extra = ""
        if flow_fee:
            fee_extra += " ｜ 流量费 %s 元/笔" % theme.fmt_num(flow_fee, 2)
        if ticks:
            fee_extra += " ｜ 跳数滑点 %s 跳" % theme.fmt_num(ticks, 0)
        self._meta_var.set(
            "区间 %s ~ %s ｜ 策略 %s ｜ 初始资金 %s 元 ｜ 基准 %s ｜ 标的 %s ｜ 佣金 %.5f/滑点 %s bp%s"
            % (start, end, _text(strategy.get("name"), self._selected_id),
               theme.fmt_money(request.get("initial_cash")), _text(request.get("benchmark")),
               ("、".join(str(code) for code in symbols) or "策略默认"),
               _number(fee.get("commission_rate"), 0.0) or 0.0, _text(fee.get("slippage_bps"), "0"),
               fee_extra))

    def _render_metrics(self, metrics: Dict[str, Any]) -> None:
        dd_start = _text(metrics.get("max_drawdown_start"), "")
        dd_end = _text(metrics.get("max_drawdown_end"), "")
        for key, _title, kind, sub, color in METRIC_SPECS:
            card = self._cards.get(key)
            if card is None:
                continue
            raw = metrics.get(key)
            number = _number(raw)
            extra = sub
            if key == "max_drawdown_pct" and (dd_start or dd_end):
                extra = "%s → %s" % (dd_start or "—", dd_end or "—")
            try:
                card.set(self._format_value(kind, raw), color_by=self._color_by(color, number),
                         sub=extra)
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
        return theme.fmt_num(raw, 2)

    @staticmethod
    def _color_by(mode: str, number: Optional[float]) -> Optional[float]:
        if mode == "none" or number is None:
            return None
        if mode == "invert":
            return -abs(number)
        return number

    def _render_charts(self, data: Dict[str, Any]) -> None:
        nav = [row for row in (data.get("nav") or []) if isinstance(row, dict)]
        dates = [str(row.get("date") or "") for row in nav]
        strategy = [_number(row.get("strategy")) for row in nav]
        benchmark = [_number(row.get("benchmark")) for row in nav]
        self._set_chart("nav", [
            {"name": "策略净值", "values": strategy, "color": theme.COLORS["brand"]},
            {"name": "基准净值", "values": benchmark, "dashed": True,
             "color": theme.COLORS["text2"]},
        ], dates=dates, kind="number")

        daily: List[Optional[float]] = []
        daily_dates: List[str] = []
        previous = None
        for index, value in enumerate(strategy):
            if previous not in (None, 0) and value is not None:
                daily.append((value / previous - 1.0) * 100.0)
                daily_dates.append(dates[index])
            previous = value if value is not None else previous
        self._set_chart("daily", daily, dates=daily_dates, kind="pct")

        drawdown = [row for row in (data.get("drawdown") or []) if isinstance(row, dict)]
        dd_dates = [str(row.get("date") or "") for row in drawdown]
        dd_values = []
        for row in drawdown:
            value = _number(row.get("dd_pct"))
            dd_values.append(None if value is None else -abs(value))
        self._set_chart("dd", dd_values, dates=dd_dates, kind="pct")

        monthly = [row for row in (data.get("monthly") or []) if isinstance(row, dict)]
        labels = [str(row.get("month") or "") for row in monthly]
        values = [_number(row.get("ret_pct")) for row in monthly]
        self._set_chart("monthly", labels, values, kind="pct")

    def _set_chart(self, key: str, *args: Any, **kwargs: Any) -> None:
        chart = self._charts.get(key)
        if chart is None:
            return
        setter = getattr(chart, "set_data", None)
        if not callable(setter):
            return
        try:
            setter(*args, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            self.status("%s 图表渲染失败：%s" % (key, exc))

    def _render_trades(self, rows: Any) -> None:
        items = rows if isinstance(rows, list) else []
        out: List[Dict[str, Any]] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            data = dict(row)
            data["side_text"] = "买入" if str(row.get("side")) == "buy" else "卖出"
            out.append(data)
        try:
            self._trades.set_rows(out, key_field=None)
        except Exception as exc:                      # noqa: BLE001
            self.status("交易记录渲染失败：%s" % exc)

    def _render_positions(self, rows: Any) -> None:
        items = rows if isinstance(rows, list) else []
        out = [dict(row) for row in items if isinstance(row, dict)]
        try:
            self._positions.set_rows(out, key_field="code")
        except Exception as exc:                      # noqa: BLE001
            self.status("期末持仓渲染失败：%s" % exc)


__all__ = ["BacktestView"]


