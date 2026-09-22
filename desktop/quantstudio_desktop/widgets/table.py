# -*- coding: utf-8 -*-
"""``DataTable``：``ttk.Treeview`` 的通用封装（自选股 / 持仓 / 成交 / 委托 / 策略列表都用它）。

设计要点
--------
- **列声明式**：列定义是 dict 列表，见 ``_normalize_column``；
  列类型 ``kind`` ∈ ``text | num | money | pct | int | code | badge``，
  格式化统一走 ``theme.fmt_money / fmt_pct / fmt_num``，不在控件里另写数字格式化；
- **着色用 tag**：Tk 的 Treeview 只能给「整行」挂 tag（没有单元格级渲染），
  因此为每个「行 × 列」组合生成 tag（如 ``up:price`` / ``down:change_pct``）并按列顺序挂到行上，
  同色冲突时**排在前面的列优先**（实现上把第一列的 tag 放在 tag 列表末尾，Tk 取最后一个匹配 tag 的选项）；
- **排序**：点表头切换升/降序，表头带 ▲/▼；数值列按数值排，文本列按大小写不敏感字符串排，
  缺失值永远排在最后（不受升降序影响）；
- **空状态**：无数据时在表格区域中间盖一层灰字标签（不是只留空表头）；
- 列宽/行高/配色全部来自 ``theme``；``striped=True`` 时偶数行用 tag 略深。

线程约束：只允许在主线程调用（Tk 限制）。
"""

import os
import re
import traceback
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import theme

# --------------------------------------------------------------------------- 常量
KIND_TEXT = "text"
KIND_NUM = "num"
KIND_MONEY = "money"
KIND_PCT = "pct"
KIND_INT = "int"
KIND_CODE = "code"
KIND_BADGE = "badge"

KINDS: Tuple[str, ...] = (KIND_TEXT, KIND_NUM, KIND_MONEY, KIND_PCT, KIND_INT, KIND_CODE, KIND_BADGE)
NUMERIC_KINDS: Tuple[str, ...] = (KIND_NUM, KIND_MONEY, KIND_PCT, KIND_INT)
# 默认按涨跌自动着色的列类型
COLOR_KINDS: Tuple[str, ...] = NUMERIC_KINDS

EMPTY_TEXT = "暂无数据"

TAG_SELECTED = "row:selected"
TAG_STRIPE = "row:even"

DEFAULT_WIDTHS = {KIND_TEXT: 120, KIND_NUM: 90, KIND_MONEY: 110, KIND_PCT: 90,
                  KIND_INT: 80, KIND_CODE: 100, KIND_BADGE: 80}
DEFAULT_ANCHORS = {KIND_TEXT: "w", KIND_NUM: "e", KIND_MONEY: "e", KIND_PCT: "e",
                   KIND_INT: "e", KIND_CODE: "w", KIND_BADGE: "center"}
DEFAULT_DIGITS = {KIND_NUM: 2, KIND_MONEY: 2, KIND_PCT: 2, KIND_INT: 0}

_ANCHOR_ALIASES = {"left": "w", "west": "w", "right": "e", "east": "e",
                   "middle": "center", "centre": "center"}
_TAG_SAFE = re.compile(r"[^0-9A-Za-z_]+")


# --------------------------------------------------------------------------- 小工具
def _base_size() -> int:
    """主题基准字号（``theme`` 未初始化时退回 10）。"""
    try:
        return int(getattr(theme, "_state", {}).get("base", 10))
    except Exception:  # noqa: BLE001 - 主题状态异常不应影响控件构建
        return 10


def _safe_call(fn: Optional[Callable], *args: Any) -> Any:
    """调用回调：异常绝不冒泡到 Tk 事件循环（``QS_DEBUG=1`` 时打印堆栈便于排障）。"""
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - 回调异常不能影响界面
        if os.environ.get("QS_DEBUG"):
            traceback.print_exc()
        return None


def _as_number(value: Any) -> Optional[float]:
    """宽松转数字：容忍千分位、``%``、``+``、空串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if text.endswith("%"):
        text = text[:-1]
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_row(row: Any) -> Dict[str, Any]:
    """行数据统一成 dict（保留原始对象引用时用 dict 原样返回）。"""
    if isinstance(row, dict):
        return row
    if isinstance(row, Mapping):
        return dict(row)
    as_dict = getattr(row, "_asdict", None)
    if callable(as_dict):
        return dict(as_dict())
    if hasattr(row, "__dict__"):
        return dict(vars(row))
    raise TypeError("行数据必须是 dict（或带 __dict__ 的对象），得到 %r" % (row,))


def _normalize_column(column: Any, index: int = 0) -> Dict[str, Any]:
    """列定义 → 标准 dict。接受 dict / ``"key"`` / ``(key, label)``。"""
    if isinstance(column, str):
        spec: Dict[str, Any] = {"key": column}
    elif isinstance(column, (tuple, list)):
        spec = {"key": column[0], "label": column[1] if len(column) > 1 else column[0]}
    elif isinstance(column, dict):
        spec = dict(column)
    elif isinstance(column, Mapping):
        spec = dict(column)
    else:
        raise TypeError("列定义必须是 dict / str / (key, label)，得到 %r" % (column,))

    key = spec.get("key") or spec.get("name") or spec.get("id")
    if not key:
        raise ValueError("第 %d 列缺少 key" % (index + 1))
    key = str(key)
    kind = str(spec.get("kind") or KIND_TEXT).strip().lower()
    if kind not in KINDS:
        raise ValueError("列 %r 的 kind=%r 不支持（可用：%s）" % (key, kind, " / ".join(KINDS)))

    digits = spec.get("digits", DEFAULT_DIGITS.get(kind, 2))
    try:
        digits = int(digits)
    except (TypeError, ValueError):
        digits = DEFAULT_DIGITS.get(kind, 2)

    width = spec.get("width")
    try:
        width = int(width) if width else DEFAULT_WIDTHS.get(kind, 90)
    except (TypeError, ValueError):
        width = DEFAULT_WIDTHS.get(kind, 90)

    anchor = str(spec.get("anchor") or DEFAULT_ANCHORS.get(kind, "w")).strip().lower()
    anchor = _ANCHOR_ALIASES.get(anchor, anchor)
    if anchor not in ("w", "e", "center"):
        anchor = DEFAULT_ANCHORS.get(kind, "w")

    return {
        "key": key,
        "label": str(spec.get("label") or key),
        "kind": kind,
        "width": max(30, width),
        "minwidth": int(spec.get("minwidth") or min(max(30, width), 60)),
        "anchor": anchor,
        "digits": max(0, min(6, digits)),
        "sort_key": spec.get("sort_key"),
        "stretch": bool(spec.get("stretch", False)),
        # kind=badge 时可给 {值: 颜色名或 #hex} 映射，用 tag 着色
        "badge_colors": spec.get("badge_colors") or {},
    }


# --------------------------------------------------------------------------- DataTable
class DataTable(ttk.Frame):
    """``ttk.Treeview`` 封装：列格式化、按列着色、可排序表头、空状态、选中回调。

    列定义（``columns``）::

        [{"key": "price", "label": "现价", "width": 90, "anchor": "e",
          "kind": "num", "digits": 2, "sort_key": None}]

    ``kind`` 决定格式化与默认对齐/宽度：

    ==========  ====================================================
    kind        显示
    ==========  ====================================================
    ``text``    原样文本（左对齐）
    ``num``     千分位 + ``digits`` 位小数（默认 2）
    ``money``   同 ``theme.fmt_money``
    ``pct``     ``theme.fmt_pct``（带 +/- 号）
    ``int``     千分位整数（默认 0 位小数）
    ``code``    左对齐 + 等宽字体 tag（``mono:<key>``）
    ``badge``   原样文本，可用 ``badge_colors`` 映射值→颜色
    ==========  ====================================================
    """

    def __init__(self, parent: tk.Misc, columns: Sequence[Any], height: int = 8,
                 on_select: Optional[Callable[[Dict[str, Any], int], Any]] = None,
                 on_double_click: Optional[Callable[[Dict[str, Any], int], Any]] = None,
                 sortable: bool = True, striped: bool = True):
        super().__init__(parent, style="TFrame")
        self.columns: List[Dict[str, Any]] = [_normalize_column(col, i)
                                             for i, col in enumerate(columns or [])]
        self._column_by_key: Dict[str, Dict[str, Any]] = {c["key"]: c for c in self.columns}
        self.height = int(height)
        self.sortable = bool(sortable)
        self.striped = bool(striped)
        self.on_select = on_select
        self.on_double_click = on_double_click

        self.rows: List[Dict[str, Any]] = []      # 当前显示顺序的原始 dict（排序后即排序结果）
        self.key_field: Optional[str] = None
        self.color_rules: List[Dict[str, Any]] = []
        self.empty_text = EMPTY_TEXT

        self._sort_by: Optional[str] = None
        self._sort_desc = False
        self._row_by_iid: Dict[str, Dict[str, Any]] = {}
        self._index_by_iid: Dict[str, int] = {}
        self._row_tags: Dict[str, Tuple[str, ...]] = {}
        self._tags: Dict[str, Dict[str, Any]] = {}
        self._suppress_select = False
        self._last_notified: Optional[str] = None
        self._selected_iid: Optional[str] = None
        self._empty_visible = False
        self._hsync_pending = False
        self._hsb_shown = True

        self._build_ui()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=0)

        # body：Treeview + 空状态标签容器
        self._body = tk.Frame(self, bg=theme.COLORS["panel"], highlightthickness=0, bd=0)
        self._body.grid(row=0, column=0, sticky="nsew")
        self._body.columnconfigure(0, weight=1)
        self._body.rowconfigure(0, weight=1)

        keys = [c["key"] for c in self.columns]
        self.tree = ttk.Treeview(self._body, columns=keys, show="headings",
                                 height=self.height, selectmode="browse")
        for column in self.columns:
            key = column["key"]
            self.tree.column(key, width=column["width"], minwidth=column["minwidth"],
                             anchor=column["anchor"], stretch=column["stretch"])
            self.tree.heading(key, text=column["label"], anchor=column["anchor"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self._display_keys = list(keys)

        self.vsb = ttk.Scrollbar(self._body, orient="vertical", command=self.tree.yview)
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)

        # 空状态：盖在表格区域正中的灰字（不是只有空表头）
        self.empty_label = tk.Label(self._body, text=self.empty_text, bg=theme.COLORS["panel"],
                                    fg=theme.COLORS["text2"], font=theme.font(_base_size()),
                                    justify="center")
        self._ensure_tag(TAG_SELECTED, background=theme.COLORS["select"], foreground="#ffffff")
        if self.striped:
            self._ensure_tag(TAG_STRIPE,
                             background=theme.mix(theme.COLORS["panel"], theme.COLORS["bg"], 0.45))

        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<Double-Button-1>", self._on_double_click, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select, add="+")
        self.tree.bind("<Configure>", self._on_tree_configure, add="+")
        self._update_empty_state()

    # ------------------------------------------------------------------ 对外 API
    def set_rows(self, rows: Optional[Iterable[Any]], key_field: Optional[str] = None,
                 preserve_selection: bool = True,
                 color_rules: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        """渲染数据。``rows`` 为 dict 列表；``key_field`` 用于跨刷新保持选中行。"""
        self.rows = [_as_row(row) for row in (rows or [])]
        if key_field is not None:
            self.key_field = key_field
        if color_rules is not None:
            self.color_rules = list(color_rules)
        self._render(preserve_selection=preserve_selection, notify=True)

    def clear(self) -> None:
        """清空数据（保留列定义与排序状态）。"""
        self.rows = []
        self._render(preserve_selection=False, notify=False)

    def set_empty_text(self, text: str) -> None:
        """自定义空状态文案。"""
        self.empty_text = str(text)
        self.empty_label.configure(text=self.empty_text)

    def empty_visible(self) -> bool:
        """空状态标签当前是否显示。"""
        return bool(self._empty_visible)

    def set_visible_columns(self, keys: Optional[Sequence[str]]) -> None:
        """只显示给定列（策略列表/持仓表切换列用）；``None`` 或空列表 = 全部显示。"""
        if not keys:
            self._display_keys = [c["key"] for c in self.columns]
            self.tree["displaycolumns"] = tuple(self._display_keys)
        else:
            chosen = [str(k) for k in keys if str(k) in self._column_by_key]
            if not chosen:
                return
            self._display_keys = chosen
            self.tree["displaycolumns"] = tuple(chosen)
        self._refresh_headings()
        self._on_tree_configure()

    def visible_columns(self) -> List[str]:
        return list(self._display_keys)

    def selected(self) -> Optional[Dict[str, Any]]:
        """当前选中行的**原始 dict**（未选中 / 数据已刷新时返回 None）。"""
        selection = self.tree.selection()
        if not selection:
            return None
        return self._row_by_iid.get(selection[0])

    def selected_index(self) -> Optional[int]:
        """当前选中行在 ``self.rows``（当前显示顺序）中的下标。"""
        selection = self.tree.selection()
        if not selection:
            return None
        return self._index_by_iid.get(selection[0])

    def select_index(self, index: int, notify: bool = True) -> bool:
        """选中第 ``index`` 行（显示顺序）。越界返回 False。"""
        if index is None or index < 0 or index >= len(self.rows):
            return False
        iid = self._iid_for_index(int(index))
        if iid is None:
            return False
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.tree.see(iid)
        except tk.TclError:
            pass
        self._apply_selection_tags()
        if notify:
            self._notify_selection(force=True)
        return True

    def sort_by(self, key: str, desc: Optional[bool] = None) -> None:
        """按列排序；``desc=None`` 时同列保持方向、换列默认升序。"""
        if key not in self._column_by_key:
            return
        if desc is None:
            desc = self._sort_desc if self._sort_by == key else False
        self._sort_by = key
        self._sort_desc = bool(desc)
        self._refresh_headings()
        self._render(preserve_selection=True, notify=True)

    def toggle_sort(self, key: str) -> None:
        """点表头：同列切换升/降序，换列则升序。"""
        if self._sort_by == key:
            self.sort_by(key, not self._sort_desc)
        else:
            self.sort_by(key, False)

    def sort_state(self) -> Tuple[Optional[str], bool]:
        """当前排序状态 ``(列 key, 是否降序)``。"""
        return self._sort_by, self._sort_desc

    def format_cell(self, key: str, value: Any) -> str:
        """按列类型格式化单元格（对外暴露，便于页面复用同一套显示规则）。"""
        column = self._column_by_key.get(key)
        if column is None:
            return "" if value is None else str(value)
        return self._format(column, value)

    def row_count(self) -> int:
        return len(self.tree.get_children())

    # ------------------------------------------------------------------ 渲染
    def _iid_for_index(self, index: int) -> Optional[str]:
        for iid, idx in self._index_by_iid.items():
            if idx == index:
                return iid
        return None

    def _sorted_rows(self) -> List[Dict[str, Any]]:
        rows = list(self.rows)
        column = self._column_by_key.get(self._sort_by) if self._sort_by else None
        if column is None:
            return rows
        pairs = []
        for row in rows:
            pairs.append((self._sort_scalar(column, row), row))
        present = [(value, row) for value, row in pairs if value is not None]
        missing = [row for value, row in pairs if value is None]
        try:
            present.sort(key=lambda item: item[0], reverse=self._sort_desc)
        except TypeError:      # sort_key 返回混合类型时退回字符串比较，保证不炸
            present.sort(key=lambda item: str(item[0]), reverse=self._sort_desc)
        return [row for _value, row in present] + missing

    def _sort_scalar(self, column: Dict[str, Any], row: Dict[str, Any]) -> Any:
        raw = row.get(column["key"])
        sort_key = column.get("sort_key")
        if callable(sort_key):
            try:
                value = sort_key(raw, row)
            except TypeError:
                value = sort_key(raw)
        elif isinstance(sort_key, str):
            value = row.get(sort_key)
        elif column["kind"] in NUMERIC_KINDS:
            value = _as_number(raw)
        else:
            value = raw
            if value is None:
                return None
            value = str(value).strip()
            if value == "":
                return None
            return value.casefold()
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return float(value)
        return str(value).casefold()

    def _format(self, column: Dict[str, Any], value: Any) -> str:
        kind = column["kind"]
        digits = column["digits"]
        if kind in NUMERIC_KINDS:
            number = _as_number(value)
            if number is None:
                return "—" if value is None or value == "" else str(value)
            if kind == KIND_NUM:
                return theme.fmt_num(number, digits)
            if kind == KIND_MONEY:
                return theme.fmt_money(number, digits)
            if kind == KIND_PCT:
                return theme.fmt_pct(number, digits)
            return theme.fmt_num(number, digits)
        if value is None:
            return ""
        return str(value)

    def _color_for_cell(self, column: Dict[str, Any], value: Any,
                        row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """→ (tag 名, 前景色)；无可着色时 (None, None)。"""
        key = column["key"]
        for index, rule in enumerate(self.color_rules or []):
            if not isinstance(rule, dict):
                continue
            target = rule.get("column") or rule.get("key")
            if target and str(target) != key:
                continue
            color = rule.get("color")
            when = rule.get("when")
            if callable(color):
                try:
                    color = color(value, row)
                except Exception:  # noqa: BLE001 - 规则异常按不匹配处理
                    color = None
            if callable(when):
                try:
                    matched = bool(when(value, row))
                except Exception:  # noqa: BLE001
                    matched = False
            else:
                matched = color is not None
            if not matched or color is None:
                continue
            tag = rule.get("tag") or "rule:%s:%d" % (_TAG_SAFE.sub("_", key) or "col", index)
            return tag, str(color)

        if column["kind"] in COLOR_KINDS:
            number = _as_number(value)
            if number is None:
                return None, None
            if number > 0:
                return "up:%s" % key, theme.COLORS["up"]
            if number < 0:
                return "down:%s" % key, theme.COLORS["down"]
            return "flat:%s" % key, theme.COLORS["flat"]

        if column["kind"] == KIND_BADGE:
            mapping = column.get("badge_colors") or {}
            if value is not None:
                for index, candidate in enumerate(mapping.keys()):
                    if str(candidate) == str(value):
                        color = mapping.get(candidate)
                        if not color:
                            break
                        name = str(color)
                        color = theme.COLORS.get(name, name)
                        return "badge:%s:%d" % (_TAG_SAFE.sub("_", key) or "col", index), color
        return None, None

    def _ensure_tag(self, name: str, **options: Any) -> str:
        if self._tags.get(name) == options and name in self._tags:
            return name
        self.tree.tag_configure(name, **options)
        self._tags[name] = dict(options)
        return name

    def _build_tags(self, row: Dict[str, Any], index: int) -> Tuple[str, ...]:
        color_tags: List[str] = []
        mono_tags: List[str] = []
        for column in self.columns:
            key = column["key"]
            tag, color = self._color_for_cell(column, row.get(key), row)
            if tag:
                self._ensure_tag(tag, foreground=color)
                color_tags.append(tag)
            if column["kind"] == KIND_CODE:
                mono_tags.append(self._ensure_tag("mono:%s" % key,
                                                  font=theme.font(_base_size(), mono=True)))
        tags: List[str] = []
        if self.striped and index % 2 == 1:      # 1-based 偶数行略深
            tags.append(TAG_STRIPE)
        # 反过来挂：排在最前面的列位于 tag 列表末尾 → 冲突时优先（Tk 取最后一个匹配 tag）
        tags.extend(reversed(color_tags))
        tags.extend(mono_tags)
        return tuple(tags)

    def _render(self, preserve_selection: bool = True, notify: bool = True) -> None:
        previous_iid = self.tree.selection()[0] if self.tree.selection() else None
        previous_index = self._index_by_iid.get(previous_iid) if previous_iid else None
        previous_row = self._row_by_iid.get(previous_iid) if previous_iid else None

        self.rows = self._sorted_rows()
        self._suppress_select = True
        target_iid: Optional[str] = None
        try:
            children = self.tree.get_children()
            if children:
                self.tree.delete(*children)
            self._row_by_iid.clear()
            self._index_by_iid.clear()
            self._row_tags.clear()
            self._selected_iid = None
            self._last_notified = None

            for index, row in enumerate(self.rows):
                iid = "r%d" % index
                values = [self._format(column, row.get(column["key"])) for column in self.columns]
                tags = self._build_tags(row, index)
                self.tree.insert("", "end", iid=iid, values=values, tags=tags)
                self._row_by_iid[iid] = row
                self._index_by_iid[iid] = index
                self._row_tags[iid] = tags

            target_iid = self._find_restore_target(previous_row, previous_index) if preserve_selection else None
            if target_iid:
                self.tree.selection_set(target_iid)
                self.tree.focus(target_iid)
                try:
                    self.tree.see(target_iid)
                except tk.TclError:
                    pass
        finally:
            self._suppress_select = False

        self._update_empty_state()
        self._apply_selection_tags()
        if notify and target_iid:
            self._notify_selection(force=True)

    def _find_restore_target(self, previous_row: Optional[Dict[str, Any]],
                             previous_index: Optional[int]) -> Optional[str]:
        if self.key_field and previous_row is not None:
            wanted = previous_row.get(self.key_field)
            if wanted is not None:
                for iid, row in self._row_by_iid.items():
                    if row.get(self.key_field) == wanted:
                        return iid
            return None
        if previous_index is not None:
            for iid, index in self._index_by_iid.items():
                if index == previous_index:
                    return iid
        return None

    def _update_empty_state(self) -> None:
        has_rows = bool(self.tree.get_children())
        self._empty_visible = not has_rows
        if has_rows:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
            try:
                self.empty_label.lift()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ 表头 / 交互
    def _refresh_headings(self) -> None:
        for column in self.columns:
            key = column["key"]
            text = column["label"]
            if self.sortable and self._sort_by == key:
                text += " ▼" if self._sort_desc else " ▲"
            self.tree.heading(key, text=text, anchor=column["anchor"])

    def _on_click(self, event: "tk.Event") -> Optional[str]:
        if not self.sortable:
            return None
        try:
            if self.tree.identify_region(event.x, event.y) != "heading":
                return None
            column_id = self.tree.identify_column(event.x)
        except tk.TclError:
            return None
        try:
            position = int(str(column_id).lstrip("#")) - 1
        except (TypeError, ValueError):
            return None
        if 0 <= position < len(self._display_keys):
            self.toggle_sort(self._display_keys[position])
        return None

    def _on_double_click(self, event: "tk.Event") -> Optional[str]:
        try:
            if self.tree.identify_region(event.x, event.y) == "heading":
                return None
            iid = self.tree.identify_row(event.y)
        except tk.TclError:
            return None
        row = self._row_by_iid.get(iid)
        if row is None:
            return None
        _safe_call(self.on_double_click, row, self._index_by_iid.get(iid))
        return None

    def _on_tree_select(self, _event: Any = None) -> None:
        self._apply_selection_tags()
        self._notify_selection()

    def _apply_selection_tags(self) -> None:
        selection = self.tree.selection()
        current = selection[0] if selection else None
        if self._selected_iid and self._selected_iid != current:
            self._retag(self._selected_iid)
        if current:
            self._selected_iid = current
            base = list(self._row_tags.get(current, ()))
            if TAG_SELECTED not in base:
                try:
                    self.tree.item(current, tags=tuple(base + [TAG_SELECTED]))
                except tk.TclError:
                    return
        else:
            self._selected_iid = None

    def _retag(self, iid: str) -> None:
        tags = self._row_tags.get(iid)
        if tags is None:
            return
        try:
            self.tree.item(iid, tags=tags)
        except tk.TclError:
            pass

    def _notify_selection(self, force: bool = False) -> None:
        if self._suppress_select:
            return
        selection = self.tree.selection()
        iid = selection[0] if selection else None
        if not force and iid == self._last_notified:
            return
        self._last_notified = iid
        if iid is None:
            return
        row = self._row_by_iid.get(iid)
        if row is None:
            return
        _safe_call(self.on_select, row, self._index_by_iid.get(iid))

    # ------------------------------------------------------------------ 横向滚动条
    def _on_tree_configure(self, _event: Any = None) -> None:
        if self._hsync_pending:
            return
        self._hsync_pending = True
        try:
            self.after_idle(self._sync_hsb)
        except tk.TclError:
            self._hsync_pending = False

    def _sync_hsb(self) -> None:
        self._hsync_pending = False
        try:
            width = int(self.tree.winfo_width())
            total = sum(int(self.tree.column(key, "width")) for key in self._display_keys)
        except (tk.TclError, TypeError, ValueError):
            return
        if width <= 80:          # 还没布局完成
            return
        needed = total > width + 1
        if needed and not self._hsb_shown:
            self.hsb.grid()
            self._hsb_shown = True
        elif not needed and self._hsb_shown:
            self.hsb.grid_remove()
            self._hsb_shown = False


__all__ = ["DataTable", "KINDS", "NUMERIC_KINDS", "KIND_TEXT", "KIND_NUM", "KIND_MONEY",
           "KIND_PCT", "KIND_INT", "KIND_CODE", "KIND_BADGE", "TAG_SELECTED", "TAG_STRIPE"]
