"use strict";

// ---------------------------------------------------------------- state & helpers
const S = {
  session: sessionStorage.getItem("nftops.session"),
  page: (location.hash || "#overview").slice(1),
  chainId: Number(localStorageGet("nftops.chain") || 4663),
  networks: [], selected: new Set(), walletFilter: { group: "", q: "" },
  elig: { slug: "", filter: "ALL", stage: "ALL" },
  mint: { slug: "", stage: "", source: "eligible", group: "", preview: null },
  tx: { tab: "nfts", preview: null, nft: { contract: "", data: null, picked: new Set(), mode: "consolidate" },
        sweep: { asset: "native", source: "group", group: "", reserve: "0.0005", min_balance: "0" },
        dist: { asset: "native", mode: "equal_each" } },
  stopped: false,
};
const PAGES = ["overview", "wallets", "collections", "eligibility", "mint", "transfers", "contracts", "history", "networks", "settings"];
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
    el.classList.toggle("soon", left > 0 && left <= 60);
  }
}, 1000);

const BADGE = {
  ELIGIBLE: "good", NOT_ELIGIBLE: "bad", UNKNOWN: "warn", UNVERIFIED: "warn", "NOT SCANNED": "warn",
  READY: "good", confirmed: "good", active: "good", healthy: "good",
  pending: "info", submitted: "info", broadcasting: "info", waiting: "info", preparing: "info", queued: "info", running: "info", upcoming: "info",
  failed: "bad", blocked: "bad", not_submitted: "bad", ended: "", cancelled: "warn", stopped: "warn", finished: "", replaced: "", not_needed: "",
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
  api("/api/alerts").then((a) => setBell(a.unread)).catch(() => {});
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
  if (ev.kind === "alert") onAlert(ev);
  if (ev.kind === "locked") { toast(`Vault ${ev.reason}`); showLock(); }
  if (ev.kind === "sale_open") toast("Sale is open — sending now", false);
  if (["replace", "replace_done"].includes(ev.kind) && S.page === "history") scheduleRefresh();
  if (ev.kind === "failover") toast(`RPC did not answer — switched to a backup link (${ev.operation}).`, true);
  if (["run", "job", "mint_log"].includes(ev.kind) && ["mint", "overview"].includes(S.page)) scheduleRefresh();
  if (["txrun", "txjob"].includes(ev.kind) && ["transfers", "overview", "contracts"].includes(S.page)) scheduleRefresh();
  if (ev.kind === "eligibility" && S.page === "eligibility") {
    const bar = $("#scan-bar"); if (bar) { bar.style.width = `${(100 * ev.done) / ev.total}%`; $("#scan-text").textContent = `${ev.done} / ${ev.total} wallets checked`; }
  }
  if (ev.kind === "eligibility_done") { toast("Eligibility scan finished"); if (S.page === "eligibility") render(); }
}
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    if (S.page === "mint") await renderJobs();
    else if (S.page === "transfers" || S.page === "contracts") await renderTxJobs();
    else if (S.page === "history") await render();
    else if (S.page === "overview") { if (S.mint.preview || $("#jobs")?.childElementCount) await renderJobs(); else await render(); }
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
  const [d, ctx, a] = await Promise.all([api("/api/overview"), mintContext(), api("/api/alerts")]);
  setStopped(d.stopped); setBell(a.unread);
  const c = d.last24h;
  const w = ctx ? ctx.w : await api(`/api/wallets?chain_id=${S.chainId}`);
  let elig = {};
  if (ctx && ctx.stage) {
    const e = await api(`/api/eligibility?slug=${encodeURIComponent(S.mint.slug)}`);
    for (const r of e.rows) if (String(r.stage_index) === String(S.mint.stage)) elig[r.address] = r.status;
    if (ctx.stage.type === "PUBLIC_SALE") elig = Object.fromEntries(w.wallets.map((x) => [x.address, "ELIGIBLE"]));
  }
  const phases = [];
  for (const col of S.collections || []) for (const s of col.stages) if (s.state !== "ended") phases.push({ col, s });
  phases.sort((x, y) => (x.s.state === "active" ? 0 : 1) - (y.s.state === "active" ? 0 : 1) || x.s.starts_at - y.s.starts_at);
  const sym = symbol(S.chainId);
  const ready = w.wallets.filter((x) => x.has_key && elig[x.address] === "ELIGIBLE").length;
  $("#main").innerHTML = `
    <div class="stat-strip">
      <div class="stat"><div class="k">Wallets</div><div class="v">${d.wallets}</div></div>
      <div class="stat"><div class="k">With keys</div><div class="v">${d.with_keys}</div></div>
      <div class="stat hot"><div class="k">Eligible (any)</div><div class="v">${d.eligible_wallets}</div></div>
      ${d.totals.map((t) => `<div class="stat"><div class="k">${esc(t.name)}</div><div class="v">${esc(t.native)} <span class="tiny muted">${esc(t.native_symbol)}</span></div></div>`).join("")}
      <div class="stat hot"><div class="k">Minted 24h</div><div class="v">${c.confirmed || 0}</div></div>
      <div class="stat"><div class="k">Failed / blocked 24h</div><div class="v">${(c.failed || 0) + (c.blocked || 0)}</div></div>
    </div>
    <div class="cc-grid">
      <section class="panel cc-phases"><h2>Phases</h2><div class="cc-scroll">
        ${phases.length ? phases.map(({ col, s }) => `<div class="phase ${s.state === "active" ? "active" : ""}">
          <div class="top"><span class="name">${esc(col.slug)}</span><span class="spacer"></span>${badge(s.state === "active" ? "active" : "upcoming")}</div>
          <div class="tiny muted">${esc(s.name)} · ${esc(col.chain_name)}${col.watch ? " · 👁 watching" : ""}</div>
          <div class="big-count">${s.state === "active" ? "OPEN" : countdown(s.starts_at)}</div>
          <div class="row"><span class="meta">${s.price_wei === null ? "price —" : `${esc(weiToStr(s.price_wei))} ${esc(symbol(col.chain_id))}`} · ${s.type === "PUBLIC_SALE" ? "public" : `${s.eligible_wallets} eligible`}</span>
            <span class="spacer"></span><button class="small primary" data-action="cc-pick" data-slug="${esc(col.slug)}" data-stage="${s.index}">Mint ▸</button></div>
        </div>`).join("") : `<div class="empty">No open or upcoming phases.<br><button class="small" data-page-link="collections">Add a collection</button></div>`}
      </div></section>
      <section class="panel cc-mint">${ctx ? mintPanelHTML(ctx, true) : `<h2>Quick mint</h2><div class="empty">Add a collection first.</div>`}
        <div id="preview">${S.mint.preview ? previewHTML(S.mint.preview) : ""}</div><div id="jobs"></div></section>
      <section class="panel cc-alerts"><div class="row"><h2 style="margin:0">Alerts</h2><span class="spacer"></span>${a.unread ? `<button class="small" data-action="alerts-read">Mark read</button>` : ""}</div>
        <div class="cc-scroll" style="margin-top:8px">${a.alerts.length ? alertsHTML(a.alerts.slice(0, 15)) : `<div class="empty">No alerts yet. Click Watch on a collection to get phase alerts.</div>`}</div></section>
      <section class="panel cc-wallets">
        <div class="row"><h2 style="margin:0">Wallets</h2><span class="muted tiny">${ctx && ctx.stage ? `${ready} eligible for ${esc(S.mint.slug)} · ${esc(ctx.stage.name)}` : ""}</span><span class="spacer"></span>
          <label>Network ${chainSelect("cc-chain", S.chainId)}</label><button class="small" data-action="refresh-balances">Refresh balances</button></div>
        ${w.wallets.length ? `<div class="table-wrap cc-scroll" style="margin-top:10px;max-height:360px"><table>
          <tr><th>Wallet</th><th>Groups</th><th>Key</th><th>This phase</th><th class="num">${esc(sym)}</th><th class="num">NFTs</th></tr>
          ${w.wallets.map((x) => `<tr><td>${addr(x.address, x.label)}</td><td>${x.groups.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</td>
            <td>${x.has_key ? `<span class="badge good">key</span>` : `<span class="badge">watch</span>`}</td>
            <td>${elig[x.address] ? badge(elig[x.address]) : `<span class="muted tiny">—</span>`}</td>
            <td class="num">${x.native ?? "—"}</td><td class="num">${x.nfts}</td></tr>`).join("")}</table></div>`
          : `<div class="empty">No wallets yet. <button class="small" data-page-link="wallets">Add wallets</button></div>`}
      </section>
    </div>`;
  if (ctx) bindMintPanel(ctx);
  $("#cc-chain").onchange = (e) => { S.chainId = Number(e.target.value); localStorageSet("nftops.chain", S.chainId); render(); };
  await renderJobs();
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
        · checked ${esc(fmtTime(c.checked_at || c.updated_at))}${c.supply ? ` · minted ${c.supply.total}${c.supply.max ? ` / ${c.supply.max}` : ""}` : ""}</div></div><span class="spacer"></span>
        <button class="small ${c.watch ? "primary" : ""}" data-action="col-watch" data-slug="${esc(c.slug)}" data-watch="${c.watch ? 0 : 1}" title="Alerts for phase openings, changes and sell-outs">${c.watch ? "Watching ✓" : "Watch"}</button>
        <button class="small" data-action="col-check" data-slug="${esc(c.slug)}">Check now</button>
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
  const ctx = await mintContext();
  if (!ctx) { $("#main").innerHTML = `<div class="page-head"><h1>Mint</h1></div><div class="panel empty">Add a collection first.</div><div id="jobs"></div>`; return renderJobs(); }
  $("#main").innerHTML = `<div class="page-head"><h1>Mint</h1></div>
    <div class="panel">${mintPanelHTML(ctx, false)}</div>
    <div id="preview">${S.mint.preview ? previewHTML(S.mint.preview) : ""}</div>
    <div id="jobs"></div>`;
  bindMintPanel(ctx);
  await renderJobs();
}
// Shared by the Mint page and the command center's quick-mint panel.
async function mintContext() {
  const [cols, w, settings, nets] = await Promise.all([api("/api/collections"), api(`/api/wallets?chain_id=${S.chainId}`), api("/api/settings"), api("/api/networks")]);
  S.collections = liveCollections(cols.collections); S.networks = nets.networks;
  const m = S.mint, def = settings.mint;
  if (!S.collections.length) return null;
  if (!S.collections.some((c) => c.slug === m.slug)) m.slug = S.collections[0].slug;
  const col = S.collections.find((c) => c.slug === m.slug);
  const stages = col.stages.filter((s) => s.state !== "ended");
  if (!stages.some((s) => String(s.index) === String(m.stage))) m.stage = stages.length ? String(stages[0].index) : "";
  const stage = col.stages.find((s) => String(s.index) === String(m.stage));
  m.form = m.form || { quantity: 1, gas_limit: def.gas_limit, fee_mode: def.fee_mode, max_fee_gwei: def.max_fee_gwei, priority_fee_gwei: def.priority_fee_gwei, public_offset_ms: def.public_offset_ms, operation_cap: "" };
  if (m.source === "group" && !m.group && w.groups.length) m.group = w.groups[0].name;
  return { w, col, stages, stage };
}
function mintPanelHTML({ w, col, stages, stage }, compact) {
  const m = S.mint, f = m.form;
  const advanced = `
        <label>Gas limit <input id="m-gas" type="number" min="21000" value="${esc(f.gas_limit)}" style="width:110px"></label>
        <label>Fees <select id="m-fee"><option value="auto" ${f.fee_mode === "auto" ? "selected" : ""}>Automatic</option><option value="manual" ${f.fee_mode === "manual" ? "selected" : ""}>Manual</option></select></label>
        ${f.fee_mode === "manual" ? `<label>Max fee (gwei)<input id="m-maxfee" value="${esc(f.max_fee_gwei)}" style="width:100px"></label><label>Priority (gwei)<input id="m-prio" value="${esc(f.priority_fee_gwei)}" style="width:100px"></label>` : ""}
        ${stage && stage.type === "PUBLIC_SALE" ? `<label>Send early (ms)<input id="m-offset" type="number" min="0" max="60000" value="${esc(f.public_offset_ms)}" style="width:100px"></label>` : ""}
        <label>Max total (${esc(symbol(col.chain_id))}) <input id="m-cap" value="${esc(f.operation_cap)}" placeholder="from Settings" style="width:130px"></label>`;
  return `
      ${compact ? `<h2>Quick mint</h2>` : ""}
      <div class="row">
        <label>Collection <select id="m-slug">${options(S.collections, m.slug, (c) => `${c.slug} (${c.chain_name})`, (c) => c.slug)}</select></label>
        <label>Phase <select id="m-stage">${stages.length ? options(stages, m.stage, (s) => `${s.name} — ${s.state}`, (s) => s.index) : "<option>No open phase</option>"}</select></label>
        ${stage ? `<div class="muted">${stage.state === "upcoming" ? `Opens in ${countdown(stage.starts_at)}` : `<span class="badge good">open now</span>`} · ${stage.price_wei === null ? "—" : esc(weiToStr(stage.price_wei)) + " " + esc(symbol(col.chain_id))} · max ${stage.max_per_wallet ?? "—"}/wallet</div>` : ""}
      </div>
      <div class="row" style="margin-top:10px">
        <label>Wallets <select id="m-source">
          <option value="eligible" ${m.source === "eligible" ? "selected" : ""}>Eligible for this phase</option>
          <option value="group" ${m.source === "group" ? "selected" : ""}>A group</option>
          <option value="selected" ${m.source === "selected" ? "selected" : ""}>Selected wallets (${S.selected.size})</option>
          <option value="all" ${m.source === "all" ? "selected" : ""}>All wallets with keys</option></select></label>
        ${m.source === "group" ? `<label>Group <select id="m-group">${options(w.groups, m.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>` : ""}
        <label>Qty / wallet <input id="m-quantity" type="number" min="1" value="${esc(f.quantity)}" style="width:80px"></label>
        ${compact ? "" : advanced}
        <button class="primary" data-action="preview" ${stage ? "" : "disabled"}>Preview</button>
      </div>
      ${compact ? `<details><summary class="tiny muted">Gas, fees and limits</summary><div class="row" style="margin-top:8px">${advanced}</div></details>` : ""}
      <p class="tiny muted">Preview checks every wallet (eligibility, balance, gas, allocation, limits; open public phases are simulated on-chain). Nothing is sent until you type MINT.</p>`;
}
function bindMintPanel({ w }) {
  const m = S.mint, f = m.form;
  $("#m-slug").onchange = (e) => { m.slug = e.target.value; m.stage = ""; m.preview = null; render(); };
  $("#m-stage").onchange = (e) => { m.stage = e.target.value; m.preview = null; render(); };
  $("#m-source").onchange = (e) => { readMintForm(); m.source = e.target.value; m.preview = null; render(); };
  $("#m-fee").onchange = (e) => { readMintForm(); f.fee_mode = e.target.value; render(); };
  if ($("#m-group")) $("#m-group").onchange = (e) => { m.group = e.target.value; m.preview = null; };
  if (m.source === "group" && !m.group && w.groups.length) m.group = w.groups[0].name;
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
function shortRows(rows) { return rows.filter((r) => r.shortfall && Number(r.shortfall) > 0); }
function fundButton(rows, chainId, key) {
  const n = new Set(shortRows(rows).map((r) => r[key])).size;
  return n ? `<button class="primary" data-action="fund-short" data-chain="${chainId}" data-key="${key}">⚡ Fund ${n} short wallet${n === 1 ? "" : "s"}</button>` : "";
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
    ${fundButton(p.rows, p.collection.chain_id, "address") ? `<div class="row" style="margin-top:10px">${fundButton(p.rows, p.collection.chain_id, "address")}<span class="tiny muted">Opens Distribute with each short wallet and the exact amount it is missing (+2%).</span></div>` : ""}
    ${p.token ? `<div class="row" style="margin-top:14px">
      <label>Type MINT to send ${t.ready} transaction${t.ready === 1 ? "" : "s"}<input id="m-confirm" autocomplete="off" style="width:140px"></label>
      <button class="primary" data-action="start">Start mint</button>
      <span class="tiny muted">Preview valid for ${countdown(p.created + p.expires_in)}. Only READY wallets are included.</span></div>` : ""}
  </div>`;
}
async function renderJobs() {
  const el = $("#jobs"); if (!el) return;
  const d = await api("/api/mint/jobs");
  if (S.page === "overview") {   // command center: just the latest operations, inline
    el.innerHTML = d.jobs.slice(0, 2).map(jobCard).join("");
    return;
  }
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
    <tr><th>Time</th><th>Wallet</th><th>Operation</th><th>Details</th><th>Status</th><th>Transaction</th><th class="num">Value</th><th class="num">Gas</th><th>Error</th></tr>
    ${rows.map((r) => `<tr><td class="tiny">${esc(fmtTime(r.created_at))}</td><td>${addr(r.address)}</td><td>${esc(r.operation.replace("_", " "))}${r.job_id ? ` #${r.job_id}` : ""}</td>
      <td>${r.operation === "mint" ? `${esc(r.slug || "")}<div class="tiny muted">${esc(r.stage_name || "")}</div>` : `${esc(r.amount || "")} ${esc(assetName(r.asset, r.chain_id))}${r.token_id ? ` #${esc(r.token_id)}` : ""}<div class="tiny muted">→ ${esc(short(r.recipient))}</div>`}</td><td>${badge(r.status)}</td><td>${txLink(r.chain_id, r.tx_hash)}</td>
      <td class="num">${r.value_wei ? esc(weiToStr(r.value_wei)) : "—"}</td><td class="num">${r.gas_cost_wei ? esc(weiToStr(r.gas_cost_wei)) : "—"}</td>
      <td class="wrap tiny">${esc(r.error || "")}${["submitted", "pending"].includes(r.status) && r.tx_hash && r.nonce !== null ? `
        <div class="row" style="margin-top:4px"><button class="small" data-action="replace" data-id="${r.id}" data-mode="speedup">Speed up</button>
        <button class="small danger" data-action="replace" data-id="${r.id}" data-mode="cancel">Cancel</button></div>` : ""}</td></tr>`).join("")}</table></div>`;
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
      <form class="panel" id="monitor-form"><h2>Collection monitor</h2>
        <p class="tiny muted">Watched collections are re-checked on this schedule, every 10 s in the last 10 minutes before a phase, and right when it opens. The monitor never sends transactions.</p>
        <label>Check every (seconds, 20-3600)<input name="interval_seconds" type="number" value="${esc(d.monitor.interval_seconds)}"></label>
        <label style="display:flex;gap:8px;align-items:center"><input name="auto_rescan" type="checkbox" ${d.monitor.auto_rescan ? "checked" : ""} style="width:auto"> Re-check eligibility automatically when a phase opens</label>
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save</button>
          <button type="button" data-action="notify-on">${localStorageGet("nftops.notify") === "1" ? "Desktop alerts: on" : "Turn on desktop alerts"}</button>
          <button type="button" data-action="sound-toggle">${localStorageGet("nftops.sound") === "1" ? "Sound: on" : "Sound: off"}</button></div></form>
      <form class="panel" id="elig-form"><h2>Eligibility scan pacing</h2>
        <p class="tiny muted">Lower values are gentler on OpenSea and avoid rate limits.</p>
        <label>Wallets checked at the same time (1-10)<input name="concurrency" type="number" value="${esc(d.eligibility.concurrency)}"></label>
        <label>Pause between sign-ins (seconds)<input name="spacing_seconds" value="${esc(d.eligibility.spacing_seconds)}"></label>
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save</button></div></form>
      <form class="panel" id="security-form"><h2>Security &amp; backup</h2>
        <label>Lock automatically after (minutes without activity, 0 = never)<input name="auto_lock_minutes" type="number" min="0" max="1440" value="${esc(d.security.auto_lock_minutes)}"></label>
        <p class="tiny muted">It never locks while a mint, transfer or scan is running.</p>
        <label style="display:flex;gap:8px;align-items:center"><input name="broadcast_all" type="checkbox" ${d.network.broadcast_all ? "checked" : ""} style="width:auto"> Send each transaction through every healthy RPC at once (faster; never creates duplicates)</label>
        <div class="row" style="margin-top:10px"><button class="primary" type="submit">Save</button><span class="spacer"></span><button type="button" data-action="backup">Download backup</button></div>
        <p class="tiny muted">A backup is a zip with your encrypted vault and your database. Restore it with <span class="mono">uv run --frozen nft-ops --restore file.zip</span>; it needs the same password.</p></form>
      <form class="panel" id="pw-form"><h2>Vault</h2>
        <label>Current password<input name="old" type="password" autocomplete="current-password"></label>
        <label>New password (10+ characters)<input name="new" type="password" autocomplete="new-password"></label>
        <div class="row" style="margin-top:10px"><button type="submit">Change password</button><span class="spacer"></span><button type="button" class="danger" data-action="lock">Lock now</button></div></form>
    </div>`;
  const formData = (f) => Object.fromEntries(new FormData(f).entries());
  $("#limits-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { limits: formData(e.target) }); toast("Limits saved"); }); };
  $("#mint-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { mint: formData(e.target) }); S.mint.form = null; toast("Mint defaults saved"); }); };
  $("#monitor-form").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    await api("/api/monitor/settings", { interval_seconds: Number(e.target.interval_seconds.value), auto_rescan: e.target.auto_rescan.checked }); toast("Monitor settings saved"); }); };
  $("#elig-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/settings", { eligibility: formData(e.target) }); toast("Saved"); }); };
  $("#security-form").onsubmit = (e) => { e.preventDefault(); guard(async () => {
    await api("/api/settings", { security: { auto_lock_minutes: Number(e.target.auto_lock_minutes.value) }, network: { broadcast_all: e.target.broadcast_all.checked } }); toast("Saved"); }); };
  $("#pw-form").onsubmit = (e) => { e.preventDefault(); guard(async () => { await api("/api/vault/password", formData(e.target)); e.target.reset(); toast("Password changed"); }); };
}

const ACTIONS_EXTRA = {};
// ---------------------------------------------------------------- theme
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = $("#theme-toggle"); if (b) b.textContent = t === "dark" ? "☀" : "☾";
}
applyTheme(localStorageGet("nftops.theme") || "dark");
$("#theme-toggle").addEventListener("click", () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorageSet("nftops.theme", t); applyTheme(t);
});
document.addEventListener("click", (e) => { const l = e.target.closest("[data-page-link]"); if (l) go(l.dataset.pageLink); });


// ---------------------------------------------------------------- alerts
function setBell(n) { const c = $("#bell-count"); c.textContent = n; c.classList.toggle("hidden", !n); }
function alertsHTML(rows) {
  return rows.map((a) => `<div class="alert-row ${a.read ? "" : "unread"}"><span class="dot ${esc(a.level)}"></span>
    <div><div class="msg">${esc(a.message)}</div><div class="tiny muted">${esc(fmtTime(a.created_at))}</div></div></div>`).join("");
}
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.18);
  } catch { /* no audio */ }
}
function onAlert(ev) {
  setBell(Number($("#bell-count").textContent || 0) + 1);
  toast(ev.message, ev.level === "warn");
  if (localStorageGet("nftops.sound") === "1") beep();
  if (localStorageGet("nftops.notify") === "1" && "Notification" in window && Notification.permission === "granted") {
    try { new Notification("NFT Ops", { body: ev.message, icon: "/static/icon.svg" }); } catch { /* ignore */ }
  }
  if (S.page === "overview") scheduleRefresh();
}
$("#bell").addEventListener("click", () => ACTIONS_EXTRA.alerts());

// ---------------------------------------------------------------- transfers
function assetName(asset, chainId) {
  if (!asset) return "";
  if (asset === "native") return symbol(chainId);
  if (asset.startsWith("erc721:") || asset.startsWith("erc1155:")) return `NFT ${short(asset.split(":")[1])}`;
  const t = (S.tokens || []).find((x) => x.address === asset);
  return t ? t.symbol : short(asset);
}
async function renderTransfers() {
  const t = S.tx;
  const [w, book] = await Promise.all([api(`/api/wallets?chain_id=${S.chainId}`), api("/api/addressbook")]);
  S.tokens = w.tokens; S.book = book; S.walletData = w;
  const tabs = [["nfts", "NFTs"], ["sweep", "Sweep funds"], ["distribute", "Distribute funds"], ["book", "Address book"]];
  $("#main").innerHTML = `
    <div class="page-head"><h1>Transfers</h1><label>Network ${chainSelect("t-chain", S.chainId)}</label></div>
    <div class="chips">${tabs.map(([k, l]) => `<button class="${t.tab === k ? "on" : ""}" data-action="tx-tab" data-tab="${k}">${esc(l)}</button>`).join("")}</div>
    <div id="tx-body"></div><div id="tx-preview">${t.preview ? txPreviewHTML(t.preview) : ""}</div><div id="txjobs"></div>`;
  $("#t-chain").onchange = (e) => { S.chainId = Number(e.target.value); localStorageSet("nftops.chain", S.chainId); t.preview = null; t.nft.data = null; render(); };
  await ({ nfts: tabNfts, sweep: tabSweep, distribute: tabDistribute, book: tabBook })[t.tab](w, book);
  await renderTxJobs();
}
function destinationOptions(w, book, value) {
  const own = w.wallets.map((x) => ({ v: x.address, l: `${x.label || short(x.address)} (your wallet)` }));
  const ext = book.entries.map((x) => ({ v: x.address, l: `${x.label || short(x.address)} (address book)` }));
  return options([...own, ...ext], value, (o) => o.l, (o) => o.v);
}
function sourceControls(prefix, cfg, w) {
  return `<label>From wallets <select id="${prefix}-source">
      <option value="group" ${cfg.source === "group" ? "selected" : ""}>A group</option>
      <option value="all" ${cfg.source === "all" ? "selected" : ""}>All wallets</option>
      <option value="selected" ${cfg.source === "selected" ? "selected" : ""}>Selected on Wallets page (${S.selected.size})</option></select></label>
    ${cfg.source === "group" ? `<label>Group <select id="${prefix}-group">${options(w.groups, cfg.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>` : ""}`;
}
function sourceSelection(cfg) {
  if (cfg.source === "group") return { group: cfg.group };
  if (cfg.source === "selected") return { addresses: [...S.selected] };
  return {};
}
function assetSelect(id, value, w) {
  return `<select id="${id}"><option value="native">${esc(symbol(S.chainId))} (native)</option>${options(w.tokens, value, (x) => x.symbol, (x) => x.address)}</select>`;
}

async function tabNfts(w, book) {
  const n = S.tx.nft;
  const cols = (await api("/api/collections")).collections.filter((c) => c.chain_id === S.chainId);
  const d = n.data;
  $("#tx-body").innerHTML = `<div class="panel">
    <div class="row">
      <label>Collection <select id="n-col"><option value="">— choose —</option>${options(cols, n.contract, (c) => c.slug, (c) => c.address)}</select></label>
      <label style="flex:1 1 300px">or contract address<input id="n-contract" class="mono" value="${esc(n.contract)}" placeholder="0x…"></label>
      <button class="primary" data-action="nft-load">Load</button>
    </div>
    ${d ? `<div class="row" style="margin-top:10px">
      <span class="badge info">${esc(d.standard.toUpperCase())}</span><span class="muted">${d.holdings.length} NFT${d.holdings.length === 1 ? "" : "s"} known in your wallets</span><span class="spacer"></span>
      <label>Look-back blocks (0 = skip history scan)<input id="n-lookback" type="number" min="0" value="0" style="width:130px"></label>
      <button data-action="nft-discover">Find my NFTs</button><button data-action="nft-manual">Add token IDs</button></div>
      <p class="tiny muted">“Find my NFTs” checks your mint records and the contract's own token list, and with a look-back it also scans transfer history. Every NFT is checked on-chain before it's shown.</p>` : ""}
  </div>
  ${d ? `<div class="panel">
    ${d.holdings.length ? `<div class="table-wrap"><table>
      <tr><th><input type="checkbox" id="n-all" ${d.holdings.length && d.holdings.every((h) => n.picked.has(nftKey(h))) ? "checked" : ""}></th><th>Wallet</th><th>Token ID</th><th class="num">Held</th>${d.standard === "erc1155" ? `<th class="num">Send</th>` : ""}<th>Found by</th></tr>
      ${d.holdings.map((h) => `<tr><td><input type="checkbox" data-nft="${esc(nftKey(h))}" ${n.picked.has(nftKey(h)) ? "checked" : ""}></td>
        <td>${addr(h.owner, h.label)}</td><td class="mono">${esc(h.token_id)}</td><td class="num">${esc(h.amount)}</td>
        ${d.standard === "erc1155" ? `<td class="num"><input data-amount="${esc(nftKey(h))}" type="number" min="1" max="${esc(h.amount)}" value="${esc(h.amount)}" style="width:80px"></td>` : ""}
        <td class="tiny">${esc(h.source)}</td></tr>`).join("")}</table></div>` : `<div class="empty">No NFTs found yet. Use “Find my NFTs”.</div>`}
    <div class="row" style="margin-top:12px">
      <label>Mode <select id="n-mode"><option value="consolidate" ${n.mode === "consolidate" ? "selected" : ""}>Consolidate — all into one wallet</option>
        <option value="distribute" ${n.mode === "distribute" ? "selected" : ""}>Distribute — spread to recipients</option></select></label>
      ${n.mode === "consolidate" ? `<label>Send to <select id="n-dest">${destinationOptions(w, book, n.dest)}</select></label>`
        : `<label>Saved list <select id="n-list"><option value="">— type below —</option>${options(book.lists, "", (l) => `${l.name} (${l.rows.length})`, (l) => l.name)}</select></label>`}
      <button class="primary" data-action="nft-preview">Preview ${n.picked.size} NFT${n.picked.size === 1 ? "" : "s"}</button>
    </div>
    ${n.mode === "distribute" ? `<label style="margin-top:8px">Recipients — one address per line (NFTs are dealt out in order). Or CSV with <span class="mono">address,token_id</span> to give each NFT its own recipient.<textarea id="n-recipients" rows="5" spellcheck="false">${esc(n.recipients || "")}</textarea></label>
      <div class="row" style="margin-top:8px"><button class="primary" data-action="pick-recipients" data-target="n-recipients">+ Add from my wallets</button></div>` : ""}
  </div>` : ""}`;
  $("#n-col").onchange = (e) => { $("#n-contract").value = e.target.value; };
  if ($("#n-all")) $("#n-all").onchange = (e) => { d.holdings.forEach((h) => (e.target.checked ? n.picked.add(nftKey(h)) : n.picked.delete(nftKey(h)))); render(); };
  $$("[data-nft]").forEach((cb) => (cb.onchange = (e) => { e.target.checked ? n.picked.add(cb.dataset.nft) : n.picked.delete(cb.dataset.nft); readNftForm(); render(); }));
  if ($("#n-mode")) $("#n-mode").onchange = (e) => { readNftForm(); n.mode = e.target.value; render(); };
}
const nftKey = (h) => `${h.owner}:${h.token_id}`;
function readNftForm() {
  const n = S.tx.nft;
  if ($("#n-dest")) n.dest = $("#n-dest").value;
  if ($("#n-recipients")) n.recipients = $("#n-recipients").value;
  n.amounts = n.amounts || {};
  $$("[data-amount]").forEach((i) => { n.amounts[i.dataset.amount] = i.value; });
}

async function tabSweep(w, book) {
  const c = S.tx.sweep;
  if (c.source === "group" && !c.group && w.groups.length) c.group = w.groups[0].name;
  $("#tx-body").innerHTML = `<div class="panel">
    <p class="muted">Collect funds from many wallets into one. Destinations must be your own wallet or in the address book.</p>
    <div class="row">
      <label>Asset ${assetSelect("s-asset", c.asset, w)}</label>
      ${sourceControls("s", c, w)}
      <label>Send to <select id="s-dest">${destinationOptions(w, book, c.dest)}</select></label>
    </div>
    <div class="row" style="margin-top:10px">
      <label>Leave in each wallet (${esc(symbol(S.chainId))} gas reserve)<input id="s-reserve" value="${esc(c.reserve)}" style="width:120px"></label>
      <label>Skip wallets below<input id="s-min" value="${esc(c.min_balance)}" style="width:110px"></label>
      <label>Max moved this time (${esc(symbol(S.chainId))}, optional)<input id="s-cap" value="${esc(c.cap || "")}" style="width:130px" placeholder="from Settings"></label>
      <button class="primary" data-action="sweep-preview">Preview sweep</button>
    </div>
    <p class="tiny muted">Native sweeps send everything above the reserve, minus this transfer's gas. Token sweeps send each wallet's full token balance (gas is paid in ${esc(symbol(S.chainId))}).</p>
  </div>`;
  $("#s-source").onchange = (e) => { readSweep(); c.source = e.target.value; render(); };
}
function readSweep() {
  const c = S.tx.sweep, v = (id) => ($(id) ? $(id).value : undefined);
  c.asset = v("#s-asset") ?? c.asset; c.dest = v("#s-dest") ?? c.dest; c.group = v("#s-group") ?? c.group;
  c.reserve = v("#s-reserve") ?? c.reserve; c.min_balance = v("#s-min") ?? c.min_balance; c.cap = v("#s-cap") ?? c.cap;
}

async function tabDistribute(w, book) {
  const c = S.tx.dist;
  const keyed = w.wallets.filter((x) => x.has_key);
  const modes = [["equal_each", "Same amount to each"], ["equal_split", "Split a total equally"], ["percent", "Split a total by percent"], ["custom", "Custom amount each"], ["pairs", "Many-to-many (from a list)"]];
  const help = {
    equal_each: "One address per line.", equal_split: "One address per line.",
    percent: "CSV: address,percent — percentages must add up to 100.", custom: "CSV: address,amount",
    pairs: "CSV: source,address,amount — source must be one of your wallets.",
  }[c.mode];
  $("#tx-body").innerHTML = `<div class="panel">
    <div class="row">
      <label>Asset ${assetSelect("d-asset", c.asset, w)}</label>
      <label>How <select id="d-mode">${options(modes, c.mode, (m) => m[1], (m) => m[0])}</select></label>
      ${c.mode !== "pairs" ? `<label>Paid from <select id="d-source">${options(keyed, c.source, (x) => `${x.label || short(x.address)} — ${x.native ?? "?"} ${symbol(S.chainId)}`, (x) => x.address)}</select></label>` : ""}
      ${c.mode === "equal_each" ? `<label>Amount each<input id="d-each" value="${esc(c.each || "")}" style="width:120px"></label>` : ""}
      ${["equal_split", "percent"].includes(c.mode) ? `<label>Total<input id="d-total" value="${esc(c.total || "")}" style="width:120px"></label>` : ""}
    </div>
    <div class="row" style="margin-top:10px">
      <label>Saved list <select id="d-list"><option value="">— type below —</option>${options(book.lists, c.list || "", (l) => `${l.name} (${l.rows.length})`, (l) => l.name)}</select></label>
      <input type="file" id="d-file" accept=".csv,text/csv">
      <label>Max ${esc(symbol(S.chainId))} moved (optional)<input id="d-cap" value="${esc(c.cap || "")}" style="width:120px" placeholder="from Settings"></label>
    </div>
    <label style="margin-top:8px">Recipients — ${esc(help)}<textarea id="d-text" rows="7" spellcheck="false">${esc(c.text || "")}</textarea></label>
    <div class="row" style="margin-top:10px"><button class="primary" data-action="pick-recipients" data-target="d-text">+ Add from my wallets</button><button data-action="dist-check">Check list</button><button data-action="dist-save">Save list…</button>
      <span class="spacer"></span><button class="primary" data-action="dist-preview">Preview distribution</button></div>
    <div id="d-result"></div>
  </div>`;
  $("#d-mode").onchange = (e) => { readDist(); c.mode = e.target.value; render(); };
}
function readDist() {
  const c = S.tx.dist, v = (id) => ($(id) ? $(id).value : undefined);
  c.asset = v("#d-asset") ?? c.asset; c.source = v("#d-source") ?? c.source; c.each = v("#d-each") ?? c.each;
  c.total = v("#d-total") ?? c.total; c.list = v("#d-list") ?? c.list; c.text = v("#d-text") ?? c.text; c.cap = v("#d-cap") ?? c.cap;
}

async function tabBook(w, book) {
  $("#tx-body").innerHTML = `<div class="two">
    <div class="panel"><h2>Address book</h2>
      <p class="tiny muted">Approved destinations outside your own wallets (cold storage, exchange deposit, partner). Sweeps and NFT consolidation can only send to your wallets or these.</p>
      <div class="row"><label style="flex:1 1 260px">Address<input id="b-addr" class="mono"></label><label>Label<input id="b-label"></label><button class="primary" data-action="book-add">Add</button></div>
      ${book.entries.length ? `<div class="table-wrap" style="margin-top:10px"><table><tr><th>Label</th><th>Address</th><th></th></tr>
        ${book.entries.map((e) => `<tr><td>${esc(e.label)}</td><td>${addr(e.address)}</td><td><button class="small danger" data-action="book-remove" data-address="${esc(e.address)}">Remove</button></td></tr>`).join("")}</table></div>` : `<div class="empty">No entries.</div>`}
    </div>
    <div class="panel"><h2>Saved recipient lists</h2>
      <p class="tiny muted">Create lists on the Distribute tab (“Save list…”).</p>
      ${book.lists.length ? `<div class="table-wrap"><table><tr><th>Name</th><th class="num">Recipients</th><th>Updated</th><th></th></tr>
        ${book.lists.map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${l.rows.length}</td><td class="tiny">${esc(fmtTime(l.updated_at))}</td><td><button class="small danger" data-action="list-delete" data-name="${esc(l.name)}">Delete</button></td></tr>`).join("")}</table></div>` : `<div class="empty">No saved lists.</div>`}
    </div></div>`;
}

function txPreviewHTML(p) {
  const t = p.totals, sym = symbol(p.chain_id);
  return `<div class="panel">
    <div class="row"><h2 style="margin:0">Preview — ${esc(p.title)}</h2><span class="spacer"></span><span class="muted">max fee ${esc(p.fees.max_fee_gwei)} gwei</span></div>
    <div class="grid cards" style="margin:12px 0">
      <div class="card"><div class="muted">Ready</div><div class="big">${t.ready} / ${t.total}</div><div class="tiny muted">${t.wallets} sending wallet${t.wallets === 1 ? "" : "s"}</div></div>
      <div class="card"><div class="muted">${p.kind === "custom_mint" ? "Mint total" : `${esc(sym)} moved`}</div><div class="big">${esc(t.native_moved)}</div><div class="tiny muted">limit ${esc(t.move_cap)}${p.scheduled_at ? ` · sends in ${countdown(p.scheduled_at)}` : ""}</div></div>
      <div class="card"><div class="muted">Max gas</div><div class="big">${esc(t.max_gas)}</div><div class="tiny muted">${esc(sym)}, usually much less</div></div>
      <div class="card"><div class="muted">Leaves your wallets</div><div class="big">${esc(t.external_spend)}</div><div class="tiny muted">${p.kind === "custom_mint" ? "paid to the NFT contract" : `${t.external_recipients} outside recipient${t.external_recipients === 1 ? "" : "s"}`} · 24h ${esc(t.daily_used)}/${esc(t.daily_limit)}</div></div>
    </div>
    ${p.blocks.map((b) => `<div class="notice bad">${esc(b)}</div>`).join("")}
    ${p.notes.map((n) => `<div class="notice info">${esc(n)}</div>`).join("")}
    <div class="table-wrap"><table>
      <tr><th>From</th><th>${p.kind === "custom_mint" ? "NFTs go to" : "To"}</th><th>Asset</th><th class="num">Amount</th><th class="num">Max gas</th><th>Status</th><th>Reason</th></tr>
      ${p.rows.map((r) => `<tr><td>${addr(r.source)}</td><td>${addr(r.recipient)}${r.external && p.kind !== "custom_mint" ? ` <span class="badge warn">outside</span>` : ""}</td>
        <td>${esc(r.asset)}</td><td class="num">${esc(r.amount)}</td><td class="num">${esc(r.max_gas_cost)}</td><td>${badge(r.status)}</td><td class="wrap">${esc(r.reason)}</td></tr>`).join("")}
    </table></div>
    ${fundButton(p.rows, p.chain_id, "source") ? `<div class="row" style="margin-top:10px">${fundButton(p.rows, p.chain_id, "source")}<span class="tiny muted">Opens Distribute with each short wallet and the exact amount it is missing (+2%).</span></div>` : ""}
    ${p.token ? `<div class="row" style="margin-top:14px">
      <label>Check every row above, then type ${esc(p.confirm_word)}<input id="tx-confirm" autocomplete="off" style="width:140px"></label>
      <button class="primary" data-action="tx-start">Send ${t.ready} transaction${t.ready === 1 ? "" : "s"}</button>
      <span class="tiny muted">Preview valid for ${countdown(p.created + p.expires_in)}. Only READY rows are sent, exactly as shown.</span></div>` : ""}
  </div>`;
}
async function renderTxJobs() {
  const el = $("#txjobs"); if (!el) return;
  const d = await api("/api/tx/jobs");
  el.innerHTML = `<div class="panel"><h2>Transfer operations</h2>${d.jobs.length ? d.jobs.map(txJobCard).join("") : `<div class="empty">No transfer operations yet.</div>`}</div>`;
}
function txJobCard(j) {
  const done = j.runs.filter((r) => r.phase === "confirmed").length;
  return `<div class="panel" style="padding:12px">
    <div class="row"><b>#${j.id} ${esc(j.title)}</b> ${badge(j.status)}<span class="muted">${done}/${j.runs.length} confirmed</span></div>
    <div class="table-wrap" style="margin-top:8px"><table>
      <tr><th>From</th><th>To</th><th>Asset</th><th class="num">Amount</th><th>Status</th><th>Message</th><th>Transaction</th></tr>
      ${j.runs.map((r) => `<tr><td>${addr(r.source)}</td><td>${addr(r.recipient)}</td><td>${esc(r.asset)}</td><td class="num">${esc(r.amount)}</td>
        <td>${badge(r.phase)}</td><td class="wrap">${esc(r.message)}</td><td>${txLink(j.chain_id, r.tx_hash)}</td></tr>`).join("")}
    </table></div></div>`;
}
async function showTxPreview(promise) {
  const p = await promise;
  p.created = Date.now() / 1000; S.tx.preview = p;
  $("#tx-preview").innerHTML = txPreviewHTML(p);
  $("#tx-preview").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function withBusy(el, label, fn) {
  el.disabled = true; const old = el.textContent; el.textContent = label;
  try { await fn(); } finally { el.disabled = false; el.textContent = old; }
}
Object.assign(ACTIONS_EXTRA, {
  "tx-tab": (el) => { S.tx.tab = el.dataset.tab; S.tx.preview = null; render(); },
  "nft-load": (el) => guard(() => withBusy(el, "Loading…", async () => {
    const n = S.tx.nft; n.contract = $("#n-contract").value.trim(); n.picked.clear();
    n.data = await api(`/api/nfts?chain_id=${S.chainId}&contract=${encodeURIComponent(n.contract)}`); render();
  })),
  "nft-discover": (el) => guard(() => withBusy(el, "Searching…", async () => {
    const n = S.tx.nft;
    const r = await api("/api/nfts/discover", { chain_id: S.chainId, contract: n.contract, lookback_blocks: Number($("#n-lookback").value || 0) });
    toast(`Found ${r.found} NFT${r.found === 1 ? "" : "s"}${r.notes.length ? ` — ${r.notes[0]}` : ""}`);
    n.data = await api(`/api/nfts?chain_id=${S.chainId}&contract=${encodeURIComponent(n.contract)}`); render();
  })),
  "nft-manual": () => modal(`<h2>Add token IDs</h2><p class="tiny muted">Token IDs separated by spaces, commas or new lines. Each is checked on-chain; only ones held by your wallets are added.</p>
    <textarea id="n-ids" rows="5"></textarea><div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="nft-manual-go">Add</button></div>`),
  "nft-manual-go": () => guard(async () => {
    const n = S.tx.nft;
    const r = await api("/api/nfts/manual", { chain_id: S.chainId, contract: n.contract, ids: $("#n-ids").value });
    closeModal(); toast(`Added ${r.added}${r.not_yours.length ? `; not in your wallets: ${r.not_yours.slice(0, 5).join(", ")}` : ""}`);
    n.data = await api(`/api/nfts?chain_id=${S.chainId}&contract=${encodeURIComponent(n.contract)}`); render();
  }),
  "nft-preview": (el) => guard(() => withBusy(el, "Checking…", async () => {
    readNftForm(); const n = S.tx.nft;
    const holdings = n.data.holdings.filter((h) => n.picked.has(nftKey(h)));
    if (!holdings.length) throw new ApiError("Tick the NFTs to send first.");
    let selection = holdings.map((h) => ({ owner: h.owner, token_id: h.token_id, amount: Number((n.amounts || {})[nftKey(h)] || h.amount) }));
    const body = { chain_id: S.chainId, contract: n.contract, mode: n.mode, destination: n.dest };
    if (n.mode === "distribute") {
      const listName = $("#n-list") ? $("#n-list").value : "";
      const text = n.recipients || "";
      if (!listName && /token_id/i.test(text.split("\n")[0])) {
        const parsed = await api("/api/addressbook", { action: "parse_list", text });
        if (!parsed.ok) throw new ApiError(parsed.problems.join("; "));
        const byId = Object.fromEntries(parsed.rows.filter((r) => r.token_id).map((r) => [r.token_id, r.address]));
        selection = selection.map((s) => ({ ...s, recipient: byId[String(s.token_id)] })).filter((s) => s.recipient);
        body.text = parsed.rows.map((r) => r.address).join("\n");
      } else { body.list = listName; body.text = text; }
    }
    await showTxPreview(api("/api/nfts/preview", { ...body, selection }));
  })),
  "sweep-preview": (el) => guard(() => withBusy(el, "Checking…", async () => {
    readSweep(); const c = S.tx.sweep;
    await showTxPreview(api("/api/sweep/preview", { chain_id: S.chainId, ...sourceSelection(c), destination: c.dest || $("#s-dest").value, asset: c.asset, reserve: c.reserve, min_balance: c.min_balance, cap: c.cap }));
  })),
  "dist-check": () => guard(async () => {
    readDist();
    const r = await api("/api/addressbook", { action: "parse_list", text: S.tx.dist.text || "" });
    $("#d-result").innerHTML = r.ok ? `<div class="notice info">${r.rows.length} recipients look good.</div>` : r.problems.map((p) => `<div class="notice bad">${esc(p)}</div>`).join("");
  }),
  "dist-save": () => { readDist(); modal(`<h2>Save recipient list</h2><label>Name<input id="list-name"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="dist-save-go">Save</button></div>`); },
  "dist-save-go": () => guard(async () => { await api("/api/addressbook", { action: "save_list", name: $("#list-name").value, text: S.tx.dist.text || "" }); closeModal(); toast("List saved"); render(); }),
  "dist-preview": (el) => guard(() => withBusy(el, "Checking…", async () => {
    readDist(); const c = S.tx.dist;
    await showTxPreview(api("/api/distribute/preview", { chain_id: S.chainId, mode: c.mode, asset: c.asset, source: c.source || ($("#d-source") && $("#d-source").value), each: c.each, total: c.total, list: c.list, text: c.text, cap: c.cap }));
  })),
  "book-add": () => guard(async () => { await api("/api/addressbook", { action: "add", address: $("#b-addr").value, label: $("#b-label").value }); toast("Added"); render(); }),
  "book-remove": (el) => guard(async () => { if (!confirm("Remove this address from the address book?")) return; await api("/api/addressbook", { action: "remove", address: el.dataset.address }); render(); }),
  "list-delete": (el) => guard(async () => { if (!confirm(`Delete list “${el.dataset.name}”?`)) return; await api("/api/addressbook", { action: "delete_list", name: el.dataset.name }); render(); }),
  "tx-start": (el) => guard(() => withBusy(el, "Sending…", async () => {
    const r = await api("/api/tx/start", { token: S.tx.preview.token, confirm: $("#tx-confirm").value });
    S.tx.preview = null; ["#tx-preview", "#c-preview"].forEach((id) => { if ($(id)) $(id).innerHTML = ""; });
    toast(`Operation #${r.job_id} started`); renderTxJobs();
  })),
});

// ---------------------------------------------------------------- recipient picker
// Adds addresses from your own wallets / groups / address book to a recipient box.
const PICK = { target: "", group: "", q: "", chosen: new Set() };
function pickerRows() {
  const w = S.walletData ? S.walletData.wallets : [];
  const book = (S.book && S.book.entries) || [];
  const rows = [...w.map((x) => ({ address: x.address, label: x.label, groups: x.groups, kind: "wallet" })),
    ...book.filter((b) => !w.some((x) => x.address === b.address)).map((b) => ({ address: b.address, label: b.label, groups: [], kind: "book" }))];
  const q = PICK.q.toLowerCase();
  return rows.filter((r) => (!PICK.group || (PICK.group === "__book" ? r.kind === "book" : r.groups.includes(PICK.group)))
    && (!q || r.address.toLowerCase().includes(q) || (r.label || "").toLowerCase().includes(q)));
}
function pickerHTML() {
  const rows = pickerRows();
  const groups = (S.walletData && S.walletData.groups) || [];
  return `<h2>Add recipients</h2>
    <div class="row"><label>Show <select id="pk-group"><option value="">All my wallets + address book</option>
      ${options(groups, PICK.group, (g) => `Group: ${g.name} (${g.size})`, (g) => g.name)}<option value="__book" ${PICK.group === "__book" ? "selected" : ""}>Address book only</option></select></label>
      <label style="flex:1 1 160px">Search<input id="pk-q" value="${esc(PICK.q)}" placeholder="label or address"></label></div>
    <div class="row"><button class="small" data-action="pick-all">Select all shown (${rows.length})</button><button class="small" data-action="pick-none">Clear</button><span class="spacer"></span><span class="muted tiny">${PICK.chosen.size} chosen</span></div>
    <div class="table-wrap" style="max-height:340px;overflow:auto"><table>
      ${rows.map((r) => `<tr><td><input type="checkbox" data-pick="${esc(r.address)}" ${PICK.chosen.has(r.address) ? "checked" : ""}></td>
        <td>${esc(r.label || "")}<div class="mono tiny muted">${esc(r.address)}</div></td>
        <td>${r.kind === "book" ? `<span class="badge">address book</span>` : r.groups.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</td></tr>`).join("") || `<tr><td class="empty">Nothing matches.</td></tr>`}
    </table></div>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="pick-add">Add ${PICK.chosen.size}</button></div>`;
}
function openPicker() {
  $("#modal-card").innerHTML = pickerHTML(); $("#modal").classList.remove("hidden");
  $("#pk-group").onchange = (e) => { PICK.group = e.target.value; openPicker(); };
  $("#pk-q").oninput = (e) => { PICK.q = e.target.value; clearTimeout(PICK.t); PICK.t = setTimeout(() => { openPicker(); const i = $("#pk-q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 250); };
  $$("[data-pick]").forEach((cb) => (cb.onchange = (e) => { e.target.checked ? PICK.chosen.add(cb.dataset.pick) : PICK.chosen.delete(cb.dataset.pick); $("[data-action=pick-add]").textContent = `Add ${PICK.chosen.size}`; }));
}
Object.assign(ACTIONS_EXTRA, {
  "pick-recipients": (el) => { PICK.target = el.dataset.target; PICK.chosen = new Set(); PICK.q = ""; openPicker(); },
  "pick-all": () => { pickerRows().forEach((r) => PICK.chosen.add(r.address)); openPicker(); },
  "pick-none": () => { PICK.chosen.clear(); openPicker(); },
  "pick-add": () => {
    const box = $(`#${PICK.target}`); if (!box) return closeModal();
    const payer = PICK.target === "d-text" && $("#d-source") ? $("#d-source").value.toLowerCase() : "";
    const existing = new Set(box.value.split("\n").map((l) => l.split(",")[0].trim().toLowerCase()).filter(Boolean));
    const needsAmount = PICK.target === "d-text" && ["custom", "percent"].includes(S.tx.dist.mode);
    const add = [...PICK.chosen].filter((a) => a.toLowerCase() !== payer && !existing.has(a.toLowerCase()));
    const header = needsAmount && !/address/i.test(box.value.split("\n")[0] || "") ? `address,${S.tx.dist.mode === "percent" ? "percent" : "amount"}\n` : "";
    const lines = add.map((a) => (needsAmount ? `${a},` : a));
    box.value = (box.value.trim() ? `${box.value.trim()}\n` : header) + lines.join("\n");
    if (PICK.target === "d-text") S.tx.dist.text = box.value; else S.tx.nft.recipients = box.value;
    closeModal();
    const skipped = PICK.chosen.size - add.length;
    toast(`Added ${add.length} recipient${add.length === 1 ? "" : "s"}${skipped ? ` (${skipped} skipped: payer or already listed)` : ""}${needsAmount ? " — now type each amount after the comma" : ""}`);
  },
});

// ---------------------------------------------------------------- contracts
S.c = { address: "", report: null, fn: null, args: [], form: { quantity: 1, price: "", gas_limit: "", start: "", fast: false, proofs: "", source: "all", group: "" } };
async function renderContracts() {
  const c = S.c;
  const [w, list] = await Promise.all([api(`/api/wallets?chain_id=${S.chainId}`), api("/api/contracts")]);
  S.walletData = w;
  const r = c.report;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Contracts</h1></div>
    <div class="panel">
      <p class="muted">For mints that are not OpenSea drops. Paste the NFT contract: the analyser reads it and lists how to mint, the price, supply and restrictions. Nothing is sent until you preview and type MINT.</p>
      <div class="row">
        <label>Network ${chainSelect("c-chain", S.chainId)}</label>
        <label style="flex:1 1 360px">Contract address<input id="c-address" class="mono" value="${esc(c.address)}" placeholder="0x…"></label>
        <button class="primary" data-action="c-analyze">Analyse</button>
      </div>
      <details style="margin-top:8px"><summary class="tiny">Paste the ABI (optional — for unverified contracts, gives an exact report)</summary>
        <textarea id="c-abi" rows="5" spellcheck="false" placeholder='[{"type":"function","name":"mint",...}]'></textarea></details>
      ${list.reports.length ? `<div class="chips">${list.reports.slice(0, 8).map((x) => `<button class="small" data-action="c-load" data-address="${esc(x.address)}" data-chain="${x.chain_id}">${esc(x.name || short(x.address))}</button>`).join("")}</div>` : ""}
    </div>
    ${r ? reportHTML(r) : ""}
    ${r && c.fnSpec ? mintFormHTML(r, w) : ""}
    <div id="c-preview">${S.tx.preview && S.tx.preview.kind === "custom_mint" ? txPreviewHTML(S.tx.preview) : ""}</div>
    <div id="txjobs"></div>`;
  $("#c-chain").onchange = (e) => { S.chainId = Number(e.target.value); localStorageSet("nftops.chain", S.chainId); };
  if ($("#cm-source")) $("#cm-source").onchange = (e) => { readMintCForm(); c.form.source = e.target.value; render(); };
  if ($("#cm-when")) $("#cm-when").onchange = (e) => { readMintCForm(); c.form.when = e.target.value; if (e.target.value !== "now" && !c.form.gas_limit) c.form.gas_limit = 200000; render(); };
  await renderTxJobs();
}
function reportHTML(r) {
  const supply = r.reads["totalSupply()"] ? `${r.reads["totalSupply()"]}${r.reads["maxSupply()"] || r.reads["MAX_SUPPLY()"] ? ` / ${r.reads["maxSupply()"] || r.reads["MAX_SUPPLY()"]}` : ""}` : "—";
  return `<div class="panel">
    <div class="row"><h2 style="margin:0">${esc(r.name || "Contract")} ${r.symbol ? `<span class="muted">(${esc(r.symbol)})</span>` : ""}</h2><span class="spacer"></span>
      <span class="badge ${r.confidence === "high" ? "good" : r.confidence === "medium" ? "warn" : "bad"}">${esc(r.confidence)} confidence</span></div>
    <div class="tiny muted">${addr(r.address)} · ${esc(network(r.chain_id)?.name || r.chain_id)}${r.implementation ? ` · proxy → ${esc(short(r.implementation))}` : ""}</div>
    <div class="grid cards" style="margin:12px 0">
      <div class="card"><div class="muted">Standard</div><div class="big">${esc(r.standard.toUpperCase())}</div></div>
      <div class="card"><div class="muted">Source</div><div class="big" style="font-size:15px">${esc(r.abi_source)}</div><div class="tiny muted">${r.verified ? "verified" : "not verified"}</div></div>
      <div class="card"><div class="muted">Price read from contract</div><div class="big">${r.price_wei ? `${esc(weiToStr(r.price_wei))} ${esc(symbol(r.chain_id))}` : "—"}</div></div>
      <div class="card"><div class="muted">Minted / max</div><div class="big">${esc(supply)}</div></div>
    </div>
    ${r.restrictions.map((x) => `<div class="notice warn">${esc(x)}</div>`).join("")}
    ${r.notes.map((x) => `<div class="notice info">${esc(x)}</div>`).join("")}
    <h3 style="margin-top:12px">Mint functions found</h3>
    ${r.mint_functions.length ? `<div class="table-wrap"><table><tr><th>Function</th><th>Payable</th><th>Needs</th><th>Found by</th><th></th></tr>
      ${r.mint_functions.map((f, i) => `<tr><td class="mono">${esc(f.signature)}</td><td>${esc(f.mutability)}</td><td class="wrap">${esc(f.needs || "—")}</td>
        <td class="tiny">${esc(f.source)} (${esc(f.confidence)})</td><td><button class="small ${S.c.fn === i ? "primary" : ""}" data-action="c-use" data-index="${i}">Use</button></td></tr>`).join("")}</table></div>`
      : `<div class="empty">No mint function recognised. Paste the ABI, or type the function yourself below.</div>`}
    <div class="row" style="margin-top:10px"><label style="flex:1 1 300px">Or type a function<input id="c-custom-fn" class="mono" placeholder="mint(uint256)"></label><button data-action="c-use-custom">Use</button></div>
    ${Object.keys(r.reads).length ? `<details style="margin-top:10px"><summary class="tiny">All values read from the contract (${Object.keys(r.reads).length})</summary>
      <div class="table-wrap"><table>${Object.entries(r.reads).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="mono wrap">${esc(v)}</td></tr>`).join("")}</table></div></details>` : ""}
  </div>`;
}
// Split "a,(b,c),d" on top-level commas only (struct parameters contain commas).
function splitTop(text) {
  const parts = []; let depth = 0, cur = "";
  for (const ch of text) {
    if (ch === "(") depth++; if (ch === ")") depth--;
    if (ch === "," && depth === 0) { parts.push(cur); cur = ""; } else cur += ch;
  }
  if (cur) parts.push(cur);
  return parts;
}
function tupleDefault(t, n) {
  const parts = splitTop(t.slice(1, t.lastIndexOf(")")));
  const val = (p) => p === "bytes32[]" ? "{proof}" : p.endsWith("]") ? [] : p.startsWith("uint") || p.startsWith("int") ? (/price/.test(n) ? "{price_wei}" : 0)
    : p === "address" ? (/currency|token/.test(n) ? "{native}" : "0x0000000000000000000000000000000000000000") : p === "bool" ? false : p.startsWith("(") ? JSON.parse(tupleDefault(p, "")) : "0x";
  return JSON.stringify(parts.map(val));
}
function guessArg(input, index, types) {
  const n = (input.name || "").toLowerCase(), t = input.type;
  if (t.startsWith("(")) return tupleDefault(t, n);
  if (t === "bytes") return "0x";
  if (t === "address" && /currency/.test(n)) return "{native}";
  if (t.startsWith("uint") && /price/.test(n)) return "{price_wei}";
  if (t === "bytes32[]") return "{proof}";
  if (t === "address" && (/to|recipient|minter|receiver|account|wallet/.test(n) || !n)) return "{wallet}";
  if (t.startsWith("uint") && (/qty|quantity|amount|count|num|_n$|^n$/.test(n) || types.filter((x) => x.startsWith("uint")).length === 1)) return "{quantity}";
  return "";
}
function mintFormHTML(r, w) {
  const c = S.c, f = c.fnSpec, form = c.form;
  if (!c.args.length && f.inputs.length) c.args = f.inputs.map((i, k) => guessArg(i, k, f.inputs.map((x) => x.type)));
  const usesProof = c.args.includes("{proof}");
  if (!form.price && r.price_wei) form.price = weiToStr(r.price_wei);
  if (form.source === "group" && !form.group && w.groups.length) form.group = w.groups[0].name;
  return `<div class="panel"><h2>Custom mint — <span class="mono">${esc(f.signature)}</span></h2>
    ${f.needs && f.needs.includes("signature") ? `<div class="notice bad">This function needs a signature issued by the project for each wallet. That is not supported.</div>` : ""}
    <p class="tiny muted">Fill each input. Placeholders: <span class="mono">{quantity}</span> = quantity below, <span class="mono">{wallet}</span> = each minting wallet's own address, <span class="mono">{proof}</span> = that wallet's allowlist proof, <span class="mono">{price_wei}</span> = price per NFT in wei, <span class="mono">{native}</span> = the "native coin" address used by thirdweb-style drops. Bundled (struct) inputs are a JSON list; placeholders work inside it.</p>
    <div class="row">${f.inputs.map((i, k) => i.type.startsWith("(") || i.type.endsWith("[]") && i.type.startsWith("(")
      ? `<label style="flex:1 1 100%">${esc(i.name || `input ${k + 1}`)} <span class="mono">${esc(i.type)}</span><textarea data-arg="${k}" rows="2" spellcheck="false">${esc(c.args[k] || "")}</textarea></label>`
      : `<label>${esc(i.name || `input ${k + 1}`)} <span class="mono">${esc(i.type)}</span><input data-arg="${k}" value="${esc(c.args[k] || "")}" style="width:200px"></label>`).join("") || `<span class="muted">This function takes no inputs.</span>`}</div>
    <div class="row" style="margin-top:10px">
      <label>Quantity / wallet<input id="cm-qty" type="number" min="1" value="${esc(form.quantity)}" style="width:90px"></label>
      <label>Price per NFT (${esc(symbol(r.chain_id))})<input id="cm-price" value="${esc(form.price)}" style="width:120px" placeholder="0 if free"></label>
      ${sourceControls("cm", form, w)}
    </div>
    <div class="row" style="margin-top:10px">
      <label>When to send <select id="cm-when">
        <option value="now" ${form.when === "now" || !form.when ? "selected" : ""}>Now</option>
        <option value="time" ${form.when === "time" ? "selected" : ""}>At a set time</option>
        <option value="open" ${form.when === "open" ? "selected" : ""}>The moment the sale opens</option></select></label>
      ${form.when === "time" ? `<label>Start time<input id="cm-start" type="datetime-local" step="1" value="${esc(form.start)}"></label>` : ""}
      ${form.when === "open" ? `<label>Wait at most (minutes)<input id="cm-maxwait" type="number" min="1" max="1440" value="${esc(form.maxwait || 60)}" style="width:110px"></label>` : ""}
      <label>Gas limit (needed unless sending now)<input id="cm-gas" type="number" value="${esc(form.gas_limit)}" placeholder="e.g. 200000" style="width:130px"></label>
      <label style="display:flex;gap:6px;align-items:center"><input id="cm-fast" type="checkbox" ${form.fast ? "checked" : ""} style="width:auto"> Fast mode: skip the last-second test run</label>
      <label>Max total (${esc(symbol(r.chain_id))}, optional)<input id="cm-cap" value="${esc(form.cap || "")}" style="width:110px" placeholder="from Settings"></label>
    </div>
    ${usesProof ? `<label style="margin-top:8px">Allowlist proofs — JSON <span class="mono">{"0xWallet": ["0x…", …]}</span> or lines <span class="mono">0xWallet, 0xproof1 0xproof2</span><textarea id="cm-proofs" rows="5" spellcheck="false">${esc(form.proofs)}</textarea></label>` : ""}
    <p class="tiny muted">Each wallet is test-run on-chain in the preview. For a scheduled start, wallets wait, refresh 3 s before, re-run the test at the start time and only send if it passes (unless fast mode is on).</p>
    <div class="row"><span class="spacer"></span><button class="primary" data-action="cm-preview">Preview mint</button></div>
  </div>`;
}
function readMintCForm() {
  const c = S.c, form = c.form, v = (id) => ($(id) ? $(id).value : undefined);
  $$("[data-arg]").forEach((i) => { c.args[Number(i.dataset.arg)] = i.value; });
  form.quantity = v("#cm-qty") ?? form.quantity; form.price = v("#cm-price") ?? form.price; form.group = v("#cm-group") ?? form.group;
  form.start = v("#cm-start") ?? form.start; form.gas_limit = v("#cm-gas") ?? form.gas_limit; form.cap = v("#cm-cap") ?? form.cap;
  form.proofs = v("#cm-proofs") ?? form.proofs; if ($("#cm-fast")) form.fast = $("#cm-fast").checked;
  form.when = v("#cm-when") ?? form.when; form.maxwait = v("#cm-maxwait") ?? form.maxwait;
}
Object.assign(ACTIONS_EXTRA, {
  alerts: () => guard(async () => {
    const a = await api("/api/alerts");
    modal(`<div class="row"><h2 style="margin:0">Alerts</h2><span class="spacer"></span><button class="small" data-action="alerts-read">Mark all read</button><button class="small" data-action="close">Close</button></div>
      <div>${a.alerts.length ? alertsHTML(a.alerts) : `<div class="empty">No alerts yet.</div>`}</div>`);
  }),
  "alerts-read": () => guard(async () => { await api("/api/alerts/read", {}); setBell(0); closeModal(); if (S.page === "overview") render(); }),
  "col-watch": (el) => guard(async () => { await api("/api/collections/watch", { slug: el.dataset.slug, watch: el.dataset.watch === "1" }); render(); }),
  "col-check": (el) => guard(() => withBusy(el, "Checking…", async () => { await api("/api/collections/check", { slug: el.dataset.slug }); toast("Checked"); render(); })),
  "notify-on": (el) => {
    if (!("Notification" in window)) return toast("This browser does not support desktop notifications", true);
    if (localStorageGet("nftops.notify") === "1") { localStorageSet("nftops.notify", "0"); el.textContent = "Turn on desktop alerts"; return; }
    Notification.requestPermission().then((p) => {
      if (p === "granted") { localStorageSet("nftops.notify", "1"); el.textContent = "Desktop alerts: on"; new Notification("NFT Ops", { body: "Desktop alerts are on." }); }
      else toast("Notifications were blocked in the browser settings", true);
    });
  },
  "sound-toggle": (el) => { const on = localStorageGet("nftops.sound") !== "1"; localStorageSet("nftops.sound", on ? "1" : "0"); el.textContent = on ? "Sound: on" : "Sound: off"; if (on) beep(); },
  "c-analyze": (el) => guard(() => withBusy(el, "Reading contract…", async () => {
    const c = S.c; c.address = $("#c-address").value.trim(); S.chainId = Number($("#c-chain").value);
    const res = await api("/api/contracts/analyze", { chain_id: S.chainId, address: c.address, abi: $("#c-abi").value });
    c.report = res.report; c.fn = null; c.fnSpec = null; c.args = []; S.tx.preview = null; render();
  })),
  "c-load": (el) => guard(async () => {
    const c = S.c; c.address = el.dataset.address; S.chainId = Number(el.dataset.chain);
    c.report = (await api("/api/contracts")).reports.find((x) => x.address === el.dataset.address && x.chain_id === S.chainId);
    c.fn = null; c.fnSpec = null; c.args = []; render();
  }),
  "c-use": (el) => { const c = S.c; c.fn = Number(el.dataset.index); c.fnSpec = c.report.mint_functions[c.fn]; c.args = []; S.tx.preview = null; render(); },
  "c-use-custom": () => {
    const sig = $("#c-custom-fn").value.trim(); const m = /^([A-Za-z_]\w*)\((.*)\)$/.exec(sig.replace(/\s/g, ""));
    if (!m) return toast("Write it like mint(uint256)", true);
    const c = S.c; c.fn = -1; c.args = [];
    c.fnSpec = { signature: sig.replace(/\s/g, ""), needs: m[2].includes("bytes32[]") ? "Merkle proof" : "", inputs: splitTop(m[2]).map((t) => ({ name: "", type: t })) };
    render();
  },
  "cm-preview": (el) => guard(() => withBusy(el, "Test-running every wallet…", async () => {
    readMintCForm(); const c = S.c, form = c.form;
    const start = form.when === "time" && form.start ? new Date(form.start).getTime() / 1000 : null;
    if (form.when === "time" && !start) throw new ApiError("Pick the start time.");
    const p = await api("/api/contracts/mint/preview", { chain_id: c.report.chain_id, address: c.report.address, signature: c.fnSpec.signature,
      args: c.args, quantity: Number(form.quantity), price: form.price || "0", ...sourceSelection(form), start_at: start,
      gas_limit: Number(form.gas_limit || 0), resimulate: !form.fast, proofs: form.proofs, cap: form.cap, label: c.report.name || "",
      wait_for_open_minutes: form.when === "open" ? Number(form.maxwait || 60) : 0 });
    p.created = Date.now() / 1000; S.tx.preview = p; $("#c-preview").innerHTML = txPreviewHTML(p);
    $("#c-preview").scrollIntoView({ behavior: "smooth", block: "start" });
  })),
});

const PAGE_RENDER = { overview: renderOverview, wallets: renderWallets, collections: renderCollections, eligibility: renderEligibility, mint: renderMint, transfers: renderTransfers, contracts: renderContracts, history: renderHistory, networks: renderNetworks, settings: renderSettings };

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
  "fund-short": (el) => {
    const p = el.closest("#preview") ? S.mint.preview : S.tx.preview;
    const key = el.dataset.key, per = {};
    for (const r of shortRows(p.rows)) per[r[key]] = Math.max(per[r[key]] || 0, Number(r.shortfall));
    const lines = Object.entries(per).map(([a, v]) => `${a},${(Math.ceil(v * 1.02 * 1e6) / 1e6).toFixed(6)}`);
    S.chainId = Number(el.dataset.chain); localStorageSet("nftops.chain", S.chainId);
    S.tx.tab = "distribute"; S.tx.preview = null;
    S.tx.dist = { asset: "native", mode: "custom", text: `address,amount\n${lines.join("\n")}` };
    go("transfers"); toast(`Pick the wallet that pays, then Preview distribution`);
  },
  replace: (el) => guard(async () => {
    const p = await api("/api/tx/replace/preview", { history_id: Number(el.dataset.id), mode: el.dataset.mode });
    S.replace = p;
    modal(`<h2>${p.mode === "cancel" ? "Cancel" : "Speed up"} stuck transaction</h2>
      <p class="muted">${p.mode === "cancel" ? "Replaces it with a 0-value transfer to yourself. If the replacement confirms first, the original never happens." : "Sends the same transaction again with higher gas. Only one of the two can confirm."}</p>
      <div class="table-wrap"><table>
        <tr><td>Wallet</td><td class="mono">${esc(p.address)}</td></tr><tr><td>Nonce</td><td class="mono">${p.nonce}</td></tr>
        <tr><td>Original</td><td>${txLink(p.chain_id, p.original_hash)}</td></tr>
        <tr><td>Max fee</td><td class="mono">${esc(p.old_max_fee_gwei)} → ${esc(p.new_max_fee_gwei)} gwei</td></tr>
        <tr><td>Max gas cost</td><td class="mono">${esc(p.max_gas_cost)} ${esc(symbol(p.chain_id))}</td></tr></table></div>
      ${p.problems.map((x) => `<div class="notice bad">${esc(x)}</div>`).join("")}
      ${p.token ? `<label>Type ${esc(p.confirm_word)} to send the replacement<input id="replace-confirm" autocomplete="off"></label>` : ""}
      <div class="row"><span class="spacer"></span><button data-action="close">Close</button>${p.token ? `<button class="primary" data-action="replace-go">Send replacement</button>` : ""}</div>`);
  }),
  "replace-go": (el) => guard(() => withBusy(el, "Sending…", async () => {
    const r = await api("/api/tx/replace/send", { token: S.replace.token, confirm: $("#replace-confirm").value });
    closeModal(); toast(`Replacement sent: ${short(r.tx_hash)}`); render();
  })),
  backup: () => modal(`<h2>Download backup</h2><p class="muted">Your keys stay encrypted inside it. Keep the file private and remember the password.</p>
    <label>Vault password<input id="backup-pw" type="password" autocomplete="current-password"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="backup-go">Download</button></div>`),
  "backup-go": (el) => guard(() => withBusy(el, "Preparing…", async () => {
    const res = await fetch("/api/backup", { method: "POST", headers: { "X-Session": S.session, "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#backup-pw").value }) });
    if (!res.ok) throw new ApiError((await res.json()).error || "backup failed");
    const name = (res.headers.get("content-disposition") || "").match(/filename="(.+)"/)?.[1] || "nft-ops-backup.zip";
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name }); document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    closeModal(); toast(`Saved ${name}`);
  })),
  "cc-pick": (el) => { S.mint.slug = el.dataset.slug; S.mint.stage = el.dataset.stage; S.mint.preview = null; render(); },
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
  const fn = el && (ACTIONS[el.dataset.action] || ACTIONS_EXTRA[el.dataset.action]);
  if (fn) { e.preventDefault(); fn(el, e); }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "csv-file" && e.target.files[0]) e.target.files[0].text().then((t) => { $("#csv-text").value = t; });
  if (e.target.id === "d-file" && e.target.files[0]) e.target.files[0].text().then((t) => { $("#d-text").value = t; S.tx.dist.text = t; });
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

boot();
