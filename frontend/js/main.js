/*
 * main.js — 应用入口：视图注册、Tab 路由、顶部时钟、数据源状态探测与全局错误兜底
 * 职责：
 *   1. 注册视图模块（新增视图只需在此 App.register(...) 一行）
 *   2. Tab 路由：点击 / 键盘方向键 / URL hash 三种方式切换，切换后重算图表尺寸
 *   3. 启动流程：/api/health（连通性）→ /api/system/status（数据源与配置）→ 自选池 → 首屏视图
 *   4. 全局错误兜底：未捕获异常与 Promise 拒绝弹出 toast，避免只留白屏
 * 依赖顺序：api.js → store.js → ui.js → charts.js → views/*.js → main.js
 */
"use strict";

const App = {
  views: {},

  register(view) {
    if (!view || !view.id) throw new Error("视图必须提供 id");
    this.views[view.id] = view;
    return this;
  },

  /* ---------------- 启动 ---------------- */
  async init() {
    this.bindTopbar();
    this.bindGlobalErrors();
    SourceBar.mount(document.getElementById("source-bar"), { onRefresh: () => this.refreshStatus() });
    this.tickClock();
    setInterval(() => this.tickClock(), 30000);
    /* 状态类接口并行探测，失败也只提示不阻塞首屏 */
    await Promise.all([this.checkHealth(), this.loadStatus()]);
    /* 自选池：多个视图共用（行情看板 / 回测标的池 / 交易快捷输入） */
    Store.loadWatchlist(false).catch(() => {});
    setInterval(() => this.loadStatus(true), 120000);

    const initial = (location.hash || "").replace("#", "");
    await this.activate(this.views[initial] ? initial : "market");
    window.addEventListener("resize", () => Charts.resizeAll());
    window.addEventListener("hashchange", () => {
      const id = (location.hash || "").replace("#", "");
      if (this.views[id] && id !== Store.state.view) this.activate(id);
    });
  },

  /* ---------------- 顶栏 ---------------- */
  bindTopbar() {
    const tabs = Array.from(document.querySelectorAll(".tab"));
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => this.activate(tab.dataset.view));
      tab.addEventListener("keydown", (e) => {
        const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        e.preventDefault();
        const next = tabs[(i + step + tabs.length) % tabs.length];
        next.focus();
        this.activate(next.dataset.view);
      });
    });
  },

  tickClock() {
    const el = document.getElementById("clock");
    if (el) el.textContent = fmt.stamp(new Date());
  },

  /* ---------------- 路由 ---------------- */
  async activate(viewId, params) {
    const view = this.views[viewId];
    if (!view) return;
    Store.state.view = viewId;
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t.dataset.view === viewId;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
      t.tabIndex = active ? 0 : -1;
    });
    let section = document.getElementById("view-" + viewId);
    if (!section) {
      section = document.createElement("section");
      section.id = "view-" + viewId;
      section.className = "view";
      section.setAttribute("role", "tabpanel");
      section.setAttribute("aria-label", view.label || viewId);
      document.getElementById("views").appendChild(section);
    }
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    section.classList.add("active");
    if (location.hash !== "#" + viewId) history.replaceState(null, "", "#" + viewId);
    if (params) view.params = params;
    try {
      await view.render(section);
    } catch (e) {
      console.error("view render failed:", viewId, e);
      Toast.err("页面渲染失败：" + UI.apiErrorText(e));
      UI.setError(section, {
        title: viewId + " 渲染失败",
        message: UI.apiErrorText(e),
        hint: "可切换到其它标签页后返回重试",
        retry: () => view.render(section),
      });
    }
    /* 视图切换后容器尺寸才生效，延后一帧重算图表 */
    requestAnimationFrame(() => Charts.resizeAll());
  },

  refreshCurrent() {
    const view = this.views[Store.state.view];
    if (view && typeof view.refresh === "function") return view.refresh();
    return Promise.resolve();
  },

  /* ---------------- 数据源状态 ---------------- */
  async checkHealth() {
    try {
      const json = await API.health();
      /* HTTP 可达 ≠ 真实数据可用：后端自称 ok=false / mode=offline / provider=unavailable 时按离线处理 */
      const mode = String(json.mode || "").toLowerCase();
      const provider = String(json.provider || "").toLowerCase();
      const usable = json.ok !== false && mode !== "offline" && provider !== "unavailable";
      Store.noteHealth({
        reachable: true,
        ok: usable,
        info: json,
        error: usable ? null : ("后端自报 mode=" + fmt.or(json.mode, "—") + " · provider=" + fmt.or(json.provider, "—")),
      });
    } catch (e) {
      Store.noteHealth({ reachable: false, ok: false, info: null, error: UI.apiErrorText(e) });
    }
  },

  async loadStatus(silent) {
    try {
      const json = await API.systemStatus();
      Store.noteStatus(json.data || null, null);
    } catch (e) {
      Store.noteStatus(null, e);
      if (!silent) {
        /* /api/system/status 未实现时提示一次，不影响其它区域 */
        Toast.warn("数据源状态接口不可用：" + UI.apiErrorText(e));
      }
    }
  },

  async refreshStatus() {
    await Promise.all([this.checkHealth(), this.loadStatus(false)]);
    const view = this.views[Store.state.view];
    if (view && typeof view.refresh === "function") view.refresh();
  },

  /* ---------------- 全局兜底 ---------------- */
  bindGlobalErrors() {
    window.addEventListener("error", (e) => {
      const msg = e && e.message ? e.message : "未知脚本错误";
      console.error("global error:", e.error || msg);
      Toast.err("页面脚本错误：" + msg);
    });
    window.addEventListener("unhandledrejection", (e) => {
      const reason = e && e.reason;
      const msg = reason instanceof ApiError ? UI.apiErrorText(reason)
        : (reason && reason.message) || String(reason);
      console.error("unhandled rejection:", reason);
      Toast.err("请求或处理失败：" + msg);
    });
  },
};

/* ---------------- 视图注册（新增视图只需一行） ---------------- */
App.register(window.MarketView)
  .register(window.StrategiesView)
  .register(window.BacktestView)
  .register(window.PortfolioView)
  .register(window.TradeView)
  .register(window.Level2View);

window.App = App;
window.addEventListener("DOMContentLoaded", () => {
  App.init().catch((e) => {
    console.error("init failed", e);
    Toast.err("初始化失败：" + UI.apiErrorText(e));
  });
});
