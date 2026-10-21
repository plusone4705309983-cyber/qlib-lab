"use strict";

const state = {
  catalog: [],
  market: null,
  symbol: null,
  payload: null,
  mode: "real",        // real | hfq
  log: false,
  showAnom: true,
  chart: null,
  indicator: "",       // "" | ma | ema | boll | macd | rsi | atr
  indData: null,       // 当前标的+口径的指标数据
  indCache: {},        // market|symbol|mode -> 指标数据
  anomOnlySig: false,  // 异常清单: 只看信号
  anomYears: 0,        // 异常清单: 时间范围 (0=全部)
};

const UP = "#d94838";
const DOWN = "#1a9e5c";
const ERROR = "#e5484d";
const INFO = "#8a9099";

// 各指标分组: overlay 叠加在价格主图, sub 单独子面板
const IND_META = {
  ma: { kind: "overlay", lines: [
    ["ma5", "MA5", "#f2a22d"], ["ma10", "MA10", "#3b7dd8"],
    ["ma20", "MA20", "#9b59b6"], ["ma60", "MA60", "#5a6b7b"]] },
  ema: { kind: "overlay", lines: [
    ["ema12", "EMA12", "#e67e22"], ["ema26", "EMA26", "#2980b9"]] },
  boll: { kind: "overlay", lines: [
    ["boll_up", "上轨", "#c0392b"], ["boll_mid", "中轨", "#7f8c8d"], ["boll_low", "下轨", "#27ae60"]] },
  macd: { kind: "sub", pane: "macd" },
  rsi: { kind: "sub", pane: "rsi" },
  atr: { kind: "sub", pane: "atr" },
};

const $ = (id) => document.getElementById(id);
const MQ_NARROW = window.matchMedia("(max-width: 760px)");
const isShort = () => window.innerHeight < 620;
let viewMode = "";

function fmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
function fmtInt(v) {
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- 侧栏
async function loadSymbols() {
  const res = await fetch("/api/symbols");
  state.catalog = await res.json();
  $("pool-count").textContent = `(${state.catalog.length})`;

  const list = $("symbol-list");
  list.innerHTML = "";
  let curMarket = null;
  for (const s of state.catalog) {
    if (s.market !== curMarket) {
      curMarket = s.market;
      const h = document.createElement("div");
      h.className = "market-head";
      h.textContent = s.market_label;
      list.appendChild(h);
    }
    const n = (s.anomaly_counts && s.anomaly_counts.error) || 0;
    const div = document.createElement("div");
    div.className = "sym";
    div.dataset.market = s.market;
    div.dataset.symbol = s.qlib;
    div.innerHTML =
      `<span><span class="s-name">${s.name}</span> ` +
      `<span class="s-code">${s.qlib.toUpperCase()}</span></span>` +
      `<span class="s-badge ${n ? "" : "zero"}">${n}</span>`;
    div.onclick = () => selectSymbol(s.market, s.qlib);
    list.appendChild(div);
  }
  const first = state.catalog[0];
  if (first && !state.symbol) selectSymbol(first.market, first.qlib);
}

function markActive() {
  document.querySelectorAll(".sym").forEach((el) => {
    const on = el.dataset.market === state.market && el.dataset.symbol === state.symbol;
    el.classList.toggle("active", on);
  });
}

// ---------------------------------------------------------------- 数据
async function ensureIndicators() {
  if (!state.indicator) {
    state.indData = null;
    return;
  }
  const key = `${state.market}|${state.symbol}|${state.mode}`;
  if (state.indCache[key]) {
    state.indData = state.indCache[key];
    return;
  }
  try {
    const url = `/api/indicators?market=${state.market}&symbol=${state.symbol}&mode=${state.mode}`;
    const d = await (await fetch(url)).json();
    if (d.error) {
      console.warn("指标计算失败:", d.error);
      state.indData = null;
      return;
    }
    state.indCache[key] = d;
    state.indData = d;
  } catch (e) {
    console.warn("指标加载失败:", e);
    state.indData = null;
  }
}

async function selectSymbol(market, symbol) {
  state.market = market;
  state.symbol = symbol;
  markActive();
  if (MQ_NARROW.matches) $("sidebar").classList.add("collapsed");
  $("bin-result").innerHTML = "";
  clearFactor("已切换标的，点『计算』重新分析");
  const res = await fetch(`/api/kline?market=${market}&symbol=${symbol}`);
  const data = await res.json();
  if (state.market !== market || state.symbol !== symbol) return;
  if (data.error) {
    $("summary").textContent = data.error;
    return;
  }
  state.payload = data;
  state.indData = null;
  $("cur-name").textContent = data.meta.name;
  $("cur-code").textContent = `${data.meta.market_label} · ${data.meta.symbol.toUpperCase()} · ${data.meta.currency}`;
  renderSummary(data);
  renderAnomalies(data);
  renderBacktest(data);
  renderHolders(data);
  await ensureIndicators();
  if (state.market === market && state.symbol === symbol) renderChart(data);
}

function renderSummary(p) {
  const st = p.stats;
  const c = st.anomaly_counts || {};
  const parts = [
    `行数 <b>${fmtInt(st.rows)}</b>`,
    `区间 <b>${st.start} ~ ${st.end}</b>`,
  ];
  if (c.factor_drop) parts.push(`<b style="color:${ERROR}">因子下调 ${c.factor_drop}</b>`);
  if (c.close_out_of_range) parts.push(`<b style="color:${ERROR}">OHLC 越界 ${c.close_out_of_range}</b>`);
  if (c.missing_day) parts.push(`缺失日 ${c.missing_day}`);
  if (c.big_move) parts.push(`异动 ${c.big_move}`);
  if (c.us_unadjusted_split) parts.push(`未复权拆股 ${c.us_unadjusted_split}`);
  $("summary").innerHTML = parts.join("　·　");
}

// ---------------------------------------------------------------- 图表
function renderChart(p) {
  if (!state.chart) state.chart = echarts.init($("chart"));
  const dates = p.dates;
  const src = state.mode === "hfq" ? p.hfq : p.real;
  const ohlc = dates.map((_, i) => [src.open[i], src.close[i], src.low[i], src.high[i]]);
  const volColors = dates.map((_, i) => src.close[i] >= src.open[i] ? UP : DOWN);
  const volume = p.volume.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } }));
  const factor = p.factor;

  const idxOf = {};
  dates.forEach((d, i) => { idxOf[d] = i; });
  const errPts = [];
  const infoPts = [];
  const buyPts = [];
  const sellPts = [];
  if (state.showAnom) {
    for (const a of p.anomalies) {
      const i = idxOf[a.date];
      if (i === undefined) continue;
      if (a.type.startsWith("signal_")) {
        // 技术信号: 看多标记在下方, 看空标记在上方
        const bullish = a.level === "info";
        const y = bullish ? src.low[i] * 0.97 : src.high[i] * 1.03;
        const pt = { value: [i, y], name: a.date, anom: a };
        (bullish ? buyPts : sellPts).push(pt);
        continue;
      }
      const y = src.high[i] * 1.03;
      const pt = { value: [i, y], name: a.date, anom: a };
      (a.level === "error" ? errPts : infoPts).push(pt);
    }
  }

  const ind = state.indData && state.indicator ? state.indData : null;
  const meta = ind ? IND_META[state.indicator] : null;
  const hasSub = !!meta && meta.kind === "sub";
  const hasOverlay = !!meta && meta.kind === "overlay";

  const narrow = MQ_NARROW.matches;
  const short = isShort();
  const tiny = narrow || short;
  viewMode = (narrow ? "n" : "d") + (short ? "s" : "");
  const barsShown = narrow ? 60 : (short ? 120 : 260);
  const start = Math.max(0, dates.length - barsShown);
  const lastAxis = hasSub ? 3 : 2;
  const axisIdx = [0, 1, 2].concat(hasSub ? [3] : []);
  const zoom = [
    { type: "inside", xAxisIndex: axisIdx, startValue: start, endValue: dates.length - 1 },
    { type: "slider", xAxisIndex: axisIdx, bottom: 4, height: tiny ? 14 : 18, startValue: start, endValue: dates.length - 1 },
  ];

  // 指标按日期对齐到 K 线 (指标日期 == CSV 交易日, 这里再按日期映射兜底)
  let indGet = null;
  if (ind) {
    const im = {};
    ind.dates.forEach((d, k) => { im[d] = k; });
    indGet = (key) => dates.map((d) => {
      const k = im[d];
      return k === undefined ? null : ind.series[key][k];
    });
  }

  let grids;
  if (hasSub) {
    if (narrow) {
      grids = [
        { left: 46, right: 8, top: 16, height: "40%" },
        { left: 46, right: 8, top: "58%", height: "8%" },
        { left: 46, right: 8, top: "68%", height: "10%" },
        { left: 46, right: 8, top: "80%", height: "13%" },
      ];
    } else if (short) {
      grids = [
        { left: 54, right: 12, top: 10, height: "42%" },
        { left: 54, right: 12, top: "54%", height: "7%" },
        { left: 54, right: 12, top: "63%", height: "9%" },
        { left: 54, right: 12, top: "74%", height: "13%" },
      ];
    } else {
      grids = [
        { left: 64, right: 26, top: 18, height: "37%" },
        { left: 64, right: 26, top: "57%", height: "8%" },
        { left: 64, right: 26, top: "67%", height: "10%" },
        { left: 64, right: 26, top: "79%", height: "13%" },
      ];
    }
  } else if (narrow) {
    grids = [
      { left: 46, right: 8, top: 18, height: "50%" },
      { left: 46, right: 8, top: "66%", height: "9%" },
      { left: 46, right: 8, top: "79%", height: "13%" },
    ];
  } else if (short) {
    grids = [
      { left: 54, right: 12, top: 12, height: "52%" },
      { left: 54, right: 12, top: "64%", height: "8%" },
      { left: 54, right: 12, top: "76%", height: "12%" },
    ];
  } else {
    grids = [
      { left: 64, right: 26, top: 24, height: "46%" },
      { left: 64, right: 26, top: "56%", height: "10%" },
      { left: 64, right: 26, top: "71%", height: "16%" },
    ];
  }
  const xAxes = axisIdx.map((n) => ({
    type: "category",
    gridIndex: n,
    data: dates,
    boundaryGap: true,
    axisLine: { lineStyle: { color: "#c8ccd2" } },
    axisLabel: { show: n === lastAxis, color: "#6b7280", fontSize: tiny ? 10 : 11, hideOverlap: true },
    axisTick: { show: false },
    splitLine: { show: false },
  }));
  const yAxes = [
    { scale: true, gridIndex: 0, log: state.log, splitLine: { lineStyle: { color: "#eef0f3" } } },
    { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } },
    { scale: true, gridIndex: 2, splitNumber: 3, splitLine: { lineStyle: { color: "#eef0f3" } } },
  ];
  if (hasSub) {
    if (meta.pane === "rsi") {
      yAxes.push({ gridIndex: 3, min: 0, max: 100, splitNumber: 2, splitLine: { lineStyle: { color: "#eef0f3" } } });
    } else if (meta.pane === "atr") {
      yAxes.push({ scale: true, gridIndex: 3, splitNumber: 2, splitLine: { lineStyle: { color: "#eef0f3" } } });
    } else {
      yAxes.push({ scale: true, gridIndex: 3, splitNumber: 3, splitLine: { lineStyle: { color: "#eef0f3" } } });
    }
  }

  const series = [
    {
      type: "candlestick",
      name: "K线",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: ohlc,
      itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
    },
    { type: "bar", name: "成交量", xAxisIndex: 1, yAxisIndex: 1, data: volume },
    {
      type: "line",
      name: "factor",
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: factor,
      showSymbol: false,
      lineStyle: { color: "#2563eb", width: 1.4 },
      areaStyle: { color: "rgba(37,99,235,0.06)" },
    },
  ];

  if (hasOverlay && indGet) {
    for (const [key, label, color] of meta.lines) {
      series.push({
        type: "line",
        name: label,
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: indGet(key),
        showSymbol: false,
        lineStyle: { color: color, width: 1.2 },
        z: 5,
      });
    }
  }
  if (hasSub && indGet) {
    const x = { xAxisIndex: 3, yAxisIndex: 3 };
    if (meta.pane === "macd") {
      const hist = indGet("macd_hist");
      series.push({
        type: "bar",
        name: "MACD",
        ...x,
        data: hist.map((v) => ({ value: v, itemStyle: { color: v === null ? INFO : (v >= 0 ? UP : DOWN) } })),
        barWidth: "60%",
      });
      series.push({ type: "line", name: "DIF", ...x, data: indGet("macd_dif"), showSymbol: false, lineStyle: { color: "#f2a22d", width: 1.2 } });
      series.push({ type: "line", name: "DEA", ...x, data: indGet("macd_dea"), showSymbol: false, lineStyle: { color: "#3b7dd8", width: 1.2 } });
    } else if (meta.pane === "rsi") {
      const cols = [["rsi6", "RSI6", "#f2a22d"], ["rsi12", "RSI12", "#3b7dd8"], ["rsi24", "RSI24", "#9b59b6"]];
      for (const [key, label, color] of cols) {
        series.push({ type: "line", name: label, ...x, data: indGet(key), showSymbol: false, lineStyle: { color, width: 1.2 } });
      }
    } else if (meta.pane === "atr") {
      series.push({ type: "line", name: "ATR(14)", ...x, data: indGet("atr14"), showSymbol: false, lineStyle: { color: "#e67e22", width: 1.2 } });
    }
  }

  series.push(
    {
      type: "scatter",
      name: "错误",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: errPts,
      symbol: "pin",
      symbolSize: 26,
      itemStyle: { color: ERROR },
      z: 10,
    },
    {
      type: "scatter",
      name: "提示",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: infoPts,
      symbol: "circle",
      symbolSize: 9,
      itemStyle: { color: INFO, opacity: 0.85 },
      z: 9,
    },
    {
      type: "scatter",
      name: "看多信号",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: buyPts,
      symbol: "triangle",
      symbolSize: 10,
      symbolOffset: [0, -2],
      itemStyle: { color: UP },
      z: 11,
    },
    {
      type: "scatter",
      name: "看空信号",
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: sellPts,
      symbol: "triangle",
      symbolRotate: 180,
      symbolSize: 10,
      itemStyle: { color: DOWN },
      z: 11,
    }
  );

  let indTip = [];
  if (hasOverlay && indGet) {
    indTip = meta.lines.map(([key, label]) => [label, indGet(key)]);
  } else if (hasSub && indGet) {
    if (meta.pane === "macd") indTip = [["DIF", indGet("macd_dif")], ["DEA", indGet("macd_dea")], ["MACD", indGet("macd_hist")]];
    else if (meta.pane === "rsi") indTip = [["RSI6", indGet("rsi6")], ["RSI12", indGet("rsi12")], ["RSI24", indGet("rsi24")]];
    else if (meta.pane === "atr") indTip = [["ATR(14)", indGet("atr14")]];
  }

  state.chart.setOption({
    animation: false,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: zoom,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: function (ps) {
        if (!ps || !ps.length) return "";
        const i = ps[0].dataIndex;
        const c = ohlc[i];
        const a = p.anomalies.filter((x) => x.date === dates[i]);
        let html =
          `<b>${dates[i]}</b><br/>` +
          `开 ${fmt(c[0])}　高 ${fmt(c[3])}<br/>` +
          `低 ${fmt(c[2])}　收 ${fmt(c[1])}<br/>` +
          `量 ${fmtInt(p.volume[i])}　因子 ${factor[i]}`;
        if (indTip.length) {
          html += "<br/>" + indTip.map(([label, arr]) => {
            const v = arr[i];
            return `· ${label} ${v === null || v === undefined ? "—" : fmt(v)}`;
          }).join("　");
        }
        if (a.length) {
          html += "<br/><br/>" + a.map((x) => `· <span style="color:${x.level === "error" ? ERROR : INFO}">${x.msg}</span>`).join("<br/>");
        }
        return html;
      },
    },
    series: series,
  }, true);
}

// ---------------------------------------------------------------- 异常表
function signalCutoff(p) {
  if (!state.anomYears || !p.dates.length) return null;
  const last = p.dates[p.dates.length - 1];
  const y = parseInt(last.slice(0, 4), 10) - state.anomYears;
  return `${y}${last.slice(4)}`;
}

function filteredAnomalies(p) {
  const cutoff = signalCutoff(p);
  return p.anomalies.filter((a) => {
    if (state.anomOnlySig && !a.type.startsWith("signal_")) return false;
    if (cutoff && a.date < cutoff) return false;
    return true;
  });
}

function renderAnomalies(p) {
  const box = $("anom-table");
  const list = filteredAnomalies(p);
  $("anom-count").textContent = list.length === p.anomalies.length
    ? `共 ${list.length} 条` : `筛选 ${list.length} / 共 ${p.anomalies.length} 条`;
  if (!list.length) {
    box.innerHTML = `<div class="empty">当前筛选下无记录</div>`;
    return;
  }
  const lvlLabel = { error: "错误", warn: "警示", info: "提示" };
  const rows = list
    .map((a) => {
      const isSig = a.type.startsWith("signal_");
      const cls = isSig ? (a.level === "warn" ? "signal-warn" : "signal") : a.level;
      const label = isSig ? "信号" : (lvlLabel[a.level] || "提示");
      return (
        `<tr><td class="date">${a.date}</td>` +
        `<td><span class="tag ${cls}">${label}</span></td>` +
        `<td>${a.msg}</td></tr>`
      );
    })
    .join("");
  box.innerHTML = `<table><thead><tr><th>日期</th><th>级别</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderBacktest(p) {
  const wrap = $("bt-wrap");
  const box = $("bt-table");
  const bt = p.signal_backtest;
  if (!bt || !Object.keys(bt).length) {
    wrap.style.display = "none";
    box.innerHTML = "";
    return;
  }
  wrap.style.display = "";
  const hs = (p.signal_horizons && p.signal_horizons.length) ? p.signal_horizons : [5, 10, 20];
  const labels = p.signal_labels || {};
  const order = ["ma_gold_cross", "ma_death_cross", "macd_cross_up", "macd_cross_down", "rsi_overbought", "rsi_oversold"];
  const types = order.filter((t) => bt[t]).concat(Object.keys(bt).filter((t) => !order.includes(t)));
  const head = `<tr><th>信号</th><th>方向</th>` + hs.map((h) => `<th>${h}日胜率</th>`).join("") + `</tr>`;
  const body = types.map((t) => {
    const bullish = bt[t][hs[0]] ? bt[t][hs[0]].bullish : false;
    const dir = `<span class="bt-dir ${bullish ? "up" : "down"}">${bullish ? "看多" : "看空"}</span>`;
    const cells = hs.map((h) => {
      const c = bt[t][h];
      if (!c || !c.n) return `<td class="bt-empty">—</td>`;
      const wr = (c.win_rate * 100).toFixed(1);
      const ar = (c.avg_ret * 100).toFixed(1);
      const col = c.win_rate >= 0.55 ? "bt-good" : (c.win_rate <= 0.45 ? "bt-bad" : "");
      return `<td class="${col}"><b>${wr}%</b><span class="bt-sub">均值 ${ar >= 0 ? "+" : ""}${ar}% · n=${c.n}</span></td>`;
    }).join("");
    return `<tr><td>${labels[t] || t}</td><td>${dir}</td>${cells}</tr>`;
  }).join("");
  box.innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------- 股东穿透
function clearHolders() {
  $("hold-wrap").style.display = "none";
  $("hold-meta").textContent = "";
  $("hold-table").innerHTML = "";
  $("hold-detail").innerHTML = "";
}

function dirCls(dir) {
  if (dir === "减持") return "down";
  if (dir === "增持" || dir === "新进") return "up";
  return "";
}
function dirHtml(direction, change) {
  const cls = dirCls(direction);
  const chg = change ? `<span class="hchg">${change > 0 ? "+" : ""}${fmtInt(change)}</span>` : "";
  return `<span class="hdir ${cls}">${esc(direction || "—")}</span>${chg}`;
}

async function renderHolders(p) {
  if (p.meta.market !== "cn") { clearHolders(); return; }
  const wrap = $("hold-wrap");
  wrap.style.display = "";
  $("hold-detail").innerHTML = "";
  $("hold-table").innerHTML = "";
  $("hold-meta").textContent = "加载中…";
  let d;
  try {
    const res = await fetch(`/api/holders?market=${p.meta.market}&symbol=${p.meta.symbol}`);
    d = await res.json();
  } catch (e) {
    $("hold-meta").textContent = "加载失败: " + e.message;
    return;
  }
  if (state.symbol !== p.meta.symbol || state.market !== p.meta.market) return;
  if (d.error) { $("hold-meta").textContent = d.error; return; }
  $("hold-meta").textContent =
    `报告期 ${d.end_date} · 共 ${d.count} 个主体 (十大股东 ∪ 十大流通股东)`;
  const rows = (d.holders || []).map((h) => {
    const tag = h.special
      ? `<span class="htag sp">通道</span>`
      : (h.is_org ? `<span class="htag org">机构</span>` : `<span class="htag person">自然人</span>`);
    const ratio = h.hold_ratio === null || h.hold_ratio === undefined
      ? "—" : h.hold_ratio.toFixed(2) + "%";
    return `<tr>` +
      `<td class="rank">${h.rank == null ? "" : h.rank}</td>` +
      `<td><span class="hk-name" data-name="${esc(h.name)}" title="点击查看其持仓">${esc(h.name)}</span> ${tag}</td>` +
      `<td class="num">${fmtInt(h.hold_num)}</td>` +
      `<td class="num">${ratio}</td>` +
      `<td>${dirHtml(h.direction, h.change)}</td>` +
      `</tr>`;
  }).join("");
  $("hold-table").innerHTML =
    `<table><thead><tr><th>#</th><th>股东</th><th>持股数</th><th>占比</th><th>变动</th></tr></thead><tbody>${rows}</tbody></table>`;
  $("hold-table").querySelectorAll(".hk-name").forEach((el) => {
    el.onclick = () => loadHolderStocks(el.dataset.name);
  });
}

async function loadHolderStocks(name) {
  const box = $("hold-detail");
  box.innerHTML = `<div class="hold-loading">加载「${esc(name)}」的持仓…</div>`;
  let d;
  try {
    const res = await fetch(`/api/holder_stocks?name=${encodeURIComponent(name)}`);
    d = await res.json();
  } catch (e) {
    box.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
    return;
  }
  if (d.error) { box.innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
  const pool = {};
  state.catalog.forEach((s) => { pool[s.qlib] = s; });
  const rows = (d.stocks || []).map((s) => {
    const inPool = pool[s.symbol];
    const nameCell = inPool
      ? `<span class="hk-name" data-mkt="${inPool.market}" data-sym="${inPool.qlib}" title="在股票池中, 点击查看K线">${esc(s.name)}</span>`
      : esc(s.name);
    const codeCell = inPool
      ? `<span class="code in-pool">${esc(s.symbol.toUpperCase())}</span>`
      : `<span class="code">${esc(s.symbol.toUpperCase())}</span>`;
    const ratio = s.hold_ratio === null || s.hold_ratio === undefined
      ? "—" : s.hold_ratio.toFixed(2) + "%";
    return `<tr>` +
      `<td>${nameCell}</td>` +
      `<td>${codeCell}</td>` +
      `<td class="num">${fmtInt(s.hold_num)}</td>` +
      `<td class="num">${ratio}</td>` +
      `<td>${dirHtml(s.direction, s.change)}</td>` +
      `</tr>`;
  }).join("");
  const warn = d.special
    ? `<div class="hold-note warn">⚠ 该主体为通道/国家队 (如陆股通名义持有人), 其"持仓"不代表主动投资行为。</div>`
    : "";
  const trunc = d.truncated ? `<div class="hold-note">结果较多, 仅覆盖前若干报告期。</div>` : "";
  box.innerHTML =
    `<div class="hold-dhead"><b>${esc(name)}</b> 的持仓 <span class="muted">共 ${d.count} 只</span>` +
    `<button class="mini-btn" id="hold-close">收起</button></div>` +
    warn + trunc +
    (d.stocks && d.stocks.length
      ? `<table><thead><tr><th>股票</th><th>代码</th><th>持股数</th><th>占比</th><th>变动</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<div class="empty">未查到持仓</div>`);
  $("hold-close").onclick = () => { box.innerHTML = ""; };
  box.querySelectorAll(".hk-name[data-sym]").forEach((el) => {
    el.onclick = () => {
      selectSymbol(el.dataset.mkt, el.dataset.sym);
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
  });
}

// ---------------------------------------------------------------- .bin 校验
async function checkBin() {
  const btn = $("btn-bin");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "读取中…";
  $("bin-result").innerHTML = "";
  try {
    const res = await fetch(`/api/check_bin?market=${state.market}&symbol=${state.symbol}`);
    const d = await res.json();
    if (d.error) {
      $("bin-result").innerHTML = `<span class="bad">${d.error}</span>`;
      return;
    }
    const m = d.match;
    // .bin 是 float32, CSV 存 4 位小数, 末价允许 ~1e-5 的相对舍入差
    const tol = Math.abs(d.csv.last_close) * 1e-4 + 0.01;
    const ok = m.rows && m.start && m.end && Math.abs(m.last_close_diff) < tol;
    const cls = ok ? "ok" : "bad";
    $("bin-result").innerHTML =
      `<span class="${cls}">${ok ? "✓ CSV 与 .bin 一致" : "✗ CSV 与 .bin 不一致"}</span>` +
      `<table><tr><th></th><th>行数</th><th>起始</th><th>末行</th><th>末收盘</th></tr>` +
      `<tr><td>CSV</td><td>${fmtInt(d.csv.rows)}</td><td>${d.csv.start}</td><td>${d.csv.end}</td><td>${fmt(d.csv.last_close)}</td></tr>` +
      `<tr><td>.bin</td><td>${fmtInt(d.bin.rows)}</td><td>${d.bin.start}</td><td>${d.bin.end}</td><td>${fmt(d.bin.last_close)}</td></tr>` +
      `</table>` +
      `<div style="color:#8a9099;margin-top:4px">日历末 ${d.bin.calendar_end}</div>`;
  } catch (e) {
    $("bin-result").innerHTML = `<span class="bad">${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

// ---------------------------------------------------------------- 因子实验室
let factChart = null;
let factBucketChart = null;
const factState = { data: null, busy: false };

const num3 = (v) => {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return fmt(v);
  if (a >= 1) return Number(v).toFixed(3);
  return Number(v).toPrecision(3);
};
const signedPct = (v, nd = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(nd)}%`);
const plainPct = (v, nd = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(nd)}%`);

function factStatus(msg) {
  $("fact-status").textContent = msg || "";
}

function clearFactor(msg) {
  factState.data = null;
  factStatus(msg || "");
  $("fact-current").style.display = "none";
  $("fact-current").innerHTML = "";
  $("fact-ic").innerHTML = "";
  $("fact-table").innerHTML = "";
  if (factChart) { factChart.dispose(); factChart = null; }
  if (factBucketChart) { factBucketChart.dispose(); factBucketChart = null; }
  $("fact-chart").innerHTML = "";
  $("fact-bucket-chart").innerHTML = "";
}

async function runFactor() {
  if (!state.market || !state.symbol) { factStatus("请先在左侧选择一只股票"); return; }
  if (factState.busy) return;
  const expr = $("fact-expr").value.trim();
  if (!expr) { factStatus("请输入因子表达式"); return; }
  const market = state.market;
  const symbol = state.symbol;
  factState.busy = true;
  $("fact-run").disabled = true;
  factStatus("计算中… 首次会启动 qlib 子进程，约 5~15 秒");
  try {
    const url = `/api/factor?market=${market}&symbol=${symbol}&expr=${encodeURIComponent(expr)}`;
    const res = await fetch(url);
    const d = await res.json();
    if (d.error) { factState.data = null; factStatus(`错误：${d.error}`); return; }
    if (state.market !== market || state.symbol !== symbol) return;
    factState.data = d;
    factStatus(
      `样本 ${fmtInt(d.count)} 天 · ${d.dates[0]} ~ ${d.dates[d.dates.length - 1]} · 分组 = 过去 ${d.hist_win} 日滚动分位` +
        (d.note ? ` · ${d.note}` : "")
    );
    renderFactor(d);
  } catch (e) {
    factStatus(`请求失败：${e}`);
  } finally {
    factState.busy = false;
    $("fact-run").disabled = false;
  }
}

function renderFactor(d) {
  renderFactCurrent(d);
  renderFactIC(d);
  renderFactTable(d);
  renderFactChart(d);
  renderFactBucketChart(d);
}

function renderFactCurrent(d) {
  const box = $("fact-current");
  const c = d.current;
  if (!c) { box.style.display = "none"; return; }
  const cls = c.level === "info" ? "up" : c.level === "warn" ? "down" : "";
  const lvl = { info: ["看多", "signal"], warn: ["看空", "signal-warn"], neutral: ["中性", "info"] }[c.level] || ["中性", "info"];
  box.className = `fact-current ${cls}`;
  box.innerHTML = `
    <div class="fc-head">
      <span>当前读数</span><span>${c.date}</span>
      <span class="tag ${lvl[1]}">${lvl[0]}</span>
      <span class="muted">因子 ${num3(c.factor)} · 分位 ${plainPct(c.pct, 1)} · ${c.name}</span>
    </div>
    <div>${esc(c.msg)}</div>`;
  box.style.display = "";
}

function renderFactIC(d) {
  const items = d.horizons.map((h) => {
    const it = d.ic[String(h)] || {};
    if (it.ic === null || it.ic === undefined) return `<span>IC(${h}日) <b>—</b></span>`;
    const cls = it.ic > 0 ? "pos" : it.ic < 0 ? "neg" : "";
    return `<span>IC(${h}日) <b class="${cls}">${it.ic.toFixed(3)}</b> <span class="muted">n=${fmtInt(it.n)}</span></span>`;
  });
  items.push(`<span class="muted">IC = 因子值与未来 N 日收益的 Spearman 秩相关（描述性统计）</span>`);
  $("fact-ic").innerHTML = items.join("");
}

function factCell(c) {
  if (!c || c.avg_ret === null || c.avg_ret === undefined) {
    return `<td class="bt-empty">—</td>`;
  }
  const cls = c.avg_ret > 0 ? "bt-good" : "bt-bad";
  return `<td class="${cls}"><b>${signedPct(c.avg_ret)}</b><span class="bt-sub">胜率 ${plainPct(c.win_rate, 0)} · n=${fmtInt(c.n)}</span></td>`;
}

function renderFactTable(d) {
  const hs = d.horizons;
  const head = `<tr><th>分位组</th><th>组内样本</th><th>因子区间</th><th>历史分位</th>${hs
    .map((h) => `<th>未来 ${h} 日</th>`)
    .join("")}</tr>`;
  const rows = d.buckets.map((b) => {
    const cur = d.current && d.current.group === b.i ? "fact-cur-row" : "";
    const fr = b.factor_lo === null ? "—" : `${num3(b.factor_lo)} ~ ${num3(b.factor_hi)}`;
    const pr = b.pct_lo === null ? "—" : `${plainPct(b.pct_lo, 0)} ~ ${plainPct(b.pct_hi, 0)}`;
    return `<tr class="${cur}"><td>${b.name}</td><td>${fmtInt(b.n)}</td><td>${fr}</td><td>${pr}</td>${hs
      .map((h) => factCell(b.h[String(h)]))
      .join("")}</tr>`;
  });
  const base = `<tr><td>全样本</td><td>—</td><td>—</td><td>—</td>${hs
    .map((h) => factCell(d.overall[String(h)]))
    .join("")}</tr>`;
  $("fact-table").innerHTML = `<table><thead>${head}</thead><tbody>${rows.join("")}${base}</tbody></table>`;
}

function renderFactChart(d) {
  if (!factChart) factChart = echarts.init($("fact-chart"));
  factChart.setOption(
    {
      animation: false,
      grid: { left: 62, right: 62, top: 30, bottom: 34 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const lines = [`<b>${ps[0].axisValue}</b>`];
          ps.forEach((p) => {
            const v = p.seriesName === "因子" ? num3(p.value) : fmt(p.value);
            lines.push(`${p.marker}${p.seriesName}: ${v}`);
          });
          return lines.join("<br>");
        },
      },
      legend: { top: 0, left: 8, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11 } },
      xAxis: {
        type: "category",
        data: d.dates,
        axisLabel: { fontSize: 10, formatter: (v) => (v && v.slice(5) === "01-01" ? v.slice(0, 4) : "") },
        axisTick: { show: false },
      },
      yAxis: [
        { type: "value", name: "因子", scale: true, nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 10 } },
        {
          type: "value",
          name: "真实价",
          scale: true,
          nameTextStyle: { fontSize: 11 },
          axisLabel: { fontSize: 10 },
          splitLine: { show: false },
        },
      ],
      dataZoom: [{ type: "inside" }],
      series: [
        {
          name: "因子",
          type: "line",
          data: d.factor,
          showSymbol: false,
          sampling: "lttb",
          lineStyle: { width: 1.3 },
          color: "#3b7dd8",
          connectNulls: false,
        },
        {
          name: "真实价",
          type: "line",
          yAxisIndex: 1,
          data: d.close_real,
          showSymbol: false,
          sampling: "lttb",
          lineStyle: { width: 1, type: "dashed" },
          color: "#8a9099",
          connectNulls: false,
        },
      ],
    },
    true
  );
  factChart.resize();
}

function renderFactBucketChart(d) {
  if (!factBucketChart) factBucketChart = echarts.init($("fact-bucket-chart"));
  const colors = ["#f2a22d", "#3b7dd8", "#9b59b6"];
  const series = d.horizons.map((h, i) => ({
    name: `未来 ${h} 日`,
    type: "bar",
    data: d.buckets.map((b) => {
      const c = b.h[String(h)];
      return c && c.avg_ret !== null ? +(c.avg_ret * 100).toFixed(3) : null;
    }),
    itemStyle: { color: colors[i % colors.length] },
    barMaxWidth: 34,
  }));
  factBucketChart.setOption(
    {
      animation: false,
      grid: { left: 56, right: 20, top: 34, bottom: 28 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (ps) => {
          const g = d.buckets[ps[0].dataIndex];
          const lines = [`<b>${g.name}</b> <span style="color:#8a9099">样本 ${g.n}</span>`];
          ps.forEach((p) => {
            const c = g.h[String(d.horizons[p.seriesIndex])];
            if (!c || c.avg_ret === null) {
              lines.push(`${p.marker}${p.seriesName}: —`);
            } else {
              lines.push(`${p.marker}${p.seriesName}: ${signedPct(c.avg_ret)} · 胜率 ${plainPct(c.win_rate, 0)} · n=${c.n}`);
            }
          });
          return lines.join("<br>");
        },
      },
      legend: { top: 0, left: 8, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11 } },
      xAxis: {
        type: "category",
        data: d.buckets.map((b) => b.name),
        axisTick: { show: false },
        axisLabel: { fontSize: 11 },
      },
      yAxis: {
        type: "value",
        name: "平均收益 %",
        nameTextStyle: { fontSize: 11 },
        axisLabel: { fontSize: 10, formatter: "{value}%" },
      },
      series,
    },
    true
  );
  factBucketChart.resize();
}

$("fact-run").onclick = runFactor;
$("fact-expr").onkeydown = (e) => {
  if (e.key === "Enter") runFactor();
};
$("fact-preset").onchange = (e) => {
  const v = e.target.value;
  if (!v) return;
  $("fact-expr").value = v;
  e.target.value = ""; // 复位, 便于重复选择同一预置重新计算
  runFactor();
};

// ---------------------------------------------------------------- 事件
$("price-mode").onchange = async (e) => {
  state.mode = e.target.value;
  if (state.payload) {
    await ensureIndicators();
    renderChart(state.payload);
  }
};
$("indicator").onchange = async (e) => {
  state.indicator = e.target.value;
  await ensureIndicators();
  if (state.payload) renderChart(state.payload);
};
$("log-scale").onchange = (e) => {
  state.log = e.target.checked;
  if (state.payload) renderChart(state.payload);
};
$("show-anom").onchange = (e) => {
  state.showAnom = e.target.checked;
  if (state.payload) renderChart(state.payload);
};
$("anom-only-sig").onchange = (e) => {
  state.anomOnlySig = e.target.checked;
  if (state.payload) renderAnomalies(state.payload);
};
$("anom-years").onchange = (e) => {
  state.anomYears = parseInt(e.target.value, 10) || 0;
  if (state.payload) renderAnomalies(state.payload);
};
$("btn-bin").onclick = checkBin;

// 图表容器尺寸变化(拖动侧栏 / 折叠 / 旋转)时重绘
if (window.ResizeObserver) {
  new ResizeObserver(() => state.chart && state.chart.resize()).observe($("chart"));
  new ResizeObserver(() => {
    if (factChart) factChart.resize();
    if (factBucketChart) factBucketChart.resize();
  }).observe($("fact-wrap"));
}

// 窄屏: 折叠/展开顶部股票池
$("side-toggle").onclick = () => {
  $("sidebar").classList.toggle("collapsed");
  if (state.chart) state.chart.resize();
};

// 侧栏宽度可拖动 + 本地记忆
const sidebarEl = $("sidebar");
(function initResizer() {
  const w = parseInt(localStorage.getItem("sidebarW") || "", 10);
  if (w >= 140 && w <= 440) sidebarEl.style.flexBasis = w + "px";
  const resizer = $("resizer");
  let dragging = false;
  resizer.addEventListener("pointerdown", (e) => {
    dragging = true;
    resizer.classList.add("dragging");
    try { resizer.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  window.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const w2 = Math.max(140, Math.min(440, e.clientX - sidebarEl.getBoundingClientRect().left));
    sidebarEl.style.flexBasis = w2 + "px";
    if (state.chart) state.chart.resize();
  });
  window.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove("dragging");
    localStorage.setItem("sidebarW", String(Math.round(sidebarEl.getBoundingClientRect().width)));
  });
})();

// 顶部 ☰ 按钮: 整块隐藏/显示股票池 (桌面/横屏)
$("nav-toggle").onclick = () => {
  document.body.classList.toggle("nav-collapsed");
  localStorage.setItem("navCollapsed", document.body.classList.contains("nav-collapsed") ? "1" : "0");
  if (state.chart) state.chart.resize();
};
function syncNav() {
  if (MQ_NARROW.matches) document.body.classList.remove("nav-collapsed");
  else if (localStorage.getItem("navCollapsed") === "1") document.body.classList.add("nav-collapsed");
}
syncNav();

// 视口变化: 竖屏/横屏/桌面模式切换时重绘, 否则仅 resize
function onViewportChange() {
  const m = (MQ_NARROW.matches ? "n" : "d") + (isShort() ? "s" : "");
  if (m !== viewMode && state.payload) renderChart(state.payload);
  else if (state.chart) state.chart.resize();
}
window.addEventListener("resize", onViewportChange);
window.addEventListener("orientationchange", onViewportChange);
if (MQ_NARROW.addEventListener) MQ_NARROW.addEventListener("change", () => { syncNav(); onViewportChange(); });

if (MQ_NARROW.matches) sidebarEl.classList.add("collapsed");

// ---------------------------------------------------------------- 添加股票
const addState = { polling: null, shown: 0 };

function openModal() {
  $("modal").classList.remove("hidden");
  $("add-search").focus();
}
function closeModal() {
  $("modal").classList.add("hidden");
}
async function doSearch() {
  const q = $("add-search").value.trim();
  if (!q) return;
  const box = $("add-results");
  box.innerHTML = '<div class="none">搜索中…</div>';
  try {
    const data = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
    if (data.error) return void (box.innerHTML = `<div class="none">${data.error}</div>`);
    if (!data.length) return void (box.innerHTML = '<div class="none">没有找到匹配的股票</div>');
    box.innerHTML = "";
    data.forEach((c) => {
      const div = document.createElement("div");
      div.className = "res";
      div.innerHTML =
        `<span><span class="r-name">${c.name}</span> <span class="r-meta">${c.code}</span></span>` +
        `<span class="r-meta">${c.market_label} · ${c.secid} · ${c.tencent}</span>`;
      div.onclick = () => pickCandidate(c);
      box.appendChild(div);
    });
  } catch (e) {
    box.innerHTML = `<div class="none">${e.message}</div>`;
  }
}
function pickCandidate(c) {
  $("f-market").textContent = `${c.market_label} (${c.market})`;
  $("f-market").dataset.v = c.market;
  $("f-code").value = c.code;
  $("f-name").value = c.name;
  $("f-secid").value = c.secid;
  $("f-tencent").value = c.tencent;
  $("f-qlib").value = c.qlib;
  $("add-form").classList.remove("hidden");
  $("add-log").classList.remove("show");
}
async function submitAdd() {
  const payload = {
    market: $("f-market").dataset.v,
    code: $("f-code").value.trim(),
    name: $("f-name").value.trim(),
    secid: $("f-secid").value.trim(),
    tencent: $("f-tencent").value.trim(),
    qlib: $("f-qlib").value.trim(),
  };
  const btn = $("add-submit");
  const logBox = $("add-log");
  btn.disabled = true;
  logBox.classList.add("show");
  logBox.innerHTML = "";
  addState.shown = 0;
  try {
    const d = await (
      await fetch("/api/add_stock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
    ).json();
    if (d.error) {
      logBox.innerHTML = `<div class="err">${d.error}</div>`;
      btn.disabled = false;
      return;
    }
    pollJob(d.job_id, btn);
  } catch (e) {
    logBox.innerHTML = `<div class="err">${e.message}</div>`;
    btn.disabled = false;
  }
}
function pollJob(jobId, btn) {
  clearInterval(addState.polling);
  const logBox = $("add-log");
  addState.polling = setInterval(async () => {
    const job = await (await fetch(`/api/job/${jobId}`)).json();
    const lines = job.log || [];
    for (let i = addState.shown; i < lines.length; i++) {
      const div = document.createElement("div");
      if (lines[i].includes("✓") || lines[i].startsWith("完成")) div.className = "ok";
      if (lines[i].startsWith("错误")) div.className = "err";
      div.textContent = lines[i];
      logBox.appendChild(div);
    }
    addState.shown = lines.length;
    logBox.scrollTop = logBox.scrollHeight;
    if (job.done) {
      clearInterval(addState.polling);
      btn.disabled = false;
      if (job.status === "ok") {
        state.indCache = {};
        await loadSymbols();
        if (job.result) selectSymbol(job.result.market, job.result.qlib);
      }
    }
  }, 1000);
}
$("btn-add").onclick = openModal;
$("modal-close").onclick = closeModal;
$("modal").onclick = (e) => {
  if (e.target.id === "modal") closeModal();
};
$("add-search-btn").onclick = doSearch;
$("add-search").onkeydown = (e) => {
  if (e.key === "Enter") doSearch();
};
$("add-submit").onclick = submitAdd;

loadSymbols();
