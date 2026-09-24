/*
 * views/level2.js — 盘口 / L2 视图（公开源近似的五档 · 逐笔 · 资金流分档 + 4 个 L2 工具）
 * 职责：
 *   1. 代码输入 → 查询：GET /api/level2/<code>/orderbook | /ticks?limit=60 | /flow?limit=2000
 *   2. 指标卡：现价 / 涨跌幅 / 委比 / 委差 / 外盘 / 内盘
 *   3. 五档表（卖 5 → 卖 1 → 买 1 → 买 5，卖绿买红；量额并列）
 *   4. 逐笔表（时间 / 价格 / 手数 / 金额 / 方向，买红卖绿，最多 60 行）
 *   5. 资金流卡（超大单 / 大单 / 中单 / 小单 的净额与买入占比 + 主力净额与占比）
 *   6. L2 工具（只在手动点击时请求，3 秒自动刷新不触发）：
 *      大单追踪 /big-orders · 资金流分时 /flow-series · 封板状态 /seal · 扫描与排行 /scan + /flow-rank
 *   7. 自动刷新（默认关，3 秒）：仅在本页可见且已填代码时发请求，只刷新盘口 / 逐笔 / 资金流
 * 数据边界（页面上必须如实标注，不含糊）：
 *   - 五档 / 外盘 / 内盘是公开快照，不是推送；
 *   - 逐笔方向是第三方「盘口方向标记」，不是交易所 Level-2 的主动买卖判定；
 *   - 大单 / 分时 / 排行都由逐笔样本自算（约最近 4000 笔），是当日至今的近似；
 *   - 封板只按当前快照判断此刻状态，不承诺开板次数；
 *   - 十档 / 逐笔委托 / 委托队列没有公开来源，需付费授权。
 * 降级：各接口失败只在对应区块内画空状态（带原因 + 重试），绝不清空整页、不弹窗；
 *       离线（后端不可达 / meta.offline）时所有区块都画空状态并说明原因。
 * 对外接口：Level2View.render(container) / refresh()
 */
"use strict";

const LEVEL_TICK_LIMIT = 60;      /* 逐笔最多展示 60 行（与桌面版一致） */
const LEVEL_FLOW_LIMIT = 2000;    /* 资金流样本笔数（契约默认） */
const LEVEL_AUTO_MS = 3000;       /* 自动刷新间隔（默认关） */
const LEVEL_DISCLAIMER = "五档为公开源快照，逐笔方向为第三方盘口标记（非交易所 Level-2）；" +
  "十档/逐笔委托/委托队列需付费授权";
const LEVEL_FLOW_NOTE = "口径：净额 = 买入额 − 卖出额，按单笔成交额分档自算" +
  "（≥100 万 超大单 / 20–100 万 大单 / 5–20 万 中单 / <5 万 小单）；主力 = 超大单 + 大单";

/* ---- L2 工具（全部手动触发，不进自动刷新）---- */
const LEVEL_BIG_THRESHOLDS = [
  { value: 200000, label: "20 万" },
  { value: 500000, label: "50 万" },
  { value: 1000000, label: "100 万" },
  { value: 2000000, label: "200 万" },
];
const LEVEL_BIG_DEFAULT = 1000000;      /* 默认阈值 100 万（元） */
const LEVEL_BIG_LIMIT = 50;             /* 大单表最多 50 行（契约默认） */
const LEVEL_SERIES_LIMIT = 2000;        /* 分时聚合的逐笔样本上限（契约默认） */
const LEVEL_SCAN_LIMIT = 10;            /* 扫描标的数上限（后端上限也是 10） */
const LEVEL_RANK_TOP = 10;              /* 排行标的数上限 */
const LEVEL_RANK_LIMIT = 1000;          /* 排行每只标的的逐笔样本上限（契约默认） */
const LEVEL_BIG_COST = "预计 3–10 秒（单只标的拉取逐笔后筛选）";
const LEVEL_SERIES_COST = "预计 3–10 秒（单只标的拉取逐笔后按分钟聚合）";
const LEVEL_SCAN_COST = "每只标的约 1 秒（最多 10 只）";
const LEVEL_RANK_COST = "每只标的 10–25 秒，进行中最多约 2–4 分钟（最多 10 只）";
const LEVEL_BIG_NOTE = "口径：单笔成交额 ≥ 阈值的成交（买 / 卖 / 中性）；方向为第三方盘口标记，" +
  "样本约覆盖最近 4000 笔 → 当日至今的近似。";
const LEVEL_SERIES_NOTE = "口径：每分钟净额 = 该分钟主动买 − 主动卖（第三方方向标记）；" +
  "曲线为累计净额（万元），只覆盖逐笔样本（最近约 4000 笔）。";
const LEVEL_SEAL_NOTE = "口径：只按当前快照判断此刻是否封板；开板次数需要盘中多次采样，" +
  "不在单次调用里给出。";

/* 与桌面版同一套显示规则：手数用整数、金额换算成万元、方向标记译成中文 */
const LEVEL_SIDE_LABELS = { buy: "买", sell: "卖", neutral: "中性", b: "买", s: "卖", m: "中性" };

function levelNum(value) {
  const n = Number(value);
  return isFinite(n) ? n : null;
}

function levelWan(value, digits) {
  const n = levelNum(value);
  return n === null ? "—" : fmt.num(n / 10000, digits === undefined ? 2 : digits);
}

/* 缺失值判定：Number(null) === 0、Number("") === 0，不能直接交给 Number() */
function levelMissing(value) {
  return value === null || value === undefined || String(value).trim() === "";
}

/* 可能缺失的百分比：null / undefined / "" → "—"（fmt.pct 会把 null 当成 0） */
function levelPct(value, withSign) {
  if (levelMissing(value)) return "—";
  const n = levelNum(value);
  return n === null ? "—" : fmt.pct(n, withSign);
}

/* 可能缺失的数字：null / undefined / "" → "—"（fmt.num / fmt.int 会把 null 当成 0） */
function levelFixed(value, digits) {
  if (levelMissing(value)) return "—";
  const n = levelNum(value);
  return n === null ? "—" : fmt.num(n, digits);
}

function levelInt(value) {
  if (levelMissing(value)) return "—";
  const n = levelNum(value);
  return n === null ? "—" : fmt.int(n);
}

function levelChangePct(price, prevClose) {
  const p = levelNum(price);
  const q = levelNum(prevClose);
  return (p === null || !q) ? null : (p - q) / q * 100;
}

function levelSideLabel(value) {
  const key = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
  return LEVEL_SIDE_LABELS[key] || (key ? String(value) : "—");
}

function levelSideClass(value) {
  const label = levelSideLabel(value);
  return label === "买" ? "up" : label === "卖" ? "down" : "flat";
}

const Level2View = {
  id: "level2",
  label: "盘口 / L2",
  container: null,
  state: {
    code: null,                   /* 最近一次查询的代码 */
    auto: false,                  /* 自动刷新开关（默认关） */
    timer: null,                  /* 自动刷新的 setInterval id */
    loading: false,
    errors: [],                   /* 本轮三个接口的失败原因（页面顶部统一提示） */
    bigThreshold: LEVEL_BIG_DEFAULT,   /* 大单阈值（元），默认 100 万 */
    toolBusy: { big: false, series: false, seal: false, scan: false, rank: false },
  },

  render(container) {
    this.container = container;
    this.clearTimer();
    /* 重绘前销毁旧图表实例：图表容器 id 固定，避免旧实例残留在 Charts._instances */
    Charts.dispose("l2-series-chart");
    container.innerHTML = `
      <h2 class="view-title">盘口 / L2 <span class="view-sub" id="l2-asof"></span></h2>

      <div class="panel-title">
        <span class="panel-sub">${fmt.esc(LEVEL_DISCLAIMER)}</span>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>查询</span>
          <span class="panel-sub">支持 600519 / sh600519 / 600519.SH</span>
        </div>
        <div class="toolbar">
          <label class="inline-label" for="l2-code">标的</label>
          <input type="text" id="l2-code" class="input-inline" autocomplete="off"
            placeholder="600519 / sh600519 / 600519.SH" aria-label="标的代码"
            value="${fmt.esc(this.state.code || "")}">
          <button type="button" class="btn btn-sm btn-primary" id="l2-query">查询</button>
          <label class="check-line">
            <input type="checkbox" id="l2-auto" ${this.state.auto ? "checked" : ""}>
            自动刷新（3 秒）
          </label>
        </div>
        <div id="l2-msg" class="inline-msg" role="status" hidden></div>
      </div>

      <div id="l2-metrics" class="metric-grid metric-grid-fluid"></div>

      <div class="panel">
        <div class="panel-title">
          <span>五档盘口</span>
          <span class="panel-sub" id="l2-level-sub">卖 5 → 卖 1 → 买 1 → 买 5（卖绿买红）</span>
        </div>
        <div class="table-wrap">
          <table class="data-table" id="l2-level-table">
            <thead>
              <tr>
                <th>档位</th><th class="num">价格</th><th class="num">手数</th><th class="num">金额(万元)</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>逐笔成交</span>
          <span class="panel-sub" id="l2-tick-sub">最多展示最近 ${LEVEL_TICK_LIMIT} 笔；方向为第三方盘口标记</span>
        </div>
        <div class="table-wrap table-scroll">
          <table class="data-table" id="l2-tick-table">
            <thead>
              <tr>
                <th>时间</th><th class="num">价格</th><th class="num">手数</th>
                <th class="num">金额(万元)</th><th>方向</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>资金流（按单笔成交额分档）</span>
          <span class="panel-sub" id="l2-flow-sub"></span>
        </div>
        <div id="l2-flow-cards" class="metric-grid metric-grid-fluid"></div>
        <div class="table-wrap">
          <table class="data-table" id="l2-flow-table">
            <thead>
              <tr>
                <th>档位</th><th class="num">买入(万元)</th><th class="num">卖出(万元)</th>
                <th class="num">净额(万元)</th><th class="num">买入占比</th><th class="num">笔数</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
        <div class="panel-sub">${fmt.esc(LEVEL_FLOW_NOTE)}</div>

      <div class="panel">
        <div class="panel-title">
          <span>大单追踪 <span class="panel-sub" id="l2-big-sub"></span></span>
          <span class="toolbar">
            <label class="inline-label" for="l2-big-threshold">阈值</label>
            <select id="l2-big-threshold" class="input-inline">
              ${LEVEL_BIG_THRESHOLDS.map((t) => `<option value="${t.value}"${t.value === this.state.bigThreshold ? " selected" : ""}>${fmt.esc(t.label)}</option>`).join("")}
            </select>
            <button type="button" class="btn btn-sm btn-primary" id="l2-big-run">查大单</button>
          </span>
        </div>
        <div id="l2-big-stats" class="metric-grid metric-grid-fluid"></div>
        <div class="table-wrap table-scroll">
          <table class="data-table" id="l2-big-table">
            <thead>
              <tr>
                <th>时间</th><th class="num">价格</th><th class="num">手数</th>
                <th class="num">金额(万元)</th><th>方向</th><th>档位</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
        <div class="panel-sub">${fmt.esc(LEVEL_BIG_COST)} · ${fmt.esc(LEVEL_BIG_NOTE)}</div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>资金流分时（累计净额） <span class="panel-sub" id="l2-series-sub"></span></span>
          <span class="toolbar">
            <button type="button" class="btn btn-sm btn-primary" id="l2-series-run">查分时</button>
          </span>
        </div>
        <div id="l2-series-metrics" class="metric-grid metric-grid-fluid"></div>
        <div id="l2-series-state"></div>
        <div id="l2-series-chart" class="chart chart-md"></div>
        <div class="table-wrap">
          <table class="data-table" id="l2-series-buckets">
            <thead>
              <tr>
                <th>档位</th><th class="num">净额(万元)</th><th class="num">买入占比</th><th class="num">笔数</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
        <div class="panel-sub">${fmt.esc(LEVEL_SERIES_COST)} · ${fmt.esc(LEVEL_SERIES_NOTE)}</div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>封板状态 <span class="panel-sub" id="l2-seal-sub"></span></span>
          <span class="toolbar">
            <button type="button" class="btn btn-sm btn-primary" id="l2-seal-run">查封板</button>
          </span>
        </div>
        <div id="l2-seal-body"></div>
        <div class="panel-sub">${fmt.esc(LEVEL_SEAL_NOTE)}</div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>扫描与排行 <span class="panel-sub" id="l2-tools-sub">扫描自选池 / 资金流排行</span></span>
          <span class="toolbar">
            <button type="button" class="btn btn-sm btn-primary" id="l2-scan-run">扫描自选池</button>
            <button type="button" class="btn btn-sm btn-primary" id="l2-rank-run">资金流排行</button>
          </span>
        </div>
        <div id="l2-scan-status" class="panel-sub"></div>
        <div class="table-wrap">
          <table class="data-table" id="l2-scan-table">
            <thead>
              <tr>
                <th>代码</th><th>名称</th><th class="num">现价</th><th class="num">涨跌幅</th>
                <th class="num">委比</th><th class="num">委差比</th><th class="num">价差</th><th class="num">量比</th>
                <th>封板</th><th class="num">封单额(万元)</th><th class="num">距涨停</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
        <div id="l2-rank-status" class="panel-sub"></div>
        <div class="table-wrap">
          <table class="data-table" id="l2-rank-table">
            <thead>
              <tr>
                <th>代码</th><th>名称</th><th class="num">主力净额(万元)</th><th class="num">主力净额占比</th>
                <th class="num">净额(万元)</th><th class="num">成交额(万元)</th><th class="num">逐笔笔数</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
        <div class="panel-sub">${fmt.esc(LEVEL_SCAN_COST)} · ${fmt.esc(LEVEL_RANK_COST)}</div>
      </div>
      </div>`;

    this.bind();
    if (this.state.auto) this.setAuto(true, true);   /* 重绘后保持开关状态（不重复提示） */
    this.renderToolsIdle();           /* 4 个工具先画「尚未查询 / 点按钮开始」空状态 */
    return this.load();
  },

  refresh() { return this.render(this.container); },

  bind() {
    const c = this.container;
    const input = c.querySelector("#l2-code");
    c.querySelector("#l2-query").addEventListener("click", () => this.query());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); this.query(); }
    });
    input.addEventListener("input", () => this.msg(""));
    c.querySelector("#l2-auto").addEventListener("change", (e) => this.setAuto(e.target.checked));
    /* L2 工具：只在点击时请求（绝不进 3 秒自动刷新） */
    c.querySelector("#l2-big-threshold").addEventListener("change", (e) => {
      const next = levelNum(e.target.value);
      this.state.bigThreshold = next && next > 0 ? next : LEVEL_BIG_DEFAULT;
    });
    c.querySelector("#l2-big-run").addEventListener("click", () => this.loadBigOrders());
    c.querySelector("#l2-series-run").addEventListener("click", () => this.loadFlowSeries());
    c.querySelector("#l2-seal-run").addEventListener("click", () => this.loadSeal());
    c.querySelector("#l2-scan-run").addEventListener("click", () => this.runScan());
    c.querySelector("#l2-rank-run").addEventListener("click", () => this.runFlowRank());
  },

  /* ---------------- 提示与自动刷新 ---------------- */

  msg(text, type) {
    const el = this.container ? this.container.querySelector("#l2-msg") : null;
    if (!el) return;
    if (!text) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.className = "inline-msg inline-msg-" + (type || "info");
    el.textContent = text;
  },

  setAuto(on, silent) {
    this.clearTimer();
    this.state.auto = !!on;
    if (on) {
      this.state.timer = setInterval(() => this.tick(), LEVEL_AUTO_MS);
      if (!silent) {
        this.msg("自动刷新已开启：每 " + Math.round(LEVEL_AUTO_MS / 1000) +
          " 秒重新查询（切到其它标签页时不发请求）", "ok");
      }
    } else if (!silent) {
      this.msg("自动刷新已关闭", "info");
    }
  },

  clearTimer() {
    if (this.state.timer) {
      clearInterval(this.state.timer);
      this.state.timer = null;
    }
  },

  tick() {
    if (!this.state.auto) return;                  /* 开关关闭 → 绝不发请求 */
    if (Store.state.view !== this.id) return;      /* 不在本页 → 不发请求 */
    if (!this.state.code) return;
    this.load();
  },

  /* ---------------- 查询 ---------------- */

  query() {
    const input = this.container.querySelector("#l2-code");
    const check = UI.normalizeSymbol(input.value);
    if (!check.ok) {
      this.msg(check.message, "err");
      input.focus();
      return Promise.resolve();
    }
    this.state.code = check.code;
    return this.load();
  },

  async load() {
    const code = this.state.code;
    if (!code) { this.idle(); return; }

    const offline = this.offlineReason();
    if (offline) { this.showOffline(offline); return; }

    if (this.state.loading) return;                /* 防止自动刷新叠加请求 */
    this.state.loading = true;
    this.state.errors = [];                        /* 三个接口各自记录失败原因 */
    this.msg("正在查询 " + code + " 的盘口 / 逐笔 / 资金流…", "info");
    const restore = UI.busy(this.container.querySelector("#l2-query"), true, "查询中…");
    try {
      await Promise.all([this.loadOrderbook(code), this.loadTicks(code), this.loadFlow(code)]);
    } finally {
      this.state.loading = false;
      restore();
      /* 三个区块都成功后清掉「正在查询…」，否则把失败原因留在页面上方 */
      if (this.state.errors.length) {
        this.msg(this.state.errors.join("；"), "err");
      } else {
        this.msg("");
      }
    }
  },

  /* 离线判定：后端不可达 / 响应 meta.offline / 系统状态自报 offline */
  offlineReason() {
    const st = Store.state;
    const meta = st.lastMeta || {};
    const backend = st.backend || {};
    const status = st.status || {};
    if (backend.checked && !backend.reachable) {
      return "后端未连接：" + fmt.or(backend.error, "无法访问 /api/health");
    }
    if (meta.offline) return "响应 meta.offline = true（来源：" + fmt.or(meta.source, "本地示例") + "）";
    if (status.offline) return "后端 /api/system/status 自报 offline";
    if (st.level === "offline" && backend.checked && !backend.ok) {
      return "后端已连接但未接入真实行情";
    }
    return "";
  },

  /* ---------------- 空状态 ---------------- */

  idle() {
    this.setAsOf("");
    UI.tbody(this.container.querySelector("#l2-level-table tbody"), [], {
      colspan: 4, empty: "尚未查询", emptyHint: "输入标的代码（例如 600519 / sh600519 / 600519.SH）后点「查询」",
    });
    UI.tbody(this.container.querySelector("#l2-tick-table tbody"), [], {
      colspan: 5, empty: "尚未查询", emptyHint: "逐笔方向为第三方盘口标记，最多展示最近 " + LEVEL_TICK_LIMIT + " 笔",
    });
    UI.tbody(this.container.querySelector("#l2-flow-table tbody"), [], {
      colspan: 6, empty: "尚未查询", emptyHint: "资金流需要逐笔成交样本，先查询标的",
    });
    this.container.querySelector("#l2-level-sub").textContent = "卖 5 → 卖 1 → 买 1 → 买 5（卖绿买红）";
    this.container.querySelector("#l2-tick-sub").textContent =
      "最多展示最近 " + LEVEL_TICK_LIMIT + " 笔；方向为第三方盘口标记";
    this.container.querySelector("#l2-flow-sub").textContent = "";
    this.renderEmptyCards("等待查询…", this.container.querySelector("#l2-metrics"), 6);
    this.renderEmptyCards("等待查询…", this.container.querySelector("#l2-flow-cards"), 2);
    if (!this.state.code) this.msg("");
  },

  /* 离线：三个区块都画空状态并给出原因（不弹窗、不发请求） */
  showOffline(reason) {
    const hint = reason + "；盘口 / L2 依赖公开源实时接口，离线时没有可展示的快照。";
    this.setAsOf("");
    this.msg("当前离线：不可用于交易决策", "warn");
    this.renderEmptyCards("离线", this.container.querySelector("#l2-metrics"), 6);
    this.renderEmptyCards("离线", this.container.querySelector("#l2-flow-cards"), 2);
    UI.tbody(this.container.querySelector("#l2-level-table tbody"), [], {
      colspan: 4, empty: "离线：暂无五档快照", emptyHint: hint,
    });
    UI.tbody(this.container.querySelector("#l2-tick-table tbody"), [], {
      colspan: 5, empty: "离线：暂无逐笔成交", emptyHint: hint,
    });
    UI.tbody(this.container.querySelector("#l2-flow-table tbody"), [], {
      colspan: 6, empty: "离线：暂无资金流数据", emptyHint: hint,
    });
    this.container.querySelector("#l2-level-sub").textContent = "离线 / 演示数据";
    this.container.querySelector("#l2-tick-sub").textContent = "离线 / 演示数据";
    this.container.querySelector("#l2-flow-sub").textContent = "离线 / 演示数据";
    this.renderToolsOffline(reason);
  },

  renderEmptyCards(text, el, count) {
    if (!el) return;
    const labels = Array.isArray(count)
      ? count
      : count === 6
        ? ["现价", "涨跌幅", "委比", "委差（手）", "外盘（手）", "内盘（手）"]
        : ["主力净额（万元）", "主力净额占比"];
    el.innerHTML = labels.map((k) => `
      <div class="metric-card">
        <div class="k">${fmt.esc(k)}</div>
        <div class="v flat">—</div>
        <div class="s flat">${fmt.esc(text)}</div>
      </div>`).join("");
  },

  setAsOf(text) {
    const el = this.container.querySelector("#l2-asof");
    if (el) el.textContent = text || "";
  },

  /* ---------------- 五档盘口 ---------------- */

  async loadOrderbook(code) {
    const tbody = this.container.querySelector("#l2-level-table tbody");
    UI.tbodyLoading(tbody, 4, "五档盘口加载中…");
    try {
      const json = await API.level2Orderbook(code);
      this.renderOrderbook(json);
    } catch (e) {
      this.renderEmptyCards("加载失败", this.container.querySelector("#l2-metrics"), 6);
      this.container.querySelector("#l2-level-sub").textContent = "加载失败：" + UI.apiErrorText(e);
      UI.tbodyError(tbody, {
        colspan: 4, title: "五档盘口加载失败", message: UI.apiErrorText(e),
        retry: () => this.loadOrderbook(code),
      });
      this.state.errors.push("五档盘口：" + UI.apiErrorText(e));
    }
  },

  renderOrderbook(json) {
    const d = (json && json.data) || {};
    const meta = d.meta || {};
    this.setAsOf(json && json.as_of ? "数据时点 " + json.as_of : fmt.or(d.ts, ""));
    if (meta.offline || meta.stale) this.msg(this.metaNote(meta), "warn");

    const summary = d.summary || {};
    const price = levelNum(d.price);
    const prevClose = levelNum(d.prev_close);
    const changePct = levelChangePct(price, prevClose);
    const change = (price === null || prevClose === null) ? null : price - prevClose;
    const bidVolume = levelNum(summary.bid_volume);
    const askVolume = levelNum(summary.ask_volume);
    const diff = (bidVolume === null || askVolume === null) ? null : bidVolume - askVolume;
    const imbalance = levelNum(summary.imbalance_pct);

    const cards = [
      { k: "现价", v: fmt.num(price, 2), c: fmt.color(changePct),
        s: "昨收 " + fmt.num(prevClose, 2) + " · 来源 " + fmt.or(d.source, "—") },
      { k: "涨跌幅", v: fmt.pct(changePct, true), c: fmt.color(changePct),
        s: "涨跌额 " + fmt.signed(change, 2) },
      { k: "委比", v: fmt.pct(imbalance, true), c: fmt.color(imbalance),
        s: "委买 " + fmt.int(bidVolume) + " 手 / 委卖 " + fmt.int(askVolume) + " 手" },
      { k: "委差（手）", v: fmt.signed(diff, 0), c: fmt.color(diff), s: "委差 = 委买 − 委卖" },
      { k: "外盘（手）", v: fmt.int(d.outer_volume), c: "flat", s: "公开源口径的主动买手数" },
      { k: "内盘（手）", v: fmt.int(d.inner_volume), c: "flat", s: "公开源口径的主动卖手数" },
    ];
    this.container.querySelector("#l2-metrics").innerHTML = cards.map((x) => `
      <div class="metric-card">
        <div class="k">${fmt.esc(x.k)}</div>
        <div class="v ${x.c}">${fmt.esc(x.v)}</div>
        <div class="s flat">${fmt.esc(x.s)}</div>
      </div>`).join("");

    const rows = this.levelRows(d);
    const sub = [
      (d.name ? d.name + " " : "") + (d.code || this.state.code || ""),
      "来源 " + fmt.or(d.source, "—"),
      d.ts ? "时点 " + d.ts : "",
      rows.length ? (rows.length / 2) + " 档" : "",
      this.capabilityText(d.capabilities),
    ].filter(Boolean);
    this.container.querySelector("#l2-level-sub").textContent = sub.join(" · ");

    UI.tbody(this.container.querySelector("#l2-level-table tbody"),
      rows.map((row) => `
        <tr class="${row.cls}">
          <td class="${row.cls}">${fmt.esc(row.slot)}</td>
          <td class="num">${fmt.esc(row.price)}</td>
          <td class="num">${fmt.esc(row.volume)}</td>
          <td class="num">${fmt.esc(row.amount)}</td>
        </tr>`),
      {
        colspan: 4,
        empty: (meta.offline ? "离线 / 演示数据" : "暂无五档数据"),
        emptyHint: "公开源盘口快照为空，或该标的当前不在交易时段",
      });
  },

  /* 行序固定：卖 5 → 卖 1 → 买 1 → 买 5（档数不足时按实际的卖 N…卖 1 / 买 1…买 N） */
  levelRows(d) {
    const asks = (Array.isArray(d.asks) ? d.asks : []).filter(Boolean).slice(0, 5);
    const bids = (Array.isArray(d.bids) ? d.bids : []).filter(Boolean).slice(0, 5);
    const rows = [];
    asks.slice().reverse().forEach((level, offset) => {
      rows.push(this.levelRow("卖" + (asks.length - offset), level, "down"));
    });
    bids.forEach((level, index) => {
      rows.push(this.levelRow("买" + (index + 1), level, "up"));
    });
    return rows;
  },

  levelRow(slot, level, cls) {
    const price = levelNum(level.price);
    const volume = levelNum(level.volume);
    return {
      slot: slot, cls: cls,
      price: price === null ? "—" : fmt.num(price, 2),
      volume: volume === null ? "—" : fmt.int(volume),
      amount: levelWan(level.amount, 2),
    };
  },

  capabilityText(caps) {
    const c = caps || {};
    const parts = [];
    if (c.orderbook) parts.push(fmt.or(c.orderbook_levels, 5) + " 档盘口");
    if (c.ticks) parts.push("逐笔成交");
    if (c.orders) parts.push("逐笔委托");
    if (c.queue) parts.push("委托队列");
    if (!c.orders && !c.queue) parts.push("无逐笔委托 / 委托队列（需付费授权）");
    if (c.import) parts.push("本地导入通道可用");
    return parts.join(" · ");
  },

  /* ---------------- 逐笔成交 ---------------- */

  async loadTicks(code) {
    const tbody = this.container.querySelector("#l2-tick-table tbody");
    UI.tbodyLoading(tbody, 5, "逐笔成交加载中…");
    try {
      const json = await API.level2Ticks(code, LEVEL_TICK_LIMIT);
      this.renderTicks(json);
    } catch (e) {
      this.container.querySelector("#l2-tick-sub").textContent = "加载失败：" + UI.apiErrorText(e);
      UI.tbodyError(tbody, {
        colspan: 5, title: "逐笔成交加载失败", message: UI.apiErrorText(e),
        retry: () => this.loadTicks(code),
      });
      this.state.errors.push("逐笔成交：" + UI.apiErrorText(e));
    }
  },

  renderTicks(json) {
    const d = (json && json.data) || {};
    const meta = d.meta || {};
    const items = (Array.isArray(d.items) ? d.items : []).filter(Boolean).slice(0, LEVEL_TICK_LIMIT);
    const stats = d.stats || {};
    const sub = items.length
      ? [
        "展示 " + items.length + " / 共 " + fmt.int(d.count === undefined ? items.length : d.count) + " 笔",
        "买 " + fmt.int(stats.buy_volume) + " / 卖 " + fmt.int(stats.sell_volume) + "（手）",
        "净额 " + levelWan(stats.net_amount, 2) + " 万元",
        fmt.or(d.note, ""),
      ].filter(Boolean).join(" · ")
      : "最多展示最近 " + LEVEL_TICK_LIMIT + " 笔；方向为第三方盘口标记";
    this.container.querySelector("#l2-tick-sub").textContent = sub;

    UI.tbody(this.container.querySelector("#l2-tick-table tbody"),
      items.map((t) => {
        const cls = levelSideClass(t.side);
        const label = levelSideLabel(t.side);
        const badge = label === "买" ? "badge-buy" : label === "卖" ? "badge-sell" : "";
        return `
        <tr class="${cls}">
          <td>${fmt.esc(fmt.or(t.time, "—"))}</td>
          <td class="num">${fmt.num(t.price, 2)}</td>
          <td class="num">${fmt.int(t.volume)}</td>
          <td class="num">${levelWan(t.amount, 2)}</td>
          <td><span class="badge ${badge}">${fmt.esc(label)}</span></td>
        </tr>`;
      }),
      {
        colspan: 5,
        empty: (meta.offline ? "离线 / 演示数据" : "暂无逐笔成交数据"),
        emptyHint: "公开源逐笔接口只覆盖最近约 4000 笔；盘前 / 停牌时为空属正常",
      });
  },

  /* ---------------- 资金流 ---------------- */

  async loadFlow(code) {
    const tbody = this.container.querySelector("#l2-flow-table tbody");
    UI.tbodyLoading(tbody, 6, "资金流加载中…");
    try {
      const json = await API.level2Flow(code, LEVEL_FLOW_LIMIT);
      this.renderFlow(json);
    } catch (e) {
      this.container.querySelector("#l2-flow-sub").textContent = "加载失败：" + UI.apiErrorText(e);
      this.renderEmptyCards("加载失败", this.container.querySelector("#l2-flow-cards"), 2);
      UI.tbodyError(tbody, {
        colspan: 6, title: "资金流加载失败", message: UI.apiErrorText(e),
        retry: () => this.loadFlow(code),
      });
      this.state.errors.push("资金流：" + UI.apiErrorText(e));
    }
  },

  renderFlow(json) {
    const d = (json && json.data) || {};
    const meta = d.meta || {};
    const buckets = d.buckets || {};
    const order = ["super_big", "big", "mid", "small"];
    const labels = { super_big: "超大单", big: "大单", mid: "中单", small: "小单" };
    const sample = levelNum(d.amount_total) || 0;
    const tickCount = levelNum(d.tick_count) || 0;
    const hasSample = sample > 0 || tickCount > 0;
    const rows = hasSample ? order.map((key) => {
      const row = buckets[key] || {};
      return {
        label: row.label || labels[key],
        buy: levelWan(row.buy, 2),
        sell: levelWan(row.sell, 2),
        net: levelNum(row.net),
        pct: fmt.pct(row.buy_pct),
        count: fmt.int(row.count),
      };
    }) : [];

    if (hasSample) {
      const cards = [
        { k: "主力净额（万元）", v: levelWan(d.main_net, 2), c: fmt.color(d.main_net),
          s: "超大单 + 大单净额 · 样本成交额 " + levelWan(d.amount_total, 2) + " 万元" },
        { k: "主力净额占比", v: fmt.pct(d.main_net_pct), c: fmt.color(d.main_net_pct),
          s: "主力净额 / 样本成交额 · 逐笔 " + fmt.int(d.tick_count) + " 笔" },
      ];
      this.container.querySelector("#l2-flow-cards").innerHTML = cards.map((x) => `
        <div class="metric-card">
          <div class="k">${fmt.esc(x.k)}</div>
          <div class="v ${x.c}">${fmt.esc(x.v)}</div>
          <div class="s flat">${fmt.esc(x.s)}</div>
        </div>`).join("");
      this.container.querySelector("#l2-flow-sub").textContent =
        "样本 " + fmt.int(d.tick_count) + " 笔 · 成交额 " + levelWan(d.amount_total, 2) + " 万元";
    } else {
      this.renderEmptyCards("无逐笔样本", this.container.querySelector("#l2-flow-cards"), 2);
      this.container.querySelector("#l2-flow-sub").textContent = meta.offline ? "离线 / 演示数据" : "无逐笔样本";
    }

    UI.tbody(this.container.querySelector("#l2-flow-table tbody"),
      rows.map((row) => `
        <tr>
          <td>${fmt.esc(row.label)}</td>
          <td class="num">${fmt.esc(row.buy)}</td>
          <td class="num">${fmt.esc(row.sell)}</td>
          <td class="num ${fmt.color(row.net)}">${row.net === null ? "—" : fmt.signed(row.net, 2)}</td>
          <td class="num">${fmt.esc(row.pct)}</td>
          <td class="num">${fmt.esc(row.count)}</td>
        </tr>`),
      {
        colspan: 6,
        empty: (meta.offline ? "离线 / 演示数据" : "暂无资金流数据"),
        emptyHint: "资金流由逐笔成交额分档自算：没有逐笔样本时无法给出净额与占比",
      });
  },


  /* ---------------- L2 工具（手动触发；失败只坏自身） ---------------- */

  /* 4 个工具：大单追踪 / 资金流分时 / 封板状态 / 扫描与排行。
     全部只在点击按钮时请求；3 秒自动刷新（tick → load）只刷新盘口 / 逐笔 / 资金流。 */

  metricCards(cards) {
    return (cards || []).map((x) => `
      <div class="metric-card">
        <div class="k">${fmt.esc(x.k)}</div>
        <div class="v ${x.c || "flat"}">${fmt.esc(x.v)}</div>
        <div class="s flat">${fmt.esc(x.s)}</div>
      </div>`).join("");
  },

  /* 工具区状态行：无失败时灰字，有 failures 时改用警告条（不弹窗） */
  setToolStatus(el, text, warn) {
    if (!el) return;
    el.className = warn ? "inline-msg inline-msg-warn" : "panel-sub";
    el.textContent = text || "";
  },

  /* 工具未查询 / 离线时的提示（只写在本区块内） */
  toolHint() {
    return this.state.code
      ? "点本区块的按钮开始（" + this.state.code + "）：这 4 个工具只在手动点击时请求，不随自动刷新触发"
      : "尚未查询：先在上方输入标的代码并点「查询」";
  },

  /* 扫描 / 排行的标的：优先自选池（最多 10 只，后端上限也是 10），为空时回退到当前查询标的 */
  toolCodes() {
    const list = Store.state.watchlist && Array.isArray(Store.state.watchlist.codes)
      ? Store.state.watchlist.codes.filter(Boolean) : [];
    if (list.length) return { codes: list.slice(0, 10), source: "自选池 " + list.length + " 只" };
    if (this.state.code) return { codes: [this.state.code], source: "自选池为空，回退为当前标的" };
    return { codes: [], source: "自选池为空" };
  },

  /* failures 行：只展示前 3 个失败原因，超出用「等」收尾 */
  failuresText(failures) {
    const list = (failures || []).filter(Boolean);
    if (!list.length) return "";
    const shown = list.slice(0, 3)
      .map((f) => fmt.or(f.code, "?") + "（" + fmt.or(f.error, "失败") + "）").join("；");
    return "⚠ 失败 " + list.length + " 只：" + shown + (list.length > 3 ? " 等" : "");
  },

  /* ---- 大单追踪 ---- */

  bigEmpty(text, hint) {
    const c = this.container;
    this.renderEmptyCards(text, c.querySelector("#l2-big-stats"),
      ["大单笔数", "买入额（万元）", "卖出额（万元）", "净额（万元）"]);
    UI.tbody(c.querySelector("#l2-big-table tbody"), [], { colspan: 6, empty: text, emptyHint: hint });
  },

  async loadBigOrders() {
    const c = this.container;
    const tbody = c.querySelector("#l2-big-table tbody");
    const stats = c.querySelector("#l2-big-stats");
    const sub = c.querySelector("#l2-big-sub");
    if (this.state.toolBusy.big) return;
    if (!this.state.code) {
      this.bigEmpty("尚未查询", this.toolHint());
      sub.textContent = "";
      return;
    }
    const offline = this.offlineReason();
    if (offline) {
      this.bigEmpty("离线", offline + "；大单追踪依赖公开源逐笔接口，离线时没有可展示的数据。");
      sub.textContent = "离线 / 演示数据";
      return;
    }
    this.state.toolBusy.big = true;
    const restore = UI.busy(c.querySelector("#l2-big-run"), true, "查询中…");
    const started = Date.now();
    UI.tbodyLoading(tbody, 6, "大单加载中（" + LEVEL_BIG_COST + "）…");
    try {
      const json = await API.level2BigOrders(this.state.code, this.state.bigThreshold, LEVEL_BIG_LIMIT);
      this.renderBigOrders(json, (Date.now() - started) / 1000);
    } catch (e) {
      this.renderEmptyCards("加载失败", stats, ["大单笔数", "买入额（万元）", "卖出额（万元）", "净额（万元）"]);
      sub.textContent = "加载失败：" + UI.apiErrorText(e);
      UI.tbodyError(tbody, {
        colspan: 6, title: "大单追踪加载失败", message: UI.apiErrorText(e),
        hint: LEVEL_BIG_COST, retry: () => this.loadBigOrders(),
      });
    } finally {
      this.state.toolBusy.big = false;
      restore();
    }
  },

  renderBigOrders(json, seconds) {
    const c = this.container;
    const d = (json && json.data) || {};
    const summary = d.summary || {};
    const items = (Array.isArray(d.items) ? d.items : []).filter(Boolean);
    const biggest = summary.biggest || null;
    const threshold = levelNum(summary.threshold === undefined ? d.threshold : summary.threshold);
    const buyPct = levelNum(summary.buy_amount_pct);
    const cards = [
      { k: "大单笔数", v: levelInt(summary.count), c: "flat",
        s: "买 " + levelInt(summary.buy_count) + " 笔 / 卖 " + levelInt(summary.sell_count) + " 笔" },
      { k: "买入额（万元）", v: levelWan(summary.buy_amount, 2), c: "up",
        s: "买入占大单额 " + (buyPct === null ? "—" : fmt.pct(buyPct)) },
      { k: "卖出额（万元）", v: levelWan(summary.sell_amount, 2), c: "down",
        s: "卖出占大单额 " + (buyPct === null ? "—" : fmt.pct(100 - buyPct)) },
      { k: "净额（万元）", v: levelWan(summary.net_amount, 2), c: fmt.color(summary.net_amount),
        s: "净额 = 买入 − 卖出" },
    ];
    c.querySelector("#l2-big-stats").innerHTML = this.metricCards(cards);
    c.querySelector("#l2-big-sub").textContent = [
      (d.name ? d.name + " " : "") + (d.code || this.state.code || ""),
      "阈值 ≥ " + levelWan(threshold, 0) + " 万元",
      "展示 " + levelInt(d.shown === undefined ? items.length : d.shown) +
        " / 共 " + levelInt(d.count === undefined ? summary.count : d.count) + " 笔",
      biggest ? "最大单笔 " + levelWan(biggest.amount, 2) + " 万元（" +
        fmt.or(biggest.bucket_label, fmt.or(biggest.bucket, "—")) + "）" : "",
      "占样本成交额 " + levelPct(summary.amount_share_pct),
      "耗时 " + fmt.num(seconds, 1) + " 秒",
    ].filter(Boolean).join(" · ");
    UI.tbody(c.querySelector("#l2-big-table tbody"),
      items.map((t) => {
        const cls = levelSideClass(t.side);
        const label = levelSideLabel(t.side);
        const badge = label === "买" ? "badge badge-buy"
          : label === "卖" ? "badge badge-sell" : "badge badge-cat";
        return `
        <tr class="${cls}">
          <td>${fmt.esc(fmt.or(t.time, "—"))}</td>
          <td class="num">${levelFixed(t.price, 2)}</td>
          <td class="num">${levelInt(t.volume)}</td>
          <td class="num">${levelWan(t.amount, 2)}</td>
          <td><span class="${badge}">${fmt.esc(label)}</span></td>
          <td>${fmt.esc(fmt.or(t.bucket_label, fmt.or(t.bucket, "—")))}</td>
        </tr>`;
      }),
      {
        colspan: 6,
        empty: "暂无大单",
        emptyHint: "当前逐笔样本里没有单笔 ≥ " + levelWan(threshold, 0) + " 万元的成交；可降低阈值或稍后重试",
      });
  },

  /* ---- 资金流分时 ---- */

  seriesEmpty(text, hint) {
    const c = this.container;
    this.renderEmptyCards(text, c.querySelector("#l2-series-metrics"),
      ["主力净额（万元）", "主力净额占比", "分钟数"]);
    c.querySelector("#l2-series-state").innerHTML = "";
    Charts.placeholder("l2-series-chart", text, hint);
    UI.tbody(c.querySelector("#l2-series-buckets tbody"), [], { colspan: 4, empty: text, emptyHint: hint });
  },

  async loadFlowSeries() {
    const c = this.container;
    const tbody = c.querySelector("#l2-series-buckets tbody");
    const metrics = c.querySelector("#l2-series-metrics");
    const stateEl = c.querySelector("#l2-series-state");
    const sub = c.querySelector("#l2-series-sub");
    if (this.state.toolBusy.series) return;
    if (!this.state.code) {
      this.seriesEmpty("尚未查询", this.toolHint());
      sub.textContent = "";
      return;
    }
    const offline = this.offlineReason();
    if (offline) {
      this.seriesEmpty("离线", offline + "；资金流分时依赖公开源逐笔接口，离线时没有可展示的数据。");
      sub.textContent = "离线 / 演示数据";
      return;
    }
    this.state.toolBusy.series = true;
    const restore = UI.busy(c.querySelector("#l2-series-run"), true, "查询中…");
    const started = Date.now();
    UI.loading(stateEl, "资金流分时加载中（" + LEVEL_SERIES_COST + "）…");
    Charts.placeholder("l2-series-chart", "加载中…", "逐笔按分钟聚合后绘制累计净额曲线");
    try {
      const json = await API.level2FlowSeries(this.state.code, LEVEL_SERIES_LIMIT);
      stateEl.innerHTML = "";
      this.renderFlowSeries(json, (Date.now() - started) / 1000);
    } catch (e) {
      stateEl.innerHTML = "";
      UI.setError(stateEl, {
        title: "资金流分时加载失败", message: UI.apiErrorText(e),
        hint: LEVEL_SERIES_COST, retry: () => this.loadFlowSeries(),
      });
      this.renderEmptyCards("加载失败", metrics, ["主力净额（万元）", "主力净额占比", "分钟数"]);
      Charts.placeholder("l2-series-chart", "加载失败", UI.apiErrorText(e));
      UI.tbody(tbody, [], { colspan: 4, empty: "加载失败", emptyHint: "可点上方错误提示里的「重试」" });
      sub.textContent = "加载失败：" + UI.apiErrorText(e);
    } finally {
      this.state.toolBusy.series = false;
      restore();
    }
  },

  renderFlowSeries(json, seconds) {
    const c = this.container;
    const d = (json && json.data) || {};
    const series = (Array.isArray(d.series) ? d.series : []).filter(Boolean);
    const buckets = d.buckets || {};
    const order = ["super_big", "big", "mid", "small"];
    const labels = { super_big: "超大单", big: "大单", mid: "中单", small: "小单" };
    const first = series.length ? fmt.or(series[0].time, "") : "";
    const last = series.length ? fmt.or(series[series.length - 1].time, "") : "";
    const cards = [
      { k: "主力净额（万元）", v: levelWan(d.main_net, 2), c: fmt.color(d.main_net),
        s: "超大单 + 大单净额 · 样本成交额 " + levelWan(d.amount_total, 2) + " 万元" },
      { k: "主力净额占比", v: levelPct(d.main_net_pct), c: fmt.color(d.main_net_pct),
        s: "主力净额 / 样本成交额 · 逐笔 " + levelInt(d.tick_sample) + " 笔" },
      { k: "分钟数", v: levelInt(d.minutes === undefined ? series.length : d.minutes), c: "flat",
        s: series.length ? "覆盖 " + first + "–" + last : "暂无逐笔样本" },
    ];
    c.querySelector("#l2-series-metrics").innerHTML = this.metricCards(cards);
    c.querySelector("#l2-series-sub").textContent = [
      (d.name ? d.name + " " : "") + (d.code || this.state.code || ""),
      "样本 " + levelInt(d.tick_sample) + " 笔 · 成交额 " + levelWan(d.amount_total, 2) + " 万元",
      "耗时 " + fmt.num(seconds, 1) + " 秒",
      fmt.or(d.note, ""),
    ].filter(Boolean).join(" · ");

    if (series.length) {
      Charts.flowNet("l2-series-chart", series.map((x) => fmt.or(x.time, "")),
        series.map((x) => { const n = levelNum(x.cum_net); return n === null ? null : n / 1e4; }));
    } else {
      Charts.placeholder("l2-series-chart", "暂无分时资金流", "该标的当前没有可聚合的逐笔成交样本");
    }

    const hasSample = levelNum(d.amount_total) > 0 || series.length > 0;
    const rows = hasSample ? order.map((key) => {
      const row = buckets[key] || {};
      return {
        label: row.label || labels[key],
        net: levelNum(row.net),
        pct: levelNum(row.buy_pct) === null ? "—" : fmt.pct(row.buy_pct),
        count: levelInt(row.count),
      };
    }) : [];
    UI.tbody(c.querySelector("#l2-series-buckets tbody"),
      rows.map((row) => `
        <tr>
          <td>${fmt.esc(row.label)}</td>
          <td class="num ${fmt.color(row.net)}">${levelWan(row.net, 2)}</td>
          <td class="num">${fmt.esc(row.pct)}</td>
          <td class="num">${fmt.esc(row.count)}</td>
        </tr>`),
      {
        colspan: 4, empty: "暂无逐笔样本",
        emptyHint: "没有逐笔成交样本时无法给出分档净额（盘前 / 停牌时为空属正常）",
      });
  },

  /* ---- 封板状态 ---- */

  sealEmpty(title, hint) {
    const body = this.container.querySelector("#l2-seal-body");
    if (!body) return;
    body.innerHTML = UI.emptyBlock({ title: title, hint: hint });
  },

  async loadSeal() {
    const c = this.container;
    const body = c.querySelector("#l2-seal-body");
    const sub = c.querySelector("#l2-seal-sub");
    if (this.state.toolBusy.seal) return;
    if (!this.state.code) {
      this.sealEmpty("尚未查询", this.toolHint());
      sub.textContent = "";
      return;
    }
    const offline = this.offlineReason();
    if (offline) {
      this.sealEmpty("离线", offline + "；封板状态依赖公开源快照，离线时无法判断此刻是否封板。");
      sub.textContent = "离线 / 演示数据";
      return;
    }
    this.state.toolBusy.seal = true;
    const restore = UI.busy(c.querySelector("#l2-seal-run"), true, "查询中…");
    const started = Date.now();
    UI.loading(body, "封板状态加载中…");
    try {
      const json = await API.level2Seal(this.state.code);
      this.renderSeal(json, (Date.now() - started) / 1000);
    } catch (e) {
      sub.textContent = "加载失败：" + UI.apiErrorText(e);
      UI.setError(body, {
        title: "封板状态加载失败", message: UI.apiErrorText(e),
        retry: () => this.loadSeal(),
      });
    } finally {
      this.state.toolBusy.seal = false;
      restore();
    }
  },

  renderSeal(json, seconds) {
    const c = this.container;
    const d = (json && json.data) || {};
    const seal = d.seal || {};
    const state = String(seal.state || "unknown");
    const isUp = state === "limit_up";
    const isDown = state === "limit_down";
    const label = seal.label || (isUp ? "涨停" : isDown ? "跌停" : state === "normal" ? "未封板" : "数据不足");
    const badge = isUp ? "badge badge-buy" : isDown ? "badge badge-sell" : "badge badge-cat";
    const distanceHint = isDown
      ? "距跌停 = (现价 − 跌停价) / 跌停价"
      : "距涨停 = (涨停价 − 现价) / 现价；已封板时约 0%";
    const sealVolumeHint = isUp ? "涨停价买一挂单量" : isDown ? "跌停价卖一挂单量" : "未封板时为 0";
    const cards = [
      { k: "涨停价", v: levelFixed(seal.limit_up_price, 2), c: "up",
        s: "昨收 " + levelFixed(d.prev_close, 2) + " · 跌停价 " + levelFixed(seal.limit_down_price, 2) },
      { k: isDown ? "距跌停" : "距涨停", v: levelPct(seal.distance_pct, true), c: "flat", s: distanceHint },
      { k: "封单量（手）", v: levelInt(seal.seal_volume), c: "flat", s: sealVolumeHint },
      { k: "封单额（万元）", v: levelWan(seal.seal_amount, 2), c: "flat", s: "封单量 × 封板价" },
      { k: "封成比", v: levelPct(seal.seal_ratio), c: "flat",
        s: "封单额 / 当日成交额 · 成交额 " + levelWan(seal.amount_total, 2) + " 万元" },
    ];
    c.querySelector("#l2-seal-body").innerHTML = `
      <div class="panel-sub">
        <span class="${badge}" id="l2-seal-badge">${fmt.esc(label)}</span>
        ${fmt.esc(fmt.or(d.seal_text, seal.limit_pct_text || "—"))}
      </div>
      <div class="metric-grid metric-grid-fluid">${this.metricCards(cards)}</div>`;
    c.querySelector("#l2-seal-sub").textContent = [
      (d.name ? d.name + " " : "") + (d.code || this.state.code || ""),
      "现价 " + levelFixed(d.price, 2),
      "耗时 " + fmt.num(seconds, 1) + " 秒",
      "仅当前快照",
    ].filter(Boolean).join(" · ");
  },

  /* ---- 扫描与排行 ---- */

  async runScan() {
    const c = this.container;
    const tbody = c.querySelector("#l2-scan-table tbody");
    const status = c.querySelector("#l2-scan-status");
    if (this.state.toolBusy.scan) return;
    const pick = this.toolCodes();
    if (!pick.codes.length) {
      this.setToolStatus(status, "自选池为空，且上方未查询标的：请先在行情页添加自选，或查询一个标的", true);
      UI.tbody(tbody, [], {
        colspan: 11, empty: "没有可扫描的标的",
        emptyHint: "「扫描自选池」使用自选池（最多 10 只）；自选池为空时可先在上方查询一个标的",
      });
      return;
    }
    const offline = this.offlineReason();
    if (offline) {
      this.setToolStatus(status, "离线 / 演示数据：" + offline, true);
      UI.tbody(tbody, [], { colspan: 11, empty: "离线：暂无扫描数据", emptyHint: offline });
      return;
    }
    this.state.toolBusy.scan = true;
    const restore = UI.busy(c.querySelector("#l2-scan-run"), true, "扫描中…");
    const started = Date.now();
    UI.tbodyLoading(tbody, 11, "扫描中（" + LEVEL_SCAN_COST + "）…");
    try {
      const json = await API.level2Scan(pick.codes, LEVEL_SCAN_LIMIT);
      this.renderScan(json, pick, (Date.now() - started) / 1000);
    } catch (e) {
      this.setToolStatus(status, "扫描失败：" + UI.apiErrorText(e), true);
      UI.tbodyError(tbody, {
        colspan: 11, title: "扫描失败", message: UI.apiErrorText(e),
        hint: LEVEL_SCAN_COST, retry: () => this.runScan(),
      });
    } finally {
      this.state.toolBusy.scan = false;
      restore();
    }
  },

  renderScan(json, pick, seconds) {
    const c = this.container;
    const d = (json && json.data) || {};
    const failures = (Array.isArray(d.failures) ? d.failures : []).filter(Boolean);
    const items = (Array.isArray(d.items) ? d.items : []).filter(Boolean)
      .slice().sort((a, b) => (levelNum(b.imbalance_pct) || 0) - (levelNum(a.imbalance_pct) || 0));
    const status = [
      "扫描 " + fmt.int(d.count === undefined ? items.length : d.count) + " / " +
        fmt.int(d.requested === undefined ? items.length : d.requested) + " 只（按委比降序）",
      "来源：" + pick.source,
      "耗时 " + fmt.num(seconds, 1) + " 秒",
      this.failuresText(failures),
    ].filter(Boolean).join(" · ");
    this.setToolStatus(c.querySelector("#l2-scan-status"), status, failures.length > 0);
    UI.tbody(c.querySelector("#l2-scan-table tbody"),
      items.map((x) => {
        const sealState = String(x.seal_state || "");
        const sealBadge = sealState === "limit_up" ? "badge badge-buy"
          : sealState === "limit_down" ? "badge badge-sell" : "badge badge-cat";
        return `
        <tr>
          <td>${fmt.esc(fmt.or(x.code, "—"))}</td>
          <td>${fmt.esc(fmt.or(x.name, "—"))}</td>
          <td class="num">${levelFixed(x.price, 2)}</td>
          <td class="num ${fmt.color(x.change_pct)}">${levelPct(x.change_pct, true)}</td>
          <td class="num ${fmt.color(x.imbalance_pct)}">${levelPct(x.imbalance_pct, true)}</td>
          <td class="num">${levelFixed(x.ratio, 2)}</td>
          <td class="num">${levelFixed(x.spread, 2)}</td>
          <td class="num">${levelFixed(x.volume_ratio, 2)}</td>
          <td><span class="${sealBadge}">${fmt.esc(fmt.or(x.seal_label, "—"))}</span></td>
          <td class="num">${levelWan(x.seal_amount, 2)}</td>
          <td class="num">${levelPct(x.distance_pct, true)}</td>
        </tr>`;
      }),
      {
        colspan: 11,
        empty: "暂无扫描结果",
        emptyHint: "扫描基于单次快照的静态特征（委比 / 委差比 / 量比 / 封板），没有可扫描的标的时为空",
      });
  },

  async runFlowRank() {
    const c = this.container;
    const tbody = c.querySelector("#l2-rank-table tbody");
    const status = c.querySelector("#l2-rank-status");
    if (this.state.toolBusy.rank) return;
    const pick = this.toolCodes();
    if (!pick.codes.length) {
      this.setToolStatus(status, "自选池为空，且上方未查询标的：请先在行情页添加自选，或查询一个标的", true);
      UI.tbody(tbody, [], {
        colspan: 7, empty: "没有可排行的标的",
        emptyHint: "「资金流排行」使用自选池（最多 10 只）；自选池为空时可先在上方查询一个标的",
      });
      return;
    }
    const offline = this.offlineReason();
    if (offline) {
      this.setToolStatus(status, "离线 / 演示数据：" + offline, true);
      UI.tbody(tbody, [], { colspan: 7, empty: "离线：暂无排行数据", emptyHint: offline });
      return;
    }
    this.state.toolBusy.rank = true;
    const restore = UI.busy(c.querySelector("#l2-rank-run"), true, "排行中…");
    const started = Date.now();
    UI.tbodyLoading(tbody, 7, "资金流排行中（" + LEVEL_RANK_COST + "）…");
    try {
      const json = await API.level2FlowRank(pick.codes, LEVEL_RANK_TOP, LEVEL_RANK_LIMIT);
      this.renderFlowRank(json, pick, (Date.now() - started) / 1000);
    } catch (e) {
      this.setToolStatus(status, "排行失败：" + UI.apiErrorText(e), true);
      UI.tbodyError(tbody, {
        colspan: 7, title: "资金流排行失败", message: UI.apiErrorText(e),
        hint: LEVEL_RANK_COST, retry: () => this.runFlowRank(),
      });
    } finally {
      this.state.toolBusy.rank = false;
      restore();
    }
  },

  renderFlowRank(json, pick, seconds) {
    const c = this.container;
    const d = (json && json.data) || {};
    const failures = (Array.isArray(d.failures) ? d.failures : []).filter(Boolean);
    const items = (Array.isArray(d.items) ? d.items : []).filter(Boolean)
      .slice().sort((a, b) => (levelNum(b.main_net) || 0) - (levelNum(a.main_net) || 0));
    const status = [
      "排行 " + fmt.int(d.count === undefined ? items.length : d.count) + " / " +
        fmt.int(d.requested === undefined ? items.length : d.requested) + " 只（按主力净额降序）",
      "来源：" + pick.source,
      "耗时 " + fmt.num(seconds, 1) + " 秒",
      this.failuresText(failures),
    ].filter(Boolean).join(" · ");
    this.setToolStatus(c.querySelector("#l2-rank-status"), status, failures.length > 0);
    UI.tbody(c.querySelector("#l2-rank-table tbody"),
      items.map((x) => `
        <tr>
          <td>${fmt.esc(fmt.or(x.code, "—"))}</td>
          <td>${fmt.esc(fmt.or(x.name, "—"))}</td>
          <td class="num ${fmt.color(x.main_net)}">${levelWan(x.main_net, 2)}</td>
          <td class="num ${fmt.color(x.main_net_pct)}">${levelPct(x.main_net_pct)}</td>
          <td class="num ${fmt.color(x.net_amount)}">${levelWan(x.net_amount, 2)}</td>
          <td class="num">${levelWan(x.amount_total, 2)}</td>
          <td class="num">${levelInt(x.tick_count)}</td>
        </tr>`),
      {
        colspan: 7,
        empty: "暂无排行结果",
        emptyHint: "排行按主力净额（超大单 + 大单净额）降序；逐笔样本为空时没有可排行的数据",
      });
  },

  /* ---- 工具区整块状态（未查询 / 离线） ---- */

  renderToolsIdle() {
    const c = this.container;
    this.bigEmpty("尚未查询", this.toolHint());
    c.querySelector("#l2-big-sub").textContent = "";
    this.seriesEmpty("尚未查询", this.toolHint());
    c.querySelector("#l2-series-sub").textContent = "";
    this.sealEmpty("尚未查询", this.toolHint());
    c.querySelector("#l2-seal-sub").textContent = "";
    this.setToolStatus(c.querySelector("#l2-scan-status"), "", false);
    this.setToolStatus(c.querySelector("#l2-rank-status"), "", false);
    UI.tbody(c.querySelector("#l2-scan-table tbody"), [], {
      colspan: 11, empty: "尚未扫描", emptyHint: "点「扫描自选池」（" + LEVEL_SCAN_COST + "）",
    });
    UI.tbody(c.querySelector("#l2-rank-table tbody"), [], {
      colspan: 7, empty: "尚未排行", emptyHint: "点「资金流排行」（" + LEVEL_RANK_COST + "）",
    });
  },

  renderToolsOffline(reason) {
    const c = this.container;
    const hint = reason + "；这 4 个工具都依赖公开源快照 / 逐笔接口，离线时没有可展示的数据。";
    this.bigEmpty("离线", hint);
    c.querySelector("#l2-big-sub").textContent = "离线 / 演示数据";
    this.seriesEmpty("离线", hint);
    c.querySelector("#l2-series-sub").textContent = "离线 / 演示数据";
    this.sealEmpty("离线", hint);
    c.querySelector("#l2-seal-sub").textContent = "离线 / 演示数据";
    this.setToolStatus(c.querySelector("#l2-scan-status"), "离线 / 演示数据：" + reason, true);
    this.setToolStatus(c.querySelector("#l2-rank-status"), "离线 / 演示数据：" + reason, true);
    UI.tbody(c.querySelector("#l2-scan-table tbody"), [], { colspan: 11, empty: "离线：暂无扫描数据", emptyHint: hint });
    UI.tbody(c.querySelector("#l2-rank-table tbody"), [], { colspan: 7, empty: "离线：暂无排行数据", emptyHint: hint });
  },

  /* ---------------- 小工具 ---------------- */

  metaNote(meta) {
    const parts = [];
    if (meta.offline) parts.push("离线/演示数据（来源：" + fmt.or(meta.source, "本地") + "），不可用于交易决策");
    if (meta.stale) parts.push("数据为缓存快照");
    (meta.notes || []).forEach((note) => { if (note) parts.push(String(note)); });
    return parts.join(" · ");
  },
};

window.Level2View = Level2View;
