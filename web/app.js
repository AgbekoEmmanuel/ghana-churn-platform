/* =====================================================================
   Pulse360 - app.js
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
  if (isGet) {
    apiCache.set(path, data);
    saveSnapshot(path, data);
  }
  return data;
}

// ---- Instant start: the last screens you saw are kept in this browser ----
// On the next visit the page draws them at once, then swaps in fresh data as it arrives.
// Only the small dashboard answers are kept (no customer profiles).
const SNAPSHOT_KEY = "cw-snapshot-v1";
const SNAPSHOT_PATHS = /^\/api\/(summary|trends|breakdown\/|model$|customers\/overview|customers\?risk=high&limit=7$|dormancy\/(summary|idle-buckets|drivers))/;
let snapshot = {};
try { snapshot = JSON.parse(localStorage.getItem(SNAPSHOT_KEY) || "{}"); } catch (e) { snapshot = {}; }
let snapshotTimer = null;
function saveSnapshot(path, data) {
  if (!SNAPSHOT_PATHS.test(path)) return;
  snapshot[path] = data;
  clearTimeout(snapshotTimer);
  snapshotTimer = setTimeout(() => {
    try { localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snapshot)); } catch (e) { /* storage full or blocked: fine */ }
  }, 500);
}

/** Make text safe to put inside HTML (stops odd characters breaking the page). */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const fmtInt = (n) => (n ?? 0).toLocaleString();
const fmtPct = (p) => (p == null ? "–" : `${(p * 100).toFixed(1)}%`);
const fmtMoney = (n) => "GH₵ " + Math.round(n ?? 0).toLocaleString();

// ---- Customer balances are hidden until someone clicks to reveal them ----
// Shows "GH₵ ••••••" with a closed eye; a click shows the amount (open eye), another click hides it.
// (A screen-privacy mask: it stops balances being read over someone's shoulder.)
const MASK = "GH₵ ••••••";
function maskedMoney(n) {
  return `<button type="button" class="masked" data-amount="${esc(fmtMoney(n))}" aria-label="Show balance" title="Click to show the balance">` +
         `<svg><use href="#i-eye-off"/></svg><span>${MASK}</span></button>`;
}
// Capture phase: runs before the row's own click, so revealing a balance doesn't open the customer.
document.addEventListener("click", (e) => {
  const m = e.target.closest?.(".masked");
  if (!m) return;
  e.stopPropagation();
  e.preventDefault();
  const shown = m.classList.toggle("revealed");
  m.querySelector("span").textContent = shown ? m.dataset.amount : MASK;
  m.querySelector("use").setAttribute("href", shown ? "#i-eye" : "#i-eye-off");
  m.setAttribute("aria-label", shown ? "Hide balance" : "Show balance");
  m.title = shown ? "Click to hide the balance" : "Click to show the balance";
}, true);
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
/** Round avatar: the customer's initials (or region code), with a small risk dot. */
const avatar = (c, size = "") =>
  `<span class="avatar ${size} ${c.risk_band || "unscored"}" title="${esc(c.full_name || c.region)}">${esc(c.initials || regionCode(c.region))}</span>`;

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
  customers: ["Customers", "Who our retail customers are, and every customer's full profile"],
  dormancy: ["Dormancy", "Accounts gone quiet, and those drifting that way"],
  whatif: ["What-if", "Build a customer and see what the models think"],
  model: ["Models", "What's running in production and how well it scores"],
};

/** Show a section. "customers/directory" opens the Customers section on its directory tab. */
function showView(target) {
  let [name, sub] = String(target).split("/");
  if (!TITLES[name]) name = "overview";
  if (name === "customers") sub = CUST_TABS.includes(sub) ? sub : custTab;
  const hash = name === "customers" ? `#customers/${sub}` : `#${name}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);   // bookmarkable sections
  document.querySelectorAll(".nav-btn").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  const [title, subtitle] = TITLES[name];
  $("#view-title").textContent = title;
  $("#view-sub").textContent = subtitle;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "customers") setCustTab(sub);
  if (name === "dormancy") loadDormancy().catch(showError);
  if (name === "model") loadModel().catch(showError);
}

// ---- Tabs inside the Customers section: "base" (analytics) and "directory" (the table) ----
const CUST_TABS = ["base", "directory"];
let custTab = "base";
function setCustTab(tab) {
  custTab = CUST_TABS.includes(tab) ? tab : "base";
  document.querySelectorAll("[data-ctab]").forEach((t) => {
    const on = t.dataset.ctab === custTab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  document.querySelectorAll("#view-customers .subview").forEach((v) => v.classList.toggle("active", v.id === `ctab-${custTab}`));
  $("#scope-bar").hidden = custTab !== "base";
  if (location.hash.startsWith("#customers") && location.hash !== `#customers/${custTab}`) {
    history.replaceState(null, "", `#customers/${custTab}`);
  }
  if (custTab === "base") loadOverview().catch(showError);
  else loadCustomers().catch(showError);
}
document.querySelectorAll("[data-ctab]").forEach((t) => t.addEventListener("click", () => setCustTab(t.dataset.ctab)));
document.querySelectorAll(".nav-btn").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
// Typing a section link (e.g. #customers/directory) or pressing Back switches section too.
window.addEventListener("hashchange", () => { if (location.hash.length > 1) showView(location.hash.slice(1)); });
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
    ticks: { padding: 6, maxRotation: 0, autoSkipPadding: 14, autoSkip: !horizontal, color: cssVar("--text-2") },   // bar lists keep every name
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
  const note = canvas.parentElement.querySelector(".chart-empty");   // some boxes (donuts) have none
  canvas.style.visibility = message ? "hidden" : "visible";
  if (!note) return;
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
  rememberCustomers(customers);
  $("#top-risk").innerHTML = customers.length
    ? customers.map((c) => `
        <li><button class="watch-item" data-id="${c.customer_id}">
          ${avatar(c)}
          <span class="who"><strong>${esc(c.full_name || `#${c.customer_id}`)}</strong><span>#${c.customer_id} · ${esc(c.region)} · ${esc(pretty(c.primary_channel))}</span></span>
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
  `<div class="cust">${avatar(c)}<div><strong>${esc(c.full_name || `#${c.customer_id}`)}</strong><span>#${c.customer_id} · ${esc(c.region)}</span></div></div>`;

async function loadCustomers() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset, sort: $("#f-sort").value });
  if ($("#f-risk").value) params.set("risk", $("#f-risk").value);
  if ($("#f-region").value) params.set("region", $("#f-region").value);
  if ($("#f-search").value.trim()) params.set("search", $("#f-search").value.trim());
  const { from, to } = joinedRange();
  if (from) params.set("joined_from", from);
  if (to) params.set("joined_to", to);

  const { total, customers } = await api(`/api/customers?${params}`);
  rememberCustomers(customers);
  $("#cust-rows").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}" tabindex="0">
          <td>${custCell(c)}</td>
          <td class="gender-cell">${esc(c.gender ?? "–")}</td>
          <td>${c.age}</td>
          <td>${esc(c.phone ?? "–")}</td>
          <td>${fmtJoined(c.joined_on)}</td>
          <td>${esc(pretty(c.primary_channel))}</td>
          <td>${c.products_number}</td>
          <td>${c.days_since_last_txn}</td>
          <td class="stage">${esc(c.lifecycle_stage)}</td>
          <td><div class="risk-cell">${riskPill(c.risk_band)}${meter(c.churn_probability, c.risk_band)}<span class="muted">${fmtPct(c.churn_probability)}</span></div></td>
        </tr>`).join("")
    : `<tr class="empty-row"><td colspan="10">No customers match these filters</td></tr>`;

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
let drawerTab = "summary";           // remembered while you click from customer to customer
const fmtDate = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "–");

// ---- Loading a profile fast and safely ----
// knownCustomers: the rows already on screen, so the panel can show the right name instantly.
// profileCache:   profiles fetched (or being fetched) in the last minute, keyed by customer id.
// openSeq:        a ticket per click. A slow answer for an EARLIER click is ignored, so the
//                 panel can never show a different customer from the one you clicked last.
const knownCustomers = new Map();
const profileCache = new Map();
const PROFILE_TTL_MS = 60 * 1000;
let openSeq = 0;

function rememberCustomers(list) {
  (list || []).forEach((c) => knownCustomers.set(String(c.customer_id), c));
}

function fetchProfile(id) {
  id = String(id);
  const hit = profileCache.get(id);
  if (hit && Date.now() - hit.t < PROFILE_TTL_MS) return hit.promise;
  const promise = api(`/api/customers/${encodeURIComponent(id)}`)
    .catch((err) => { profileCache.delete(id); throw err; });
  profileCache.set(id, { t: Date.now(), promise });
  return promise;
}

async function openCustomer(id) {
  id = String(id);
  const seq = ++openSeq;
  openCustomerId = id;
  const row = knownCustomers.get(id) || {};
  // Show who you clicked straight away; the details fill in when they arrive.
  $("#drawer-body").innerHTML = `
    <div class="drawer-head">
      ${avatar({ ...row, customer_id: id }, "lg")}
      <div><p class="eyebrow">Customer 360</p><h2>${esc(row.full_name || `#${id}`)}</h2>
        <p class="muted">#${esc(id)}${row.region ? ` · ${esc(row.region)}` : ""}</p></div>
    </div>
    <p class="muted loading-line"><span class="spinner"></span>Loading full profile…</p>`;
  $("#drawer").classList.remove("hidden");
  $("#drawer-backdrop").classList.remove("hidden");
  try {
    const c = await fetchProfile(id);
    if (seq !== openSeq) return;            // you have clicked someone else since: ignore this answer
    renderCustomer(c);
  } catch (err) {
    if (seq !== openSeq) return;
    $("#drawer-body").innerHTML = `<p class="muted">Could not load customer: ${esc(err.message)}</p>`;
  }
}

// Start loading a profile when the mouse rests on a row for a moment, so a click opens it instantly.
let hoverTimer = null;
document.addEventListener("pointerover", (e) => {
  const item = e.target.closest?.("[data-id]");
  clearTimeout(hoverTimer);
  if (item) hoverTimer = setTimeout(() => fetchProfile(item.dataset.id).catch(() => {}), 150);
});

/** The Customer 360 panel: Summary · Personal & KYC · Accounts & activity. */
function renderCustomer(c) {
  const p = c.profile || {};
  const kin = p.next_of_kin || {};
  const chips = Object.entries(c.channels).map(([name, v]) =>
    name.includes("(90d)")
      ? `<span class="chip on">${esc(name.replace(" (90d)", ""))}: ${v}</span>`
      : `<span class="chip ${v ? "on" : "off"}">${esc(name)}</span>`).join("");
  // kv(): label/value tiles. Values are escaped unless passed as {html: "..."} (our own markup only).
  const kv = (pairs, cls = "") => `<div class="kv ${cls}">${pairs.map(([k, v]) =>
    `<div><span>${k}</span><span>${v && typeof v === "object" ? v.html : esc(v ?? "–")}</span></div>`).join("")}</div>`;
  const dormantNote = c.dormancy_status === "dormant"
    ? `<p class="stage-line"><span class="pill dormant">dormant</span> ${c.days_since_last_txn - 90} days past the 90-day rule</p>` : "";
  const tel = (num) => esc(String(num ?? "").replace(/\s/g, ""));
  const kycBadge = p.kyc_status === "Verified"
    ? `<span class="badge ok">✓ KYC verified</span>` : `<span class="badge warn">KYC: ${esc(p.kyc_status ?? "unknown")}</span>`;

  $("#drawer-body").innerHTML = `
    <div class="drawer-head">
      ${avatar({ ...c, initials: p.initials, full_name: p.full_name }, "lg")}
      <div><p class="eyebrow">Customer 360</p><h2>${esc(p.full_name || `#${c.customer_id}`)}</h2>
        <p class="muted">#${c.customer_id} · ${esc(p.account_number ?? "")} · customer since ${fmtJoined(c.joined_on)}</p>
        <div class="badges">${kycBadge}<span class="badge">${esc(p.gender ?? c.gender ?? "–")}</span>
          <span class="badge">${p.age ?? c.age} yrs</span><span class="badge">${esc(c.region)}</span>
          <span class="badge">${esc(c.lifecycle_stage)}</span></div></div>
    </div>

    <div class="dtabs" role="tablist">
      <button class="dtab" data-dtab="summary">Summary</button>
      <button class="dtab" data-dtab="personal">Personal &amp; KYC</button>
      <button class="dtab" data-dtab="accounts">Accounts &amp; activity</button>
    </div>

    <div class="dpane" data-dpane="summary">
      <div class="rings">
        ${ring(c.churn_probability, bandColour(c.risk_band), { size: 170, small: true, label: "churn risk" })}
        ${dormancyRing(c, 170)}
      </div>
      <p class="stage-line">${riskPill(c.risk_band)} &nbsp;${esc(c.lifecycle_stage)}</p>
      ${dormantNote}
      <div class="contact">
        <a href="tel:${tel(p.phone)}"><svg><use href="#i-phone"/></svg><span>${esc(p.phone ?? "–")}<small>${esc(p.phone_network ?? "")} · mobile</small></span></a>
        <a href="mailto:${esc(p.email ?? "")}"><svg><use href="#i-mail"/></svg><span>${esc(p.email ?? "–")}<small>email</small></span></a>
        <div><svg><use href="#i-pin"/></svg><span>${esc(p.address ?? "–")}<small>${esc(p.digital_address ?? "")} · ${esc(p.home_branch ?? "")}</small></span></div>
      </div>
      <h3 class="section-title">Churn warning signs</h3>${signsList(c.warning_signs)}
      <h3 class="section-title">Dormancy signals</h3>${signsList(c.dormancy_reasons)}
    </div>

    <div class="dpane" data-dpane="personal">
      <h3 class="section-title">Identity</h3>
      ${kv([["Full name", p.full_name], ["Gender", p.gender], ["Date of birth", fmtDate(p.date_of_birth)],
            ["Age", `${p.age ?? c.age} years`], ["Marital status", p.marital_status], ["Ghana Card", p.ghana_card],
            ["Preferred language", p.preferred_language], ["KYC status", p.kyc_status], ["Nationality", "Ghanaian"]], "three")}
      <h3 class="section-title">Contact &amp; address</h3>
      ${kv([["Mobile", { html: `<a href="tel:${tel(p.phone)}">${esc(p.phone ?? "–")}</a>` }], ["Network", p.phone_network],
            ["Email", { html: `<a href="mailto:${esc(p.email ?? "")}">${esc(p.email ?? "–")}</a>` }],
            ["Residential address", p.address], ["Digital address (GPS)", p.digital_address], ["Region", c.region]], "three")}
      <h3 class="section-title">Employment &amp; income</h3>
      ${kv([["Employment", p.employment_status], ["Occupation", p.occupation],
            ["Monthly income", p.monthly_income != null ? fmtMoney(p.monthly_income) : "–"],
            ["Income tier", p.income_tier], ["Salary paid in here", c.salary_credited ? "Yes" : "No"],
            ["Active member", c.active_member ? "Yes" : "No"]], "three")}
      <h3 class="section-title">Next of kin</h3>
      ${kv([["Name", kin.name], ["Relationship", kin.relationship],
            ["Phone", { html: `<a href="tel:${tel(kin.phone)}">${esc(kin.phone ?? "–")}</a>` }]], "three")}
      <p class="synthetic-note"><svg><use href="#i-info"/></svg>Personal details are synthetic demo data, generated consistently for each customer.</p>
    </div>

    <div class="dpane" data-dpane="accounts">
      <h3 class="section-title">Account</h3>
      ${kv([["Account number", p.account_number], ["Account type", p.account_type], ["Home branch", p.home_branch],
            ["Balance", { html: maskedMoney(c.balance) }], ["Customer since", `${fmtJoined(c.joined_on)} (${c.tenure} yrs)`],
            ["Credit score", c.credit_score], ["Products held", c.products_number],
            ["Credit card", c.credit_card ? "Yes" : "No"], ["Primary channel", pretty(c.primary_channel)]], "three")}
      <h3 class="section-title">Products</h3>
      <div class="chips">${(p.products || []).map((x) => `<span class="chip on">${esc(x)}</span>`).join("")}</div>
      <h3 class="section-title">Channels</h3><div class="chips">${chips}</div>
      <h3 class="section-title">Activity · last 90 days</h3>
      ${kv([["Transactions", c.total_txn_count_90d], ["Days since last", c.days_since_last_txn],
            ["Failed txns", c.failed_txn_count_90d], ["Complaints", c.complaints_90d],
            ["Branch visits", c.branch_visits_90d], ["ATM withdrawals", c.atm_withdrawals_90d],
            ["Balance change", `${c.balance_change_3m_pct}%`], ["Salary paid in", c.salary_credited ? "Yes" : "No"],
            ["Country (Kaggle)", c.country]], "three")}
      <p class="foot-note">Scored by ${esc(c.model_version ?? "–")} at ${fmtTime(c.scored_at)}</p>
    </div>`;
  setDrawerTab(drawerTab);
}

function setDrawerTab(tab) {
  drawerTab = tab;
  document.querySelectorAll("[data-dtab]").forEach((b) => b.classList.toggle("active", b.dataset.dtab === tab));
  document.querySelectorAll("[data-dpane]").forEach((p) => p.classList.toggle("active", p.dataset.dpane === tab));
}
$("#drawer-body").addEventListener("click", (e) => {
  const b = e.target.closest("[data-dtab]");
  if (b) setDrawerTab(b.dataset.dtab);
});

function closeDrawer() {
  openSeq++;                         // any profile still loading must not pop up later
  openCustomerId = null;
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
  rememberCustomers(customers);

  const going = dStatus === "going";
  $("#d-head").innerHTML = `<th>Customer</th><th>Days idle</th><th>Txns (90d)</th><th>Balance</th>
    <th>${going ? "Going dormant" : "Days past rule"}</th><th>Churn risk</th><th>Main reason</th>`;
  $("#d-rows").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}" tabindex="0">
          <td>${custCell(c)}</td>
          <td>${c.days_since_last_txn}</td>
          <td>${c.total_txn_count_90d}</td>
          <td>${maskedMoney(c.balance)}</td>
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
// CUSTOMER BASE (analytics of the whole book)
// =====================================================================
let ovScope = "live";
const SCOPE_NAMES = { live: "live book", historical: "historical book", all: "whole book" };
const STATUS_VARS = { "Active": "--st-active", "Slowing": "--st-slowing", "Going dormant": "--st-going",
                      "Likely to churn": "--st-churn", "Dormant": "--st-dormant", "Churned": "--st-churned" };
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const statusColour = (s) => cssVar(STATUS_VARS[s] || "--unscored");

/** Ordered tiers get one blue, light -> dark (a sequential scale, never a rainbow). */
function ramp(n, from = 0.38) {
  const rgb = cssVar("--chart-rgb");
  return Array.from({ length: n }, (_, i) => `rgba(${rgb}, ${(from + ((1 - from) * i) / Math.max(1, n - 1)).toFixed(2)})`);
}

/** Small value labels at the end of each bar (e.g. "32.4%"), read from dataset.valueText. */
const valueLabels = {
  id: "valueLabels",
  afterDatasetsDraw(chart) {
    const ds = chart.data.datasets[0];
    if (!ds?.valueText) return;
    const horizontal = chart.options.indexAxis === "y";
    const { ctx } = chart;
    ctx.save();
    ctx.font = "600 11px Poppins, system-ui, sans-serif";
    ctx.fillStyle = cssVar("--text-2");
    chart.getDatasetMeta(0).data.forEach((bar, i) => {
      const text = ds.valueText[i];
      if (!text) return;
      if (horizontal) { ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(text, bar.x + 6, bar.y); }
      else { ctx.textAlign = "center"; ctx.textBaseline = "bottom"; ctx.fillText(text, bar.x, bar.y - 4); }
    });
    ctx.restore();
  },
};

/** Tooltip lines shared by every breakdown chart. */
function bucketTooltip(r) {
  const lines = [` ${fmtInt(r.customers)} customers · ${pct(r.share)} of the book`];
  if (r.churn_rate != null) lines.push(` Churned: ${pct(r.churn_rate)}`);
  if (r.avg_churn_probability != null) lines.push(` Avg churn risk: ${pct(r.avg_churn_probability)}`);
  if (r.dormant_rate != null) lines.push(` Dormant: ${pct(r.dormant_rate)}`);
  if (r.avg_balance != null) lines.push(` Avg balance: ${fmtMoney(r.avg_balance)}`);
  return lines;
}

/** One bar chart of a breakdown (share of customers per bucket). */
function shareBars(key, sel, rows, { horizontal = false, colours = null, label = (r) => r.label, maxBar = 34, values = true } = {}) {
  if (!chartsAvailable()) return;
  if (!rows.length) { setEmpty(sel, "No customers in this view yet."); return; }
  const data = rows.map((r) => +(r.share * 100).toFixed(1));
  const scales = axes({ horizontal, pct: true });
  const valueAxis = horizontal ? scales.x : scales.y;
  const categoryAxis = horizontal ? scales.y : scales.x;
  categoryAxis.ticks.autoSkip = false;          // every bucket keeps its name
  valueAxis.suggestedMax = Math.min(100, Math.ceil((Math.max(...data) * 1.18) / 5) * 5);
  drawChart(key, sel, {
    type: "bar",
    data: {
      labels: rows.map(label),
      datasets: [{ label: "Share of customers", data, extra: rows,
                   valueText: values ? data.map((v) => `${v}%`) : null,
                   backgroundColor: colours || cssVar("--chart-line"), hoverBackgroundColor: cssVar("--accent-2"),
                   borderRadius: 5, borderSkipped: "start", maxBarThickness: maxBar }],
    },
    options: {
      indexAxis: horizontal ? "y" : "x", responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      layout: { padding: horizontal ? { right: 46 } : { top: 18 } },
      scales,
      plugins: { tooltip: { callbacks: {
        title: (items) => items[0].label,
        label: (ctx) => bucketTooltip(ctx.dataset.extra[ctx.dataIndex]),
      } } },
    },
    plugins: [valueLabels],
  });
}

function doughnut(key, sel, labels, values, colours, extra) {
  if (!chartsAvailable()) return;
  drawChart(key, sel, {
    type: "doughnut",
    data: { labels, datasets: [{ data: values, extra, backgroundColor: colours, borderColor: cssVar("--surface"),
                                 borderWidth: 2, hoverOffset: 6, spacing: 1 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "74%", animation: { duration: 600 },
      plugins: { tooltip: { callbacks: { label: (ctx) => bucketTooltip(ctx.dataset.extra[ctx.dataIndex]) } } },
    },
  });
}

const legendRows = (rows, colourOf) => rows.map((r) =>
  `<li><i style="background:${colourOf(r)}"></i><span>${esc(r.label)}</span><b>${fmtInt(r.customers)}</b><em>${pct(r.share)}</em></li>`).join("");

async function loadOverview() {
  const box = $("#ctab-base");
  box.classList.add("loading");
  try {
    renderOverview(await api(`/api/customers/overview?scope=${ovScope}`));
  } finally {
    box.classList.remove("loading");
  }
}

function renderOverview(d) {
  const k = d.kpis, st = k.status, n = k.customers || 0, D = d.dimensions;
  const share = (x) => (n ? x / n : 0);

  // ---- Headline KPIs ----
  $("#ov-total").textContent = fmtInt(n);
  $("#ov-total-sub").textContent = d.scope === "live" ? `+${fmtInt(k.new_30d)} joined in the last 30 days`
    : d.scope === "historical" ? "historical snapshot (Kaggle)" : "live + historical customers";
  $("#ov-active").textContent = fmtInt(st["Active"]);
  $("#ov-active-sub").textContent = `${pct(share(st["Active"]))} of customers`;
  $("#ov-dormant").textContent = fmtInt(st["Dormant"]);
  $("#ov-dormant-sub").textContent = `${pct(share(st["Dormant"]))} · 90+ days with no activity`;
  if (d.scope === "live") {
    $("#ov-lost-label").textContent = "Likely to churn";
    $("#ov-lost").textContent = fmtInt(st["Likely to churn"]);
    $("#ov-lost-sub").textContent = `${pct(share(st["Likely to churn"]))} flagged by the churn model`;
  } else if (d.scope === "historical") {
    $("#ov-lost-label").textContent = "Churned";
    $("#ov-lost").textContent = fmtInt(st["Churned"]);
    $("#ov-lost-sub").textContent = `${pct(share(st["Churned"]))} left the bank`;
  } else {
    $("#ov-lost-label").textContent = "Churned · at risk";
    $("#ov-lost").textContent = fmtInt(st["Churned"] + st["Likely to churn"]);
    $("#ov-lost-sub").textContent = `${fmtInt(st["Churned"])} churned · ${fmtInt(st["Likely to churn"])} at risk`;
  }

  // ---- Small stat tiles ----
  const stat = (label, value, note, title = "") =>
    `<div class="stat" title="${esc(title)}"><span>${label}</span><b>${value}</b><em>${note}</em></div>`;
  $("#ov-stats").innerHTML = [
    stat("Total deposits", fmtMoneyShort(k.total_balance), `${fmtMoneyShort(k.avg_balance)} per customer`, fmtMoney(k.total_balance)),
    stat("Median monthly income", fmtMoney(k.median_income), `average ${fmtMoney(k.avg_income)}`),
    stat("Median age", `${Math.round(k.median_age ?? 0)} yrs`, `average ${k.avg_age ?? "–"} yrs`),
    stat("Years with the bank", `${(+k.avg_tenure || 0).toFixed(1)} yrs`, "average tenure"),
    stat("Products per customer", (+k.avg_products || 0).toFixed(2), `${pct(share(k.credit_card), 0)} hold a credit card`),
    stat("Credit score", fmtInt(k.avg_credit_score), "average (300–850)"),
  ].join("");

  // ---- Status donut ----
  const status = D.status.filter((r) => r.customers > 0);
  doughnut("ov-status", "#ov-status-chart", status.map((r) => r.label), status.map((r) => r.customers),
           status.map((r) => statusColour(r.label)), status);
  $("#ov-status-total").textContent = fmtInt(n);
  $("#ov-status-legend").innerHTML = legendRows(status, (r) => statusColour(r.label));

  // ---- Age ----
  shareBars("ov-age", "#ov-age-chart", D.age);
  const under35 = D.age.filter((r) => r.label === "18–24" || r.label === "25–34").reduce((a, r) => a + r.share, 0);
  $("#ov-age-note").textContent = `share of customers in each age band · ${pct(under35)} are under 35`;

  // ---- Gender ----
  const g = D.gender;
  const gColour = (r) => cssVar(r.label === "Male" ? "--g-male" : "--g-female");
  doughnut("ov-gender", "#ov-gender-chart", g.map((r) => r.label), g.map((r) => r.customers), g.map(gColour), g);
  const lead = g[0];
  $("#ov-gender-lead").textContent = lead ? pct(lead.share, 0) : "–";
  $("#ov-gender-lead-label").textContent = lead ? lead.label.toLowerCase() : "";
  $("#ov-gender-legend").innerHTML = legendRows(g, gColour);
  const riskRow = d.scope === "historical"
    ? ["Churned", (r) => pct(r.churn_rate)]
    : ["Avg churn risk", (r) => pct(r.avg_churn_probability)];
  const rowsCmp = [
    ["Average age", (r) => `${r.avg_age} yrs`],
    ["Avg monthly income", (r) => fmtMoney(r.avg_income)],
    ["Avg balance", (r) => fmtMoney(r.avg_balance)],
    ["Dormant", (r) => pct(r.dormant_rate)],
    riskRow,
  ];
  $("#ov-gender-compare").innerHTML = `<tr><th></th>${g.map((r) => `<th>${esc(r.label)}</th>`).join("")}</tr>` +
    rowsCmp.map(([name, f]) => `<tr><td>${name}</td>${g.map((r) => `<td>${f(r)}</td>`).join("")}</tr>`).join("");

  // ---- Income & balance tiers (ordered: one blue, light -> dark) ----
  shareBars("ov-income", "#ov-income-chart", D.income, { horizontal: true, colours: ramp(D.income.length), maxBar: 26 });
  $("#ov-income-note").textContent = `monthly income, share of customers · median ${fmtMoney(k.median_income)}`;
  const balColours = ramp(D.balance.length).map((c, i) => (D.balance[i]?.label === "Zero balance" ? cssVar("--neutral-bar") : c));
  shareBars("ov-balance", "#ov-balance-chart", D.balance, { colours: balColours, label: (r) => r.label.replace("GH₵ ", "") });
  $("#ov-balance-note").textContent = `account balance in GH₵, share of customers · ${pct(share(k.zero_balance))} hold nothing`;

  // ---- Status mix inside each age band (100% stacked) ----
  drawAgeStatus(d);

  // ---- Region, channel & adoption ----
  shareBars("ov-region", "#ov-region-chart", D.region, { horizontal: true, maxBar: 18 });
  shareBars("ov-channel", "#ov-channel-chart", D.channel, { horizontal: true, maxBar: 16, label: (r) => pretty(r.label) });
  const adopt = [["Any digital channel", k.digital], ["Mobile app", k.uses_app], ["USSD", k.uses_ussd],
                 ["MoMo linked", k.momo_linked], ["Internet banking", k.uses_internet_banking],
                 ["Salary paid in", k.salary_credited]];
  $("#ov-adoption").innerHTML = adopt.map(([name, v]) =>
    `<div class="adopt"><span>${name}</span><b>${pct(share(v), 0)}</b><span class="meter"><span style="width:${(share(v) * 100).toFixed(1)}%"></span></span></div>`).join("");

  // ---- Tenure, credit score, products ----
  shareBars("ov-tenure", "#ov-tenure-chart", D.tenure, { maxBar: 22, values: false, label: (r) => `${r.label}y` });
  shareBars("ov-credit", "#ov-credit-chart", D.credit, { colours: ramp(D.credit.length),
    label: (r) => r.label.split(" (")[0].replace("Very good", "V. good") });
  shareBars("ov-products", "#ov-products-chart", D.products, { maxBar: 40, label: (r) => `${r.label} product${r.label === "1" ? "" : "s"}` });

  // ---- Plain-language facts ----
  renderInsights(d);
  $("#ov-notes").textContent = Object.values(d.notes || {}).join(" ");
}

function drawAgeStatus(d) {
  if (!chartsAvailable()) return;
  const bands = d.dimensions.age.map((r) => r.label);
  const totals = Object.fromEntries(bands.map((b) => [b, 0]));
  d.age_status.forEach((r) => { totals[r.age_band] = (totals[r.age_band] || 0) + r.customers; });
  const present = d.status_order.filter((s) => d.age_status.some((r) => r.status === s && r.customers > 0));
  $("#ov-agestatus-legend").innerHTML = present.map((s) =>
    `<span><i class="sw" style="background:${statusColour(s)}"></i>${s}</span>`).join("");
  if (!bands.length) { setEmpty("#ov-agestatus-chart", "No customers in this view yet."); return; }
  const datasets = present.map((s) => ({
    label: s,
    data: bands.map((b) => {
      const hit = d.age_status.find((r) => r.age_band === b && r.status === s);
      return totals[b] ? +((100 * (hit?.customers || 0)) / totals[b]).toFixed(1) : 0;
    }),
    counts: bands.map((b) => d.age_status.find((r) => r.age_band === b && r.status === s)?.customers || 0),
    backgroundColor: statusColour(s), borderColor: cssVar("--surface"), borderWidth: { right: 2 },
    borderSkipped: false, maxBarThickness: 26, stack: "s",
  }));
  const scales = axes({ horizontal: true, pct: true });
  scales.x.max = 100; scales.x.stacked = true; scales.y.stacked = true;
  drawChart("ov-agestatus", "#ov-agestatus-chart", {
    type: "bar",
    data: { labels: bands, datasets },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      interaction: { mode: "index", axis: "y", intersect: false },
      scales,
      plugins: { tooltip: { callbacks: {
        title: (items) => `Age ${items[0].label} · ${fmtInt(totals[items[0].label])} customers`,
        label: (ctx) => ` ${ctx.dataset.label}: ${ctx.parsed.x}% (${fmtInt(ctx.dataset.counts[ctx.dataIndex])})`,
      } } },
    },
  });
}

function renderInsights(d) {
  const k = d.kpis, st = k.status, n = k.customers || 0, D = d.dimensions;
  if (!n) { $("#ov-insights").innerHTML = '<li><i>i</i><span>No customers in this view yet.</span></li>'; return; }
  const top = (rows) => rows.reduce((a, r) => (r.share > (a?.share ?? -1) ? r : a), null);
  const share = (x) => x / n;
  const age = top(D.age), income = top(D.income), regions = [...D.region].sort((a, b) => b.share - a.share);
  const male = D.gender.find((r) => r.label === "Male"), female = D.gender.find((r) => r.label === "Female");
  const facts = [
    `<strong>${fmtInt(n)}</strong> customers in the ${SCOPE_NAMES[d.scope]}: <strong>${pct(share(st["Active"]))}</strong> active, <strong>${pct(share(st["Dormant"]))}</strong> dormant.`,
    age && `The largest age group is <strong>${age.label}</strong> (${pct(age.share)}); the median customer is <strong>${Math.round(k.median_age)}</strong>.`,
    male && female && `<strong>${pct(male.share)}</strong> male and <strong>${pct(female.share)}</strong> female.`,
    income && `Most customers earn <strong>${esc(income.label)}</strong> a month (${pct(income.share)}); median income is <strong>${fmtMoney(k.median_income)}</strong>.`,
    `<strong>${pct(share(k.zero_balance))}</strong> hold a zero balance; deposits total <strong>${fmtMoneyShort(k.total_balance)}</strong>.`,
    regions.length > 1 && `<strong>${esc(regions[0].label)}</strong> is the biggest region (${pct(regions[0].share)}), then ${esc(regions[1].label)} (${pct(regions[1].share)}).`,
    `<strong>${pct(share(k.digital))}</strong> use at least one digital channel; <strong>${pct(share(k.momo_linked))}</strong> have MoMo linked.`,
  ];
  if (d.scope === "live") {
    const worst = D.age.reduce((a, r) => ((r.dormant_rate ?? 0) > (a?.dormant_rate ?? -1) ? r : a), null);
    if (worst) facts.push(`Dormancy is highest among customers aged <strong>${worst.label}</strong> (${pct(worst.dormant_rate)} dormant).`);
  } else {
    const worst = D.age.reduce((a, r) => ((r.churn_rate ?? 0) > (a?.churn_rate ?? -1) ? r : a), null);
    if (worst?.churn_rate != null) facts.push(`Customers aged <strong>${worst.label}</strong> churned the most: <strong>${pct(worst.churn_rate)}</strong> left.`);
  }
  $("#ov-insights").innerHTML = facts.filter(Boolean).map((f, i) => `<li><i>${i + 1}</i><span>${f}</span></li>`).join("");
}

document.querySelectorAll("[data-scope]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-scope]").forEach((x) => x.classList.toggle("active", x === b));
  ovScope = b.dataset.scope;
  loadOverview().catch(showError);
}));

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
    if ($("#view-customers").classList.contains("active")) await (custTab === "base" ? loadOverview() : loadCustomers());
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
    if ($("#view-customers").classList.contains("active")) await (custTab === "base" ? loadOverview() : loadCustomers());
    if (openCustomerId && !$("#drawer").classList.contains("hidden")) await openCustomer(openCustomerId);
  } finally {
    useCache = false;
  }
}

// Refresh on a timer, but skip it while the browser tab is hidden (saves Neon compute).
setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });

/** Start-up: draw the saved snapshot instantly (if this browser has one), then go live. */
async function boot() {
  const hash = location.hash.length > 1 ? location.hash.slice(1) : null;
  const saved = Object.entries(snapshot);
  if (saved.length) {
    saved.forEach(([path, data]) => apiCache.set(path, data));
    useCache = true;                     // the section's first draw also uses the snapshot
    if (hash) showView(hash);
    useCache = false;
    await redrawFromCache().catch(() => {});
    $("#status-text").textContent = "Saved view · updating…";
  } else if (hash) {
    showView(hash);
  }
  refreshAll();
}
boot();
