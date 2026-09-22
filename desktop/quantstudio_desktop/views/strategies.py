# -*- coding: utf-8 -*-
"""策略管理页面：策略库列表 + 右侧详情 + 新建 / 删除 / 跳转回测。

契约见 ``views/__init__.py``：``build()`` 只建一次控件，``reload()`` 只刷新数据。
所有服务调用都走 ``self.load``（后台线程 + 主线程回调），不阻塞界面。
``..widgets`` 下的控件子模块若缺失，页面显示内联错误而不是崩溃。
"""

import importlib
import json
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from .. import theme
from . import BaseView

ORIGIN_LABELS = {"builtin": "内置", "local": "本地代码", "user": "自定义"}
STATUS_LABELS = {"running": "运行中", "paused": "已暂停"}
CATEGORY_CHOICES = ["趋势跟踪", "动量", "震荡市", "稳健", "均值回归", "自定义"]
STATUS_CHOICES = ["running", "paused"]
FREQ_CHOICES = ["日线", "周度", "月度", "盘中"]
FILTER_CHOICES = ["全部", "代码策略", "自定义"]

STRATEGY_COLUMNS = [
    {"key": "name", "label": "名称", "width": 156, "anchor": "w", "kind": "text"},
    {"key": "category", "label": "类别", "width": 82, "anchor": "w", "kind": "text"},
    {"key": "origin_text", "label": "来源", "width": 84, "anchor": "center", "kind": "badge"},
    {"key": "status_text", "label": "状态", "width": 72, "anchor": "center", "kind": "badge"},
    {"key": "param_count", "label": "参数个数", "width": 78, "anchor": "center", "kind": "int"},
    {"key": "min_bars", "label": "最少 bar", "width": 78, "anchor": "center", "kind": "int"},
]

PARAM_COLUMNS = [
    {"key": "key", "label": "参数", "width": 104, "anchor": "w", "kind": "code"},
    {"key": "label", "label": "名称", "width": 104, "anchor": "w", "kind": "text"},
    {"key": "value", "label": "当前值", "width": 88, "anchor": "e", "kind": "text"},
    {"key": "range", "label": "范围", "width": 116, "anchor": "w", "kind": "text"},
    {"key": "help", "label": "说明", "width": 220, "anchor": "w", "kind": "text"},
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


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _choice_value(value: Any) -> str:
    """下拉返回值可能是 ``id · 名称`` 形式，取 ``·`` 前的部分。"""
    text = str(value or "").strip()
    for sep in ("·", "|", " - "):
        if sep in text:
            head = text.split(sep)[0].strip()
            if head:
                return head
    return text


class StrategiesView(BaseView):
    """内置 / 本地代码 / 自定义策略的统一管理页。"""

    title = "策略管理"

    def __init__(self, parent: Any, app: Any):
        super().__init__(parent, app)
        self._mods: Dict[str, Any] = {}
        self._missing: List[str] = []
        self._degraded = False
        self._suppress_select = False
        self._strategies: List[Dict[str, Any]] = []
        self._display_rows: List[Dict[str, Any]] = []
        self._row_by_id: Dict[str, Dict[str, Any]] = {}
        self._selected_id = ""
        self._filter = tk.StringVar(master=self, value=FILTER_CHOICES[0])
        self._detail_title = tk.StringVar(master=self, value="未选择策略")
        self._loading = False

    # ------------------------------------------------------------------ 构建
    def build(self) -> None:
        self._mods, self._missing = load_widgets("cards", "table", "forms")
        if self._mods.get("cards") is None or self._mods.get("table") is None:
            self._degraded = True
            self._build_inline_error(
                "策略管理页面缺少控件模块：%s\n请确认 quantstudio_desktop/widgets/ 下的 "
                "cards.py 与 table.py 已就位。" % "、".join(self._missing)
            )
            return
        try:
            self._build_ui()
        except Exception as exc:                      # noqa: BLE001 - 控件构建失败降级为内联错误
            self._degraded = True
            self._build_inline_error("策略管理页面构建失败：%s" % exc)

    def _build_inline_error(self, message: str) -> None:
        box = ttk.Frame(self, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=message, style="Card.TLabel", wraplength=760,
                  justify="left").pack(anchor="w")

    def _place(self, widget: Any, **pack_options: Any) -> Any:
        """控件若已由工厂函数自行布局（pack/grid）则不再重复放置。"""
        try:
            if not widget.winfo_manager():
                widget.pack(**pack_options)
        except Exception:                             # noqa: BLE001
            pass
        return widget

    def _mount(self, widget: Any, container: Any, row: int = 0, column: int = 0,
               **grid_options: Any) -> Any:
        """放进使用 grid 的容器（控件自己已管理布局时保持原样，避免 pack/grid 混用）。"""
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
        cards = self._mods["cards"]
        table_mod = self._mods["table"]
        SectionTitle = widget_class(self._mods, "cards", "SectionTitle")
        KeyValueTable = widget_class(self._mods, "cards", "KeyValueTable")

        # ---- 工具行：标题（含新建按钮）+ 筛选
        if SectionTitle is not None:
            arguments = {"hint": "内置 / 本地代码 / 自定义策略统一管理"}
            try:
                title = SectionTitle(self, "策略库", action=("+ 新建策略", self._new_strategy),
                                     **arguments)
            except Exception:                         # noqa: BLE001 - action 契约不匹配时退化
                title = SectionTitle(self, "策略库", **arguments)
                self._fallback_new_button = True
            else:
                self._fallback_new_button = False
            self._place(title, fill="x", pady=(0, 4))
        else:
            ttk.Label(self, text="策略库", style="Title.TLabel").pack(anchor="w")
            self._fallback_new_button = True

        filters = ttk.Frame(self, style="TFrame")
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="筛选", style="Muted.TLabel").pack(side="left")
        self._filter_box = ttk.Combobox(filters, textvariable=self._filter, values=FILTER_CHOICES,
                                        state="readonly", width=12)
        self._filter_box.pack(side="left", padx=(6, 0))
        self._filter_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())
        ttk.Button(filters, text="刷新", command=self.refresh).pack(side="left", padx=8)
        if getattr(self, "_fallback_new_button", False):
            ttk.Button(filters, text="+ 新建策略", style="Primary.TButton",
                       command=self._new_strategy).pack(side="left")

        # ---- 主体：左列表 + 右详情
        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=3, minsize=380)
        body.columnconfigure(1, weight=4, minsize=420)

        left = ttk.Frame(body, style="Card.TFrame", padding=(8, 8))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._table = table_mod.DataTable(left, STRATEGY_COLUMNS, height=14,
                                         on_select=self._on_table_select,
                                         on_double_click=self._on_table_double_click,
                                         sortable=True)
        self._mount(self._table, left, sticky="nsew")

        right = ttk.Frame(body, style="Card.TFrame", padding=(12, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(5, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, textvariable=self._detail_title, style="H2.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))

        if KeyValueTable is not None:
            self._kv = KeyValueTable(right)
            self._mount(self._kv, right, row=1, sticky="ew")
        else:
            self._kv = None

        ttk.Label(right, text="描述", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w",
                                                                   pady=(10, 2))
        self._desc = tk.Text(right, height=5, wrap="word", bg=theme.COLORS["bg2"],
                             fg=theme.COLORS["text"], relief="flat", padx=8, pady=6,
                             insertbackground=theme.COLORS["text"], font=theme.font(10))
        self._desc.grid(row=3, column=0, sticky="nsew")
        self._desc.configure(state="disabled")

        ttk.Label(right, text="参数", style="CardMuted.TLabel").grid(row=4, column=0, sticky="w",
                                                                   pady=(10, 2))
        self._params = table_mod.DataTable(right, PARAM_COLUMNS, height=7, sortable=True)
        self._mount(self._params, right, row=5, sticky="nsew")

        actions = ttk.Frame(right, style="Card.TFrame")
        actions.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="新建策略", style="Primary.TButton",
                   command=self._new_strategy).pack(side="left")
        self._delete_btn = ttk.Button(actions, text="删除", style="Danger.TButton",
                                      command=self._delete_strategy)
        self._delete_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="查看回测", command=self._open_backtest).pack(side="left")
        self._delete_btn.configure(state="disabled")

    # ------------------------------------------------------------------ 数据
    def reload(self) -> None:
        if self._degraded:
            return
        self._loading = True
        self.status("正在加载策略库…")
        self.load(self.services.strategies, on_done=self._on_strategies,
                  on_error=self._on_load_error, name="strategies")

    def _on_load_error(self, exc: BaseException) -> None:
        self._loading = False
        self.toast_error(exc)
        try:
            self._detail_title.set("策略库加载失败")
            self._set_desc("策略库加载失败：%s" % exc)
        except Exception:                             # noqa: BLE001
            pass

    def _on_strategies(self, rows: Any) -> None:
        self._loading = False
        data = rows if isinstance(rows, list) else []
        self._strategies = [item for item in data if isinstance(item, dict)]
        self._row_by_id = {str(item.get("id") or ""): item for item in self._strategies}
        self._apply_filter()
        self.status("策略库：%d 个策略" % len(self._strategies))

    def _apply_filter(self) -> None:
        if self._degraded or not hasattr(self, "_table"):
            return
        selected = str(self._filter.get() or FILTER_CHOICES[0])
        rows: List[Dict[str, Any]] = []
        for item in self._strategies:
            origin = str(item.get("origin") or ("builtin" if item.get("builtin") else "user"))
            if selected == "代码策略" and origin == "user":
                continue
            if selected == "自定义" and origin != "user":
                continue
            rows.append(self._view_row(item, origin))
        self._display_rows = rows
        wanted = self._selected_id
        self._suppress_select = True
        try:
            self._table.set_rows(rows, key_field="id", preserve_selection=True)
        except Exception as exc:                      # noqa: BLE001
            self._set_desc("策略列表渲染失败：%s" % exc)
        finally:
            self._suppress_select = False
        self._restore_selection(wanted)

    @staticmethod
    def _view_row(item: Dict[str, Any], origin: str) -> Dict[str, Any]:
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        row = dict(item)
        row["origin"] = origin
        row["origin_text"] = ORIGIN_LABELS.get(origin, origin or "—")
        row["status_text"] = STATUS_LABELS.get(str(item.get("status") or ""),
                                               _text(item.get("status")))
        row["param_count"] = len(params)
        row["min_bars"] = _int(item.get("min_bars"), 0)
        row["name"] = _text(item.get("name"), _text(item.get("id")))
        row["category"] = _text(item.get("category"))
        return row

    def _restore_selection(self, wanted: str = "") -> None:
        """刷新后按 id 恢复选中项；没有任何选中项时默认选中第一行。"""
        wanted = wanted or self._selected_id
        if wanted and wanted in self._row_by_id:
            for index, row in enumerate(self._display_rows):
                if str(row.get("id") or "") == wanted:
                    try:
                        self._table.select_index(index, notify=False)
                    except Exception:                 # noqa: BLE001
                        pass
                    self._render_detail(self._row_by_id[wanted])
                    self._sync_delete_button(self._row_by_id[wanted])
                    return
        self._selected_id = ""
        if self._display_rows:
            try:
                self._table.select_index(0)
            except Exception:                         # noqa: BLE001
                pass
            if not self._selected_id:                 # 表格未回调时兜底
                self._select_row(self._display_rows[0])
            return
        self._render_detail(None)

    # ------------------------------------------------------------------ 选择
    def _on_table_select(self, *_args: Any) -> None:
        if self._degraded or self._suppress_select:
            return
        row = self._row_from_selection(self._safe_selected())
        if row is not None:
            self._select_row(row)

    def _on_table_double_click(self, *_args: Any) -> None:
        if not self._degraded:
            self._open_backtest()

    def _safe_selected(self) -> Any:
        try:
            return self._table.selected()
        except Exception:                             # noqa: BLE001
            return None

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

    def _select_row(self, row: Dict[str, Any]) -> None:
        sid = str(row.get("id") or "")
        if not sid:
            return
        self._selected_id = sid
        self._sync_table_selection(sid)
        self._render_detail(row)
        self._sync_delete_button(row)
        self.load(self.services.strategy, sid, on_done=self._on_detail,
                  on_error=self.toast_error, name="strategy-detail")

    def _sync_table_selection(self, sid: str) -> None:
        """让表格高亮跟随逻辑选中项（``notify=False`` 避免回调递归）。"""
        for index, row in enumerate(self._display_rows):
            if str(row.get("id") or "") == sid:
                try:
                    self._table.select_index(index, notify=False)
                except Exception:                     # noqa: BLE001
                    pass
                return

    def _sync_delete_button(self, row: Optional[Dict[str, Any]]) -> None:
        if not hasattr(self, "_delete_btn"):
            return
        origin = str((row or {}).get("origin") or "")
        try:
            self._delete_btn.configure(state="normal" if origin == "user" else "disabled")
        except Exception:                             # noqa: BLE001
            pass

    def _on_detail(self, spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        if str(spec.get("id") or "") != self._selected_id:
            return                                    # 过期响应（用户已切换）
        self._render_detail(spec)

    def _current(self) -> Optional[Dict[str, Any]]:
        return self._row_by_id.get(self._selected_id)

    # ------------------------------------------------------------------ 详情渲染
    def _render_detail(self, spec: Optional[Dict[str, Any]]) -> None:
        if self._degraded:
            return
        if not isinstance(spec, dict):
            self._detail_title.set("未选择策略")
            if self._kv is not None:
                try:
                    self._kv.set_items([("提示", "从左侧列表选择一个策略查看详情")])
                except Exception:                     # noqa: BLE001
                    pass
            self._set_desc("")
            try:
                self._params.clear()
            except Exception:                         # noqa: BLE001
                pass
            if hasattr(self, "_delete_btn"):
                self._delete_btn.configure(state="disabled")
            return

        origin = str(spec.get("origin") or ("builtin" if spec.get("builtin") else "user"))
        params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        self._detail_title.set("%s（%s）" % (_text(spec.get("name")), _text(spec.get("id"))))
        if self._kv is not None:
            items = [
                ("ID", _text(spec.get("id"))),
                ("类别", _text(spec.get("category"))),
                ("来源", ORIGIN_LABELS.get(origin, origin)),
                ("状态", STATUS_LABELS.get(str(spec.get("status") or ""), _text(spec.get("status")))),
                ("频率", _text(spec.get("freq"))),
                ("标的池", _text(spec.get("universe"))),
                ("最少 bar", str(_int(spec.get("min_bars"), 0))),
                ("参数个数", str(len(params))),
                ("版本", _text(spec.get("version"))),
            ]
            try:
                self._kv.set_items(items)
            except Exception:                         # noqa: BLE001
                pass
        self._set_desc(_text(spec.get("desc"), "（无描述）"))
        self._render_params(params, spec.get("param_schema") or {})
        self._sync_delete_button(spec)

    def _render_params(self, params: Dict[str, Any], schema: Any) -> None:
        schema = schema if isinstance(schema, dict) else {}
        rows: List[Dict[str, Any]] = []
        keys: List[str] = []
        for key in schema:
            keys.append(str(key))
        for key in params:
            if str(key) not in keys:
                keys.append(str(key))
        for key in keys:
            meta = schema.get(key) if isinstance(schema.get(key), dict) else {}
            raw = params.get(key, meta.get("default"))
            rows.append({
                "key": key,
                "label": _text(meta.get("label"), ""),
                "value": self._param_text(raw),
                "range": self._range_text(meta),
                "help": _text(meta.get("help"), ""),
            })
        try:
            self._params.set_rows(rows, key_field="key")
        except Exception as exc:                      # noqa: BLE001
            self._set_desc("参数渲染失败：%s" % exc)

    @staticmethod
    def _param_text(value: Any) -> str:
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, (int, float)):
            if value == int(value):
                return str(int(value))
            return theme.fmt_num(value, 4).rstrip("0").rstrip(".")
        return _text(value)

    @staticmethod
    def _range_text(meta: Dict[str, Any]) -> str:
        low, high = meta.get("min"), meta.get("max")
        ptype = str(meta.get("type") or "")
        if low is None and high is None:
            return ptype or "—"
        return "%s ~ %s" % ("—" if low is None else theme.fmt_num(low, 4),
                            "—" if high is None else theme.fmt_num(high, 4))

    def _set_desc(self, text: str) -> None:
        if not hasattr(self, "_desc"):
            return
        try:
            self._desc.configure(state="normal")
            self._desc.delete("1.0", "end")
            if text:
                self._desc.insert("1.0", text)
            self._desc.configure(state="disabled")
        except Exception:                             # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 动作
    def _notify(self, text: str, kind: str = "info") -> None:
        try:
            self.toast.show(text, kind=kind)
        except Exception:                             # noqa: BLE001
            self.status(text)

    def _new_strategy(self) -> None:
        if self._degraded:
            return
        FormDialog = widget_class(self._mods, "forms", "FormDialog")
        if FormDialog is None:
            self._notify("表单控件（forms）不可用，无法新建策略：%s" % "、".join(self._missing),
                         kind="error")
            return
        templates = [item for item in self._strategies
                     if str(item.get("origin") or "builtin") != "user"]
        if not templates:
            self._notify("策略库为空或尚未加载完成，请先刷新", kind="warn")
            return
        choices = ["%s · %s" % (_text(item.get("id")), _text(item.get("name"))) for item in templates]
        fields = [
            {"name": "name", "label": "策略名称", "type": "text", "required": True,
             "help": "2~24 个字符，不能与已有策略重名"},
            {"name": "template", "label": "模板策略", "type": "choice", "choices": choices,
             "default": choices[0], "help": "用户策略 = 内置模板 + 一组参数"},
            {"name": "category", "label": "类别", "type": "choice", "choices": list(CATEGORY_CHOICES),
             "default": "自定义"},
            {"name": "status", "label": "状态", "type": "choice", "choices": list(STATUS_CHOICES),
             "default": "paused", "help": "running=运行中，paused=暂停"},
            {"name": "freq", "label": "频率", "type": "choice", "choices": list(FREQ_CHOICES),
             "default": "日线"},
            {"name": "universe", "label": "标的池", "type": "text", "default": "",
             "help": "人类可读说明，例如「自选股」"},
            {"name": "desc", "label": "描述", "type": "text", "default": "",
             "help": "不超过 200 字"},
            {"name": "params", "label": "参数（JSON）", "type": "json", "default": "{}",
             "help": '键需落在模板参数表内，例如 {"short_ma": 10, "long_ma": 30}'},
        ]
        try:
            data = FormDialog(self, "新建策略", fields, values=None, submit_label="创建",
                              on_submit=None, hint="参数会按模板的 param_schema 校验").show()
        except Exception as exc:                      # noqa: BLE001
            self.toast_error(exc)
            return
        if not data:
            return
        payload = self._payload_from_form(data)
        if payload is None:
            return
        self.status("正在创建策略：%s …" % payload.get("name"))
        self.load(self.services.create_strategy, payload, on_done=self._on_created,
                  on_error=self.toast_error, name="create-strategy")

    def _payload_from_form(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(data.get("name") or "").strip()
        if not (2 <= len(name) <= 24):
            self._notify("策略名称需为 2~24 个字符（当前 %d 个）" % len(name), kind="error")
            return None
        template = _choice_value(data.get("template"))
        if not template:
            self._notify("请选择模板策略", kind="error")
            return None
        raw_params = data.get("params")
        if isinstance(raw_params, str):
            text = raw_params.strip() or "{}"
            try:
                raw_params = json.loads(text)
            except ValueError as exc:
                self._notify("参数需为合法 JSON：%s" % exc, kind="error")
                return None
        if raw_params in (None, ""):
            raw_params = {}
        if not isinstance(raw_params, dict):
            self._notify("参数需为 JSON 对象（dict）", kind="error")
            return None
        payload = {
            "name": name,
            "template": template,
            "category": _choice_value(data.get("category")) or "自定义",
            "status": _choice_value(data.get("status")) or "paused",
            "freq": _choice_value(data.get("freq")) or "日线",
            "universe": str(data.get("universe") or ""),
            "desc": str(data.get("desc") or ""),
            "params": raw_params,
        }
        return payload

    def _on_created(self, spec: Any) -> None:
        sid = str((spec or {}).get("id") or "") if isinstance(spec, dict) else ""
        self._notify("策略已创建：%s" % (_text((spec or {}).get("name")) if isinstance(spec, dict)
                                        else sid), kind="ok")
        if sid:
            self._selected_id = sid
        self.reload()

    def _delete_strategy(self) -> None:
        spec = self._current()
        if spec is None:
            self._notify("请先在左侧选择一个策略", kind="warn")
            return
        origin = str(spec.get("origin") or ("builtin" if spec.get("builtin") else "user"))
        if origin != "user":
            self._notify("只有「自定义」策略可以删除（当前来源：%s）"
                         % ORIGIN_LABELS.get(origin, origin), kind="warn")
            return
        sid = str(spec.get("id") or "")
        name = _text(spec.get("name"), sid)
        try:
            confirmed = messagebox.askyesno(
                "删除策略", "确定删除自定义策略「%s」？\n该操作不可撤销。" % name, parent=self)
        except Exception as exc:                      # noqa: BLE001
            self.toast_error(exc)
            return
        if not confirmed:
            return
        self.status("正在删除策略：%s …" % name)
        self.load(self.services.delete_strategy, sid, on_done=self._on_deleted,
                  on_error=self.toast_error, name="delete-strategy")

    def _on_deleted(self, result: Any) -> None:
        self._selected_id = ""
        self._notify("策略已删除：%s" % _text((result or {}).get("id") if isinstance(result, dict)
                                             else result), kind="ok")
        self.reload()

    def _open_backtest(self) -> None:
        spec = self._current()
        if spec is None:
            self._notify("请先在左侧选择一个策略", kind="warn")
            return
        sid = str(spec.get("id") or "")
        if not sid:
            return
        try:
            self.app.pending_backtest_strategy = sid
            self.app.select_view("backtest")
        except Exception as exc:                      # noqa: BLE001
            self.toast_error(exc)
            return
        self.status("已跳转到回测分析：%s" % _text(spec.get("name"), sid))


__all__ = ["StrategiesView"]
