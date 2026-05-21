/**
 * map.js  —  Leaflet + OpenStreetMap
 * 100% free. No account. No token. No credit card.
 */

const API_BASE = "http://localhost:8000/api";
const WS_URL   = "ws://localhost:8000/ws/live";

// ── State ──────────────────────────────────────────────────
let allSensors    = [];
let closedSensors = new Set();
let markers       = {};        // sensor_id → Leaflet circleMarker
let simMode       = false;

// ── Colour helpers ─────────────────────────────────────────
function congestionColour(c) {
  if (c == null) return "#555555";
  if (c < 0.30)  return "#27ae60";
  if (c < 0.60)  return "#f39c12";
  if (c < 0.80)  return "#e74c3c";
  return "#7b241c";
}

function congestionLabel(c) {
  if (c == null) return "—";
  return Math.round(c * 100) + "%";
}

// ── Init Leaflet map ───────────────────────────────────────
const map = L.map("map", {
  center:  [48.45, 10.45],   // midpoint Stuttgart–Munich
  zoom:    8,
  zoomControl: true,
});

// OpenStreetMap dark-ish tile — free, no key
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/">CARTO</a>',
  subdomains:  "abcd",
  maxZoom:     19,
}).addTo(map);

// ── Draw / update a single sensor marker ──────────────────
function upsertMarker(sensor) {
  const colour = congestionColour(sensor.congestion);
  const isClosed = closedSensors.has(sensor.sensor_id);
  const opacity  = isClosed ? 0.25 : 0.9;

  const popupHtml = `
    <div class="popup-road">${sensor.road_name}</div>
    <div class="popup-cong" style="color:${colour}">
      ${congestionLabel(sensor.congestion)}
    </div>
    <div class="popup-meta">
      ${sensor.city} · ${sensor.road_type || ""}<br>
      Speed: ${sensor.speed_avg ? Math.round(sensor.speed_avg) + " km/h" : "—"}<br>
      Volume: ${sensor.volume ?? "—"} veh/5min
    </div>
  `;

  if (markers[sensor.sensor_id]) {
    // Update existing marker
    const m = markers[sensor.sensor_id];
    m.setStyle({
      color:       colour,
      fillColor:   colour,
      fillOpacity: opacity,
    });
    m.getPopup().setContent(popupHtml);
  } else {
    // Create new marker
    const m = L.circleMarker([sensor.latitude, sensor.longitude], {
      radius:      10,
      color:       colour,
      fillColor:   colour,
      fillOpacity: opacity,
      weight:      2,
      opacity:     1,
    }).addTo(map);

    // Outer glow ring
    L.circleMarker([sensor.latitude, sensor.longitude], {
      radius:      16,
      color:       colour,
      fillColor:   colour,
      fillOpacity: 0.12,
      weight:      0,
      interactive: false,
    }).addTo(map);

    m.bindPopup(popupHtml, { className: "traffic-popup" });

    m.on("click", () => {
  console.log("sensor clicked:", sensor.sensor_id, sensor.road_name);

  if (typeof window.loadChart === "function") {
    window.loadChart(sensor.sensor_id, sensor.road_name);
  } else {
    console.error("loadChart is not available");
  }
});

    // Road name tooltip
    m.bindTooltip(sensor.road_name, {
      permanent:  false,
      direction:  "top",
      className:  "sensor-tooltip",
    });

    markers[sensor.sensor_id] = m;
  }
}

// ── Refresh all sensors from FastAPI ──────────────────────
async function refreshSensors() {
  try {
    const res  = await fetch(`${API_BASE}/sensors`);
    allSensors = await res.json();

    updateTopBar(allSensors);
    buildSidebarToggles(allSensors);
    allSensors.forEach(s => upsertMarker(s));
  } catch (e) {
    console.warn("Sensor fetch failed:", e);
  }
}

// ── Top bar ────────────────────────────────────────────────
function updateTopBar(sensors) {
  document.getElementById("sensor-count").textContent = sensors.length;
  const withData = sensors.filter(s => s.congestion != null);
  const avg = withData.length
    ? withData.reduce((s, d) => s + d.congestion, 0) / withData.length
    : 0;
  document.getElementById("avg-congestion").textContent = congestionLabel(avg);
  document.getElementById("last-updated").textContent =
    "Updated: " + new Date().toLocaleTimeString("de-DE");
}

// ── Sidebar toggles ────────────────────────────────────────
function buildSidebarToggles(sensors) {
  const container = document.getElementById("sensor-toggles");
  if (container.children.length > 0) return;

  sensors.forEach(s => {
    const row = document.createElement("div");
    row.className    = "toggle-row";
    row.dataset.id   = s.sensor_id;
    row.innerHTML = `
      <div>
        <div class="toggle-label">${s.road_name}</div>
        <div class="toggle-city">${s.city}</div>
      </div>
      <div class="toggle-switch"></div>
    `;
    row.addEventListener("click", () => {
      row.classList.toggle("active");
      closedSensors[row.classList.contains("active") ? "add" : "delete"](s.sensor_id);
      simMode = false;
      // Dim the marker immediately
      if (markers[s.sensor_id]) {
        markers[s.sensor_id].setStyle({
          fillOpacity: row.classList.contains("active") ? 0.2 : 0.9,
        });
      }
    });
    container.appendChild(row);
  });
}

// ── Simulate button ────────────────────────────────────────
document.getElementById("btn-simulate").addEventListener("click", async () => {
  if (closedSensors.size === 0) {
    alert("Toggle at least one road closed first.");
    return;
  }
  try {
    const res  = await fetch(`${API_BASE}/simulate`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ closed_sensor_ids: [...closedSensors] }),
    });
    const data = await res.json();

    // Apply simulation colours to map
    data.sensors.forEach(sim => {
      const m = markers[sim.sensor_id];
      if (!m) return;
      const c = sim.closed ? null : sim.congestion_after;
      m.setStyle({
        color:       congestionColour(c),
        fillColor:   congestionColour(c),
        fillOpacity: sim.closed ? 0.2 : 0.9,
      });
    });

    simMode = true;
    document.getElementById("sim-result").style.display  = "block";
    document.getElementById("sim-affected").textContent  = data.affected_count;
  } catch (e) {
    console.error("Simulation failed:", e);
  }
});

// ── Reset button ───────────────────────────────────────────
document.getElementById("btn-reset").addEventListener("click", () => {
  closedSensors.clear();
  simMode = false;
  document.querySelectorAll(".toggle-row.active").forEach(r => r.classList.remove("active"));
  document.getElementById("sim-result").style.display = "none";
  allSensors.forEach(s => upsertMarker(s));   // restore real colours
});

// ── WebSocket live feed ────────────────────────────────────
function startWebSocket() {
  const ws = new WebSocket(WS_URL);

  ws.onmessage = (event) => {
    if (simMode) return;
    try {
      const msg = JSON.parse(event.data);
      const idx = allSensors.findIndex(s => s.sensor_id === msg.sensor_id);
      if (idx !== -1) {
        allSensors[idx] = { ...allSensors[idx], ...msg };
        upsertMarker(allSensors[idx]);
        updateTopBar(allSensors);
      }
    } catch (_) {}
  };

  ws.onclose = () => setTimeout(startWebSocket, 5000);
}

// ── Add Leaflet popup styles dynamically ──────────────────
const style = document.createElement("style");
style.textContent = `
  .traffic-popup .leaflet-popup-content-wrapper {
    background: #1a1d27;
    border: 1px solid #2a2d3a;
    border-radius: 8px;
    color: #e8eaf0;
    font-family: -apple-system, sans-serif;
    font-size: 12px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }
  .traffic-popup .leaflet-popup-tip { background: #1a1d27; }
  .sensor-tooltip {
    background: #1a1d27;
    border: 1px solid #2a2d3a;
    color: #ccc;
    font-size: 11px;
    border-radius: 4px;
  }
  .popup-road  { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .popup-cong  { font-size: 22px; font-weight: 700; margin: 4px 0; }
  .popup-meta  { color: #8b8fa8; font-size: 11px; line-height: 1.6; }
`;
document.head.appendChild(style);

// ── Boot ───────────────────────────────────────────────────
refreshSensors();
setInterval(refreshSensors, 60_000);
startWebSocket();