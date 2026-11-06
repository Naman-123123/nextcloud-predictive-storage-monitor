"use strict";

const $ = (sel) => document.querySelector(sel);
const REFRESH_MS = 15000;
const state = { overview: null, charts: {}, scope: "system", range: 30 };

// ------------------------------------------------------------------ formatting
function fmtBytes(n) {
  n = Number(n || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i < 2 ? `${n.toFixed(0)} ${units[i]}` : `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`;
}
function fmtDays(d) {
  if (d === null || d === undefined) return "—";
  if (d < 1 / 24) return `${Math.max(d * 1440, 1).toFixed(0)} min`;
  if (d < 1) return `${(d * 24).toFixed(1)} h`;
  if (d >= 365) return `${(d / 365).toFixed(1)} yr`;
  return `${d.toFixed(1)} d`;
}
function fmtDate(iso, withTime = true) {
  if (!iso) return "—";
  const opts = { day: "2-digit", month: "short", year: "numeric" };
  if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
  return new Date(iso).toLocaleString(undefined, opts);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function withAlpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
function statusFor(days, t) {
  if (days === null || days === undefined) return "good";
  if (days <= t.days_crit) return "critical";
  if (days <= t.days_warn) return "warning";
  return "good";
}
const ICON = { critical: "⛔", warning: "⚠", good: "✓" };
const LABEL = { critical: "Critical", warning: "Warning", good: "OK" };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// ------------------------------------------------------------------ overview & KPIs
function renderOverview(o) {
  state.overview = o;
  const pill = $("#mode-pill");
  pill.textContent = o.mode === "demo" ? "Demo mode (simulated Nextcloud)" : "Live: Nextcloud";
  pill.className = `pill ${o.mode === "demo" ? "demo" : "live"}`;
  $("#last-run").textContent = o.collector.last_run ? `Last collection ${fmtDate(o.collector.last_run)} · every ${o.collector.interval_s}s` : "Waiting for first collection…";
  if (o.nextcloud_url) { const a = $("#nc-link"); a.href = o.nextcloud_url; a.hidden = false; }
  if (!state.rangeChosen && o.window_days < 1) {   // live demo: growth happens over minutes
    state.range = 1 / 24;
    $("#range").value = String(state.range);
    state.rangeChosen = true;
  }

  const err = $("#error-bar");
  err.hidden = !o.collector.last_error;
  err.textContent = o.collector.last_error ? `Last collection failed: ${o.collector.last_error}` : "";

  if (o.mode === "demo") {
    $("#demo-bar").hidden = false;
    const sel = $("#scenario");
    if (!sel.options.length) {
      for (const [k, v] of Object.entries(o.scenarios)) sel.add(new Option(v.label, k));
    }
    if (document.activeElement !== sel) sel.value = o.scenario;
    $("#scenario-desc").textContent = o.scenarios[sel.value]?.description || "";
    $("#sim-now").textContent = fmtDate(o.now);
  }

  const st = o.storage, p = o.prediction, t = o.thresholds;
  if (st) {
    const pct = st.used_bytes / st.capacity_bytes * 100;
    $("#k-used").textContent = `${pct.toFixed(1)}%`;
    $("#k-used-foot").textContent = `${fmtBytes(st.used_bytes)} of ${fmtBytes(st.capacity_bytes)}`;
    const m = $("#k-meter");
    m.style.width = `${Math.min(pct, 100)}%`;
    m.parentElement.className = `meter ${pct >= t.util_crit ? "critical" : pct >= t.util_warn ? "warning" : ""}`;
    $("#k-free").textContent = fmtBytes(Math.max(st.capacity_bytes - st.used_bytes, 0));
    $("#k-free-foot").textContent = `files ${fmtBytes(st.files_bytes)} · trash ${fmtBytes(st.trash_bytes)} · versions ${fmtBytes(st.versions_bytes)}`;
  }
  if (p) {
    $("#k-growth").textContent = `${fmtBytes(p.growth_bytes_per_day)}/day`;
    const win = p.window_days < 1 ? `${Math.round(p.window_days * 1440)}-min` : `${p.window_days}-day`;
    $("#k-growth-foot").textContent = `linear regression · ${win} window · R² ${p.r_squared === null ? "—" : p.r_squared.toFixed(2)}`;
    const s = statusFor(p.days_remaining, t);
    $("#k-days").innerHTML = p.days_remaining === null ? `<span class="status good">${ICON.good} Not filling</span>`
      : `<span class="status ${s}">${ICON[s]} ${fmtDays(p.days_remaining)}</span>`;
    $("#k-days-foot").textContent = p.predicted_full_at ? `full around ${fmtDate(p.predicted_full_at)}` : "usage is flat or decreasing";
  }
  const a = o.alerts;
  $("#k-alerts").innerHTML = a.critical + a.warning === 0 ? `<span class="status good">${ICON.good} 0</span>`
    : `<span class="status ${a.critical ? "critical" : "warning"}">${a.critical ? ICON.critical : ICON.warning} ${a.critical + a.warning}</span>`;
  $("#k-alerts-foot").textContent = `${a.critical} critical · ${a.warning} warning`;
  $("#k-savings").textContent = fmtBytes(o.potential_savings_bytes);

  $("#entities").innerHTML = Object.entries(o.entities)
    .map(([k, v]) => `<div class="entity"><b>${v.toLocaleString()}</b><span>${k}</span></div>`).join("");
}

// ------------------------------------------------------------------ charts
function baseOptions(yFormat) {
  const grid = css("--grid"), muted = css("--muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "nearest", axis: "x", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${yFormat(c.parsed.y)}` } },
    },
    scales: {
      x: { type: "linear", grid: { color: grid }, border: { color: css("--axis") },
           ticks: { color: muted, maxTicksLimit: 8, callback: function (v) {
             // clock times when the axis spans less than two days, dates otherwise
             const d = new Date(v);
             return this.max - this.min < 2 * 86400000
               ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
               : d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
           } } },
      y: { beginAtZero: true, grid: { color: grid }, border: { display: false },
           ticks: { color: muted, callback: (v) => yFormat(v) } },
    },
  };
}

// byte axes tick on round binary steps (256 MB, 1 GB, 2 GB, ...)
function byteStep(max, ticks = 6) {
  const MB = 1024 ** 2;
  const steps = [1, 2, 5, 10, 25, 50, 100, 128, 256, 512, 1024, 2048, 5120, 10240, 20480, 51200, 102400].map((m) => m * MB);
  return steps.find((s) => max / s <= ticks) || steps[steps.length - 1];
}

function upsertChart(key, canvas, config) {
  const existing = state.charts[key];
  if (existing && existing.config.type === config.type) {
    existing.data = config.data;
    existing.options = config.options;
    existing.update("none");
  } else {
    existing?.destroy();
    state.charts[key] = new Chart(canvas, config);
  }
}

function renderUsageChart(h) {
  const s1 = css("--series-1"), s2 = css("--series-2"), crit = css("--critical");
  const pts = h.points.map((p) => ({ x: Date.parse(p.t), y: p.used }));
  const fc = h.forecast.map((p) => ({ x: Date.parse(p.t), y: p.used }));
  const xs = pts.concat(fc).map((p) => p.x);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const datasets = [
    { label: "Used", data: pts, borderColor: s1, backgroundColor: withAlpha(s1, 0.12), fill: "origin",
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, tension: 0.15 },
    { label: "Trend & forecast", data: fc, borderColor: s2, borderDash: [6, 4], borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 5, fill: false },
  ];
  const limitLabel = h.scope === "system" ? "Capacity" : "Quota";
  if (h.capacity_bytes && pts.length) {
    datasets.push({ label: limitLabel, data: [{ x: xmin, y: h.capacity_bytes }, { x: xmax, y: h.capacity_bytes }],
      borderColor: crit, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0, fill: false });
  }
  const opts = baseOptions(fmtBytes);
  opts.scales.x.min = xmin; opts.scales.x.max = xmax;
  const ymax = Math.max(h.capacity_bytes || 0, ...pts.concat(fc).map((p) => p.y), 1);
  opts.scales.y.ticks.stepSize = byteStep(ymax);
  opts.scales.y.max = Math.ceil(ymax * 1.04 / opts.scales.y.ticks.stepSize) * opts.scales.y.ticks.stepSize;
  upsertChart("usage", $("#usage-chart"), { type: "line", data: { datasets }, options: opts });

  $("#usage-legend").innerHTML =
    `<span><i style="border-color:${s1}"></i>Used storage</span>` +
    `<span><i class="dash" style="border-color:${s2}"></i>Regression trend &amp; forecast</span>` +
    (h.capacity_bytes ? `<span><i style="border-color:${crit}"></i>${limitLabel} (${fmtBytes(h.capacity_bytes)})</span>` : "");
  $("#chart-note").textContent = pts.length ? "" : "No usage samples yet: the first collection runs shortly after start-up.";
}

function renderTypeChart(byType) {
  const s1 = css("--series-1");
  const opts = baseOptions(fmtBytes);
  opts.indexAxis = "y";
  opts.interaction = { mode: "nearest", axis: "y", intersect: false };
  opts.scales = {
    x: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => fmtBytes(v), stepSize: byteStep(Math.max(...byType.map((d) => d.bytes), 1), 5) } },
    y: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2") } },
  };
  opts.plugins.tooltip.callbacks.label = (c) => fmtBytes(c.parsed.x);
  upsertChart("type", $("#type-chart"), {
    type: "bar",
    data: { labels: byType.map((d) => d.type), datasets: [{ label: "Bytes", data: byType.map((d) => d.bytes), backgroundColor: s1, borderRadius: 4, barThickness: 16 }] },
    options: opts,
  });
}

function renderPredChart(preds) {
  const s1 = css("--series-1");
  const data = preds.map((p) => ({ x: Date.parse(p.created_at), y: p.days_remaining }));
  const opts = baseOptions((v) => (v === null ? "—" : `${Number(v).toFixed(v < 10 ? 1 : 0)} d`));
  opts.plugins.tooltip.callbacks.label = (c) => `Predicted time to full: ${fmtDays(c.parsed.y)}`;
  // cap the axis so "practically never" forecasts do not flatten the interesting part
  const finite = data.map((d) => d.y).filter((y) => y !== null);
  if (finite.length && Math.max(...finite) > 120) opts.scales.y.max = 120;
  upsertChart("pred", $("#pred-chart"), {
    type: "line",
    data: { datasets: [{ label: "Days remaining", data, borderColor: s1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, spanGaps: false }] },
    options: opts,
  });
}

// ------------------------------------------------------------------ lists & tables
function renderAlerts(list) {
  const ul = $("#alerts");
  if (!list.length) { ul.innerHTML = `<li class="empty" style="border:none">${ICON.good} No active alerts. Storage is within thresholds.</li>`; return; }
  ul.innerHTML = list.map((a) => {
    const cls = a.resolved_at ? "resolved" : a.severity;
    const lbl = a.resolved_at ? `<span class="status good">${ICON.good} Resolved</span>` : `<span class="status ${a.severity}">${ICON[a.severity]} ${LABEL[a.severity]}</span>`;
    return `<li class="${cls}">${lbl} <span class="tag">${esc(a.type.replace("_", " "))}</span>
      <div>${esc(a.message)}</div>
      <div class="meta">raised ${fmtDate(a.created_at)}${a.resolved_at ? ` · resolved ${fmtDate(a.resolved_at)}` : ` · updated ${fmtDate(a.updated_at)}`}${a.notified ? " · admin notified" : ""}</div></li>`;
  }).join("");
}

function renderUsers(users) {
  const t = state.overview?.thresholds || { util_warn: 80, util_crit: 90, days_warn: 14, days_crit: 3 };
  $("#users-table tbody").innerHTML = users.map((u) => {
    const p = u.prediction;
    let quota = `<span class="muted">Unlimited</span>`;
    if (u.quota_bytes) {
      const pct = u.used_bytes / u.quota_bytes * 100;
      const cls = pct >= t.util_crit ? "critical" : pct >= t.util_warn ? "warning" : "";
      quota = `<div class="qbar"><div class="meter ${cls}"><div style="width:${Math.min(pct, 100)}%"></div></div><span>${pct.toFixed(0)}% of ${fmtBytes(u.quota_bytes)}</span></div>`;
    }
    let ttf = `<span class="muted">—</span>`;
    if (u.quota_bytes && p) {
      const s = statusFor(p.days_remaining, t);
      ttf = p.days_remaining === null ? `<span class="status good">${ICON.good} stable</span>` : `<span class="status ${s}">${ICON[s]} ${fmtDays(p.days_remaining)}</span>`;
    }
    return `<tr><td><b>${esc(u.display_name)}</b> <span class="muted small">${esc(u.uid)}</span></td><td>${esc(u.role)}</td>
      <td class="num">${fmtBytes(u.used_bytes)}</td><td>${quota}</td><td class="num">${fmtBytes(u.trash_bytes)}</td>
      <td class="num">${fmtBytes(u.versions_bytes)}</td><td class="num">${p ? fmtBytes(p.growth_bytes_per_day) : "—"}</td><td class="num">${ttf}</td>
      <td class="num">${esc(state.overview?.billing?.currency || "")} ${u.cost_month.toFixed(2)}</td>
      <td><button class="btn ghost small" data-quota="${esc(u.uid)}" data-current="${u.quota_bytes || ""}">Set quota</button></td></tr>`;
  }).join("");

  const scope = $("#scope");
  const want = ["system", ...users.map((u) => u.uid)];
  if (scope.options.length !== want.length) {
    scope.innerHTML = `<option value="system">Whole storage pool</option>` + users.map((u) => `<option value="${esc(u.uid)}">User: ${esc(u.display_name)}</option>`).join("");
    scope.value = state.scope;
  }
}

function renderRecs(recs) {
  const ul = $("#recs");
  if (!recs.length) { ul.innerHTML = `<li class="empty" style="border:none">No recommendations right now.</li>`; return; }
  const names = { compress: "compress", large_file: "large file", duplicate: "duplicate", trash: "trash bin", versions: "versions", old_files: "cold data", quota: "quota", capacity: "capacity planning" };
  ul.innerHTML = recs.map((r) => `<li class="p${r.priority}"><div><span class="tag">${names[r.category] || r.category}</span>${r.priority === 1 ? '<span class="tag">high priority</span>' : ""}
      <div>${esc(r.message)}</div>${r.savings_bytes ? `<div class="meta">potential saving ${fmtBytes(r.savings_bytes)}</div>` : ""}</div>
      <button class="btn ghost small" data-dismiss="${r.id}" title="Dismiss">Dismiss</button></li>`).join("");
}

function renderFiles(c) {
  $("#files-table tbody").innerHTML = c.files.map((f) => `<tr><td class="path" title="${esc(f.path)}">${esc(f.name)}</td>
    <td>${esc(f.owner)}</td><td class="num">${fmtBytes(f.size)}</td><td class="num">${fmtDate(f.mtime, false)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">No files yet.</td></tr>`;
  renderTypeChart(c.by_type);
}

function renderShares(list) {
  $("#shares-table tbody").innerHTML = list.map((s) => `<tr><td>${esc(s.owner)}</td><td>${esc(s.path)} <span class="muted small">${esc(s.type)}</span></td>
    <td>${s.share_type === "link" ? "Public link" : `${esc(s.share_type)}: ${esc(s.share_with)}`}</td>
    <td class="small">${(s.permissions || []).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">No shares yet.</td></tr>`;
}

function renderScaling(sc) {
  const p = sc.pool;
  const pct = p.capacity_bytes ? p.used_bytes / p.capacity_bytes * 100 : 0;
  const t = state.overview?.thresholds || { util_warn: 80, util_crit: 90 };
  $("#pool-meter").className = `meter ${pct >= t.util_crit ? "critical" : pct >= t.util_warn ? "warning" : ""}`;
  $("#pool-meter > div").style.width = `${Math.min(pct, 100)}%`;
  $("#pool-used").textContent = `${fmtBytes(p.used_bytes)} of ${(p.capacity_bytes / 1024 ** 3).toFixed(2)} GB (${pct.toFixed(1)}%)`;
  $("#pool-note").textContent = `volume limit ${fmtBytes(p.max_capacity_bytes)}`;
  const over = p.allocated_quota_bytes / p.capacity_bytes * 100;
  $("#pool-alloc").textContent = `Quotas allocated to users: ${fmtBytes(p.allocated_quota_bytes)} (${over.toFixed(0)}% of the pool)` +
    (over > 100 ? " · over-committed (thin provisioning): not every user can fill their quota at once." : "");
  if (!state.poolDirty) {             // keep the admin's unsaved edits across auto-refreshes
    $("#pool-gb").value = (p.capacity_bytes / 1024 ** 3).toFixed(2);
    $("#as-step").value = p.autoscale_step_pct;
    $("#as-at").value = p.autoscale_at_pct;
    $("#as-on").checked = p.autoscale;
  }
  $("#events").innerHTML = sc.events.map((e) => `<li><div><span class="tag">${esc(e.kind)}</span><span class="tag">${esc(e.actor)}</span>
      <div>${esc(e.note)}</div><div class="meta">${fmtDate(e.at)}</div></div></li>`).join("")
    || `<li class="empty" style="border:none">No scaling activity yet.</li>`;
  $("#requests").innerHTML = sc.requests.map((r) => {
    const cur = r.current_quota_bytes ? fmtBytes(r.current_quota_bytes) : "unlimited";
    const head = `<b>${esc(r.user)}</b> asks for ${fmtBytes(r.requested_quota_bytes)} <span class="muted small">(now ${cur}, using ${fmtBytes(r.used_bytes)})</span>`;
    const status = r.status === "pending" ? `<span class="status warning">${ICON.warning} Pending</span>`
      : r.status === "approved" ? `<span class="status good">${ICON.good} Approved ${fmtBytes(r.granted_quota_bytes)}</span>`
      : `<span class="status critical">${ICON.critical} Denied</span>`;
    const actions = r.status === "pending" ? `<div class="actions"><button class="btn small" data-approve="${r.id}" data-req="${r.requested_quota_bytes}">Approve</button>
      <button class="btn ghost small" data-deny="${r.id}">Deny</button></div>` : "<span></span>";
    return `<li class="${r.status === "pending" ? "p1" : ""}"><div>${status} ${head}<div>${esc(r.reason || "No reason given")}</div>
      <div class="meta">requested ${fmtDate(r.created_at)}${r.decided_at ? ` · decided ${fmtDate(r.decided_at)}` : ""}</div></div>${actions}</li>`;
  }).join("") || `<li class="empty" style="border:none">No quota requests yet. Users raise them from the portal at /me.</li>`;
}

// ------------------------------------------------------------------ data loading
async function loadChart() {
  const q = state.scope === "system" ? "scope=system" : `scope=user&user=${encodeURIComponent(state.scope)}`;
  const h = await api(`/api/history?${q}&days=${state.range}`);
  $("#chart-title").textContent = state.scope === "system" ? "Storage usage & forecast" : `Usage & forecast: ${state.scope}`;
  renderUsageChart(h);
}

async function load() {
  try {
    const [o, users, alerts, recs, cons, shares, preds, sc] = await Promise.all([
      api("/api/overview"), api("/api/users"), api(`/api/alerts?include_resolved=${$("#show-resolved").checked}`),
      api("/api/recommendations"), api("/api/consumers"), api("/api/shares"), api("/api/predictions"),
      api("/api/admin/scaling"),
    ]);
    renderOverview(o);
    renderScaling(sc);
    $("#billing-note").textContent = `chargeback at ${o.billing.currency} ${o.billing.cost_per_gb_month}/GB-month · pool total ${o.billing.currency} ${o.billing.total_month.toFixed(2)}/month`;
    renderUsers(users);
    renderAlerts(alerts);
    renderRecs(recs);
    renderFiles(cons);
    renderShares(shares);
    renderPredChart(preds);
    await loadChart();
  } catch (e) {
    const err = $("#error-bar");
    err.hidden = false;
    err.textContent = `Dashboard could not load data: ${e.message}`;
  }
}

async function busy(btn, fn) {
  const buttons = document.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try { await fn(); } catch (e) { alert(e.message); }
  buttons.forEach((b) => (b.disabled = false));
  await load();
}

// ------------------------------------------------------------------ events
$("#btn-collect").addEventListener("click", (e) => busy(e.target, () => post("/api/collect")));
$("#btn-reset").addEventListener("click", (e) => busy(e.target, () => post("/api/demo/reset", { scenario: $("#scenario").value })));
$("#scenario").addEventListener("change", () => {
  $("#scenario-desc").textContent = state.overview?.scenarios[$("#scenario").value]?.description || "";
});
document.querySelectorAll("[data-advance]").forEach((b) =>
  b.addEventListener("click", () => busy(b, () => post("/api/demo/advance", { days: Number(b.dataset.advance) }))));
$("#scope").addEventListener("change", (e) => { state.scope = e.target.value; loadChart(); });
$("#range").addEventListener("change", (e) => { state.range = Number(e.target.value); state.rangeChosen = true; loadChart(); });
$("#show-resolved").addEventListener("change", load);
$("#recs").addEventListener("click", (e) => {
  const id = e.target.dataset?.dismiss;
  if (id) busy(e.target, () => post(`/api/recommendations/${id}/dismiss`));
});
["#pool-gb", "#as-on", "#as-step", "#as-at"].forEach((id) =>
  ["input", "change"].forEach((ev) => $(id).addEventListener(ev, () => {
    state.poolDirty = true;
    $("#btn-as").classList.toggle("ghost", id === "#pool-gb");
    if (id !== "#pool-gb") $("#btn-as").textContent = "Save policy *";
  })));
$("#btn-pool").addEventListener("click", (e) => busy(e.target, async () => {
  await post("/api/admin/pool", { capacity_gb: Number($("#pool-gb").value) });
  state.poolDirty = false;
}));
$("#btn-as").addEventListener("click", (e) => busy(e.target, async () => {
  await post("/api/admin/pool", {
    autoscale: $("#as-on").checked, autoscale_step_pct: Number($("#as-step").value), autoscale_at_pct: Number($("#as-at").value) });
  state.poolDirty = false;
  e.target.textContent = "Save policy";
  e.target.classList.add("ghost");
}));
$("#requests").addEventListener("click", (e) => {
  const d = e.target.dataset || {};
  if (d.approve) {
    const mb = prompt("Grant quota (MB):", Math.round(Number(d.req) / 1024 ** 2));
    if (mb) busy(e.target, () => post(`/api/admin/quota-requests/${d.approve}/approve`, { quota_mb: Number(mb) }));
  }
  if (d.deny) busy(e.target, () => post(`/api/admin/quota-requests/${d.deny}/deny`));
});
$("#users-table").addEventListener("click", (e) => {
  const d = e.target.dataset || {};
  if (!d.quota) return;
  const mb = prompt(`New quota for ${d.quota} (MB):`, d.current ? Math.round(Number(d.current) / 1024 ** 2) : 1024);
  if (mb) busy(e.target, () => post(`/api/admin/users/${encodeURIComponent(d.quota)}/quota`, { quota_mb: Number(mb) }));
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  Object.values(state.charts).forEach((c) => c.destroy());
  state.charts = {};
  load();
});

load();
setInterval(load, REFRESH_MS);
