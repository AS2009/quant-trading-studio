# -*- coding: utf-8 -*-
"""控件层测试。

两部分：
1. **纯函数**：``FormDialog.validate`` 的各类型校验（不需要 Tk，12/Tk-less 环境也能跑）；
2. **GUI 烟雾测试**：真的建 ``Tk()``（失败则整类 ``skipTest``），构造 DataTable / 卡片 /
   FormDialog 后断言控件与文本，不进入 ``wait_window`` 事件循环。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.dirname(HERE)
if DESKTOP not in sys.path:
    sys.path.insert(0, DESKTOP)

try:
    import tkinter as tk
    from tkinter import ttk
except Exception as exc:  # noqa: BLE001 - 无 tkinter 时只跳过 GUI 部分
    tk = None
    ttk = None
    TK_IMPORT_ERROR = exc
else:
    TK_IMPORT_ERROR = None

from quantstudio_desktop import theme
from quantstudio_desktop.widgets import cards, forms, table


# --------------------------------------------------------------------------- 工具
def _tags_of(tree, iid):
    tags = tree.item(iid, "tags")
    if isinstance(tags, str):
        return (tags,) if tags else ()
    return tuple(tags or ())


def _style_of(widget):
    try:
        return str(widget.cget("style"))
    except Exception:  # noqa: BLE001
        return ""


class _Event(object):
    """最小事件对象：给点击处理器喂坐标（无显示器时无法真的点鼠标）。"""

    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class ValidateTests(unittest.TestCase):
    """``FormDialog.validate``：纯函数，不碰 Tk。"""

    FIELDS = [
        forms.Field("symbol", "证券代码", required=True),
        forms.Field("qty", "数量", type="int", required=True, min=100, max=100000),
        forms.Field("price", "价格", type="float", min=0.01, max=1000000),
        forms.Field("side", "方向", type="choice", choices=["买入", "卖出"], default="买入"),
        forms.Field("dry_run", "仅预演", type="bool", default=True),
        forms.Field("extra", "扩展参数", type="json"),
        forms.Field("when", "生效日期", type="date"),
        forms.Field("secret", "密钥", type="password"),
    ]

    def _valid_values(self):
        return {"symbol": "600519.SH", "qty": "300", "price": "1688.5", "side": "买入",
                "dry_run": "true", "extra": '{"stop_loss": -5}', "when": "2024-01-02",
                "secret": "s3cret"}

    def test_missing_required(self):
        clean, errors = forms.FormDialog.validate(self.FIELDS, {})
        self.assertIn("symbol", errors)
        self.assertIn("qty", errors)
        self.assertEqual(errors["symbol"], "不能为空")
        self.assertNotIn("symbol", clean)

    def test_valid_values_are_typed(self):
        clean, errors = forms.FormDialog.validate(self.FIELDS, self._valid_values())
        self.assertEqual(errors, {})
        self.assertEqual(clean["qty"], 300)
        self.assertIsInstance(clean["qty"], int)
        self.assertAlmostEqual(clean["price"], 1688.5)
        self.assertEqual(clean["side"], "买入")
        self.assertIs(clean["dry_run"], True)
        self.assertEqual(clean["extra"], {"stop_loss": -5})
        self.assertEqual(clean["when"], "2024-01-02")
        self.assertEqual(clean["secret"], "s3cret")

    def test_int_out_of_range_and_not_integer(self):
        values = self._valid_values()
        values["qty"] = "99"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["qty"], "不能小于 100")

        values["qty"] = "100001"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["qty"], "不能大于 100000")

        values["qty"] = "300.5"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["qty"], "请输入整数")

    def test_float_bounds_and_nan(self):
        values = self._valid_values()
        values["price"] = "0"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["price"], "不能小于 0.01")

        values["price"] = "abc"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["price"], "请输入数字")

        values["price"] = "nan"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["price"], "请输入数字")

    def test_optional_number_empty_becomes_none(self):
        clean, errors = forms.FormDialog.validate(self.FIELDS, {"symbol": "A", "qty": "100"})
        self.assertEqual(errors, {})
        self.assertIsNone(clean["price"])

    def test_choice_whitelist(self):
        values = self._valid_values()
        values["side"] = "做空"
        clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertIn("只能选择", errors["side"])
        self.assertNotIn("side", clean)

    def test_json_errors_and_ok(self):
        values = self._valid_values()
        values["extra"] = "{not json"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertIn("JSON 格式错误", errors["extra"])

        values["extra"] = "[1, 2, 3]"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertIn("必须是 JSON 对象", errors["extra"])

        values["extra"] = ""
        clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors, {})
        self.assertEqual(clean["extra"], {})

        values["extra"] = {"already": "dict"}
        clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(clean["extra"], {"already": "dict"})

    def test_date_format(self):
        values = self._valid_values()
        values["when"] = "2024/01/02"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["when"], "日期格式应为 YYYY-MM-DD")

        values["when"] = "2024-02-30"
        _clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors["when"], "日期格式应为 YYYY-MM-DD")

        values["when"] = ""
        clean, errors = forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(errors, {})
        self.assertEqual(clean["when"], "")

    def test_bool_and_passthrough(self):
        clean, errors = forms.FormDialog.validate(self.FIELDS,
                                                  {"symbol": "A", "qty": "100",
                                                   "dry_run": "0", "hidden": "keep-me"})
        self.assertEqual(errors, {})
        self.assertIs(clean["dry_run"], False)
        self.assertEqual(clean["hidden"], "keep-me")

    def test_extra_json_field_not_required_and_empty_values(self):
        clean, errors = forms.FormDialog.validate([{"name": "a"}, {"name": "b", "type": "dict"}],
                                                  {"a": "  "})
        self.assertEqual(errors, {})
        self.assertEqual(clean["a"], "")
        self.assertEqual(clean["b"], {})

    def test_field_from_dict_and_aliases(self):
        field = forms.Field.from_dict({"name": "x", "label": "X", "type": "integer",
                                       "min": 1, "unknown_key": "ignored"})
        self.assertEqual(field.type, "int")
        self.assertEqual(field.label, "X")
        self.assertEqual(field.min, 1)
        clean, errors = forms.FormDialog.validate([field], {"x": "3"})
        self.assertEqual(errors, {})
        self.assertEqual(clean["x"], 3)

        field = forms.Field("y")
        self.assertEqual(field.label, "y")            # label 缺省用 name
        self.assertEqual(forms.normalize_type("Select"), "choice")
        self.assertEqual(forms.normalize_type(None), "text")

    def test_unknown_type_reports_error(self):
        clean, errors = forms.FormDialog.validate([{"name": "z", "type": "magic"}], {"z": "1"})
        self.assertIn("不支持", errors["z"])
        self.assertNotIn("z", clean)

    def test_validate_is_pure(self):
        values = self._valid_values()
        snapshot = dict(values)
        forms.FormDialog.validate(self.FIELDS, values)
        self.assertEqual(values, snapshot)            # 不改入参


# --------------------------------------------------------------------------- GUI
@unittest.skipIf(tk is None, "当前解释器没有 tkinter：%s" % (TK_IMPORT_ERROR,))
class GuiSmokeTests(unittest.TestCase):
    """GUI 烟雾测试：真的建窗口，但不进事件循环（FormDialog 不 wait_window）。"""

    root = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except Exception as exc:  # noqa: BLE001 - 无显示器/无 Tk 构建
            raise unittest.SkipTest("Tk 初始化失败（无显示器？）：%s" % exc)
        cls.root.geometry("1100x780+30+30")
        cls.root.withdraw()                     # 不真的弹窗：无显示器时映射窗口会卡住
        theme.init(cls.root)
        try:
            cls.root.update()
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:  # noqa: BLE001
                pass
            cls.root = None

    def setUp(self):
        self.frame = ttk.Frame(self.root, style="TFrame")
        self.frame.pack(fill="both", expand=True)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.update()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- DataTable
    COLUMNS = [
        {"key": "code", "label": "代码", "width": 90, "kind": "code"},
        {"key": "name", "label": "名称", "width": 100},
        {"key": "price", "label": "现价", "width": 80, "kind": "num", "digits": 2},
        {"key": "change_pct", "label": "涨跌幅", "width": 80, "kind": "pct"},
        {"key": "qty", "label": "数量", "width": 80, "kind": "int"},
        {"key": "amount", "label": "金额", "width": 110, "kind": "money"},
        {"key": "status", "label": "状态", "width": 80, "kind": "badge",
         "badge_colors": {"运行中": "down", "已停止": "up"}},
    ]

    ROWS = [
        {"code": "600519.SH", "name": "贵州茅台", "price": 1688.5, "change_pct": 1.23,
         "qty": 1200, "amount": 2026200.0, "status": "运行中"},
        {"code": "000001.SZ", "name": "平安银行", "price": 11.24, "change_pct": -0.88,
         "qty": 20000, "amount": 224800.0, "status": "已停止"},
        {"code": "300750.SZ", "name": "宁德时代", "price": 205.0, "change_pct": 0.0,
         "qty": 300, "amount": 61500.0, "status": "运行中"},
    ]

    def _make_table(self, **kwargs):
        options = dict(on_select=None, on_double_click=None)
        options.update(kwargs)
        table_widget = table.DataTable(self.frame, self.COLUMNS, height=6, **options)
        table_widget.pack(fill="both", expand=True)
        return table_widget

    def test_table_rows_and_empty_state(self):
        table_widget = self._make_table()
        self.assertTrue(table_widget.empty_visible())
        self.assertEqual(table_widget.empty_label.cget("text"), "暂无数据")
        self.assertNotEqual(dict(table_widget.empty_label.place_info()), {})

        table_widget.set_rows(self.ROWS)
        self.assertEqual(len(table_widget.tree.get_children()), 3)
        self.assertFalse(table_widget.empty_visible())
        self.assertEqual(dict(table_widget.empty_label.place_info()), {})
        self.assertEqual(len(table_widget.rows), 3)

        table_widget.set_rows([])
        self.assertEqual(len(table_widget.tree.get_children()), 0)
        self.assertTrue(table_widget.empty_visible())

        table_widget.clear()
        self.assertTrue(table_widget.empty_visible())

    def test_table_cell_formatting(self):
        table_widget = self._make_table()
        self.assertEqual(table_widget.format_cell("price", 1234.5), "1,234.50")
        self.assertEqual(table_widget.format_cell("change_pct", 1.234), "+1.23%")
        self.assertEqual(table_widget.format_cell("change_pct", -0.8), "-0.80%")
        self.assertEqual(table_widget.format_cell("qty", 1234567), "1,234,567")
        self.assertEqual(table_widget.format_cell("amount", 999.999), "1,000.00")
        self.assertEqual(table_widget.format_cell("price", None), "—")
        self.assertEqual(table_widget.format_cell("code", "600519.SH"), "600519.SH")

        table_widget.set_rows(self.ROWS)
        first = table_widget.tree.item(table_widget.tree.get_children()[0], "values")
        self.assertEqual(list(first)[:5], ["600519.SH", "贵州茅台", "1,688.50", "+1.23%", "1,200"])

    def test_table_row_color_tags(self):
        table_widget = self._make_table()
        table_widget.set_rows(self.ROWS)
        iids = table_widget.tree.get_children()
        up_tags = _tags_of(table_widget.tree, iids[0])          # 茅台：价格/涨跌幅都为正
        mixed_tags = _tags_of(table_widget.tree, iids[1])       # 平安：价格为正、涨跌幅为负
        flat_tags = _tags_of(table_widget.tree, iids[2])        # 宁德：涨跌幅为 0
        self.assertIn("up:price", up_tags)
        self.assertIn("up:change_pct", up_tags)
        self.assertIn("up:qty", up_tags)                        # int 列也按数值着色
        self.assertIn("up:amount", up_tags)                     # money 列同上
        self.assertIn("up:price", mixed_tags)
        self.assertIn("down:change_pct", mixed_tags)            # 同一行不同列不同色（各挂一个 tag）
        self.assertIn("flat:change_pct", flat_tags)
        self.assertIn("badge:status:0", up_tags)               # 运行中 → 映射到 down 色
        self.assertIn("mono:code", up_tags)                    # code 列等宽字体 tag
        self.assertEqual(table_widget.tree.tag_configure("up:price")["foreground"],
                         theme.COLORS["up"])
        self.assertEqual(table_widget.tree.tag_configure("down:change_pct")["foreground"],
                         theme.COLORS["down"])
        self.assertEqual(table_widget.tree.tag_configure("badge:status:0")["foreground"],
                         theme.COLORS["down"])
        # striped：第 2 行（1-based 偶数行）有略深背景 tag
        self.assertIn(table.TAG_STRIPE, mixed_tags)
        self.assertIn("row:even", mixed_tags)
        self.assertNotIn(table.TAG_STRIPE, up_tags)

    def test_table_color_rules_override(self):
        table_widget = self._make_table()
        rules = [{"column": "change_pct", "when": lambda value, row: row.get("code", "").startswith("600"),
                  "color": theme.COLORS["warn"]}]
        table_widget.set_rows(self.ROWS, color_rules=rules)
        iids = table_widget.tree.get_children()
        tags = _tags_of(table_widget.tree, iids[0])
        self.assertIn("rule:change_pct:0", tags)
        self.assertNotIn("up:change_pct", tags)
        self.assertEqual(table_widget.tree.tag_configure("rule:change_pct:0")["foreground"],
                         theme.COLORS["warn"])
        self.assertIn("down:change_pct", _tags_of(table_widget.tree, iids[1]))

    def test_table_sorting_by_header_click(self):
        table_widget = self._make_table(striped=False)
        table_widget.set_rows(self.ROWS)
        self.root.update()
        tree = table_widget.tree
        self.assertIn("<Button-1>", tree.bind())           # 表头点击已绑定

        # 真实鼠标点击依赖窗口已映射（无显示器时拿不到坐标），
        # 这里喂等价事件走 DataTable._on_click 的同一条逻辑：
        # identify_region → identify_column → toggle_sort
        def click_price_header():
            real_region, real_column = tree.identify_region, tree.identify_column
            try:
                tree.identify_region = lambda _x, _y: "heading"
                tree.identify_column = lambda _x: "#3"      # 第 3 列 = price
                table_widget._on_click(_Event(10, 5))
            finally:
                tree.identify_region, tree.identify_column = real_region, real_column

        click_price_header()
        self.assertEqual(table_widget.sort_state(), ("price", False))
        self.assertEqual(self._first_price(table_widget), 11.24)
        self.assertIn("▲", table_widget.tree.heading("price", "text"))

        click_price_header()                               # 同一列 → 切降序
        self.assertEqual(table_widget.sort_state(), ("price", True))
        self.assertEqual(self._first_price(table_widget), 1688.5)
        self.assertIn("▼", table_widget.tree.heading("price", "text"))

        # 换列：默认升序，且旧列不再显示箭头
        table_widget.sort_by("name", desc=False)
        self.assertEqual(table_widget.rows[0]["name"], "宁德时代")
        self.assertEqual(table_widget.sort_state()[0], "name")
        self.assertNotIn("▲", table_widget.tree.heading("price", "text"))
        self.assertIn("▲", table_widget.tree.heading("name", "text"))

    def _first_price(self, table_widget):
        return table_widget.rows[0]["price"]

    def test_table_missing_values_sort_last(self):
        table_widget = self._make_table()
        rows = [{"code": "A", "price": 5.0}, {"code": "B", "price": None},
                {"code": "C", "price": 1.0}, {"code": "D"}]
        table_widget.set_rows(rows)
        table_widget.sort_by("price", desc=False)
        self.assertEqual([row["code"] for row in table_widget.rows], ["C", "A", "B", "D"])
        table_widget.sort_by("price", desc=True)
        self.assertEqual([row["code"] for row in table_widget.rows], ["A", "C", "B", "D"])

    def test_table_selection_callbacks_and_preserve(self):
        selected = []
        doubled = []
        table_widget = self._make_table(
            on_select=lambda row, index: selected.append((row, index)),
            on_double_click=lambda row, index: doubled.append((row, index)))
        table_widget.set_rows(self.ROWS, key_field="code")

        self.assertTrue(table_widget.select_index(1))
        self.assertEqual(table_widget.selected_index(), 1)
        self.assertIs(table_widget.selected(), self.ROWS[1])
        self.assertEqual(selected[-1][1], 1)
        self.assertIs(selected[-1][0], self.ROWS[1])
        self.assertIn("row:selected", _tags_of(table_widget.tree, table_widget.tree.selection()[0]))

        # 刷新 + 重排：按 key_field 保持选中
        reordered = [self.ROWS[1], self.ROWS[0], self.ROWS[2]]
        table_widget.set_rows(reordered, key_field="code", preserve_selection=True)
        self.assertEqual(table_widget.selected_index(), 0)
        self.assertIs(table_widget.selected(), reordered[0])

        # 选中的行消失 → 不再选中
        table_widget.set_rows([self.ROWS[0], self.ROWS[2]], key_field="code")
        self.assertIsNone(table_widget.selected())
        self.assertIsNone(table_widget.selected_index())

        # 双击回调（同样喂等价事件，不依赖窗口映射）
        tree = table_widget.tree
        real_identify_row, real_region = tree.identify_row, tree.identify_region
        try:
            tree.identify_region = lambda _x, _y: "cell"
            tree.identify_row = lambda _y: tree.get_children()[0]
            table_widget._on_double_click(_Event(10, 30))
        finally:
            tree.identify_row, tree.identify_region = real_identify_row, real_region
        self.assertEqual(len(doubled), 1)
        self.assertEqual(doubled[-1][1], 0)
        self.assertEqual(len(table_widget.tree.get_children()), 2)

    def test_table_visible_columns(self):
        table_widget = self._make_table()
        table_widget.set_rows(self.ROWS)
        table_widget.set_visible_columns(["code", "price"])
        self.assertEqual(table_widget.visible_columns(), ["code", "price"])
        self.assertIn("code", str(table_widget.tree["displaycolumns"]))
        self.assertNotIn("name", str(table_widget.tree["displaycolumns"]))
        table_widget.set_visible_columns(None)
        self.assertEqual(table_widget.visible_columns(),
                         [column["key"] for column in self.COLUMNS])
        self.assertEqual(len(table_widget.tree.get_children()), 3)

    def test_table_callback_exception_does_not_propagate(self):
        def boom(_row, _index):
            raise RuntimeError("回调炸了")
        table_widget = self._make_table(on_select=boom)
        table_widget.set_rows(self.ROWS, key_field="code")
        self.assertTrue(table_widget.select_index(0))       # 不应抛出

    # -------------------------------------------------------------- 卡片
    def test_stat_card(self):
        card = cards.StatCard(self.frame, "总资产", 1234567.891, unit="元", sub="较昨日 +1.2%")
        card.pack(anchor="w")
        self.assertEqual(card.title_label.cget("text"), "总资产")
        self.assertEqual(card.value_label.cget("text"), "1,234,567.89")
        self.assertEqual(card.unit_label.cget("text"), "元")
        self.assertEqual(card.sub_label.cget("text"), "较昨日 +1.2%")

        card.set(-2.5, color_by=-2.5, sub="较昨日 -2.5")
        self.assertEqual(card.value_label.cget("text"), "-2.50")
        self.assertEqual(card.sub_label.cget("text"), "较昨日 -2.5")
        self.assertEqual(_style_of(card.value_label), "StatDown.TLabel")

        card.set(3.5, color_by=3.5)
        self.assertEqual(_style_of(card.value_label), "StatUp.TLabel")

        card.set(0, color_by=0)
        self.assertEqual(_style_of(card.value_label), "StatFlat.TLabel")
        up_card = cards.StatCard(self.frame, "今日盈亏", color_key="up", width=180)
        self.assertEqual(up_card.value_label.cget("text"), "—")
        self.assertEqual(_style_of(up_card.value_label), "StatUp.TLabel")
        up_card.set("1.20 万")
        self.assertEqual(up_card.value_label.cget("text"), "1.20 万")

    def test_card_grid(self):
        grid = cards.CardGrid(self.frame, columns=3, gap=8)
        grid.pack(fill="x")
        for index in range(4):
            grid.add(ttk.Label(grid, text="卡片 %d" % index))
        self.assertEqual(grid.count(), 4)
        self.assertEqual(int(grid.grid_columnconfigure(0)["weight"]), 1)
        self.assertEqual(int(grid.grid_columnconfigure(2)["weight"]), 1)
        self.assertEqual(int(grid.widgets[3].grid_info()["row"]), 1)
        self.assertEqual(int(grid.widgets[3].grid_info()["column"]), 0)

        fixed = ttk.Label(grid, text="指定列")
        grid.add(fixed, row=0, col=5)
        self.assertEqual(int(fixed.grid_info()["column"]), 5)

        grid.clear()
        self.assertEqual(grid.count(), 0)
        self.assertEqual(grid.winfo_children(), [])

    def test_section_title(self):
        calls = []
        section = cards.SectionTitle(self.frame, "自选股", hint="8 只",
                                     action=("刷新", lambda: calls.append(1)))
        section.pack(fill="x")
        self.assertEqual(section.title_label.cget("text"), "自选股")
        self.assertEqual(section.hint_label.cget("text"), "8 只")
        self.assertIsNotNone(section.action_button)
        self.assertEqual(section.action_button.cget("text"), "刷新")
        section.action_button.invoke()
        self.assertEqual(calls, [1])
        section.set_text("持仓")
        self.assertEqual(section.title_label.cget("text"), "持仓")

    def test_badge(self):
        badge = cards.Badge(self.frame, "运行中", kind="up")
        badge.pack(anchor="w")
        self.assertEqual(badge.cget("text"), "运行中")
        self.assertEqual(badge.cget("fg"), theme.COLORS["up"])
        badge.set("已停止", kind="down")
        self.assertEqual(badge.cget("text"), "已停止")
        self.assertEqual(badge.cget("fg"), theme.COLORS["down"])
        self.assertEqual(badge.kind, "down")
        badge.set("本地策略", kind="local")
        self.assertEqual(badge.cget("fg"), theme.COLORS["cyan"])
        badge.set("自定义", kind="#123456")
        self.assertEqual(badge.cget("fg"), "#123456")
        self.assertEqual(cards.Badge(self.frame, "x").cget("fg"), theme.COLORS["flat"])

    def test_divider(self):
        divider = cards.Divider(self.frame)
        divider.pack(fill="x")
        self.assertEqual(str(divider.cget("bg")), theme.COLORS["line"])
        self.assertEqual(int(divider.cget("height")), 1)

    def test_key_value_table(self):
        info = cards.KeyValueTable(self.frame, [("代码", "600519.SH"), ("名称", "贵州茅台"),
                                                ("持仓", 1200), ("盈亏", 1234.5)])
        info.pack(fill="x")
        self.assertEqual(info.count(), 4)
        self.assertEqual(info.value_labels["代码"].cget("text"), "600519.SH")
        self.assertEqual(info.value_labels["持仓"].cget("text"), "1,200")
        self.assertEqual(info.value_labels["盈亏"].cget("text"), "1,234.50")
        self.assertEqual(info.get("名称"), "贵州茅台")
        info.set_value("持仓", 1300)
        self.assertEqual(info.value_labels["持仓"].cget("text"), "1,300")
        info.set_items([("板块", None)])
        self.assertEqual(info.value_labels["板块"].cget("text"), "—")
        self.assertEqual(info.count(), 1)

    # -------------------------------------------------------------- 表单弹窗
    FORM_FIELDS = [
        forms.Field("symbol", "证券代码", required=True, help="例如 600519.SH"),
        forms.Field("qty", "数量", type="int", required=True, min=100, max=100000),
        forms.Field("price", "价格", type="float", min=0.01, default=10.5),
        forms.Field("side", "方向", type="choice", choices=["买入", "卖出"], default="买入"),
        forms.Field("dry_run", "仅预演", type="bool", default=True),
        forms.Field("extra", "扩展参数", type="json", default='{"stop_loss": -5}'),
        forms.Field("when", "生效日期", type="date", default="2024-01-02"),
        forms.Field("secret", "密钥", type="password"),
        forms.Field("frozen", "只读字段", default="固定值", readonly=True),
    ]

    def _make_dialog(self, **kwargs):
        options = {"values": {"symbol": "600519.SH"}}
        options.update(kwargs)
        dialog = forms.FormDialog(self.root, "下单", self.FORM_FIELDS, **options)
        self.dialogs.append(dialog)
        dialog.update_idletasks()
        return dialog

    def test_form_dialog_widgets(self):
        dialog = self._make_dialog(hint="按 Enter 提交，Esc 取消")
        self.assertEqual(len(dialog.inputs), len(self.FORM_FIELDS))
        self.assertEqual(len(dialog.inputs), len(dialog.entries))
        self.assertIsInstance(dialog.inputs["symbol"], ttk.Entry)
        self.assertIsInstance(dialog.inputs["side"], ttk.Combobox)
        self.assertEqual(str(dialog.inputs["side"].cget("state")), "readonly")
        self.assertIsInstance(dialog.inputs["dry_run"], ttk.Checkbutton)
        self.assertIsInstance(dialog.inputs["extra"], ttk.Frame)
        self.assertIsInstance(dialog.json_texts["extra"], tk.Text)
        self.assertEqual(str(dialog.inputs["secret"].cget("show")), "*")
        self.assertEqual(str(dialog.inputs["frozen"].cget("state")), "readonly")
        self.assertIsInstance(dialog.error_label, ttk.Label)
        self.assertEqual(dialog.submit_button.cget("text"), "保存")
        self.assertEqual(dialog.cancel_button.cget("text"), "取消")
        self.assertEqual(dialog.hint_label.cget("text"), "按 Enter 提交，Esc 取消")

        self.assertEqual(dialog.vars["symbol"].get(), "600519.SH")
        self.assertEqual(dialog.vars["side"].get(), "买入")
        self.assertTrue(dialog.vars["dry_run"].get())
        self.assertEqual(dialog.vars["frozen"].get(), "固定值")
        self.assertIn("stop_loss", dialog.json_texts["extra"].get("1.0", "end-1c"))
        self.assertIn("✓", dialog.json_hints["extra"].cget("text"))
        self.assertEqual(dialog.collect()["qty"], "")

        # 校验错误显示在弹窗内
        dialog.set_values({"qty": "50"})
        self.assertFalse(dialog.submit())
        self.assertTrue(dialog.winfo_exists())
        self.assertIn("数量", dialog.error_label.cget("text"))
        self.assertIn("不能小于 100", dialog.error_label.cget("text"))

        # 修正后提交成功 → 关闭并拿到 values
        seen = []
        dialog.on_submit = lambda values: seen.append(values)
        dialog.set_values({"qty": "300", "price": "1688.5", "dry_run": False})
        self.assertTrue(dialog.submit())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["symbol"], "600519.SH")
        self.assertEqual(seen[0]["qty"], 300)
        self.assertIs(seen[0]["dry_run"], False)
        self.assertEqual(seen[0]["extra"], {"stop_loss": -5})
        self.assertEqual(dialog.result["qty"], 300)
        self.assertEqual(int(dialog.winfo_exists()), 0)

    def test_form_dialog_on_submit_failure_keeps_open(self):
        dialog = self._make_dialog(on_submit=lambda _values: "资金不足")
        dialog.set_values({"qty": "300", "secret": "x", "when": "2024-01-02"})
        self.assertFalse(dialog.submit())
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.error_label.cget("text"), "资金不足")
        self.assertIsNone(dialog.result)

        def boom(_values):
            raise RuntimeError("服务不可用")
        dialog.on_submit = boom
        self.assertFalse(dialog.submit())
        self.assertTrue(dialog.winfo_exists())
        self.assertIn("服务不可用", dialog.error_label.cget("text"))

        dialog.on_submit = None                              # 无提交回调 → 直接成功
        self.assertTrue(dialog.submit())
        self.assertEqual(int(dialog.winfo_exists()), 0)
        self.assertEqual(dialog.result["qty"], 300)

    def test_form_dialog_json_live_hint(self):
        dialog = self._make_dialog()
        dialog.update_idletasks()
        self.assertIn("✓", dialog.json_hints["extra"].cget("text"))
        dialog.set_values({"extra": "{broken"})
        self.assertIn("✗", dialog.json_hints["extra"].cget("text"))
        self.assertFalse(dialog.submit())
        self.assertIn("JSON", dialog.error_label.cget("text"))
        dialog.set_values({"extra": '{"ok": true}'})
        self.assertIn("✓", dialog.json_hints["extra"].cget("text"))
        self.assertEqual(dialog.collect()["extra"], '{"ok": true}')

    def test_form_dialog_cancel(self):
        dialog = self._make_dialog()
        dialog.cancel()
        self.assertIsNone(dialog.result)
        self.assertEqual(int(dialog.winfo_exists()), 0)

    def test_form_dialog_unknown_field_type_raises(self):
        with self.assertRaises(ValueError):
            forms.FormDialog(self.root, "坏的", [{"name": "x", "type": "nope"}])

    def test_widgets_avoid_tk86_only_features(self):
        """静态检查：控件源码不使用 Tk 8.6 专有控件（ttk.Spinbox 等）。"""
        for module in (table, cards, forms):
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("ttk.Spinbox(", source, "%s 不应使用 ttk.Spinbox" % module.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
