/* =====================================================================
   ChurnWatch GH - app.js
   All the behaviour of the platform. Plain JavaScript, no framework.

   How it works:
     1. fetch() asks our FastAPI backend for JSON (e.g. /api/summary).
     2. We put the numbers into the page, draw charts with Chart.js and
        draw the round "thermostat" gauges as small SVGs.
     3. Every REFRESH_MS milliseconds we do it again, so the screen stays live.
   ===================================================================== */

// ---------- Settings ----------
// Same server that sent the page (laptop :8000 or Render). Only when the page is
// opened as a file or from VS Code Live Server do we point at the laptop API.
const API = (location.protocol === "file:" || location.port === "5500") ? "http://127.0.0.1:8000" : "";
const REFRESH_MS = 60 * 1000;      // refresh every 60 s
const PAGE_SIZE = 25;
const BUCKETS = { 180: 5, 720: 15, 1440: 30 };   // time range (minutes) -> bucket size (minutes)

// ---------- Small helpers ----------
const $ = (sel) => document.querySelector(sel);

// The last answer for each GET address. A theme switch redraws from these at once
// instead of waiting for the database again.
const apiCache = new Map();
let useCache = false;

/** Ask the API for JSON. Throws if the server answers with an error. */
async function api(path, options = {}) {
  const isGet = !options.method || options.method === "GET";
  if (isGet && useCache && apiCache.has(path)) return apiCache.get(path);
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  const data = await res.json();
  if (isGet) apiCache.set(path, data);
  return data;
}

/** Make text safe to put inside HTML (stops odd characters breaking the page). */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const fmtInt = (n) => (n ?? 0).toLocaleString();
const fmtPct = (p) => (p == null ? "–" : `${(p * 100).toFixed(1)}%`);
const fmtMoney = (n) => "GH₵ " + Math.round(n ?? 0).toLocaleString();
/** Short money for big headline numbers: GH₵ 19.8M */
function fmtMoneyShort(n) {
  const v = n ?? 0;
  if (Math.abs(v) >= 1e9) return `GH₵ ${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `GH₵ ${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `GH₵ ${(v / 1e3).toFixed(1)}K`;
  return fmtMoney(v);
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–");
// Nice display names for database codes (e.g. "mobile_app" -> "Mobile app", "ussd" -> "USSD").
const NAMES = { ussd: "USSD", momo: "MoMo", mobile_app: "Mobile app", internet_banking: "Internet banking", branch: "Branch" };
const pretty = (s) => NAMES[s] ?? String(s ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/** "Greater Accra" -> "GA", "Ashanti" -> "AS": used in the round avatars. */
function regionCode(region) {
  const words = String(region ?? "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "–";
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase();
}
const avatar = (c, size = "") =>
  `<span class="avatar ${size} ${c.risk_band || "unscored"}" title="${esc(c.region)}">${esc(regionCode(c.region))}</span>`;

function riskPill(band) {
  const b = band || "unscored";
  return `<span class="pill ${b}">${b === "unscored" ? "not scored" : b}</span>`;
}

/** Read a colour token from styles.css so charts match the page. */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const bandColour = (band) => cssVar(`--${band || "unscored"}`);

function meter(p, band) {
  return `<span class="meter"><span style="width:${((p ?? 0) * 100).toFixed(1)}%;background:${bandColour(band)}"></span></span>`;
}

// =====================================================================
// RING GAUGE (the round "thermostat" dial)
//   p       0..1 value, or null for "no value"
//   colour  arc colour
//   marker  optional 0..1 position of a small reference tick (e.g. historical rate)
// =====================================================================
function ring(p, colour, { size = 190, stroke = 12, label = "", valueText = null, marker = null, small = false } = {}) {
  const c = size / 2;
  const r = c - stroke / 2 - 6;
  const C = 2 * Math.PI * r;
  const arc = C * 0.75;                                   // 270° dial, open at the bottom
  const v = p == null ? 0 : Math.max(0, Math.min(1, p));
  const pointAt = (frac, radius) => {
    const a = ((135 + 270 * frac) * Math.PI) / 180;
    return [c + radius * Math.cos(a), c + radius * Math.sin(a)];
  };
  const [kx, ky] = pointAt(v, r);
  let markerSvg = "";
  if (marker != null) {
    const [x1, y1] = pointAt(marker, r - stroke / 2 - 4);
    const [x2, y2] = pointAt(marker, r + stroke / 2 + 4);
    markerSvg = `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${cssVar("--ref-line")}" stroke-width="2" stroke-linecap="round"/>`;
  }
  const valueArc = p == null ? "" : `
      <circle class="val" cx="${c}" cy="${c}" r="${r}" fill="none" stroke="${colour}" stroke-width="${stroke}"
        stroke-linecap="round" stroke-dasharray="${arc * v} ${C}" transform="rotate(135 ${c} ${c})"
        style="filter: drop-shadow(0 0 10px ${colour}88)"/>
      <circle cx="${kx}" cy="${ky}" r="${stroke / 2 + 3}" fill="#fff" stroke="${colour}" stroke-width="3"/>`;
  return `
    <div class="ring ${small ? "sm" : ""}" style="width:${size}px;height:${size}px">
      <svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${esc(label)} ${valueText ?? fmtPct(p)}">
        <circle class="track" cx="${c}" cy="${c}" r="${r}" fill="none" stroke-width="${stroke}"
          stroke-linecap="round" stroke-dasharray="${arc} ${C}" transform="rotate(135 ${c} ${c})"/>
        ${valueArc}${markerSvg}
      </svg>
      <div class="ring-center"><span class="ring-value">${valueText ?? fmtPct(p)}</span><span class="ring-label">${esc(label)}</span></div>
    </div>`;
}

// =====================================================================
// NAVIGATION
// =====================================================================
const TITLES = {
  overview: ["Dashboard", "Live churn & dormancy across Ghana retail customers"],
  customers: ["Customers", "Every live customer, scored by the churn model"],
  dormancy: ["Dormancy", "Accounts gone quiet, and those drifting that way"],
  whatif: ["What-if", "Build a customer and see what the models think"],
  model: ["Models", "What's running in production and how well it scores"],
};

function showView(name) {
  if (!TITLES[name]) name = "overview";
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);   // bookmarkable sections
  document.querySelectorAll(".nav-btn").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  const [title, sub] = TITLES[name];
  $("#view-title").textContent = title;
  $("#view-sub").textContent = sub;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "customers") loadCustomers().catch(showError);
  if (name === "dormancy") loadDormancy().catch(showError);
  if (name === "model") loadModel().catch(showError);
}
document.querySelectorAll(".nav-btn").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto)));

// =====================================================================
// CHART SETUP (Chart.js)
// =====================================================================
const charts = {};       // keep chart objects so we can update instead of re-creating

function chartsAvailable() {
  if (window.Chart) return true;
  document.querySelectorAll(".chart-empty").forEach((p) => {
    p.textContent = "Charts need an internet connection (Chart.js loads from a CDN).";
    p.hidden = false;
  });
  return false;
}

/** Chart text/tooltip colours come from the current theme; called again on every theme switch. */
function applyChartTheme() {
  if (!window.Chart) return;
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.font.family = "Poppins, system-ui, sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.display = false;    // legends are drawn in HTML, in the card header
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: cssVar("--tooltip-bg"),
    borderColor: cssVar("--line-strong"),
    borderWidth: 1,
    padding: 12,
    cornerRadius: 12,
    titleColor: cssVar("--text"),
    bodyColor: cssVar("--text-2"),
    footerColor: cssVar("--muted"),
    titleFont: { weight: "600", size: 12 },
    bodyFont: { size: 12 },
    footerFont: { size: 11, weight: "400" },
    boxPadding: 5,
    usePointStyle: true,
    caretSize: 0,
  });
}
applyChartTheme();
if (window.Chart) {
  // Once the Poppins font has loaded, redraw so the chart text uses it.
  document.fonts?.ready.then(() => Object.values(charts).forEach((ch) => ch.update("none")));
}

/** Dashed vertical line that follows the mouse on line charts. */
const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements?.() ?? [];
    if (!active.length) return;
    const { ctx, chartArea: { top, bottom } } = chart;
    const x = active[0].element.x;
    ctx.save();
    ctx.strokeStyle = cssVar("--ref-line");
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.restore();
  },
};

function axes({ horizontal = false, pct = false } = {}) {
  const value = {
    beginAtZero: true,
    border: { display: false },
    grid: { color: cssVar("--grid"), drawTicks: false },
    ticks: { padding: 8, maxTicksLimit: 6, callback: pct ? (v) => `${v}%` : undefined },
  };
  const category = {
    border: { display: true, color: cssVar("--line-strong") },
    grid: { display: false },
    ticks: { padding: 6, maxRotation: 0, autoSkipPadding: 14, color: cssVar("--text-2") },
  };
  return horizontal ? { x: value, y: category } : { x: category, y: value };
}

/** Create the chart the first time, update it afterwards (smooth, no flicker). */
function drawChart(key, canvasSel, config) {
  setEmpty(canvasSel, null);
  if (charts[key]) {
    charts[key].data = config.data;
    if (config.options?.scales) Object.assign(charts[key].options.scales, config.options.scales);
    charts[key].update();
    return;
  }
  charts[key] = new Chart($(canvasSel), config);
}

/** Show a message in place of a chart (e.g. "no data yet"), or clear it. */
function setEmpty(canvasSel, message) {
  const canvas = $(canvasSel);
  const note = canvas.parentElement.querySelector(".chart-empty");
  canvas.style.visibility = message ? "hidden" : "visible";
  note.hidden = !message;
  if (message) note.textContent = message;
}

/** Vertical gradient under a line (orange fading to nothing). */
function areaGradient(context) {
  const { ctx, chartArea } = context.chart;
  if (!chartArea) return "transparent";
  const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
  const rgb = cssVar("--chart-rgb");
  g.addColorStop(0, `rgba(${rgb}, .38)`);
  g.addColorStop(0.6, `rgba(${rgb}, .08)`);
  g.addColorStop(1, `rgba(${rgb}, 0)`);
  return g;
}

// =====================================================================
// OVERVIEW
// =====================================================================
let lastSummary = null;
let trendMinutes = 720;

async function loadSummary() {
  const s = await api("/api/summary");
  lastSummary = s;
  // Customers keep their last risk band while being re-scored, so count bands directly.
  const banded = (s.high_risk ?? 0) + (s.medium_risk ?? 0) + (s.low_risk ?? 0);
  const unscored = Math.max(0, (s.live_customers ?? 0) - banded);

  $("#kpi-live").textContent = fmtInt(s.live_customers);
  $("#kpi-new").textContent = `+${fmtInt(s.new_last_hour)} in the last hour`;
  $("#kpi-high").textContent = fmtInt(s.high_risk);
  $("#kpi-high-pct").textContent = banded ? `${((s.high_risk / banded) * 100).toFixed(1)}% of scored customers` : "nothing scored yet";
  $("#kpi-balance").textContent = fmtMoneyShort(s.balance_at_risk);
  $("#kpi-balance").title = fmtMoney(s.balance_at_risk);

  const hist = s.historical_churn_rate_pct;
  $("#risk-ring").innerHTML = ring(s.avg_churn_probability, cssVar("--chart-line"), {
    label: "avg churn risk", marker: hist != null ? hist / 100 : null,
  });
  $("#kpi-hist").innerHTML = hist != null
    ? `<i class="sw dash"></i>Historical churn rate <strong>${hist}%</strong>`
    : "";

  const parts = [
    ["High risk", s.high_risk, "--high"], ["Medium", s.medium_risk, "--medium"],
    ["Low risk", s.low_risk, "--low"], ["Not scored", unscored, "--unscored"],
  ];
  const total = parts.reduce((a, [, n]) => a + (n ?? 0), 0) || 1;
  $("#mix-bar").innerHTML = parts.filter(([, n]) => n > 0)
    .map(([label, n, col]) => `<span style="flex-grow:${n};background:var(${col})" title="${label}: ${fmtInt(n)}"></span>`).join("");
  $("#mix-legend").innerHTML = parts.map(([label, n, col]) =>
    `<li><i class="sw" style="background:var(${col})"></i>${label}<b>${fmtInt(n)}</b><em>${((n / total) * 100).toFixed(1)}%</em></li>`).join("");
  return s;
}

async function loadTrends() {
  const rows = await api(`/api/trends?minutes=${trendMinutes}&bucket_minutes=${BUCKETS[trendMinutes]}`);
  if (!chartsAvailable()) return;
  if (!rows.length) {
    setEmpty("#risk-chart", "No new customers in this time window yet.");
    setEmpty("#arrivals-chart", "No new customers in this time window yet.");
    return;
  }
  const labels = rows.map((r) => fmtTime(r.bucket));
  const risk = rows.map((r) => (r.avg_churn_probability == null ? null : +(r.avg_churn_probability * 100).toFixed(1)));
  const hist = lastSummary?.historical_churn_rate_pct;
  const maxY = Math.max(...risk.filter((v) => v != null), hist ?? 0);
  const accent = cssVar("--chart-line");
  const lastIdx = risk.length - 1;

  // ---- Average churn risk: smooth area line + dashed historical reference ----
  drawChart("risk", "#risk-chart", {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Avg churn risk", data: risk, borderColor: accent, backgroundColor: areaGradient, fill: "origin",
          borderWidth: 2.5, tension: 0.42, spanGaps: true,
          pointRadius: (ctx) => (ctx.dataIndex === lastIdx ? 4 : 0),       // highlight "now"
          pointBackgroundColor: accent, pointBorderColor: "#fff", pointBorderWidth: 2,
          pointHoverRadius: 6, pointHoverBackgroundColor: accent, pointHoverBorderColor: "#fff", pointHoverBorderWidth: 2 },
        ...(hist != null ? [{ label: "Historical rate", data: rows.map(() => hist), borderColor: cssVar("--ref-line"),
          borderWidth: 1.5, borderDash: [6, 6], pointRadius: 0, pointHoverRadius: 0, fill: false }] : []),
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 6, right: 6 } },
      scales: (() => { const s = axes({ pct: true }); s.y.suggestedMax = Math.ceil((maxY + 5) / 5) * 5; return s; })(),
      plugins: { tooltip: { callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${ctx.parsed.y}%` } } },
    },
    plugins: [crosshair],
  });

  // ---- New customers: stacked bars, high-risk part anchored to the baseline ----
  const high = rows.map((r) => r.high_risk ?? 0);
  const other = rows.map((r, i) => Math.max(0, (r.new_customers ?? 0) - high[i]));
  drawChart("arrivals", "#arrivals-chart", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "High risk", data: high, backgroundColor: cssVar("--high"), stack: "a",
          borderRadius: 0, maxBarThickness: 18 },
        { label: "Other", data: other, backgroundColor: cssVar("--neutral-bar"), stack: "a",
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: false,
          borderWidth: { top: 0, left: 0, right: 0, bottom: 2 }, borderColor: cssVar("--surface"), maxBarThickness: 18 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
      interaction: { mode: "index", intersect: false },
      scales: (() => { const s = axes(); s.x.stacked = true; s.y.stacked = true; return s; })(),
      plugins: { tooltip: { callbacks: {
        footer: (items) => `Total: ${items.reduce((a, it) => a + it.parsed.y, 0)} new customers`,
      } } },
    },
  });
}

document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-range]").forEach((x) => x.classList.toggle("active", x === b));
  trendMinutes = Number(b.dataset.range);
  loadTrends().catch(showError);
}));

async function loadBreakdown() {
  const dim = $("#breakdown-select").value;
  const rows = await api(`/api/breakdown/${dim}`);
  if (!chartsAvailable()) return;
  if (!rows.length) { setEmpty("#breakdown-chart", "Waiting for the scorer to finish a round…"); return; }
  drawChart("breakdown", "#breakdown-chart", {
    type: "bar",
    data: {
      labels: rows.map((r) => (dim === "products" ? `${r.label} product${r.label === "1" ? "" : "s"}` : pretty(r.label))),
      datasets: [{ label: "Avg churn risk", data: rows.map((r) => +((r.avg_churn_probability ?? 0) * 100).toFixed(1)),
                   extra: rows, backgroundColor: cssVar("--chart-line"), hoverBackgroundColor: cssVar("--accent-2"),
                   borderRadius: 4, borderSkipped: "start", maxBarThickness: 14 }],
    },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
      scales: axes({ horizontal: true, pct: true }),
      plugins: { tooltip: { callbacks: {
        label: (ctx) => ` Avg churn risk: ${ctx.parsed.x}%`,
        afterLabel: (ctx) => {
          const r = ctx.dataset.extra[ctx.dataIndex];
          return r ? ` ${fmtInt(r.customers)} customers · ${fmtInt(r.high_risk)} high risk` : "";
        },
      } } },
    },
  });
}
$("#breakdown-select").addEventListener("change", () => loadBreakdown().catch(showError));

async function loadTopRisk() {
  const { customers } = await api("/api/customers?risk=high&limit=7");
  $("#top-risk").innerHTML = customers.length
    ? customers.map((c) => `
        <li><button class="watch-item" data-id="${c.customer_id}">
          ${avatar(c)}
          <span class="who"><strong>#${c.customer_id}</strong><span>${esc(c.region)} · ${esc(pretty(c.primary_channel))}</span></span>
          <span class="score-chip">${fmtPct(c.churn_probability)}</span>
        </button></li>`).join("")
    : `<li class="empty-msg">No high-risk customers right now</li>`;
}

// =====================================================================
// CUSTOMERS TABLE
// =====================================================================
const REGIONS = ["Greater Accra", "Ashanti", "Western", "Eastern", "Central",
                 "Northern", "Volta", "Bono", "Upper East", "Upper West"];
$("#f-region").insertAdjacentHTML("beforeend", REGIONS.map((r) => `<option>${r}</option>`).join(""));
$("#d-region").insertAdjacentHTML("beforeend", REGIONS.map((r) => `<option>${r}</option>`).join(""));

let offset = 0;

const custCell = (c) =>
  `<div class="cust">${avatar(c)}<div><strong>#${c.customer_id}</strong><span>${esc(c.region)}</span></div></div>`;

async function loadCustomers() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset, sort: $("#f-sort").value });
  if ($("#f-risk").value) params.set("risk", $("#f-risk").value);
  if ($("#f-region").value) params.set("region", $("#f-region").value);
  if ($("#f-search").value.trim()) params.set("search", $("#f-search").value.trim());
  const { from, to } = joinedRange();
  if (from) params.set("joined_from", from);
  if (to) params.set("joined_to", to);

  const { total, customers } = await api(`/api/customers?${params}`);
  $("#cust-rows").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}" tabindex="0">
          <td>${custCell(c)}</td>
          <td>${fmtJoined(c.joined_on)}</td>
          <td>${c.age}</td>
          <td>${esc(pretty(c.primary_channel))}</td>
          <td>${c.products_number}</td>
          <td>${c.total_txn_count_90d}</td>
          <td>${c.days_since_last_txn}</td>
          <td class="stage">${esc(c.lifecycle_stage)}</td>
          <td><div class="risk-cell">${riskPill(c.risk_band)}${meter(c.churn_probability, c.risk_band)}<span class="muted">${fmtPct(c.churn_probability)}</span></div></td>
        </tr>`).join("")
    : `<tr class="empty-row"><td colspan="9">No customers match these filters</td></tr>`;

  const end = Math.min(offset + PAGE_SIZE, total);
  $("#page-info").textContent = total ? `${offset + 1}–${end} of ${fmtInt(total)}` : "";
  $("#prev").disabled = offset === 0;
  $("#next").disabled = end >= total;
}

// ---- "Joined the bank" filter ----
// Turns a preset like "1-3" (joined 1 to 3 years ago) into two dates, or reads
// the two date boxes when "Joined between dates" is chosen.
function yearsAgo(n) {
  const d = new Date();
  d.setFullYear(d.getFullYear() - n);
  return d.toISOString().slice(0, 10);          // YYYY-MM-DD
}
function joinedRange() {
  const v = $("#f-joined").value;
  if (!v) return {};
  if (v === "custom") return { from: $("#f-joined-from").value, to: $("#f-joined-to").value };
  const [lo, hi] = v.split("-");                // "1-3" -> joined between 3 and 1 years ago
  return { from: hi ? yearsAgo(Number(hi)) : "", to: Number(lo) ? yearsAgo(Number(lo)) : "" };
}
function fmtJoined(iso) {
  if (!iso) return "–";
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}
$("#f-joined").addEventListener("change", () => {
  $("#f-joined-range").hidden = $("#f-joined").value !== "custom";
});

// Re-load when a filter changes (search waits until you stop typing for 300 ms).
let searchTimer;
$("#f-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { offset = 0; loadCustomers().catch(showError); }, 300);
});
["#f-risk", "#f-region", "#f-sort", "#f-joined", "#f-joined-from", "#f-joined-to"].forEach((id) =>
  $(id).addEventListener("change", () => { offset = 0; loadCustomers().catch(showError); }));
$("#prev").addEventListener("click", () => { offset = Math.max(0, offset - PAGE_SIZE); loadCustomers().catch(showError); });
$("#next").addEventListener("click", () => { offset += PAGE_SIZE; loadCustomers().catch(showError); });

// Clicking (or pressing Enter on) any customer row / watchlist item opens the 360 drawer.
document.addEventListener("click", (e) => {
  const item = e.target.closest("[data-id]");
  if (item) openCustomer(item.dataset.id);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches?.("tr[data-id]")) openCustomer(e.target.dataset.id);
});

// =====================================================================
// CUSTOMER 360 DRAWER
// =====================================================================
function signsList(signs) {
  return signs.length
    ? `<ul class="signs">${signs.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>`
    : `<p class="ok-msg">No warning signs: this customer looks healthy.</p>`;
}

/** The dormancy dial: full rose ring when already dormant, the model's chance otherwise. */
function dormancyRing(x, size) {
  if (x.dormancy_status === "dormant") {
    return ring(1, cssVar("--high"), { size, small: true, label: "already dormant", valueText: "Dormant" });
  }
  if (x.dormancy_probability == null) {
    return ring(null, cssVar("--unscored"), { size, small: true, label: "dormancy not scored" });
  }
  return ring(x.dormancy_probability, bandColour(x.dormancy_band), { size, small: true, label: "dormancy risk" });
}

let openCustomerId = null;           // which customer the drawer shows (re-drawn on theme switch)
async function openCustomer(id) {
  openCustomerId = id;
  $("#drawer-body").innerHTML = '<p class="muted">Loading…</p>';
  $("#drawer").classList.remove("hidden");
  $("#drawer-backdrop").classList.remove("hidden");
  try {
    const c = await api(`/api/customers/${encodeURIComponent(id)}`);
    const chips = Object.entries(c.channels).map(([name, v]) =>
      name.includes("(90d)")
        ? `<span class="chip on">${esc(name.replace(" (90d)", ""))}: ${v}</span>`
        : `<span class="chip ${v ? "on" : "off"}">${esc(name)}</span>`).join("");
    const kv = (pairs) => `<div class="kv">${pairs.map(([k, v]) => `<div><span>${k}</span><span>${esc(v)}</span></div>`).join("")}</div>`;
    const dormantNote = c.dormancy_status === "dormant"
      ? `<p class="stage-line"><span class="pill dormant">dormant</span> ${c.days_since_last_txn - 90} days past the 90-day rule</p>` : "";

    $("#drawer-body").innerHTML = `
      <div class="drawer-head">
        ${avatar(c, "lg")}
        <div><p class="eyebrow">Customer 360</p><h2>#${c.customer_id}</h2>
          <p class="muted">${esc(c.region)} · ${c.age} yrs · arrived ${fmtTime(c.created_at)}</p></div>
      </div>

      <div class="rings">
        ${ring(c.churn_probability, bandColour(c.risk_band), { size: 170, small: true, label: "churn risk" })}
        ${dormancyRing(c, 170)}
      </div>
      <p class="stage-line">${riskPill(c.risk_band)} &nbsp;${esc(c.lifecycle_stage)}</p>
      ${dormantNote}

      <h3 class="section-title">Churn warning signs</h3>${signsList(c.warning_signs)}
      <h3 class="section-title">Dormancy signals</h3>${signsList(c.dormancy_reasons)}
      <h3 class="section-title">Channels</h3><div class="chips">${chips}</div>
      <h3 class="section-title">Activity · last 90 days</h3>
      ${kv([["Transactions", c.total_txn_count_90d], ["Days since last", c.days_since_last_txn],
            ["Failed txns", c.failed_txn_count_90d], ["Complaints", c.complaints_90d],
            ["Branch visits", c.branch_visits_90d], ["ATM withdrawals", c.atm_withdrawals_90d],
            ["Balance change", `${c.balance_change_3m_pct}%`], ["Salary paid in", c.salary_credited ? "Yes" : "No"]])}
      <h3 class="section-title">Profile</h3>
      ${kv([["Balance", fmtMoney(c.balance)], ["Products", c.products_number],
            ["Joined", `${fmtJoined(c.joined_on)} (${c.tenure} yrs)`], ["Credit score", c.credit_score],
            ["Active member", c.active_member ? "Yes" : "No"], ["Credit card", c.credit_card ? "Yes" : "No"],
            ["Primary channel", pretty(c.primary_channel)], ["Country (Kaggle)", c.country]])}
      <p class="foot-note">Scored by ${esc(c.model_version ?? "–")} at ${fmtTime(c.scored_at)}</p>`;
  } catch (err) {
    $("#drawer-body").innerHTML = `<p class="muted">Could not load customer: ${esc(err.message)}</p>`;
  }
}

function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer-backdrop").classList.add("hidden");
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// =====================================================================
// WHAT-IF SCORING
// =====================================================================
$("#whatif-form").addEventListener("submit", async (e) => {
  e.preventDefault();                                // stop the page reloading
  const form = e.target;
  const body = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") body[el.name] = el.checked ? 1 : 0;
    else if (el.type === "number") body[el.name] = Number(el.value);
    else body[el.name] = el.value;
  }
  const box = $("#whatif-result");
  box.className = "result";
  box.innerHTML = '<p class="muted">Scoring…</p>';
  try {
    lastScore = await api("/api/score", { method: "POST", body: JSON.stringify(body) });
    renderScore(lastScore);
  } catch (err) {
    box.innerHTML = `<p class="muted">Could not score: ${esc(err.message)}</p>`;
  }
});

let lastScore = null;                 // kept so a theme switch can redraw the dials in the new colours
function renderScore(r) {
  $("#whatif-result").innerHTML = `
    <div class="rings">
      ${ring(r.churn_probability, bandColour(r.risk_band), { size: 180, small: true, label: "churn risk" })}
      ${dormancyRing(r, 180)}
    </div>
    <p class="stage-line">${riskPill(r.risk_band)} &nbsp;${esc(r.lifecycle_stage)}</p>
    <h3 class="section-title">Churn warning signs</h3>
    ${signsList(r.warning_signs)}
    <p class="foot-note">Not saved: this only asks the model. Model ${esc(r.model_version)}</p>`;
}

// =====================================================================
// MODEL PAGE
// =====================================================================
async function loadModel() {
  const m = await api("/api/model");
  const x = m.metrics;
  const chip = (label, v) => `<span>${label} <b>${esc(v)}</b></span>`;
  const score = (label, v, asPct = false) => v == null ? "" : `
    <div class="score-row"><span>${label}</span><b>${asPct ? fmtPct(v) : v.toFixed(3)}</b>
      <span class="meter"><span style="width:${(v * 100).toFixed(1)}%"></span></span></div>`;
  const tile = (v, label) => `<div class="tile"><b>${v}</b><span>${label}</span></div>`;

  $("#model-info").innerHTML = `
    <div class="model-chips">
      ${chip("Model", m.model_name)}${chip("Version", m.model_version)}
      ${chip("Threshold", m.threshold.toFixed(2))}${chip("Trained", new Date(m.trained_at).toLocaleDateString())}
    </div>
    <div class="scores">
      ${score("ROC-AUC", x.roc_auc)}
      ${score("PR-AUC", x.pr_auc)}
      ${score("Precision: flagged customers who did leave", x.precision, true)}
      ${score("Recall: leavers we caught", x.recall, true)}
      ${score("Kaggle-only ROC-AUC", x.roc_auc_without_ghana_features)}
    </div>
    <div class="tiles">
      ${tile(fmtInt(x.confusion_matrix.tp), "Churners caught")}
      ${tile(fmtInt(x.confusion_matrix.fn), "Churners missed")}
      ${tile(fmtInt(x.confusion_matrix.fp), "False alarms")}
    </div>`;

  const d = m.dormancy;
  $("#dormancy-model-info").innerHTML = d
    ? `<div class="model-chips">
        ${chip("Model", d.model_name)}${chip("Version", d.model_version)}${chip("Threshold", d.threshold.toFixed(2))}
       </div>
       <div class="scores">
        ${score("ROC-AUC", d.metrics.roc_auc)}
        ${score("PR-AUC", d.metrics.pr_auc)}
        ${score("Precision", d.metrics.precision, true)}
        ${score("Recall: drifters we caught", d.metrics.recall, true)}
       </div>
       <div class="tiles"><div class="tile text"><b>${esc(d.drivers[0]?.label ?? "–")}</b><span>Top driver of dormancy</span></div></div>`
    : '<p class="muted">Not trained yet. Run <code>python scripts\\train_dormancy_model.py</code></p>';
}

// =====================================================================
// DORMANCY PAGE (Phase 8)
// =====================================================================
let dStatus = "going";
let dOffset = 0;

async function loadDormancy() {
  const s = await api("/api/dormancy/summary");
  $("#d-dormant").textContent = fmtInt(s.dormant);
  $("#d-dormant-sub").textContent = s.dormant ? `idle ${fmtInt(s.avg_days_idle_dormant)} days on average` : "none yet";
  $("#d-dormant-bal").textContent = fmtMoneyShort(s.dormant_balance);
  $("#d-dormant-bal").title = fmtMoney(s.dormant_balance);
  $("#d-going").textContent = s.model_trained ? fmtInt(s.going_dormant_high) : "–";
  $("#d-going-sub").textContent = s.model_trained
    ? `of ${fmtInt(s.active)} active · avg risk ${fmtPct(s.avg_dormancy_probability)}`
    : "train the dormancy model first";
  $("#d-going-bal").textContent = fmtMoneyShort(s.going_dormant_balance);
  $("#d-going-bal").title = fmtMoney(s.going_dormant_balance);

  const jobs = [loadIdleBuckets(), loadDormancyTable()];
  if (s.model_trained) jobs.push(loadDrivers());
  await Promise.all(jobs);
}

async function loadDrivers() {
  if (charts.drivers || !chartsAvailable()) return;          // drivers only change when retrained
  const rows = (await api("/api/dormancy/drivers")).slice(0, 8);
  drawChart("drivers", "#drivers-chart", {
    type: "bar",
    data: { labels: rows.map((r) => r.label),
            datasets: [{ label: "Importance", data: rows.map((r) => +(r.importance * 100).toFixed(1)),
                         backgroundColor: cssVar("--chart-line"), hoverBackgroundColor: cssVar("--accent-2"),
                         borderRadius: 4, borderSkipped: "start", maxBarThickness: 14 }] },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      scales: axes({ horizontal: true, pct: true }),
      plugins: { tooltip: { callbacks: { label: (ctx) => ` Share of importance: ${ctx.parsed.x}%` } } },
    },
  });
}

async function loadIdleBuckets() {
  const rows = await api("/api/dormancy/idle-buckets");
  if (!chartsAvailable()) return;
  if (!rows.length) { setEmpty("#idle-chart", "No live customers yet."); return; }
  const isDormant = (r) => r.label.includes("dormant");
  drawChart("idle", "#idle-chart", {
    type: "bar",
    data: {
      labels: rows.map((r) => r.label.replace(" (dormant)", "").replace("+ days", "d+").replace(" days", "d")),
      datasets: [{ label: "Customers", data: rows.map((r) => r.customers), extra: rows,
                   backgroundColor: rows.map((r) => (isDormant(r) ? cssVar("--high") : cssVar("--neutral-bar"))),
                   borderRadius: 4, borderSkipped: "start", maxBarThickness: 44 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
      scales: axes(),
      plugins: { tooltip: { callbacks: {
        title: (items) => items[0].dataset.extra[items[0].dataIndex].label,
        label: (ctx) => ` ${fmtInt(ctx.parsed.y)} customers`,
      } } },
    },
  });
}

async function loadDormancyTable() {
  const params = new URLSearchParams({ status: dStatus, limit: PAGE_SIZE, offset: dOffset });
  if ($("#d-region").value) params.set("region", $("#d-region").value);
  const { total, customers } = await api(`/api/dormancy/customers?${params}`);

  const going = dStatus === "going";
  $("#d-head").innerHTML = `<th>Customer</th><th>Days idle</th><th>Txns (90d)</th><th>Balance</th>
    <th>${going ? "Going dormant" : "Days past rule"}</th><th>Churn risk</th><th>Main reason</th>`;
  $("#d-rows").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}" tabindex="0">
          <td>${custCell(c)}</td>
          <td>${c.days_since_last_txn}</td>
          <td>${c.total_txn_count_90d}</td>
          <td>${fmtMoney(c.balance)}</td>
          <td>${going ? `<div class="risk-cell">${riskPill(c.dormancy_band)}<span class="muted">${fmtPct(c.dormancy_probability)}</span></div>`
                      : `<span class="pill dormant">${c.days_since_last_txn - 90} days</span>`}</td>
          <td>${riskPill(c.risk_band)}</td>
          <td class="reason">${esc((c.dormancy_reasons || [])[going ? 0 : 1] ?? (c.dormancy_reasons || [])[0] ?? "")}</td>
        </tr>`).join("")
    : `<tr class="empty-row"><td colspan="7">${going ? "No active customers scored for dormancy yet" : "No dormant customers yet"}</td></tr>`;

  const end = Math.min(dOffset + PAGE_SIZE, total);
  $("#d-page-info").textContent = total ? `${dOffset + 1}–${end} of ${fmtInt(total)}` : "";
  $("#d-prev").disabled = dOffset === 0;
  $("#d-next").disabled = end >= total;
}

document.querySelectorAll("[data-dstatus]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-dstatus]").forEach((x) => x.classList.toggle("active", x === b));
  dStatus = b.dataset.dstatus;
  dOffset = 0;
  loadDormancyTable().catch(showError);
}));
$("#d-region").addEventListener("change", () => { dOffset = 0; loadDormancyTable().catch(showError); });
$("#d-prev").addEventListener("click", () => { dOffset = Math.max(0, dOffset - PAGE_SIZE); loadDormancyTable().catch(showError); });
$("#d-next").addEventListener("click", () => { dOffset += PAGE_SIZE; loadDormancyTable().catch(showError); });

// =====================================================================
// LIVE REFRESH LOOP
// =====================================================================
function showError(err) {
  console.error(err);
  $("#live-dot").className = "dot err";
  $("#status-text").textContent = "API not reachable. Is uvicorn running?";
}

async function refreshAll() {
  try {
    const s = await loadSummary();
    await Promise.all([loadTrends(), loadBreakdown(), loadTopRisk()]);
    if ($("#view-customers").classList.contains("active")) await loadCustomers();
    if ($("#view-dormancy").classList.contains("active")) await loadDormancy();
    $("#live-dot").className = "dot ok";
    const last = s.last_customer_at ? `last customer ${fmtTime(s.last_customer_at)}` : "no live customers yet";
    $("#status-text").textContent = `Live · ${fmtTime(new Date().toISOString())} · ${last}`;
  } catch (err) {
    showError(err);
  }
}

$("#refresh-btn").addEventListener("click", refreshAll);

// =====================================================================
// LIGHT / DARK MODE
// The theme lives on <html data-theme="...">; styles.css swaps every colour token.
// Charts and dials bake their colours in when drawn, so we redraw them.
// =====================================================================
$("#theme-btn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("cw-theme", next); } catch (e) { /* private window: just don't remember */ }
  applyChartTheme();
  Object.keys(charts).forEach((k) => { charts[k].destroy(); delete charts[k]; });
  if (lastScore) renderScore(lastScore);
  redrawFromCache().catch(showError);
});

/** Redraw everything in the new colours from the answers we already have (no waiting). */
async function redrawFromCache() {
  useCache = true;
  try {
    await loadSummary();
    await Promise.all([loadTrends(), loadBreakdown(), loadTopRisk()]);
    if ($("#view-dormancy").classList.contains("active")) await loadDormancy();
    if ($("#view-customers").classList.contains("active")) await loadCustomers();
    if (openCustomerId && !$("#drawer").classList.contains("hidden")) await openCustomer(openCustomerId);
  } finally {
    useCache = false;
  }
}

// Refresh on a timer, but skip it while the browser tab is hidden (saves Neon compute).
setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });

if (location.hash.length > 1) showView(location.hash.slice(1));
refreshAll();
