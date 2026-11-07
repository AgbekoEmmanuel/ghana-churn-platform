/* =====================================================================
   ChurnWatch GH - app.js
   All the behaviour of the platform. Plain JavaScript, no framework.

   How it works:
     1. fetch() asks our FastAPI backend for JSON (e.g. /api/summary).
     2. We put the numbers into the page and draw charts with Chart.js.
     3. Every REFRESH_MS milliseconds we do it again, so the screen stays live.
   ===================================================================== */

// ---------- Settings ----------
// If the page is served by FastAPI itself (http://127.0.0.1:8000), use the same
// address. If it's opened another way (e.g. VS Code Live Server), point at port 8000.
const API = location.port === "8000" ? "" : "http://127.0.0.1:8000";
const REFRESH_MS = 60 * 1000;      // refresh every 60 s (the simulator adds data every 5 min)
const PAGE_SIZE = 25;

// ---------- Small helpers ----------
const $ = (sel) => document.querySelector(sel);

/** Ask the API for JSON. Throws if the server answers with an error. */
async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  return res.json();
}

/** Make text safe to put inside HTML (stops odd characters breaking the page). */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const fmtInt = (n) => (n ?? 0).toLocaleString();
const fmtPct = (p) => (p == null ? "–" : `${(p * 100).toFixed(1)}%`);
const fmtMoney = (n) => "GH₵ " + Math.round(n ?? 0).toLocaleString();
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–");
// Nice display names for database codes (e.g. "mobile_app" -> "Mobile App", "ussd" -> "USSD").
const NAMES = { ussd: "USSD", momo: "MoMo", mobile_app: "Mobile app", internet_banking: "Internet banking", branch: "Branch" };
const pretty = (s) => NAMES[s] ?? String(s ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function riskPill(band) {
  const b = band || "unscored";
  return `<span class="pill ${b}">${b === "unscored" ? "not scored" : b}</span>`;
}

/** Read a colour token from styles.css so charts match light/dark mode. */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// =====================================================================
// TABS
// =====================================================================
function showView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "customers") loadCustomers();
  if (name === "model") loadModel();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto)));

// =====================================================================
// OVERVIEW: KPIs
// =====================================================================
async function loadSummary() {
  const s = await api("/api/summary");
  $("#kpi-live").textContent = fmtInt(s.live_customers);
  $("#kpi-new").textContent = `+${fmtInt(s.new_last_hour)} in the last hour`;
  $("#kpi-high").textContent = fmtInt(s.high_risk);
  $("#kpi-high-pct").textContent = s.scored ? `${((s.high_risk / s.scored) * 100).toFixed(1)}% of scored customers` : "nothing scored yet";
  $("#kpi-avg").textContent = fmtPct(s.avg_churn_probability);
  $("#kpi-hist").textContent = `historical churn rate: ${s.historical_churn_rate_pct ?? "–"}%`;
  $("#kpi-balance").textContent = fmtMoney(s.balance_at_risk);
  drawMix(s);
  return s;
}

// =====================================================================
// OVERVIEW: charts (Chart.js)
// =====================================================================
const charts = {};       // keep chart objects so we can update instead of re-creating

function chartsAvailable() {
  if (window.Chart) return true;
  document.querySelectorAll(".chart-box").forEach((box) => {
    box.innerHTML = '<p class="muted">Charts need an internet connection (Chart.js loads from a CDN).</p>';
  });
  return false;
}

function baseOptions() {
  const grid = cssVar("--border"), text = cssVar("--muted");
  Chart.defaults.color = text;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
    plugins: { legend: { labels: { boxWidth: 12 } } },
    scales: { x: { grid: { color: grid } }, y: { grid: { color: grid }, beginAtZero: true } },
  };
}

async function loadTrends() {
  const rows = await api("/api/trends?minutes=720&bucket_minutes=15");
  if (!chartsAvailable()) return;
  const labels = rows.map((r) => fmtTime(r.bucket));
  const data = {
    labels,
    datasets: [
      { type: "bar", label: "New customers", data: rows.map((r) => r.new_customers),
        backgroundColor: cssVar("--brand") + "99", yAxisID: "y" },
      { type: "bar", label: "High risk", data: rows.map((r) => r.high_risk),
        backgroundColor: cssVar("--high") + "cc", yAxisID: "y" },
      { type: "line", label: "Avg churn risk (%)", data: rows.map((r) => r.avg_churn_probability == null ? null : +(r.avg_churn_probability * 100).toFixed(1)),
        borderColor: cssVar("--accent"), backgroundColor: cssVar("--accent"), tension: 0.3, yAxisID: "y1", spanGaps: true },
    ],
  };
  if (charts.trend) { charts.trend.data = data; charts.trend.update(); return; }
  const opts = baseOptions();
  opts.scales.y.title = { display: true, text: "customers" };
  opts.scales.y1 = { position: "right", beginAtZero: true, max: 100, grid: { drawOnChartArea: false },
                     title: { display: true, text: "avg risk %" } };
  charts.trend = new Chart($("#trend-chart"), { data, options: opts });
}

function drawMix(s) {
  if (!chartsAvailable()) return;
  const data = {
    labels: ["High", "Medium", "Low", "Not scored"],
    datasets: [{ data: [s.high_risk, s.medium_risk, s.low_risk, s.waiting_to_score],
                 backgroundColor: [cssVar("--high"), cssVar("--medium"), cssVar("--low"), cssVar("--unscored")],
                 borderWidth: 0 }],
  };
  if (charts.mix) { charts.mix.data = data; charts.mix.update(); return; }
  charts.mix = new Chart($("#mix-chart"), {
    type: "doughnut", data,
    options: { responsive: true, maintainAspectRatio: false, cutout: "65%",
               plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } } },
  });
}

async function loadBreakdown() {
  const dim = $("#breakdown-select").value;
  const rows = await api(`/api/breakdown/${dim}`);
  if (!chartsAvailable()) return;
  const data = {
    labels: rows.map((r) => (dim === "products" ? `${r.label} product(s)` : pretty(r.label))),
    datasets: [{ label: "Avg churn risk (%)", data: rows.map((r) => +((r.avg_churn_probability ?? 0) * 100).toFixed(1)),
                 backgroundColor: cssVar("--brand") + "cc", borderRadius: 4 }],
  };
  if (charts.breakdown) { charts.breakdown.data = data; charts.breakdown.update(); return; }
  const opts = baseOptions();
  opts.indexAxis = "y";                               // horizontal bars
  opts.plugins.legend = { display: false };
  opts.plugins.tooltip = { callbacks: { afterLabel: (ctx) => `${rows[ctx.dataIndex]?.customers ?? ""} customers` } };
  charts.breakdown = new Chart($("#breakdown-chart"), { type: "bar", data, options: opts });
}
$("#breakdown-select").addEventListener("change", () => {
  if (charts.breakdown) { charts.breakdown.destroy(); charts.breakdown = null; }
  loadBreakdown().catch(showError);
});

async function loadTopRisk() {
  const { customers } = await api("/api/customers?risk=high&limit=8");
  $("#top-risk").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}">
          <td><strong>${c.customer_id}</strong><div class="muted">${esc(c.region)} · ${esc(pretty(c.primary_channel))}</div></td>
          <td class="stage">${esc(c.lifecycle_stage)}</td>
          <td style="text-align:right">${fmtPct(c.churn_probability)}</td>
        </tr>`).join("")
    : `<tr class="empty-row"><td>No high-risk customers right now</td></tr>`;
}

// =====================================================================
// CUSTOMERS TABLE
// =====================================================================
const REGIONS = ["Greater Accra", "Ashanti", "Western", "Eastern", "Central",
                 "Northern", "Volta", "Bono", "Upper East", "Upper West"];
$("#f-region").insertAdjacentHTML("beforeend", REGIONS.map((r) => `<option>${r}</option>`).join(""));

let offset = 0;

async function loadCustomers() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset, sort: $("#f-sort").value });
  if ($("#f-risk").value) params.set("risk", $("#f-risk").value);
  if ($("#f-region").value) params.set("region", $("#f-region").value);
  if ($("#f-search").value.trim()) params.set("search", $("#f-search").value.trim());

  const { total, customers } = await api(`/api/customers?${params}`);
  $("#cust-rows").innerHTML = customers.length
    ? customers.map((c) => `
        <tr data-id="${c.customer_id}">
          <td><strong>${c.customer_id}</strong></td>
          <td>${c.age}</td>
          <td>${esc(c.region)}</td>
          <td>${esc(pretty(c.primary_channel))}</td>
          <td>${c.products_number}</td>
          <td>${c.total_txn_count_90d}</td>
          <td>${c.days_since_last_txn}</td>
          <td class="stage">${esc(c.lifecycle_stage)}</td>
          <td>${riskPill(c.risk_band)} <span class="muted">${fmtPct(c.churn_probability)}</span></td>
        </tr>`).join("")
    : `<tr class="empty-row"><td colspan="9">No customers match these filters</td></tr>`;

  const end = Math.min(offset + PAGE_SIZE, total);
  $("#page-info").textContent = total ? `${offset + 1}–${end} of ${fmtInt(total)}` : "";
  $("#prev").disabled = offset === 0;
  $("#next").disabled = end >= total;
}

// Re-load when a filter changes (search waits until you stop typing for 300 ms).
let searchTimer;
$("#f-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { offset = 0; loadCustomers().catch(showError); }, 300);
});
["#f-risk", "#f-region", "#f-sort"].forEach((id) =>
  $(id).addEventListener("change", () => { offset = 0; loadCustomers().catch(showError); }));
$("#prev").addEventListener("click", () => { offset = Math.max(0, offset - PAGE_SIZE); loadCustomers().catch(showError); });
$("#next").addEventListener("click", () => { offset += PAGE_SIZE; loadCustomers().catch(showError); });

// Clicking any customer row (table or top-8 list) opens the 360 drawer.
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (row) openCustomer(row.dataset.id);
});

// =====================================================================
// CUSTOMER 360 DRAWER
// =====================================================================
function riskBar(p, band) {
  const colour = cssVar(`--${band || "unscored"}`);
  return `<div class="bar"><span style="width:${((p ?? 0) * 100).toFixed(1)}%;background:${colour}"></span></div>`;
}

function signsList(signs) {
  return signs.length
    ? `<ul class="signs">${signs.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>`
    : `<p class="ok-msg">No warning signs: this customer looks healthy.</p>`;
}

async function openCustomer(id) {
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

    $("#drawer-body").innerHTML = `
      <p class="muted">Customer 360</p>
      <h2 style="font-size:1.4rem">#${c.customer_id}</h2>
      <p class="muted">${esc(c.region)} · ${c.age} yrs · arrived ${fmtTime(c.created_at)}</p>

      <div class="gauge" style="margin-top:16px">
        <span class="gauge-value">${fmtPct(c.churn_probability)}</span>${riskPill(c.risk_band)}
      </div>
      ${riskBar(c.churn_probability, c.risk_band)}
      <p><strong>Stage:</strong> ${esc(c.lifecycle_stage)}</p>

      <h3>Warning signs</h3>${signsList(c.warning_signs)}
      <h3>Channels</h3><div class="chips">${chips}</div>
      <h3>Activity (last 90 days)</h3>
      ${kv([["Transactions", c.total_txn_count_90d], ["Days since last", c.days_since_last_txn],
            ["Failed txns", c.failed_txn_count_90d], ["Complaints", c.complaints_90d],
            ["Branch visits", c.branch_visits_90d], ["ATM withdrawals", c.atm_withdrawals_90d],
            ["Balance change", `${c.balance_change_3m_pct}%`], ["Salary paid in", c.salary_credited ? "Yes" : "No"]])}
      <h3>Profile</h3>
      ${kv([["Balance", fmtMoney(c.balance)], ["Products", c.products_number],
            ["Tenure", `${c.tenure} yrs`], ["Credit score", c.credit_score],
            ["Active member", c.active_member ? "Yes" : "No"], ["Credit card", c.credit_card ? "Yes" : "No"],
            ["Primary channel", pretty(c.primary_channel)], ["Country (Kaggle)", c.country]])}
      <p class="muted" style="margin-top:16px">Scored by ${esc(c.model_version ?? "–")} at ${fmtTime(c.scored_at)}</p>`;
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
    const r = await api("/api/score", { method: "POST", body: JSON.stringify(body) });
    box.innerHTML = `
      <div class="gauge"><span class="gauge-value">${fmtPct(r.churn_probability)}</span>${riskPill(r.risk_band)}</div>
      ${riskBar(r.churn_probability, r.risk_band)}
      <p><strong>Stage:</strong> ${esc(r.lifecycle_stage)}</p>
      <h3 class="muted" style="font-size:.8rem;text-transform:uppercase">Warning signs</h3>
      ${signsList(r.warning_signs)}
      <p class="muted" style="margin-top:16px">Not saved: this only asks the model. Model ${esc(r.model_version)}</p>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">Could not score: ${esc(err.message)}</p>`;
  }
});

// =====================================================================
// MODEL PAGE
// =====================================================================
async function loadModel() {
  const m = await api("/api/model");
  const x = m.metrics;
  const cell = (v, label) => `<div class="metric"><b>${v}</b><span>${label}</span></div>`;
  $("#model-info").innerHTML = [
    cell(esc(m.model_name), "Model"),
    cell(esc(m.model_version), "Version"),
    cell(m.threshold.toFixed(2), "Decision threshold"),
    cell(new Date(m.trained_at).toLocaleDateString(), "Trained"),
    cell(x.roc_auc.toFixed(3), "ROC-AUC"),
    cell(x.pr_auc.toFixed(3), "PR-AUC"),
    cell(fmtPct(x.precision), "Precision: flagged who left"),
    cell(fmtPct(x.recall), "Recall: leavers we caught"),
    cell(x.roc_auc_without_ghana_features?.toFixed(3) ?? "–", "Kaggle-only ROC-AUC"),
    cell(fmtInt(x.confusion_matrix.tp), "Churners caught (test)"),
    cell(fmtInt(x.confusion_matrix.fn), "Churners missed (test)"),
    cell(fmtInt(x.confusion_matrix.fp), "False alarms (test)"),
  ].join("");
}

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
    $("#live-dot").className = "dot ok";
    const last = s.last_customer_at ? `last customer ${fmtTime(s.last_customer_at)}` : "no live customers yet";
    $("#status-text").textContent = `Live · updated ${fmtTime(new Date().toISOString())} · ${last}`;
  } catch (err) {
    showError(err);
  }
}

$("#refresh-btn").addEventListener("click", refreshAll);

// Refresh on a timer, but skip it while the browser tab is hidden (saves Neon compute).
setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });

refreshAll();
