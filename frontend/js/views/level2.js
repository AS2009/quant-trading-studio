/*
 * views/level2.js — 盘口 / L2 视图（公开源近似的五档 · 逐笔 · 资金流分档）
 * 职责：
 *   1. 代码输入 → 查询：GET /api/level2/<code>/orderbook | /ticks?limit=60 | /flow?limit=2000
 *   2. 指标卡：现价 / 涨跌幅 / 委比 / 委差 / 外盘 / 内盘
 *   3. 五档表（卖 5 → 卖 1 → 买 1 → 买 5，卖绿买红；量额并列）
 *   4. 逐笔表（时间 / 价格 / 手数 / 金额 / 方向，买红卖绿，最多 60 行）
 *   5. 资金流卡（超大单 / 大单 / 中单 / 小单 的净额与买入占比 + 主力净额与占比）
 *   6. 自动刷新（默认关，3 秒）：仅在本页可见且已填代码时发请求
 * 数据边界（页面上必须如实标注，不含糊）：
 *   - 五档 / 外盘 / 内盘是公开快照，不是推送；
 *   - 逐笔方向是第三方「盘口方向标记」，不是交易所 Level-2 的主动买卖判定；
 *   - 十档 / 逐笔委托 / 委托队列没有公开来源，需付费授权。
 * 降级：三个接口各自失败只在对应区块内画空状态（带原因 + 重试），绝不清空整页、不弹窗；
 *       离线（后端不可达 / meta.offline）时三个区块都画空状态并说明原因。
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
  },

  render(container) {
    this.container = container;
    this.clearTimer();
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
      </div>`;

    this.bind();
    if (this.state.auto) this.setAuto(true, true);   /* 重绘后保持开关状态（不重复提示） */
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
  },

  renderEmptyCards(text, el, count) {
    if (!el) return;
    const labels = count === 6
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
