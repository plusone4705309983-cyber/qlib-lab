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
};

const UP = "#d94838";
const DOWN = "#1a9e5c";
const ERROR = "#e5484d";
const INFO = "#8a9099";

const $ = (id) => document.getElementById(id);

function fmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
function fmtInt(v) {
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
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
async function selectSymbol(market, symbol) {
  state.market = market;
  state.symbol = symbol;
  markActive();
  $("bin-result").innerHTML = "";
  const res = await fetch(`/api/kline?market=${market}&symbol=${symbol}`);
  const data = await res.json();
  if (data.error) {
    $("summary").textContent = data.error;
    return;
  }
  state.payload = data;
  $("cur-name").textContent = data.meta.name;
  $("cur-code").textContent = `${data.meta.market_label} · ${data.meta.symbol.toUpperCase()} · ${data.meta.currency}`;
  renderSummary(data);
  renderChart(data);
  renderAnomalies(data);
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
  if (state.showAnom) {
    for (const a of p.anomalies) {
      const i = idxOf[a.date];
      if (i === undefined) continue;
      const y = src.high[i] * 1.03;
      const pt = { value: [i, y], name: a.date, anom: a };
      (a.level === "error" ? errPts : infoPts).push(pt);
    }
  }

  const start = Math.max(0, dates.length - 260);
  const zoom = [
    { type: "inside", xAxisIndex: [0, 1, 2], startValue: start, endValue: dates.length - 1 },
    { type: "slider", xAxisIndex: [0, 1, 2], bottom: 6, height: 18, startValue: start, endValue: dates.length - 1 },
  ];

  const axisIdx = [0, 1, 2];
  const grids = [
    { left: 64, right: 26, top: 24, height: "46%" },
    { left: 64, right: 26, top: "56%", height: "10%" },
    { left: 64, right: 26, top: "71%", height: "16%" },
  ];
  const xAxes = axisIdx.map((n) => ({
    type: "category",
    gridIndex: n,
    data: dates,
    boundaryGap: true,
    axisLine: { lineStyle: { color: "#c8ccd2" } },
    axisLabel: { show: n === 2, color: "#6b7280" },
    axisTick: { show: false },
    splitLine: { show: false },
  }));
  const yAxes = [
    { scale: true, gridIndex: 0, log: state.log, splitLine: { lineStyle: { color: "#eef0f3" } } },
    { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } },
    { scale: true, gridIndex: 2, splitNumber: 3, splitLine: { lineStyle: { color: "#eef0f3" } } },
  ];

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
  ];

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
function renderAnomalies(p) {
  const box = $("anom-table");
  $("anom-count").textContent = `共 ${p.anomalies.length} 条`;
  if (!p.anomalies.length) {
    box.innerHTML = `<div class="empty">未发现异常</div>`;
    return;
  }
  const rows = p.anomalies
    .map(
      (a) =>
        `<tr><td class="date">${a.date}</td>` +
        `<td><span class="tag ${a.level}">${a.level === "error" ? "错误" : "提示"}</span></td>` +
        `<td>${a.msg}</td></tr>`
    )
    .join("");
  box.innerHTML = `<table><thead><tr><th>日期</th><th>级别</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table>`;
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

// ---------------------------------------------------------------- 事件
$("price-mode").onchange = (e) => {
  state.mode = e.target.value;
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
$("btn-bin").onclick = checkBin;
window.addEventListener("resize", () => state.chart && state.chart.resize());

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
