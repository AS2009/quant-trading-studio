/*
 * views/trade.js — 交易视图（模拟盘 / 纸面交易）
 * 职责：
 *   1. 显著标注模拟盘：不会发送任何真实委托
 *   2. 下单表单（代码 / 方向 / 数量 / 价格可空=市价）→ POST /api/orders
 *   3. 账户概览与当前持仓（/api/portfolio/overview、/api/portfolio/holdings）
 *   4. 委托列表（可撤单 DELETE /api/orders/<order_id>）与成交列表（/api/fills）
 *   5. 重置模拟盘（POST /api/orders/reset，二次确认）
 * 对外接口：TradeView.render(container) / refresh()
 */
"use strict";

const TradeView = {
  id: "trade",
  label: "交易",
  container: null,

  ORDER_STATUS: {
    new: { text: "待成交", cls: "badge-paused" },
    filled: { text: "已成交", cls: "badge-running" },
    partial: { text: "部分成交", cls: "badge-cat" },
    rejected: { text: "已拒绝", cls: "badge-sell" },
    cancelled: { text: "已撤销", cls: "badge-custom" },
  },

  state: { loading: false, side: "buy", orders: [], lastSubmitAt: 0 },

  render(container) {
    this.container = container;
    const status = Store.state.status || {};
    const trade = status.trade || {};
    const mode = trade.mode || "paper";
    container.innerHTML = `
      <h2 class="view-title">交易 <span class="view-sub">模拟盘（纸面交易）</span></h2>

      <div class="sim-banner" role="note">
        <span class="sim-tag">模拟盘</span>
        <span>本页所有委托仅在本地模拟撮合，<strong>不会发送任何真实委托、不会连接券商</strong>；数据用于策略与流程验证，不可用于真实交易决策。</span>
        <span class="flat">账户模式：${fmt.esc(mode === "paper" ? "模拟盘（paper）" : "手工记账（manual）")}</span>
      </div>

      <div id="td-cards" class="card-grid cols-4"></div>

      <div class="trade-layout">
        <div class="panel">
          <div class="panel-title">
            <span>下单</span>
            <span class="panel-sub" id="td-form-sub"></span>
          </div>
          <form id="td-form" novalidate>
            <div class="field">
              <label class="field-label" for="td-code">标的代码 <em>*</em></label>
              <input type="text" id="td-code" list="td-code-list" placeholder="600519 / sh600519 / 600519.SH"
                autocomplete="off" aria-describedby="td-code-hint">
              <datalist id="td-code-list"></datalist>
              <div class="field-hint" id="td-code-hint">来自自选池，可手动输入其它代码</div>
            </div>
            <div class="field">
              <label class="field-label">方向 <em>*</em></label>
              <span class="segmented" role="group" aria-label="买卖方向">
                <button type="button" class="seg seg-buy active" data-side="buy">买入</button>
                <button type="button" class="seg seg-sell" data-side="sell">卖出</button>
              </span>
            </div>
            <div class="field-row">
              ${UI.field({ id: "td-qty", label: "数量（股）", required: true, type: "number", min: 100, step: 100,
                placeholder: "100 的整数倍", hint: "A 股最小 100 股；卖出零股可不足 100" })}
              ${UI.field({ id: "td-price", label: "价格（元，留空 = 市价）", type: "number", min: 0, step: 0.01,
                placeholder: "市价", hint: "限价单请填写价格" })}
            </div>
            <div id="td-est" class="code-block"></div>
            <div class="btn-row td-form-foot">
              <button type="submit" class="btn btn-primary" id="td-submit">提交委托</button>
              <button type="button" class="btn" id="td-reset">重置模拟盘</button>
              <button type="button" class="btn" id="td-reload">刷新</button>
            </div>
            <div id="td-msg" class="inline-msg" role="status" hidden></div>
          </form>
        </div>

        <div class="panel">
          <div class="panel-title">
            <span>当前持仓 <span class="panel-sub" id="td-pos-count"></span></span>
          </div>
          <div class="table-wrap table-scroll">
            <table class="data-table" id="td-positions-table">
              <thead>
                <tr>
                  <th>代码</th><th>名称</th><th class="num">数量</th><th class="num">可卖</th>
                  <th class="num">成本价</th><th class="num">现价</th><th class="num">市值(元)</th>
                  <th class="num">当日盈亏</th><th class="num">累计盈亏</th><th class="num">收益率</th>
                </tr>
              </thead>
              <tbody><!-- JS --></tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>委托列表 <span class="panel-sub" id="td-order-count"></span></span>
          <span class="panel-sub">撤单仅影响本地模拟委托</span>
        </div>
        <div class="table-wrap">
          <table class="data-table" id="td-orders-table">
            <thead>
              <tr>
                <th>委托时间</th><th>委托号</th><th>代码</th><th>名称</th><th>方向</th>
                <th class="num">委托价</th><th class="num">委托量</th><th class="num">已成</th><th class="num">成交均价</th>
                <th class="num">手续费</th><th>状态</th><th>原因</th><th class="act">操作</th>
              </tr>
            </thead>
            <tbody><!-- JS --></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>成交列表 <span class="panel-sub" id="td-fill-count"></span></span>
        </div>
        <div class="table-wrap">
          <table class="data-table" id="td-fills-table">
            <thead>
              <tr>
                <th>成交时间</th><th>委托号</th><th>代码</th><th>名称</th><th>方向</th>
                <th class="num">成交价</th><th class="num">数量</th><th class="num">金额(元)</th>
                <th class="num">手续费</th><th>说明</th>
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
    c.querySelector("#td-form").addEventListener("submit", (e) => {
      e.preventDefault();
      this.submit();
    });
    c.querySelectorAll("[data-side]").forEach((b) => {
      b.addEventListener("click", () => {
        this.state.side = b.dataset.side;
        this.syncSide();
        this.updateEstimate();
      });
    });
    this.syncSide();
    ["td-code", "td-qty", "td-price"].forEach((id) => {
      const el = c.querySelector("#" + id);
      el.addEventListener("input", () => { this.msg(""); this.updateEstimate(); });
    });
    c.querySelector("#td-reset").addEventListener("click", () => this.reset());
    c.querySelector("#td-reload").addEventListener("click", () => this.load(true));
    this.updateEstimate();
  },

  /* 让分段控件与 state.side 保持一致（渲染后 / 切换后调用，避免 UI 与状态不一致） */
  syncSide() {
    const c = this.container;
    if (!c) return;
    const buy = this.state.side === "buy";
    c.querySelectorAll("[data-side]").forEach((x) =>
      x.classList.toggle("active", x.dataset.side === this.state.side));
    const btn = c.querySelector("#td-submit");
    if (!btn) return;
    btn.textContent = buy ? "提交买入委托" : "提交卖出委托";
    btn.className = "btn " + (buy ? "btn-primary" : "btn-sell");
  },

  codeText() {
    const el = this.container.querySelector("#td-code");
    return el ? String(el.value || "").trim() : "";
  },

  msg(message, type) {
    const el = this.container.querySelector("#td-msg");
    if (!el) return;
    if (!message) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.className = "inline-msg inline-msg-" + (type || "info");
    el.textContent = message;
  },

  quoteOf(code) {
    const items = Store.state.watchlist.items || [];
    return items.find((q) => q.code === code) || null;
  },

  /* 估算金额：优先用输入价格，其次用自选池最新价（仅提示，不作为成交依据） */
  updateEstimate() {
    const c = this.container;
    const est = c.querySelector("#td-est");
    if (!est) return;
    const check = UI.normalizeSymbol(this.codeText());
    const qty = Number(c.querySelector("#td-qty").value || 0);
    const priceRaw = c.querySelector("#td-price").value;
    const price = priceRaw === "" ? null : Number(priceRaw);
    const q = check.ok ? this.quoteOf(check.code) : null;
    const ref = price !== null && isFinite(price) && price > 0 ? price : (q ? Number(q.price) : null);
    const ov = this.state.overview || {};
    const avail = Number(ov.available_cash !== undefined ? ov.available_cash : ov.cash);
    const lines = [];
    if (check.ok && qty > 0 && ref) {
      lines.push("预估" + (this.state.side === "buy" ? "买入" : "卖出") + "金额 ≈ " + fmt.num(qty * ref, 2) + " 元" +
        (price === null ? "（按最新价 " + fmt.num(ref, 2) + " 估算）" : "（按限价）"));
    } else {
      lines.push("填写代码与数量后显示金额估算" + (price === null ? "（市价单按最新价估算）" : ""));
    }
    if (q && price === null) lines.push("最新价 " + fmt.num(q.price, 2) + " · " + fmt.pct(q.change_pct, true));
    if (isFinite(avail)) lines.push("账户可用现金 " + fmt.num(avail, 2) + " 元");
    if (this.state.side === "sell") {
      const pos = (this.state.positions || []).find((p) => check.ok && p.code === check.code);
      lines.push(pos ? "可卖 " + fmt.int(pos.available_qty) + " 股" : "该标的当前无持仓");
    }
    est.textContent = lines.join("；");
  },

  async load(isManual) {
    if (this.state.loading) return;
    this.state.loading = true;
    if (isManual) this.msg("");
    this.renderCodeList();
    await Promise.all([
      this.loadAccount(),
      this.loadPositions(),
      this.loadOrders(),
      this.loadFills(),
    ]);
    this.state.loading = false;
    this.updateEstimate();
  },

  renderCodeList() {
    const dl = this.container.querySelector("#td-code-list");
    const items = Store.state.watchlist.items || [];
    dl.innerHTML = items.map((q) => `<option value="${fmt.esc(q.code)}">${fmt.esc(q.name)}</option>`).join("");
    const sub = this.container.querySelector("#td-form-sub");
    sub.textContent = items.length ? "自选池 " + items.length + " 只可作为快捷输入" : "自选池为空，可手动输入代码";
  },

  async loadAccount() {
    const el = this.container.querySelector("#td-cards");
    UI.loading(el, "账户数据加载中…");
    try {
      const json = await API.portfolioOverview();
      const ov = json.data || {};
      this.state.overview = ov;
      const cards = [
        { k: "总资产(元)", v: fmt.num(ov.total_assets, 2), c: "flat" },
        { k: "持仓市值(元)", v: fmt.num(ov.market_value, 2), c: "flat" },
        { k: "可用现金(元)", v: fmt.num(ov.available_cash !== undefined ? ov.available_cash : ov.cash, 2), c: "flat" },
        { k: "当日盈亏(元)", v: fmt.signed(ov.day_pnl, 2), c: fmt.color(ov.day_pnl) },
        { k: "累计盈亏(元)", v: fmt.signed(ov.total_pnl, 2), c: fmt.color(ov.total_pnl) },
        { k: "总收益率", v: fmt.pct(ov.total_return_pct, true), c: fmt.color(ov.total_return_pct) },
        { k: "持仓数量", v: fmt.int(ov.positions_count) + " 只", c: "flat" },
        { k: "数据时点", v: fmt.or(ov.as_of || json.as_of, "—"), c: "flat", small: "模拟盘账面" },
      ];
      el.innerHTML = cards.map((x) => `
        <div class="stat-card">
          <div class="k">${fmt.esc(x.k)}</div>
          <div class="v ${x.c}">${fmt.esc(x.v)}</div>
          ${x.small ? `<div class="s flat">${fmt.esc(x.small)}</div>` : ""}
        </div>`).join("");
    } catch (e) {
      UI.setError(el, { title: "账户数据加载失败", message: UI.apiErrorText(e), retry: () => this.loadAccount() });
    }
  },

  async loadPositions() {
    const c = this.container;
    const tbody = c.querySelector("#td-positions-table tbody");
    if (!tbody) return;
    UI.tbodyLoading(tbody, 10, "持仓加载中…");
    try {
      const json = await API.holdings();
      const list = (json.data || []).filter(Boolean);
      this.state.positions = list;
      c.querySelector("#td-pos-count").textContent = list.length ? list.length + " 只" : "";
      UI.tbody(tbody, list.map((p) => `
        <tr>
          <td>${fmt.esc(p.code)}</td>
          <td>${fmt.esc(p.name || "—")}</td>
          <td class="num">${fmt.int(p.qty)}</td>
          <td class="num flat">${fmt.int(p.available_qty)}</td>
          <td class="num">${fmt.num(p.cost, 3)}</td>
          <td class="num">${fmt.num(p.price, 2)}</td>
          <td class="num">${fmt.num(p.market_value, 2)}</td>
          <td class="num ${fmt.color(p.day_pnl)}">${fmt.signed(p.day_pnl, 2)}</td>
          <td class="num ${fmt.color(p.total_pnl)}">${fmt.signed(p.total_pnl, 2)}</td>
          <td class="num ${fmt.color(p.return_pct)}">${fmt.pct(p.return_pct, true)}</td>
        </tr>`), {
        colspan: 10,
        empty: "当前无持仓",
        emptyHint: "用左侧下单表单买入，或在「持仓管理」手工录入",
      });
    } catch (e) {
      UI.tbodyError(tbody, {
        colspan: 10, title: "持仓加载失败",
        message: UI.apiErrorText(e), retry: () => this.loadPositions(),
      });
    }
  },

  async loadOrders() {
    const c = this.container;
    const tbody = c.querySelector("#td-orders-table tbody");
    if (!tbody) return;
    UI.tbodyLoading(tbody, 13, "委托列表加载中…");
    try {
      const json = await API.orders(100);
      const list = (json.data || []).filter(Boolean);
      this.state.orders = list;
      c.querySelector("#td-order-count").textContent = list.length ? "最近 " + list.length + " 条" : "";
      UI.tbody(tbody, list.map((o) => this.orderRow(o)), {
        colspan: 13,
        empty: "暂无委托",
        emptyHint: "提交一笔模拟委托后在此查看状态与撤单",
      });
      tbody.querySelectorAll("[data-cancel]").forEach((b) =>
        b.addEventListener("click", () => this.cancel(b.dataset.cancel, b)));
    } catch (e) {
      UI.tbodyError(tbody, {
        colspan: 13, title: "委托列表加载失败",
        message: UI.apiErrorText(e), retry: () => this.loadOrders(),
      });
    }
  },

  orderRow(o) {
    const isBuy = o.side === "buy";
    const st = this.ORDER_STATUS[o.status] || { text: fmt.or(o.status, "未知"), cls: "badge-cat" };
    const cancellable = o.status === "new" || o.status === "partial";
    return `
      <tr>
        <td>${fmt.esc(fmt.or(o.created_at, "—"))}</td>
        <td class="mono">${fmt.esc(fmt.or(o.order_id, "—"))}</td>
        <td>${fmt.esc(o.code)}</td>
        <td>${fmt.esc(fmt.or(o.name, "—"))}</td>
        <td><span class="badge ${isBuy ? "badge-buy" : "badge-sell"}">${isBuy ? "买入" : "卖出"}</span></td>
        <td class="num">${o.price === null || o.price === undefined ? "市价" : fmt.num(o.price, 2)}</td>
        <td class="num">${fmt.int(o.qty)}</td>
        <td class="num">${fmt.int(o.filled_qty)}</td>
        <td class="num">${o.avg_price ? fmt.num(o.avg_price, 3) : "—"}</td>
        <td class="num flat">${fmt.num(o.fee, 2)}</td>
        <td><span class="badge ${st.cls}">${fmt.esc(st.text)}</span></td>
        <td class="flat">${fmt.esc(fmt.or(o.reason, "—"))}</td>
        <td class="act">${cancellable
          ? `<button type="button" class="btn btn-sm btn-danger" data-cancel="${fmt.esc(o.order_id)}">撤单</button>`
          : '<span class="flat">—</span>'}</td>
      </tr>`;
  },

  async loadFills() {
    const c = this.container;
    const tbody = c.querySelector("#td-fills-table tbody");
    if (!tbody) return;
    UI.tbodyLoading(tbody, 10, "成交列表加载中…");
    try {
      const json = await API.fills(100);
      const list = (json.data || []).filter(Boolean);
      c.querySelector("#td-fill-count").textContent = list.length ? "最近 " + list.length + " 条" : "";
      UI.tbody(tbody, list.map((f) => {
        const isBuy = f.side === "buy";
        return `
        <tr>
          <td>${fmt.esc(fmt.or(f.ts, "—"))}</td>
          <td class="mono">${fmt.esc(fmt.or(f.order_id, "—"))}</td>
          <td>${fmt.esc(f.code)}</td>
          <td>${fmt.esc(fmt.or(f.name, "—"))}</td>
          <td><span class="badge ${isBuy ? "badge-buy" : "badge-sell"}">${isBuy ? "买入" : "卖出"}</span></td>
          <td class="num">${fmt.num(f.price, 2)}</td>
          <td class="num">${fmt.int(f.qty)}</td>
          <td class="num">${fmt.num(f.amount, 2)}</td>
          <td class="num flat">${fmt.num(f.fee, 2)}</td>
          <td class="flat">${fmt.esc(fmt.or(f.reason, "—"))}</td>
        </tr>`;
      }), { colspan: 10, empty: "暂无成交", emptyHint: "模拟撮合成交后会在此记录" });
    } catch (e) {
      UI.tbodyError(tbody, {
        colspan: 10, title: "成交列表加载失败",
        message: UI.apiErrorText(e), retry: () => this.loadFills(),
      });
    }
  },

  /* ---------------- 下单 ---------------- */
  validate() {
    const c = this.container;
    const check = UI.normalizeSymbol(this.codeText());
    if (!check.ok) return { message: check.message, field: "#td-code" };
    const qty = Number(c.querySelector("#td-qty").value);
    if (!qty || qty <= 0 || !Number.isInteger(qty)) return { message: "数量需为正整数（股）", field: "#td-qty" };
    const priceRaw = c.querySelector("#td-price").value;
    if (priceRaw !== "" && (!isFinite(Number(priceRaw)) || Number(priceRaw) <= 0)) {
      return { message: "价格需大于 0，或留空使用市价", field: "#td-price" };
    }
    return { ok: true, code: check.code, qty: qty, price: priceRaw === "" ? null : Number(priceRaw) };
  },

  async submit() {
    const c = this.container;
    const v = this.validate();
    if (!v.ok) {
      this.msg(v.message, "err");
      const el = c.querySelector(v.field);
      if (el) el.focus();
      return;
    }
    if (v.qty % 100 !== 0) {
      const go = await Dialog.confirm({
        title: "数量不是 100 的整数倍",
        message: "A 股买入需为 100 股整数倍；若为卖出零股可继续提交，最终由后端校验。是否继续提交？",
        confirmText: "继续提交",
      });
      if (!go) return;
    }
    const btn = c.querySelector("#td-submit");
    const restore = UI.busy(btn, true, "委托提交中…");
    try {
      const json = await API.placeOrder({
        code: v.code,
        side: this.state.side,
        qty: v.qty,
        price: v.price,
      });
      const data = json.data || {};
      const order = data.order || {};
      const fill = data.fill || null;
      this.state.lastSubmitAt = Date.now();
      const st = this.ORDER_STATUS[order.status] || { text: fmt.or(order.status, "已提交") };
      Toast.ok("委托已提交：" + (order.side === "sell" ? "卖出" : "买入") + " " + (order.name || v.code) +
        " " + fmt.int(order.qty || v.qty) + " 股 · " + st.text +
        (fill ? " · 成交 " + fmt.int(fill.qty) + " 股 @ " + fmt.num(fill.price, 2) : ""));
      this.msg((fill
        ? "已成交：" + fmt.int(fill.qty) + " 股 @ " + fmt.num(fill.price, 2) + " 元，手续费 " + fmt.num(fill.fee, 2) + " 元"
        : "委托已受理，状态：" + st.text + (order.reason ? "（" + order.reason + "）" : "")) +
        (order.order_id ? " · 委托号 " + order.order_id : ""), fill ? "ok" : "info");
      /* 成交量/价格保留，数量清空，便于连续下单 */
      c.querySelector("#td-qty").value = "";
      await this.load();
    } catch (e) {
      this.msg("委托被拒绝或提交失败：" + UI.apiErrorText(e), "err");
      Toast.err("委托失败：" + UI.apiErrorText(e));
    } finally {
      restore();
    }
  },

  async cancel(orderId, btn) {
    const o = (this.state.orders || []).find((x) => x.order_id === orderId) || {};
    const ok = await Dialog.confirm({
      title: "撤销委托",
      confirmText: "撤单",
      danger: true,
      html: `确认撤销委托 <span class="mono">${fmt.esc(orderId)}</span>（${
        o.side === "sell" ? "卖出" : "买入"} ${fmt.esc(o.code)} ${fmt.int(o.qty)} 股）？<br>
        <span class="flat">撤单仅作用于本地模拟委托。</span>`,
    });
    if (!ok) return;
    const restore = UI.busy(btn, true, "撤单中…");
    try {
      const json = await API.cancelOrder(orderId);
      const order = (json.data) || {};
      Toast.ok("已撤单：" + orderId + (order.status ? "（状态 " + order.status + "）" : ""));
      this.msg("撤单成功：" + orderId +
        (order.code ? order.code + " " + fmt.int(order.qty) + " 股" : "") +
        (order.reason ? " · " + order.reason : ""), "ok");
      await this.load();
    } catch (e) {
      Toast.err("撤单失败：" + UI.apiErrorText(e));
      this.msg("撤单失败：" + UI.apiErrorText(e), "err");
      restore();
    }
  },

  async reset() {
    const ok = await Dialog.confirm({
      title: "重置模拟盘",
      confirmText: "重置",
      danger: true,
      html: `将清空模拟委托 / 成交记录并把模拟账户恢复为初始资金。<br>
        <span class="flat">该操作不可撤销，仅影响本地模拟盘数据。</span>`,
    });
    if (!ok) return;
    const btn = this.container.querySelector("#td-reset");
    const restore = UI.busy(btn, true, "重置中…");
    try {
      const json = await API.resetPaper();
      const acc = json.data || {};
      Toast.ok("模拟盘已重置，总资产 " + fmt.num(acc.total_assets, 2) + " 元");
      this.msg("模拟盘已重置：可用现金 " +
        fmt.num(acc.available_cash !== undefined ? acc.available_cash : acc.cash, 2) + " 元", "ok");
      await this.load();
    } catch (e) {
      Toast.err("重置失败：" + UI.apiErrorText(e));
      this.msg("重置失败：" + UI.apiErrorText(e) + "（模拟盘接口可能尚未实现）", "err");
    } finally {
      restore();
    }
  },
};

window.TradeView = TradeView;
