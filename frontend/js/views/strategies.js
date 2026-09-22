/*
 * views/strategies.js — 策略管理视图
 * 职责：
 *   1. 策略库卡片（GET /api/strategies）：内置 / 自定义、状态、标的池、频率、参数、min_bars、version
 *   2. 策略详情弹窗（GET /api/strategies/<id>）：完整 spec + param_schema
 *   3. 新建自定义策略（POST /api/strategies）：必须选择模板（template=内置策略 id），
 *      参数表单由模板 param_schema 动态生成，提交时只发送 schema 内声明的键（契约要求的合法子集）
 *   4. 删除自定义策略（DELETE /api/strategies/<id>，内置返回 403 → 表单内提示）
 * 对外接口：StrategiesView.render(container) / StrategiesView.refresh()
 */
"use strict";

const StrategiesView = {
  id: "strategy",
  label: "策略管理",
  container: null,
  state: { filter: "all", loading: false },

  render(container) {
    this.container = container;
    container.innerHTML = `
      <h2 class="view-title">策略管理 <span class="view-sub" id="st-count"></span></h2>
      <div class="panel">
        <div class="panel-title">
          <span class="toolbar">
            <span class="segmented" role="group" aria-label="筛选策略">
              <button type="button" class="seg active" data-filter="all">全部</button>
              <button type="button" class="seg" data-filter="code">代码</button>
              <button type="button" class="seg" data-filter="custom">自定义</button>
            </span>
            <button type="button" class="btn btn-sm" id="st-refresh">刷新</button>
          </span>
          <button type="button" class="btn btn-primary" id="st-new">+ 新建策略</button>
        </div>
        <div id="st-cards" class="card-grid cols-3"></div>
      </div>`;

    container.querySelector("#st-new").addEventListener("click", () => this.openCreate());
    container.querySelector("#st-refresh").addEventListener("click", () => this.refresh());
    container.querySelectorAll("[data-filter]").forEach((b) => {
      b.addEventListener("click", () => {
        this.state.filter = b.dataset.filter;
        container.querySelectorAll("[data-filter]").forEach((x) =>
          x.classList.toggle("active", x === b));
        this.renderCards();
      });
    });
    return this.load();
  },

  refresh() { return this.render(this.container); },

  async load() {
    if (this.state.loading) return;
    this.state.loading = true;
    const cardsEl = this.container.querySelector("#st-cards");
    UI.loading(cardsEl, "策略列表加载中…");
    try {
      const json = await API.strategies();
      Store.setStrategies(json.data || []);
      this.renderCards();
    } catch (e) {
      Store.state.strategiesError = e;
      UI.setError(cardsEl, {
        title: "策略列表加载失败", message: UI.apiErrorText(e), retry: () => this.load(),
      });
      this.container.querySelector("#st-count").textContent = "";
    } finally {
      this.state.loading = false;
    }
  },

  filtered() {
    const list = Store.state.strategies;
    // “代码”＝随仓库发布的内置策略 + strategies/local/ 下的本地代码策略；“自定义”＝界面新建的模板实例
    if (this.state.filter === "code" || this.state.filter === "builtin") {
      return list.filter((s) => (s.origin || (s.builtin === false ? "user" : "builtin")) !== "user");
    }
    if (this.state.filter === "custom") return list.filter((s) => (s.origin || (s.builtin === false ? "user" : "builtin")) === "user");
    return list;
  },

  /* param_schema → 可渲染条目：统一成 {key, label, type, min, max, step, options, default, desc} */
  schemaEntries(schema) {
    if (!schema || typeof schema !== "object") return [];
    return Object.keys(schema).map((key) => {
      const raw = schema[key];
      if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const type = String(raw.type || "").toLowerCase() ||
          (typeof raw.default === "number" ? (Number.isInteger(raw.default) ? "int" : "number")
            : typeof raw.default === "boolean" ? "bool" : "str");
        return {
          key: key,
          label: raw.label || raw.title || key,
          type: type,
          min: raw.min, max: raw.max, step: raw.step,
          options: raw.options || raw.choices || raw.enum || null,
          default: raw.default,
          desc: raw.desc || raw.description || "",
        };
      }
      return {
        key: key,
        label: key,
        type: typeof raw === "boolean" ? "bool" : typeof raw === "number" ? "number" : "str",
        default: raw,
        desc: "",
      };
    });
  },

  renderCards() {
    const cardsEl = this.container.querySelector("#st-cards");
    const list = this.filtered();
    this.container.querySelector("#st-count").textContent =
      "共 " + Store.state.strategies.length + " 个策略（代码 " +
      Store.state.strategies.filter((s) => (s.origin || (s.builtin === false ? "user" : "builtin")) !== "user").length +
      " / 自定义 " +
      Store.state.strategies.filter((s) => (s.origin || (s.builtin === false ? "user" : "builtin")) === "user").length + "）";

    if (!list.length) {
      cardsEl.innerHTML = UI.emptyBlock({
        title: Store.state.strategies.length ? "当前筛选下没有策略" : "暂无策略",
        hint: Store.state.strategies.length ? "切换筛选条件查看其它策略" : "点击右上角「+ 新建策略」基于内置模板创建",
      });
      return;
    }

    const statusBadge = (s) => (s.status === "running"
      ? '<span class="badge badge-running">运行中</span>'
      : '<span class="badge badge-paused">已暂停</span>');

    cardsEl.innerHTML = list.map((s) => {
      const entries = this.schemaEntries(s.param_schema);
      const labelOf = (k) => {
        const hit = entries.find((e) => e.key === k);
        return hit ? hit.label : k;
      };
      const paramText = Object.entries(s.params || {})
        .map(([k, v]) => `<span class="param-chip">${fmt.esc(labelOf(k))}=${fmt.esc(fmt.or(v, "—"))}</span>`)
        .join("") || '<span class="flat">无参数</span>';
      // 来源：builtin（随仓库发布）/ local（strategies/local 下的代码）/ user（界面新建的模板实例）
      const origin = s.origin || (s.builtin === false ? "user" : "builtin");
      const originBadge = origin === "local"
        ? ' <span class="badge badge-local" title="代码位于 strategies/local/，删除方式是移除文件并重启">本地代码</span>'
        : (origin === "user" ? ' <span class="badge badge-custom">自定义</span>' : "");
      const canDelete = origin === "user";
      return `
      <div class="strategy-card">
        <h3>${fmt.esc(s.name)} ${statusBadge(s)}${originBadge}</h3>
        <p class="desc">${fmt.esc(s.desc || "（无描述）")}</p>
        <div class="meta">
          <span class="badge badge-cat">${fmt.esc(s.category || "未分类")}</span>
          标的池：${fmt.esc(s.universe || "—")}
          <span class="flat">（${fmt.esc(fmt.universeLabel(s.universe_type))}）</span>
          · 频率：${fmt.esc(s.freq || "—")}
          · 最少 bar：${fmt.esc(s.min_bars === undefined ? "—" : s.min_bars)}
          ${s.version ? " · v" + fmt.esc(s.version) : ""}
        </div>
        <div class="params">参数：${paramText}</div>
        <div class="cards-foot">
          <span class="btn-row">
            <button type="button" class="btn btn-sm" data-bt="${fmt.esc(s.id)}">查看回测</button>
            <button type="button" class="btn btn-sm" data-detail="${fmt.esc(s.id)}">详情</button>
          </span>
          ${canDelete ? `<button type="button" class="btn btn-sm btn-danger" data-del="${fmt.esc(s.id)}">删除</button>` : ""}
        </div>
      </div>`;
    }).join("");

    cardsEl.querySelectorAll("[data-bt]").forEach((btn) => {
      btn.addEventListener("click", () => {
        Store.state.selectedStrategy = btn.dataset.bt;
        App.activate("backtest");
      });
    });
    cardsEl.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.addEventListener("click", () => this.openDetail(btn.dataset.detail));
    });
    cardsEl.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", () => this.remove(btn.dataset.del, btn));
    });
  },

  async openDetail(id) {
    const local = Store.findStrategy(id) || {};
    const dialog = Dialog.open({
      title: local.name ? local.name + " · 策略详情" : "策略详情",
      size: "lg",
      submitText: "关闭",
      cancelText: "取消",
      body: UI.loading ? '<div class="state-block"><span class="spinner"></span><span>详情加载中…</span></div>' : "",
      onRender(d) {
        /* 只读详情：隐藏「取消」，主按钮「关闭」即关闭弹窗（提交回调由 Dialog 默认关闭处理） */
        d.el.querySelector("[data-cancel]").hidden = true;
      },
    });
    try {
      const json = await API.strategy(id);
      const s = json.data || local;
      dialog.setContent(`
        <div class="detail-grid">
          ${UI.kvGrid({
            id: s.id, name: s.name, category: s.category, status: s.status,
            universe: s.universe, universe_type: s.universe_type, freq: s.freq,
            builtin: s.builtin, origin: s.origin, min_bars: s.min_bars, version: s.version,
          })}
        </div>
        <div class="detail-section">
          <div class="detail-title">描述</div>
          <div class="detail-text">${fmt.esc(s.desc || "（无）")}</div>
        </div>
        <div class="detail-section">
          <div class="detail-title">当前参数</div>
          <pre class="code-block">${fmt.esc(JSON.stringify(s.params || {}, null, 2))}</pre>
        </div>
        <div class="detail-section">
          <div class="detail-title">参数 schema（新建策略时的可填字段）</div>
          <pre class="code-block">${fmt.esc(JSON.stringify(s.param_schema || {}, null, 2))}</pre>
        </div>
        <div class="field-hint">数据时点：${fmt.esc(json.as_of || "—")}</div>`);
    } catch (e) {
      dialog.setContent(UI.notice({
        level: "warn", title: "详情接口不可用",
        text: UI.apiErrorText(e) + "；以下为列表页已加载的字段。",
      }) + `<pre class="code-block">${fmt.esc(JSON.stringify(local, null, 2))}</pre>`);
    }
  },

  async remove(id, btn) {
    const s = Store.findStrategy(id) || {};
    const ok = await Dialog.confirm({
      title: "删除自定义策略",
      danger: true,
      confirmText: "删除",
      html: `确认删除 <strong>${fmt.esc(s.name || id)}</strong>？<br>
        <span class="flat">删除后其回测结果一并移除，内置策略不可删除。</span>`,
    });
    if (!ok) return;
    const restore = UI.busy(btn, true, "删除中…");
    try {
      await API.deleteStrategy(id);
      if (Store.state.selectedStrategy === id) Store.state.selectedStrategy = null;
      Toast.ok("策略已删除");
      await this.load();
      /* 回测页同步失效，避免展示已删除策略的结果 */
      if (window.BacktestView) BacktestView.invalidate();
    } catch (e) {
      Toast.err("删除失败：" + UI.apiErrorText(e));
      restore();
    }
  },

  /* ---------------- 新建策略 ---------------- */
  openCreate() {
    const builtins = Store.state.strategies.filter((s) => (s.origin || (s.builtin === false ? "user" : "builtin")) !== "user");
    if (!builtins.length) {
      Toast.err("没有可用的内置模板，请先刷新策略列表");
      return;
    }
    const first = builtins[0];
    const dialog = Dialog.open({
      title: "新建策略",
      subtitle: "基于内置模板创建自定义策略，参数需为模板 param_schema 的合法子集",
      size: "lg",
      submitText: "创建策略",
      busyText: "创建中…",
      body: `
        <div class="field-row">
          ${UI.field({ id: "ns-template", label: "基于模板", required: true, type: "select",
            options: builtins.map((s) => ({ value: s.id, label: s.name + "（" + s.category + "）" })) })}
          ${UI.field({ id: "ns-name", label: "策略名称", required: true, placeholder: "例如：均线突破增强（2-24 字）", max: 24 })}
        </div>
        <div class="field-row">
          ${UI.field({ id: "ns-status", label: "状态", type: "select", value: "paused",
            options: [{ value: "paused", label: "已暂停" }, { value: "running", label: "运行中" }] })}
          ${UI.field({ id: "ns-freq", label: "运行频率", type: "select", value: "日线",
            options: ["日线", "周度", "月度", "盘中"] })}
        </div>
        <div class="field-row">
          ${UI.field({ id: "ns-category", label: "类别", value: "自定义" })}
          ${UI.field({ id: "ns-universe", label: "标的池说明", placeholder: "例如：沪深300成分股" })}
        </div>
        ${UI.field({ id: "ns-desc", label: "策略描述", type: "textarea", rows: 2, placeholder: "简述信号逻辑与适用行情" })}
        <div class="detail-title">参数（来自模板 schema）</div>
        <div id="ns-params" class="param-form"></div>
        <div id="ns-preview" class="code-block" aria-live="polite"></div>
        <div class="field-hint">提交给后端的 params 只包含上方 schema 内声明的键。</div>`,
      onRender(d) {
        const tplSel = d.form.querySelector("#ns-template");
        const paramsEl = d.form.querySelector("#ns-params");
        const previewEl = d.form.querySelector("#ns-preview");

        const renderParams = () => {
          const tpl = Store.findStrategy(tplSel.value) || first;
          const entries = StrategiesView.schemaEntries(tpl.param_schema);
          d.templateId = tpl.id;
          d.entries = entries;
          if (!entries.length) {
            paramsEl.innerHTML = UI.notice({
              level: "info", title: "该模板未声明 param_schema",
              text: "将发送空参数对象 {}，如需自定义参数请选择其它模板。",
            });
            d.fallbackJson = true;
            updatePreview();
            return;
          }
          d.fallbackJson = false;
          paramsEl.innerHTML = entries.map((e) => {
            const value = e.default === undefined ? "" : e.default;
            if (e.type === "bool") {
              return `<label class="check-line">
                <input type="checkbox" data-param="${fmt.esc(e.key)}" ${value ? "checked" : ""}>
                <span>${fmt.esc(e.label)}<span class="flat"> · ${fmt.esc(e.key)}</span></span></label>`;
            }
            if (e.options) {
              const opts = (e.options || []).map((o) => ({
                value: typeof o === "object" ? o.value : o,
                label: typeof o === "object" ? (o.label || o.value) : o,
              }));
              const hasDefault = opts.some((o) => String(o.value) === String(value));
              if (!hasDefault && value !== "") opts.unshift({ value: value, label: String(value) });
              return UI.field({
                id: "np-" + e.key, name: "param:" + e.key, label: e.label + "（" + e.key + "）",
                type: "select", options: opts, value: value,
                hint: e.desc ? fmt.esc(e.desc) : "",
              });
            }
            return UI.field({
              id: "np-" + e.key, name: "param:" + e.key, label: e.label + "（" + e.key + "）",
              type: e.type === "str" ? "text" : "number",
              value: value, min: e.min, max: e.max,
              step: e.step !== undefined ? e.step : (e.type === "int" ? 1 : "any"),
              hint: (e.desc ? fmt.esc(e.desc) + " " : "") +
                (e.min !== undefined || e.max !== undefined
                  ? `<span class="flat">取值 ${fmt.or(e.min, "-∞")} ~ ${fmt.or(e.max, "+∞")}</span>` : ""),
            });
          }).join("");
          paramsEl.querySelectorAll("input,select").forEach((el) => el.addEventListener("input", updatePreview));
          paramsEl.querySelectorAll("input,select").forEach((el) => el.addEventListener("change", updatePreview));
          updatePreview();
        };

        const collect = () => {
          if (d.fallbackJson) return {};
          const out = {};
          (d.entries || []).forEach((e) => {
            const el = paramsEl.querySelector(`[data-param="${CSS.escape(e.key)}"], [name="param:${CSS.escape(e.key)}"]`);
            if (!el) return;
            if (e.type === "bool") out[e.key] = !!el.checked;
            else if (e.type === "int") {
              const n = parseInt(el.value, 10);
              if (!isNaN(n)) out[e.key] = n;
            } else if (e.type === "number") {
              const n = parseFloat(el.value);
              if (!isNaN(n)) out[e.key] = n;
            } else if (el.value !== "") out[e.key] = el.value;
          });
          return out;
        };

        function updatePreview() {
          d.params = collect();
          previewEl.textContent = "将提交：params = " + JSON.stringify(d.params);
        }

        tplSel.addEventListener("change", () => {
          const tpl = Store.findStrategy(tplSel.value) || {};
          if (tpl.category) d.form.querySelector("#ns-category").value = tpl.category;
          if (tpl.freq) d.form.querySelector("#ns-freq").value = tpl.freq;
          if (tpl.universe) d.form.querySelector("#ns-universe").value = tpl.universe;
          renderParams();
        });
        if (first.category) d.form.querySelector("#ns-category").value = first.category;
        if (first.freq) d.form.querySelector("#ns-freq").value = first.freq;
        if (first.universe) d.form.querySelector("#ns-universe").value = first.universe;
        renderParams();
        d.el.addEventListener("input", updatePreview);
      },
      validate(d) {
        const name = String(d.form.querySelector("#ns-name").value || "").trim();
        if (name.length < 2 || name.length > 24) return "策略名称长度需为 2-24 个字符";
        if (!d.templateId) return "请选择内置模板";
        if (Store.state.strategies.some((s) => s.name === name)) return "已存在同名策略，请换一个名称";
        return null;
      },
      /* 箭头函数：回调里的 this 必须指向视图，否则创建成功后会因 this.load 未定义而报错 */
      onSubmit: async (d) => {
        const payload = {
          template: d.templateId,
          name: String(d.form.querySelector("#ns-name").value || "").trim(),
          category: String(d.form.querySelector("#ns-category").value || "自定义").trim() || "自定义",
          status: d.form.querySelector("#ns-status").value,
          freq: d.form.querySelector("#ns-freq").value,
          universe: String(d.form.querySelector("#ns-universe").value || "").trim(),
          desc: String(d.form.querySelector("#ns-desc").value || "").trim(),
          params: d.params || {},
        };
        try {
          const json = await API.createStrategy(payload);
          const created = json.data || {};
          Toast.ok("策略「" + (created.name || payload.name) + "」已创建");
          await this.load();
          if (window.BacktestView) BacktestView.invalidate();
          return true;
        } catch (e) {
          d.setError("创建失败：" + UI.apiErrorText(e));
          return false;
        }
      },
    });
    return dialog;
  },
};

window.StrategiesView = StrategiesView;
