"use strict";
// Stage 6.5: design-system behaviour, sidebar navigation, human-friendly labels,
// Emergency Stop dialog, and the redesigned Overview, Eligibility and Mint Calendar pages.
// Loaded after app.js and stage6.js; reuses their helpers (api, esc, modal, askConfirm, ...).

const S65 = { elig: { status: "ALL", stage: "ALL", sort: "address", dir: 1, page: 0, ready: {} },
  cal: { view: "month", anchor: Date.now() / 1000, network: "", slug: "", type: "", status: "" }, health: null };
PAGES.push("calendar");
const TITLES = { overview: "Overview", calendar: "Mint calendar", eligibility: "Eligibility", collections: "Collections", mint: "Mint",
  schedules: "Scheduled mints", wallets: "Wallets", transfers: "Transfers", portfolio: "Portfolio", offers: "Offers", contracts: "Contracts",
  history: "History", notifications: "Notifications", networks: "Networks", settings: "Settings" };

// ---------------------------------------------------------------- plain-language labels
const PHASE_TYPE = { PUBLIC_SALE: "Public sale", SIGNED_PRESALE: "Allowlist", MERKLE_PRESALE: "Allowlist", PRESALE: "Presale" };
const HUMAN = {
  ELIGIBLE: ["Eligible", "OpenSea confirmed this wallet can mint in this phase."],
  NOT_ELIGIBLE: ["Not eligible", "OpenSea says this wallet is not on this phase's list."],
  UNKNOWN: ["Unknown", "Unable to verify eligibility. The source did not return a reliable answer. This does not mean the wallet is ineligible — scan again."],
  UNVERIFIED: ["Unverified", "Watch-only wallet: without its key it cannot sign in to OpenSea, so eligibility could not be checked."],
  "NOT SCANNED": ["Not scanned", "Run an eligibility scan for this collection first."],
  READY: ["Ready", "Passed every check in the preview."],
  NO_KEY: ["No key", "Watch-only wallet: import its key to mint."],
  ALREADY_RUNNING: ["Busy", "This wallet is already in another operation."],
  PHASE_NOT_ACTIVE: ["Phase ended", "This mint phase has ended."],
  SOLD_OUT: ["Sold out", "OpenSea reports the drop is minted out or disabled."],
  ELIGIBILITY_UNKNOWN: ["Eligibility unknown", "Eligibility has not been verified. Run an eligibility scan."],
  ALLOCATION_EXCEEDED: ["Allocation used", "The wallet's allocation does not cover this quantity."],
  RPC_ERROR: ["Network error", "Network connection failed. The bot could not read this wallet. Try again or add another RPC endpoint."],
  INSUFFICIENT_BALANCE: ["Not enough funds", "This wallet does not have enough funds to cover the mint price."],
  INSUFFICIENT_GAS: ["Not enough for fees", "This wallet does not have enough funds to cover the mint price and estimated network fees."],
  LIMIT_EXCEEDED: ["Over limit", "This would exceed one of your spending limits (Settings)."],
  SIMULATION_FAILED: ["Test run failed", "The transaction was tested and would fail on-chain, so it is not sent."],
  not_submitted: ["Not sent", "Nothing was sent for this wallet."], confirmed: ["Confirmed", "Mined successfully on-chain."],
  pending: ["Pending", "Sent and waiting for confirmation."], submitted: ["Sent", "Broadcast, waiting for confirmation."],
  failed: ["Failed", "The transaction failed or reverted."], blocked: ["Blocked", "Stopped by a safety limit before sending."],
  cancelled: ["Cancelled", "Cancelled before anything was sent."], queued: ["Queued", ""], preparing: ["Preparing", ""],
  waiting: ["Waiting for launch", ""], broadcasting: ["Sending", ""], interrupted: ["Interrupted", "NFT Ops was closed while this was running."],
  active: ["Open now", ""], upcoming: ["Upcoming", ""], ended: ["Ended", ""],
  "Ready": ["Ready", "Every check passed."], "Almost Ready": ["Almost ready", "Only a minor issue, such as a pending transaction."],
  "Not Ready": ["Not ready", "At least one required check failed. Open the wallet to see why."],
  "Needs Attention": ["Needs attention", "Some checks could not be verified (for example eligibility not scanned or the network did not answer)."],
};
const BADGE_CLASS = { "Almost Ready": "warn", "Not Ready": "bad", "Needs Attention": "warn", Ready: "good", interrupted: "bad",
  "Not attempted": "warn", Minted: "good", "Transaction failed": "bad", "Not sent": "bad" };
const badgeOrig = badge;
badge = function badgeHuman(status) {
  const key = String(status ?? "");
  const h = HUMAN[key] || HUMAN[key.toUpperCase()];
  const cls = BADGE_CLASS[key] ?? (h ? null : undefined);
  if (!h) return badgeOrig(status);
  const base = badgeOrig(key);
  const klass = cls || (base.match(/badge ([a-z]*)/) || [])[1] || "";
  return `<span class="badge ${klass}"${h[1] ? ` title="${esc(h[1])}"` : ""}>${esc(h[0])}</span>`;
};
const phaseType = (t) => PHASE_TYPE[t] || (t ? String(t).replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) : "—");
const ago = (ts) => {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return fmtTime(ts);
};
const tzName = safeTz;

// ---------------------------------------------------------------- shell: sidebar, title, status
const shell = $("#app");
if (localStorageGet("nftops.side") === "collapsed") shell.classList.add("collapsed");
$("#collapse-toggle").addEventListener("click", () => {
  shell.classList.toggle("collapsed");
  localStorageSet("nftops.side", shell.classList.contains("collapsed") ? "collapsed" : "open");
  $("#collapse-toggle").textContent = shell.classList.contains("collapsed") ? "»" : "«";
});
$("#collapse-toggle").textContent = shell.classList.contains("collapsed") ? "»" : "«";
const closeDrawerNav = () => { shell.classList.remove("drawer-open"); $("#scrim").classList.add("hidden"); };
$("#menu-toggle").addEventListener("click", () => { shell.classList.add("drawer-open"); $("#scrim").classList.remove("hidden"); });
$("#scrim").addEventListener("click", closeDrawerNav);
$("#sys-status").addEventListener("click", () => go("overview"));
const goOrig = go;
go = function goS65(page) { closeDrawerNav(); closeDrawer(); goOrig(page); $("#page-title").textContent = TITLES[page] || ""; };
const setBellOrig = setBell;
setBell = function setBellS65(n) {
  setBellOrig(n);
  const c = $("#nav-count-notifications"); if (c) { c.textContent = n; c.classList.toggle("hidden", !n); }
};
setStopped = function setStoppedS65(on) {
  S.stopped = on;
  $("#stopped-banner").classList.toggle("hidden", !on);
  const b = $("#stop-button"); b.textContent = on ? "Stopped" : "Emergency stop"; b.classList.toggle("on", on);
  paintStatus();
};
async function refreshHealth() {
  try { S65.health = await api("/api/health"); } catch { S65.health = null; }
  paintStatus();
}
function paintStatus() {
  const el = $("#sys-status"); if (!el) return;
  const h = S65.health;
  let cls = "good", text = "All systems normal";
  if (S.stopped) { cls = "bad"; text = "Emergency stop active"; }
  else if (!h) { cls = "off"; text = "Status unknown"; }
  else if (!h.scheduler.ok) { cls = "bad"; text = "Scheduler not running"; }
  else if (!h.rpc.ok) { cls = "warn"; text = "RPC needs attention"; }
  el.innerHTML = `<span class="dot ${cls}"></span><span class="lbl">${esc(text)}</span>`;
  el.title = text;
}
setInterval(() => { if (!$("#app").classList.contains("hidden")) refreshHealth(); }, 30000);
setTimeout(refreshHealth, 1500);

// Emergency stop: explain first, then stop.
{
  const old = $("#stop-button"); const btn = old.cloneNode(true); old.replaceWith(btn);
  btn.addEventListener("click", () => guard(async () => {
    if (S.stopped) return $("#resume-button").click();
    const yes = await askConfirm("Emergency stop", `
      <p>This immediately halts everything that has <b>not been sent yet</b>:</p>
      <ul><li>mints waiting for launch and wallets still preparing</li><li>transfers and fund moves not yet sent</li>
      <li>scheduled automatic mints (they will not start while stopped)</li><li>automatic offer acceptance</li></ul>
      <div class="notice warn">Transactions already sent to the blockchain <b>cannot be recalled</b>. They keep being tracked. To speed up or cancel one, use History.</div>
      <p class="tiny muted">You can resume at any time with the Resume button.</p>`, "Stop everything");
    if (!yes) return;
    await api("/api/stop", {}); setStopped(true); toast("Emergency stop is active. Nothing new will be sent.", true);
  }));
}

// ---------------------------------------------------------------- drawer component
function openDrawer(html) {
  closeDrawer();
  const d = document.createElement("aside"); d.className = "drawer"; d.id = "drawer"; d.innerHTML = html;
  document.body.append(d);
}
function closeDrawer() { const d = $("#drawer"); if (d) d.remove(); }
ACTIONS_EXTRA["drawer-close"] = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

function scoreHTML(r) {
  if (!r) return `<span class="muted tiny">Not checked</span>`;
  const color = r.status === "Ready" ? "var(--good)" : r.status === "Not Ready" ? "var(--bad)" : "var(--warn)";
  return `<span class="score" title="${esc((HUMAN[r.status] || [])[1] || "")}"><span class="bar"><div style="width:${r.score}%;background:${color}"></div></span><b>${r.score}</b> ${badge(r.status)}</span>`;
}
function checksHTML(r) {
  const mark = { pass: "✓", fail: "✕", unverified: "?", info: "i" };
  return r.checks.map((c) => `<div class="check"><span class="mark ${c.state}">${mark[c.state] || "·"}</span><span>${esc(c.label)}</span>
    <span class="tiny muted">${c.weight ? `${c.state === "pass" ? c.weight : 0}/${c.weight}` : ""}</span>
    ${c.detail ? `<span class="det">${esc(c.detail)}</span>` : ""}${c.fix ? `<span class="fix">→ ${esc(c.fix)}</span>` : ""}</div>`).join("");
}

// ---------------------------------------------------------------- OVERVIEW
async function renderOverview65() {
  const [d, sched, notif, cal] = await Promise.all([api("/api/overview"), api("/api/schedules"), api("/api/notifications"), api("/api/calendar")]);
  await refreshHealth();
  const h = S65.health; setStopped(d.stopped); setBell(notif.unread);
  const now = Date.now() / 1000, c24 = d.last24h || {};
  const open = sched.tasks.filter((t) => ["scheduled", "monitoring", "awaiting_confirmation", "running", "paused"].includes(t.status));
  const upcoming = cal.events.filter((e) => e.kind === "phase" && (e.start > now || (e.end && e.end > now)));
  const failed = (c24.failed || 0) + (c24.blocked || 0) + (c24.not_submitted || 0);
  const attention = failed + open.filter((t) => t.status === "awaiting_confirmation").length;
  const rd = d.readiness_24h || {};
  const pending = (c24.pending || 0) + (c24.submitted || 0);
  const next = cal.events.filter((e) => (e.start || 0) > now && !(e.kind === "task" && ["cancelled", "done", "failed", "missed", "partial"].includes(e.state))).slice(0, 7);
  const kpi = (k, v, s, cls = "", page = "") => `<div class="kpi ${cls}${page ? " clickable" : ""}" ${page ? `data-action="goto" data-page="${page}"` : ""}><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  const item = (ok, label, val) => `<div class="health-item"><span class="dot ${ok === null ? "off" : ok ? "good" : "bad"}"></span>${esc(label)}<span class="val">${esc(val)}</span></div>`;
  $("#main").innerHTML = `
    <div class="page-head"><div><h1>Overview</h1><div class="subtitle">Everything happening across your wallets, mints and schedules.</div></div>
      <div class="quick"><button data-action="goto" data-page="eligibility">Check eligibility</button><button data-action="sched-new">Schedule a mint</button><button class="primary" data-action="goto" data-page="mint">Mint</button></div></div>
    ${S.stopped ? `<div class="notice bad">Emergency stop is active. Nothing new is being sent. Resume from the red bar above.</div>` : ""}
    <div class="kpis">
      ${kpi("Wallets", d.wallets, `${d.with_keys} can sign`, "", "wallets")}
      ${kpi("Collections tracked", d.collections ?? "—", `${upcoming.length} upcoming or open phase${upcoming.length === 1 ? "" : "s"}`, "", "collections")}
      ${kpi("Scheduled tasks", open.length, open.length ? `next ${esc(open[0].name)}` : "nothing scheduled", open.length ? "info" : "", "schedules")}
      ${kpi("Successful (24h)", c24.confirmed || 0, "confirmed transactions", (c24.confirmed ? "good" : ""), "history")}
      ${kpi("Needs attention", attention, failed ? `${failed} failed or not sent (24h)` : "no failures in 24h", attention ? "bad" : "", attention ? "notifications" : "")}
    </div>
    <div class="cols-2">
      <div>
        <section class="panel"><div class="section-head"><h2>Coming up</h2><button class="small ghost" data-action="goto" data-page="calendar">Open calendar</button></div>
          ${next.length ? next.map((e) => `<div class="next-item">${e.kind === "task" ? `<span class="badge info">Bot</span>` : badge(e.state)}
            <div style="min-width:0"><div><b>${esc(e.kind === "task" ? e.name : e.slug)}</b> · ${esc(e.phase || "")}</div>
            <div class="tiny muted">${esc(e.network || "")} · ${esc(phaseType(e.phase_type))}${e.kind === "task" ? ` · ${esc({ monitor: "Monitor only", confirm: "Notify & confirm", auto: "Automatic" }[e.mode] || "")}${e.total ? ` · ${e.ready}/${e.total} ready` : ""}` : ` · ${e.eligible_wallets} eligible wallet${e.eligible_wallets === 1 ? "" : "s"}`}</div></div>
            <div class="when"><div>${countdown(e.start)}</div><div class="tiny muted">${esc(inTz(e.start, tzName()))}</div></div></div>`).join("")
            : `<div class="empty">No upcoming mints. Add a collection to start tracking its phases.<div style="margin-top:10px"><button data-action="goto" data-page="collections">Add a collection</button></div></div>`}
        </section>
        <section class="panel"><div class="section-head"><h2>Wallets</h2><button class="small ghost" data-action="goto" data-page="wallets">Manage</button></div>
          <div class="kpis" style="margin:0">
            ${kpi("Eligible (any phase)", d.eligible_wallets, "from the latest scans", "good")}
            ${kpi("Ready to mint", rd.Ready || 0, "readiness checked in 24h", rd.Ready ? "good" : "")}
            ${kpi("Need attention", (rd["Not Ready"] || 0) + (rd["Needs Attention"] || 0) + (rd["Almost Ready"] || 0), "see Eligibility → readiness", (rd["Not Ready"] || rd["Needs Attention"]) ? "warn" : "")}
          </div>
          <div class="table-wrap" style="margin-top:12px"><table><tr><th>Network</th><th class="num">Balance (all wallets)</th><th class="num">NFTs held</th></tr>
            ${d.totals.map((t) => `<tr><td>${esc(t.name)}</td><td class="num">${esc(t.native)} ${esc(t.native_symbol)}</td><td class="num">${t.nfts}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">Refresh balances on the Wallets page.</td></tr>`}</table></div>
        </section>
      </div>
      <div>
        <section class="panel"><div class="section-head"><h2>System health</h2><button class="small ghost" data-action="goto" data-page="networks">Networks</button></div>
          <div class="health-list">
            ${item(!d.stopped, "Emergency stop", d.stopped ? "Active — nothing is sent" : "Off")}
            ${h ? item(h.scheduler.ok, "Scheduler", h.scheduler.text) : ""}
            ${h ? item(h.monitor.ok, "Collection monitor", h.monitor.text) : ""}
            ${h ? item(h.offers.ok, "Offer manager", h.offers.text) : ""}
            ${h ? h.rpc.key_networks.map((n) => item(n.total && n.checked_at ? n.healthy > 0 : null, `RPC · ${n.name}`, !n.total ? "no endpoint" : n.checked_at ? `${n.healthy}/${n.total} healthy` : "checking…")).join("") : ""}
            ${item(true, "Notifications", h && h.notifications.unread_failures ? `${h.notifications.unread_failures} unread failure(s)` : "Working")}
            ${item(pending === 0 ? true : null, "Pending transactions", pending ? `${pending} waiting` : "None")}
          </div></section>
        <section class="panel"><div class="section-head"><h2>Recent activity</h2><button class="small ghost" data-action="goto" data-page="notifications">All</button></div>
          ${notif.notifications.slice(0, 8).map((n) => `<div class="feed-item"><span class="dot ${esc(n.level)}"></span><div style="min-width:0">${n.title ? `<b>${esc(n.title)}</b> — ` : ""}<span class="muted">${esc(n.message)}</span></div><span class="t">${esc(ago(n.created_at))}</span></div>`).join("")
            || `<div class="empty">No activity yet.</div>`}
        </section>
      </div>
    </div>`;
}

// ---------------------------------------------------------------- ELIGIBILITY
const STATUS_RANK = { ELIGIBLE: 4, UNKNOWN: 3, UNVERIFIED: 2, NOT_ELIGIBLE: 1 };
async function renderEligibility65() {
  const cols = liveCollections((await api("/api/collections")).collections); S.collections = cols;
  if (!cols.length) {
    $("#main").innerHTML = `<div class="page-head"><div><h1>Eligibility</h1><div class="subtitle">Check which wallets qualify for a collection's mint phases.</div></div></div>
      <div class="panel empty"><b>No collections yet.</b><br>Add the collection you want to mint first; its phases and network are detected automatically.<div style="margin-top:12px"><button class="primary" data-action="goto" data-page="collections">Add a collection</button></div></div>`;
    return;
  }
  if (!cols.some((c) => c.slug === S.elig.slug)) S.elig.slug = cols[0].slug;
  const col = cols.find((c) => c.slug === S.elig.slug), q = S65.elig;
  const [d, w, rd] = await Promise.all([api(`/api/eligibility?slug=${encodeURIComponent(col.slug)}`), api(`/api/wallets?chain_id=${col.chain_id}`),
    api(`/api/readiness?slug=${encodeURIComponent(col.slug)}`)]);
  S65.ready = {}; for (const r of rd.wallets) S65.ready[`${r.stage_index}:${r.address}`] = r;
  S65.eligRows = d.rows; S65.eligWallets = w;
  const p = d.progress || {}, scanning = p.running && p.slug === col.slug;
  if (q.stage !== "ALL" && !col.stages.some((s) => String(s.index) === q.stage)) q.stage = "ALL";
  const phaseRows = d.rows.filter((r) => q.stage === "ALL" || String(r.stage_index) === q.stage);
  const best = {};
  for (const r of phaseRows) if (!best[r.address] || STATUS_RANK[r.status] > STATUS_RANK[best[r.address]]) best[r.address] = r.status;
  const n = (s) => Object.values(best).filter((x) => x === s).length;
  const total = Object.keys(best).length;
  let rows = phaseRows.filter((r) => q.status === "ALL" || r.status === q.status || (q.status === "UNKNOWN" && r.status === "UNKNOWN"));
  const key = { address: (r) => (r.label || r.address).toLowerCase(), status: (r) => -STATUS_RANK[r.status], phase: (r) => r.stage_index,
    alloc: (r) => r.max_quantity ?? -1, price: (r) => Number(r.price_wei || 0), ready: (r) => (S65.ready[`${r.stage_index}:${r.address}`] || { score: -1 }).score, checked: (r) => r.checked_at || 0 }[q.sort];
  rows = rows.slice().sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * q.dir);
  const per = 50, pages = Math.max(1, Math.ceil(rows.length / per)); q.page = Math.min(q.page, pages - 1);
  const shown = rows.slice(q.page * per, q.page * per + per);
  const sym = symbol(col.chain_id);
  const th = (k, label, cls = "") => `<th class="sortable ${cls}" data-action="elig-sort" data-key="${k}">${label}${q.sort === k ? `<span class="arrow">${q.dir > 0 ? "▲" : "▼"}</span>` : ""}</th>`;
  const stageSel = col.stages.filter((s) => s.state !== "ended");
  $("#main").innerHTML = `
    <div class="page-head"><div><h1>Eligibility</h1><div class="subtitle">Check which wallets qualify for a collection's mint phases.</div></div>
      <div class="row"><button class="ghost" data-action="elig-history">Scan history</button><button class="ghost" data-action="export-elig">Export CSV</button><button class="ghost" data-action="clear-elig" ${scanning ? "disabled" : ""}>Clear results</button></div></div>
    <div class="kpis">
      <div class="kpi"><div class="k">Total wallets</div><div class="v">${total}</div><div class="s">${d.last_scan ? `in the last scan (${esc(d.last_scan.group)})` : "no scan yet"}</div></div>
      <div class="kpi good clickable" data-action="elig-filter" data-status="ELIGIBLE"><div class="k">Eligible</div><div class="v">${n("ELIGIBLE")}</div><div class="s">${q.stage === "ALL" ? "for at least one phase" : "for this phase"}</div></div>
      <div class="kpi bad clickable" data-action="elig-filter" data-status="NOT_ELIGIBLE"><div class="k">Not eligible</div><div class="v">${n("NOT_ELIGIBLE")}</div><div class="s">not on the list</div></div>
      <div class="kpi warn clickable" data-action="elig-filter" data-status="UNKNOWN"><div class="k">Unknown / unverified</div><div class="v">${n("UNKNOWN") + n("UNVERIFIED")}</div><div class="s">could not be confirmed — not the same as ineligible</div></div>
    </div>
    <section class="panel">
      <div class="row">
        <label>Collection<select id="e-slug">${options(cols, col.slug, (c) => `${c.name || c.slug} · ${c.chain_name}`, (c) => c.slug)}</select></label>
        <label>Wallets<select id="e-group"><option value="">All wallets (${w.wallets.length})</option>${options(w.groups, S65.elig.group || "", (g) => `Group: ${g.name} (${g.size})`, (g) => g.name)}</select></label>
        <span class="spacer"></span>
        <button data-action="scan-selected" ${scanning || !S.selected.size ? "disabled" : ""} title="Select wallets on the Wallets page first">Scan selected (${S.selected.size})</button>
        <button class="primary" data-action="scan" ${scanning ? "disabled" : ""}>${scanning ? `<span class="spinner"></span> Scanning…` : "Scan wallets"}</button>
      </div>
      <div class="tiny muted" style="margin-top:10px">Network: <b>${esc(col.chain_name)}</b> (detected from the collection) ·
        ${col.stages.map((s) => `${esc(s.name)} <span class="muted">(${esc(phaseType(s.type))}, ${esc(HUMAN[s.state] ? HUMAN[s.state][0] : s.state)})</span>`).join(" · ")}</div>
      <div style="margin-top:12px;${scanning ? "" : "display:none"}"><div class="progress"><div id="scan-bar" style="width:${p.total ? (100 * p.done) / p.total : 0}%"></div></div>
        <div class="tiny muted" id="scan-text" style="margin-top:6px">${p.done || 0} of ${p.total || 0} wallets checked · ${Math.max(0, (p.total || 0) - (p.done || 0))} remaining${d.signin && d.signin.cooling_down_seconds ? ` — OpenSea asked us to slow down, waiting ${d.signin.cooling_down_seconds}s` : ""}</div></div>
      ${!scanning && d.last_scan ? `<div class="tiny muted" style="margin-top:8px">Last scan: <b>${esc(d.last_scan.group)}</b> · ${d.last_scan.total} wallets · ${esc(ago(d.last_scan.at))}. A new scan replaces these results; earlier scans stay in <a href="#" data-action="elig-history">Scan history</a>.</div>` : ""}
      ${!scanning && p.rate_limited && p.slug === col.slug ? `<div class="notice warn">The last scan stopped early because OpenSea was limiting sign-ins. Wallets it did not reach show Unknown — wait a few minutes and scan again.</div>` : ""}
    </section>
    <section class="panel">
      <div class="section-head"><h2>Results</h2>
        <label>Phase<select id="e-stage"><option value="ALL">All phases</option>${options(col.stages, q.stage, (s) => `${s.name} (${phaseType(s.type)})`, (s) => String(s.index))}</select></label>
        <button data-action="elig-ready" ${stageSel.length ? "" : "disabled"} title="Runs the real mint checks: balance, fees, allocation, limits, pending transactions">Check readiness</button></div>
      <div class="chips">${[["ALL", "All"], ["ELIGIBLE", "Eligible"], ["NOT_ELIGIBLE", "Not eligible"], ["UNKNOWN", "Unknown"], ["UNVERIFIED", "Unverified"]].map(([k, l]) =>
        `<button class="${q.status === k ? "on" : ""}" data-action="elig-filter" data-status="${k}">${l}</button>`).join("")}</div>
      ${shown.length ? `<div class="table-wrap"><table class="responsive"><thead><tr>${th("address", "Wallet")}${th("status", "Eligibility")}${th("phase", "Mint phase")}${th("alloc", "Allocation", "num")}${th("price", "Mint price", "num")}${th("ready", "Readiness")}${th("checked", "Last checked")}<th></th></tr></thead><tbody>
        ${shown.map((r) => `<tr class="clickable" data-action="elig-wallet" data-address="${esc(r.address)}">
          <td>${r.label ? `<b>${esc(r.label)}</b> ` : ""}<span class="mono muted" title="${esc(r.address)}">${esc(short(r.address))}</span> <button class="small ghost" data-action="copy" data-text="${esc(r.address)}" title="Copy address">⧉</button></td>
          <td data-label="Eligibility">${badge(r.status)}</td>
          <td data-label="Phase">${esc(r.stage_name)} <span class="tiny muted">${esc(phaseType(r.stage_type))}</span></td>
          <td class="num" data-label="Allocation">${r.max_quantity ?? "—"}${r.minted ? ` <span class="tiny muted">(${r.minted} minted)</span>` : ""}</td>
          <td class="num" data-label="Price">${r.price_wei === null || r.price_wei === undefined ? "—" : `${esc(weiToStr(r.price_wei))} ${esc(sym)}`}</td>
          <td data-label="Readiness">${scoreHTML(S65.ready[`${r.stage_index}:${r.address}`])}</td>
          <td class="tiny muted" data-label="Checked">${esc(ago(r.checked_at))}</td>
          <td><button class="small" data-action="elig-wallet" data-address="${esc(r.address)}">Details</button></td></tr>`).join("")}</tbody></table></div>
        ${pages > 1 ? `<div class="pager">${rows.length} rows · page ${q.page + 1} of ${pages}<button class="small" data-action="elig-page" data-d="-1" ${q.page ? "" : "disabled"}>Previous</button><button class="small" data-action="elig-page" data-d="1" ${q.page < pages - 1 ? "" : "disabled"}>Next</button></div>` : ""}`
        : `<div class="empty">${d.rows.length ? "No wallets match this filter." : "<b>No results yet.</b><br>Choose wallets above and click <b>Scan wallets</b>. Each wallet signs in to OpenSea (a free signature, no transaction) and gets OpenSea's own answer."}</div>`}
      <p class="tiny muted" style="margin-top:10px"><b>Eligibility</b> = OpenSea's list. <b>Readiness</b> = whether the wallet can actually mint right now (funds, fees, allocation, limits, pending transactions). A wallet can be eligible but not ready.</p>
    </section>`;
  $("#e-slug").onchange = (e) => { S.elig.slug = e.target.value; q.stage = "ALL"; q.page = 0; render(); };
  $("#e-group").onchange = (e) => { S65.elig.group = e.target.value; };
  $("#e-stage").onchange = (e) => { q.stage = e.target.value; q.page = 0; render(); };
}
function walletDrawer(address) {
  const col = S.collections.find((c) => c.slug === S.elig.slug), sym = symbol(col.chain_id);
  const rows = S65.eligRows.filter((r) => r.address === address);
  const w = (S65.eligWallets.wallets || []).find((x) => x.address === address) || {};
  const stage = S65.elig.stage !== "ALL" ? Number(S65.elig.stage) : (col.stages.find((s) => s.state !== "ended") || col.stages[0]).index;
  const r = S65.ready[`${stage}:${address}`];
  const elig = rows.find((x) => x.stage_index === stage) || rows[0] || {};
  const next = !elig.status ? "Scan this wallet to learn whether it qualifies."
    : elig.status === "NOT_ELIGIBLE" ? "This wallet is not on the list for this phase. Use another wallet, or wait for a phase it qualifies for (e.g. the public sale)."
    : elig.status !== "ELIGIBLE" ? "Re-check this wallet. Unknown is not the same as ineligible."
    : !r ? "Eligible. Click Check readiness to confirm it has funds and fees for the mint."
    : r.status === "Ready" ? "Ready. Include it in a mint or a scheduled mint."
    : "Eligible but not ready — fix the items marked below, then check again.";
  openDrawer(`<div class="drawer-head"><h2>${esc(w.label || short(address))}</h2><button class="ghost icon-btn" data-action="drawer-close">✕</button></div>
    <div class="kv"><span>Address</span><span class="mono">${esc(address)} <button class="small ghost" data-action="copy" data-text="${esc(address)}">⧉</button></span>
      <span>Collection</span><span>${esc(col.name || col.slug)} · ${esc(col.chain_name)}</span>
      <span>Balance</span><span>${r && r.balance ? `${esc(r.balance)} ${esc(sym)}` : w.native != null ? `${esc(w.native)} ${esc(sym)}` : "— (refresh on Wallets)"}</span>
      <span>Last verified</span><span>${esc(elig.checked_at ? fmtTime(elig.checked_at) : "never")}</span>
      <span>Source</span><span>${esc(elig.source || "—")}</span></div>
    <div class="notice info"><b>What to do next:</b> ${esc(next)}</div>
    <h3>Phases</h3>
    <div class="table-wrap"><table><tr><th>Phase</th><th>Eligibility</th><th class="num">Allocation</th><th class="num">Price</th></tr>
      ${rows.map((x) => `<tr><td>${esc(x.stage_name)}<div class="tiny muted">${esc(phaseType(x.stage_type))}</div></td><td>${badge(x.status)}<div class="tiny muted wrap">${esc(x.detail || "")}</div></td><td class="num">${x.max_quantity ?? "—"}</td><td class="num">${x.price_wei == null ? "—" : `${esc(weiToStr(x.price_wei))} ${esc(sym)}`}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">Not scanned yet.</td></tr>`}</table></div>
    <div class="section-head" style="margin:6px 0 0"><h2>Readiness — ${esc((col.stages.find((s) => s.index === stage) || {}).name || "")}</h2>
      <button class="small" data-action="ready-one" data-address="${esc(address)}" data-stage="${stage}">${r ? "Check again" : "Check readiness"}</button></div>
    ${r ? `<div>${scoreHTML(r)} <span class="tiny muted">checked ${esc(ago(r.checked_at))}</span></div>
      ${Date.now() / 1000 - r.checked_at > 600 ? `<div class="notice warn">This result is ${esc(ago(r.checked_at))}. Balances and fees change — check again before minting. Mints always re-check automatically.</div>` : ""}
      <div>${checksHTML(r)}</div>` : `<div class="empty">Not checked yet for this phase.</div>`}
    <div class="row"><button data-action="rescan-one" data-address="${esc(address)}">Re-scan eligibility</button><span class="spacer"></span><button class="primary" data-action="goto" data-page="mint">Go to Mint</button></div>`);
}
Object.assign(ACTIONS_EXTRA, {
  "elig-filter": (el) => { S65.elig.status = el.dataset.status; S65.elig.page = 0; render(); },
  "elig-sort": (el) => { const q = S65.elig; q.dir = q.sort === el.dataset.key ? -q.dir : 1; q.sort = el.dataset.key; render(); },
  "elig-page": (el) => { S65.elig.page += Number(el.dataset.d); render(); },
  "elig-wallet": (el, e) => { if (e.target.closest("[data-action=copy]")) return; walletDrawer(el.dataset.address); },
  "scan": (el) => guard(async () => {
    const r = await api("/api/eligibility/scan", { slug: S.elig.slug, group: S65.elig.group || "" });
    toast(`Checking ${r.total} wallets…`); render();
  }),
  "scan-selected": () => guard(async () => {
    const r = await api("/api/eligibility/scan", { slug: S.elig.slug, addresses: [...S.selected] });
    toast(`Checking ${r.total} selected wallets…`); render();
  }),
  "rescan-one": (el) => guard(async () => {
    await api("/api/eligibility/scan", { slug: S.elig.slug, addresses: [el.dataset.address], replace: false });
    toast("Re-checking this wallet…"); closeDrawer(); setTimeout(render, 4000);
  }),
  "elig-ready": (el) => guard(() => withBusy(el, "Checking…", async () => {
    const col = S.collections.find((c) => c.slug === S.elig.slug);
    const stage = S65.elig.stage !== "ALL" ? Number(S65.elig.stage) : (col.stages.find((s) => s.state !== "ended") || {}).index;
    const addrs = [...new Set(S65.eligRows.filter((r) => S65.elig.status === "ALL" || r.status === S65.elig.status).map((r) => r.address))];
    if (!addrs.length) throw new ApiError("Scan some wallets first.");
    const r = await api("/api/readiness", { slug: S.elig.slug, stage_index: stage, addresses: addrs });
    toast(Object.entries(r.counts).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ")); render();
  })),
  "ready-one": (el) => guard(() => withBusy(el, "Checking…", async () => {
    const r = await api("/api/readiness", { slug: S.elig.slug, stage_index: Number(el.dataset.stage), addresses: [el.dataset.address] });
    S65.ready[`${el.dataset.stage}:${el.dataset.address}`] = { ...r.wallets[0], stage_index: Number(el.dataset.stage) };
    walletDrawer(el.dataset.address);
  })),
  "elig-history": () => guard(async () => {
    const d = await api(`/api/eligibility/scans?slug=${encodeURIComponent(S.elig.slug)}`);
    modal(`<h2>Scan history — ${esc(S.elig.slug)}</h2>
      ${d.scans.length ? `<div class="table-wrap"><table><tr><th>When</th><th>Wallets</th><th class="num">Total</th><th class="num">Eligible</th><th class="num">Not eligible</th><th class="num">Unknown</th><th></th></tr>
        ${d.scans.map((s) => `<tr><td>${esc(fmtTime(s.created_at))}</td><td>${esc(s.label || "")}</td><td class="num">${s.total}</td><td class="num">${s.counts.ELIGIBLE || 0}</td><td class="num">${s.counts.NOT_ELIGIBLE || 0}</td><td class="num">${(s.counts.UNKNOWN || 0) + (s.counts.UNVERIFIED || 0)}</td>
          <td><button class="small" data-action="elig-scan-view" data-id="${s.id}">View</button></td></tr>`).join("")}</table></div>` : `<div class="empty">No earlier scans recorded yet. Every new scan is kept here.</div>`}
      <div class="row"><span class="spacer"></span><button data-action="close">Close</button></div>`);
  }),
  "elig-scan-view": (el) => guard(async () => {
    const s = await api(`/api/eligibility/scans/${el.dataset.id}`);
    const now = {}; for (const r of S65.eligRows) now[`${r.address}:${r.stage_index}`] = r.status;
    const labels = {}; for (const w of S65.eligWallets.wallets || []) labels[w.address] = w.label;
    modal(`<h2>Scan of ${esc(fmtTime(s.created_at))} — ${esc(s.label || "")}</h2>
      <div class="table-wrap"><table><tr><th>Wallet</th><th>Phase</th><th>Then</th><th>Now</th></tr>
        ${s.results.map((r) => { const cur = now[`${r.address}:${r.stage_index}`]; return `<tr><td>${esc(labels[r.address] || short(r.address))}</td><td>${esc((S.collections.find((c) => c.slug === s.slug)?.stages.find((x) => x.index === r.stage_index) || {}).name || r.stage_index)}</td><td>${badge(r.status)}</td><td>${cur ? (cur === r.status ? `<span class="muted tiny">same</span>` : badge(cur) + ` <span class="badge info">changed</span>`) : `<span class="muted tiny">not in current results</span>`}</td></tr>`; }).join("")}</table></div>
      <div class="row"><span class="spacer"></span><button data-action="elig-history">Back</button><button data-action="close">Close</button></div>`);
  }),
});

// ---------------------------------------------------------------- MINT CALENDAR
function ymd(ts, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(ts * 1000)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}
const hm = (ts, tz) => new Intl.DateTimeFormat(undefined, { timeZone: tz, hour: "2-digit", minute: "2-digit" }).format(new Date(ts * 1000));
function evClass(e) {
  if (e.kind === "phase") return "phase";
  if (["failed", "missed", "interrupted"].includes(e.state)) return "task bad";
  if (["done", "partial"].includes(e.state)) return "task done";
  if (["awaiting_confirmation", "paused"].includes(e.state)) return "task warn";
  return "task";
}
function evLabel(e, tz) { return `${hm(e.start, tz)} ${e.kind === "task" ? "Bot · " + (e.name || e.slug) : `${e.slug} · ${e.phase}`}`; }
async function renderCalendar() {
  const c = S65.cal, tz = tzName();
  const d = await api("/api/calendar");
  S65.events = d.events;
  const nets = [...new Set(d.events.map((e) => e.network).filter(Boolean))], slugs = [...new Set(d.events.map((e) => e.slug))];
  const evs = d.events.filter((e) => e.start && (!c.network || e.network === c.network) && (!c.slug || e.slug === c.slug)
    && (!c.type || (c.type === "task" ? e.kind === "task" : phaseType(e.phase_type) === c.type))
    && (!c.status || (c.status === "active" ? ["active", "upcoming", "scheduled", "monitoring", "running", "awaiting_confirmation", "paused"].includes(e.state) : ["done", "partial", "failed", "missed", "ended", "interrupted"].includes(e.state))));
  // warnings: schedules whose official time no longer matches the phase, or several tasks on one phase
  const warnings = [];
  for (const t of d.events.filter((e) => e.kind === "task" && ["scheduled", "monitoring", "paused", "awaiting_confirmation"].includes(e.state))) {
    const ph = d.events.find((e) => e.kind === "phase" && e.slug === t.slug && e.stage_index === t.stage_index);
    if (!ph) warnings.push(`${t.name}: its phase is no longer listed by OpenSea.`);
    else if (t.official && Math.abs(ph.start - t.official) > 60) warnings.push(`${t.name}: the official start moved to ${inTz(ph.start, tz)}. The task is paused until you confirm.`);
    if (ph && ph.tasks.length > 1) warnings.push(`${ph.slug} · ${ph.phase} has ${ph.tasks.length} scheduled tasks — check they do not overlap.`);
  }
  const byDay = {}; for (const e of evs) (byDay[ymd(e.start, tz)] ||= []).push(e);
  const anchorDay = ymd(c.anchor, tz), [ay, am, ad] = anchorDay.split("-").map(Number), today = ymd(Date.now() / 1000, tz);
  const dayCell = (y, m, dd, other) => {
    const key = `${y}-${String(m).padStart(2, "0")}-${String(dd).padStart(2, "0")}`, list = byDay[key] || [];
    return `<div class="cal-day ${other ? "other" : ""} ${key === today ? "today" : ""}"><span class="d">${dd}</span>
      ${list.slice(0, 4).map((e) => `<button class="cal-ev ${evClass(e)}" data-action="cal-open" data-id="${esc(e.id)}" title="${esc(evLabel(e, tz))}">${esc(evLabel(e, tz))}</button>`).join("")}
      ${list.length > 4 ? `<button class="cal-ev" data-action="cal-day" data-day="${key}">+${list.length - 4} more</button>` : ""}</div>`;
  };
  let body = "", title = "";
  if (c.view === "month") {
    title = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(new Date(Date.UTC(ay, am - 1, 15)));
    const first = new Date(Date.UTC(ay, am - 1, 1)), startDow = (first.getUTCDay() + 6) % 7, days = new Date(Date.UTC(ay, am, 0)).getUTCDate();
    const cells = [], n = Math.ceil((startDow + days) / 7) * 7;
    for (let i = 0; i < n; i++) {
      const dt = new Date(Date.UTC(ay, am - 1, 1 - startDow + i));
      cells.push(dayCell(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate(), dt.getUTCMonth() !== am - 1));
    }
    body = `<div class="cal-grid">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((x) => `<div class="cal-dow">${x}</div>`).join("")}${cells.join("")}</div>`;
  } else if (c.view === "week") {
    const base = new Date(Date.UTC(ay, am - 1, ad)), dow = (base.getUTCDay() + 6) % 7;
    const daysArr = [...Array(7)].map((_, i) => new Date(Date.UTC(ay, am - 1, ad - dow + i)));
    title = `Week of ${new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" }).format(daysArr[0])}`;
    body = `<div class="week">${daysArr.map((dt) => {
      const key = dt.toISOString().slice(0, 10), list = byDay[key] || [];
      return `<div class="col"><h3>${new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", timeZone: "UTC" }).format(dt)}${key === today ? " · today" : ""}</h3>
        ${list.map((e) => `<button class="cal-ev ${evClass(e)}" data-action="cal-open" data-id="${esc(e.id)}">${esc(evLabel(e, tz))}</button>`).join("") || `<div class="tiny muted">—</div>`}</div>`;
    }).join("")}</div>`;
  } else {
    title = "Agenda";
    const now = Date.now() / 1000 - 86400 * 3;
    const days = Object.keys(byDay).sort().filter((k) => byDay[k].some((e) => e.start >= now));
    body = days.length ? days.map((k) => `<div class="agenda-day"><h3>${esc(new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(new Date(k + "T12:00:00Z")))}</h3>
      <div class="table-wrap"><table>${byDay[k].map((e) => `<tr class="clickable" data-action="cal-open" data-id="${esc(e.id)}"><td style="width:80px">${esc(hm(e.start, tz))}</td>
        <td>${e.kind === "task" ? `<span class="badge info">Bot</span> <b>${esc(e.name)}</b>` : `<b>${esc(e.slug)}</b> · ${esc(e.phase)}`}<div class="tiny muted">${esc(e.network || "")} · ${esc(phaseType(e.phase_type))}</div></td>
        <td>${e.kind === "task" ? `${esc({ monitor: "Monitor only", confirm: "Notify & confirm", auto: "Automatic" }[e.mode] || "")}${e.total ? ` · ${e.ready}/${e.total} ready` : ""}` : `${e.eligible_wallets} eligible`}</td>
        <td>${e.kind === "task" ? taskBadge(e.state) : badge(e.state)}</td></tr>`).join("")}</table></div></div>`).join("")
      : `<div class="empty">Nothing scheduled in the coming days. Add a collection, or create a scheduled mint.</div>`;
  }
  $("#main").innerHTML = `
    <div class="page-head"><div><h1>Mint calendar</h1><div class="subtitle">Official mint phases and the bot's scheduled runs, in ${esc(tz)}.</div></div>
      <button class="primary" data-action="sched-new">+ Schedule a mint</button></div>
    ${warnings.map((w) => `<div class="notice warn">${esc(w)}</div>`).join("")}
    <section class="panel">
      <div class="section-head">
        <div class="row" style="align-items:center"><button class="small" data-action="cal-nav" data-d="-1">‹</button><button class="small" data-action="cal-nav" data-d="0">Today</button><button class="small" data-action="cal-nav" data-d="1">›</button><h2 style="margin:0 0 0 6px">${esc(title)}</h2></div>
        <span class="spacer"></span>
        <div class="seg">${["month", "week", "agenda"].map((v) => `<button class="${c.view === v ? "on" : ""}" data-action="cal-view" data-v="${v}">${v[0].toUpperCase() + v.slice(1)}</button>`).join("")}</div>
      </div>
      <div class="row" style="margin-bottom:12px">
        <label>Network<select id="cal-net"><option value="">All networks</option>${options(nets, c.network)}</select></label>
        <label>Collection<select id="cal-slug"><option value="">All collections</option>${options(slugs, c.slug)}</select></label>
        <label>Type<select id="cal-type"><option value="">Everything</option>${options(["Allowlist", "Public sale", "Presale"], c.type)}<option value="task" ${c.type === "task" ? "selected" : ""}>Bot runs only</option></select></label>
        <label>Status<select id="cal-status"><option value="">Any</option><option value="active" ${c.status === "active" ? "selected" : ""}>Upcoming / active</option><option value="done" ${c.status === "done" ? "selected" : ""}>Finished / failed</option></select></label>
        <label>Timezone<select id="cal-tz">${options([...new Set([tz, ...TIMEZONES])], tz)}</select></label>
        <span class="spacer"></span><div class="legend"><span class="lp">Official phase</span><span class="lt">Bot run</span><span class="ld">Completed</span><span class="lb">Failed / missed</span></div>
      </div>
      ${body}
    </section>`;
  $("#cal-net").onchange = (e) => { c.network = e.target.value; render(); };
  $("#cal-slug").onchange = (e) => { c.slug = e.target.value; render(); };
  $("#cal-type").onchange = (e) => { c.type = e.target.value; render(); };
  $("#cal-status").onchange = (e) => { c.status = e.target.value; render(); };
  $("#cal-tz").onchange = (e) => { localStorageSet("nftops.tz", e.target.value); render(); };
}
function calEvent(id) {
  const e = S65.events.find((x) => x.id === id), tz = tzName();
  if (!e) return;
  if (e.kind === "phase") {
    modal(`<h2>${esc(e.slug)} · ${esc(e.phase)}</h2>
      <div class="kv"><span>Network</span><span>${esc(e.network)}</span><span>Phase type</span><span>${esc(phaseType(e.phase_type))}</span>
        <span>Official start</span><span>${esc(inTz(e.start, tz))}</span><span>Ends</span><span>${e.end ? esc(inTz(e.end, tz)) : "No end time"}</span>
        <span>Price</span><span>${e.price_wei == null ? "—" : `${esc(weiToStr(e.price_wei))} ${esc(symbol(e.chain_id))}`}</span>
        <span>Status</span><span>${badge(e.state)}</span><span>Eligible wallets</span><span>${e.eligible_wallets} (from the latest scan)</span>
        <span>Scheduled tasks</span><span>${e.tasks.length ? e.tasks.map((t) => `#${t}`).join(", ") : "None"}</span></div>
      <p class="tiny muted">This is the project's official time from OpenSea. A scheduled task starts a little earlier to prepare.</p>
      <div class="row"><button data-action="cal-elig" data-slug="${esc(e.slug)}">Check eligibility</button>${e.tasks.length ? `<button data-action="goto" data-page="schedules">Open scheduled tasks</button>` : ""}
        <span class="spacer"></span><button data-action="close">Close</button>${e.state !== "ended" ? `<button class="primary" data-action="cal-schedule" data-slug="${esc(e.slug)}" data-stage="${e.stage_index}">Schedule this mint</button>` : ""}</div>`);
  } else {
    modal(`<h2>${esc(e.name)}</h2>
      <div class="kv"><span>Network</span><span>${esc(e.network)}</span><span>Phase</span><span>${esc(e.phase)} (${esc(phaseType(e.phase_type))})</span>
        <span>Official start</span><span>${esc(inTz(e.official, tz))}</span><span>Bot starts</span><span>${esc(inTz(e.start, tz))}</span>
        <span>Mode</span><span>${esc({ monitor: "Monitor only", confirm: "Notify & confirm", auto: "Automatic mint" }[e.mode] || e.mode)}</span>
        <span>Wallet readiness</span><span>${e.total ? `${e.ready} of ${e.total} ready` : "Not checked yet"}</span><span>Status</span><span>${taskBadge(e.state)}</span></div>
      <div class="row"><span class="spacer"></span><button data-action="close">Close</button><button class="primary" data-action="goto" data-page="schedules">Open task</button></div>`);
  }
}
Object.assign(ACTIONS_EXTRA, {
  "cal-view": (el) => { S65.cal.view = el.dataset.v; render(); },
  "cal-nav": (el) => {
    const c = S65.cal, dd = Number(el.dataset.d);
    if (!dd) c.anchor = Date.now() / 1000;
    else if (c.view === "month") { const t = new Date(c.anchor * 1000); t.setUTCMonth(t.getUTCMonth() + dd, 15); c.anchor = t.getTime() / 1000; }
    else c.anchor += dd * 7 * 86400;
    render();
  },
  "cal-day": (el) => { S65.cal.view = "agenda"; render(); },
  "cal-open": (el) => calEvent(el.dataset.id),
  "cal-elig": (el) => { closeModal(); S.elig.slug = el.dataset.slug; go("eligibility"); },
  "cal-schedule": (el) => { closeModal(); guard(() => scheduleModal({ slug: el.dataset.slug, stage: el.dataset.stage })); },
  goto: (el) => { closeModal(); go(el.dataset.page); },
});

Object.assign(PAGE_RENDER, { overview: renderOverview65, eligibility: renderEligibility65, calendar: renderCalendar });
$("#page-title").textContent = TITLES[S.page] || "";

// Consistent page headers: every page gets a short plain-language subtitle.
const SUBTITLES = {
  collections: "Collections you track. Phases and the network are read from OpenSea automatically.",
  mint: "Mint now with ready wallets, or schedule it to run on its own.",
  schedules: "Mints you set up once. The bot checks readiness and runs them in the mode you chose.",
  wallets: "Your wallets, groups and balances. Private keys stay encrypted on this computer.",
  transfers: "Move NFTs, consolidate funds into one wallet, or distribute funds to many.",
  portfolio: "What you hold, its value at floor, and what you spent and received.",
  offers: "Offers on your NFTs, checked against your rules before anything is accepted.",
  contracts: "Mints that are not OpenSea drops: analyse a contract and mint with your wallets.",
  history: "Every transaction the bot sent, with its result and cost.",
  notifications: "Everything worth knowing, grouped by category.",
  networks: "Blockchain networks and the RPC endpoints used to reach them.",
  settings: "Spending limits, security, scan speed and integrations.",
};
const renderOrig = render;
render = async function renderS65() {
  await renderOrig();
  $("#page-title").textContent = TITLES[S.page] || "";
  const head = $("#main .page-head"); const h1 = head && $("h1", head);
  if (h1 && SUBTITLES[S.page] && !$(".subtitle", head)) {
    const wrap = document.createElement("div"); h1.replaceWith(wrap); wrap.append(h1);
    const sub = document.createElement("div"); sub.className = "subtitle"; sub.textContent = SUBTITLES[S.page]; wrap.append(sub);
  }
};
