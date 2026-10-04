/* ECharts renderers for the three dashboard charts.
   Each init function disposes any prior instance on the element, draws, and
   returns the chart instance (kept for resize). Dark, Apex-themed. */
const Charts = (() => {
  const ACCENT = "#DA292A";
  const AMBER = "#FFA552";
  const GRID = { left: 52, right: 22, top: 20, bottom: 38, containLabel: false };

  const axisColor = "#8a91a0";
  const splitLine = { lineStyle: { color: "rgba(255,255,255,0.05)" } };
  const axisLine = { lineStyle: { color: "rgba(255,255,255,0.12)" } };

  const registry = [];

  function mount(elId) {
    const el = document.getElementById(elId);
    if (!el) return null;
    const key = "__echart";
    if (el[key]) el[key].dispose();
    const chart = echarts.init(el, null, { renderer: "canvas" });
    el[key] = chart;
    registry.push(chart);
    return chart;
  }

  function resizeAll() {
    registry.forEach((c) => { try { c.resize(); } catch (_) {} });
  }

  function fmtTime(iso) {
    const s = String(iso);
    // 服务端存的是 UTC（无时区），补 Z 让浏览器转成本地(北京)时间
    const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function fmtAxis(v) {
    if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(1) + "亿";
    if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(0) + "万";
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return String(v);
  }

  // ---- rank progress (stepped RP line) ----
  function rank(elId, points) {
    const chart = mount(elId);
    if (!chart) return;
    const xs = points.map((p) => fmtTime(p.captured_at));
    const ys = points.map((p) => (p.value == null ? null : p.value));
    chart.setOption({
      backgroundColor: "transparent",
      textStyle: { color: "#c9ced9", fontFamily: "Inter, sans-serif" },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#16181f",
        borderColor: "#2a2f3c",
        textStyle: { color: "#e6e8ee" },
      },
      grid: GRID,
      xAxis: { type: "category", data: xs, boundaryGap: false, axisLine, axisLabel: { color: axisColor, fontSize: 11 } },
      yAxis: { type: "value", axisLine: { show: false }, splitLine, axisLabel: { color: axisColor, fontSize: 11 } },
      series: [{
        name: "段位分",
        type: "line",
        step: "end",
        symbol: "circle",
        symbolSize: 6,
        data: ys,
        lineStyle: { color: AMBER, width: 2 },
        itemStyle: { color: AMBER },
      }],
    });
  }

  // ---- daily one-point-per-day line (daily delta, e.g. 均伤/伤害/击杀) ----
  function dailyLine(elId, rows, label, color = AMBER, areaColor = "rgba(255,165,82,0.14)") {
    const chart = mount(elId);
    if (!chart) return;
    chart.setOption({
      backgroundColor: "transparent",
      textStyle: { color: "#c9ced9", fontFamily: "Inter, sans-serif" },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#16181f", borderColor: "#2a2f3c", textStyle: { color: "#e6e8ee" },
        valueFormatter: (v) => (v == null ? "—" : Math.round(v).toLocaleString()),
      },
      grid: GRID,
      xAxis: { type: "category", data: rows.map((r) => r.day), boundaryGap: false, axisLine, axisLabel: { color: axisColor, fontSize: 11 } },
      yAxis: { type: "value", axisLine: { show: false }, splitLine, axisLabel: { color: axisColor, fontSize: 11, formatter: (v) => fmtAxis(v) }, scale: true },
      series: [{
        name: label,
        type: "line",
        smooth: true,
        connectNulls: true,
        symbol: "circle",
        symbolSize: 7,
        data: rows.map((r) => r.delta),
        lineStyle: { color, width: 3 },
        itemStyle: { color, borderColor: "#fff", borderWidth: 1 },
        areaStyle: { color: areaColor },
      }],
    });
  }

  // ---- daily increment bars (per-day gain) ----
  function dailyBar(elId, rows, label) {
    const chart = mount(elId);
    if (!chart) return;
    chart.setOption({
      backgroundColor: "transparent",
      textStyle: { color: "#c9ced9", fontFamily: "Inter, sans-serif" },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#16181f", borderColor: "#2a2f3c", textStyle: { color: "#e6e8ee" },
        formatter: (p) => `${p[0].axisValue}<br/>${label}: <b style="color:${p[0].value >= 0 ? "#4ade80" : "#ff3b3b"}">${p[0].value >= 0 ? "+" : ""}${Number(p[0].value).toLocaleString()}</b>`,
      },
      grid: GRID,
      xAxis: { type: "category", data: rows.map((r) => r.day), axisLine, axisLabel: { color: axisColor, fontSize: 11 } },
      yAxis: { type: "value", axisLine: { show: false }, splitLine, axisLabel: { color: axisColor, fontSize: 11, formatter: (v) => fmtAxis(v) } },
      series: [{
        name: label,
        type: "bar",
        data: rows.map((r) => r.delta),
        barWidth: "46%",
        itemStyle: {
          color: (p) => (p.value >= 0 ? "#4ade80" : ACCENT),
          borderRadius: [4, 4, 0, 0],
        },
      }],
    });
  }

  return { dailyBar, dailyLine, rank, resizeAll };
})();
