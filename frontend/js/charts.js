/*
 * charts.js — ECharts 封装（统一深色主题、红涨绿跌）
 * 职责：
 *   1. 统一配色（CHART_COLORS）与坐标轴/提示框基线，业务视图只传数据
 *   2. 提供各视图所需图表：净值、回撤、月度收益、板块、K 线、资产配置、权益曲线
 *   3. 健壮性：echarts 缺失 / 容器尺寸为 0 / 数据为空时降级为占位文案，不抛错
 *   4. 实例登记 + resizeAll（窗口缩放与视图切换后重算尺寸）
 * 约定：调用方负责在容器上调用 Charts.placeholder() 处理空数据态。
 */
"use strict";

const CHART_COLORS = {
  up: "#f6465d",
  down: "#2ebd85",
  brand: "#4f8cff",
  brandSoft: "#7fb0ff",
  benchmark: "#93a1b8",
  warn: "#f0b90b",
  grid: "#26324d",
  text: "#93a1b8",
};

const axisBase = {
  axisLine: { lineStyle: { color: CHART_COLORS.grid } },
  axisLabel: { color: CHART_COLORS.text, fontSize: 11 },
  splitLine: { lineStyle: { color: "rgba(38,50,77,0.45)" } },
};

function tooltipBase() {
  return {
    trigger: "axis",
    backgroundColor: "rgba(17,24,39,0.92)",
    borderColor: CHART_COLORS.grid,
    textStyle: { color: "#e8edf5", fontSize: 12 },
  };
}

const Charts = {
  _instances: {},

  /* 容器占位（空数据 / 库缺失 / 接口失败），替代空白画布 */
  placeholder(domId, text, hint) {
    const dom = document.getElementById(domId);
    if (!dom) return;
    this.dispose(domId);
    dom.innerHTML = `<div class="chart-placeholder" role="status">
      <div class="cp-title">${fmt.esc(text || "暂无数据")}</div>
      ${hint ? `<div class="cp-hint">${fmt.esc(hint)}</div>` : ""}</div>`;
  },

  hasData(series) {
    return Array.isArray(series) && series.length > 0;
  },

  init(domId, option) {
    const dom = document.getElementById(domId);
    if (!dom) return null;
    if (typeof echarts === "undefined") {
      this.placeholder(domId, "图表库未加载", "请检查 frontend/vendor/echarts.min.js 是否存在");
      return null;
    }
    if (!dom.clientWidth || !dom.clientHeight) {
      /* 容器被隐藏（如未激活的视图）时不初始化，切回视图后由视图重新渲染 */
      return null;
    }
    this.dispose(domId);
    dom.innerHTML = "";
    const chart = echarts.init(dom);
    try {
      chart.setOption(option);
    } catch (e) {
      console.error("chart setOption failed:", domId, e);
      this.placeholder(domId, "图表渲染失败", String(e && e.message ? e.message : e));
      return null;
    }
    this._instances[domId] = chart;
    return chart;
  },

  dispose(domId) {
    const chart = this._instances[domId];
    if (!chart) return;
    try { chart.dispose(); } catch (e) { /* 已销毁 */ }
    delete this._instances[domId];
  },

  disposeAll() {
    Object.keys(this._instances).forEach((id) => this.dispose(id));
  },

  /* 尺寸变化后统一重算（窗口 resize / 视图切换） */
  resizeAll() {
    Object.keys(this._instances).forEach((domId) => {
      const chart = this._instances[domId];
      if (!chart) return;
      if (typeof chart.isDisposed === "function" && chart.isDisposed()) return;
      try {
        chart.resize();
      } catch (e) {
        console.warn("chart resize failed:", domId, e);
      }
    });
  },

  /* 净值曲线：策略 vs 基准 */
  nav(domId, dates, stratValues, benchValues) {
    if (!this.hasData(dates)) return this.placeholder(domId, "暂无净值数据", "回测区间内没有可用交易日");
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        valueFormatter: (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(4)),
      },
      legend: { data: ["策略净值", "基准净值"], textStyle: { color: CHART_COLORS.text } },
      grid: { left: 50, right: 20, top: 40, bottom: 40 },
      xAxis: { type: "category", data: dates, ...axisBase },
      yAxis: { type: "value", scale: true, ...axisBase },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [
        {
          name: "策略净值", type: "line", data: stratValues,
          showSymbol: false, lineStyle: { width: 1.8, color: CHART_COLORS.brand },
          areaStyle: { color: "rgba(79,140,255,0.10)" },
          itemStyle: { color: CHART_COLORS.brand },
        },
        {
          name: "基准净值", type: "line", data: benchValues,
          showSymbol: false, lineStyle: { width: 1.4, color: CHART_COLORS.benchmark, type: "dashed" },
          itemStyle: { color: CHART_COLORS.benchmark },
        },
      ],
    });
  },

  /* 回撤曲线 */
  drawdown(domId, dates, ddValues) {
    if (!this.hasData(dates)) return this.placeholder(domId, "暂无回撤数据");
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        valueFormatter: (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2) + "%"),
      },
      grid: { left: 50, right: 20, top: 30, bottom: 40 },
      xAxis: { type: "category", data: dates, ...axisBase },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: "{value}%" } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [
        {
          name: "回撤", type: "line", data: ddValues,
          showSymbol: false, lineStyle: { width: 1.2, color: CHART_COLORS.down },
          areaStyle: { color: "rgba(46,189,133,0.18)" },
          itemStyle: { color: CHART_COLORS.down },
        },
      ],
    });
  },

  /* 月度收益柱状图 */
  monthly(domId, months, rets) {
    if (!this.hasData(months)) return this.placeholder(domId, "暂无月度收益数据", "回测区间不足一个自然月");
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        valueFormatter: (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2) + "%"),
      },
      grid: { left: 50, right: 20, top: 30, bottom: 60 },
      xAxis: {
        type: "category", data: months, ...axisBase,
        axisLabel: { ...axisBase.axisLabel, rotate: 45 },
      },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: "{value}%" } },
      series: [
        {
          name: "月度收益", type: "bar", data: rets,
          itemStyle: {
            color: (p) => (p.value >= 0 ? CHART_COLORS.up : CHART_COLORS.down),
            borderRadius: [3, 3, 0, 0],
          },
        },
      ],
    });
  },

  /* 板块涨幅横向条形图：extras 为与 names 等长的补充文案（如「领涨：贵州茅台」） */
  sectors(domId, names, values, extras) {
    if (!this.hasData(names)) return this.placeholder(domId, "暂无板块数据");
    const extra = extras || [];
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        formatter: (params) => {
          const p = Array.isArray(params) ? params[0] : params;
          const i = p.dataIndex;
          return `${fmt.esc(p.name)}<br/>涨幅：${fmt.pct(p.value, true)}` +
            (extra[i] ? `<br/>${fmt.esc(extra[i])}` : "");
        },
      },
      grid: { left: 76, right: 44, top: 12, bottom: 24 },
      xAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: "{value}%" } },
      yAxis: {
        type: "category", data: names, ...axisBase,
        axisLabel: { color: CHART_COLORS.text, fontSize: 11.5 },
      },
      series: [
        {
          type: "bar", data: values, barWidth: 11,
          itemStyle: {
            color: (p) => (p.value >= 0 ? CHART_COLORS.up : CHART_COLORS.down),
            borderRadius: 6,
          },
          label: {
            show: true, position: "right",
            formatter: (p) => fmt.pct(p.value, true),
            color: CHART_COLORS.text, fontSize: 11,
          },
        },
      ],
    });
  },

  /* K 线图（含 MA 均线） */
  kline(domId, klines) {
    if (!this.hasData(klines)) return this.placeholder(domId, "暂无 K 线数据", "该标的在所选区间内没有行情记录");
    const dates = klines.map((k) => k.date);
    const ohlc = klines.map((k) => [k.open, k.close, k.low, k.high]);
    const vols = klines.map((k) => k.volume_wan);
    const closes = klines.map((k) => k.close);
    const ma = (n) =>
      closes.map((_, i) => {
        if (i < n - 1) return null;
        const seg = closes.slice(i - n + 1, i + 1);
        return +(seg.reduce((a, b) => a + b, 0) / n).toFixed(2);
      });
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        trigger: "axis",
        axisPointer: { type: "cross" },
      },
      legend: { data: ["K线", "MA5", "MA20", "MA60"], textStyle: { color: CHART_COLORS.text }, top: 0 },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 60, right: 20, top: 34, height: "62%" },
        { left: 60, right: 20, bottom: 34, height: "15%" },
      ],
      xAxis: [
        { type: "category", data: dates, ...axisBase, gridIndex: 0, boundaryGap: true },
        { type: "category", data: dates, ...axisBase, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      yAxis: [
        { scale: true, ...axisBase, gridIndex: 0, splitLine: { show: true, lineStyle: { color: "rgba(38,50,77,0.45)" } } },
        { scale: true, ...axisBase, gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 55, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], start: 55, end: 100, bottom: 8, height: 16 },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: ohlc,
          itemStyle: {
            color: CHART_COLORS.up, color0: CHART_COLORS.down,
            borderColor: CHART_COLORS.up, borderColor0: CHART_COLORS.down,
          },
        },
        { name: "MA5", type: "line", data: ma(5), showSymbol: false, lineStyle: { width: 1, color: "#f0b90b" }, smooth: true, itemStyle: { color: "#f0b90b" } },
        { name: "MA20", type: "line", data: ma(20), showSymbol: false, lineStyle: { width: 1, color: "#7fb0ff" }, smooth: true, itemStyle: { color: "#7fb0ff" } },
        { name: "MA60", type: "line", data: ma(60), showSymbol: false, lineStyle: { width: 1, color: "#c084fc" }, smooth: true, itemStyle: { color: "#c084fc" } },
        {
          name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols,
          itemStyle: { color: "rgba(79,140,255,0.35)" },
        },
      ],
    });
  },

  /* 资产配置环形图 */
  allocation(domId, items) {
    const list = (items || []).filter((x) => x && x.name && Number(x.value) > 0);
    if (!list.length) return this.placeholder(domId, "暂无资产配置数据", "录入持仓或设置现金后显示");
    this.init(domId, {
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(17,24,39,0.92)",
        borderColor: CHART_COLORS.grid,
        textStyle: { color: "#e8edf5" },
        formatter: (p) => `${fmt.esc(p.name)}<br/>${fmt.money(p.value)} 元（${p.percent}%）`,
      },
      legend: { type: "scroll", orient: "vertical", right: 0, top: "middle", textStyle: { color: CHART_COLORS.text, fontSize: 12 } },
      color: ["#93a1b8", "#4f8cff", "#f6465d", "#2ebd85", "#f0b90b", "#c084fc", "#58c7f5", "#ff8fb0", "#8bd3a6"],
      series: [
        {
          name: "资产配置", type: "pie", radius: ["42%", "68%"],
          center: ["35%", "50%"],
          label: { color: CHART_COLORS.text, fontSize: 11, formatter: "{b}\n{d}%" },
          data: list,
        },
      ],
    });
  },

  /* 账户权益曲线 */
  equity(domId, dates, values) {
    if (!this.hasData(dates)) return this.placeholder(domId, "暂无权益数据", "历史快照累积中");
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        valueFormatter: (v) => (v === null || v === undefined ? "—" : "¥ " + fmt.money(v)),
      },
      grid: { left: 80, right: 20, top: 30, bottom: 40 },
      xAxis: { type: "category", data: dates, ...axisBase },
      yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: (v) => "¥" + (v / 10000).toFixed(1) + "万" } },
      dataZoom: [{ type: "inside" }],
      series: [
        {
          name: "账户权益", type: "line", data: values,
          showSymbol: false, lineStyle: { width: 1.8, color: CHART_COLORS.brand },
          areaStyle: { color: "rgba(79,140,255,0.12)" },
          itemStyle: { color: CHART_COLORS.brand },
        },
      ],
    });
  },

  /* L2 资金流分时：累计净额折线（values 单位：万元；正净流入红、净流出绿） */
  flowNet(domId, times, values) {
    if (!this.hasData(times)) {
      return this.placeholder(domId, "暂无分时资金流", "该标的当前没有可聚合的逐笔成交样本");
    }
    const data = (values || []).map((v) => (v === null || v === undefined ? null : Number(v)));
    const last = data.length ? data[data.length - 1] : 0;
    const color = (last === null || last >= 0) ? CHART_COLORS.up : CHART_COLORS.down;
    this.init(domId, {
      tooltip: {
        ...tooltipBase(),
        valueFormatter: (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2) + " 万元"),
      },
      grid: { left: 68, right: 20, top: 30, bottom: 46 },
      xAxis: { type: "category", data: times, ...axisBase },
      yAxis: { type: "value", scale: true, ...axisBase },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [
        {
          name: "累计净额(万元)", type: "line", data: data,
          showSymbol: false, lineStyle: { width: 1.6, color: color },
          itemStyle: { color: color },
          areaStyle: { color: "rgba(79,140,255,0.08)" },
        },
      ],
    });
  },
};

window.Charts = Charts;
