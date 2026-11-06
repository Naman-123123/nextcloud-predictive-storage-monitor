"use strict";

const $ = (sel) => document.querySelector(sel);
const REFRESH_MS = 15000;
let token = sessionStorage.getItem("me-token");

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
function fmtDate(iso) {
  return iso ? new Date(iso).toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers, token ? { "X-Session": token } : {});
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/me/login") { logout(); throw new Error("Session expired, please log in again"); }
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (_) { /* plain text */ }
    throw new Error(msg);
  }
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

function say(text) { const m = $("#msg"); m.hidden = !text; m.textContent = text || ""; }

function render(me) {
  $("#who").textContent = `${me.display_name} (${me.uid})`;
  const pct = me.quota_bytes ? me.used_bytes / me.quota_bytes * 100 : null;
  $("#k-used").textContent = fmtBytes(me.used_bytes);
  $("#k-used-foot").textContent = me.quota_bytes ? `${pct.toFixed(1)}% of ${fmtBytes(me.quota_bytes)} quota` : "no quota (unlimited)";
  $("#k-meter-wrap").className = `meter ${pct >= 90 ? "critical" : pct >= 80 ? "warning" : ""}`;
  $("#k-meter").style.width = `${Math.min(pct || 0, 100)}%`;
  $("#k-trash").textContent = fmtBytes(me.trash_bytes);
  $("#k-versions").textContent = fmtBytes(me.versions_bytes);
  const p = me.prediction;
  $("#k-growth").textContent = p ? `${fmtBytes(p.growth_bytes_per_day)}/d` : "—";
  $("#k-growth-foot").textContent = p ? `linear trend, R² ${p.r_squared?.toFixed(2) ?? "—"}` : "";
  $("#k-days").textContent = me.quota_bytes && p ? fmtDays(p.days_remaining) : "—";
  $("#k-cost").textContent = `${me.currency} ${me.cost_month.toFixed(2)}`;
  $("#k-cost-foot").textContent = `at ${me.currency} ${me.cost_per_gb_month}/GB-month`;

  const alerts = [...me.alerts];
  if (pct !== null && pct >= 80 && !alerts.some((a) => a.type === "quota_utilization"))
    alerts.unshift({ severity: pct >= 90 ? "critical" : "warning", message: `Your storage is ${pct.toFixed(0)}% full.` });
  $("#alerts").innerHTML = alerts.map((a) => `<li class="${a.severity}"><span class="status ${a.severity}">${a.severity === "critical" ? "⛔" : "⚠"} ${a.severity}</span>
      <div>${esc(a.message)}</div><div class="meta">Free space with the recommendations, or ask the administrator for more.</div></li>`).join("")
    || `<li class="empty" style="border:none">✓ Your storage is fine.</li>`;

  const pending = me.requests.some((r) => r.status === "pending");
  $("#btn-request").disabled = pending;
  $("#btn-request").textContent = pending ? "Request pending with the administrator" : "Ask the administrator";
  $("#requests").innerHTML = me.requests.map((r) => `<li><div><span class="tag">${esc(r.status)}</span>
      ${fmtBytes(r.current_quota_bytes || r.used_bytes)} → ${fmtBytes(r.granted_quota_bytes || r.requested_quota_bytes)}
      <div class="meta">${fmtDate(r.created_at)}${r.reason ? ` · ${esc(r.reason)}` : ""}</div></div></li>`).join("");

  const names = { compress: "compress", large_file: "large file", trash: "trash bin", versions: "old versions", old_files: "cold data", quota: "quota" };
  $("#recs").innerHTML = me.recommendations.map((r) => {
    let btn = "<span></span>";
    if (r.category === "compress") btn = `<button class="btn small" data-compress="${r.file_id}">Compress</button>`;
    else if (r.file_id) btn = `<div class="actions"><button class="btn ghost small" data-compress="${r.file_id}">Compress</button>
      <button class="btn ghost small" data-delete="${r.file_id}" data-name="${esc(r.path.split("/").pop())}">Delete</button></div>`;
    else if (r.category === "old_files") btn = `<a class="btn ghost small" href="#cold-title">Review</a>`;
    else if (r.category === "trash") btn = `<button class="btn small" data-trash="1">Empty trash</button>`;
    return `<li class="p${r.priority}"><div><span class="tag">${names[r.category] || r.category}</span>
      <div>${esc(r.message)}</div>${r.savings_bytes ? `<div class="meta">potential saving ${fmtBytes(r.savings_bytes)}</div>` : ""}</div>${btn}</li>`;
  }).join("") || `<li class="empty" style="border:none">No recommendations right now.</li>`;

  const coldTotal = me.cold_files.reduce((n, f) => n + f.size, 0);
  $("#cold-title").textContent = `Files you have not touched in ${me.old_file_days}+ days`;
  $("#cold-note").textContent = me.cold_files.length ? `${fmtBytes(coldTotal)} you could delete or archive` : "";
  $("#cold-table tbody").innerHTML = me.cold_files.map((f) => `<tr><td class="path" title="${esc(f.path)}">${esc(f.name)}</td>
      <td class="small">${esc(f.mimetype)}</td><td class="num">${fmtBytes(f.size)}</td><td class="num">${fmtDate(f.mtime)}</td>
      <td><div class="actions">${f.compressible ? `<button class="btn ghost small" data-compress="${f.id}">Compress</button>` : ""}
      <button class="btn ghost small" data-delete="${f.id}" data-name="${esc(f.name)}">Delete</button></div></td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">✓ No cold files.</td></tr>`;

  $("#files-table tbody").innerHTML = me.files.map((f) => `<tr><td class="path" title="${esc(f.path)}">${esc(f.name)}</td>
      <td class="small">${esc(f.mimetype)}${f.compressible ? ' <span class="tag">compressible</span>' : ""}</td>
      <td class="num">${fmtBytes(f.size)}</td><td class="num">${fmtDate(f.mtime)}</td>
      <td><button class="btn ${f.compressible ? "" : "ghost"} small" data-compress="${f.id}">Compress</button></td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">No files yet.</td></tr>`;
}

async function load() {
  if (!token) return;
  try { render(await api("/api/me")); } catch (e) { say(e.message); }
}

function showApp(on) {
  $("#login").hidden = on;
  $("#app").hidden = !on;
  $("#btn-logout").hidden = !on;
  if (!on) $("#who").textContent = "";
}
function logout() { token = null; sessionStorage.removeItem("me-token"); showApp(false); }

async function busy(btn, fn) {
  const buttons = document.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  const label = btn.textContent;
  btn.textContent = "Working…";
  try { await fn(); } catch (e) { say(e.message); }
  btn.textContent = label;
  buttons.forEach((b) => (b.disabled = false));
  await load();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#l-err").textContent = "";
  try {
    const r = await post("/api/me/login", { user: $("#l-user").value.trim(), password: $("#l-pass").value });
    token = r.token;
    sessionStorage.setItem("me-token", token);
    showApp(true);
    say("");
    load();
  } catch (err) { $("#l-err").textContent = err.message; }
});
$("#btn-logout").addEventListener("click", logout);
$("#btn-request").addEventListener("click", (e) => busy(e.target, async () => {
  await post("/api/me/quota-request", { extra_mb: Number($("#r-extra").value), reason: $("#r-reason").value });
  $("#r-reason").value = "";
  say("Request sent. The administrator has been notified in Nextcloud.");
}));
$("#btn-trash").addEventListener("click", (e) => {
  if (confirm("Permanently delete everything in your trash bin?"))
    busy(e.target, async () => { const r = await post("/api/me/empty-trash"); say(`Trash bin emptied: ${fmtBytes(r.freed)} freed.`); });
});
document.addEventListener("click", (e) => {
  const d = e.target.dataset || {};
  if (d.compress) busy(e.target, async () => {
    const r = await post("/api/me/compress", { file_id: Number(d.compress) });
    say(r.compressed ? `${r.message} Freed ${fmtBytes(r.before - r.after)}.` : r.message);
  });
  if (d.delete && confirm(`Permanently delete '${d.name}'? It will not stay in the trash bin.`))
    busy(e.target, async () => { const r = await post("/api/me/delete", { file_id: Number(d.delete) }); say(`Deleted '${r.name}': ${fmtBytes(r.freed)} freed.`); });
  if (d.trash && confirm("Permanently delete everything in your trash bin?"))
    busy(e.target, async () => { const r = await post("/api/me/empty-trash"); say(`Trash bin emptied: ${fmtBytes(r.freed)} freed.`); });
});

showApp(!!token);
load();
setInterval(load, REFRESH_MS);
