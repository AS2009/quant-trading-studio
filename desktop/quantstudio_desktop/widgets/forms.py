# -*- coding: utf-8 -*-
"""声明式表单 + 模态弹窗。

用法::

    fields = [
        Field("symbol", "证券代码", required=True, help="例如 600519.SH"),
        Field("side", "方向", type="choice", choices=["买入", "卖出"], required=True),
        Field("qty", "数量", type="int", min=100, max=100000, required=True),
        Field("price", "价格", type="float", min=0.01),
        Field("note", "备注"),
    ]
    values = FormDialog(self, "下单", fields, on_submit=service.place_order).show()
    if values is None:
        return                      # 用户取消

约定
----
- **无副作用**：本模块不 import 服务层；提交由调用方通过 ``on_submit(values)`` 完成，
  返回字符串表示校验/业务失败（错误显示在弹窗内、弹窗不关），返回 ``None``/``True`` 表示成功；
  抛异常同样按失败处理并把异常文本显示出来；
- ``FormDialog.validate`` 是纯函数（不碰 Tk），可直接单测；
- 只用标准库 + ``theme``；不使用 ``ttk.Spinbox``（Tk 8.6 才有），数字输入用 ``ttk.Entry`` + 校验。

线程约束：只允许在主线程调用（Tk 限制）。
"""

import datetime
import json
import math
import re
import tkinter as tk
from dataclasses import dataclass, fields as dataclass_fields
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .. import theme

# --------------------------------------------------------------------------- 字段类型
TYPE_TEXT = "text"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_CHOICE = "choice"
TYPE_BOOL = "bool"
TYPE_JSON = "json"
TYPE_DATE = "date"
TYPE_PASSWORD = "password"

FIELD_TYPES: Tuple[str, ...] = (TYPE_TEXT, TYPE_INT, TYPE_FLOAT, TYPE_CHOICE,
                                TYPE_BOOL, TYPE_JSON, TYPE_DATE, TYPE_PASSWORD)
NUMBER_TYPES: Tuple[str, ...] = (TYPE_INT, TYPE_FLOAT)

_TYPE_ALIASES = {
    "str": TYPE_TEXT, "string": TYPE_TEXT, "line": TYPE_TEXT,
    "integer": TYPE_INT, "number": TYPE_FLOAT, "decimal": TYPE_FLOAT, "money": TYPE_FLOAT,
    "select": TYPE_CHOICE, "enum": TYPE_CHOICE, "combo": TYPE_CHOICE,
    "boolean": TYPE_BOOL, "checkbox": TYPE_BOOL, "switch": TYPE_BOOL,
    "dict": TYPE_JSON, "object": TYPE_JSON, "jsonobject": TYPE_JSON,
    "day": TYPE_DATE, "date_str": TYPE_DATE,
    "secret": TYPE_PASSWORD, "pwd": TYPE_PASSWORD,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t", "是", "开", "启用"}
_FALSE_VALUES = {"", "0", "false", "no", "off", "n", "f", "否", "关", "停用"}


def normalize_type(value: Any) -> str:
    """字段类型归一化（``str``/``integer``/``select`` 等别名都接受）。"""
    name = str(value or TYPE_TEXT).strip().lower()
    return _TYPE_ALIASES.get(name, name)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _to_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return bool(text)


def _valid_date(text: str) -> bool:
    if not _DATE_RE.match(text):
        return False
    try:
        datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _number_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "%g" % number


def _parse_number(text: str, kind: str) -> Optional[Union[int, float]]:
    cleaned = text.replace(",", "").replace("，", "").strip()
    if kind == TYPE_INT:
        try:
            return int(cleaned)
        except ValueError:
            return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


# --------------------------------------------------------------------------- Field
@dataclass
class Field:
    """一个表单字段（声明式）。

    :param name: 取值键（提交 dict 的 key）
    :param label: 界面标签（留空用 ``name``）
    :param type: ``text | int | float | choice | bool | json | date | password``
    :param default: 初始值
    :param choices: ``choice`` 的可选值（白名单，只读下拉）
    :param min: 数值下界（含）
    :param max: 数值上界（含）
    :param step: 步长提示（Tk 8.5 无 Spinbox，仅作为说明性元数据保留）
    :param help: 输入框下方的小灰字说明
    :param required: 必填
    :param width: 输入框宽度（字符数）
    :param readonly: 只读（可选中但不可编辑）
    """

    name: str
    label: str = ""
    type: str = TYPE_TEXT
    default: Any = None
    choices: Optional[Sequence[Any]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    help: str = ""
    required: bool = False
    width: Optional[int] = None
    readonly: bool = False

    def __post_init__(self) -> None:
        self.type = normalize_type(self.type)
        if not self.label:
            self.label = self.name
        if self.choices is not None:
            self.choices = [str(choice) for choice in self.choices]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Field":
        """dict → Field（未知键忽略，便于页面把元数据一起塞进来）。"""
        payload = dict(data)
        if not payload.get("name"):
            raise ValueError("字段定义缺少 name：%r" % (data,))
        known = {item.name for item in dataclass_fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})

    def to_dict(self) -> Dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in dataclass_fields(self)}


#: 表单声明：``[Field, ...]`` 或 ``[{"name": ...}, ...]``
FormSpec = List[Field]


def normalize_fields(spec: Sequence[Any]) -> List[Field]:
    """``FormSpec`` 归一化为 ``Field`` 列表。"""
    result: List[Field] = []
    for index, item in enumerate(spec or []):
        if isinstance(item, Field):
            result.append(item)
        elif isinstance(item, dict):
            result.append(Field.from_dict(item))
        else:
            raise TypeError("第 %d 个字段必须是 Field 或 dict，得到 %r" % (index + 1, item))
    return result


# --------------------------------------------------------------------------- 样式
def _base_size() -> int:
    try:
        return int(getattr(theme, "_state", {}).get("base", 10))
    except Exception:  # noqa: BLE001 - 主题状态异常不应影响控件构建
        return 10


def _ensure_dialog_styles(widget: tk.Misc) -> None:
    """弹窗自己的几个 ttk 样式（幂等；不修改 theme.py）。"""
    base = _base_size()
    style = ttk.Style(widget)
    bg = theme.COLORS["bg"]
    style.configure("DialogTitle.TLabel", background=bg, foreground=theme.COLORS["text"],
                    font=theme.font(base + 4, "bold"))
    style.configure("DialogHint.TLabel", background=bg, foreground=theme.COLORS["text2"],
                    font=theme.font(base - 1))
    style.configure("DialogError.TLabel", background=bg, foreground=theme.COLORS["up"],
                    font=theme.font(base - 1))
    style.configure("DialogOk.TLabel", background=bg, foreground=theme.COLORS["down"],
                    font=theme.font(base - 1))
    style.configure("Dialog.TCheckbutton", background=bg, foreground=theme.COLORS["text"],
                    font=theme.font(base))
    style.map("Dialog.TCheckbutton", background=[("active", bg)])


# --------------------------------------------------------------------------- FormDialog
class FormDialog(tk.Toplevel):
    """模态表单弹窗。

    构造时只建控件并保持隐藏（避免闪烁），:meth:`show` 才显示窗口、抢输入焦点并 ``wait_window``；
    校验走纯函数 :meth:`FormDialog.validate`，提交逻辑由 ``on_submit`` 提供。

    只想跑测试/自定义流程时可以不调 ``show()``：直接 :meth:`submit` / :meth:`cancel`。
    """

    def __init__(self, master: tk.Misc, title: str, fields: Sequence[Any],
                 values: Optional[Dict[str, Any]] = None, submit_label: str = "保存",
                 on_submit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 hint: Optional[str] = None, width: int = 460):
        # 先做字段声明校验（纯 Python，不建窗口）：声明写错时不会留下悬空 Toplevel
        form_fields = normalize_fields(fields)
        for index, form_field in enumerate(form_fields):
            if form_field.type not in FIELD_TYPES:
                raise ValueError("第 %d 个字段 %r 的类型 %r 不支持（可用：%s）" % (
                    index + 1, form_field.name, form_field.type, " / ".join(FIELD_TYPES)))

        super().__init__(master)
        self.withdraw()                     # 构造期间隐藏：避免闪烁，也避免异常留下悬空窗口
        self.form_fields: List[Field] = form_fields
        self._field_by_name: Dict[str, Field] = {f.name: f for f in form_fields}

        self.on_submit = on_submit
        self.submit_label = str(submit_label)
        self.hint = hint
        self.width = int(width or 460)
        self.result: Optional[Dict[str, Any]] = None

        self._initial: Dict[str, Any] = dict(values or {})
        self.inputs: Dict[str, tk.Widget] = {}
        self.entries: Dict[str, tk.Widget] = self.inputs          # 别名，便于外部取控件
        self.vars: Dict[str, tk.Variable] = {}
        self.labels: Dict[str, ttk.Label] = {}
        self.helps: Dict[str, ttk.Label] = {}
        self._json_texts: Dict[str, tk.Text] = {}
        self._json_hints: Dict[str, ttk.Label] = {}
        self.json_texts = self._json_texts        # 公开别名（页面/测试可直接取控件）
        self.json_hints = self._json_hints
        self._submitting = False

        self.title(str(title))
        self.configure(bg=theme.COLORS["bg"])
        try:
            self.transient(master)
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        self.withdraw()                     # 构造时先隐藏，显示交给 show()（模态入口）
        _ensure_dialog_styles(self)
        self._build()
        self._bind_keys()
        self._focus_first()
        # 注意：不在这里 grab_set——窗口还没显示，抢输入会让整个界面像卡死；
        # 模态抓取统一放在 show() 里（那里先 deiconify 再 grab_set）。

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        container = ttk.Frame(self, style="TFrame", padding=(16, 14))
        container.pack(fill="both", expand=True)

        self.title_label = ttk.Label(container, text=self.title(), style="DialogTitle.TLabel",
                                     anchor="w")
        self.title_label.pack(fill="x")
        self.hint_label: Optional[ttk.Label] = None
        if self.hint:
            self.hint_label = ttk.Label(container, text=str(self.hint), style="DialogHint.TLabel",
                                        anchor="w", justify="left", wraplength=max(200, self.width - 60))
            self.hint_label.pack(fill="x", pady=(4, 0))

        self.body = ttk.Frame(container, style="TFrame")
        self.body.pack(fill="both", expand=True, pady=(10, 0))
        self.body.columnconfigure(1, weight=1)
        for index, form_field in enumerate(self.form_fields):
            self._add_field(index, form_field)

        self.error_label = ttk.Label(container, text="", style="DialogError.TLabel", anchor="w",
                                     justify="left", wraplength=max(200, self.width - 60))
        self.error_label.pack(fill="x", pady=(8, 0))

        footer = ttk.Frame(container, style="TFrame")
        footer.pack(fill="x", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.cancel_button = ttk.Button(footer, text="取消", command=self.cancel)
        self.cancel_button.grid(row=0, column=1, padx=(0, 6))
        self.submit_button = ttk.Button(footer, text=self.submit_label, command=self.submit,
                                        style="Primary.TButton")
        self.submit_button.grid(row=0, column=2)

        try:
            self.geometry("%dx" % self.width)      # 只定宽，高度由内容决定
            self.minsize(self.width, 0)
            self.resizable(True, True)
        except tk.TclError:
            pass

    def _add_field(self, index: int, form_field: Field) -> None:
        row = index * 2
        label = ttk.Label(self.body, text=form_field.label + (" *" if form_field.required else ""),
                          style="TLabel", anchor="e")
        label.grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=(4, 0))
        self.labels[form_field.name] = label

        widget = self._build_input(form_field)
        widget.grid(row=row, column=1, sticky="nwe", pady=(4, 0))
        self.inputs[form_field.name] = widget

        if form_field.help:
            help_label = ttk.Label(self.body, text=str(form_field.help), style="Muted.TLabel",
                                   anchor="w", justify="left",
                                   wraplength=max(180, self.width - 180))
            help_label.grid(row=row + 1, column=1, sticky="w", pady=(0, 4))
            self.helps[form_field.name] = help_label

    def _build_input(self, form_field: Field) -> tk.Widget:
        name = form_field.name
        initial = self._initial.get(name, form_field.default)
        kind = form_field.type

        if kind == TYPE_BOOL:
            var = tk.BooleanVar(master=self, value=_to_bool(initial))
            self.vars[name] = var
            return ttk.Checkbutton(self.body, variable=var, text="", style="Dialog.TCheckbutton")

        if kind == TYPE_CHOICE:
            var = tk.StringVar(master=self, value=_as_text(initial))
            self.vars[name] = var
            combo = ttk.Combobox(self.body, textvariable=var, state="readonly",
                                 values=[str(choice) for choice in (form_field.choices or [])],
                                 width=form_field.width or 28)
            if form_field.readonly:
                combo.configure(state="disabled")
            return combo

        if kind == TYPE_JSON:
            wrapper = ttk.Frame(self.body, style="TFrame")
            text = tk.Text(wrapper, height=6, width=form_field.width or 42, wrap="word",
                           bg=theme.COLORS["bg2"], fg=theme.COLORS["text"],
                           insertbackground=theme.COLORS["text"],
                           font=theme.font(_base_size(), mono=True), relief="flat",
                           highlightthickness=1, highlightbackground=theme.COLORS["line"],
                           highlightcolor=theme.COLORS["brand"], padx=6, pady=4)
            text.insert("1.0", _as_text(initial))
            if form_field.readonly:
                text.configure(state="disabled")
            text.pack(fill="x", expand=True)
            json_hint = ttk.Label(wrapper, text="", style="DialogHint.TLabel", anchor="w",
                                  justify="left", wraplength=max(180, self.width - 180))
            json_hint.pack(anchor="w", pady=(2, 0))
            self._json_texts[name] = text
            self._json_hints[name] = json_hint
            text.bind("<KeyRelease>", lambda _e, n=name: self._update_json_hint(n))
            text.bind("<<Paste>>", lambda _e, n=name: self.after_idle(self._update_json_hint, n))
            self._update_json_hint(name)
            return wrapper

        # text / int / float / date / password
        var = tk.StringVar(master=self, value=_as_text(initial))
        self.vars[name] = var
        default_width = 18 if kind in NUMBER_TYPES else (14 if kind == TYPE_DATE else 28)
        entry = ttk.Entry(self.body, textvariable=var, width=form_field.width or default_width,
                          show="*" if kind == TYPE_PASSWORD else "")
        if form_field.readonly:
            entry.configure(state="readonly")
        return entry

    def _bind_keys(self) -> None:
        self.bind("<Escape>", lambda _e: (self.cancel(), "break")[1])
        self.bind("<Return>", self._on_return)
        self.bind("<KP_Enter>", self._on_return)

    def _on_return(self, _event: Any = None) -> Optional[str]:
        focused = self.focus_get()
        if isinstance(focused, tk.Text):
            return None                        # JSON 多行输入：回车换行
        if isinstance(focused, ttk.Button):
            return None                        # 按钮自己的绑定已处理
        self.submit()
        return "break"

    def _focus_first(self) -> Optional[tk.Widget]:
        """聚焦第一个可编辑字段（没有可编辑字段时聚焦第一个输入控件）。"""
        for form_field in self.form_fields:
            if form_field.readonly or form_field.type == TYPE_BOOL:
                continue
            widget = self.inputs.get(form_field.name)
            if widget is None:
                continue
            try:
                widget.focus_set()
            except tk.TclError:
                return None
            return widget
        for form_field in self.form_fields:
            widget = self.inputs.get(form_field.name)
            if widget is not None:
                try:
                    widget.focus_set()
                except tk.TclError:
                    return None
                return widget
        return None

    # ------------------------------------------------------------------ 取值 / 校验
    def collect(self) -> Dict[str, Any]:
        """当前控件里的原始值（未校验）。"""
        values: Dict[str, Any] = dict(self._initial)
        for form_field in self.form_fields:
            name = form_field.name
            text_widget = self._json_texts.get(name)
            if text_widget is not None:
                values[name] = text_widget.get("1.0", "end-1c")
                continue
            var = self.vars.get(name)
            if var is None:
                if name not in values:
                    values[name] = form_field.default
                continue
            values[name] = bool(var.get()) if form_field.type == TYPE_BOOL else var.get()
        return values

    def get_values(self) -> Dict[str, Any]:
        return self.collect()

    def set_values(self, mapping: Dict[str, Any]) -> "FormDialog":
        """程序化写值（页面预填 / 测试用）。"""
        for name, value in dict(mapping or {}).items():
            text_widget = self._json_texts.get(name)
            if text_widget is not None:
                text_widget.delete("1.0", "end")
                text_widget.insert("1.0", _as_text(value))
                self._update_json_hint(name)
                continue
            var = self.vars.get(name)
            if var is None:
                self._initial[name] = value
                continue
            form_field = self._field_by_name.get(name)
            if form_field is not None and form_field.type == TYPE_BOOL:
                var.set(_to_bool(value))
            else:
                var.set(_as_text(value))
        return self

    @staticmethod
    def validate(fields: Sequence[Any], values: Optional[Dict[str, Any]]
                 ) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """纯函数校验：→ ``(clean_values, errors)``；``errors`` 为 ``{字段名: 提示}``。"""
        form_fields = normalize_fields(fields)
        source = dict(values or {})
        clean: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        known = set()

        for form_field in form_fields:
            name = form_field.name
            known.add(name)
            raw = source.get(name)
            kind = form_field.type

            if kind not in FIELD_TYPES:
                errors[name] = "不支持的字段类型：%s" % kind
                continue

            if kind == TYPE_BOOL:
                clean[name] = _to_bool(raw if raw is not None else form_field.default)
                continue

            if kind == TYPE_JSON:
                value = raw if raw is not None else form_field.default
                if isinstance(value, dict):
                    clean[name] = value
                    continue
                if isinstance(value, (list, tuple)):
                    errors[name] = "必须是 JSON 对象（{...}），不能是数组"
                    continue
                text = "" if value is None else str(value).strip()
                if text == "":
                    if form_field.required:
                        errors[name] = "不能为空"
                    else:
                        clean[name] = {}
                    continue
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    errors[name] = "JSON 格式错误：%s" % exc
                    continue
                if not isinstance(parsed, dict):
                    errors[name] = "必须是 JSON 对象（{...}）"
                    continue
                clean[name] = parsed
                continue

            if kind in NUMBER_TYPES:
                text = "" if raw is None else str(raw).strip()
                if text == "":
                    if form_field.required:
                        errors[name] = "不能为空"
                    else:
                        clean[name] = None
                    continue
                number = _parse_number(text, kind)
                if number is None:
                    errors[name] = "请输入整数" if kind == TYPE_INT else "请输入数字"
                    continue
                if form_field.min is not None and number < form_field.min:
                    errors[name] = "不能小于 %s" % _number_text(form_field.min)
                    continue
                if form_field.max is not None and number > form_field.max:
                    errors[name] = "不能大于 %s" % _number_text(form_field.max)
                    continue
                clean[name] = number
                continue

            # text / password / date / choice
            if kind == TYPE_PASSWORD:
                text = "" if raw is None else str(raw)
                if form_field.required and text.strip() == "":
                    errors[name] = "不能为空"
                    continue
                clean[name] = text
                continue

            text = "" if raw is None else str(raw).strip()
            if form_field.required and text == "":
                errors[name] = "不能为空"
                continue
            if kind == TYPE_DATE and text:
                if not _valid_date(text):
                    errors[name] = "日期格式应为 YYYY-MM-DD"
                    continue
            if kind == TYPE_CHOICE and text:
                choices = [str(choice) for choice in (form_field.choices or [])]
                if choices and text not in choices:
                    errors[name] = "只能选择：%s" % "、".join(choices)
                    continue
            clean[name] = text

        for extra_key, extra_value in source.items():      # 透传未声明字段（隐藏值）
            if extra_key not in known:
                clean[extra_key] = extra_value
        return clean, errors

    # ------------------------------------------------------------------ 提交 / 关闭
    def submit(self) -> bool:
        """校验 → 调 ``on_submit`` → 成功则关闭。返回是否通过。"""
        if self._submitting:
            return False
        self._submitting = True
        try:
            clean, errors = self.validate(self.form_fields, self.collect())
            if errors:
                self.show_errors(errors)
                return False
            self._clear_errors()
            if callable(self.on_submit):
                try:
                    outcome = self.on_submit(clean)
                except Exception as exc:  # noqa: BLE001 - 业务异常显示在弹窗里，不冒泡
                    self.show_error("提交失败：%s" % exc)
                    return False
                if outcome is False:
                    return False
                if isinstance(outcome, str) and outcome.strip():
                    self.show_error(outcome.strip())
                    return False
            self.result = clean
            self._close()
            return True
        finally:
            self._submitting = False

    def cancel(self) -> None:
        """取消：``result`` 保持 None。"""
        self.result = None
        self._close()

    def show(self) -> Optional[Dict[str, Any]]:
        """模态显示并等待关闭；返回提交后的 values 或 None（取消）。"""
        if self.winfo_exists():
            try:
                self.deiconify()
                self.lift()
            except tk.TclError:
                pass
            self._try_grab()
            self._focus_first()
            self.wait_window(self)
        return self.result

    def show_error(self, text: str) -> None:
        """在弹窗内显示一条错误（不清空字段高亮）。"""
        try:
            self.error_label.configure(text=str(text))
        except tk.TclError:
            pass

    def show_errors(self, errors: Dict[str, str]) -> None:
        """按字段显示校验错误，并把出错字段的标签标红。"""
        self._clear_errors()
        lines = []
        for name, message in (errors or {}).items():
            form_field = self._field_by_name.get(name)
            label_text = form_field.label if form_field is not None else name
            lines.append("「%s」%s" % (label_text, message))
            label = self.labels.get(name)
            if label is not None:
                try:
                    label.configure(style="DialogError.TLabel")
                except tk.TclError:
                    pass
        self.show_error("\n".join(lines))

    def _clear_errors(self) -> None:
        try:
            self.error_label.configure(text="")
        except tk.TclError:
            pass
        for label in self.labels.values():
            try:
                label.configure(style="TLabel")
            except tk.TclError:
                pass

    def _try_grab(self) -> None:
        try:
            if self.grab_current() is not self:
                self.grab_set()
        except tk.TclError:
            pass                            # 无窗口管理器/未 viewable 时忽略，show() 会再试

    def _close(self) -> None:
        try:
            if self.grab_current() is self:
                self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ JSON 实时提示
    def _update_json_hint(self, name: str) -> None:
        text_widget = self._json_texts.get(name)
        hint_label = self._json_hints.get(name)
        if text_widget is None or hint_label is None:
            return
        form_field = self._field_by_name.get(name)
        required = bool(form_field.required) if form_field is not None else False
        try:
            raw = text_widget.get("1.0", "end-1c").strip()
        except tk.TclError:
            return
        if raw == "":
            hint_label.configure(text="必填：JSON 对象，例如 {\"key\": 1}" if required
                                 else "可留空（提交时视为 {}）", style="DialogHint.TLabel")
            return
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            hint_label.configure(text="✗ JSON 格式错误：%s" % exc, style="DialogError.TLabel")
            return
        if not isinstance(parsed, dict):
            hint_label.configure(text="✗ 必须是 JSON 对象（{...}）", style="DialogError.TLabel")
            return
        hint_label.configure(text="✓ JSON 有效（%d 个键）" % len(parsed), style="DialogOk.TLabel")


def open_form(master: tk.Misc, title: str, fields: Sequence[Any],
              **kwargs: Any) -> Optional[Dict[str, Any]]:
    """便捷函数：构造 + 模态显示 ``FormDialog``。"""
    return FormDialog(master, title, fields, **kwargs).show()


__all__ = ["Field", "FormSpec", "FormDialog", "open_form", "normalize_fields", "normalize_type",
           "FIELD_TYPES", "NUMBER_TYPES", "TYPE_TEXT", "TYPE_INT", "TYPE_FLOAT", "TYPE_CHOICE",
           "TYPE_BOOL", "TYPE_JSON", "TYPE_DATE", "TYPE_PASSWORD"]
