"use strict";

// ---------------------------------------------------------------- state & helpers
const S = {
  session: sessionStorage.getItem("nftops.session"),
  page: (location.hash || "#overview").slice(1),
  chainId: Number(localStorageGet("nftops.chain") || 4663),
  networks: [], selected: new Set(), walletFilter: { group: "", q: "" },
  elig: { slug: "", filter: "ALL", stage: "ALL" },
  mint: { slug: "", stage: "", source: "eligible", group: "", preview: null },
  stopped: false,
};
const PAGES = ["overview", "wallets", "collections", "eligibility", "mint", "history", "networks", "settings"];
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "");
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

class ApiError extends Error {}
async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (S.session) opts.headers["X-Session"] = S.session;
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 401) { showLock(); throw new ApiError("locked"); }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new ApiError((data && data.error) || `HTTP ${res.status}`);
  return data;
}
async function download(path, name) {
  const res = await fetch(path, { headers: { "X-Session": S.session } });
  if (!res.ok) return toast("Export failed", true);
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg; t.className = `toast${bad ? " bad" : ""}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), bad ? 6000 : 3000);
}
async function guard(fn) {
  try { return await fn(); } catch (e) { if (!(e instanceof ApiError && e.message === "locked")) toast(e.message, true); }
}
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
const countdown = (ts) => `<span class="countdown" data-until="${Number(ts)}">${esc(fmtDur(ts - Date.now() / 1000))}</span>`;
setInterval(() => {
  const now = Date.now() / 1000;
  for (const el of $$("[data-until]")) {
    const left = Number(el.dataset.until) - now;
    el.textContent = left > 0 ? fmtDur(left) : "now";
  }
}, 1000);

const BADGE = {
  ELIGIBLE: "good", NOT_ELIGIBLE: "bad", UNKNOWN: "warn", UNVERIFIED: "warn", "NOT SCANNED": "warn",
  READY: "good", confirmed: "good", active: "good", healthy: "good",
  pending: "info", submitted: "info", broadcasting: "info", waiting: "info", preparing: "info", queued: "info", running: "info", upcoming: "info",
  failed: "bad", blocked: "bad", not_submitted: "bad", ended: "", cancelled: "warn", stopped: "warn", finished: "",
};
function badge(status) {
  const cls = BADGE[status] ?? (/^(INSUFFICIENT|NOT_|SOLD|PHASE|SIMULATION|LIMIT|ALLOCATION|NO_KEY|GAS|RPC)/.test(status) ? "bad"
    : /UNKNOWN|ALREADY/.test(status) ? "warn" : "");
  return `<span class="badge ${cls}">${esc(String(status).replaceAll("_", " "))}</span>`;
}
function liveState(s) {
  const now = Date.now() / 1000;
  if (s.ends_at && now >= s.ends_at) return "ended";
  return now >= s.starts_at ? "active" : "upcoming";
}
function liveCollections(cols) { for (const c of cols) for (const s of c.stages) s.state = liveState(s); return cols; }
function network(chainId) { return S.networks.find((n) => n.chain_id === Number(chainId)); }
function txLink(chainId, hash) {
  if (!hash) return "—";
  const n = network(chainId);
  const base = n && n.explorer_url ? n.explorer_url.replace(/\/$/, "") : "";
  return base ? `<a class="mono" href="${esc(base)}/tx/${esc(hash)}" target="_blank" rel="noopener">${esc(short(hash))}</a>`
    : `<span class="mono" title="${esc(hash)}">${esc(short(hash))}</span>`;
}
function addr(a, label) {
  return `<span class="mono" title="${esc(a)}">${esc(short(a))}</span> <button class="small ghost" data-action="copy" data-text="${esc(a)}" title="Copy">⧉</button>${label ? ` <span>${esc(label)}</span>` : ""}`;
}
function options(list, value, label = (x) => x, val = (x) => x) {
  return list.map((x) => `<option value="${esc(val(x))}"${String(val(x)) === String(value) ? " selected" : ""}>${esc(label(x))}</option>`).join("");
}
function chainSelect(id, value) {
  return `<select id="${id}">${options(S.networks.filter((n) => n.enabled), value, (n) => n.name, (n) => n.chain_id)}</select>`;
}
const symbol = (chainId) => network(chainId)?.native_symbol || "ETH";

// ---------------------------------------------------------------- modal
function modal(html) { $("#modal-card").innerHTML = html; $("#modal").classList.remove("hidden"); const f = $("#modal-card input, #modal-card textarea"); if (f) f.focus(); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-card").innerHTML = ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

// ---------------------------------------------------------------- lock screen
async function boot() {
  const st = await fetch("/api/state").then((r) => r.json());
  S.stopped = st.stopped;
  if (!S.session || !st.unlocked) return showLock(st);
  try { await api("/api/networks").then((d) => { S.networks = d.networks; }); } catch { return; }
  showApp();
}
function showLock(st) {
  $("#app").classList.add("hidden"); $("#lock").classList.remove("hidden");
  S.session = null; sessionStorage.removeItem("nftops.session");
  const create = st ? !st.vault_exists : false;
  $("#lock-form").dataset.mode = create ? "create" : "unlock";
  $("#lock-text").textContent = create
    ? "Create a vault password (10+ characters). It encrypts your wallet keys. If you forget it, the keys cannot be recovered."
    : "Enter your vault password.";
  $("#lock-password2").classList.toggle("hidden", !create);
  $("#lock-button").textContent = create ? "Create vault" : "Unlock";
  if (!st) fetch("/api/state").then((r) => r.json()).then(showLock);
  $("#lock-password").focus();
}
$("#lock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.target.dataset.mode, pw = $("#lock-password").value;
  $("#lock-error").textContent = "";
  if (mode === "create" && pw !== $("#lock-password2").value) { $("#lock-error").textContent = "Passwords do not match."; return; }
  $("#lock-button").disabled = true;
  try {
    const res = await api(mode === "create" ? "/api/vault/create" : "/api/vault/unlock", { password: pw });
    S.session = res.session; sessionStorage.setItem("nftops.session", res.session);
    $("#lock-password").value = ""; $("#lock-password2").value = "";
    S.networks = (await api("/api/networks")).networks;
    showApp();
  } catch (err) { $("#lock-error").textContent = err.message; } finally { $("#lock-button").disabled = false; }
});
function showApp() {
  $("#lock").classList.add("hidden"); $("#app").classList.remove("hidden");
  setStopped(S.stopped); startEvents(); go(PAGES.includes(S.page) ? S.page : "overview");
}

// ---------------------------------------------------------------- STOP
function setStopped(on) {
  S.stopped = on;
  $("#stopped-banner").classList.toggle("hidden", !on);
  $("#stop-button").textContent = on ? "STOPPED" : "STOP";
}
$("#stop-button").addEventListener("click", () => guard(async () => {
  await api("/api/stop", {}); setStopped(true); toast("Emergency STOP active. Nothing new will be sent.");
}));
$("#resume-button").addEventListener("click", () => modal(`
  <h2>Resume operations</h2>
  <p>Transactions already sent cannot be recalled; they keep being tracked. Resuming allows new transactions again.</p>
  <label>Type RESUME to confirm<input id="resume-text" autocomplete="off"></label>
  <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="resume">Resume</button></div>`));

// ---------------------------------------------------------------- live events
let eventsOn = false, refreshTimer = null;
async function startEvents() {
  if (eventsOn) return; eventsOn = true;
  while (S.session) {
    try {
      const res = await fetch("/api/events", { headers: { "X-Session": S.session } });
      if (!res.ok) break;
      $("#live").classList.add("on");
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i; while ((i = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (line) onEvent(JSON.parse(line.slice(6)));
        }
      }
    } catch { /* reconnect */ }
    $("#live").classList.remove("on");
    await new Promise((r) => setTimeout(r, 2000));
  }
  eventsOn = false;
}
function onEvent(ev) {
  if (ev.kind === "safety") setStopped(ev.stopped);
  if (["run", "job", "mint_log"].includes(ev.kind) && ["mint", "overview"].includes(S.page)) scheduleRefresh();
  if (ev.kind === "eligibility" && S.page === "eligibility") {
    const bar = $("#scan-bar"); if (bar) { bar.style.width = `${(100 * ev.done) / ev.total}%`; $("#scan-text").textContent = `${ev.done} / ${ev.total} wallets checked`; }
  }
  if (ev.kind === "eligibility_done") { toast("Eligibility scan finished"); if (S.page === "eligibility") render(); }
}
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    if (S.page === "mint") await renderJobs(); else if (S.page === "overview") await render();
  }, 600);
}

// ---------------------------------------------------------------- navigation
function go(page) { S.page = page; history.replaceState(null, "", `#${page}`); render(); }
$("#nav").addEventListener("click", (e) => { const a = e.target.closest("a[data-page]"); if (a) go(a.dataset.page); });
async function render() {
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === S.page));
  await guard(() => PAGE_RENDER[S.page]());
}

// ---------------------------------------------------------------- overview
async function renderOverview() {
  const d = await api("/api/overview");
  setStopped(d.stopped);
  d.upcoming.forEach((s) => { s.state = liveState(s); });
  const c = d.last24h;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Overview</h1></div>
    <div class="grid cards">
      <div class="card"><div class="muted">Wallets</div><div class="big">${d.wallets}</div><div class="tiny muted">${d.with_keys} with keys</div></div>
      <div class="card"><div class="muted">Eligible wallets</div><div class="big">${d.eligible_wallets}</div><div class="tiny muted">any loaded collection</div></div>
      ${d.totals.map((t) => `<div class="card"><div class="muted">${esc(t.name)}</div><div class="big">${esc(t.native)} ${esc(t.native_symbol)}</div><div class="tiny muted">${t.nfts} NFTs in tracked collections</div></div>`).join("")}
      <div class="card"><div class="muted">Last 24 hours</div><div class="big">${c.confirmed || 0} minted</div>
        <div class="tiny muted">${c.failed || 0} failed · ${(c.pending || 0) + (c.submitted || 0)} pending · ${c.blocked || 0} blocked</div></div>
    </div>
    <div class="two" style="margin-top:14px">
      <div class="panel"><h2>Upcoming & active phases</h2>${d.upcoming.length ? `<div class="table-wrap"><table>
        <tr><th>Collection</th><th>Phase</th><th>State</th><th>Starts</th><th class="num">Eligible</th></tr>
        ${d.upcoming.map((s) => `<tr><td>${esc(s.slug)}<div class="tiny muted">${esc(s.chain_name)}</div></td><td>${esc(s.name)}</td><td>${badge(s.state)}</td>
          <td>${s.state === "upcoming" ? countdown(s.starts_at) : "open"}</td><td class="num">${s.eligible_wallets}</td></tr>`).join("")}</table></div>`
        : `<div class="empty">No upcoming phases. Add a collection.</div>`}</div>
      <div class="panel"><h2>Active operations</h2>${d.active_jobs.length ? d.active_jobs.map(jobCard).join("") : `<div class="empty">Nothing running.</div>`}</div>
    </div>
    <div class="panel"><h2>Recent transactions</h2>${historyTable(d.recent)}</div>`;
}

// ---------------------------------------------------------------- wallets
async function renderWallets() {
  const d = await api(`/api/wallets?chain_id=${S.chainId}`);
  S.walletData = d;
  const f = S.walletFilter, q = f.q.toLowerCase();
  const rows = d.wallets.filter((w) => (!f.group || w.groups.includes(f.group)) && (!q || w.address.toLowerCase().includes(q) || w.label.toLowerCase().includes(q)));
  for (const a of [...S.selected]) if (!d.wallets.some((w) => w.address === a)) S.selected.delete(a);
  const sym = symbol(S.chainId);
  $("#main").innerHTML = `
    <div class="page-head"><h1>Wallets</h1>
      <button class="primary" data-action="import-keys">Import keys</button>
      <button data-action="generate">Generate</button>
      <button data-action="watch">Add watch-only</button>
      <button data-action="csv-import">Import CSV</button>
      <button data-action="export-wallets">Export CSV</button>
    </div>
    <div class="panel">
      <div class="row">
        <label>Network ${chainSelect("w-chain", S.chainId)}</label>
        <label>Group <select id="w-group"><option value="">All wallets</option>${options(d.groups, f.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>
        <label>Search <input id="w-search" value="${esc(f.q)}" placeholder="address or label"></label>
        <span class="spacer"></span>
        <button data-action="refresh-balances">Refresh balances</button>
        <button data-action="add-token">Track token</button>
      </div>
      <div class="row" style="margin-top:10px">
        <span class="muted">${S.selected.size} selected</span>
        <button class="small" data-action="group-add" ${S.selected.size ? "" : "disabled"}>Add to group</button>
        <button class="small" data-action="group-remove" ${S.selected.size && f.group ? "" : "disabled"}>Remove from “${esc(f.group || "group")}”</button>
        <button class="small" data-action="mint-selected" ${S.selected.size ? "" : "disabled"}>Mint with selected</button>
        <button class="small danger" data-action="remove-wallets" ${S.selected.size ? "" : "disabled"}>Remove</button>
        <span class="spacer"></span>
        <button class="small" data-action="new-group">New group</button>
        ${f.group ? `<button class="small" data-action="rename-group">Rename group</button><button class="small danger" data-action="delete-group">Delete group</button>` : ""}
      </div>
    </div>
    ${d.wallets.length ? `<div class="table-wrap"><table>
      <tr><th><input type="checkbox" id="w-all" ${rows.length && rows.every((w) => S.selected.has(w.address)) ? "checked" : ""}></th>
        <th>Label</th><th>Address</th><th>Groups</th><th>Key</th><th class="num">${esc(sym)}</th>
        ${d.tokens.map((t) => `<th class="num">${esc(t.symbol)}</th>`).join("")}<th class="num">NFTs</th></tr>
      ${rows.map((w) => `<tr>
        <td><input type="checkbox" data-select="${esc(w.address)}" ${S.selected.has(w.address) ? "checked" : ""}></td>
        <td><a data-action="label" data-address="${esc(w.address)}" data-label="${esc(w.label)}">${esc(w.label || "add label")}</a></td>
        <td>${addr(w.address)}</td>
        <td>${w.groups.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</td>
        <td>${w.has_key ? `<span class="badge good">key</span>` : `<span class="badge">watch</span>`}</td>
        <td class="num">${w.native ?? "—"}</td>
        ${d.tokens.map((t) => `<td class="num">${esc(w.tokens[t.symbol] ?? "—")}</td>`).join("")}
        <td class="num">${w.nfts}</td></tr>`).join("")}
    </table></div>` : `<div class="panel empty">No wallets yet. Import your keys or generate new wallets.</div>`}`;
  $("#w-chain").onchange = (e) => { S.chainId = Number(e.target.value); localStorageSet("nftops.chain", S.chainId); render(); };
  $("#w-group").onchange = (e) => { S.walletFilter.group = e.target.value; render(); };
  $("#w-search").oninput = (e) => { S.walletFilter.q = e.target.value; clearTimeout(S.searchT); S.searchT = setTimeout(() => { render().then(() => { const i = $("#w-search"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 300); };
  const all = $("#w-all");
  if (all) all.onchange = (e) => { rows.forEach((w) => (e.target.checked ? S.selected.add(w.address) : S.selected.delete(w.address))); render(); };
  $$("[data-select]").forEach((cb) => (cb.onchange = (e) => { e.target.checked ? S.selected.add(cb.dataset.select) : S.selected.delete(cb.dataset.select); render(); }));
}

// ---------------------------------------------------------------- collections
async function renderCollections() {
  const d = await api("/api/collections");
  S.collections = liveCollections(d.collections);
  $("#main").innerHTML = `
    <div class="page-head"><h1>Collections</h1></div>
    <div class="panel"><form id="add-col" class="row">
      <label style="flex:1 1 320px">OpenSea link, slug, or contract address<input id="col-loc" placeholder="https://opensea.io/collection/…" required></label>
      <label>Network (for a contract address) ${chainSelect("col-chain", S.chainId)}</label>
      <button class="primary" type="submit">Add collection</button></form>
      <p class="tiny muted">Stage 1 supports OpenSea SeaDrop ERC-721 drops (the standard OpenSea drop contract), on any configured network.</p></div>
    ${d.collections.length ? d.collections.map((c) => `<div class="panel">
      <div class="row"><div><h2 style="margin:0">${esc(c.slug)}</h2>
        <div class="tiny muted">${esc(c.chain_name)} · ${addr(c.address)} · <a href="${esc(c.opensea_url)}" target="_blank" rel="noopener">OpenSea</a>
        ${c.minted_out ? ' · <span class="badge bad">minted out</span>' : ""}${c.disabled ? ' · <span class="badge bad">disabled</span>' : ""}
        · updated ${esc(fmtTime(c.updated_at))}</div></div><span class="spacer"></span>
        <button class="small" data-action="col-refresh" data-slug="${esc(c.slug)}">Refresh</button>
        <button class="small" data-action="col-elig" data-slug="${esc(c.slug)}">Eligibility</button>
        <button class="small primary" data-action="col-mint" data-slug="${esc(c.slug)}">Mint</button>
        <button class="small danger" data-action="col-delete" data-slug="${esc(c.slug)}">Remove</button></div>
      <div class="table-wrap" style="margin-top:10px"><table>
        <tr><th>Phase</th><th>Type</th><th>State</th><th>Opens</th><th>Closes</th><th class="num">Price</th><th class="num">Max / wallet</th><th class="num">Eligible wallets</th></tr>
        ${c.stages.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.type)}</td><td>${badge(s.state)}</td>
          <td>${s.state === "upcoming" ? `${countdown(s.starts_at)}<div class="tiny muted">${esc(fmtTime(s.starts_at))}</div>` : esc(fmtTime(s.starts_at))}</td>
          <td>${s.ends_at ? esc(fmtTime(s.ends_at)) : "no end"}</td>
          <td class="num">${s.price_wei === null ? "—" : `${esc(weiToStr(s.price_wei))} ${esc(symbol(c.chain_id))}`}</td>
          <td class="num">${s.max_per_wallet ?? "—"}</td><td class="num">${s.type === "PUBLIC_SALE" ? "all" : s.eligible_wallets}</td></tr>`).join("")}
      </table></div></div>`).join("") : `<div class="panel empty">No collections yet.</div>`}`;
  $("#add-col").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    const btn = e.target.querySelector("button"); btn.disabled = true;
    try { const r = await api("/api/collections", { locator: $("#col-loc").value, chain_id: Number($("#col-chain").value) }); toast(`Added ${r.collection.slug}`); render(); }
    finally { btn.disabled = false; }
  }); };
}
function weiToStr(w) {
  const s = String(w).padStart(19, "0"); const whole = s.slice(0, -18).replace(/^0+(?=\d)/, ""); const frac = s.slice(-18).replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole;
}

// ---------------------------------------------------------------- eligibility
async function renderEligibility() {
  const cols = liveCollections((await api("/api/collections")).collections); S.collections = cols;
  if (!cols.length) { $("#main").innerHTML = `<div class="page-head"><h1>Eligibility</h1></div><div class="panel empty">Add a collection first.</div>`; return; }
  if (!cols.some((c) => c.slug === S.elig.slug)) S.elig.slug = cols[0].slug;
  const [d, w] = await Promise.all([api(`/api/eligibility?slug=${encodeURIComponent(S.elig.slug)}`), api(`/api/wallets?chain_id=${S.chainId}`)]);
  const filters = ["ALL", "ELIGIBLE", "NOT_ELIGIBLE", "UNKNOWN", "UNVERIFIED"];
  const stages = d.collection.stages;
  const rows = d.rows.filter((r) => (S.elig.filter === "ALL" || r.status === S.elig.filter) && (S.elig.stage === "ALL" || String(r.stage_index) === S.elig.stage));
  const p = d.progress || {};
  const scanning = p.running && p.slug === S.elig.slug;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Eligibility</h1><button data-action="export-elig">Export CSV</button></div>
    <div class="panel">
      <div class="row">
        <label>Collection <select id="e-slug">${options(cols, S.elig.slug, (c) => `${c.slug} (${c.chain_name})`, (c) => c.slug)}</select></label>
        <label>Wallets <select id="e-group"><option value="">All wallets</option>${options(w.groups, "", (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>
        <button class="primary" data-action="scan" ${scanning ? "disabled" : ""}>Scan eligibility</button>
      </div>
      <div style="margin-top:10px;${scanning ? "" : "display:none"}"><div class="progress"><div id="scan-bar" style="width:${p.total ? (100 * p.done) / p.total : 0}%"></div></div>
        <div class="tiny muted" id="scan-text">${p.done || 0} / ${p.total || 0} wallets checked</div></div>
      <p class="tiny muted">Each wallet signs in to OpenSea (a free signature, no transaction) and asks which phases it qualifies for.
        <b>ELIGIBLE</b> = OpenSea confirmed. <b>UNKNOWN</b> = could not get an answer (try again), not “ineligible”. <b>UNVERIFIED</b> = watch-only wallet, could not ask.
        Balances and gas are checked separately in the Mint preview.</p>
    </div>
    <div class="chips">${filters.map((f) => `<button class="small ${S.elig.filter === f ? "on" : ""}" data-action="elig-filter" data-filter="${f}">${esc(f.replace("_", " "))} (${f === "ALL" ? d.rows.length : d.rows.filter((r) => r.status === f).length})</button>`).join("")}
      <span class="spacer"></span><select id="e-stage"><option value="ALL">All phases</option>${options(stages, S.elig.stage, (s) => s.name, (s) => s.index)}</select></div>
    ${rows.length ? `<div class="table-wrap"><table>
      <tr><th>Wallet</th><th>Phase</th><th>Status</th><th>Detail</th><th class="num">Can mint</th><th class="num">Price</th><th>Source</th><th>Checked</th></tr>
      ${rows.map((r) => `<tr><td>${addr(r.address, r.label)}</td><td>${esc(r.stage_name)}<div class="tiny muted">${esc(r.stage_type)}</div></td>
        <td>${badge(r.status)}</td><td class="wrap">${esc(r.detail)}</td><td class="num">${r.max_quantity ?? "—"}</td>
        <td class="num">${r.price_wei === null ? "—" : esc(weiToStr(r.price_wei))}</td><td class="tiny">${esc(r.source)}</td><td class="tiny">${esc(fmtTime(r.checked_at))}</td></tr>`).join("")}
    </table></div>` : `<div class="panel empty">No results yet. Run a scan.</div>`}`;
  $("#e-slug").onchange = (e) => { S.elig.slug = e.target.value; S.elig.stage = "ALL"; render(); };
  $("#e-stage").onchange = (e) => { S.elig.stage = e.target.value; render(); };
}

// ---------------------------------------------------------------- mint
async function renderMint() {
  const [cols, w, settings, nets] = await Promise.all([api("/api/collections"), api(`/api/wallets?chain_id=${S.chainId}`), api("/api/settings"), api("/api/networks")]);
  S.collections = liveCollections(cols.collections); S.networks = nets.networks;
  const m = S.mint, def = settings.mint;
  if (!S.collections.length) { $("#main").innerHTML = `<div class="page-head"><h1>Mint</h1></div><div class="panel empty">Add a collection first.</div><div id="jobs"></div>`; return renderJobs(); }
  if (!S.collections.some((c) => c.slug === m.slug)) m.slug = S.collections[0].slug;
  const col = S.collections.find((c) => c.slug === m.slug);
  const stages = col.stages.filter((s) => s.state !== "ended");
  if (!stages.some((s) => String(s.index) === String(m.stage))) m.stage = stages.length ? String(stages[0].index) : "";
  const stage = col.stages.find((s) => String(s.index) === String(m.stage));
  m.form = m.form || { quantity: 1, gas_limit: def.gas_limit, fee_mode: def.fee_mode, max_fee_gwei: def.max_fee_gwei, priority_fee_gwei: def.priority_fee_gwei, public_offset_ms: def.public_offset_ms, operation_cap: "" };
  const f = m.form;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Mint</h1></div>
    <div class="panel">
      <div class="row">
        <label>Collection <select id="m-slug">${options(S.collections, m.slug, (c) => `${c.slug} (${c.chain_name})`, (c) => c.slug)}</select></label>
        <label>Phase <select id="m-stage">${stages.length ? options(stages, m.stage, (s) => `${s.name} — ${s.state}`, (s) => s.index) : "<option>No open phase</option>"}</select></label>
        ${stage ? `<div class="muted">${stage.state === "upcoming" ? `Opens in ${countdown(stage.starts_at)}` : "Open now"} · price ${stage.price_wei === null ? "—" : esc(weiToStr(stage.price_wei)) + " " + esc(symbol(col.chain_id))} · max ${stage.max_per_wallet ?? "—"}/wallet</div>` : ""}
      </div>
      <div class="row" style="margin-top:10px">
        <label>Wallets <select id="m-source">
          <option value="eligible" ${m.source === "eligible" ? "selected" : ""}>Eligible for this phase (from scan)</option>
          <option value="group" ${m.source === "group" ? "selected" : ""}>A group</option>
          <option value="selected" ${m.source === "selected" ? "selected" : ""}>Selected on Wallets page (${S.selected.size})</option>
          <option value="all" ${m.source === "all" ? "selected" : ""}>All wallets with keys</option></select></label>
        ${m.source === "group" ? `<label>Group <select id="m-group">${options(w.groups, m.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>` : ""}
        <label>Quantity / wallet <input id="m-quantity" type="number" min="1" value="${esc(f.quantity)}" style="width:90px"></label>
        <label>Gas limit <input id="m-gas" type="number" min="21000" value="${esc(f.gas_limit)}" style="width:110px"></label>
        <label>Fees <select id="m-fee"><option value="auto" ${f.fee_mode === "auto" ? "selected" : ""}>Automatic</option><option value="manual" ${f.fee_mode === "manual" ? "selected" : ""}>Manual</option></select></label>
        ${f.fee_mode === "manual" ? `<label>Max fee (gwei)<input id="m-maxfee" value="${esc(f.max_fee_gwei)}" style="width:100px"></label><label>Priority (gwei)<input id="m-prio" value="${esc(f.priority_fee_gwei)}" style="width:100px"></label>` : ""}
        ${stage && stage.type === "PUBLIC_SALE" ? `<label>Send early (ms)<input id="m-offset" type="number" min="0" max="60000" value="${esc(f.public_offset_ms)}" style="width:100px"></label>` : ""}
        <label>Max total for this operation (${esc(symbol(col.chain_id))}) <input id="m-cap" value="${esc(f.operation_cap)}" placeholder="limit from Settings" style="width:150px"></label>
        <button class="primary" data-action="preview" ${stage ? "" : "disabled"}>Preview</button>
      </div>
      <p class="tiny muted">Preview checks every wallet (eligibility, balance, gas, allocation, limits; public phases are also simulated on-chain). Nothing is sent until you confirm.</p>
    </div>
    <div id="preview">${m.preview ? previewHTML(m.preview) : ""}</div>
    <div id="jobs"></div>`;
  $("#m-slug").onchange = (e) => { m.slug = e.target.value; m.stage = ""; m.preview = null; render(); };
  $("#m-stage").onchange = (e) => { m.stage = e.target.value; m.preview = null; render(); };
  $("#m-source").onchange = (e) => { readMintForm(); m.source = e.target.value; m.preview = null; render(); };
  $("#m-fee").onchange = (e) => { readMintForm(); f.fee_mode = e.target.value; render(); };
  if ($("#m-group")) $("#m-group").onchange = (e) => { m.group = e.target.value; m.preview = null; };
  if (m.source === "group" && !m.group && w.groups.length) m.group = w.groups[0].name;
  await renderJobs();
}
function readMintForm() {
  const f = S.mint.form, v = (id) => ($(id) ? $(id).value : undefined);
  f.quantity = v("#m-quantity") ?? f.quantity; f.gas_limit = v("#m-gas") ?? f.gas_limit;
  f.max_fee_gwei = v("#m-maxfee") ?? f.max_fee_gwei; f.priority_fee_gwei = v("#m-prio") ?? f.priority_fee_gwei;
  f.public_offset_ms = v("#m-offset") ?? f.public_offset_ms; f.operation_cap = v("#m-cap") ?? f.operation_cap;
}
async function mintSelection() {
  const m = S.mint;
  if (m.source === "group") return { group: m.group };
  if (m.source === "selected") return { addresses: [...S.selected] };
  const w = await api(`/api/wallets?chain_id=${S.chainId}`);
  const keyed = w.wallets.filter((x) => x.has_key).map((x) => x.address);
  if (m.source === "all") return { addresses: keyed };
  const col = S.collections.find((c) => c.slug === m.slug);
  const stage = col.stages.find((s) => String(s.index) === String(m.stage));
  if (stage.type === "PUBLIC_SALE") return { addresses: keyed };
  const e = await api(`/api/eligibility?slug=${encodeURIComponent(m.slug)}`);
  return { addresses: e.rows.filter((r) => String(r.stage_index) === String(m.stage) && r.status === "ELIGIBLE" && r.max_quantity !== 0).map((r) => r.address) };
}
function previewHTML(p) {
  const t = p.totals, sym = symbol(p.collection.chain_id);
  p.stage.state = liveState(p.stage);
  return `<div class="panel">
    <div class="row"><h2 style="margin:0">Preview — ${esc(p.collection.slug)} · ${esc(p.stage.name)}</h2><span class="spacer"></span>
      ${p.stage.state === "upcoming" ? `<span class="muted">Opens in ${countdown(p.stage.starts_at)}</span>` : badge(p.stage.state)}</div>
    <div class="grid cards" style="margin:12px 0">
      <div class="card"><div class="muted">Ready</div><div class="big">${t.ready} / ${t.selected}</div></div>
      <div class="card"><div class="muted">Mint total</div><div class="big">${esc(t.value)} ${esc(sym)}</div></div>
      <div class="card"><div class="muted">Max gas total</div><div class="big">${esc(t.max_gas)} ${esc(sym)}</div><div class="tiny muted">max fee ${esc(p.fees.max_fee_gwei)} gwei × ${esc(p.fees.gas_limit)} gas</div></div>
      <div class="card"><div class="muted">Worst case</div><div class="big">${esc(t.worst_case)} ${esc(sym)}</div><div class="tiny muted">limit ${esc(t.operation_cap)} · 24h ${esc(t.daily_used)}/${esc(t.daily_limit)}</div></div>
    </div>
    ${p.blocks.map((b) => `<div class="notice bad">${esc(b)}</div>`).join("")}
    ${p.notes.map((n) => `<div class="notice info">${esc(n)}</div>`).join("")}
    <div class="table-wrap"><table>
      <tr><th>Wallet</th><th>Eligibility</th><th>Readiness</th><th>Reason</th><th class="num">Balance</th><th class="num">Mint</th><th class="num">Max gas</th><th>Simulation</th></tr>
      ${p.rows.map((r) => `<tr><td>${addr(r.address, r.label)}</td><td>${badge(r.eligibility)}</td><td>${badge(r.status)}</td><td class="wrap">${esc(r.reason)}</td>
        <td class="num">${r.balance ?? "—"}</td><td class="num">${esc(r.value)}</td><td class="num">${esc(r.max_gas_cost)}</td><td class="tiny">${esc(r.simulation || "—")}</td></tr>`).join("")}
    </table></div>
    ${p.token ? `<div class="row" style="margin-top:14px">
      <label>Type MINT to send ${t.ready} transaction${t.ready === 1 ? "" : "s"}<input id="m-confirm" autocomplete="off" style="width:140px"></label>
      <button class="primary" data-action="start">Start mint</button>
      <span class="tiny muted">Preview valid for ${countdown(p.created + p.expires_in)}. Only READY wallets are included.</span></div>` : ""}
  </div>`;
}
async function renderJobs() {
  const el = $("#jobs"); if (!el) return;
  const d = await api("/api/mint/jobs");
  el.innerHTML = `<div class="panel"><h2>Operations</h2>${d.jobs.length ? d.jobs.map(jobCard).join("") : `<div class="empty">No operations yet.</div>`}</div>`;
}
function jobCard(j) {
  const open = j.starts_at > Date.now() / 1000;
  return `<div class="panel" style="padding:12px">
    <div class="row"><b>#${j.id} ${esc(j.slug)} · ${esc(j.stage)}</b> ${badge(j.status)}
      <span class="muted">${j.runs.length} wallet${j.runs.length === 1 ? "" : "s"} · qty ${j.quantity} · reserved ${esc(j.reserved)} / ${esc(j.cap)}</span>
      <span class="spacer"></span>${open && j.status === "running" ? `<span>Starts in ${countdown(j.starts_at)}</span>` : ""}</div>
    <div class="table-wrap" style="margin-top:8px"><table>
      <tr><th>Wallet</th><th>Status</th><th>Message</th><th>Transaction</th><th>Log</th></tr>
      ${j.runs.map((r) => `<tr><td>${addr(r.address, r.label)}</td><td>${badge(r.phase)}</td><td class="wrap">${esc(r.message)}</td>
        <td>${txLink(j.chain_id, r.tx_hash)}</td>
        <td><details><summary class="tiny">${r.logs.length} lines</summary><div class="log">${r.logs.map((l) => `<div class="${esc(l.level)}">${esc(new Date(l.t * 1000).toLocaleTimeString())} ${esc(l.msg)}</div>`).join("")}</div></details></td></tr>`).join("")}
    </table></div></div>`;
}

// ---------------------------------------------------------------- history
function historyTable(rows) {
  if (!rows.length) return `<div class="empty">No transactions yet.</div>`;
  return `<div class="table-wrap"><table>
    <tr><th>Time</th><th>Wallet</th><th>Operation</th><th>Collection</th><th>Status</th><th>Transaction</th><th class="num">Value</th><th class="num">Gas</th><th>Error</th></tr>
    ${rows.map((r) => `<tr><td class="tiny">${esc(fmtTime(r.created_at))}</td><td>${addr(r.address)}</td><td>${esc(r.operation)} #${r.job_id ?? ""}</td>
      <td>${esc(r.slug || "")}<div class="tiny muted">${esc(r.stage_name || "")}</div></td><td>${badge(r.status)}</td><td>${txLink(r.chain_id, r.tx_hash)}</td>
      <td class="num">${r.value_wei ? esc(weiToStr(r.value_wei)) : "—"}</td><td class="num">${r.gas_cost_wei ? esc(weiToStr(r.gas_cost_wei)) : "—"}</td>
      <td class="wrap tiny">${esc(r.error || "")}</td></tr>`).join("")}</table></div>`;
}
async function renderHistory() {
  const d = await api("/api/history?limit=500");
  $("#main").innerHTML = `<div class="page-head"><h1>History</h1><button data-action="export-history">Export CSV</button></div>${historyTable(d.rows)}`;
}

// ---------------------------------------------------------------- networks
async function renderNetworks() {
  const d = await api("/api/networks"); S.networks = d.networks;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Networks</h1><button data-action="add-network">Add network</button></div>
    ${d.networks.map((n) => `<div class="panel">
      <div class="row"><h2 style="margin:0">${esc(n.name)}</h2><span class="muted">chain ${n.chain_id} · ${esc(n.native_symbol)}</span>
        ${n.enabled ? badge("active") : `<span class="badge">disabled</span>`}<span class="spacer"></span>
        <button class="small" data-action="net-check" data-chain="${n.chain_id}">Check now</button>
        <button class="small" data-action="net-toggle" data-chain="${n.chain_id}">${n.enabled ? "Disable" : "Enable"}</button></div>
      <div class="table-wrap" style="margin-top:10px"><table>
        <tr><th>RPC endpoint</th><th>Health</th><th class="num">Latency</th><th class="num">Block</th><th>Last error</th><th>Checked</th><th></th></tr>
        ${n.rpcs.map((r) => `<tr><td class="mono">${esc(maskUrl(r.url))}</td>
          <td>${!r.enabled ? `<span class="badge">off</span>` : r.healthy === 1 ? badge("healthy") : r.healthy === 0 ? `<span class="badge bad">down</span>` : `<span class="badge">unchecked</span>`}</td>
          <td class="num">${r.latency_ms != null ? `${r.latency_ms} ms` : "—"}</td><td class="num">${r.block_number ?? "—"}</td>
          <td class="wrap tiny">${esc(r.last_error || "")}</td><td class="tiny">${esc(fmtTime(r.checked_at))}</td>
          <td><button class="small" data-action="rpc-toggle" data-chain="${n.chain_id}" data-id="${r.id}">${r.enabled ? "Disable" : "Enable"}</button>
            <button class="small danger" data-action="rpc-remove" data-chain="${n.chain_id}" data-id="${r.id}">Remove</button></td></tr>`).join("")}
      </table></div>
      <form class="row" data-add-rpc="${n.chain_id}" style="margin-top:10px">
        <label style="flex:1 1 360px">Add RPC URL<input name="url" placeholder="https://…" required></label><button type="submit">Add & test</button></form>
    </div>`).join("")}
    <p class="tiny muted">Endpoints are used only after they report the right chain ID. Reads fall back to the next healthy endpoint automatically; a mint operation uses the fastest healthy endpoint at the moment it starts.</p>`;
  $$("form[data-add-rpc]").forEach((form) => (form.onsubmit = (e) => { e.preventDefault(); guard(async () => {
    const r = await api("/api/networks", { action: "add_rpc", chain_id: Number(form.dataset.addRpc), url: form.url.value });
    const added = r.rpcs.find((x) => x.url === form.url.value.trim());
    toast(added && added.healthy ? "RPC added and healthy" : `RPC added but not healthy: ${added?.last_error || "check failed"}`, !(added && added.healthy));
    render();
  }); }));
}
function maskUrl(u) {
  try { const x = new URL(u); const p = x.pathname.length > 12 ? `${x.pathname.slice(0, 10)}…` : x.pathname; return `${x.protocol}//${x.host}${p}`; } catch { return u; }
}

// ---------------------------------------------------------------- settings
async function renderSettings() {
  const d = await api("/api/settings");
  $("#main").innerHTML = `
    <div class="page-head"><h1>Settings</h1></div>
    <div class="two">
      <form class="panel" id="limits-form"><h2>Spending limits</h2>
        <p class="tiny muted">In the network's native token (ETH on Robinhood Chain). Enforced by the backend at the moment each transaction would be sent.</p>
        ${Object.entries(d.limits).map(([k, v]) => `<label>${esc(d.limit_labels[k])}<input name="${esc(k)}" value="${esc(v)}"></label>`).join("")}
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save limits</button></div></form>
      <form class="panel" id="mint-form"><h2>Mint defaults</h2>
        <label>Gas limit per transaction<input name="gas_limit" type="number" value="${esc(d.mint.gas_limit)}"></label>
        <label>Fee mode<select name="fee_mode">${options(["auto", "manual"], d.mint.fee_mode)}</select></label>
        <label>Manual max fee (gwei)<input name="max_fee_gwei" value="${esc(d.mint.max_fee_gwei)}"></label>
        <label>Manual priority fee (gwei)<input name="priority_fee_gwei" value="${esc(d.mint.priority_fee_gwei)}"></label>
        <label>Public mint: send early by (ms) — early sends can revert<input name="public_offset_ms" type="number" value="${esc(d.mint.public_offset_ms)}"></label>
        <label>Wallets preparing at the same time (OpenSea sign-ins)<input name="setup_concurrency" type="number" value="${esc(d.mint.setup_concurrency)}"></label>
        <label>Receipt wait before marking pending (seconds)<input name="pending_timeout_seconds" type="number" value="${esc(d.mint.pending_timeout_seconds)}"></label>
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save mint defaults</button></div></form>
      <form class="panel" id="elig-form"><h2>Eligibility scan pacing</h2>
        <p class="tiny muted">Lower values are gentler on OpenSea and avoid rate limits.</p>
        <label>Wallets checked at the same time (1-10)<input name="concurrency" type="number" value="${esc(d.eligibility.concurrency)}"></label>
        <label>Pause between sign-ins (seconds)<input name="spacing_seconds" value="${esc(d.eligibility.spacing_seconds)}"></label>
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save</button></div></form>
      <form class="panel" id="pw-form"><h2>Vault</h2>
        <label>Current password<input name="old" type="password" autocomplete="current-password"></label>
        <label>New password (10+ characters)<input name="new" type="password" autocomplete="new-password"></label>
        <div class="row" style="margin-top:10px"><button type="submit">Change password</button><span class="spacer"></span><button type="button" class="danger" data-action="lock">Lock now</button></div></form>
    </div>`;
  const formData = (f) => Object.fromEntries(new FormData(f).entries());
  $("#limits-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { limits: formData(e.target) }); toast("Limits saved"); }); };
  $("#mint-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { mint: formData(e.target) }); S.mint.form = null; toast("Mint defaults saved"); }); };
  $("#elig-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { eligibility: formData(e.target) }); toast("Saved"); }); };
  $("#pw-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/vault/password", formData(e.target)); e.target.reset(); toast("Password changed"); }); };
}

const PAGE_RENDER = { overview: renderOverview, wallets: renderWallets, collections: renderCollections, eligibility: renderEligibility, mint: renderMint, history: renderHistory, networks: renderNetworks, settings: renderSettings };

// ---------------------------------------------------------------- actions
const ACTIONS = {
  close: closeModal,
  copy: (el) => { navigator.clipboard?.writeText(el.dataset.text); toast("Copied"); },
  resume: () => guard(async () => { await api("/api/resume", { confirm: $("#resume-text").value }); setStopped(false); closeModal(); toast("Resumed"); }),
  lock: () => guard(async () => { await api("/api/vault/lock", {}); showLock(); }),
  "import-keys": () => modal(`<h2>Import private keys</h2>
    <p class="tiny muted">One wallet per line: <span class="mono">KEY</span> or <span class="mono">label, KEY</span>. Keys are encrypted into the local vault and never shown again. Lines starting with # are ignored.</p>
    <textarea id="keys" rows="8" spellcheck="false" autocomplete="off"></textarea>
    <label>Add to group (optional)<input id="keys-group"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="import-keys-go">Import</button></div>`),
  "import-keys-go": () => guard(async () => {
    const r = await api("/api/wallets/import-keys", { text: $("#keys").value, group: $("#keys-group").value });
    $("#keys").value = ""; closeModal(); toast(`Imported ${r.imported} (${r.new} new)`); render();
  }),
  generate: () => modal(`<h2>Generate new wallets</h2>
    <p class="tiny muted">New keys are created on this computer and stored only in the encrypted vault. Back up the <span class="mono">data</span> folder and your password.</p>
    <label>How many (1-500)<input id="gen-count" type="number" min="1" max="500" value="5"></label>
    <label>Label prefix<input id="gen-prefix" value="mint"></label><label>Add to group (optional)<input id="gen-group"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="generate-go">Generate</button></div>`),
  "generate-go": () => guard(async () => {
    const r = await api("/api/wallets/generate", { count: Number($("#gen-count").value), prefix: $("#gen-prefix").value, group: $("#gen-group").value });
    closeModal(); toast(`Generated ${r.addresses.length} wallets`); render();
  }),
  watch: () => modal(`<h2>Add watch-only wallet</h2><p class="tiny muted">Track balances of an address without its key (it cannot mint).</p>
    <label>Address<input id="watch-addr" class="mono"></label><label>Label<input id="watch-label"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="watch-go">Add</button></div>`),
  "watch-go": () => guard(async () => { await api("/api/wallets/watch", { address: $("#watch-addr").value, label: $("#watch-label").value }); closeModal(); render(); }),
  "csv-import": () => modal(`<h2>Import wallet list (CSV)</h2>
    <p class="tiny muted">Columns: <span class="mono">address,label,groups</span> (groups separated by ;). Addresses only — files containing private keys or seed phrases are refused.</p>
    <input type="file" id="csv-file" accept=".csv,text/csv"><textarea id="csv-text" rows="7" placeholder="address,label,groups"></textarea>
    <div id="csv-result"></div>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button data-action="csv-validate">Validate</button><button class="primary" data-action="csv-go">Import</button></div>`),
  "csv-validate": () => guard(async () => {
    const r = await api("/api/wallets/csv/validate", { text: $("#csv-text").value });
    $("#csv-result").innerHTML = r.ok ? `<div class="notice info">${r.rows.length} rows look good.</div>` : r.problems.map((p) => `<div class="notice bad">${esc(p)}</div>`).join("");
  }),
  "csv-go": () => guard(async () => { const r = await api("/api/wallets/csv/import", { text: $("#csv-text").value }); closeModal(); toast(`Imported ${r.imported} rows`); render(); }),
  "export-wallets": () => download("/api/wallets/export.csv", "wallets.csv"),
  "export-history": () => download("/api/history.csv", "history.csv"),
  "export-elig": () => download(`/api/eligibility.csv?slug=${encodeURIComponent(S.elig.slug)}`, `eligibility-${S.elig.slug}.csv`),
  "refresh-balances": (el) => guard(async () => {
    el.disabled = true;
    try { const r = await api("/api/balances/scan", { chain_id: S.chainId, group: S.walletFilter.group }); toast(`Updated ${r.updated} balances${r.failed ? `, ${r.failed} failed` : ""}`); render(); }
    finally { el.disabled = false; }
  }),
  "add-token": () => modal(`<h2>Track an ERC-20 token on ${esc(network(S.chainId)?.name || "")}</h2>
    <label>Token contract address<input id="token-addr" class="mono"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="token-go">Add</button></div>`),
  "token-go": () => guard(async () => { const r = await api("/api/tokens", { chain_id: S.chainId, address: $("#token-addr").value }); closeModal(); toast(`Tracking ${r.token.symbol}`); render(); }),
  label: (el) => modal(`<h2>Label</h2><p class="mono tiny">${esc(el.dataset.address)}</p><label>Label<input id="label-text" value="${esc(el.dataset.label)}"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="label-go" data-address="${esc(el.dataset.address)}">Save</button></div>`),
  "label-go": (el) => guard(async () => { await api("/api/wallets/label", { address: el.dataset.address, label: $("#label-text").value }); closeModal(); render(); }),
  "new-group": () => modal(`<h2>New group</h2><label>Name<input id="group-name"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="new-group-go">Create</button></div>`),
  "new-group-go": () => guard(async () => { await api("/api/groups", { action: "create", name: $("#group-name").value }); closeModal(); render(); }),
  "rename-group": () => modal(`<h2>Rename group</h2><label>New name<input id="group-name" value="${esc(S.walletFilter.group)}"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="rename-group-go">Rename</button></div>`),
  "rename-group-go": () => guard(async () => { const n = $("#group-name").value; await api("/api/groups", { action: "rename", name: S.walletFilter.group, new_name: n }); S.walletFilter.group = n; closeModal(); render(); }),
  "delete-group": () => guard(async () => { if (!confirm(`Delete group “${S.walletFilter.group}”? Wallets are kept.`)) return; await api("/api/groups", { action: "delete", name: S.walletFilter.group }); S.walletFilter.group = ""; render(); }),
  "group-add": () => modal(`<h2>Add ${S.selected.size} wallets to a group</h2>
    <label>Group<input id="group-name" list="group-list"><datalist id="group-list">${options(S.walletData.groups, "", (g) => g.name, (g) => g.name)}</datalist></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="group-add-go">Add</button></div>`),
  "group-add-go": () => guard(async () => { await api("/api/groups", { action: "add", name: $("#group-name").value, addresses: [...S.selected] }); closeModal(); render(); }),
  "group-remove": () => guard(async () => { await api("/api/groups", { action: "remove", name: S.walletFilter.group, addresses: [...S.selected] }); render(); }),
  "remove-wallets": () => modal(`<h2>Remove ${S.selected.size} wallets</h2>
    <div class="notice bad">Their private keys are deleted from the vault. Make sure you have a backup or the wallets are empty.</div>
    <label>Type REMOVE to confirm<input id="remove-text" autocomplete="off"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="danger" data-action="remove-go">Remove</button></div>`),
  "remove-go": () => guard(async () => { const r = await api("/api/wallets/remove", { addresses: [...S.selected], confirm: $("#remove-text").value }); S.selected.clear(); closeModal(); toast(`Removed ${r.removed}`); render(); }),
  "mint-selected": () => { S.mint.source = "selected"; S.mint.preview = null; go("mint"); },
  "col-refresh": (el) => guard(async () => { await api("/api/collections", { locator: el.dataset.slug }); toast("Refreshed"); render(); }),
  "col-elig": (el) => { S.elig.slug = el.dataset.slug; go("eligibility"); },
  "col-mint": (el) => { S.mint.slug = el.dataset.slug; S.mint.preview = null; go("mint"); },
  "col-delete": (el) => guard(async () => { if (!confirm(`Remove ${el.dataset.slug} and its eligibility results?`)) return; await api("/api/collections/delete", { slug: el.dataset.slug }); render(); }),
  scan: () => guard(async () => { const r = await api("/api/eligibility/scan", { slug: S.elig.slug, group: $("#e-group").value }); toast(`Checking ${r.total} wallets…`); render(); }),
  "elig-filter": (el) => { S.elig.filter = el.dataset.filter; render(); },
  preview: (el) => guard(async () => {
    readMintForm(); const f = S.mint.form; el.disabled = true; el.textContent = "Checking…";
    try {
      const sel = await mintSelection();
      if (sel.addresses && !sel.addresses.length) throw new ApiError(S.mint.source === "eligible" ? "No eligible wallets for this phase yet — run the eligibility scan, or choose another wallet source." : "No wallets selected.");
      const p = await api("/api/mint/preview", { slug: S.mint.slug, stage_index: Number(S.mint.stage), ...sel, quantity: Number(f.quantity), gas_limit: Number(f.gas_limit), fee_mode: f.fee_mode, max_fee_gwei: f.max_fee_gwei, priority_fee_gwei: f.priority_fee_gwei, public_offset_ms: Number(f.public_offset_ms || 0), operation_cap: f.operation_cap });
      p.created = Date.now() / 1000; S.mint.preview = p; $("#preview").innerHTML = previewHTML(p);
      $("#preview").scrollIntoView({ behavior: "smooth", block: "start" });
    } finally { el.disabled = false; el.textContent = "Preview"; }
  }),
  start: (el) => guard(async () => {
    el.disabled = true;
    try {
      const r = await api("/api/mint/start", { token: S.mint.preview.token, confirm: $("#m-confirm").value });
      S.mint.preview = null; $("#preview").innerHTML = ""; toast(`Operation #${r.job_id} started`); renderJobs();
    } finally { el.disabled = false; }
  }),
  "net-check": (el) => guard(async () => { await api("/api/networks", { action: "check", chain_id: Number(el.dataset.chain) }); render(); }),
  "net-toggle": (el) => guard(async () => { await api("/api/networks", { action: "toggle", chain_id: Number(el.dataset.chain) }); render(); }),
  "rpc-toggle": (el) => guard(async () => { await api("/api/networks", { action: "toggle_rpc", chain_id: Number(el.dataset.chain), rpc_id: Number(el.dataset.id) }); render(); }),
  "rpc-remove": (el) => guard(async () => { if (!confirm("Remove this RPC endpoint?")) return; await api("/api/networks", { action: "remove_rpc", chain_id: Number(el.dataset.chain), rpc_id: Number(el.dataset.id) }); render(); }),
  "add-network": () => modal(`<h2>Add network</h2><p class="tiny muted">Any EVM chain OpenSea supports. You can add RPC endpoints after creating it.</p>
    <label>Chain ID<input id="net-id" type="number"></label><label>Name<input id="net-name"></label>
    <label>Native token symbol<input id="net-symbol" value="ETH"></label><label>Explorer URL (optional)<input id="net-explorer" placeholder="https://…"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="add-network-go">Add</button></div>`),
  "add-network-go": () => guard(async () => { await api("/api/networks", { action: "add", chain_id: Number($("#net-id").value), name: $("#net-name").value, symbol: $("#net-symbol").value, explorer: $("#net-explorer").value }); closeModal(); render(); }),
};
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (el && ACTIONS[el.dataset.action]) { e.preventDefault(); ACTIONS[el.dataset.action](el, e); }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "csv-file" && e.target.files[0]) e.target.files[0].text().then((t) => { $("#csv-text").value = t; });
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

boot();
