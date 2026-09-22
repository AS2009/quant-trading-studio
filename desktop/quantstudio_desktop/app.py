# -*- coding: utf-8 -*-
"""主窗口：菜单栏 / 顶栏（数据源徽标）/ 左侧导航 / 内容区 / 状态栏 / 提示层。

页面（views）只需实现 ``build()`` 与 ``reload()``，并通过 ``self.tasks`` 做后台调用；
窗口负责：懒加载页面、快捷键、数据源状态轮询、后台任务进度、异常提示、退出清理。
"""

import os
import sys
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from . import __version__, boot, theme, widgets
from .services import GuiServices, TaskRunner
from .views import VIEW_SPECS, BaseView, load_view_class

APP_TITLE = "QuantTrading Studio 量化交易工作台"
WINDOW_MIN = (1080, 680)
WINDOW_DEFAULT = (1360, 860)


class DesktopApp(tk.Tk):
    """应用主窗口。"""

    def __init__(self, settings: Any = None, start_view: str = "market"):
        super().__init__()
        self.settings = settings
        self.services = GuiServices(settings)
        self.tasks = TaskRunner(self, max_workers=3)
        self.tasks.on_error = self._on_task_error
        self.tasks.on_busy_change = self._on_busy_change

        self._views: Dict[str, BaseView] = {}
        self._nav_buttons: Dict[str, ttk.Button] = {}
        self.current_key = ""
        self._status_text = ""
        self._badge_state: Dict[str, Any] = {}
        self._status_polling = False        # 状态查询单飞标志（防「查询→完成→再查询」死循环）
        self._status_polled_at = 0.0        # 上次发起状态查询的时间戳

        self.title(APP_TITLE)
        self.minsize(*WINDOW_MIN)
        self._center(*WINDOW_DEFAULT)
        self.configure(bg=theme.COLORS["bg"])
        theme.init(self)

        self._build_menu()
        self._build_layout()
        self.toast = widgets.toast.Toast(self, host=self.toast_host)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_shortcuts()
        self.select_view(start_view)
        self._tick_clock()
        self._poll_status(first=True)

    # ------------------------------------------------------------------ 布局
    def _center(self, width: int, height: int) -> None:
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, int((screen_w - width) / 2))
        y = max(0, int((screen_h - height) / 3))
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        colors = theme.COLORS
        try:
            menubar.configure(bg=colors["bg2"], fg=colors["text"], activebackground=colors["select"],
                              activeforeground="#ffffff", borderwidth=0)
        except tk.TclError:
            pass

        file_menu = tk.Menu(menubar, tearoff=0, bg=colors["bg2"], fg=colors["text"],
                            activebackground=colors["select"], activeforeground="#ffffff")
        file_menu.add_command(label="刷新当前页", accelerator="Ctrl+R", command=self.refresh_current)
        file_menu.add_command(label="刷新数据源状态", command=lambda: self._poll_status(force=True))
        file_menu.add_separator()
        file_menu.add_command(label="打开数据目录", command=self.open_data_dir)
        file_menu.add_command(label="打开文档目录", command=self.open_docs_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", accelerator="Ctrl+Q", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0, bg=colors["bg2"], fg=colors["text"],
                            activebackground=colors["select"], activeforeground="#ffffff")
        for index, (key, title, _module, _cls) in enumerate(VIEW_SPECS, start=1):
            view_menu.add_command(label="%s\tCtrl+%d" % (title, index),
                                  command=lambda k=key: self.select_view(k))
        menubar.add_cascade(label="视图", menu=view_menu)

        tool_menu = tk.Menu(menubar, tearoff=0, bg=colors["bg2"], fg=colors["text"],
                            activebackground=colors["select"], activeforeground="#ffffff")
        tool_menu.add_command(label="数据源自检…", command=self.show_self_check)
        tool_menu.add_command(label="清空回测缓存", command=self.clear_backtest_cache)
        tool_menu.add_separator()
        tool_menu.add_command(label="重置模拟盘账户…", command=self.reset_paper_account)
        menubar.add_cascade(label="工具", menu=tool_menu)

        help_menu = tk.Menu(menubar, tearoff=0, bg=colors["bg2"], fg=colors["text"],
                            activebackground=colors["select"], activeforeground="#ffffff")
        help_menu.add_command(label="关于", command=self.show_about)
        help_menu.add_command(label="如何编写策略（文档）", command=lambda: self.open_docs_dir("strategy-spec.md"))
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=menubar)

    def _build_layout(self) -> None:
        # 顶栏
        header = ttk.Frame(self, style="Bar.TFrame", padding=(14, 10))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="QuantTrading Studio", style="Title.TLabel",
                  background=theme.COLORS["bg2"]).pack(side="left")
        ttk.Label(header, text="桌面版", style="Muted.TLabel",
                  background=theme.COLORS["bg2"]).pack(side="left", padx=(8, 0), pady=(6, 0))

        self.busy_bar = ttk.Progressbar(header, mode="indeterminate", length=90)
        self.badge = tk.Label(header, text="正在读取数据源…", bg=theme.COLORS["bg2"],
                              fg=theme.COLORS["text2"], font=theme.font(10), padx=10, pady=4)
        self.badge.pack(side="right")
        ttk.Button(header, text="刷新", command=self.refresh_current).pack(side="right", padx=8)
        ttk.Button(header, text="数据源详情", command=self.show_self_check).pack(side="right")

        # 主体：左侧导航 + 内容区
        body = ttk.Frame(self, style="TFrame")
        body.pack(side="top", fill="both", expand=True)
        self.nav = ttk.Frame(body, style="Bar.TFrame", width=188)
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        for key, title, _module, _cls in VIEW_SPECS:
            button = ttk.Button(self.nav, text=title, style="Nav.TButton",
                                command=lambda k=key: self.select_view(k))
            button.pack(fill="x", padx=8, pady=2)
            self._nav_buttons[key] = button
        ttk.Separator(self.nav).pack(fill="x", padx=10, pady=8)
        ttk.Label(self.nav, text="提示：耗时操作在后台执行\n（回测/行情抓取不会卡住界面）",
                  style="Muted.TLabel", background=theme.COLORS["bg2"],
                  justify="left").pack(fill="x", padx=12, pady=(0, 8))

        self.content = ttk.Frame(body, style="TFrame", padding=(12, 10))
        self.content.pack(side="left", fill="both", expand=True)

        # 底部：状态栏 + 提示层
        footer = ttk.Frame(self, style="Bar.TFrame", padding=(12, 6))
        footer.pack(side="bottom", fill="x")
        self.toast_host = tk.Frame(footer, bg=theme.COLORS["bg2"])
        self.toast_host.pack(side="right")
        self.source_label = tk.Label(footer, text="数据源：初始化中…", bg=theme.COLORS["bg2"],
                                     fg=theme.COLORS["text2"], font=theme.font(9))
        self.source_label.pack(side="left")
        self.status_label = tk.Label(footer, text="", bg=theme.COLORS["bg2"],
                                     fg=theme.COLORS["text2"], font=theme.font(9))
        self.status_label.pack(side="left", padx=(16, 0))
        self.clock_label = tk.Label(footer, text="", bg=theme.COLORS["bg2"],
                                    fg=theme.COLORS["text2"], font=theme.font(9, mono=True))
        self.clock_label.pack(side="right")

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-r>", lambda _e: self.refresh_current())
        self.bind("<F5>", lambda _e: self.refresh_current())
        self.bind("<Control-q>", lambda _e: self.on_close())
        for index, (key, _title, _m, _c) in enumerate(VIEW_SPECS, start=1):
            self.bind("<Control-Key-%d>" % index, lambda _e, k=key: self.select_view(k))

    # ------------------------------------------------------------------ 页面切换
    def select_view(self, key: str) -> None:
        if key not in dict((k, t) for k, t, _m, _c in VIEW_SPECS):
            return
        for other, button in self._nav_buttons.items():
            button.configure(style="NavActive.TButton" if other == key else "Nav.TButton")
        if self.current_key and self.current_key in self._views:
            self._views[self.current_key].pack_forget()
        view = self._views.get(key)
        if view is None:
            spec = next(item for item in VIEW_SPECS if item[0] == key)
            _key, title, module_name, class_name = spec
            try:
                view_class = load_view_class(module_name, class_name)
                view = view_class(self.content, self)
            except Exception as exc:                     # noqa: BLE001 - 单个页面失败不应导致整个程序不可用
                self.toast.show("页面「%s」加载失败：%s" % (title, exc), kind="error", timeout=0)
                placeholder = ttk.Frame(self.content, style="Card.TFrame", padding=18)
                ttk.Label(placeholder, text="页面加载失败：%s\n%s" % (title, exc),
                          style="Card.TLabel", wraplength=760, justify="left").pack(anchor="w")
                view = placeholder                                  # type: ignore[assignment]
            self._views[key] = view                            # type: ignore[assignment]
        view.pack(fill="both", expand=True)                    # type: ignore[attr-defined]
        self.current_key = key
        if isinstance(view, BaseView):
            view.refresh()
        self.status("已切换到：%s" % dict((k, t) for k, t, _m, _c in VIEW_SPECS)[key])

    def refresh_current(self) -> None:
        view = self._views.get(self.current_key)
        if isinstance(view, BaseView):
            view.refresh(force=True)
        self._poll_status(force=True)

    # ------------------------------------------------------------------ 状态与提示
    def status(self, text: str) -> None:
        self._status_text = text
        if hasattr(self, "status_label"):
            self.status_label.configure(text=text)

    def _tick_clock(self) -> None:
        import datetime

        now = datetime.datetime.now()
        self.clock_label.configure(text=now.strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _on_busy_change(self, busy: int) -> None:
        if busy > 0:
            if not self.busy_bar.winfo_manager():
                self.busy_bar.pack(side="right", padx=(0, 10))
            self.busy_bar.start(60)
            self.status("后台任务进行中（%d）…" % busy)
        else:
            self.busy_bar.stop()
            self.busy_bar.pack_forget()
            if self._status_text.startswith("后台任务进行中"):
                self.status("就绪")
            if time.time() - self._status_polled_at >= 5.0:
                self._poll_status(force=True)   # 节流：busy 频繁归零时不反复查询数据源

    def _on_task_error(self, exc: BaseException) -> None:
        self.toast.show(str(exc), kind="error", timeout=6000)

    def _poll_status(self, first: bool = False, force: bool = False) -> None:

        now = time.time()
        if self._status_polling:
            return                              # 单飞：已有一个查询在飞，直接返回
        if now - self._status_polled_at < (0.0 if first else (5.0 if force else 40.0)):
            return                              # 节流（非强制路径至少 40s 一次）
        self._status_polling = True
        self._status_polled_at = now

        def apply(summary: Dict[str, Any]) -> None:
            self._status_polling = False
            self._badge_state = summary
            mode = summary.get("mode")
            color = {"real": theme.COLORS["down"], "cache": theme.COLORS["warn"],
                     "offline": theme.COLORS["up"], "error": theme.COLORS["up"]}.get(
                         mode, theme.COLORS["text2"])
            self.badge.configure(text="● " + str(summary.get("text") or "数据源状态未知"), fg=color)
            detail = summary.get("as_of") or ""
            strategies = summary.get("strategies") or {}
            counts = ""
            if strategies:
                counts = " | 策略 %s（内置 %s / 本地 %s）" % (strategies.get("total"), strategies.get("builtin"),
                                                          strategies.get("local"))
            self.source_label.configure(text="数据时点：%s%s" % (detail or "—", counts))

        def failed(exc: BaseException) -> None:
            self._status_polling = False
            self.badge.configure(text="● 数据源不可用：%s" % exc, fg=theme.COLORS["up"])

        self.tasks.run(self.services.data_source_summary, on_done=apply, on_error=failed, name="status")
        self.after(60000, self._poll_status)    # 只在真正发起查询时续挂，定时器数量因此有界

    # ------------------------------------------------------------------ 工具菜单动作
    def show_about(self) -> None:
        info = boot.prepare()
        messagebox.showinfo(
            "关于 QuantTrading Studio",
            "%s\n\n版本：桌面版 %s / 核心 %s\nPython：%s\n打包运行：%s\n数据目录：\n%s\n\n"
            "行情来自公开接口（新浪/腾讯/东方财富），仅供个人研究学习；\n"
            "本程序默认只做研究与模拟交易，不会向券商发送真实委托。" % (
                APP_TITLE, __version__, _core_version(), info["python"],
                "是" if info["frozen"] else "否（源码运行）", info["data_dir"]),
            parent=self)

    def show_self_check(self) -> None:
        dialog = _TextDialog(self, "数据源与运行状态")
        dialog.set_text("正在自检，请稍候…")

        def collect() -> str:
            lines = []
            summary = self.services.data_source_summary()
            lines.append("运行模式：%s" % summary.get("mode"))
            lines.append("状态摘要：%s" % summary.get("text"))
            lines.append("数据时点：%s" % (summary.get("as_of") or "—"))
            lines.append("数据源链：%s" % (summary.get("sources") or "—"))
            lines.append("")
            try:
                status = self.services.system_status()
                provider = status.get("provider") or {}
                lines.append("当前数据源：%s" % provider.get("active"))
                for name, item in (provider.get("available") or {}).items():
                    if isinstance(item, dict):
                        lines.append("  - %-10s %s" % (name, item.get("detail") or item))
                    else:
                        lines.append("  - %-10s %s" % (name, item))
                market = status.get("market") or {}
                lines.append("")
                lines.append("最近交易日：%s（%s）" % (market.get("last_trading_day"),
                                                "已收盘" if market.get("is_closed") else "交易中"))
                strategies = status.get("strategies") or {}
                lines.append("策略数量：%(total)s（内置 %(builtin)s / 本地 %(local)s）" % {
                    "total": strategies.get("total", "?"), "builtin": strategies.get("builtin", "?"),
                    "local": strategies.get("local", "?")})
                local = status.get("local") or {}
                if local.get("loaded"):
                    lines.append("本地策略：%s" % "、".join(local.get("loaded") or []))
                for item in local.get("errors") or []:
                    lines.append("  ! %s：%s" % (item.get("file"), item.get("error")))
                trade = status.get("trade") or {}
                lines.append("交易通道：%s（模拟盘，不会真实下单）" % ((trade.get("brokers") or {}).get("paper", {})
                                                              .get("description", "paper")))
            except Exception as exc:                        # noqa: BLE001
                lines.append("读取详细状态失败：%s" % exc)
            lines.append("")
            lines.append("缓存目录：%s" % (self.services.settings.cache_dir,))
            lines.append("数据目录：%s" % (self.services.settings.data_dir,))
            return "\n".join(str(line) for line in lines)

        self.tasks.run(collect, on_done=dialog.set_text,
                       on_error=lambda exc: dialog.set_text("自检失败：%s" % exc), name="self-check")

    def clear_backtest_cache(self) -> None:
        self.tasks.run(self.services.clear_backtest_cache,
                       on_done=lambda info: self.toast.show("回测缓存已清空：%s" % info, kind="ok"),
                       on_error=self._on_task_error, name="clear-cache")

    def reset_paper_account(self) -> None:
        if not messagebox.askyesno("重置模拟盘",
                                   "将清空模拟盘账户的持仓、委托与成交记录，并恢复初始资金。\n确定继续？",
                                   parent=self):
            return
        self.tasks.run(self.services.reset_paper,
                       on_done=lambda account: (self.toast.show("模拟盘已重置：总资产 %s 元" % theme.fmt_money(
                           (account or {}).get("total_assets")), kind="ok"),
                                                self.select_view("trade") if "trade" in self._views else None),
                       on_error=self._on_task_error, name="reset-paper")

    def open_data_dir(self) -> None:
        self._open_path(self.services.settings.data_dir)

    def open_docs_dir(self, filename: str = "") -> None:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(repo_root, "docs", filename) if filename else os.path.join(repo_root, "docs")
        self._open_path(path)

    def _open_path(self, path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)                       # type: ignore[attr-defined]  # noqa: S606
            elif sys.platform == "darwin":
                os.system('open "%s"' % path)
            else:
                webbrowser.open("file://%s" % path)
        except Exception as exc:                          # noqa: BLE001
            self.toast.show("打开失败：%s" % exc, kind="error")

    # ------------------------------------------------------------------ 退出
    def on_close(self) -> None:
        try:
            self.tasks.shutdown()
        except Exception:                                 # noqa: BLE001
            pass
        try:
            self.destroy()
        except Exception:                                 # noqa: BLE001
            pass


class _TextDialog(tk.Toplevel):
    """只读文本对话框（自检、日志、策略详情复用）。"""

    def __init__(self, master: tk.Misc, title: str, width: int = 720, height: int = 460):
        super().__init__(master)
        self.title(title)
        self.configure(bg=theme.COLORS["bg"])
        self.transient(master)
        self.geometry("%dx%d" % (width, height))
        frame = ttk.Frame(self, style="TFrame", padding=10)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, bg=theme.COLORS["bg2"], fg=theme.COLORS["text"],
                            insertbackground=theme.COLORS["text"], relief="flat",
                            font=theme.font(10, mono=True), wrap="word", padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=(0, 10))
        self.bind("<Escape>", lambda _e: self.destroy())

    def set_text(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", str(content))
        self.text.configure(state="disabled")


def _core_version() -> str:
    try:
        import quantstudio

        return getattr(quantstudio, "__version__", "未知")
    except Exception:                                     # noqa: BLE001
        return "未找到"


def main(argv: Optional[list] = None) -> int:
    """命令行入口：GUI / 自检。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("用法: python -m quantstudio_desktop [--selftest] [--selftest-gui] [--version] [--view <key>]")
        print("  --selftest       无界面自检（数据源 + 回测 + 报告），退出码 0/1")
        print("  --selftest-gui   构建整个窗口与所有页面后立即销毁（CI 用）")
        return 0
    if "--version" in argv:
        print("QuantTrading Studio 桌面版 %s" % __version__)
        return 0
    if "--selftest" in argv:
        from .selftest import run_selftest

        return run_selftest()
    if "--selftest-gui" in argv:
        from .selftest import run_gui_selftest

        return run_gui_selftest()

    start_view = "market"
    if "--view" in argv:
        try:
            start_view = argv[argv.index("--view") + 1]
        except IndexError:
            pass
    app = DesktopApp(start_view=start_view)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0
