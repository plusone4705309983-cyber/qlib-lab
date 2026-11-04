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

// 统一处理登录失效：任何 API 返回 401 时跳到 /login 或 /invite
const _nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _nativeFetch(...args);
  if (res.status === 401) {
    const d = await res.clone().json().catch(() => ({}));
    if (d && d.login_url) location.href = d.login_url;
  }
  return res;
};

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

// ---------------------------------------------------------------- 侧栏（我的自选）
async function loadSymbols() {
  const res = await fetch("/api/symbols");
  const data = await res.json();
  state.catalog = Array.isArray(data) ? data : (data.symbols || []);
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
      `<span class="sym-main"><span class="s-name">${esc(s.name)}</span> ` +
      `<span class="s-code">${esc(s.qlib.toUpperCase())}</span></span>` +
      `<span class="s-badge ${n ? "" : "zero"}">${n}</span>` +
      `<button class="sym-del" title="移出自选">×</button>`;
    div.onclick = () => selectSymbol(s.market, s.qlib);
    div.querySelector(".sym-del").onclick = (ev) => {
      ev.stopPropagation();
      removeWatch(s.market, s.qlib);
    };
    list.appendChild(div);
  }

  if (!state.catalog.length) {
    const empty = document.createElement("div");
    empty.className = "sym-empty";
    empty.innerHTML =
      '还没有自选股<br><button id="sym-empty-add" class="btn mini">从全量股票池添加</button>';
    list.appendChild(empty);
    $("sym-empty-add").onclick = openWatchModal;
  }

  const stillThere =
    state.symbol && state.catalog.some((s) => s.market === state.market && s.qlib === state.symbol);
  if (!stillThere) {
    if (state.catalog.length) {
      selectSymbol(state.catalog[0].market, state.catalog[0].qlib);
    } else {
      state.market = null;
      state.symbol = null;
      state.payload = null;
      state.indData = null;
      $("cur-name").textContent = "—";
      $("cur-code").textContent = "";
      $("summary").textContent = "请先添加自选股";
      $("anom-table").innerHTML = "";
      $("bin-result").innerHTML = "";
      clearFactor("请先添加自选股");
      clearHolders();
      if (state.chart) { try { state.chart.clear(); } catch (_) {} }
    }
  }
}

// ---------------------------------------------------------------- 管理自选
let watchCatalog = [];

async function openWatchModal() {
  $("watch-modal").classList.remove("hidden");
  $("watch-filter").value = "";
  $("watch-list").innerHTML = '<div class="none">加载中…</div>';
  try {
    const d = await (await fetch("/api/catalog")).json();
    watchCatalog = Array.isArray(d) ? d : [];
    renderWatchList();
  } catch (e) {
    $("watch-list").innerHTML = `<div class="none">加载失败：${esc(e.message)}</div>`;
  }
  $("watch-filter").focus();
}

function closeWatchModal() {
  $("watch-modal").classList.add("hidden");
}

function renderWatchList() {
  const q = $("watch-filter").value.trim().toLowerCase();
  const rows = watchCatalog.filter(
    (s) => !q || s.name.toLowerCase().includes(q) || s.qlib.includes(q)
  );
  if (!rows.length) {
    $("watch-list").innerHTML = '<div class="none">没有匹配的标的</div>';
    return;
  }
  let html = "";
  let curMarket = null;
  for (const s of rows) {
    if (s.market !== curMarket) {
      curMarket = s.market;
      html += `<div class="market-head">${esc(s.market_label || s.market)}</div>`;
    }
    html += `<div class="watch-row">
      <span class="sym-main"><span class="s-name">${esc(s.name)}</span>
        <span class="s-code">${esc(s.qlib.toUpperCase())}</span></span>
      <button class="btn mini ${s.in_watchlist ? "danger" : "btn-primary"}"
        data-market="${esc(s.market)}" data-symbol="${esc(s.qlib)}"
        data-added="${s.in_watchlist ? "1" : "0"}">
        ${s.in_watchlist ? "移出" : "添加"}
      </button>
    </div>`;
  }
  $("watch-list").innerHTML = html;
  $("watch-list").querySelectorAll("button[data-symbol]").forEach((btn) => {
    btn.onclick = () => toggleWatch(btn.dataset.market, btn.dataset.symbol, btn.dataset.added === "1", btn);
  });
}

async function toggleWatch(market, symbol, added, btn) {
  if (btn) btn.disabled = true;
  try {
    const url = added
      ? `/api/watchlist/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`
      : "/api/watchlist";
    const opts = added
      ? { method: "DELETE" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ market, symbol }),
        };
    const d = await (await fetch(url, opts)).json();
    if (d.error) throw new Error(d.error);
    const item = watchCatalog.find((x) => x.market === market && x.qlib === symbol);
    if (item) item.in_watchlist = !added;
    renderWatchList();
    await loadSymbols();
  } catch (e) {
    alert(`更新自选失败：${e.message}`);
    if (btn) btn.disabled = false;
  }
}

async function removeWatch(market, symbol) {
  try {
    const d = await (
      await fetch(`/api/watchlist/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`, {
        method: "DELETE",
      })
    ).json();
    if (d.error) throw new Error(d.error);
    await loadSymbols();
  } catch (e) {
    alert(`移出自选失败：${e.message}`);
  }
}

$("watch-manage").onclick = (ev) => {
  ev.stopPropagation();
  openWatchModal();
};
$("watch-close").onclick = closeWatchModal;
$("watch-done").onclick = closeWatchModal;
$("watch-modal").onclick = (ev) => {
  if (ev.target.id === "watch-modal") closeWatchModal();
};
$("watch-filter").oninput = renderWatchList;

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

let selectToken = 0;
function showChartLoading(show) {
  const el = $("chart-loading");
  if (!el) return;
  if (show) el.classList.add("show");
  else el.classList.remove("show");
}
async function selectSymbol(market, symbol) {
  const token = ++selectToken;
  state.market = market;
  state.symbol = symbol;
  markActive();
  if (MQ_NARROW.matches) $("sidebar").classList.add("collapsed");
  $("bin-result").innerHTML = "";
  clearFactor("已切换标的，点『计算』重新分析");
  showChartLoading(true);
  $("chart").setAttribute("aria-busy", "true");
  if (state.chart) {
    try { state.chart.clear(); } catch (_) {}
  }
  $("summary").innerHTML = "加载中…";
  $("cur-name").textContent = "加载中…";
  $("cur-code").textContent = `${market.toUpperCase()} · ${symbol.toUpperCase()}`;
  try {
    const res = await fetch(`/api/kline?market=${market}&symbol=${symbol}`);
    const data = await res.json();
    if (token !== selectToken) return;
    if (state.market !== market || state.symbol !== symbol) return;
    if (data.error) {
      $("summary").textContent = data.error;
      return;
    }
    state.payload = data;
    state.indData = null;
    $("cur-name").textContent = data.meta.name;
    $("cur-code").textContent = `${data.meta.market_label} · ${data.meta.symbol.toUpperCase()} · ${data.meta.currency} · secid ${data.meta.secid || "—"}`;
    renderSummary(data);
    renderAnomalies(data);
    renderBacktest(data);
    renderHolders(data);
    await ensureIndicators();
    if (state.market === market && state.symbol === symbol) renderChart(data);
  } finally {
    if (token === selectToken) {
      showChartLoading(false);
      $("chart").removeAttribute("aria-busy");
    }
  }
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
  $("fact-status-text").textContent = msg || "";
}

function factBusy(on) {
  $("fact-spinner").hidden = !on;
  $("fact-run").disabled = on;
  const box = $("fact-status");
  box.classList.toggle("busy", on);
  if (on) box.setAttribute("aria-busy", "true");
  else box.removeAttribute("aria-busy");
}

function clearFactor(msg) {
  factState.data = null;
  factStatus(msg || "");
  $("fact-short").classList.add("hidden");
  $("fact-short").textContent = "";
  $("fact-bucket-chart").style.display = "";
  $("fact-signal").style.display = "none";
  $("fact-signal").innerHTML = "";
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
  factBusy(true);
  factStatus("计算中… 本市场首次需启动 qlib（约 20~60 秒），之后新表达式约 1 秒");
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
    factBusy(false);
  }
}

function renderFactor(d) {
  const noGroups = !(d.buckets || []).some((b) => b.n > 0);
  const shortBox = $("fact-short");
  if (noGroups) {
    shortBox.classList.remove("hidden");
    shortBox.textContent =
      `样本不足：当前 ${fmtInt(d.count)} 个交易日，滚动分位分组需要至少 ${d.hist_win} 个交易日。` +
      `因此不能给出分位 / 偏多 / 偏空判断，也不会当作“中性”信号；因子序列和全样本 IC 仍可参考。`;
  } else {
    shortBox.classList.add("hidden");
    shortBox.textContent = "";
  }
  renderFactSignal(d);
  renderFactCurrent(d);
  renderFactIC(d);
  renderFactTable(d);
  renderFactChart(d);
  renderFactBucketChart(d);
}

function renderFactSignal(d) {
  const box = $("fact-signal");
  const s = d.signal;
  if (!s) { box.style.display = "none"; box.innerHTML = ""; return; }
  const cls = s.level === "bullish" ? "bullish" : s.level === "bearish" ? "bearish" : "neutral";
  const label = { bullish: "偏多", bearish: "偏空", neutral: "中性" }[s.level] || "中性";
  const conf = { strong: "强置信", medium: "中等置信", weak: "弱置信" }[s.confidence] || "方向不明";
  const st = s.stats || {};
  const excessCls = st.excess > 0 ? "pos" : st.excess < 0 ? "neg" : "";
  const avgCls = st.avg_ret > 0 ? "pos" : st.avg_ret < 0 ? "neg" : "";
  const stat = (name, value, klass = "") =>
    `<div class="fs-stat"><span>${name}</span><b class="${klass}">${value}</b></div>`;
  const range =
    st.q10 === null || st.q10 === undefined ? "—" : `${signedPct(st.q10)} ~ ${signedPct(st.q90)}`;
  const icTxt = s.ic === null || s.ic === undefined ? "—" : `${s.ic > 0 ? "+" : ""}${s.ic.toFixed(3)}`;
  const reasons = (s.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
  box.className = `fact-signal ${cls}`;
  box.innerHTML = `
    <div class="fs-head">
      <span class="fs-badge">${label}</span>
      <span class="fs-conf">${conf}</span>
      <span class="fs-meta">${esc(s.date)} · 分位 ${plainPct(s.pct, 1)} · ${esc(s.name)} · 因子 ${num3(s.factor)}</span>
    </div>
    <div class="fs-stats">
      ${stat(`未来 ${s.horizon} 日均值`, signedPct(st.avg_ret), avgCls)}
      ${stat("上涨概率", plainPct(st.win_rate, 0))}
      ${stat("相对全样本超额", signedPct(st.excess), excessCls)}
      ${stat("有效样本", `约 ${fmtInt(st.n_eff)}`)}
      ${stat(`IC(${s.horizon}日)`, icTxt)}
      ${stat("q10 ~ q90", range)}
    </div>
    <ul class="fs-reasons">${reasons}</ul>
    <div class="fs-invalidate">失效条件：${esc(s.invalidate)}</div>
    <div class="fs-note">${esc(s.disclaimer || "历史统计，不构成投资建议。")}</div>`;
  box.style.display = "";
}

function renderFactCurrent(d) {
  const box = $("fact-current");
  const c = d.current;
  if (!c) { box.style.display = "none"; return; }
  const cls = c.level === "info" ? "up" : c.level === "warn" ? "down" : "";
  const lvl = { info: ["偏多", "signal"], warn: ["偏空", "signal-warn"], neutral: ["中性", "info"] }[c.level] || ["中性", "info"];
  box.className = `fact-current ${cls}`;
  box.innerHTML = `
    <div class="fc-head">
      <span>原始读数</span><span>${c.date}</span>
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
  const base = `<tr><td>全样本</td><td>—</td><td>—</td><td>—</td>${hs
    .map((h) => factCell(d.overall[String(h)]))
    .join("")}</tr>`;
  if (!(d.buckets || []).some((b) => b.n > 0)) {
    $("fact-table").innerHTML = `<table><thead>${head}</thead><tbody>${base}</tbody></table>`;
    return;
  }
  const rows = d.buckets.map((b) => {
    const cur = d.current && d.current.group === b.i ? "fact-cur-row" : "";
    const fr = b.factor_lo === null ? "—" : `${num3(b.factor_lo)} ~ ${num3(b.factor_hi)}`;
    const pr = b.pct_lo === null ? "—" : `${plainPct(b.pct_lo, 0)} ~ ${plainPct(b.pct_hi, 0)}`;
    return `<tr class="${cur}"><td>${b.name}</td><td>${fmtInt(b.n)}</td><td>${fr}</td><td>${pr}</td>${hs
      .map((h) => factCell(b.h[String(h)]))
      .join("")}</tr>`;
  });
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
  const wrap = $("fact-bucket-chart");
  if (!(d.buckets || []).some((b) => b.n > 0)) {
    if (factBucketChart) factBucketChart.clear();
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "";
  if (!factBucketChart) factBucketChart = echarts.init(wrap);
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

// 图表高度可拖动 + 本地记忆 (双击恢复默认)
(function initChartResizer() {
  const chartEl = $("chart");
  const grip = $("chart-resizer");
  const saved = parseInt(localStorage.getItem("chartH") || "", 10);
  if (saved >= 200 && saved <= 3000) chartEl.style.height = saved + "px";
  let dragging = false;
  let startY = 0;
  let startH = 0;
  const applyH = (h) => {
    const max = Math.max(360, Math.round(window.innerHeight * 3));
    const v = Math.max(200, Math.min(max, h));
    chartEl.style.height = Math.round(v) + "px";
    if (state.chart) state.chart.resize();
  };
  grip.addEventListener("pointerdown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = chartEl.getBoundingClientRect().height;
    grip.classList.add("dragging");
    try { grip.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  window.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    applyH(startH + (e.clientY - startY));
  });
  window.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove("dragging");
    localStorage.setItem("chartH", String(Math.round(chartEl.getBoundingClientRect().height)));
  });
  grip.addEventListener("dblclick", () => {
    chartEl.style.height = "";
    localStorage.removeItem("chartH");
    if (state.chart) state.chart.resize();
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

// ---------------------------------------------------------------- 持仓账本
const posState = { data: null, busy: false, mode: "new", editingId: null };
const POS_DEFAULT_EXPR = "Mean($close,5)/Mean($close,20)";
const POS_DEFAULTS = {
  stop: 8,
  take: 20,
  trail: 8,
  be: 10,
  time: 20,
  atr: 2.0,
};

const num = (v, nd = 2) =>
  v === null || v === undefined || v === "" || !isFinite(v)
    ? "—"
    : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: nd });
const money = (v, cur) => {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const sym = cur === "USD" ? "$" : cur === "CNY" ? "¥" : "";
  return `${sym}${Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
};
const plClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

function posStatus(msg, busy) {
  $("pos-status-text").textContent = msg || "";
  if (busy !== undefined) {
    $("pos-spinner").hidden = !busy;
    const box = $("pos-status");
    box.classList.toggle("busy", busy);
    if (busy) box.setAttribute("aria-busy", "true");
    else box.removeAttribute("aria-busy");
  }
}

function posBusy(on) {
  posState.busy = on;
  $("pos-add").disabled = on;
  $("pos-eval").disabled = on;
  $("pos-save").disabled = on;
  posStatus($("pos-status-text").textContent, on);
}

async function posFetch(url, opts) {
  const res = await fetch(url, opts);
  let d = null;
  try {
    d = await res.json();
  } catch (e) {
    d = { error: `响应不是 JSON（HTTP ${res.status}）` };
  }
  if (!res.ok || (d && d.error)) throw new Error((d && d.error) || `HTTP ${res.status}`);
  return d;
}

async function loadPositions() {
  if (!$("pos-wrap")) return;
  try {
    const d = await posFetch("/api/positions");
    posRender(d);
    if (!$("pos-status-text").textContent) {
      posStatus(d.summary && d.summary.count ? `已加载 ${d.summary.count} 只持仓` : "还没有持仓记录");
    }
  } catch (e) {
    posStatus(`读取持仓失败：${e.message}`);
  }
}

function posSymbols(selected) {
  const sel = $("pos-symbol");
  sel.innerHTML = "";
  let curMarket = null;
  let group = null;
  for (const s of state.catalog) {
    if (s.market !== curMarket) {
      curMarket = s.market;
      group = document.createElement("optgroup");
      group.label = s.market_label || s.market;
      sel.appendChild(group);
    }
    const opt = document.createElement("option");
    opt.value = `${s.market}|${s.qlib}`;
    opt.textContent = `${s.name} (${s.qlib.toUpperCase()})`;
    if (selected && selected.market === s.market && selected.symbol === s.qlib) opt.selected = true;
    group.appendChild(opt);
  }
  // 持仓里的标的可能已不在自选（例如用户后来移出），仍要能编辑
  if (selected && !state.catalog.some((s) => s.market === selected.market && s.qlib === selected.symbol)) {
    const group2 = document.createElement("optgroup");
    group2.label = selected.market === "us" ? "美股（不在自选）" : "A股（不在自选）";
    const opt = document.createElement("option");
    opt.value = `${selected.market}|${selected.symbol}`;
    opt.textContent = `${selected.name || selected.symbol} (${String(selected.symbol).toUpperCase()})`;
    opt.selected = true;
    group2.appendChild(opt);
    sel.appendChild(group2);
  }
  if (!selected && state.market && state.symbol) {
    sel.value = `${state.market}|${state.symbol}`;
  }
}

function posFillDefaults() {
  $("pos-expr").value = POS_DEFAULT_EXPR;
  $("pos-stop").value = POS_DEFAULTS.stop;
  $("pos-take").value = POS_DEFAULTS.take;
  $("pos-trail").value = POS_DEFAULTS.trail;
  $("pos-be").value = POS_DEFAULTS.be;
  $("pos-time").value = POS_DEFAULTS.time;
  $("pos-atr").value = POS_DEFAULTS.atr;
}

function posOpenForm() {
  $("pos-form").classList.remove("hidden");
  $("pos-form").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function posCloseForm() {
  $("pos-form").classList.add("hidden");
  posState.mode = "new";
  posState.editingId = null;
  posState.quoteSeq = (posState.quoteSeq || 0) + 1;
  $("pos-quote-hint").textContent = "";
}

async function posLoadQuote() {
  const [market, symbol] = ($("pos-symbol").value || "").split("|");
  if (!market || !symbol) return;
  const seq = (posState.quoteSeq = (posState.quoteSeq || 0) + 1);
  $("pos-quote-hint").textContent = "读取最新收盘价…";
  try {
    const d = await posFetch(`/api/quote?market=${market}&symbol=${encodeURIComponent(symbol)}`);
    if (seq !== posState.quoteSeq) return;
    posState.quote = d;
    $("pos-date").value = d.date;
    $("pos-price").value = d.price;
    const existing = ((posState.data && posState.data.positions) || []).find(
      (p) => p.market === market && p.symbol === symbol
    );
    $("pos-quote-hint").textContent =
      `最新收盘 ${d.date} · ${d.price} ${d.currency}` +
      (posState.mode === "new" && existing ? " · 已有持仓，将追加买入（沿用原设置）" : "");
  } catch (e) {
    $("pos-quote-hint").textContent = `读取行情失败：${e.message}`;
  }
}

async function posOpenNew() {
  if (posState.busy) return;
  posState.mode = "new";
  posState.editingId = null;
  $("pos-form-title").textContent = "记录买入";
  $("pos-lot-fields").classList.remove("hidden");
  $("pos-settings-fields").classList.remove("hidden");
  $("pos-symbol").disabled = false;
  $("pos-symbol").closest(".pos-field").classList.remove("hidden");
  $("pos-qty").value = "";
  $("pos-fee").value = "0";
  $("pos-note").value = "";
  posFillDefaults();
  posSymbols(null);
  posOpenForm();
  await posLoadQuote();
  $("pos-qty").focus();
}

function posOpenLot(p) {
  if (posState.busy) return;
  posState.quoteSeq = (posState.quoteSeq || 0) + 1;
  posState.mode = "lot";
  posState.editingId = p.id;
  $("pos-form-title").textContent = `加仓 · ${p.name}`;
  $("pos-lot-fields").classList.remove("hidden");
  $("pos-settings-fields").classList.add("hidden");
  $("pos-symbol").disabled = true;
  $("pos-symbol").closest(".pos-field").classList.remove("hidden");
  posSymbols(p);
  $("pos-date").value = p.last_date || "";
  $("pos-price").value = p.last_price || "";
  $("pos-qty").value = "";
  $("pos-fee").value = "0";
  $("pos-note").value = "";
  $("pos-quote-hint").textContent = "追加买入沿用原有风控参数";
  posOpenForm();
  $("pos-qty").focus();
}

function posOpenSettings(p) {
  if (posState.busy) return;
  posState.quoteSeq = (posState.quoteSeq || 0) + 1;
  posState.mode = "settings";
  posState.editingId = p.id;
  $("pos-form-title").textContent = `风控设置 · ${p.name}`;
  $("pos-lot-fields").classList.add("hidden");
  $("pos-settings-fields").classList.remove("hidden");
  $("pos-symbol").disabled = true;
  $("pos-symbol").closest(".pos-field").classList.add("hidden");
  posSymbols(p);
  $("pos-expr").value = p.expr || POS_DEFAULT_EXPR;
  $("pos-stop").value = (p.stop_pct * 100).toFixed(2);
  $("pos-take").value = (p.take_profit_pct * 100).toFixed(2);
  $("pos-trail").value = (p.trail_pct * 100).toFixed(2);
  $("pos-be").value = (p.break_even_trigger * 100).toFixed(2);
  $("pos-time").value = p.time_stop_days;
  $("pos-atr").value = p.atr_mult;
  $("pos-quote-hint").textContent = "修改因子表达式后，需要重新刷新因子提醒";
  posOpenForm();
}

function posReadSettings() {
  const expr = $("pos-expr").value.trim();
  if (!expr) throw new Error("监控因子不能为空");
  const pct = (id, label) => {
    const v = parseFloat($(id).value);
    if (!isFinite(v) || v <= 0) throw new Error(`${label}必须是正数`);
    return v / 100;
  };
  const days = parseInt($("pos-time").value, 10);
  if (!isFinite(days) || days < 1) throw new Error("时间止损天数必须是正整数");
  const atr = parseFloat($("pos-atr").value);
  if (!isFinite(atr) || atr <= 0) throw new Error("ATR 倍数必须是正数");
  return {
    expr,
    stop_pct: pct("pos-stop", "止损比例"),
    take_profit_pct: pct("pos-take", "止盈比例"),
    trail_pct: pct("pos-trail", "移动止盈回撤"),
    break_even_trigger: pct("pos-be", "保本触发比例"),
    time_stop_days: days,
    atr_mult: atr,
  };
}

async function posSave() {
  if (posState.busy) return;
  const [market, symbol] = ($("pos-symbol").value || "").split("|");
  if (!market || !symbol) {
    posStatus("请选择标的");
    return;
  }
  let payload = null;
  let url = "/api/positions";
  let method = "POST";
  try {
    if (posState.mode === "settings") {
      payload = posReadSettings();
      url = `/api/positions/${posState.editingId}`;
      method = "PATCH";
    } else {
      const price = parseFloat($("pos-price").value);
      const qty = parseFloat($("pos-qty").value);
      const fee = parseFloat($("pos-fee").value || "0");
      if (!isFinite(price) || price <= 0) throw new Error("买入价必须是正数");
      if (!isFinite(qty) || qty <= 0) throw new Error("数量必须是正数");
      if (!isFinite(fee) || fee < 0) throw new Error("手续费不能为负");
      payload = {
        market,
        symbol,
        date: $("pos-date").value,
        price,
        qty,
        fee,
        note: $("pos-note").value,
      };
      if (!payload.date) throw new Error("请选择买入日期");
      if (posState.mode === "new") Object.assign(payload, posReadSettings());
    }
  } catch (e) {
    posStatus(e.message);
    return;
  }

  posBusy(true);
  posStatus(posState.mode === "settings" ? "保存风控设置…" : "保存买入记录…", true);
  const wasSettings = posState.mode === "settings";
  try {
    const d = await posFetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    posRender(d);
    posCloseForm();
    posStatus(wasSettings ? "设置已保存" : "买入记录已保存");
  } catch (e) {
    posStatus(`保存失败：${e.message}`);
  } finally {
    posBusy(false);
  }
}

async function posDelete(p) {
  if (posState.busy) return;
  if (!confirm(`删除「${p.name}」的整个持仓（含 ${(p.lots || []).length} 笔买入）？`)) return;
  posBusy(true);
  posStatus(`删除 ${p.name}…`, true);
  try {
    const d = await posFetch(`/api/positions/${p.id}`, { method: "DELETE" });
    posRender(d);
    if (posState.editingId === p.id) posCloseForm();
    posStatus(`已删除 ${p.name}`);
  } catch (e) {
    posStatus(`删除失败：${e.message}`);
  } finally {
    posBusy(false);
  }
}

async function posDeleteLot(p, lotId) {
  if (posState.busy) return;
  if (!confirm("删除这笔买入记录？")) return;
  posBusy(true);
  posStatus("删除买入记录…", true);
  try {
    const d = await posFetch(`/api/positions/${p.id}/lots/${lotId}`, { method: "DELETE" });
    posRender(d);
    posStatus("买入记录已删除");
  } catch (e) {
    posStatus(`删除失败：${e.message}`);
  } finally {
    posBusy(false);
  }
}

async function posEvaluate() {
  if (posState.busy) return;
  if (!posState.data || !(posState.data.summary || {}).count) {
    posStatus("还没有持仓，先记录买入");
    return;
  }
  const ids = (posState.data.positions || []).map((p) => p.id);
  posBusy(true);
  posStatus("计算因子提醒… 首次需启动 qlib（约 20~60 秒），之后每只约 1 秒", true);
  try {
    const d = await posFetch("/api/positions/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    posRender(d);
    const errors = (d.positions || []).filter(
      (p) => p.factor && p.factor.status === "error"
    ).length;
    posStatus(
      errors
        ? `因子提醒已刷新，${errors} 只计算失败（见持仓表）`
        : `因子提醒已刷新 · ${(d.positions || []).length} 只持仓`
    );
  } catch (e) {
    posStatus(`刷新失败：${e.message}`);
  } finally {
    posBusy(false);
  }
}

function posExport(fmt) {
  window.location.href = `/api/positions/export?fmt=${fmt}`;
  posStatus(fmt === "csv" ? "开始下载 CSV…" : "开始下载 JSON 备份…");
}

function posFactorBadge(p) {
  const f = p.factor || {};
  const sig = f.signal || {};
  const conf = { strong: "强", medium: "中", weak: "弱" }[sig.confidence] || "";
  if (f.status === "missing") {
    return `<span class="pos-badge unk">未检查</span><span class="bt-sub">点上方刷新</span>`;
  }
  if (f.status === "error") {
    return `<span class="pos-badge unk">检查失败</span><span class="bt-sub" title="${esc(f.error || "")}">${esc(
      (f.error || "").slice(0, 18)
    )}</span>`;
  }
  const map = {
    bullish: ["偏多", "bull"],
    bearish: ["偏空", "bear"],
    neutral: ["中性", "neu"],
  };
  const hit = map[sig.level] || ["样本不足", "unk"];
  const sub = hit[1] === "unk"
    ? `${sig.reason || "历史不足 250 日"}`
    : `${conf ? conf + "置信 · " : ""}${sig.date || ""}${f.status === "stale" ? " · 需刷新" : ""}`;
  return `<span class="pos-badge ${hit[1]}">${hit[0]}</span><span class="bt-sub">${esc(sub)}</span>`;
}

function posRender(d) {
  posState.data = d;
  const s = d.summary || {};
  const cards = [];
  cards.push(
    `<div class="pos-sum-card"><span>持仓</span><b>${s.count || 0} 只</b>` +
      `<span class="muted">${s.danger || 0} 条危险 · ${s.warn || 0} 条警告</span></div>`
  );
  const curOrder = { CNY: 0, USD: 1 };
  const byCur = Object.entries(s.by_currency || {}).sort(
    (a, b) => (curOrder[a[0]] ?? 9) - (curOrder[b[0]] ?? 9)
  );
  for (const [cur, b] of byCur) {
    cards.push(
      `<div class="pos-sum-card"><span>${cur === "CNY" ? "A股" : cur === "USD" ? "美股" : cur} · 市值</span>` +
        `<b>${money(b.value, cur)}</b>` +
        `<span class="muted">${b.count} 只 · 成本 ${money(b.cost, cur)} · 浮盈 ` +
        `<b class="${plClass(b.pnl)}">${money(b.pnl, cur)} (${signedPct(b.pnl_pct)})</b></span></div>`
    );
  }
  $("pos-summary").innerHTML = cards.join("");

  const alerts = d.alerts || [];
  if (alerts.length) {
    $("pos-alerts").innerHTML = alerts
      .map(
        (a) =>
          `<div class="pos-alert ${esc(a.level)}" data-pos-id="${esc(a.position_id)}">` +
          `<div class="pa-title"><b>${esc(a.title)}</b>` +
          `<span>${esc(a.name)} ${esc((a.symbol || "").toUpperCase())}</span></div>` +
          `<div class="pa-msg">${esc(a.msg)}</div></div>`
      )
      .join("");
    document.querySelectorAll("#pos-alerts .pos-alert[data-pos-id]").forEach((el) => {
      el.onclick = () => {
        const row = document.getElementById(`pos-row-${el.dataset.posId}`);
        if (!row) return;
        row.scrollIntoView({ block: "center", behavior: "smooth" });
        row.classList.add("pos-flash");
        setTimeout(() => row.classList.remove("pos-flash"), 1600);
      };
    });
  } else if (s.count) {
    $("pos-alerts").innerHTML = `<div class="pos-alert ok"><div class="pa-msg">当前没有危险/警告级别的提醒。</div></div>`;
  } else {
    $("pos-alerts").innerHTML = "";
  }

  const positions = d.positions || [];
  if (!positions.length) {
    $("pos-table").innerHTML =
      `<div class="pos-empty">还没有持仓。点「+ 记录买入」填买入价和数量，系统会按收盘价计算浮盈，` +
      `并给出止损/止盈/因子转弱提醒。</div>`;
    return;
  }

  const head =
    `<tr><th>标的</th><th>持仓</th><th>现价</th><th>浮动盈亏</th>` +
    `<th>风控线</th><th>因子</th><th>提醒</th><th>操作</th></tr>`;
  const rows = positions.map((p) => {
    const rowId = `pos-row-${p.id}`;
    const lots = p.lots || [];
    const lotHtml = lots
      .slice()
      .sort((a, b) => (a.date || "").localeCompare(b.date || ""))
      .map(
        (x) =>
          `<div class="pos-lot"><span>${esc(x.date)} · ${num(x.qty, 4)} @ ${num(x.price, 4)}</span>` +
          `<span class="muted">费 ${num(x.fee, 2)}${x.note ? " · " + esc(x.note) : ""}</span>` +
          `<button class="pos-mini" data-act="del-lot" data-pos="${esc(p.id)}" data-lot="${esc(
            x.id
          )}" title="删除这笔买入">×</button></div>`
      )
      .join("");
    const stale = (p.alerts || []).some((a) => a.type === "stale");
    const primary = p.primary_alert || { level: "ok", title: "正常", msg: "" };
    const extra = (p.alerts || []).length - 1;
    const stopKind =
      { 固定: "固定止损", ATR: "ATR 止损", 保本: "保本线", 移动止盈: "移动止盈" }[p.stop_kind] ||
      "止损线";
    return (
      `<tr id="${rowId}" class="pos-row ${esc(p.status || "ok")}">` +
      `<td><b>${esc(p.name || p.symbol)}</b>` +
      `<span class="bt-sub">${esc(p.market_label || "")} · ${esc((p.symbol || "").toUpperCase())}</span></td>` +
      `<td><b>${num(p.qty, 4)}</b> <span class="muted">股/份</span>` +
      `<span class="bt-sub">${lots.length} 笔 · 均价 ${num(p.avg_cost, 4)}</span>` +
      `<details class="pos-lots"><summary>买入明细</summary>${lotHtml}</details></td>` +
      `<td><b>${num(p.last_price, 4)}</b>` +
      `<span class="bt-sub">${esc(p.last_date || "")}${stale ? " · 旧行情" : ""}</span></td>` +
      `<td class="${plClass(p.pnl)}"><b>${money(p.pnl, p.currency)}</b>` +
      `<span class="bt-sub">${signedPct(p.pnl_pct)} · 峰值 ${signedPct(p.peak_pnl)}` +
      `<br>持有 ${num(p.holding_td, 0)} 个交易日</span></td>` +
      `<td><b>${num(p.stop_price, 4)}</b>` +
      `<span class="bt-sub">${stopKind} · 止盈 ${num(p.take_profit_price, 4)}</span></td>` +
      `<td>${posFactorBadge(p)}</td>` +
      `<td class="pos-alert-cell ${esc(primary.level)}" title="${esc(primary.msg || "")}">` +
      `<b>${esc(primary.title || "正常")}</b>` +
      `<span class="bt-sub">${extra > 0 ? `另 ${extra} 条` : esc(primary.msg || "").slice(0, 24)}</span></td>` +
      `<td><div class="pos-actions">` +
      `<button class="pos-action" data-act="lot" data-pos="${esc(p.id)}">加仓</button>` +
      `<button class="pos-action" data-act="settings" data-pos="${esc(p.id)}">设置</button>` +
      `<button class="pos-action danger" data-act="del" data-pos="${esc(p.id)}">删除</button>` +
      `</div></td></tr>`
    );
  });
  $("pos-table").innerHTML = `<table>${head}${rows.join("")}</table>`;
}

function posFind(id) {
  return ((posState.data && posState.data.positions) || []).find((p) => p.id === id);
}

// P1 按用户隔离账本前，持仓只对 admin 开放；非管理员页面不渲染该区块
if ($("pos-wrap")) {
  $("pos-add").onclick = posOpenNew;
  $("pos-eval").onclick = posEvaluate;
  $("pos-save").onclick = posSave;
  $("pos-cancel").onclick = posCloseForm;
  $("pos-export-csv").onclick = () => posExport("csv");
  $("pos-export-json").onclick = () => posExport("json");
  $("pos-symbol").onchange = () => {
    if (posState.mode === "new") posLoadQuote();
  };

  $("pos-table").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const p = posFind(btn.dataset.pos);
    if (!p) return;
    if (btn.dataset.act === "lot") posOpenLot(p);
    else if (btn.dataset.act === "settings") posOpenSettings(p);
    else if (btn.dataset.act === "del") posDelete(p);
    else if (btn.dataset.act === "del-lot") posDeleteLot(p, btn.dataset.lot);
  });
}

// ---------------------------------------------------------------- 模拟交易（Alpaca Paper）
let brokerState = { account: null, preview: null, configured: false };

async function brokerFetch(url, opts) {
  const res = await fetch(url, opts);
  let d = null;
  try {
    d = await res.json();
  } catch (e) {
    d = { error: `响应不是 JSON（HTTP ${res.status}）` };
  }
  if (!res.ok || (d && d.error)) throw new Error((d && d.error) || `HTTP ${res.status}`);
  return d;
}

function brokerMoney(v) {
  if (v === null || v === undefined || v === "") return "—";
  return Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

async function brokerStatus() {
  try {
    const d = await brokerFetch("/api/broker/status");
    brokerState.configured = true;
    brokerState.account = d.account || {};
    $("broker-status-text").textContent = "已配置（Paper）";
    const acc = d.account || {};
    $("broker-account").innerHTML = [
      ["买入力度", brokerMoney(acc.buying_power)],
      ["现金", brokerMoney(acc.cash)],
      ["权益", brokerMoney(acc.equity)],
      ["持仓市值", brokerMoney(acc.long_market_value)],
    ]
      .map(([k, v]) => `<div class="broker-card"><span>${k}</span><b>${v}</b></div>`)
      .join("");
  } catch (e) {
    brokerState.configured = false;
    brokerState.account = null;
    $("broker-status-text").textContent = e.message.includes("密钥") ? e.message : `未配置或错误：${e.message}`;
    $("broker-account").innerHTML = "";
  }
}

async function brokerPositions() {
  try {
    const d = await brokerFetch("/api/broker/positions");
    const rows = d.positions || [];
    if (!rows.length) {
      $("broker-positions").innerHTML = '<div class="empty">暂无模拟盘持仓</div>';
      return;
    }
    const head = "<tr><th>标的</th><th>数量</th><th class=\"right\">均价</th><th class=\"right\">市值</th><th class=\"right\">盈亏</th></tr>";
    const body = rows
      .map(
        (p) =>
          `<tr><td>${esc(p.symbol)}</td><td>${p.qty}</td><td class="right">${Number(p.avg_entry_price || 0).toFixed(2)}</td>` +
          `<td class="right">${brokerMoney(p.market_value)}</td><td class="right">${brokerMoney(p.unrealized_pl)}</td></tr>`
      )
      .join("");
    $("broker-positions").innerHTML = `<table>${head}${body}</table>`;
  } catch (e) {
    $("broker-positions").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

async function brokerOrders() {
  try {
    const d = await brokerFetch("/api/broker/orders?status=all&limit=20");
    const rows = d.orders || [];
    if (!rows.length) {
      $("broker-orders").innerHTML = '<div class="empty">暂无委托</div>';
      return;
    }
    const head = "<tr><th>标的</th><th>方向</th><th>数量</th><th>状态</th><th>类型</th></tr>";
    const body = rows
      .map(
        (o) =>
          `<tr><td>${esc(o.symbol)}</td><td>${esc(o.side)}</td><td>${o.qty || o.notional || ""}</td>` +
          `<td>${esc(o.status)}</td><td>${esc(o.type)}</td></tr>`
      )
      .join("");
    $("broker-orders").innerHTML = `<table>${head}${body}</table>`;
  } catch (e) {
    $("broker-orders").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function brokerFill() {
  const m = state.market;
  const s = state.symbol;
  if (m && s) {
    $("broker-symbol").value = s.toUpperCase();
  } else {
    $("broker-symbol").value = $("broker-symbol").value || "";
  }
  const fa = factState.data && factState.data.signal;
  if (fa && fa.level === "bearish") {
    $("broker-side").value = "sell";
  } else if (fa && fa.level === "bullish") {
    $("broker-side").value = "buy";
  }
  if (!$("broker-notional").value && !$("broker-qty").value) {
    $("broker-notional").value = "1000";
  }
  $("broker-hint").textContent = "已从当前标的/信号预填（市价单，需预览并确认）";
  brokerState.preview = null;
  $("broker-preview-box").classList.add("hidden");
  $("broker-preview-box").innerHTML = "";
  $("broker-submit").disabled = true;
}

async function brokerPreview() {
  const payload = {
    symbol: $("broker-symbol").value.trim(),
    side: $("broker-side").value,
    qty: $("broker-qty").value ? Number($("broker-qty").value) : undefined,
    notional: $("broker-notional").value ? Number($("broker-notional").value) : undefined,
  };
  if (!payload.symbol) {
    $("broker-hint").textContent = "请填写标的";
    return;
  }
  if (payload.qty === undefined && payload.notional === undefined) {
    $("broker-hint").textContent = "请填写股数或金额（USD）";
    return;
  }
  $("broker-hint").textContent = "预览中...";
  try {
    const d = await brokerFetch("/api/broker/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    brokerState.preview = { ...payload, preview: d.preview };
    const box = $("broker-preview-box");
    box.innerHTML =
      `<div><b>预览（手动确认）</b></div>` +
      `<div>标的：${esc(payload.symbol)} · 方向：${payload.side.toUpperCase()} · 类型：市价单（day）</div>` +
      `<div>股数：${payload.qty ?? "—"} · 金额：${payload.notional ? "$" + payload.notional : "—"}</div>` +
      `<div class="muted">${esc(d.note || "")}</div>`;
    box.classList.remove("hidden");
    $("broker-submit").disabled = false;
    $("broker-hint").textContent = "请确认无误后点「确认发往 Alpaca Paper」";
  } catch (e) {
    $("broker-hint").textContent = e.message;
    $("broker-submit").disabled = true;
  }
}

async function brokerSubmit() {
  if (!brokerState.preview) {
    $("broker-hint").textContent = "请先预览订单";
    return;
  }
  if (!confirm(`确认发往 Alpaca Paper（市价单）\n${brokerState.preview.symbol} ${brokerState.preview.side.toUpperCase()}？`)) {
    return;
  }
  $("broker-submit").disabled = true;
  $("broker-hint").textContent = "下单中...";
  try {
    const payload = {
      symbol: brokerState.preview.symbol,
      side: brokerState.preview.side,
      type: "market",
      time_in_force: "day",
      qty: brokerState.preview.qty,
      notional: brokerState.preview.notional,
    };
    const d = await brokerFetch("/api/broker/order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("broker-hint").textContent = `已提交：订单 ${d.order && d.order.id ? d.order.id : "已创建"}（状态：${d.order && d.order.status ? d.order.status : "未知"}）`;
    brokerState.preview = null;
    $("broker-preview-box").classList.add("hidden");
    $("broker-preview-box").innerHTML = "";
    brokerPositions();
    brokerOrders();
    brokerStatus();
  } catch (e) {
    $("broker-hint").textContent = e.message;
    $("broker-submit").disabled = false;
  }
}

// P1（BYOK）上线前，模拟交易只对 admin 开放；非管理员页面不渲染该区块
if ($("broker-refresh")) {
  $("broker-refresh").onclick = () => {
    brokerStatus();
    brokerPositions();
    brokerOrders();
  };
  $("broker-fill").onclick = brokerFill;
  $("broker-preview").onclick = brokerPreview;
  $("broker-submit").onclick = brokerSubmit;

  brokerStatus();
  brokerPositions();
  brokerOrders();
  setInterval(() => {
    brokerPositions();
    brokerOrders();
    brokerStatus();
  }, 60000);
}

loadSymbols().finally(() => {
  if ($("pos-wrap")) loadPositions();
});
