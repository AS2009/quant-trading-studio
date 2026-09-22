/*
 * store.js — 全局状态与订阅（视图之间唯一的共享数据通道）
 * 职责：
 *   1. 保存跨视图状态：当前视图、自选池、选中标的、策略列表、回测表单记忆、数据源状态
 *   2. 提供 on/off/emit 订阅机制，视图不直接互相引用
 *   3. 数据源等级（real / cache / offline）的唯一判定点：status + 最近响应 meta
 * 说明：无构建步骤，全局单例 Store 通过 window.Store 暴露，api.js 在拿到响应后回调 noteMeta。
 */
"use strict";

const Store = {
  state: {
    /* 视图 */
    view: "market",
    /* 后端连通性（/api/health）：{checked, ok, error, info} */
    backend: { checked: false, ok: false, error: null, info: null },
    /* /api/system/status（可能因后端未实现而失败，失败时 statusError 记录原因） */
    status: null,
    statusError: null,
    statusCheckedAt: null,
    /* 最近一次成功响应里的 meta 与时间（数据源新鲜度） */
    lastMeta: null,
    lastResponseAt: null,
    lastResponsePath: "",
    /* 数据源等级：real | cache | offline | unknown */
    level: "unknown",
    /* 自选池 */
    watchlist: { codes: [], items: [], supported: true, error: null, loaded: false },
    /* 当前 K 线标的 */
    selectedSymbol: null,
    /* 策略 */
    strategies: [],
    strategiesLoaded: false,
    strategiesError: null,
    selectedStrategy: null,
    /* 回测表单记忆（切走再切回不丢草稿） */
    backtestForm: null,
    /* 资金与基准默认值（来自 /api/system/status，取不到用本地兜底） */
    initialCash: 1000000,
    benchmark: "000300.SH",
  },

  _listeners: {},

  /* ---------------- 订阅 ---------------- */
  on(event, fn) {
    (this._listeners[event] = this._listeners[event] || []).push(fn);
    return () => this.off(event, fn);
  },
  off(event, fn) {
    const list = this._listeners[event] || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  },
  emit(event, payload) {
    (this._listeners[event] || []).slice().forEach((fn) => {
      try { fn(payload, this.state); } catch (e) { console.error("store listener failed:", event, e); }
    });
  },

  set(patch, event) {
    Object.assign(this.state, patch);
    if (event) this.emit(event, this.state);
  },

  /* ---------------- 数据源状态 ---------------- */
  /* api.js 每次成功响应都会调用：记录 meta 并重算数据源等级 */
  noteMeta(meta, asOf, path) {
    const next = {
      lastMeta: meta || null,
      lastResponseAt: new Date(),
      lastResponsePath: path || "",
    };
    if (meta && meta.as_of) next.lastMeta = Object.assign({}, meta, { as_of: meta.as_of });
    if (asOf && (!meta || !meta.as_of)) {
      next.lastMeta = Object.assign({}, meta || {}, { as_of: asOf });
    }
    this.state.lastMeta = next.lastMeta;
    this.state.lastResponseAt = next.lastResponseAt;
    this.state.lastResponsePath = next.lastResponsePath;
    this._recomputeLevel();
    /* 数据源状态条订阅 meta：新鲜度/来源标签随每次响应更新 */
    this.emit("meta", this.state);
  },

  /* health：reachable = 接口可访问；ok = 后端自称可用真实数据（ok !== false 且 mode != offline） */
  noteHealth(result) {
    const r = result || {};
    this.state.backend = {
      checked: true,
      reachable: !!r.reachable,
      ok: !!r.ok,
      info: r.info || null,
      error: r.error || null,
    };
    this._recomputeLevel();
    this.emit("backend", this.state);
  },

  noteStatus(status, error) {
    this.state.status = status || null;
    this.state.statusError = error || null;
    this.state.statusCheckedAt = new Date();
    if (status) {
      if (typeof status.initial_cash === "number" && status.initial_cash > 0) {
        this.state.initialCash = status.initial_cash;
      }
      if (status.benchmark) this.state.benchmark = status.benchmark;
    }
    this._recomputeLevel();
    this.emit("status", this.state);
  },

  /* 等级判定顺序：后端不可达 → 系统状态 → 最近一次 meta → 未知 */
  _recomputeLevel() {
    const next = this.computeLevel();
    if (next !== this.state.level) {
      this.state.level = next;
      this.emit("level", this.state);
    }
  },

  computeLevel() {
    const b = this.state.backend;
    const st = this.state.status;
    const meta = this.state.lastMeta;
    if (b.checked && !b.reachable) return "offline"; /* 后端不可用 */
    if (b.checked && !b.ok) return "offline";        /* 后端可用但自称未接入真实行情 */
    if (st) {
      if (st.offline) return "offline";
      const mode = String(st.mode || "").toLowerCase();
      if (mode === "offline") return "offline";
      if (mode === "cache") return "cache";
      if (mode === "real") return meta && meta.stale ? "cache" : "real";
    }
    if (meta) {
      if (meta.offline) return "offline";
      if (meta.stale) return "cache";
      const src = String(meta.source || "").toLowerCase();
      if (src && src !== "sample" && src !== "csv") return "real";
      return "offline"; /* 来源为 sample/csv → 仍属本地演示数据 */
    }
    /* 无 status 也无 meta：旧版后端（演示数据）或接口全部失败 */
    if (b.checked && b.ok) return "offline";
    return "unknown";
  },

  /* 是否必须显示「不可用于交易决策」的强提示（离线/降级/后端自称 ok=false） */
  isDangerous() {
    const st = this.state.status;
    const b = this.state.backend;
    if (this.state.level === "offline") return true;
    if (b.checked && !b.ok) return true;
    return !!(st && st.offline);
  },

  /* 离线原因文案（数据源状态条与详情共用） */
  offlineReason() {
    const b = this.state.backend;
    if (b.checked && !b.reachable) return "后端未连接：" + fmt.or(b.error, "无法访问 /api/health");
    if (b.checked && !b.ok && b.info) {
      return "后端已连接（version " + fmt.or(b.info.version, "—") + "，mode " + fmt.or(b.info.mode, "—") +
        "，provider " + fmt.or(b.info.provider, "—") + "）但未接入真实行情";
    }
    return "";
  },

  /* 市场是否休市（状态条副标题用） */
  marketInfo() {
    const st = this.state.status || {};
    const m = st.market || {};
    return { isClosed: !!m.is_closed, lastTradingDay: m.last_trading_day || "" };
  },

  /* ---------------- 自选池 ---------------- */
  async loadWatchlist(force) {
    if (this.state.watchlist.loaded && !force) return this.state.watchlist;
    try {
      const json = await API.watchlist();
      const data = json.data || {};
      this.state.watchlist = {
        codes: Array.isArray(data.codes) ? data.codes : [],
        items: Array.isArray(data.items) ? data.items : [],
        supported: true,
        error: null,
        loaded: true,
      };
    } catch (e) {
      /* 旧版后端无该接口：降级为「只读默认池」，由行情视图给出提示 */
      this.state.watchlist = {
        codes: [],
        items: [],
        supported: !(e instanceof ApiError && e.unimplemented),
        error: e,
        loaded: true,
      };
    }
    this.emit("watchlist", this.state);
    return this.state.watchlist;
  },

  setWatchlistCodes(codes) {
    this.state.watchlist = Object.assign({}, this.state.watchlist, {
      codes: Array.isArray(codes) ? codes : [],
      loaded: true,
    });
    this.emit("watchlist", this.state);
  },

  /* ---------------- 其他 ---------------- */
  setSelectedSymbol(code) {
    if (!code || code === this.state.selectedSymbol) return;
    this.state.selectedSymbol = code;
    this.emit("symbol", this.state);
  },

  setStrategies(list) {
    this.state.strategies = Array.isArray(list) ? list : [];
    this.state.strategiesLoaded = true;
    this.state.strategiesError = null;
    this.emit("strategies", this.state);
  },

  findStrategy(id) {
    return this.state.strategies.find((s) => s.id === id) || null;
  },
};

window.Store = Store;
