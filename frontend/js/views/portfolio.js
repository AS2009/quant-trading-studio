/*
 * views/portfolio.js — 持仓管理视图
 * 职责：
 *   1. 账户卡片 + 资产配置环形图（GET /api/portfolio/overview）
 *   2. 权益曲线（GET /api/portfolio/equity?days=90）：数据不足时如实提示「历史快照累积中，当前 N 天」
 *   3. 手工录入 / 修改 / 删除持仓（POST / DELETE /api/portfolio/holdings）
 *   4. 设置现金（POST /api/portfolio/cash）与账户模式切换 manual / paper（POST /api/portfolio/mode）
 * 对外接口：PortfolioView.render(container) / refresh()
 */
"use strict";

const PortfolioView = {
  id: "portfolio",
  label: "持仓管理",
  container: null,

  state: { loading: false, equityDays: 90 },

  render(container) {
    this.container = container;
    Charts.dispose("pf-alloc-chart");
    Charts.dispose("pf-equity-chart");
    container.innerHTML = `
      <h2 class="view-title">持仓管理
        <span class="view-sub" id="pf-asof"></span></h2>

      <div class="panel">
        <div class="panel-title">
          <span>账户操作</span>
          <span class="toolbar">
            <button type="button" class="btn btn-sm btn-primary" id="pf-add">+ 录入 / 覆盖持仓</button>
            <button type="button" class="btn btn-sm" id="pf-cash">设置现金</button>
            <button type="button" class="btn btn-sm" id="pf-reload">刷新</button>
          </span>
        </div>
        <div class="mode-row">
          <span class="inline-label">账户模式</span>
          <span class="segmented" role="group" aria-label="账户模式切换">
            <button type="button" class="seg" data-mode="manual">手工记账</button>
            <button type="button" class="seg" data-mode="paper">模拟盘</button>
          </span>
          <span class="flat" id="pf-mode-hint"></span>
        </div>
        <div id="pf-msg" class="inline-msg" role="status" hidden></div>
      </div>

      <div id="pf-cards" class="card-grid cols-4"></div>

      <div class="two-col">
        <div class="panel">
          <div class="panel-title">资产配置</div>
          <div id="pf-alloc-chart" class="chart chart-sm"></div>
        </div>
        <div class="panel">
          <div class="panel-title">
            <span>账户权益曲线</span>
            <span class="toolbar">
              <label class="inline-label" for="pf-equity-days">区间</label>
              <select id="pf-equity-days" class="input-inline">
                <option value="30">近 30 天</option>
                <option value="90" selected>近 90 天</option>
                <option value="180">近 180 天</option>
                <option value="365">近 365 天</option>
              </select>
            </span>
          </div>
          <div id="pf-equity-note" class="inline-msg inline-msg-info" hidden></div>
          <div id="pf-equity-chart" class="chart chart-md"></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>持仓明细 <span class="panel-sub" id="pf-pos-count"></span></span>
          <span class="panel-sub">数量／成本可手工录入，收益率按最新价与摊薄成本计算</span>
        </div>
        <div class="table-wrap">
          <table class="data-table" id="pf-holdings-table">
            <thead>
              <tr>
                <th>代码</th><th>名称</th><th>行业</th>
                <th class="num">数量</th><th class="num">可卖</th><th class="num">成本价</th><th class="num">现价</th>
                <th class="num">市值(元)</th><th class="num">成本金额</th>
                <th class="num">当日盈亏</th><th class="num">累计盈亏</th><th class="num">收益率</th>
                <th class="act">操作</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
      </div>`;

    this.bind();
    return this.load();
  },

  refresh() { return this.render(this.container); },

  bind() {
    const c = this.container;
    c.querySelector("#pf-add").addEventListener("click", () => this.openHoldingDialog());
    c.querySelector("#pf-cash").addEventListener("click", () => this.openCashDialog());
    c.querySelector("#pf-reload").addEventListener("click", () => this.load(true));
    c.querySelectorAll("[data-mode]").forEach((b) => {
      b.addEventListener("click", () => this.changeMode(b.dataset.mode));
    });
    const days = c.querySelector("#pf-equity-days");
    days.value = String(this.state.equityDays);
    days.addEventListener("change", () => {
      this.state.equityDays = Number(days.value) || 90;
      this.loadEquity();
    });
  },

  msg(message, type) {
    const el = this.container.querySelector("#pf-msg");
    if (!el) return;
    if (!message) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.className = "inline-msg inline-msg-" + (type || "info");
    el.textContent = message;
  },

  currentMode() {
    const st = Store.state.status || {};
    if (this.state.mode) return this.state.mode;
    if (st.trade && st.trade.mode) return st.trade.mode;
    return "manual";
  },

  renderModeBar() {
    const mode = this.currentMode();
    this.container.querySelectorAll("[data-mode]").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === mode));
    const hint = this.container.querySelector("#pf-mode-hint");
    hint.textContent = mode === "paper"
      ? "模拟盘：持仓由「交易」页的模拟委托成交驱动"
      : "手工记账：持仓/现金由本页手工录入（不含自动撮合）";
  },

  async load(isManual) {
    if (this.state.loading) return;
    this.state.loading = true;
    this.renderModeBar();
    if (isManual) this.msg("");
    await Promise.all([this.loadOverview(), this.loadHoldings(), this.loadEquity()]);
    this.state.loading = false;
  },

  async loadOverview() {
    const el = this.container.querySelector("#pf-cards");
    UI.loading(el, "账户数据加载中…");
    try {
      const json = await API.portfolioOverview();
      const ov = json.data || {};
      this.state.overviewSnapshot = ov;
      this.container.querySelector("#pf-asof").textContent =
        (json.as_of ? "数据时点 " + json.as_of : "");
      const cards = [
        { k: "总资产(元)", v: fmt.num(ov.total_assets, 2), c: "flat" },
        { k: "持仓市值(元)", v: fmt.num(ov.market_value, 2), c: "flat" },
        { k: "可用现金(元)", v: fmt.num(ov.available_cash !== undefined ? ov.available_cash : ov.cash, 2), c: "flat",
          s: ov.frozen_cash ? "冻结 " + fmt.num(ov.frozen_cash, 2) : "" },
        { k: "当日盈亏(元)", v: fmt.signed(ov.day_pnl, 2), c: fmt.color(ov.day_pnl) },
        { k: "累计盈亏(元)", v: fmt.signed(ov.total_pnl, 2), c: fmt.color(ov.total_pnl) },
        { k: "总收益率", v: fmt.pct(ov.total_return_pct, true), c: fmt.color(ov.total_return_pct) },
        { k: "持仓数量", v: fmt.int(ov.positions_count) + " 只", c: "flat" },
        { k: "现金占比", v: ov.total_assets ? fmt.pct((ov.cash / ov.total_assets) * 100, false) : "—", c: "flat" },
      ];
      el.innerHTML = cards.map((x) => `
        <div class="stat-card">
          <div class="k">${fmt.esc(x.k)}</div>
          <div class="v ${x.c}">${fmt.esc(x.v)}</div>
          ${x.s ? `<div class="s flat">${fmt.esc(x.s)}</div>` : ""}
        </div>`).join("");
      Charts.allocation("pf-alloc-chart", (ov.allocation || []).map((a) => ({ name: a.name, value: a.value })));
      requestAnimationFrame(() => Charts.resizeAll());
    } catch (e) {
      UI.setError(el, { title: "账户数据加载失败", message: UI.apiErrorText(e), retry: () => this.loadOverview() });
      Charts.placeholder("pf-alloc-chart", "无资产配置数据");
    }
  },

  async loadHoldings() {
    const c = this.container;
    const tbody = c.querySelector("#pf-holdings-table tbody");
    if (!tbody) return;
    UI.tbodyLoading(tbody, 13, "持仓加载中…");
    try {
      const json = await API.holdings();
      const list = (json.data || []).filter(Boolean);
      this.state.holdings = list;
      c.querySelector("#pf-pos-count").textContent = list.length ? list.length + " 只" : "";
      UI.tbody(tbody, list.map((h) => `
        <tr data-code="${fmt.esc(h.code)}">
          <td>${fmt.esc(h.code)}</td>
          <td>${fmt.esc(h.name || "—")}</td>
          <td class="flat">${fmt.esc(fmt.or(h.industry, "—"))}</td>
          <td class="num">${fmt.int(h.qty)}</td>
          <td class="num flat">${fmt.int(h.available_qty)}</td>
          <td class="num">${fmt.num(h.cost, 3)}</td>
          <td class="num">${fmt.num(h.price, 2)}</td>
          <td class="num">${fmt.num(h.market_value, 2)}</td>
          <td class="num flat">${fmt.num(h.cost_value, 2)}</td>
          <td class="num ${fmt.color(h.day_pnl)}">${fmt.signed(h.day_pnl, 2)}</td>
          <td class="num ${fmt.color(h.total_pnl)}">${fmt.signed(h.total_pnl, 2)}</td>
          <td class="num ${fmt.color(h.return_pct)}">${fmt.pct(h.return_pct, true)}</td>
          <td class="act">
            <span class="btn-row">
              <button type="button" class="btn btn-sm" data-edit="${fmt.esc(h.code)}">修改</button>
              <button type="button" class="btn btn-sm btn-danger" data-del="${fmt.esc(h.code)}">删除</button>
            </span>
          </td>
        </tr>`), {
        colspan: 13,
        empty: "暂无持仓",
        emptyHint: "点击上方「+ 录入 / 覆盖持仓」手工录入，或在「交易」页用模拟盘买入",
      });
      tbody.querySelectorAll("[data-edit]").forEach((b) =>
        b.addEventListener("click", () => this.openHoldingDialog(this.findHolding(b.dataset.edit))));
      tbody.querySelectorAll("[data-del]").forEach((b) =>
        b.addEventListener("click", () => this.removeHolding(b.dataset.del, b)));
    } catch (e) {
      UI.tbodyError(tbody, {
        colspan: 13, title: "持仓加载失败",
        message: UI.apiErrorText(e), retry: () => this.loadHoldings(),
      });
    }
  },

  findHolding(code) {
    return (this.state.holdings || []).find((h) => h.code === code) || null;
  },

  async loadEquity() {
    const noteEl = this.container.querySelector("#pf-equity-note");
    const chartEl = this.container.querySelector("#pf-equity-chart");
    UI.loading(chartEl, "权益曲线加载中…");
    try {
      const json = await API.portfolioEquity(this.state.equityDays);
      const list = (json.data || []).filter(Boolean);
      const points = list.length;
      if (points && points < 30) {
        /* 后端只返回已积累的快照：如实展示，不做插值 */
        noteEl.hidden = false;
        noteEl.className = "inline-msg inline-msg-info";
        noteEl.textContent = "历史快照累积中，当前 " + points + " 天（" +
          list[0].date + " ~ " + list[points - 1].date + "）。快照随每日结算累积，暂不展示更早区间。";
      } else if (!points) {
        noteEl.hidden = false;
        noteEl.className = "inline-msg inline-msg-warn";
        noteEl.textContent = "暂无历史权益快照：等待后端完成首个交易日结算后显示。";
      } else {
        noteEl.hidden = true;
      }
      Charts.equity("pf-equity-chart", list.map((x) => x.date), list.map((x) => x.equity));
      requestAnimationFrame(() => Charts.resizeAll());
    } catch (e) {
      UI.setError(chartEl, { title: "权益曲线加载失败", message: UI.apiErrorText(e), retry: () => this.loadEquity() });
    }
  },

  /* ---------------- 录入 / 修改持仓 ---------------- */
  openHoldingDialog(existing) {
    const isEdit = !!existing;
    const dialog = Dialog.open({
      title: isEdit ? "修改持仓（" + existing.code + "）" : "录入持仓",
      subtitle: "手工录入的持仓直接写入账户；同一代码再次提交视为覆盖更新",
      submitText: isEdit ? "覆盖更新" : "录入",
      busyText: "提交中…",
      body: `
        <div class="field-row">
          ${UI.field({ id: "ph-code", label: "标的代码", required: true, value: isEdit ? existing.code : "",
            placeholder: "600519 / sh600519 / 600519.SH", hint: "后端负责规范化与代码校验" })}
          ${UI.field({ id: "ph-name", label: "名称（可选）", value: isEdit ? (existing.name || "") : "",
            placeholder: "留空则由后端补全" })}
        </div>
        <div class="field-row">
          ${UI.field({ id: "ph-qty", label: "数量（股）", required: true, type: "number", min: 1, step: 100,
            value: isEdit ? existing.qty : "", hint: "A 股最小交易单位 100 股，卖出零股请按实际填写" })}
          ${UI.field({ id: "ph-cost", label: "成本价（元）", required: true, type: "number", min: 0, step: 0.001,
            value: isEdit ? existing.cost : "", hint: "摊薄成本价，最多 3 位小数" })}
        </div>
        ${UI.field({ id: "ph-available", label: "可卖数量（可选，默认等于数量）", type: "number", min: 0, step: 100,
          value: isEdit ? existing.available_qty : "", hint: "T+1 规则下当日买入不可卖；留空表示全部可卖" })}
        <div id="ph-est" class="code-block"></div>`,
      onRender(d) {
        const est = d.form.querySelector("#ph-est");
        const update = () => {
          const qty = Number(d.form.querySelector("#ph-qty").value || 0);
          const cost = Number(d.form.querySelector("#ph-cost").value || 0);
          est.textContent = qty && cost
            ? "市值估算（按成本价）= " + fmt.num(qty * cost, 2) + " 元；提交后以后端返回的最新价为准"
            : "填写数量与成本价后显示金额估算";
        };
        d.form.addEventListener("input", update);
        update();
      },
      validate(d) {
        const code = String(d.form.querySelector("#ph-code").value || "").trim();
        const check = UI.normalizeSymbol(code);
        if (!check.ok) return check.message;
        const qty = Number(d.form.querySelector("#ph-qty").value);
        if (!qty || qty <= 0 || !Number.isInteger(qty)) return "数量需为正整数（股）";
        const cost = Number(d.form.querySelector("#ph-cost").value);
        if (!isFinite(cost) || cost <= 0) return "成本价需大于 0";
        const avail = d.form.querySelector("#ph-available").value;
        if (avail !== "" && (Number(avail) < 0 || Number(avail) > qty)) return "可卖数量需在 0 ~ 数量之间";
        return null;
      },
      /* 箭头函数：回调里的 this 必须指向视图，否则成功提交后会因 this.load 未定义而报错 */
      onSubmit: async (d) => {
        const read = (id) => String(d.form.querySelector(id).value || "").trim();
        const codeCheck = UI.normalizeSymbol(read("#ph-code"));
        const avail = read("#ph-available");
        const payload = {
          code: codeCheck.code,
          qty: Number(read("#ph-qty")),
          cost: Number(read("#ph-cost")),
        };
        if (read("#ph-name")) payload.name = read("#ph-name");
        if (avail !== "") payload.available_qty = Number(avail);
        try {
          const json = await API.addHolding(payload);
          const pos = json.data || {};
          Toast.ok((isEdit ? "已更新持仓：" : "已录入持仓：") + (pos.name || payload.code) +
            " " + fmt.int(pos.qty || payload.qty) + " 股");
          this.msg("");
          await this.load();
          return true;
        } catch (e) {
          this.msg("持仓提交失败：" + UI.apiErrorText(e), "err");
          d.setError("提交失败：" + UI.apiErrorText(e));
          return false;
        }
      },
    });
    return dialog;
  },

  async removeHolding(code, btn) {
    const h = this.findHolding(code) || {};
    const ok = await Dialog.confirm({
      title: "删除持仓",
      danger: true,
      confirmText: "删除",
      html: `确认删除 <strong>${fmt.esc(h.name || "")} ${fmt.esc(code)}</strong>（${fmt.int(h.qty)} 股）？<br>
        <span class="flat">仅移除本地账户记录，不产生任何真实委托。</span>`,
    });
    if (!ok) return;
    const restore = UI.busy(btn, true, "删除中…");
    try {
      const json = await API.removeHolding(code);
      const data = json.data || {};
      Toast.ok("已删除 " + (data.code || code) + (data.deleted === false ? "（后端返回 deleted=false）" : ""));
      await this.load();
    } catch (e) {
      Toast.err("删除失败：" + UI.apiErrorText(e));
      this.msg("删除失败：" + UI.apiErrorText(e), "err");
      restore();
    }
  },

  /* ---------------- 现金 ---------------- */
  openCashDialog() {
    const ov = this.state.overviewSnapshot || {};
    Dialog.open({
      title: "调整现金",
      subtitle: "按契约 POST /api/portfolio/cash {amount}；后端当前实现决定是「入账增量」还是「直接设为该值」",
      submitText: "提交",
      busyText: "提交中…",
      body: `
        ${UI.field({ id: "pc-amount", label: "金额（元，可为负）", required: true, type: "number", step: 100,
          value: "", placeholder: "例如 50000 或 -20000",
          hint: "正数入金、负数出金；提交后以接口返回的账户可用现金为准" })}
        <div class="field-hint">当前可用现金：${fmt.esc(fmt.num(ov.available_cash !== undefined ? ov.available_cash : ov.cash, 2))} 元</div>`,
      validate(d) {
        const v = d.form.querySelector("#pc-amount").value;
        if (v === "" || !isFinite(Number(v)) || Number(v) === 0) return "请输入非 0 数字金额";
        return null;
      },
      onSubmit: async (d) => {
        const amount = Number(d.form.querySelector("#pc-amount").value);
        try {
          const json = await API.setCash(amount);
          const acc = json.data || {};
          Toast.ok("现金已提交，后端返回可用现金 " +
            fmt.num(acc.available_cash !== undefined ? acc.available_cash : acc.cash, 2) + " 元");
          this.msg("现金调整已提交：本次 " + fmt.num(amount, 2) + " 元", "ok");
          await this.load();
          return true;
        } catch (e) {
          d.setError("提交失败：" + UI.apiErrorText(e));
          return false;
        }
      },
    });
  },

  /* ---------------- 模式切换 ---------------- */
  async changeMode(mode) {
    if (mode === this.currentMode()) return;
    const segs = this.container.querySelectorAll("[data-mode]");
    segs.forEach((b) => { b.disabled = true; });
    try {
      const json = await API.setPortfolioMode(mode);
      const applied = (json.data && json.data.mode) || mode;
      this.state.mode = applied;
      Toast.ok("账户模式已切换为「" + (applied === "paper" ? "模拟盘" : "手工记账") + "」");
      this.renderModeBar();
    } catch (e) {
      Toast.err("模式切换失败：" + UI.apiErrorText(e));
      this.msg("模式切换失败：" + UI.apiErrorText(e), "err");
      this.renderModeBar();
    } finally {
      segs.forEach((b) => { b.disabled = false; });
    }
  },
};

window.PortfolioView = PortfolioView;
