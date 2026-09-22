/*
 * views/market.js — 行情看板视图
 * 职责：
 *   1. 指数卡片（/api/market/overview）、市场概况（广度 + 资金）
 *   2. 板块涨幅榜（/api/market/sectors?limit=20，提示框含领涨股）
 *   3. 自选股管理：列表 + 添加（POST /api/watchlist）+ 删除（DELETE /api/watchlist/<code>）
 *   4. 个股 K 线（/api/market/kline，支持区间切换）
 * 接口约定的关键防线：
 *   - /api/watchlist 未实现时降级为「后端默认标的」只读表，并在区域内联提示，不白屏
 *   - quotes 里 industry 不在冻结契约中，因此表格不展示行业列，避免出现 undefined
 * 对外接口：MarketView.render(container) / MarketView.refresh()
 */
"use strict";

const MarketView = {
  id: "market",
  label: "行情看板",
  container: null,

  state: {
    klineDays: 120,
    quotes: [],
    quotesMap: {},
    watchFallback: false,
    loading: false,
  },

  /* 输入 600519 / sh600519 / 600519.SH / 600519.sh 均视为合法，规范化交给后端 */
  normalizeInput(raw) { return UI.normalizeSymbol(raw); },

  render(container) {
    this.container = container;
    Charts.dispose("mk-sector-chart");
    Charts.dispose("mk-kline-chart");
    container.innerHTML = `
      <h2 class="view-title">行情看板
        <span class="view-sub" id="mk-asof"></span></h2>

      <div id="mk-indices" class="card-grid cols-4"></div>

      <div class="two-col">
        <div class="panel">
          <div class="panel-title"><span>市场概况</span><span class="panel-sub" id="mk-breadth-sub"></span></div>
          <div id="mk-breadth"></div>
        </div>
        <div class="panel">
          <div class="panel-title">
            <span>板块涨幅榜 <span class="panel-sub">仅展示涨幅前 12</span></span>
            <span class="btn-row">
              <button type="button" class="btn btn-sm" id="mk-sector-refresh">刷新</button>
            </span>
          </div>
          <div id="mk-sector-chart" class="chart chart-sm"></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>自选股行情 <span class="panel-sub" id="mk-watch-count"></span></span>
          <span class="toolbar">
            <input type="text" id="mk-watch-input" class="input-inline" inputmode="text"
              placeholder="600519 / sh600519 / 600519.SH" aria-label="添加自选标的代码">
            <button type="button" class="btn btn-sm btn-primary" id="mk-watch-add">+ 添加标的</button>
            <button type="button" class="btn btn-sm" id="mk-watch-reload">刷新行情</button>
          </span>
        </div>
        <div id="mk-watch-msg" class="inline-msg" role="status" hidden></div>
        <div class="table-wrap">
          <table class="data-table" id="mk-quotes-table">
            <thead>
              <tr>
                <th>代码</th><th>名称</th><th>数据时点</th>
                <th class="num">现价</th><th class="num">涨跌额</th><th class="num">涨跌幅</th>
                <th class="num">成交量(万手)</th><th class="num">成交额(亿)</th><th class="num">换手率</th>
                <th class="num">PE(TTM)</th><th class="num">PB</th><th class="num">总市值(亿)</th>
                <th class="act">操作</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>个股 K 线 <span class="panel-sub" id="mk-kline-label">—</span></span>
          <span class="toolbar">
            <label class="inline-label" for="mk-kline-days">区间</label>
            <select id="mk-kline-days" class="input-inline">
              <option value="60">近 60 日</option>
              <option value="120" selected>近 120 日</option>
              <option value="250">近 250 日</option>
              <option value="500">近 500 日</option>
            </select>
          </span>
        </div>
        <div id="mk-quote-detail" class="quote-detail"></div>
        <div id="mk-kline-chart" class="chart chart-lg"></div>
      </div>`;

    this.bind();
    return this.load();
  },

  refresh() { return this.render(this.container); },

  bind() {
    const c = this.container;
    const input = c.querySelector("#mk-watch-input");
    c.querySelector("#mk-watch-add").addEventListener("click", () => this.addWatch());
    if (input) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); this.addWatch(); }
      });
      input.addEventListener("input", () => this.watchMsg(""));
    }
    c.querySelector("#mk-watch-reload").addEventListener("click", () => this.loadQuotes(true));
    c.querySelector("#mk-sector-refresh").addEventListener("click", () => this.loadSectors());
    const days = c.querySelector("#mk-kline-days");
    days.value = String(this.state.klineDays);
    days.addEventListener("change", () => {
      this.state.klineDays = Number(days.value) || 120;
      this.loadKline(true);
    });
  },

  watchMsg(message, type) {
    const el = this.container.querySelector("#mk-watch-msg");
    if (!el) return;
    if (!message) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.className = "inline-msg inline-msg-" + (type || "info");
    el.textContent = message;
  },

  async load() {
    if (this.state.loading) return;
    this.state.loading = true;
    this.watchMsg("");
    try {
      await Promise.all([this.loadOverview(), this.loadSectors()]);
      await this.loadQuotes(true);
    } finally {
      this.state.loading = false;
    }
  },

  /* ---------------- 指数 + 市场概况 ---------------- */
  async loadOverview() {
    const cardsEl = this.container.querySelector("#mk-indices");
    const breadthEl = this.container.querySelector("#mk-breadth");
    UI.loading(cardsEl, "行情加载中…");
    UI.loading(breadthEl, "市场概况加载中…");
    try {
      const json = await API.marketOverview();
      const ov = json.data || {};
      const indices = Array.isArray(ov.indices) ? ov.indices : [];
      const breadth = ov.breadth || {};
      const totalAmount = ov.total_amount_yi !== undefined ? ov.total_amount_yi : breadth.total_amount_yi;
      const mainInflow = ov.main_net_inflow_yi !== undefined ? ov.main_net_inflow_yi : breadth.main_net_inflow_yi;
      const northInflow = ov.north_net_inflow_yi !== undefined ? ov.north_net_inflow_yi : breadth.north_net_inflow_yi;

      const asOfEl = this.container.querySelector("#mk-asof");
      if (asOfEl) asOfEl.textContent = json.as_of ? "数据时点 " + json.as_of : "";

      if (!indices.length) {
        cardsEl.innerHTML = UI.emptyBlock({ title: "暂无指数数据", hint: "后端返回 indices 为空" });
      } else {
        cardsEl.innerHTML = indices.map((i) => `
          <div class="stat-card">
            <div class="k">${fmt.esc(i.name)} · ${fmt.esc(i.code)}</div>
            <div class="v ${fmt.color(i.change_pct)}">${fmt.num(i.point, 2)}</div>
            <div class="s ${fmt.color(i.change_pct)}">${fmt.signed(i.change, 2)} / ${fmt.pct(i.change_pct, true)}
              <span class="flat"> 成交 ${fmt.num(i.amount_yi, 1)} 亿</span></div>
          </div>`).join("");
      }

      this.container.querySelector("#mk-breadth-sub").textContent =
        breadth.total ? "样本 " + fmt.int(breadth.total) + " 只" : "";
      const amountYi = Number(totalAmount);
      breadthEl.innerHTML = `
        <div class="stat-row">
          <div class="stat-item"><div class="k">上涨家数</div><div class="v up">${fmt.int(breadth.up)}</div></div>
          <div class="stat-item"><div class="k">下跌家数</div><div class="v down">${fmt.int(breadth.down)}</div></div>
          <div class="stat-item"><div class="k">平盘家数</div><div class="v flat">${fmt.int(breadth.flat)}</div></div>
          <div class="stat-item"><div class="k">涨停 / 跌停</div><div class="v">
            <span class="up">${fmt.int(breadth.limit_up)}</span> / <span class="down">${fmt.int(breadth.limit_down)}</span></div></div>
          <div class="stat-item"><div class="k">两市成交额</div>
            <div class="v">${isFinite(amountYi) ? (amountYi / 10000).toFixed(2) + " 万亿" : "—"}</div>
            <div class="s flat">${isFinite(amountYi) ? fmt.num(amountYi, 0) + " 亿" : "—"}</div></div>
          <div class="stat-item"><div class="k">主力净流入</div>
            <div class="v ${fmt.color(mainInflow)}">${mainInflow === null || mainInflow === undefined ? "—" : fmt.signed(mainInflow, 2) + " 亿"}</div>
            <div class="s flat">北向 ${northInflow === null || northInflow === undefined ? "暂不可用" : fmt.signed(northInflow, 2) + " 亿"}</div></div>
        </div>`;
    } catch (e) {
      UI.setError(cardsEl, { title: "指数行情加载失败", message: UI.apiErrorText(e), retry: () => this.loadOverview() });
      breadthEl.innerHTML = "";
    }
  },

  /* ---------------- 板块涨幅榜 ---------------- */
  async loadSectors() {
    const el = this.container.querySelector("#mk-sector-chart");
    UI.loading(el, "板块数据加载中…");
    try {
      const json = await API.marketSectors(20);
      const list = (json.data || []).filter(Boolean);
      if (!list.length) return Charts.placeholder("mk-sector-chart", "暂无板块数据", "数据源未返回行业板块");
      const top = list.slice(0, 12).reverse();
      Charts.sectors(
        "mk-sector-chart",
        top.map((s) => s.name),
        top.map((s) => Number(s.change_pct)),
        top.map((s) => s.leading_stock
          ? "领涨：" + s.leading_stock + (s.leading_code ? " " + s.leading_code : "") +
            " · 净流入 " + fmt.signed(s.net_inflow_yi, 2) + " 亿 · 涨/跌 " + fmt.int(s.up_count) + "/" + fmt.int(s.down_count)
          : "")
      );
      requestAnimationFrame(() => Charts.resizeAll());
    } catch (e) {
      UI.setError(el, { title: "板块数据加载失败", message: UI.apiErrorText(e), retry: () => this.loadSectors() });
    }
  },

  /* ---------------- 自选股 ---------------- */
  async loadQuotes(force) {
    const tbody = this.container.querySelector("#mk-quotes-table tbody");
    const countEl = this.container.querySelector("#mk-watch-count");
    const addBtn = this.container.querySelector("#mk-watch-add");
    const input = this.container.querySelector("#mk-watch-input");
    if (!tbody) return; /* 视图未挂载（快速切页竞态）时直接跳过，避免空引用 */
    UI.tbodyLoading(tbody, 13, "自选股行情加载中…");
    await Store.loadWatchlist(force);

    const wl = Store.state.watchlist;
    if (!wl.supported) {
      /* 旧版后端无 /api/watchlist：改为读取后端默认标的，增删按钮禁用 */
      this.state.watchFallback = true;
      addBtn.disabled = true;
      input.disabled = true;
      addBtn.title = "当前后端未提供 /api/watchlist";
      this.watchMsg("自选池接口不可用（" + UI.apiErrorText(wl.error) + "），暂时展示后端默认标的，无法增删。", "warn");
    } else {
      this.state.watchFallback = false;
      addBtn.disabled = false;
      input.disabled = false;
      if (!wl.items.length && wl.codes.length) {
        this.watchMsg("自选池有 " + wl.codes.length + " 只标的但未返回行情明细。", "warn");
      }
    }

    let quotes = wl.items;
    try {
      if (!quotes.length) {
        const json = await API.quotes(wl.supported ? wl.codes.join(",") : "");
        quotes = json.data || [];
        if (!wl.supported) this.state.watchFallback = true;
      }
      this.state.quotes = quotes.filter(Boolean);
      this.state.quotesMap = Object.fromEntries(this.state.quotes.map((q) => [q.code, q]));
      countEl.textContent = Store.state.watchlist.supported
        ? "共 " + this.state.quotes.length + " 只"
        : "共 " + this.state.quotes.length + " 只（后端默认池）";
      this.renderQuotes();
      /* 默认 K 线标的：优先上次选中，其次第一只 */
      const codes = this.state.quotes.map((q) => q.code);
      let target = Store.state.selectedSymbol;
      if (!target || codes.indexOf(target) < 0) target = codes[0] || null;
      if (target) await this.loadKline(true, target);
      else Charts.placeholder("mk-kline-chart", "请选择自选股", "点击上方表格中的标的查看 K 线");
    } catch (e) {
      UI.tbodyError(tbody, {
        colspan: 13, title: "自选股行情加载失败",
        message: UI.apiErrorText(e), retry: () => this.loadQuotes(true),
      });
    }
  },

  renderQuotes() {
    const tbody = this.container.querySelector("#mk-quotes-table tbody");
    const canEdit = !this.state.watchFallback;
    const rows = this.state.quotes.map((q) => {
      const selected = q.code === Store.state.selectedSymbol;
      return `
      <tr class="selectable ${selected ? "selected" : ""}" data-code="${fmt.esc(q.code)}">
        <td>${fmt.esc(q.code)}</td>
        <td>${fmt.esc(q.name)}</td>
        <td class="flat">${fmt.esc(fmt.or(q.ts, "—"))}</td>
        <td class="num ${fmt.color(q.change_pct)}">${fmt.num(q.price, 2)}</td>
        <td class="num ${fmt.color(q.change)}">${fmt.signed(q.change, 2)}</td>
        <td class="num ${fmt.color(q.change_pct)}">${fmt.pct(q.change_pct, true)}</td>
        <td class="num flat">${fmt.num(q.volume_wan, 2)}</td>
        <td class="num flat">${fmt.num(q.amount_yi, 2)}</td>
        <td class="num flat">${fmt.num(q.turnover_pct, 2)}%</td>
        <td class="num flat">${q.pe_ttm === null || q.pe_ttm === undefined ? "—" : fmt.num(q.pe_ttm, 2)}</td>
        <td class="num flat">${q.pb === null || q.pb === undefined ? "—" : fmt.num(q.pb, 2)}</td>
        <td class="num flat">${q.market_cap_yi === null || q.market_cap_yi === undefined ? "—" : fmt.num(q.market_cap_yi, 1)}</td>
        <td class="act">${canEdit
          ? `<button type="button" class="btn btn-sm btn-danger" data-del="${fmt.esc(q.code)}"
               aria-label="从自选池删除 ${fmt.esc(q.name)}">删除</button>`
          : '<span class="flat">—</span>'}</td>
      </tr>`;
    });
    UI.tbody(tbody, rows, {
      colspan: 13,
      empty: "自选池为空",
      emptyHint: this.state.watchFallback
        ? "后端未提供 /api/watchlist 且默认标的列表为空"
        : "输入代码后点击「+ 添加标的」，例如 600519 或 600519.SH",
    });

    if (!rows.length) {
      const cell = tbody.querySelector(".empty-inline");
      if (cell && !this.state.watchFallback) {
        const quick = document.createElement("div");
        quick.className = "quick-add";
        quick.innerHTML = [
          ["600519", "贵州茅台"], ["300750", "宁德时代"], ["002594", "比亚迪"], ["600036", "招商银行"],
        ].map(([code, name]) => `<button type="button" class="chip" data-quick="${code}">+ ${code} ${name}</button>`).join("");
        cell.appendChild(quick);
        quick.querySelectorAll("[data-quick]").forEach((b) => {
          b.addEventListener("click", () => {
            const input = this.container.querySelector("#mk-watch-input");
            input.value = b.dataset.quick;
            this.addWatch();
          });
        });
      }
    }

    tbody.querySelectorAll("tr.selectable").forEach((tr) => {
      tr.addEventListener("click", () => {
        this.loadKline(true, tr.dataset.code).catch((e) =>
          Toast.err("K 线数据加载失败：" + UI.apiErrorText(e)));
      });
    });
    tbody.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.removeWatch(btn.dataset.del, btn);
      });
    });
  },

  async addWatch() {
    const input = this.container.querySelector("#mk-watch-input");
    const btn = this.container.querySelector("#mk-watch-add");
    const check = this.normalizeInput(input.value);
    if (!check.ok) {
      this.watchMsg(check.message, "err");
      input.focus();
      return;
    }
    const restore = UI.busy(btn, true, "添加中…");
    try {
      const json = await API.addWatch(check.code);
      const codes = (json.data && json.data.codes) || [];
      input.value = "";
      this.watchMsg("已添加 " + check.code + "（后端规范化后自选池 " + codes.length + " 只）", "ok");
      Toast.ok("已添加到自选池：" + check.code);
      Store.setWatchlistCodes(codes);
      await Store.loadWatchlist(true);
      await this.loadQuotes(true);
    } catch (e) {
      this.watchMsg("添加失败：" + UI.apiErrorText(e), "err");
    } finally {
      restore();
    }
  },

  async removeWatch(code, btn) {
    const q = this.state.quotesMap[code] || {};
    const ok = await Dialog.confirm({
      title: "从自选池删除",
      danger: true,
      confirmText: "删除",
      html: `确认将 <strong>${fmt.esc(q.name || "")} ${fmt.esc(code)}</strong> 从自选池移除？<br>
        <span class="flat">仅影响自选列表，不影响持仓与策略。</span>`,
    });
    if (!ok) return;
    const restore = UI.busy(btn, true, "删除中…");
    try {
      const json = await API.removeWatch(code);
      const codes = (json.data && json.data.codes) || [];
      Store.setWatchlistCodes(codes);
      if (Store.state.selectedSymbol === code) Store.state.selectedSymbol = null;
      Toast.ok("已移除 " + code);
      await this.loadQuotes(true);
    } catch (e) {
      Toast.err("删除失败：" + UI.apiErrorText(e));
      restore();
    }
  },

  /* ---------------- K 线 ---------------- */
  async loadKline(force, code) {
    const target = code || Store.state.selectedSymbol;
    if (!target) return;
    if (!force && target === Store.state.selectedSymbol && this.state.lastKlineCode === target) return;
    Store.setSelectedSymbol(target);
    const labelEl = this.container.querySelector("#mk-kline-label");
    const q = this.state.quotesMap[target] || {};
    labelEl.textContent = (q.name ? q.name + " " : "") + target + " · 近 " + this.state.klineDays + " 日";

    const tbody = this.container.querySelector("#mk-quotes-table tbody");
    tbody.querySelectorAll("tr").forEach((tr) => {
      tr.classList.toggle("selected", tr.dataset.code === target);
    });

    this.renderQuoteDetail(q);
    const chartEl = this.container.querySelector("#mk-kline-chart");
    UI.loading(chartEl, "K 线加载中…");
    try {
      const json = await API.kline(target, this.state.klineDays);
      this.state.lastKlineCode = target;
      Charts.kline("mk-kline-chart", json.data || []);
      requestAnimationFrame(() => Charts.resizeAll());
    } catch (e) {
      UI.setError(chartEl, { title: "K 线加载失败", message: UI.apiErrorText(e), retry: () => this.loadKline(true, target) });
    }
  },

  renderQuoteDetail(q) {
    const el = this.container.querySelector("#mk-quote-detail");
    if (!el) return;
    if (!q || !q.code) { el.innerHTML = ""; return; }
    const items = [
      ["现价", fmt.num(q.price, 2), fmt.color(q.change_pct)],
      ["涨跌额", fmt.signed(q.change, 2), fmt.color(q.change)],
      ["涨跌幅", fmt.pct(q.change_pct, true), fmt.color(q.change_pct)],
      ["昨收", fmt.num(q.prev_close, 2), "flat"],
      ["今开", q.open === undefined ? "—" : fmt.num(q.open, 2), "flat"],
      ["最高", q.high === undefined ? "—" : fmt.num(q.high, 2), "flat"],
      ["最低", q.low === undefined ? "—" : fmt.num(q.low, 2), "flat"],
      ["成交量(万手)", fmt.num(q.volume_wan, 2), "flat"],
      ["成交额(亿)", fmt.num(q.amount_yi, 2), "flat"],
      ["换手率", fmt.num(q.turnover_pct, 2) + "%", "flat"],
      ["PE(TTM)", q.pe_ttm === null || q.pe_ttm === undefined ? "—" : fmt.num(q.pe_ttm, 2), "flat"],
      ["PB", q.pb === null || q.pb === undefined ? "—" : fmt.num(q.pb, 2), "flat"],
      ["总市值(亿)", q.market_cap_yi === null || q.market_cap_yi === undefined ? "—" : fmt.num(q.market_cap_yi, 1), "flat"],
      ["数据时点", fmt.or(q.ts, "—"), "flat"],
    ];
    el.innerHTML = items.map(([k, v, cls]) =>
      `<div class="qd-item"><span class="k">${fmt.esc(k)}</span><span class="v ${cls}">${fmt.esc(v)}</span></div>`).join("");
  },
};

window.MarketView = MarketView;
