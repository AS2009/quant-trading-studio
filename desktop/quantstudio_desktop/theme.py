# -*- coding: utf-8 -*-
"""主题：深色金融风配色、中文字体选择、ttk 样式、Windows 深色标题栏。

采用 ``clam`` 主题而不是 Windows 默认的 ``vista``：后者忽略大部分背景色设置，
无法做出统一的深色界面。``clam`` 在所有平台都能完整着色，观感一致。
"""

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Dict, Optional

# --------------------------------------------------------------------------- 配色（与前端 CSS 变量保持一致）
COLORS: Dict[str, str] = {
    "bg": "#0e1420",          # 窗口背景
    "bg2": "#111827",         # 顶栏/状态栏
    "panel": "#161f31",       # 面板
    "panel2": "#1b2740",      # 面板高亮
    "line": "#26324d",        # 分隔线/边框
    "text": "#e8edf5",
    "text2": "#93a1b8",
    "brand": "#4f8cff",
    "brand_dark": "#3b78e8",
    "up": "#f6465d",          # 涨（红）
    "down": "#2ebd85",        # 跌（绿）
    "flat": "#93a1b8",
    "warn": "#f0b90b",
    "purple": "#c084fc",
    "cyan": "#58c7f5",
    "select": "#22304d",
}

FONT_CANDIDATES = [
    "Microsoft YaHei UI", "Microsoft YaHei",   # Windows
    "PingFang SC", "Hiragino Sans GB",         # macOS
    "Noto Sans CJK SC", "Source Han Sans SC",  # Linux
    "Segoe UI", "Arial",
]
MONO_CANDIDATES = ["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"]

_state: Dict[str, object] = {"base": 10, "family": None, "mono": None}


def pick_font(root: tk.Misc, candidates=None) -> str:
    """挑一个系统里存在的中文字体（找不到就用 Tk 默认）。"""
    try:
        available = set(tkfont.families(root))
    except Exception:  # noqa: BLE001 - 某些极简 Tk 构建可能查询失败
        available = set()
    for name in (candidates or FONT_CANDIDATES):
        if name in available:
            return name
    return "TkDefaultFont"


def font(size: int = 10, weight: str = "normal", mono: bool = False) -> tuple:
    """返回可用于 ``widget.configure(font=...)`` 的字体元组。"""
    family = _state["mono"] if mono else _state["family"]
    return (family or "TkDefaultFont", size, weight)


def init(root: tk.Misc, base_size: int = 10) -> ttk.Style:
    """初始化主题（在创建任何 ttk 控件之前调用一次）。"""
    _state["base"] = base_size
    _state["family"] = pick_font(root)
    _state["mono"] = pick_font(root, MONO_CANDIDATES)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    base = base_size
    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"],
                    fieldbackground=COLORS["bg2"], font=font(base))
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("Bar.TFrame", background=COLORS["bg2"])
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Card.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["text2"], font=font(base - 1))
    style.configure("CardMuted.TLabel", background=COLORS["panel"], foreground=COLORS["text2"], font=font(base - 1))
    style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=font(base + 6, "bold"))
    style.configure("H2.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=font(base + 2, "bold"))
    style.configure("Stat.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=font(base + 8, "bold"))
    style.configure("Up.TLabel", background=COLORS["panel"], foreground=COLORS["up"])
    style.configure("Down.TLabel", background=COLORS["panel"], foreground=COLORS["down"])
    style.configure("Flat.TLabel", background=COLORS["panel"], foreground=COLORS["flat"])
    style.configure("UpStat.TLabel", background=COLORS["panel"], foreground=COLORS["up"], font=font(base + 8, "bold"))
    style.configure("DownStat.TLabel", background=COLORS["panel"], foreground=COLORS["down"], font=font(base + 8, "bold"))

    style.configure("TButton", background=COLORS["panel2"], foreground=COLORS["text"],
                    bordercolor=COLORS["line"], focuscolor=COLORS["panel2"], font=font(base))
    style.map("TButton",
              background=[("active", COLORS["select"]), ("disabled", COLORS["panel"])],
              foreground=[("disabled", COLORS["text2"])])
    style.configure("Primary.TButton", background=COLORS["brand"], foreground="#ffffff",
                    bordercolor=COLORS["brand"], font=font(base, "bold"))
    style.map("Primary.TButton", background=[("active", COLORS["brand_dark"]), ("disabled", COLORS["panel2"])])
    style.configure("Danger.TButton", background=COLORS["panel2"], foreground=COLORS["up"], bordercolor=COLORS["line"])

    style.configure("Nav.TButton", background=COLORS["bg"], foreground=COLORS["text2"],
                    anchor="w", padding=(14, 10), font=font(base + 1))
    style.map("Nav.TButton", background=[("active", COLORS["panel"])], foreground=[("active", COLORS["text"])])
    style.configure("NavActive.TButton", background=COLORS["panel2"], foreground="#ffffff",
                    anchor="w", padding=(14, 10), font=font(base + 1, "bold"))

    style.configure("TEntry", fieldbackground=COLORS["bg2"], foreground=COLORS["text"],
                    bordercolor=COLORS["line"], insertcolor=COLORS["text"])
    style.configure("TCombobox", fieldbackground=COLORS["bg2"], background=COLORS["panel2"],
                    foreground=COLORS["text"], arrowcolor=COLORS["text2"])
    style.map("TCombobox", fieldbackground=[("readonly", COLORS["bg2"])])
    style.configure("TSpinbox", fieldbackground=COLORS["bg2"], foreground=COLORS["text"],
                    arrowcolor=COLORS["text2"], bordercolor=COLORS["line"])
    style.configure("TCheckbutton", background=COLORS["panel"], foreground=COLORS["text"])
    style.map("TCheckbutton", background=[("active", COLORS["panel"])])
    style.configure("TRadiobutton", background=COLORS["panel"], foreground=COLORS["text"])

    style.configure("Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"],
                    foreground=COLORS["text"], bordercolor=COLORS["line"], rowheight=base + 12,
                    font=font(base))
    style.configure("Treeview.Heading", background=COLORS["bg2"], foreground=COLORS["text2"],
                    relief="flat", font=font(base, "bold"))
    style.map("Treeview.Heading", background=[("active", COLORS["select"])])
    style.map("Treeview", background=[("selected", COLORS["select"])], foreground=[("selected", "#ffffff")])

    style.configure("TNotebook", background=COLORS["bg"], bordercolor=COLORS["line"])
    style.configure("TNotebook.Tab", background=COLORS["bg2"], foreground=COLORS["text2"], padding=(14, 7))
    style.map("TNotebook.Tab", background=[("selected", COLORS["panel2"])], foreground=[("selected", "#ffffff")])
    style.configure("TSeparator", background=COLORS["line"])
    style.configure("Vertical.TScrollbar", background=COLORS["panel2"], troughcolor=COLORS["bg2"],
                    bordercolor=COLORS["bg2"], arrowcolor=COLORS["text2"])
    style.configure("Horizontal.TScrollbar", background=COLORS["panel2"], troughcolor=COLORS["bg2"],
                    bordercolor=COLORS["bg2"], arrowcolor=COLORS["text2"])
    style.configure("TProgressbar", background=COLORS["brand"], troughcolor=COLORS["bg2"], bordercolor=COLORS["bg2"])

    apply_windows_dark_titlebar(root)
    return style


def apply_windows_dark_titlebar(root: tk.Misc) -> None:
    """尽量让 Windows 标题栏也变深色（失败静默忽略：仅影响观感）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+: 20, 旧版: 19)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


def color_for(value: float) -> str:
    """数值 → 涨跌色（红涨绿跌）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return COLORS["flat"]
    if number > 0:
        return COLORS["up"]
    if number < 0:
        return COLORS["down"]
    return COLORS["flat"]


def text_style_for(value: float, light_on_dark: bool = True) -> str:
    """数值 → ttk 样式名（面板内文本）。"""
    if value is None:
        return "Flat.TLabel"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Flat.TLabel"
    if number > 0:
        return "Up.TLabel"
    if number < 0:
        return "Down.TLabel"
    return "Flat.TLabel"


def hex_to_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def mix(color_a: str, color_b: str, ratio: float) -> str:
    """按比例混合两色（用于生成网格线、hover 色等）。"""
    ra, ga, ba = hex_to_rgb(color_a)
    rb, gb, bb = hex_to_rgb(color_b)
    ratio = max(0.0, min(1.0, float(ratio)))
    return "#%02x%02x%02x" % (int(ra + (rb - ra) * ratio),
                              int(ga + (gb - ga) * ratio),
                              int(ba + (bb - ba) * ratio))


def fmt_money(value, digits: int = 2) -> str:
    """千分位金额（无 ¥ 前缀）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return "{:,.{d}f}".format(number, d=digits)


def fmt_pct(value, digits: int = 2, with_sign: bool = True) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if (with_sign and number > 0) else ""
    return "{}{:.{d}f}%".format(sign, number, d=digits)


def fmt_num(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        return "{:,.{d}f}".format(float(value), d=digits)
    except (TypeError, ValueError):
        return str(value)
