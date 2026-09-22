# -*- coding: utf-8 -*-
"""卡片类控件：指标卡 / 卡片网格 / 区块标题 / 徽标 / 分隔线 / 键值信息表。

约定
----
- 只用 ``theme`` 的配色与字体（``theme.COLORS`` / ``theme.font``），不硬编码颜色；
- 需要的几个附加 ttk 样式（``StatUp.TLabel`` 等）在本模块内用 ``ttk.Style`` 就地配置，
  **不修改** ``theme.py``；重复调用是幂等的；
- 所有控件都是普通 Tk 控件/ttk 控件，可直接 ``grid``/``pack`` 到任意父容器。

线程约束：只允许在主线程调用（Tk 限制）。
"""

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .. import theme


#: ``Badge`` 的 kind → theme.COLORS 的键
BADGE_KINDS: Dict[str, str] = {
    "flat": "flat",
    "up": "up",
    "down": "down",
    "warn": "warn",
    "brand": "brand",
    "local": "cyan",
    "custom": "purple",
}


def _base_size() -> int:
    """主题基准字号（``theme`` 未初始化时退回 10）。"""
    try:
        return int(getattr(theme, "_state", {}).get("base", 10))
    except Exception:  # noqa: BLE001 - 主题状态异常不应影响控件构建
        return 10


def _card_color(color_key: str) -> str:
    return theme.COLORS.get(str(color_key), theme.COLORS["flat"])


def _ensure_styles(widget: tk.Misc) -> None:
    """配置本模块用到的附加样式（幂等）。"""
    base = _base_size()
    style = ttk.Style(widget)
    panel = theme.COLORS["panel"]
    style.configure("StatFlat.TLabel", background=panel, foreground=theme.COLORS["flat"],
                    font=theme.font(base + 8, "bold"))
    style.configure("StatUp.TLabel", background=panel, foreground=theme.COLORS["up"],
                    font=theme.font(base + 8, "bold"))
    style.configure("StatDown.TLabel", background=panel, foreground=theme.COLORS["down"],
                    font=theme.font(base + 8, "bold"))
    style.configure("Section.TLabel", background=theme.COLORS["bg"], foreground=theme.COLORS["text"],
                    font=theme.font(base + 2, "bold"))
    style.configure("SectionHint.TLabel", background=theme.COLORS["bg"], foreground=theme.COLORS["text2"],
                    font=theme.font(base - 1))
    style.configure("KVKey.TLabel", background=panel, foreground=theme.COLORS["text2"],
                    font=theme.font(base - 1))
    style.configure("KVValue.TLabel", background=panel, foreground=theme.COLORS["text"],
                    font=theme.font(base))


def _stat_style(widget: tk.Misc, color: Optional[str]) -> str:
    """数值颜色 → 大字号数值的 ttk 样式名（非主题色的自定义色就地注册）。"""
    if color == theme.COLORS["up"]:
        return "StatUp.TLabel"
    if color == theme.COLORS["down"]:
        return "StatDown.TLabel"
    if color is None or color == theme.COLORS["flat"]:
        return "StatFlat.TLabel"
    name = "Stat" + str(color).lstrip("#").upper() + ".TLabel"
    ttk.Style(widget).configure(name, background=theme.COLORS["panel"], foreground=str(color),
                                font=theme.font(_base_size() + 8, "bold"))
    return name


def _fmt_value(value: Any) -> str:
    """卡片数值显示：数字千分位、字符串原样、空值用破折号。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return "{:,}".format(value)
    if isinstance(value, float):
        return theme.fmt_num(value, 2)
    return str(value)


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "").rstrip("%"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- StatCard
class StatCard(ttk.Frame):
    """指标卡：标题小灰字 + 数值大字 +（可选）单位 / 副标题。

    ``color_key`` 取 ``theme.COLORS`` 的键（``flat`` / ``up`` / ``down`` / ``brand`` ...）。
    ``.set(value, color_by=...)`` 传数值时按涨跌自动着色（红涨绿跌，见 ``theme.color_for``）。
    """

    def __init__(self, parent: tk.Misc, title: str, value: Any = "", unit: str = "",
                 sub: str = "", color_key: str = "flat", width: Optional[int] = None):
        _ensure_styles(parent)
        super().__init__(parent, style="Card.TFrame", padding=(12, 10))
        self.title_text = str(title)
        self.color_key = str(color_key) if str(color_key) in theme.COLORS else "flat"
        self.raw_value = None

        self.title_label = ttk.Label(self, text=self.title_text, style="CardMuted.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, columnspan=3, sticky="w")

        self.value_label = ttk.Label(self, text="—", anchor="w",
                                     style=_stat_style(self, _card_color(self.color_key)))
        self.value_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.unit_label = ttk.Label(self, text=str(unit or ""), style="CardMuted.TLabel", anchor="w")
        self.unit_label.grid(row=1, column=1, sticky="sw", padx=(4, 0), pady=(4, 2))

        self.sub_label = ttk.Label(self, text=str(sub or ""), style="CardMuted.TLabel", anchor="w")
        self.sub_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.columnconfigure(2, weight=1)

        if width:
            try:
                self.configure(width=int(width))
                self.grid_propagate(False)
                self.pack_propagate(False)
            except (tk.TclError, ValueError):
                pass

        if value != "" or unit or sub:
            self.set(value, unit=unit, sub=sub)

    def set(self, value: Any, color_by: Any = None, unit: Optional[str] = None,
            sub: Optional[str] = None) -> "StatCard":
        """更新数值/单位/副标题；``color_by`` 为数值时按涨跌自动着色。"""
        self.raw_value = value
        try:
            self.value_label.configure(text=_fmt_value(value))
        except tk.TclError:
            return self
        if unit is not None:
            self.unit_label.configure(text=str(unit))
        if sub is not None:
            self.sub_label.configure(text=str(sub))
        if color_by is not None:
            if isinstance(color_by, str) and color_by in theme.COLORS:
                color = theme.COLORS[color_by]
            else:
                number = _as_number(color_by)
                color = theme.color_for(number) if number is not None else theme.color_for(color_by)
            self.value_label.configure(style=_stat_style(self, color))
        return self

    def set_title(self, title: str) -> "StatCard":
        self.title_text = str(title)
        self.title_label.configure(text=self.title_text)
        return self


# --------------------------------------------------------------------------- CardGrid
class CardGrid(ttk.Frame):
    """等宽自适应的卡片网格容器（默认 4 列）。"""

    def __init__(self, parent: tk.Misc, columns: int = 4, gap: int = 8):
        super().__init__(parent, style="TFrame")
        self.columns = max(1, int(columns))
        self.gap = max(0, int(gap))
        self.widgets: List[tk.Widget] = []
        self._occupied: set = set()
        for index in range(self.columns):
            self.columnconfigure(index, weight=1, uniform="cardgrid")

    def add(self, widget: tk.Widget, row: Optional[int] = None,
            col: Optional[int] = None) -> tk.Widget:
        """放入下一个空位（或指定 ``row`` / ``col``）。"""
        position = self._free_cell(row, col)
        half = self.gap // 2
        widget.grid(row=position[0], column=position[1], sticky="nsew",
                    padx=half, pady=half)
        self._occupied.add(position)
        if position[1] >= self.columns:
            for index in range(self.columns, position[1] + 1):
                self.columnconfigure(index, weight=1, uniform="cardgrid")
        self.widgets.append(widget)
        return widget

    def _free_cell(self, row: Optional[int], col: Optional[int]) -> Tuple[int, int]:
        if row is None and col is None:
            index = 0
            while (index // self.columns, index % self.columns) in self._occupied:
                index += 1
            return index // self.columns, index % self.columns
        if col is None:
            candidate = 0
            while (int(row), candidate) in self._occupied:
                candidate += 1
            return int(row), candidate
        if row is None:
            candidate_row = 0
            while (candidate_row, int(col)) in self._occupied:
                candidate_row += 1
            return candidate_row, int(col)
        return int(row), int(col)

    def count(self) -> int:
        return len(self.widgets)

    def clear(self) -> None:
        """移除并销毁所有卡片。"""
        for widget in list(self.widgets):
            try:
                widget.destroy()
            except Exception:  # noqa: BLE001 - 销毁失败不应影响其它卡片
                pass
        self.widgets = []
        self._occupied = set()


# --------------------------------------------------------------------------- SectionTitle
class SectionTitle(ttk.Frame):
    """区块标题：左侧标题（+ 灰色提示），右侧可选按钮 ``action=(label, callback)``。"""

    def __init__(self, parent: tk.Misc, text: str, hint: str = "",
                 action: Optional[Tuple[str, Callable]] = None):
        _ensure_styles(parent)
        super().__init__(parent, style="TFrame")
        self.title_text = str(text)
        self.title_label = ttk.Label(self, text=self.title_text, style="Section.TLabel", anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.hint_label = ttk.Label(self, text=str(hint or ""), style="SectionHint.TLabel", anchor="w")
        if hint:
            self.hint_label.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.columnconfigure(2, weight=1)
        self.action_button: Optional[ttk.Button] = None
        if action:
            label, callback = action[0], action[1]
            self.action_button = ttk.Button(self, text=str(label), command=callback)
            self.action_button.grid(row=0, column=3, sticky="e")

    def set_text(self, text: str) -> "SectionTitle":
        self.title_text = str(text)
        self.title_label.configure(text=self.title_text)
        return self

    def set_hint(self, hint: str) -> "SectionTitle":
        self.hint_label.configure(text=str(hint or ""))
        if hint and not self.hint_label.grid_info():
            self.hint_label.grid(row=0, column=1, sticky="w", padx=(8, 0))
        elif not hint:
            self.hint_label.grid_remove()
        return self


# --------------------------------------------------------------------------- Badge
class Badge(tk.Label):
    """小徽标。``kind`` ∈ ``flat/up/down/warn/brand/local/custom``（也接受 theme 颜色名或 #hex）。"""

    def __init__(self, parent: tk.Misc, text: str, kind: str = "flat", **kwargs: Any):
        self.kind = str(kind)
        self.color = self._color_for(self.kind)
        background = theme.mix(theme.COLORS["panel"], self.color, 0.18)
        kwargs.setdefault("padx", 7)
        kwargs.setdefault("pady", 1)
        super().__init__(parent, text=str(text), bg=background, fg=self.color,
                         font=theme.font(_base_size() - 1, "bold"), bd=0,
                         highlightthickness=0, **kwargs)

    @staticmethod
    def _color_for(kind: str) -> str:
        name = str(kind)
        if name in BADGE_KINDS:
            return theme.COLORS[BADGE_KINDS[name]]
        if name in theme.COLORS:
            return theme.COLORS[name]
        if name.startswith("#") and len(name) in (4, 7):
            return name
        return theme.COLORS["flat"]

    def set(self, text: str, kind: Optional[str] = None) -> "Badge":
        if kind is not None and str(kind) != self.kind:
            self.kind = str(kind)
            self.color = self._color_for(self.kind)
            self.configure(bg=theme.mix(theme.COLORS["panel"], self.color, 0.18), fg=self.color)
        self.configure(text=str(text))
        return self


# --------------------------------------------------------------------------- Divider
class Divider(tk.Frame):
    """1px 分隔线：``Divider(parent).pack(fill="x", pady=6)``。"""

    def __init__(self, parent: tk.Misc, orient: str = "h", **kwargs: Any):
        kwargs.setdefault("bg", theme.COLORS["line"])
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("highlightthickness", 0)
        if str(orient).lower().startswith("v"):
            kwargs.setdefault("width", 1)
        else:
            kwargs.setdefault("height", 1)
        super().__init__(parent, **kwargs)
        self.orient = "v" if str(orient).lower().startswith("v") else "h"


# --------------------------------------------------------------------------- KeyValueTable
class KeyValueTable(ttk.Frame):
    """两列只读信息表（详情面板用）：``set_items([("代码", "600519.SH"), ...])``。

    ``key_labels`` / ``value_labels`` / ``data`` 可直接取用（例如断言或改单值）。
    """

    def __init__(self, parent: tk.Misc, items: Optional[Iterable[Any]] = None):
        _ensure_styles(parent)
        super().__init__(parent, style="Card.TFrame", padding=(4, 2))
        self.key_labels: Dict[str, ttk.Label] = {}
        self.value_labels: Dict[str, ttk.Label] = {}
        self.data: Dict[str, Any] = {}
        self.columnconfigure(1, weight=1)
        self.set_items(items or [])

    def set_items(self, items: Iterable[Any]) -> "KeyValueTable":
        for child in self.winfo_children():
            child.destroy()
        self.key_labels = {}
        self.value_labels = {}
        self.data = {}
        row = 0
        for item in items or []:
            pair = self._as_pair(item)
            if pair is None:
                continue
            key, value = pair
            key_text = str(key)
            key_label = ttk.Label(self, text=key_text, style="KVKey.TLabel", anchor="nw")
            value_label = ttk.Label(self, text=self._fmt(value), style="KVValue.TLabel",
                                    anchor="nw", justify="left")
            key_label.grid(row=row, column=0, sticky="nw", padx=(4, 10), pady=1)
            value_label.grid(row=row, column=1, sticky="nwe", pady=1)
            self.key_labels[key_text] = key_label
            self.value_labels[key_text] = value_label
            self.data[key_text] = value
            row += 1
        return self

    def set_value(self, key: str, value: Any) -> "KeyValueTable":
        """只改某一行的值（不重建控件）。"""
        key_text = str(key)
        self.data[key_text] = value
        label = self.value_labels.get(key_text)
        if label is not None:
            label.configure(text=self._fmt(value))
        return self

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(str(key), default)

    def count(self) -> int:
        return len(self.data)

    @staticmethod
    def _as_pair(item: Any) -> Optional[Tuple[Any, Any]]:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return item[0], item[1]
        if isinstance(item, dict):
            key = item.get("key", item.get("name"))
            if key is not None:
                return key, item.get("value")
        return None

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None or value == "":
            return "—"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, int):
            return "{:,}".format(value)
        if isinstance(value, float):
            return theme.fmt_num(value, 2)
        return str(value)


__all__ = ["StatCard", "CardGrid", "SectionTitle", "Badge", "Divider", "KeyValueTable",
           "BADGE_KINDS"]
