/**
 * chart.js  —  D3 v7 forecast chart
 */

const CHART_API = "http://localhost:8000/api";
const MARGIN    = { top: 16, right: 16, bottom: 36, left: 44 };
const W         = 288;
const H         = 210;

function cColour(c) {
  if (c == null || isNaN(c)) return "#888";
  if (c < 0.30) return "#27ae60";
  if (c < 0.60) return "#f39c12";
  if (c < 0.80) return "#e74c3c";
  return "#7b241c";
}

function pct(v) { return Math.round((v ?? 0) * 100) + "%"; }

// ── Exposed globally so map.js can call it ────────────────
window.loadChart = async function(sensorId, roadName) {
  console.log("loadChart fired:", sensorId, roadName);

  document.getElementById("chart-hint").style.display  = "none";
  document.getElementById("chart-title").textContent   = roadName;
  document.getElementById("chart-stats").style.display = "grid";
  document.getElementById("d3-chart").innerHTML =
    "<p style='color:#8b8fa8;font-size:11px;padding:12px 0'>Loading…</p>";

  ["stat-current","stat-30","stat-60"].forEach(id => {
    document.getElementById(id).textContent = "…";
    document.getElementById(id).style.color = "#888";
  });

  // Fetch history
  let history = [];
  try {
    const res = await fetch(`${CHART_API}/sensors/${sensorId}/history?limit=24`);
    if (res.ok) {
      const data = await res.json();
      history = (data.readings ?? [])
        .map(d => ({ time: new Date(d.time), congestion: +d.congestion }))
        .filter(d => !isNaN(d.congestion))
        .sort((a, b) => a.time - b.time);
    }
  } catch (e) { console.error("History fetch error:", e); }

  // Fetch forecasts
  let forecasts = [];
  try {
    const res = await fetch(`${CHART_API}/forecast/${sensorId}`);
    if (res.ok) {
      const data = await res.json();
      forecasts = (data.forecasts ?? []).map(d => ({
        time:    new Date(d.forecast_time),
        pred:    +d.congestion_pred,
        lo:      +(d.congestion_lo  ?? d.congestion_pred * 0.85),
        hi:      +(d.congestion_hi  ?? Math.min(d.congestion_pred * 1.15, 1)),
        horizon: d.horizon_min,
      }));
    }
  } catch (e) { console.error("Forecast fetch error:", e); }

  // Stat pills
  const latest = history.at(-1)?.congestion ?? null;
  const f30    = forecasts.find(f => f.horizon === 30);
  const f60    = forecasts.find(f => f.horizon === 60);

  const cur = document.getElementById("stat-current");
  cur.textContent = latest != null ? pct(latest) : "—";
  cur.style.color = cColour(latest);

  const s30 = document.getElementById("stat-30");
  s30.textContent = f30 ? pct(f30.pred) : "—";
  s30.style.color = f30 ? cColour(f30.pred) : "#888";

  const s60 = document.getElementById("stat-60");
  s60.textContent = f60 ? pct(f60.pred) : "—";
  s60.style.color = f60 ? cColour(f60.pred) : "#888";

  document.getElementById("d3-chart").innerHTML = "";

  if (history.length === 0) {
    document.getElementById("d3-chart").innerHTML =
      `<p style='color:#e74c3c;font-size:11px;padding:12px 4px;line-height:1.6'>
        No history data for ${sensorId}.<br>
        Make sure consumer.py is running.
      </p>`;
    return;
  }

  drawChart(history, forecasts);

  if (forecasts.length === 0) {
    const note = document.createElement("p");
    note.style.cssText = "color:#f39c12;font-size:10px;margin-top:6px";
    note.textContent = "⚠ No forecast — run train_prophet.py";
    document.getElementById("d3-chart").appendChild(note);
  }
};

// ── D3 drawing ─────────────────────────────────────────────
function drawChart(history, forecasts) {
  const iW = W - MARGIN.left - MARGIN.right;
  const iH = H - MARGIN.top  - MARGIN.bottom;
  const now = new Date();
  const xEnd = forecasts.length > 0
    ? forecasts.at(-1).time
    : history.at(-1).time;

  const x = d3.scaleTime().domain([history[0].time, xEnd]).range([0, iW]);
  const y = d3.scaleLinear().domain([0, 1]).range([iH, 0]);

  const svg = d3.select("#d3-chart").append("svg").attr("width", W).attr("height", H);
  const g   = svg.append("g").attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

  // Grid
  g.append("g")
    .call(d3.axisLeft(y).ticks(4).tickSize(-iW).tickFormat(""))
    .call(ax => ax.select(".domain").remove())
    .call(ax => ax.selectAll("line").attr("stroke","#2a2d3a").attr("stroke-dasharray","3,3"));

  // X axis
  g.append("g").attr("transform",`translate(0,${iH})`)
    .call(d3.axisBottom(x).ticks(4).tickFormat(d3.timeFormat("%H:%M")))
    .call(ax => ax.select(".domain").attr("stroke","#2a2d3a"))
    .call(ax => ax.selectAll("text").attr("fill","#8b8fa8").attr("font-size",10))
    .call(ax => ax.selectAll("line").attr("stroke","#2a2d3a"));

  // Y axis
  g.append("g")
    .call(d3.axisLeft(y).ticks(4).tickFormat(d => Math.round(d*100)+"%"))
    .call(ax => ax.select(".domain").remove())
    .call(ax => ax.selectAll("text").attr("fill","#8b8fa8").attr("font-size",10))
    .call(ax => ax.selectAll("line").remove());

  // Now line
  if (now >= history[0].time && now <= xEnd) {
    g.append("line")
      .attr("x1",x(now)).attr("x2",x(now)).attr("y1",0).attr("y2",iH)
      .attr("stroke","#4f8ef7").attr("stroke-width",1).attr("stroke-dasharray","4,3");
    g.append("text").attr("x",x(now)+4).attr("y",10)
      .attr("fill","#4f8ef7").attr("font-size",9).text("now");
  }

  // Forecast band + line
  if (forecasts.length > 0) {
    const bandPts = [
      { time: history.at(-1).time, lo: history.at(-1).congestion, hi: history.at(-1).congestion },
      ...forecasts,
    ];
    g.append("path").datum(bandPts)
      .attr("d", d3.area().x(d=>x(d.time)).y0(d=>y(d.lo)).y1(d=>y(d.hi)).curve(d3.curveCatmullRom))
      .attr("fill","#4f8ef7").attr("opacity",0.15);

    const forePts = [
      { time: history.at(-1).time, pred: history.at(-1).congestion },
      ...forecasts,
    ];
    g.append("path").datum(forePts)
      .attr("d", d3.line().x(d=>x(d.time)).y(d=>y(d.pred)).curve(d3.curveCatmullRom))
      .attr("fill","none").attr("stroke","#4f8ef7").attr("stroke-width",2).attr("stroke-dasharray","5,3");

    forecasts.forEach(f => {
      g.append("circle")
        .attr("cx",x(f.time)).attr("cy",y(f.pred)).attr("r",4)
        .attr("fill","#4f8ef7").attr("stroke","#fff").attr("stroke-width",1.5);
      g.append("text")
        .attr("x",x(f.time)).attr("y",y(Math.min(f.hi+0.05,1))-6)
        .attr("text-anchor","middle").attr("fill","#4f8ef7").attr("font-size",9)
        .text(`+${f.horizon}m`);
    });
  }

  // History line
  g.append("path").datum(history)
    .attr("d", d3.line().x(d=>x(d.time)).y(d=>y(d.congestion)).curve(d3.curveCatmullRom))
    .attr("fill","none").attr("stroke","#e8eaf0").attr("stroke-width",2);

  // Coloured dots
  g.selectAll(".hdot").data(history).join("circle")
    .attr("class","hdot")
    .attr("cx", d => x(d.time))
    .attr("cy", d => y(d.congestion))
    .attr("r",  2.5)
    .attr("fill", d => cColour(d.congestion));
}

// ── Close button — wrapped in DOMContentLoaded ────────────
document.addEventListener("DOMContentLoaded", () => {
  const closeBtn = document.getElementById("close-chart");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      document.getElementById("d3-chart").innerHTML          = "";
      document.getElementById("chart-stats").style.display   = "none";
      document.getElementById("chart-hint").style.display    = "block";
      document.getElementById("chart-title").textContent     = "Click a sensor to view forecast";
    });
  }
});

window.loadChart = loadChart;