/*
 * views/backtest.js — 回测分析视图（真实回测）
 * 职责：
 *   1. 左侧策略列表（GET /api/strategies），点击切换回测标的
 *   2. 参数表单：起始/结束日期、初始资金、基准、标的池（取自自选池多选）、滑点、佣金费率
 *      → POST /api/backtest/<id>（GET /api/backtest/<id> 用于首屏默认参数结果）
 *   3. 结果区：16 项指标卡、净值 vs 基准、区间回撤、月度收益、权益曲线、期末持仓、交易记录
 *   4. warnings 提示条；表单草稿记忆在 Store.state.backtestForm，切走再切回不丢
 * 降级：接口失败只在结果区/表单内提示并保留已有内容，绝不清空整页
 * 对外接口：BacktestView.render(container) / refresh() / invalidate()
 */
"use strict";

const BacktestView = {
  id: "backtest",
  label: "回测分析",
  container: null,

  BENCHMARKS: [
    { value: "000300.SH", label: "沪深300（000300.SH）" },
    { value: "000905.SH", label: "中证500（000905.SH）" },
    { value: "000001.SH", label: "上证指数（000001.SH）" },
    { value: "399001.SZ", label: "深证成指（399001.SZ）" },
    { value: "399006.SZ", label: "创业板指（399006.SZ）" },
  ],

  state: {
    loadingStrategy: false,
    running: false,
    result: null,
    resultKey: null,
    tradesShown: 30,
    timer: null,
  },

  render(container) {
    this.container = container;
    Charts.dispose("bt-nav-chart");
    Charts.dispose("bt-dd-chart");
    Charts.dispose("bt-monthly-chart");
    Charts.dispose("bt-equity-chart");
    container.innerHTML = `
      <h2 class="view-title">回测分析 <span class="view-sub" id="bt-asof"></span></h2>
      <div class="backtest-layout">
        <aside class="panel strategy-list-panel">
          <div class="panel-title">
            <span>选择策略</span>
            <button type="button" class="btn btn-sm" id="bt-reload-strategies">刷新</button>
          </div>
          <div id="bt-strategy-list" class="bt-list"></div>
        </aside>

        <div class="backtest-main">
          <div class="panel">
            <div class="panel-title">
              <span>回测参数</span>
              <span class="panel-sub">留空字段由后端使用默认值</span>
            </div>
            <form id="bt-form" class="bt-form" novalidate>
              <div id="bt-form-fields"></div>
              <div class="field field-wide">
                <label class="field-label">标的池（来自自选池，多选；不选则由策略默认池决定）</label>
                <div id="bt-symbols" class="symbol-picker"></div>
              </div>
              <div class="btn-row bt-form-foot">
                <button type="submit" class="btn btn-primary" id="bt-run">运行回测</button>
                <button type="button" class="btn" id="bt-reset">恢复默认参数</button>
                <span id="bt-run-status" class="run-status" role="status"></span>
              </div>
            </form>
          </div>

          <div id="bt-warnings"></div>

          <div class="panel">
            <div class="panel-title">
              <span id="bt-name">回测结果</span>
              <span class="panel-sub" id="bt-range"></span>
            </div>
            <div id="bt-metrics" class="metric-grid metric-grid-fluid"></div>
          </div>

          <div class="two-col">
            <div class="panel">
              <div class="panel-title">净值曲线（策略 vs 基准）</div>
              <div id="bt-nav-chart" class="chart chart-md"></div>
            </div>
            <div class="panel">
              <div class="panel-title">区间回撤</div>
              <div id="bt-dd-chart" class="chart chart-md"></div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-title">权益曲线（元）</div>
            <div id="bt-equity-chart" class="chart chart-md"></div>
          </div>

          <div class="panel">
            <div class="panel-title">月度收益</div>
            <div id="bt-monthly-chart" class="chart chart-sm"></div>
          </div>

          <div class="panel">
            <div class="panel-title">
              <span>期末持仓</span>
              <span class="panel-sub" id="bt-pos-count"></span>
            </div>
            <div class="table-wrap">
              <table class="data-table" id="bt-positions-table">
                <thead>
                  <tr>
                    <th>代码</th><th>名称</th><th>行业</th>
                    <th class="num">数量</th><th class="num">可卖</th><th class="num">成本价</th><th class="num">现价</th>
                    <th class="num">市值(元)</th><th class="num">当日盈亏</th><th class="num">累计盈亏</th><th class="num">收益率</th>
                  </tr>
                </thead>
                <tbody><!-- JS --></tbody>
              </table>
            </div>
          </div>

          <div class="panel">
            <div class="panel-title">
              <span>交易记录 <span class="panel-sub" id="bt-trade-total"></span></span>
              <button type="button" class="btn btn-sm" id="bt-trades-toggle">显示全部</button>
            </div>
            <div class="table-wrap">
              <table class="data-table" id="bt-trades-table">
                <thead>
                  <tr>
                    <th>日期</th><th>代码</th><th>名称</th><th>方向</th>
                    <th class="num">价格</th><th class="num">数量</th><th class="num">金额</th>
                    <th class="num">费用</th><th class="num">盈亏(元)</th><th class="num">收益率</th><th>原因</th>
                  </tr>
                </thead>
                <tbody><!-- JS --></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>`;

    this.bind();
    return this.boot();
  },

  refresh() { return this.render(this.container); },

  /* 策略增删后调用：丢弃缓存结果并重新拉取列表 */
  invalidate() {
    this.state.result = null;
    this.state.resultKey = null;
    if (this.container && this.container.isConnected && this.container.classList.contains("active")) {
      this.boot();
    }
  },

  bind() {
    const c = this.container;
    c.querySelector("#bt-reload-strategies").addEventListener("click", () => this.boot());
    c.querySelector("#bt-form").addEventListener("submit", (e) => {
      e.preventDefault();
      this.run();
    });
    c.querySelector("#bt-reset").addEventListener("click", () => {
      Store.state.backtestForm = null;
      this.renderForm();
      Toast.info("参数已恢复为默认值");
    });
    c.querySelector("#bt-trades-toggle").addEventListener("click", (e) => {
      this.state.tradesShown = this.state.tradesShown === 30 ? Infinity : 30;
      e.target.textContent = this.state.tradesShown === 30 ? "显示全部" : "只看最近 30 条";
      this.renderTrades(this.state.result);
    });
  },

  /* ---------------- 策略列表 ---------------- */
  async boot() {
    const listEl = this.container.querySelector("#bt-strategy-list");
    UI.loading(listEl, "策略加载中…");
    try {
      const json = await API.strategies();
      Store.setStrategies(json.data || []);
    } catch (e) {
      UI.setError(listEl, { title: "策略列表加载失败", message: UI.apiErrorText(e), retry: () => this.boot() });
      return;
    }
    const list = Store.state.strategies;
    if (!list.length) {
      listEl.innerHTML = UI.emptyBlock({ title: "暂无可用策略", hint: "请到「策略管理」新建策略" });
      UI.setError(this.container.querySelector("#bt-metrics"), {
        title: "无法回测", message: "当前没有任何策略", retry: null,
      });
      return;
    }
    if (!Store.state.selectedStrategy || !list.some((s) => s.id === Store.state.selectedStrategy)) {
      Store.state.selectedStrategy = list[0].id;
    }
    listEl.innerHTML = list.map((s) => `
      <div class="bt-item ${s.id === Store.state.selectedStrategy ? "active" : ""}" data-id="${fmt.esc(s.id)}" role="button" tabindex="0">
        ${fmt.esc(s.name)}
        <small>${fmt.esc(s.category || "未分类")} · ${s.status === "running" ? "运行中" : "已暂停"}${
          s.builtin === false ? " · 自定义" : ""}</small>
      </div>`).join("");
    listEl.querySelectorAll(".bt-item").forEach((item) => {
      const go = () => this.selectStrategy(item.dataset.id);
      item.addEventListener("click", go);
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
    this.renderForm();
    /* 首屏用 GET 默认参数结果；已有缓存结果时直接重绘，避免重复计算 */
    if (this.state.result && this.state.resultKey === Store.state.selectedStrategy) {
      this.renderResult(this.state.result, null, true);
    } else {
      await this.loadDefault(Store.state.selectedStrategy);
    }
  },

  async selectStrategy(id) {
    if (!id || this.state.loadingStrategy) return;
    Store.state.selectedStrategy = id;
    this.container.querySelectorAll(".bt-item").forEach((item) =>
      item.classList.toggle("active", item.dataset.id === id));
    this.renderForm();
    this.state.result = null;
    this.state.resultKey = null;
    await this.loadDefault(id);
  },

  /* 未提交表单时：GET /api/backtest/<id> 拿默认参数结果 */
  async loadDefault(id) {
    const metricsEl = this.container.querySelector("#bt-metrics");
    UI.loading(metricsEl, "回测结果加载中…");
    this.container.querySelector("#bt-name").textContent = "回测结果";
    ["bt-nav-chart", "bt-dd-chart", "bt-monthly-chart", "bt-equity-chart"].forEach((cid) =>
      UI.loading(document.getElementById(cid), "加载中…"));
    try {
      const json = await API.backtest(id, {}, { timeout: 90000 });
      this.state.resultKey = id;
      this.renderResult(json, null, true);
    } catch (e) {
      this.renderResultError(e);
    }
  },

  /* ---------------- 参数表单 ---------------- */
  defaultForm() {
    const status = Store.state.status || {};
    const lastDay = (status.market && status.market.last_trading_day) || "";
    const end = lastDay || fmt.stamp(new Date()).slice(0, 10);
    let start = "";
    const startTime = fmt.parseTime(end);
    if (startTime !== null) {
      const d = new Date(startTime);
      d.setFullYear(d.getFullYear() - 2);
      start = fmt.stamp(d).slice(0, 10);
    }
    return {
      start: start,
      end: end,
      cash: Store.state.initialCash,
      benchmark: Store.state.benchmark,
      symbols: [],
      slippage_bps: 2,
      commission_wan: 2.5, /* 展示单位：万分之；提交时换算为 commission_rate */
      flow_fee: 0,
      slippage_ticks: 0,
    };
  },

  form() {
    return Store.state.backtestForm || this.defaultForm();
  },

  renderForm() {
    const c = this.container;
    const f = this.form();
    const s = Store.state.strategies.find((x) => x.id === Store.state.selectedStrategy) || {};
    c.querySelector("#bt-form-fields").innerHTML = `
      <div class="form-grid">
        ${UI.field({ id: "bt-start", name: "start", label: "起始日期", type: "date", value: f.start, hint: "包含该日" })}
        ${UI.field({ id: "bt-end", name: "end", label: "结束日期", type: "date", value: f.end, hint: "包含该日" })}
        ${UI.field({ id: "bt-cash", name: "cash", label: "初始资金（元）", type: "number", value: f.cash,
          min: 10000, step: 10000, hint: "最小 1 万，默认取系统配置" })}
        ${UI.field({ id: "bt-benchmark", name: "benchmark", label: "基准", type: "select",
          options: this.BENCHMARKS, value: f.benchmark })}
        ${UI.field({ id: "bt-slippage", name: "slippage_bps", label: "滑点（基点）", type: "number",
          value: f.slippage_bps, min: 0, max: 100, step: 0.5, hint: "双边计入成交价，1bp = 0.01%" })}
        ${UI.field({ id: "bt-commission", name: "commission_wan", label: "佣金费率（万分之）", type: "number",
          value: f.commission_wan, min: 0, max: 30, step: 0.05,
          hint: "提交值 commission_rate = " + fmt.esc(String(Number(f.commission_wan || 0) / 10000)) })}
        ${UI.field({ id: "bt-flow-fee", name: "flow_fee", label: "流量费（元/笔）", type: "number",
          value: f.flow_fee, min: 0, max: 100, step: 0.5,
          hint: "每笔委托固定费用，买卖各收一次；默认 0" })}
        ${UI.field({ id: "bt-ticks", name: "slippage_ticks", label: "滑点（跳数）", type: "number",
          value: f.slippage_ticks, min: 0, max: 100, step: 1,
          hint: "按最小变动价位（0.01 元）的跳数叠加，取对买方不利方向" })}
      </div>
      <div class="form-meta">
        策略：<strong>${fmt.esc(s.name || "—")}</strong>
        · 最少 bar：${fmt.esc(s.min_bars === undefined ? "—" : s.min_bars)}
        · 标的池类型：${fmt.esc(fmt.universeLabel(s.universe_type))}
        · 默认池：${fmt.esc(s.universe || "—")}
      </div>`;
    this.renderSymbolPicker();
    /* 表单变更写入草稿 */
    c.querySelectorAll("#bt-form-fields input, #bt-form-fields select").forEach((el) => {
      el.addEventListener("change", () => this.saveForm());
      el.addEventListener("input", () => this.saveForm());
    });
  },

  saveForm() {
    const c = this.container;
    const f = this.form();
    const read = (sel, asNumber) => {
      const el = c.querySelector(sel);
      if (!el) return undefined;
      return asNumber ? (el.value === "" ? null : Number(el.value)) : el.value;
    };
    f.start = read("#bt-start");
    f.end = read("#bt-end");
    f.cash = read("#bt-cash", true);
    f.benchmark = read("#bt-benchmark");
    f.slippage_bps = read("#bt-slippage", true);
    f.commission_wan = read("#bt-commission", true);
    f.flow_fee = read("#bt-flow-fee", true);
    f.slippage_ticks = read("#bt-ticks", true);
    Store.state.backtestForm = f;
    /* 佣金提示实时联动 */
    const hint = c.querySelector("#bt-commission")?.parentElement.querySelector(".field-hint");
    if (hint) hint.textContent = "提交值 commission_rate = " + String(Number(f.commission_wan || 0) / 10000);
  },

  renderSymbolPicker() {
    const el = this.container.querySelector("#bt-symbols");
    const wl = Store.state.watchlist;
    const chosen = this.form().symbols || [];
    const items = wl.items || [];
    if (!wl.supported) {
      el.innerHTML = `<div class="inline-msg inline-msg-warn">自选池接口不可用，改为手动输入代码</div>
        <input type="text" id="bt-symbols-manual" class="input-inline input-wide"
          placeholder="600519.SH,300750.SZ（逗号分隔，留空用默认池）"
          value="${fmt.esc((chosen || []).join(","))}" aria-label="标的池代码">`;
      el.querySelector("#bt-symbols-manual").addEventListener("change", (e) => {
        this.form().symbols = String(e.target.value || "").split(/[,，\s]+/).filter(Boolean);
      });
      return;
    }
    if (!items.length) {
      el.innerHTML = UI.notice({
        level: "info",
        text: "自选池为空，本次回测将使用策略默认标的池（" + fmt.or(Store.state.strategies.find((x) => x.id === Store.state.selectedStrategy)?.universe, "—") + "）。可先到「行情看板」添加自选。",
      });
      return;
    }
    el.innerHTML = `
      <div class="symbol-head">
        <span class="flat">已选 <strong id="bt-sym-count">${chosen.length}</strong> / ${items.length} 只</span>
        <span class="btn-row">
          <button type="button" class="btn btn-sm" data-sym-all>全选</button>
          <button type="button" class="btn btn-sm" data-sym-none>清空</button>
        </span>
      </div>
      <div class="symbol-list">
        ${items.map((q) => `
          <label class="symbol-item">
            <input type="checkbox" data-sym="${fmt.esc(q.code)}" ${chosen.indexOf(q.code) >= 0 ? "checked" : ""}>
            <span>${fmt.esc(q.code)} <span class="flat">${fmt.esc(q.name)}</span></span>
            <span class="${fmt.color(q.change_pct)}">${fmt.pct(q.change_pct, true)}</span>
          </label>`).join("")}
      </div>`;
    const sync = () => {
      const list = Array.from(el.querySelectorAll("[data-sym]:checked")).map((x) => x.dataset.sym);
      this.form().symbols = list;
      el.querySelector("#bt-sym-count").textContent = String(list.length);
    };
    el.querySelectorAll("[data-sym]").forEach((x) => x.addEventListener("change", sync));
    el.querySelector("[data-sym-all]").addEventListener("click", () => {
      el.querySelectorAll("[data-sym]").forEach((x) => { x.checked = true; });
      sync();
    });
    el.querySelector("[data-sym-none]").addEventListener("click", () => {
      el.querySelectorAll("[data-sym]").forEach((x) => { x.checked = false; });
      sync();
    });
  },

  /* ---------------- 运行回测 ---------------- */
  validateForm() {
    const f = this.form();
    if (!f.start || !f.end) return { field: "#bt-start", message: "请选择完整的回测起止日期" };
    if (f.start > f.end) return { field: "#bt-start", message: "起始日期不能晚于结束日期" };
    if (!f.cash || f.cash < 10000) return { field: "#bt-cash", message: "初始资金至少 1 万元" };
    if (f.slippage_bps === null || f.slippage_bps < 0) return { field: "#bt-slippage", message: "滑点不能为负" };
    if (f.commission_wan === null || f.commission_wan < 0) return { field: "#bt-commission", message: "佣金费率不能为负" };
    if (f.flow_fee === null || f.flow_fee < 0) return { field: "#bt-flow-fee", message: "流量费不能为负" };
    if (f.slippage_ticks === null || f.slippage_ticks < 0) return { field: "#bt-ticks", message: "滑点跳数不能为负" };
    const max = 20; /* 与后端 symbols 上限保持一致的前端前置校验 */
    if ((f.symbols || []).length > max) {
      return { field: "#bt-symbols", message: "标的池最多 " + max + " 只，请减少选择" };
    }
    return null;
  },

  async run() {
    if (this.state.running) return;
    const bad = this.validateForm();
    if (bad) {
      Toast.err(bad.message);
      const el = this.container.querySelector(bad.field);
      if (el) el.focus();
      return;
    }
    const id = Store.state.selectedStrategy;
    if (!id) { Toast.err("请先选择策略"); return; }
    const f = this.form();
    const btn = this.container.querySelector("#bt-run");
    const statusEl = this.container.querySelector("#bt-run-status");
    const restore = UI.busy(btn, true, "回测计算中…");
    this.state.running = true;
    const startedAt = Date.now();
    statusEl.textContent = "回测计算中… 0s（真实逐日回测，耗时取决于区间与标的数量）";
    this.state.timer = setInterval(() => {
      statusEl.textContent = "回测计算中… " + Math.round((Date.now() - startedAt) / 1000) + "s";
    }, 1000);
    const resultEl = this.container.querySelector("#bt-metrics");
    resultEl.classList.add("is-loading");
    try {
      const params = {
        start: f.start, end: f.end, cash: f.cash, benchmark: f.benchmark,
        symbols: f.symbols || [], slippage_bps: f.slippage_bps,
        flow_fee: f.flow_fee, slippage_ticks: f.slippage_ticks,
        commission_rate: Number(f.commission_wan || 0) / 10000,
      };
      const json = await API.runBacktest(id, params, { timeout: 120000 });
      this.state.resultKey = id;
      this.renderResult(json, params, false);
      Toast.ok("回测完成，区间 " + ((json.data && json.data.range) ? json.data.range.start + " ~ " + json.data.range.end : ""));
    } catch (e) {
      this.renderResultError(e, params);
      Toast.err("回测失败：" + UI.apiErrorText(e));
    } finally {
      clearInterval(this.state.timer);
      this.state.timer = null;
      this.state.running = false;
      statusEl.textContent = "上次耗时 " + Math.round((Date.now() - startedAt) / 1000) + "s";
      resultEl.classList.remove("is-loading");
      restore();
    }
  },

  /* ---------------- 结果渲染 ---------------- */
  renderResult(json, submitted, fromCache) {
    const d = (json && json.data) || {};
    if (!d.metrics) {
      this.renderResultError(new ApiError("后端未返回 data.metrics", { status: 500 }), submitted);
      return;
    }
    this.state.result = json;
    this.state.tradesShown = 30;
    const m = d.metrics;
    const s = d.strategy || {};
    const c = this.container;

    c.querySelector("#bt-asof").textContent = json.as_of ? "数据时点 " + json.as_of : "";
    c.querySelector("#bt-name").textContent = (s.name || "回测结果") + " · 回测结果" + (fromCache ? "（缓存视图）" : "");
    const req = d.request || {};
    const range = d.range || {};
    c.querySelector("#bt-range").textContent = [
      (range.start || req.start || "—") + " ~ " + (range.end || req.end || "—"),
      fmt.fixed(m.trading_days, 0) + " 个交易日",
      "基准 " + (req.benchmark || ""),
      "初始资金 " + fmt.num(req.initial_cash, 0) + " 元",
      "费率 " + fmt.num((req.fee && req.fee.commission_rate) !== undefined
        ? req.fee.commission_rate * 10000 : undefined, 2) + "‱",
      (req.fee && Number(req.fee.flow_fee)) ? "流量费 " + fmt.num(req.fee.flow_fee, 2) + " 元/笔" : "",
      (req.fee && Number(req.fee.slippage_ticks)) ? "跳数滑点 " + fmt.num(req.fee.slippage_ticks, 0) + " 跳" : "",
    ].filter(Boolean).join(" · ");

    /* warnings */
    const warnEl = c.querySelector("#bt-warnings");
    const warnings = (d.warnings || []).filter(Boolean);
    warnEl.innerHTML = warnings.length ? UI.notice({
      level: "warn", title: "本次回测有以下提示（数据或参数限制）", items: warnings,
    }) : "";

    /* 指标卡 */
    const ddWindow = (m.max_drawdown_start || m.max_drawdown_end)
      ? m.max_drawdown_start + " → " + m.max_drawdown_end : "";
    const cards = [
      { k: "累计收益", v: fmt.pct(m.total_return_pct, true), c: fmt.color(m.total_return_pct) },
      { k: "年化收益", v: fmt.pct(m.annual_return_pct, true), c: fmt.color(m.annual_return_pct) },
      { k: "最大回撤", v: fmt.pct(-Math.abs(m.max_drawdown_pct), false), c: "down", s: ddWindow },
      { k: "夏普比率", v: fmt.fixed(m.sharpe, 2), c: fmt.color(m.sharpe) },
      { k: "索提诺比率", v: fmt.fixed(m.sortino, 2), c: fmt.color(m.sortino) },
      { k: "卡玛比率", v: fmt.fixed(m.calmar, 2), c: fmt.color(m.calmar) },
      { k: "年化波动", v: fmt.pct(m.volatility_pct, false), c: "flat" },
      { k: "胜率", v: fmt.pct(m.win_rate_pct, false), c: "flat" },
      { k: "交易次数", v: fmt.int(m.trade_count), c: "flat", s: d.trade_total !== undefined ? "总成交 " + fmt.int(d.trade_total) : "" },
      { k: "盈亏比", v: fmt.fixed(m.profit_loss_ratio, 2), c: "flat" },
      { k: "换手率", v: fmt.pct(m.turnover_pct, false), c: "flat" },
      { k: "基准收益", v: fmt.pct(m.benchmark_return_pct, true), c: fmt.color(m.benchmark_return_pct) },
      { k: "Alpha", v: fmt.pct(m.alpha_pct, true), c: fmt.color(m.alpha_pct) },
      { k: "Beta", v: fmt.fixed(m.beta, 3), c: "flat" },
      { k: "总费用(元)", v: fmt.num(m.total_fee, 2), c: "flat" },
      { k: "期末持仓", v: fmt.int((d.positions || []).length) + " 只", c: "flat",
        s: "交易日 " + fmt.int(m.trading_days) },
    ];
    c.querySelector("#bt-metrics").innerHTML = cards.map((x) => `
      <div class="metric-card">
        <div class="k">${fmt.esc(x.k)}</div>
        <div class="v ${x.c}">${fmt.esc(x.v)}</div>
        ${x.s ? `<div class="s flat">${fmt.esc(x.s)}</div>` : ""}
      </div>`).join("");

    /* 图表 */
    const nav = d.nav || [];
    Charts.nav("bt-nav-chart", nav.map((x) => x.date), nav.map((x) => x.strategy), nav.map((x) => x.benchmark));
    Charts.drawdown("bt-dd-chart", (d.drawdown || []).map((x) => x.date), (d.drawdown || []).map((x) => x.dd_pct));
    Charts.monthly("bt-monthly-chart", (d.monthly || []).map((x) => x.month), (d.monthly || []).map((x) => x.ret_pct));
    const eq = d.equity || [];
    Charts.equity("bt-equity-chart", eq.map((x) => x.date), eq.map((x) => x.equity));

    this.renderPositions(d.positions || []);
    this.renderTrades(json);
    requestAnimationFrame(() => Charts.resizeAll());
  },

  renderPositions(positions) {
    const c = this.container;
    c.querySelector("#bt-pos-count").textContent =
      positions.length ? positions.length + " 只" : "";
    UI.tbody(c.querySelector("#bt-positions-table tbody"), positions.map((p) => `
      <tr>
        <td>${fmt.esc(p.code)}</td>
        <td>${fmt.esc(p.name)}</td>
        <td class="flat">${fmt.esc(fmt.or(p.industry, "—"))}</td>
        <td class="num">${fmt.int(p.qty)}</td>
        <td class="num flat">${fmt.int(p.available_qty)}</td>
        <td class="num">${fmt.num(p.cost, 2)}</td>
        <td class="num">${fmt.num(p.price, 2)}</td>
        <td class="num">${fmt.num(p.market_value, 2)}</td>
        <td class="num ${fmt.color(p.day_pnl)}">${fmt.signed(p.day_pnl, 2)}</td>
        <td class="num ${fmt.color(p.total_pnl)}">${fmt.signed(p.total_pnl, 2)}</td>
        <td class="num ${fmt.color(p.return_pct)}">${fmt.pct(p.return_pct, true)}</td>
      </tr>`), { colspan: 11, empty: "期末无持仓", emptyHint: "回测区间结束时策略为空仓" });
  },

  renderTrades(json) {
    if (!json || !json.data) return;
    const trades = json.data.trades || [];
    const c = this.container;
    const total = json.data.trade_total !== undefined ? json.data.trade_total : trades.length;
    c.querySelector("#bt-trade-total").textContent = total ? "共 " + total + " 笔" : "";
    const shown = this.state.tradesShown === Infinity ? trades : trades.slice(0, this.state.tradesShown);
    const toggle = c.querySelector("#bt-trades-toggle");
    toggle.hidden = trades.length <= 30;
    UI.tbody(c.querySelector("#bt-trades-table tbody"), shown.map((t) => {
      const isBuy = t.side === "buy";
      const hasPnl = t.pnl !== null && t.pnl !== undefined;
      return `
      <tr>
        <td>${fmt.esc(t.date)}</td>
        <td>${fmt.esc(t.code)}</td>
        <td>${fmt.esc(t.name)}</td>
        <td><span class="badge ${isBuy ? "badge-buy" : "badge-sell"}">${isBuy ? "买入" : "卖出"}</span></td>
        <td class="num">${fmt.num(t.price, 2)}</td>
        <td class="num">${fmt.int(t.qty)}</td>
        <td class="num flat">${fmt.num(t.amount, 2)}</td>
        <td class="num flat">${fmt.num(t.fee, 2)}</td>
        <td class="num ${hasPnl ? fmt.color(t.pnl) : "flat"}">${hasPnl ? fmt.signed(t.pnl, 2) : "—"}</td>
        <td class="num ${hasPnl ? fmt.color(t.ret_pct) : "flat"}">${
          t.ret_pct === null || t.ret_pct === undefined ? "—" : fmt.pct(t.ret_pct, true)}</td>
        <td class="flat">${fmt.esc(fmt.or(t.reason, "—"))}</td>
      </tr>`;
    }), { colspan: 11, empty: "回测区间内没有成交", emptyHint: "可放宽条件或延长区间后重跑" });
    const label = this.state.tradesShown === Infinity ? "只看最近 30 条" : "显示全部";
    toggle.textContent = label;
  },

  renderResultError(e, params) {
    const c = this.container;
    const msg = UI.apiErrorText(e);
    const unimplemented = e instanceof ApiError && e.unimplemented;
    const metricsEl = c.querySelector("#bt-metrics");
    const hint = unimplemented
      ? "该接口在当前后端版本未实现（契约：GET/POST /api/backtest/<id>）。后端就绪后点击重试即可，无需改动前端。"
      : "回测计算失败或参数不合法，可在上方表单调整参数后重试。";
    UI.setError(metricsEl, {
      title: "回测结果加载失败", message: msg, hint: hint, retry: () => this.loadDefault(Store.state.selectedStrategy),
    });
    ["bt-nav-chart", "bt-dd-chart", "bt-monthly-chart", "bt-equity-chart"].forEach((id) =>
      Charts.placeholder(id, "无结果"));
    c.querySelector("#bt-warnings").innerHTML = UI.notice({
      level: "warn", title: "回测未完成",
      text: params
        ? "已提交参数：" + JSON.stringify(params)
        : "默认参数回测失败。",
    });
    this.renderPositions([]);
    UI.tbody(c.querySelector("#bt-trades-table tbody"), [], { colspan: 11, empty: "无可展示的成交记录" });
  },
};

window.BacktestView = BacktestView;
