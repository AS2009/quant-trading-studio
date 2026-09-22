/*
 * ui.js — 通用 UI 基建：格式化、轻提示、弹窗、确认框、表格与状态占位、数据源状态条
 * 职责：
 *   1. fmt     数字/涨跌/转义格式化（全站唯一实现，插入 innerHTML 的文本必须走 fmt.esc）
 *   2. Toast   右下角轻提示（info / ok / err / warn）
 *   3. Dialog  通用弹窗（焦点管理 / Esc / 背景 inert / 校验 / 提交忙碌态）
 *   4. 表格与占位：UI.tbody / UI.loading / UI.setError / UI.emptyBlock / UI.kvGrid
 *   5. SourceBar 顶部数据源状态条（真实 / 缓存 / 离线三态 + 可展开详情）
 * 约定：所有后端文本经 fmt.esc；所有异步按钮用 UI.busy 进入禁用态。
 */
"use strict";

/* ============================ 格式化 ============================ */
const fmt = {
  /* 涨跌色：红涨绿跌，与 style.css 的 .up/.down/.flat 对应 */
  color(v) {
    const n = Number(v);
    if (!isFinite(n) || n === 0) return "flat";
    return n > 0 ? "up" : "down";
  },
  sign(v) {
    return Number(v) > 0 ? "+" : "";
  },
  /* 千分位数字：digits 默认 2 */
  num(v, digits) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    const d = digits === undefined ? 2 : digits;
    return n.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  /* 整数千分位 */
  int(v) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    return Math.round(n).toLocaleString("zh-CN");
  },
  money(v) { return fmt.num(v, 2); },
  /* 百分比：withSign 为 true 时正数带 + */
  pct(v, withSign, digits) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    const d = digits === undefined ? 2 : digits;
    return (withSign && n > 0 ? "+" : "") + n.toFixed(d) + "%";
  },
  /* 带符号金额（用于盈亏） */
  signed(v, digits) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    return fmt.sign(n) + fmt.num(n, digits === undefined ? 2 : digits);
  },
  /* 大额自动换算：>= 1 亿显示 x.xx 亿，>= 1 万显示 x.xx 万 */
  compact(v) {
    const n = Number(v);
    if (!isFinite(n)) return "—";
    const abs = Math.abs(n);
    if (abs >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
    if (abs >= 1e4) return (n / 1e4).toFixed(2) + " 万";
    return fmt.num(n, 2);
  },
  /* 固定小数位，非法值给占位符 */
  fixed(v, digits, dash) {
    const n = Number(v);
    if (!isFinite(n)) return dash || "—";
    return n.toFixed(digits === undefined ? 2 : digits);
  },
  /* null / undefined / 空串 → 占位符 */
  or(v, dash) {
    if (v === null || v === undefined || v === "") return dash === undefined ? "—" : dash;
    return v;
  },
  bool(v) { return v ? "是" : "否"; },
  /* "YYYY-MM-DD HH:MM:SS" → "HH:MM:SS" */
  timeOf(ts) {
    const s = String(ts || "");
    const m = s.match(/(\d{2}:\d{2}(?::\d{2})?)/);
    return m ? m[1] : "";
  },
  dateOf(ts) { return String(ts || "").slice(0, 10); },
  /* 距现在多少分钟（无法解析返回 null） */
  minutesSince(ts) {
    const t = fmt.parseTime(ts);
    if (t === null) return null;
    return Math.max(0, Math.round((Date.now() - t) / 60000));
  },
  parseTime(ts) {
    const s = String(ts || "").trim();
    if (!s) return null;
    const iso = s.replace(" ", "T");
    const t = Date.parse(iso);
    return isNaN(t) ? null : t;
  },
  clock(d) {
    const dt = d instanceof Date ? d : new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return pad(dt.getHours()) + ":" + pad(dt.getMinutes()) + ":" + pad(dt.getSeconds());
  },
  /* 展示用日期时间（本地） */
  stamp(d) {
    const dt = d instanceof Date ? d : new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return dt.getFullYear() + "-" + pad(dt.getMonth() + 1) + "-" + pad(dt.getDate()) + " " +
      pad(dt.getHours()) + ":" + pad(dt.getMinutes()) + ":" + pad(dt.getSeconds());
  },
  /* 拼接进 innerHTML 的后端/用户文本一律转义 */
  /* 策略标的池类型：把后端枚举值译成中文，避免界面出现 (single)/(multi) */
  universeLabel(v) {
    const map = { single: "单标的", multi: "多标的", index: "指数", etf: "ETF" };
    return map[v] || (v ? String(v) : "—");
  },
  esc(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  },
};

/* ============================ 轻提示 ============================ */
const Toast = {
  MAX: 4,
  show(message, type) {
    const wrap = document.getElementById("toast-wrap");
    if (!wrap) return;
    while (wrap.children.length >= this.MAX) wrap.removeChild(wrap.firstChild);
    const el = document.createElement("div");
    el.className = "toast toast-" + (type || "info");
    el.setAttribute("role", type === "err" ? "alert" : "status");
    el.textContent = String(message);
    el.title = "点击关闭";
    el.addEventListener("click", () => el.remove());
    wrap.appendChild(el);
    const life = type === "err" ? 6000 : 3600;
    setTimeout(() => {
      el.classList.add("toast-out");
      setTimeout(() => el.remove(), 320);
    }, life);
  },
  ok(m) { this.show(m, "ok"); },
  err(m) { this.show(m, "err"); },
  warn(m) { this.show(m, "warn"); },
  info(m) { this.show(m, "info"); },
};

/* ============================ 小工具 ============================ */
const UI = {
  /* 标的代码基础校验与规范化提示：接受 600519 / sh600519 / 600519.SH / 600519.sh，
     最终规范化仍由后端负责（前端只拦明显错误，减少无谓请求） */
  normalizeSymbol(raw) {
    const s = String(raw === undefined || raw === null ? "" : raw).trim();
    if (!s) return { ok: false, message: "请输入标的代码" };
    const m = s.match(/^(?:(sh|sz|bj)\.?)?(\d{6})(?:\.?(sh|sz|bj))?$/i);
    if (!m) return { ok: false, message: "代码格式不正确，示例：600519、sh600519、600519.SH" };
    const market = (m[3] || m[1] || "").toUpperCase();
    return { ok: true, code: market ? m[2] + "." + market : m[2], digit: m[2] };
  },
  fmt: fmt,

  /* 后端错误的用户可读文案（区分「接口未实现」与「数据错误」） */
  apiErrorText(e) {
    if (!e) return "未知错误";
    if (e instanceof ApiError) {
      if (e.unimplemented) return "接口未实现（HTTP " + e.status + "）：" + (e.message || e.path);
      if (e.status === 0) return e.message;
      if (e.status >= 500) return "后端内部错误（HTTP " + e.status + "）：" + e.message;
      return e.message + (e.field ? "（字段：" + e.field + "）" : "");
    }
    return e.message || String(e);
  },

  /* 表格 tbody 渲染：rows 为 <tr> HTML 字符串数组；空数据给「暂无数据」占位 */
  tbody(tbodyEl, rows, opts) {
    if (!tbodyEl) return;
    const o = opts || {};
    const list = (rows || []).filter(Boolean);
    if (!list.length) {
      tbodyEl.innerHTML = `<tr class="empty-row"><td colspan="${o.colspan || 1}">
        <div class="empty-inline">
          <div class="empty-title">${fmt.esc(o.empty || "暂无数据")}</div>
          ${o.emptyHint ? `<div class="empty-hint">${fmt.esc(o.emptyHint)}</div>` : ""}
        </div></td></tr>`;
      return;
    }
    tbodyEl.innerHTML = list.join("");
    if (typeof o.after === "function") o.after();
  },

  /* 区域加载态 */
  loading(el, text) {
    if (!el) return;
    el.innerHTML = `<div class="state-block" role="status">
      <span class="spinner" aria-hidden="true"></span>
      <span>${fmt.esc(text || "加载中…")}</span></div>`;
  },

  /* 区域错误态（含可选重试按钮） */
  setError(el, opts) {
    if (!el) return;
    const o = typeof opts === "string" ? { message: opts } : (opts || {});
    el.innerHTML = `<div class="state-block state-error" role="alert">
      <div class="state-title">${fmt.esc(o.title || "数据加载失败")}</div>
      <div class="state-msg">${fmt.esc(o.message || "未知错误")}</div>
      ${o.hint ? `<div class="state-hint">${fmt.esc(o.hint)}</div>` : ""}
      ${o.retry ? `<button type="button" class="btn btn-sm" data-retry>重试</button>` : ""}
    </div>`;
    const btn = el.querySelector("[data-retry]");
    if (btn && typeof o.retry === "function") btn.addEventListener("click", () => o.retry());
  },

  /* 表格内的加载 / 错误状态：必须写在 tbody 里，
     否则替换 .table-wrap 的 innerHTML 会把 <table> 本身删掉（后续查询全部拿到 null）。 */
  tbodyLoading(tbodyEl, colspan, text) {
    if (!tbodyEl) return;
    tbodyEl.innerHTML = `<tr class="state-row"><td colspan="${colspan || 1}">
      <div class="state-inline" role="status"><span class="spinner" aria-hidden="true"></span>
      <span>${fmt.esc(text || "加载中…")}</span></div></td></tr>`;
  },

  tbodyError(tbodyEl, opts) {
    if (!tbodyEl) return;
    const o = opts || {};
    tbodyEl.innerHTML = `<tr class="state-row"><td colspan="${o.colspan || 1}">
      <div class="state-inline state-error" role="alert">
        <div class="state-title">${fmt.esc(o.title || "数据加载失败")}</div>
        <div class="state-msg">${fmt.esc(o.message || "未知错误")}</div>
        ${o.hint ? `<div class="state-hint">${fmt.esc(o.hint)}</div>` : ""}
        ${o.retry ? '<button type="button" class="btn btn-sm" data-retry>重试</button>' : ""}
      </div></td></tr>`;
    const btn = tbodyEl.querySelector("[data-retry]");
    if (btn && typeof o.retry === "function") btn.addEventListener("click", () => o.retry());
  },

  /* 空状态块（非表格场景） */
  emptyBlock(opts) {
    const o = typeof opts === "string" ? { title: opts } : (opts || {});
    return `<div class="state-block">
      <div class="state-title">${fmt.esc(o.title || "暂无数据")}</div>
      ${o.hint ? `<div class="state-hint">${fmt.esc(o.hint)}</div>` : ""}
      ${o.actions || ""}
    </div>`;
  },

  /* 内联提示条（如 warnings / 降级说明） */
  notice(opts) {
    const o = opts || {};
    const items = Array.isArray(o.items) ? o.items : null;
    const body = items
      ? `<ul class="notice-list">${items.map((t) => "<li>" + fmt.esc(t) + "</li>").join("")}</ul>`
      : `<div class="notice-text">${o.html ? o.html : fmt.esc(o.text || "")}</div>`;
    return `<div class="notice notice-${o.level || "info"}" role="${o.level === "err" ? "alert" : "status"}">
      ${o.title ? `<div class="notice-title">${fmt.esc(o.title)}</div>` : ""}${body}</div>`;
  },

  /* 表单控件生成（统一 .field 样式，label 关联 id 保证点标签可聚焦） */
  field(spec) {
    const s = spec || {};
    const id = s.id || ("f-" + Math.random().toString(36).slice(2, 9));
    const label = `<label class="field-label" for="${fmt.esc(id)}">${fmt.esc(s.label || "")}${
      s.required ? ' <em>*</em>' : ""}</label>`;
    let control = "";
    if (s.type === "select") {
      const opts = (s.options || []).map((o) => {
        const value = typeof o === "object" ? o.value : o;
        const text = typeof o === "object" ? o.label : o;
        const sel = String(value) === String(s.value === undefined ? "" : s.value) ? " selected" : "";
        return `<option value="${fmt.esc(value)}"${sel}>${fmt.esc(text)}</option>`;
      }).join("");
      control = `<select id="${fmt.esc(id)}" name="${fmt.esc(s.name || id)}"${s.disabled ? " disabled" : ""}>${opts}</select>`;
    } else if (s.type === "textarea") {
      control = `<textarea id="${fmt.esc(id)}" name="${fmt.esc(s.name || id)}" rows="${s.rows || 3}"${
        s.spellcheck === false ? ' spellcheck="false"' : ""}${s.disabled ? " disabled" : ""}>
        placeholder="${fmt.esc(s.placeholder || "")}">${fmt.esc(s.value || "")}</textarea>`;
    } else {
      const attrs = [
        'type="' + (s.type || "text") + '"',
        'id="' + fmt.esc(id) + '"',
        'name="' + fmt.esc(s.name || id) + '"',
        'value="' + fmt.esc(s.value === undefined ? "" : s.value) + '"',
        s.placeholder ? 'placeholder="' + fmt.esc(s.placeholder) + '"' : "",
        s.min !== undefined ? 'min="' + fmt.esc(s.min) + '"' : "",
        s.max !== undefined ? 'max="' + fmt.esc(s.max) + '"' : "",
        s.step !== undefined ? 'step="' + fmt.esc(s.step) + '"' : "",
        s.autocomplete ? 'autocomplete="' + fmt.esc(s.autocomplete) + '"' : "",
        s.disabled ? "disabled" : "",
        s.inputmode ? 'inputmode="' + fmt.esc(s.inputmode) + '"' : "",
      ].filter(Boolean).join(" ");
      control = `<input ${attrs}>`;
    }
    return `<div class="field${s.wide ? " field-wide" : ""}">${label}${control}${
      s.hint ? `<div class="field-hint">${s.hint}</div>` : ""}</div>`;
  },

  /* 按钮忙碌态：禁用 + 文案替换，返回恢复函数 */
  busy(btn, on, busyText) {
    if (!btn) return () => {};
    if (on) {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.disabled = true;
      btn.classList.add("is-busy");
      if (busyText) btn.textContent = busyText;
      return () => UI.busy(btn, false);
    }
    btn.disabled = false;
    btn.classList.remove("is-busy");
    if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
    return () => {};
  },

  /* kv 键值网格（数据源详情、元信息展示） */
  kvGrid(obj, labels) {
    const rows = UI.kvRows(obj, labels);
    if (!rows.length) return '<div class="kv-empty">无可用信息</div>';
    return `<div class="kv-grid">${rows.join("")}</div>`;
  },
  kvRows(obj, labels) {
    if (!obj || typeof obj !== "object") return [];
    const L = labels || {};
    return Object.keys(obj).sort().map((k) => {
      const v = obj[k];
      if (v === undefined) return "";
      let text;
      if (v === null) text = "—";
      else if (typeof v === "boolean") text = v ? "是" : "否";
      else if (typeof v === "number") text = fmt.num(v, Number.isInteger(v) ? 0 : 2);
      else if (Array.isArray(v)) {
        text = v.map((x) => (x && typeof x === "object" ? JSON.stringify(x) : String(x))).join("、") || "—";
      } else if (typeof v === "object") text = JSON.stringify(v);
      else text = String(v) || "—";
      return `<div class="kv"><span class="k">${fmt.esc(L[k] || k)}</span><span class="v">${fmt.esc(text)}</span></div>`;
    }).filter(Boolean);
  },
};

/* ============================ 弹窗 ============================ */
const Dialog = {
  stack: [],
  root: null,

  _root() {
    if (!this.root) {
      this.root = document.getElementById("modal-root");
      if (!this.root) {
        this.root = document.createElement("div");
        this.root.id = "modal-root";
        document.body.appendChild(this.root);
      }
    }
    return this.root;
  },

  closeTop() {
    const top = this.stack[this.stack.length - 1];
    if (top) top.close("escape");
  },

  open(opts) {
    const o = opts || {};
    const root = this._root();
    const mask = document.createElement("div");
    mask.className = "modal-mask open";
    mask.setAttribute("aria-hidden", "false");
    const sizeCls = o.size ? " modal-" + o.size : "";
    mask.innerHTML = `
      <div class="modal${sizeCls}" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <div class="modal-head">
          <h3 id="dlg-title">${fmt.esc(o.title || "")}</h3>
          <button type="button" class="modal-close" aria-label="关闭">×</button>
        </div>
        <form class="modal-body" novalidate>
          ${o.subtitle ? `<div class="modal-sub">${fmt.esc(o.subtitle)}</div>` : ""}
          <div class="dlg-error" role="alert" hidden></div>
          <div class="dlg-content"></div>
          <div class="modal-foot">
            <div class="foot-left"></div>
            <button type="button" class="btn" data-cancel>${fmt.esc(o.cancelText || "取消")}</button>
            <button type="submit" class="btn ${o.danger ? "btn-danger-solid" : "btn-primary"}" data-submit>${
              fmt.esc(o.submitText || "确定")}</button>
          </div>
        </form>
      </div>`;
    root.appendChild(mask);

    const el = mask.querySelector(".modal");
    const form = mask.querySelector("form");
    const content = mask.querySelector(".dlg-content");
    const errorEl = mask.querySelector(".dlg-error");
    const submitBtn = mask.querySelector("[data-submit]");
    const cancelBtn = mask.querySelector("[data-cancel]");
    const footLeft = mask.querySelector(".foot-left");
    const lastFocus = document.activeElement;

    const dialog = {
      el: mask, modal: el, form: form, content: content,
      lastFocus: lastFocus,
      closed: false,
      close(reason) {
        if (dialog.closed) return;
        dialog.closed = true;
        mask.remove();
        const i = Dialog.stack.indexOf(dialog);
        if (i >= 0) Dialog.stack.splice(i, 1);
        Dialog._syncInert();
        if (lastFocus && typeof lastFocus.focus === "function" && document.contains(lastFocus)) {
          lastFocus.focus();
        }
        if (o.onClose) o.onClose(reason || "close", dialog);
      },
      setError(msg) {
        if (msg) {
          errorEl.hidden = false;
          errorEl.textContent = msg;
        } else {
          errorEl.hidden = true;
          errorEl.textContent = "";
        }
      },
      busy(on, text) {
        UI.busy(submitBtn, on, text);
        cancelBtn.disabled = !!on;
        dialog.el.classList.toggle("is-busy", !!on);
      },
      hideFooter() { mask.querySelector(".modal-foot").hidden = true; },
      addFooterButton(text, cls, handler) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn " + (cls || "");
        b.textContent = text;
        b.addEventListener("click", () => handler(dialog));
        footLeft.appendChild(b);
        return b;
      },
      setContent(node) {
        content.innerHTML = "";
        if (typeof node === "string") content.innerHTML = node;
        else if (node) content.appendChild(node);
      },
    };

    /* 外部注入内容 */
    dialog.setContent(o.body);
    Dialog.stack.push(dialog);
    Dialog._syncInert();

    mask.querySelector(".modal-close").addEventListener("click", () => dialog.close("close"));
    cancelBtn.addEventListener("click", () => dialog.close("cancel"));
    mask.addEventListener("mousedown", (e) => {
      if (e.target === mask && !dialog.el.classList.contains("is-busy")) dialog.close("backdrop");
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (submitBtn.disabled) return;
      dialog.setError("");
      if (typeof o.validate === "function") {
        const msg = o.validate(dialog);
        if (msg) {
          dialog.setError(msg);
          const bad = form.querySelector("[aria-invalid='true'], input, select, textarea");
          if (bad) bad.focus();
          return;
        }
      }
      if (typeof o.onSubmit === "function") {
        dialog.busy(true, o.busyText || "处理中…");
        try {
          const keep = await o.onSubmit(dialog);
          if (keep === false) return; /* 由 onSubmit 自行处理错误，保持弹窗打开 */
          dialog.close("submit");
        } catch (err) {
          dialog.setError(UI.apiErrorText(err));
        } finally {
          dialog.busy(false);
        }
      } else {
        dialog.close("submit");
      }
    });
    if (typeof o.validate === "function") {
      /* 输入即清除错误提示，减少「提交才报错」的挫败感 */
      form.addEventListener("input", () => dialog.setError(""));
    }

    /* 键盘：Esc 关闭最上层；Tab 在弹窗内循环 */
    mask.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        dialog.close("escape");
        return;
      }
      if (e.key !== "Tab") return;
      const nodes = Array.from(el.querySelectorAll(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
      )).filter((n) => n.offsetParent !== null);
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });

    if (typeof o.onRender === "function") o.onRender(dialog);
    /* 首个可聚焦控件（弹窗刚插入，用 rAF 等布局完成） */
    requestAnimationFrame(() => {
      const target = content.querySelector("[data-autofocus]") ||
        content.querySelector("input:not([disabled]),select:not([disabled]),textarea:not([disabled])") ||
        submitBtn;
      if (target) target.focus();
    });
    document.dispatchEvent(new CustomEvent("qs:dialog-open"));
    return dialog;
  },

  /* 弹窗打开时禁止背景交互（含键盘焦点），关闭后恢复 */
  _syncInert() {
    const active = this.stack.length > 0;
    [document.querySelector("header.topbar"), document.getElementById("app-main"), document.querySelector(".source-bar")]
      .forEach((el) => { if (el) el.inert = active; });
  },

  confirm(opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      let settled = false;
      const dialog = Dialog.open({
        title: o.title || "请确认",
        size: "sm",
        submitText: o.confirmText || "确定",
        cancelText: o.cancelText || "取消",
        danger: !!o.danger,
        body: `<div class="confirm-body">${o.html || fmt.esc(o.message || "")}</div>`,
        onSubmit() {
          settled = true;
          resolve(true);
          return true;
        },
        onClose() { if (!settled) resolve(false); },
      });
      dialog.close = ((orig) => function (reason) {
        orig.call(dialog, reason);
      })(dialog.close);
    });
  },
};

/* ============================ 数据源状态条 ============================ */
const SourceBar = {
  el: null,
  expanded: false,
  timer: null,

  mount(el, opts) {
    this.el = el;
    if (!this.el) return;
    this.el.className = "source-bar";
    this.el.addEventListener("click", (e) => {
      const toggle = e.target.closest("[data-sb-toggle]");
      if (toggle) {
        this.expanded = !this.expanded;
        this.render();
        /* 展开后把焦点交给详情里的第一个控件，键盘用户可继续操作 */
        if (this.expanded) {
          const first = this.el.querySelector(".source-bar-detail [data-sb-refresh]");
          if (first) first.focus();
        }
        return;
      }
      if (e.target.closest("[data-sb-refresh]") && opts && opts.onRefresh) opts.onRefresh();
    });
    /* 时钟/新鲜度每 30s 重算（缓存滞后分钟数会变化） */
    this.timer = setInterval(() => this.render(), 30000);
    Store.on("status", () => this.render());
    Store.on("level", () => this.render());
    Store.on("backend", () => this.render());
    Store.on("meta", () => this.render());
    this.render();
  },

  /* 文案与配色：三态 + 未知态 */
  describe() {
    const st = Store.state;
    const meta = st.lastMeta || {};
    const status = st.status || {};
    const level = st.level;
    const provider = status.provider || {};
    const sources = Array.isArray(provider.sources) && provider.sources.length
      ? provider.sources.join("/")
      : (meta.source || provider.active || "");
    const asOf = (meta && meta.as_of) || status.as_of || (st.backend.info && st.backend.info.as_of) || "";
    const time = fmt.timeOf(asOf) || fmt.timeOf(fmt.stamp(st.lastResponseAt || new Date()));
    const staleMin = st.lastResponseAt ? fmt.minutesSince(fmt.stamp(st.lastResponseAt)) : null;

    if (level === "real") {
      return {
        level: "real",
        title: "真实行情",
        detail: "来源：" + fmt.or(sources, "未知") + " · " + fmt.or(time, "—") +
          (meta.latency_ms ? " · 延迟 " + meta.latency_ms + "ms" : ""),
        alert: false,
      };
    }
    if (level === "cache") {
      const mins = staleMin === null ? null : staleMin;
      return {
        level: "cache",
        title: "缓存数据",
        detail: (mins === null ? "可能滞后" : "可能滞后 " + mins + " 分钟") +
          " · 来源：" + fmt.or(sources, "缓存") + " · " + fmt.or(time, "—"),
        alert: false,
      };
    }
    if (level === "offline") {
      const reason = Store.offlineReason ? Store.offlineReason() : "";
      const src = fmt.or(meta.source || (status.provider && status.provider.active), "本地示例");
      return {
        level: "offline",
        title: "离线演示数据",
        detail: reason
          ? reason + " · 时点 " + fmt.or(time, "—")
          : "未接入真实行情 · 来源：" + src + " · " + fmt.or(time, "—"),
        /* 只要处于离线/降级状态就必须给出不可交易的强提示 */
        alert: true,
        alertText: "当前为本地/演示数据，不可用于交易决策",
      };
    }
    return {
      level: "unknown",
      title: "数据源状态未知",
      detail: "尚未获取到 /api/system/status 与响应 meta",
      alert: true,
      alertText: "无法确认数据来源，请勿据此交易",
    };
  },

  render() {
    if (!this.el) return;
    const d = this.describe();
    const st = Store.state;
    const status = st.status || {};
    const meta = st.lastMeta || {};
    const mi = Store.marketInfo();
    const trade = status.trade || {};

    const detailRows = [];
    const provider = status.provider || {};
    detailRows.push(["数据源 provider.active", fmt.or(provider.active, "—")]);
    detailRows.push(["可用 sources", (provider.sources || []).join("、") || "—"]);
    detailRows.push(["已接入 available", (provider.available || []).join("、") || "—"]);
    detailRows.push(["最近一次刷新", st.lastResponseAt ? fmt.stamp(st.lastResponseAt) : "—"]);
    detailRows.push(["最近响应路径", st.lastResponsePath || "—"]);
    detailRows.push(["响应 meta 时点", fmt.or((meta && meta.as_of) || status.as_of, "—")]);
    detailRows.push(["延迟 latency_ms", meta.latency_ms === undefined ? "—" : String(meta.latency_ms)]);
    detailRows.push(["meta.source / stale / offline", [fmt.or(meta.source, "—"), fmt.bool(meta.stale), fmt.bool(meta.offline)].join(" / ")]);
    if (meta.notes && meta.notes.length) detailRows.push(["数据源提示", meta.notes.join("；")]);
    const cache = status.cache || {};
    if (Object.keys(cache).length) {
      Object.keys(cache).forEach((k) => {
        const v = cache[k];
        detailRows.push(["缓存 " + k, (typeof v === "object" ? JSON.stringify(v) : String(v))]);
      });
    }
    detailRows.push(["系统状态 mode", fmt.or(status.mode, st.backend.info ? st.backend.info.mode : "—")]);
    detailRows.push(["后端版本 version", fmt.or(status.version, st.backend.info ? st.backend.info.version : "—")]);
    detailRows.push(["计算后端 backend", typeof status.backend === "object" && status.backend
      ? (status.backend.engine || JSON.stringify(status.backend)) : fmt.or(status.backend, "—")]);
    detailRows.push(["数据目录 data_dir", fmt.or(status.data_dir, "—")]);
    detailRows.push(["基准 / 初始资金", fmt.or(status.benchmark, st.benchmark) + " / " + fmt.num(status.initial_cash || st.initialCash, 0)]);
    detailRows.push(["市场", (mi.isClosed ? "休市中" : "交易中") + " · 最近交易日 " + fmt.or(mi.lastTradingDay, "—")]);
    detailRows.push(["交易模式 trade.mode", trade.mode ? trade.mode + "（模拟盘）" : "—"]);
    if (trade.brokers) detailRows.push(["券商适配器", Object.keys(trade.brokers).join("、") || "—"]);
    detailRows.push(["自选池数量", String(fmt.or(status.watchlist_count, (Store.state.watchlist.codes || []).length))]);
    detailRows.push(["离线原因", fmt.or(Store.offlineReason && Store.offlineReason(), "—")]);
    if (st.statusError) detailRows.push(["状态接口错误", UI.apiErrorText(st.statusError)]);
    if (st.backend.error) detailRows.push(["健康检查错误", st.backend.error]);

    this.el.dataset.level = d.level;
    this.el.innerHTML = `
      <div class="source-bar-main">
        <span class="sb-dot" aria-hidden="true"></span>
        <span class="sb-title">${fmt.esc(d.title)}</span>
        <span class="sb-detail">${fmt.esc(d.detail)}</span>
        <button type="button" class="sb-toggle" data-sb-toggle aria-expanded="${this.expanded ? "true" : "false"}"
          aria-controls="source-detail">${this.expanded ? "收起详情" : "详情"}</button>
        <button type="button" class="sb-refresh" data-sb-refresh title="重新获取数据源状态" aria-label="重新获取数据源状态">↻</button>
      </div>
      ${d.alert ? `<div class="source-bar-alert" role="alert">⚠ ${fmt.esc(d.alertText || "")}</div>` : ""}
      <div class="source-bar-detail" id="source-detail" ${this.expanded ? "" : "hidden"}>
        <div class="kv-table">${detailRows.map((r) => `
          <div class="kv-row"><span class="k">${fmt.esc(r[0])}</span><span class="v">${fmt.esc(r[1])}</span></div>`).join("")}
        </div>
        <div class="sb-detail-foot">
          <button type="button" class="btn btn-sm" data-sb-refresh>刷新状态</button>
          <button type="button" class="btn btn-sm" data-sb-toggle>收起</button>
        </div>
      </div>`;
  },
};

window.fmt = fmt;
window.Toast = Toast;
window.UI = UI;
window.Dialog = Dialog;
window.SourceBar = SourceBar;
