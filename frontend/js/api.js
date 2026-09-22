/*
 * api.js — 后端接口访问层（唯一与网络打交道的地方）
 * 职责：
 *   1. 统一请求封装：JSON 序列化、超时、统一错误解析（{"error","code","field"}）
 *   2. 统一响应信封解析：{data, as_of, meta} → 返回 {data, as_of, meta}，并上报 meta 给 Store
 *   3. 按接口契约提供全部函数（行情 / 自选 / 策略 / 回测 / 持仓 / 交易）
 * 约定：调用方只拿 API.xxx()/API.postXxx() 的返回值，不做 fetch 细节判断。
 *      任何失败都抛 ApiError（含 status / code / field），由视图层决定降级文案。
 */
"use strict";

/* ---------------- 错误类型 ---------------- */
class ApiError extends Error {
  constructor(message, opts) {
    super(message || "请求失败");
    this.name = "ApiError";
    this.status = (opts && opts.status) || 0;      // HTTP 状态码，0 = 网络不可达
    this.code = (opts && opts.code) || null;       // 后端业务错误码
    this.field = (opts && opts.field) || null;     // 后端指出的字段名
    this.path = (opts && opts.path) || "";
  }
  /* 接口未实现 / 网关类错误 → 视图层用它区分「功能不可用」与「数据错误」 */
  get unimplemented() {
    return this.status === 404 || this.status === 405 || this.status === 501;
  }
}

const API = {
  base: "",
  defaultTimeout: 20000,
  /* 最近一次成功响应的 meta（数据源状态条读取 Store.lastMeta，此处仅留痕便于调试） */
  lastMeta: null,

  /* 统一请求：_parse 处理信封与错误 */
  async request(method, path, body, opts) {
    const options = { method: method, headers: {} };
    const timeout = (opts && opts.timeout) || this.defaultTimeout;
    if (body !== undefined && body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    if (controller) options.signal = controller.signal;
    const timer = setTimeout(() => controller && controller.abort(), timeout);

    let res;
    try {
      res = await fetch(this.base + path, options);
    } catch (e) {
      clearTimeout(timer);
      const aborted = e && (e.name === "AbortError");
      throw new ApiError(
        aborted ? "请求超时（" + Math.round(timeout / 1000) + "s）：" + path
                : "无法连接后端服务，请确认后端已启动（python app.py）",
        { status: 0, path: path }
      );
    }
    clearTimeout(timer);

    const text = await res.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (e) { payload = null; }
    }

    if (!res.ok) {
      const msg = (payload && (payload.error || payload.message)) ||
        (res.status === 404 ? "接口不存在：" + path : "HTTP " + res.status);
      throw new ApiError(msg, {
        status: res.status,
        code: payload && payload.code,
        field: payload && payload.field,
        path: path,
      });
    }
    if (payload === null) {
      throw new ApiError("后端返回内容无法解析为 JSON：" + path, { status: res.status, path: path });
    }
    this._noteMeta(payload, path);
    return payload;
  },

  /* 把响应里的 meta/as_of 上报给全局状态（数据源状态条据此刷新） */
  _noteMeta(payload, path) {
    if (!payload || typeof payload !== "object") return;
    const meta = payload.meta || null;
    const asOf = payload.as_of || (meta && meta.as_of) || null;
    if (!meta && !asOf) return;
    this.lastMeta = meta;
    if (typeof window !== "undefined" && window.Store && window.Store.noteMeta) {
      window.Store.noteMeta(meta, asOf, path);
    }
  },

  get(path, opts) { return this.request("GET", path, undefined, opts); },
  post(path, body, opts) { return this.request("POST", path, body === undefined ? {} : body, opts); },
  del(path, opts) { return this.request("DELETE", path, undefined, opts); },

  /* 查询串拼装：跳过 undefined / null / 空串 */
  qs(params) {
    const parts = [];
    Object.keys(params || {}).forEach((k) => {
      const v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
  },

  /* ---------------- 基础 ---------------- */
  health: () => API.get("/api/health", { timeout: 8000 }),
  systemStatus: () => API.get("/api/system/status"),

  /* ---------------- 行情 ---------------- */
  marketOverview: () => API.get("/api/market/overview"),
  marketSectors: (limit) => API.get("/api/market/sectors" + API.qs({ limit: limit })),
  /* codes 为空时交给后端默认自选池（旧版后端即如此） */
  quotes: (codes) => API.get("/api/market/quotes" + API.qs({ codes: codes })),
  kline: (code, days, freq, adjust) =>
    API.get("/api/market/kline" + API.qs({ code: code, days: days, freq: freq || "day", adjust: adjust || "qfq" })),

  /* ---------------- 自选池 ---------------- */
  watchlist: () => API.get("/api/watchlist"),
  addWatch: (code) => API.post("/api/watchlist", { code: code }),
  removeWatch: (code) => API.del("/api/watchlist/" + encodeURIComponent(code)),

  /* ---------------- 策略 ---------------- */
  strategies: () => API.get("/api/strategies"),
  strategy: (id) => API.get("/api/strategies/" + encodeURIComponent(id)),
  createStrategy: (payload) => API.post("/api/strategies", payload),
  deleteStrategy: (id) => API.del("/api/strategies/" + encodeURIComponent(id)),

  /* ---------------- 回测 ---------------- */
  /* params: {start,end,cash,benchmark,symbols,slippage_bps,commission_rate}，symbols 用数组 */
  backtest: (id, params, opts) =>
    API.get("/api/backtest/" + encodeURIComponent(id) + API.qs(API.backtestQuery(params)), opts),
  runBacktest: (id, params, opts) =>
    API.post("/api/backtest/" + encodeURIComponent(id), API.backtestQuery(params), opts),
  backtestQuery(params) {
    const p = params || {};
    return {
      start: p.start,
      end: p.end,
      cash: p.cash,
      benchmark: p.benchmark,
      symbols: Array.isArray(p.symbols) ? p.symbols.join(",") : p.symbols,
      slippage_bps: p.slippage_bps,
      commission_rate: p.commission_rate,
    };
  },

  /* ---------------- 持仓 ---------------- */
  portfolioOverview: () => API.get("/api/portfolio/overview"),
  holdings: () => API.get("/api/portfolio/holdings"),
  portfolioEquity: (days) => API.get("/api/portfolio/equity" + API.qs({ days: days })),
  addHolding: (payload) => API.post("/api/portfolio/holdings", payload),
  removeHolding: (code) => API.del("/api/portfolio/holdings/" + encodeURIComponent(code)),
  setCash: (amount) => API.post("/api/portfolio/cash", { amount: amount }),
  setPortfolioMode: (mode) => API.post("/api/portfolio/mode", { mode: mode }),

  /* ---------------- 交易 ---------------- */
  orders: (limit) => API.get("/api/orders" + API.qs({ limit: limit })),
  placeOrder: (payload) => API.post("/api/orders", payload),
  cancelOrder: (orderId) => API.del("/api/orders/" + encodeURIComponent(orderId)),
  fills: (limit) => API.get("/api/fills" + API.qs({ limit: limit })),
  resetPaper: (initialCash) =>
    API.post("/api/orders/reset", initialCash === undefined ? {} : { initial_cash: initialCash }),
};
