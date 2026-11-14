"use strict";
// Stage 6: Scheduled mints, Notification center, schedule-from-mint, overview summary.
// Loaded after app.js and shares its helpers (api, esc, modal, askConfirm, badge, ...).

const S6 = { notif: { category: "", unread: false }, tasks: [] };
const TIMEZONES = ["Asia/Jakarta", "Asia/Singapore", "Asia/Tokyo", "Europe/London", "America/New_York", "UTC"];
const TASK_BADGE = {
  scheduled: ["Scheduled", "info"], monitoring: ["Monitoring", "info"], executing: ["Executing", "info"],
  awaiting_confirmation: ["Needs your OK", "warn"], running: ["Executing", "info"], done: ["Successful", "good"],
  partial: ["Partially successful", "warn"], failed: ["Failed", "bad"], missed: ["Missed", "bad"],
  cancelled: ["Cancelled", ""], paused: ["Paused", "warn"], interrupted: ["Interrupted", "bad"],
};
const MODE_HELP = {
  monitor: "Checks readiness and reports. Never sends a transaction.",
  confirm: "Prepares everything and notifies you. Sends only after you click Mint now.",
  auto: "Mints automatically when the time comes, within your maximum spend.",
};
PAGES.push("schedules", "notifications");

function taskBadge(status) {
  const [label, cls] = TASK_BADGE[status] || [status, ""];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}
function inTz(ts, tz) {
  if (!ts) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { timeZone: tz, dateStyle: "medium", timeStyle: "short" }).format(new Date(ts * 1000)) + ` (${tz.split("/").pop().replace("_", " ")})`;
  } catch { return fmtTime(ts); }
}
function walletsText(t) {
  if (t.wallet_source === "group") return `Group ${t.group_name}`;
  if (t.wallet_source === "addresses") return `${t.addresses.length} selected wallet${t.addresses.length === 1 ? "" : "s"}`;
  return "All wallets with keys";
}

// ---------------------------------------------------------------- scheduled mints page
async function renderSchedules() {
  const d = await api("/api/schedules");
  S6.tasks = d.tasks;
  const open = d.tasks.filter((t) => ["scheduled", "monitoring", "awaiting_confirmation", "running", "paused", "executing"].includes(t.status));
  const past = d.tasks.filter((t) => !open.includes(t));
  const armed = open.some((t) => t.mode !== "monitor");
  $("#main").innerHTML = `
    <div class="page-head"><h1>Scheduled mints</h1><button class="primary" data-action="sched-new">+ Schedule a mint</button></div>
    ${armed ? `<div class="notice info">Keep NFT Ops running and unlocked until your automatic tasks have run — the vault will not auto-lock while one is waiting. If the PC is off at mint time, the task is marked <b>Missed</b> and you are told why.</div>` : ""}
    <div class="panel">${open.length ? taskTable(open, true) : `<div class="empty">No scheduled mints. Click <b>Schedule a mint</b>, or open the Mint page, pick a collection and phase and click <b>Schedule instead</b>.</div>`}</div>
    ${past.length ? `<div class="panel"><h2>Finished</h2>${taskTable(past.slice(0, 30), false)}</div>` : ""}`;
}
function taskTable(tasks, live) {
  return `<div class="table-wrap"><table>
    <tr><th>Task</th><th>Official start</th><th>Bot starts</th><th>Mode</th><th>Wallets</th><th>Status</th><th></th></tr>
    ${tasks.map((t) => {
      const r = t.readiness;
      return `<tr><td><b>${esc(t.name)}</b><div class="tiny muted">${esc(network(t.chain_id)?.name || "")} · ${esc(t.stage_name)} · ${t.quantity}/wallet${t.max_total ? ` · max ${esc(t.max_total)} ${esc(symbol(t.chain_id))}` : ""}</div></td>
        <td class="tiny">${esc(inTz(t.phase_starts_at, t.timezone))}</td>
        <td class="tiny">${esc(inTz(t.run_at, t.timezone))}${live && t.run_at > Date.now() / 1000 ? `<div class="muted">in ${countdown(t.run_at)}</div>` : ""}</td>
        <td>${esc(t.mode_label)}</td>
        <td class="tiny">${esc(walletsText(t))}${r ? `<div>${r.ready}/${r.total} ready</div>` : ""}</td>
        <td>${taskBadge(t.status)}<div class="tiny muted wrap">${esc(t.status_detail || "")}</div></td>
        <td class="row" style="gap:6px;flex-wrap:wrap">
          ${t.status === "awaiting_confirmation" ? `<button class="small primary" data-action="sched-confirm" data-id="${t.id}">Mint now</button>` : ""}
          ${["scheduled", "monitoring", "paused"].includes(t.status) ? `<button class="small" data-action="sched-check" data-id="${t.id}">Check readiness</button>` : ""}
          ${r ? `<button class="small" data-action="sched-ready" data-id="${t.id}">Wallets</button>` : ""}
          ${["scheduled", "monitoring", "paused"].includes(t.status) ? `<button class="small" data-action="sched-edit" data-id="${t.id}">Edit</button>` : ""}
          ${["scheduled", "monitoring", "awaiting_confirmation"].includes(t.status) ? `<button class="small" data-action="sched-pause" data-id="${t.id}">Pause</button>` : ""}
          ${t.status === "paused" ? `<button class="small" data-action="sched-resume" data-id="${t.id}">Resume</button>` : ""}
          ${["scheduled", "monitoring", "awaiting_confirmation", "paused"].includes(t.status) ? `<button class="small ghost" data-action="sched-cancel" data-id="${t.id}">Cancel</button>` : ""}
          ${t.report ? `<button class="small" data-action="sched-report" data-id="${t.id}">Report</button>` : ""}
        </td></tr>`;
    }).join("")}</table></div>`;
}
const taskById = (id) => S6.tasks.find((t) => String(t.id) === String(id));

async function scheduleModal(prefill = {}) {
  const [cols, w] = await Promise.all([api("/api/collections"), api(`/api/wallets?chain_id=${S.chainId}`)]);
  const live = liveCollections(cols.collections);
  if (!live.length) return toast("Add a collection first (Collections page).", true);
  const f = { slug: live[0].slug, stage: "", source: "all", group: w.groups[0]?.name || "", quantity: 1, max_total: "", mode: "confirm", lead: 120, tz: localStorageGet("nftops.tz") || "Asia/Jakarta", name: "", ...prefill };
  const draw = () => {
    const col = live.find((c) => c.slug === f.slug) || live[0];
    const stages = col.stages.filter((s) => s.state !== "ended");
    if (!stages.some((s) => String(s.index) === String(f.stage))) f.stage = stages[0] ? String(stages[0].index) : "";
    const st = stages.find((s) => String(s.index) === String(f.stage));
    modal(`<h2>Schedule a mint</h2>
      <div class="row"><label>Collection<select id="sc-slug">${options(live, f.slug, (c) => `${c.name || c.slug} (${c.chain_name})`, (c) => c.slug)}</select></label>
        <label>Phase<select id="sc-stage">${stages.length ? options(stages, f.stage, (s) => `${s.name} — ${s.state}`, (s) => s.index) : "<option>No upcoming phase</option>"}</select></label></div>
      ${st ? `<div class="notice info">Official start: <b>${esc(inTz(st.starts_at, f.tz))}</b> · price ${st.price_wei === null ? "—" : esc(weiToStr(st.price_wei)) + " " + esc(symbol(col.chain_id))} · max ${st.max_per_wallet ?? "—"} per wallet.
        The bot starts <b>${f.lead}s earlier</b> to sign in and prepare, then sends at launch.</div>` : ""}
      <div class="row"><label>Wallets<select id="sc-source"><option value="all" ${f.source === "all" ? "selected" : ""}>All wallets with keys (only eligible ones mint)</option>
          <option value="group" ${f.source === "group" ? "selected" : ""}>A group</option><option value="addresses" ${f.source === "addresses" ? "selected" : ""}>Selected wallets (${S.selected.size})</option></select></label>
        ${f.source === "group" ? `<label>Group<select id="sc-group">${options(w.groups, f.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>` : ""}
        <label>Qty / wallet<input id="sc-qty" type="number" min="1" value="${esc(f.quantity)}" style="width:80px"></label></div>
      <div style="margin:10px 0">${Object.entries(MODE_HELP).map(([k, help]) => `<label class="radio"><input type="radio" name="sc-mode" value="${k}" ${f.mode === k ? "checked" : ""}> <b>${{ monitor: "Monitor only", confirm: "Notify & confirm", auto: "Automatic mint" }[k]}</b> <span class="tiny muted">${help}</span></label>`).join("")}</div>
      <div class="row"><label>Maximum total spend (${esc(symbol(col.chain_id))})<input id="sc-max" value="${esc(f.max_total)}" placeholder="${f.mode === "monitor" ? "optional" : "required"}" style="width:140px"></label>
        <label>Start early (seconds)<input id="sc-lead" type="number" min="30" max="3600" value="${esc(f.lead)}" style="width:100px"></label>
        <label>Timezone<select id="sc-tz">${options([...new Set([f.tz, ...TIMEZONES])], f.tz)}</select></label>
        <label>Name (optional)<input id="sc-name" value="${esc(f.name)}" placeholder="${esc(col.slug)} · ${esc(st ? st.name : "")}"></label></div>
      <p class="tiny muted">Before running, the bot checks eligibility for these wallets, balances, gas, allocation and limits again. Spending limits from Settings always apply as well.</p>
      <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" id="sc-save" ${st ? "" : "disabled"}>Save schedule</button></div>`);
    const read = () => {
      f.quantity = $("#sc-qty").value; f.max_total = $("#sc-max").value; f.lead = Number($("#sc-lead").value || 120);
      f.tz = $("#sc-tz").value; f.name = $("#sc-name").value; f.mode = ($("input[name=sc-mode]:checked") || {}).value || f.mode;
      if ($("#sc-group")) f.group = $("#sc-group").value;
    };
    $("#sc-slug").onchange = (e) => { read(); f.slug = e.target.value; f.stage = ""; draw(); };
    $("#sc-stage").onchange = (e) => { read(); f.stage = e.target.value; draw(); };
    $("#sc-source").onchange = (e) => { read(); f.source = e.target.value; draw(); };
    $("#sc-tz").onchange = () => { read(); draw(); };
    $$("input[name=sc-mode]").forEach((r) => { r.onchange = () => { read(); draw(); }; });
    $("#sc-save").onclick = () => guard(async () => {
      read(); localStorageSet("nftops.tz", f.tz);
      if (f.mode === "auto" && !await askConfirm("Turn on automatic minting?", `<p>When the phase opens, NFT Ops will send mint transactions from the ready wallets <b>without asking again</b>, spending at most <b>${esc(f.max_total)} ${esc(symbol(col.chain_id))}</b> in total.</p><p class="tiny muted">You can pause or cancel it any time before it starts.</p>`, "Yes, mint automatically")) return draw();
      const r = await api("/api/schedules", { action: "create", slug: f.slug, stage_index: Number(f.stage), wallet_source: f.source, group: f.group,
        addresses: f.source === "addresses" ? [...S.selected] : [], quantity: Number(f.quantity), max_total: f.max_total, mode: f.mode,
        lead_seconds: f.lead, timezone: f.tz, name: f.name });
      closeModal(); toast(`Scheduled task #${r.id}`); go("schedules");
    });
  };
  draw();
}

function schedReportHTML(t) {
  const r = t.report, sym = symbol(t.chain_id);
  return `<h2>${esc(t.name)} — ${esc(r.status)}</h2>
    <p>${esc(r.summary)}</p>
    <div class="grid cards" style="margin:10px 0">
      <div class="card"><div class="muted">Succeeded</div><div class="big">${r.totals.success}</div></div>
      <div class="card"><div class="muted">Failed</div><div class="big">${r.totals.failed}</div></div>
      <div class="card"><div class="muted">Not attempted</div><div class="big">${r.totals.skipped}</div></div>
      <div class="card"><div class="muted">Minted · spent · gas</div><div class="big">${r.totals.minted}</div><div class="tiny muted">${esc(r.totals.spent)} + ${esc(r.totals.gas)} ${esc(sym)} gas</div></div></div>
    <p class="tiny muted">${esc(r.mode)} · ${esc(t.stage_name)} · ran ${esc(inTz(r.ran_at, t.timezone))}</p>
    <div class="table-wrap"><table><tr><th>Wallet</th><th>Result</th><th>Why</th><th>Transaction</th></tr>
      ${r.wallets.map((w) => `<tr><td>${addr(w.address, w.label)}</td><td>${badge(w.result)}</td><td class="wrap">${esc(w.reason || "")}</td><td>${txLink(t.chain_id, w.tx_hash)}</td></tr>`).join("")}
    </table></div><div class="row"><span class="spacer"></span><button data-action="close">Close</button></div>`;
}
function readinessHTML(t) {
  const r = t.readiness;
  return `<h2>Wallet readiness — ${esc(t.name)}</h2>
    <p>${r.ready} of ${r.total} wallets ready · worst case ${esc(r.worst_case)} ${esc(symbol(t.chain_id))} · checked ${esc(fmtTime(t.checked_at))}</p>
    ${(r.blocks || []).map((b) => `<div class="notice bad">${esc(b)}</div>`).join("")}
    <div class="table-wrap"><table><tr><th>Wallet</th><th>Eligibility</th><th>Readiness</th><th>Why / what to do</th><th class="num">Balance</th></tr>
      ${r.wallets.map((w) => `<tr><td>${addr(w.address, w.label)}</td><td>${badge(w.eligibility || "—")}</td><td>${badge(w.status)}</td><td class="wrap">${esc(w.reason)}</td><td class="num">${esc(w.balance ?? "—")}</td></tr>`).join("")}
    </table></div><p class="tiny muted">Readiness is checked again right before the mint; this table is not a guarantee.</p>
    <div class="row"><span class="spacer"></span><button data-action="close">Close</button></div>`;
}
const schedAct = (action, msg) => (el) => guard(() => withBusy(el, "Working…", async () => {
  await api("/api/schedules", { action, id: Number(el.dataset.id) }); if (msg) toast(msg); render();
}));
Object.assign(ACTIONS_EXTRA, {
  "sched-new": () => guard(() => scheduleModal()),
  "schedule-from-mint": () => guard(async () => {
    const m = S.mint; readMintForm();
    await scheduleModal({ slug: m.slug, stage: m.stage, quantity: m.form.quantity, max_total: m.form.operation_cap,
      source: m.source === "group" ? "group" : m.source === "selected" ? "addresses" : "all", group: m.group });
  }),
  "sched-check": schedAct("check", "Readiness checked"),
  "sched-pause": schedAct("pause", "Paused"),
  "sched-resume": schedAct("resume", "Resumed"),
  "sched-cancel": (el) => guard(async () => {
    if (!await askConfirm("Cancel this scheduled mint?", "<p>The task will not run. This cannot be undone.</p>", "Cancel task")) return;
    await api("/api/schedules", { action: "cancel", id: Number(el.dataset.id) }); toast("Cancelled"); render();
  }),
  "sched-confirm": (el) => guard(async () => {
    const t = taskById(el.dataset.id);
    if (!await askConfirm("Mint now?", `<p><b>${esc(t.name)}</b>: readiness is checked again, then the ready wallets mint (max ${esc(t.max_total)} ${esc(symbol(t.chain_id))}).</p>`, "Mint now")) return;
    const r = await api("/api/schedules", { action: "confirm", id: t.id }); toast(`Operation #${r.job_id} started`); render();
  }),
  "sched-report": (el) => modal(schedReportHTML(taskById(el.dataset.id))),
  "sched-ready": (el) => modal(readinessHTML(taskById(el.dataset.id))),
  "sched-edit": (el) => {
    const t = taskById(el.dataset.id);
    modal(`<h2>Edit — ${esc(t.name)}</h2>
      <label>Mode<select id="se-mode">${options(Object.keys(MODE_HELP), t.mode, (k) => ({ monitor: "Monitor only", confirm: "Notify & confirm", auto: "Automatic mint" }[k]))}</select></label>
      <label>Qty / wallet<input id="se-qty" type="number" min="1" value="${t.quantity}"></label>
      <label>Maximum total spend (${esc(symbol(t.chain_id))})<input id="se-max" value="${esc(t.max_total)}"></label>
      <label>Start early (seconds)<input id="se-lead" type="number" value="${t.lead_seconds}"></label>
      <label>Timezone<select id="se-tz">${options([...new Set([t.timezone, ...TIMEZONES])], t.timezone)}</select></label>
      <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="sched-edit-go" data-id="${t.id}">Save</button></div>`);
  },
  "sched-edit-go": (el) => guard(async () => {
    const mode = $("#se-mode").value;
    if (mode === "auto" && taskById(el.dataset.id).mode !== "auto" && !await askConfirm("Turn on automatic minting?", `<p>The task will send transactions without asking again, within ${esc($("#se-max").value)}.</p>`, "Yes")) return;
    await api("/api/schedules", { action: "update", id: Number(el.dataset.id), mode, quantity: Number($("#se-qty").value), max_total: $("#se-max").value, lead_seconds: Number($("#se-lead").value), timezone: $("#se-tz").value });
    closeModal(); toast("Saved"); render();
  }),
});

// ---------------------------------------------------------------- notification center
const notifyOn = (cat) => localStorageGet(`nftops.notify.${cat}`) !== "0";
async function renderNotifications() {
  const q = S6.notif;
  const d = await api(`/api/notifications?category=${encodeURIComponent(q.category)}&unread=${q.unread ? 1 : 0}`);
  setBell(d.unread);
  const cats = Object.entries(d.categories);
  $("#main").innerHTML = `
    <div class="page-head"><h1>Notifications</h1><div class="row"><label class="radio"><input type="checkbox" id="n-unread" ${q.unread ? "checked" : ""}> Unread only</label>
      <button data-action="notif-read-all" ${d.unread ? "" : "disabled"}>Mark all read</button></div></div>
    <div class="chips"><button class="${q.category ? "" : "on"}" data-action="notif-cat" data-cat="">All${d.unread ? ` (${d.unread})` : ""}</button>
      ${cats.map(([k, label]) => `<button class="${q.category === k ? "on" : ""}" data-action="notif-cat" data-cat="${k}">${esc(label)}${d.unread_by_category[k] ? ` (${d.unread_by_category[k]})` : ""}</button>`).join("")}</div>
    <div class="panel">${d.notifications.length ? d.notifications.map((n) => `
      <div class="alert-row n6 ${n.read ? "" : "unread"}"><span class="dot ${esc(n.level)}"></span>
        <div style="flex:1;min-width:0"><div class="msg">${n.title ? `<b>${esc(n.title)}</b> — ` : ""}${esc(n.message)}</div>
          <div class="tiny muted">${esc(fmtTime(n.created_at))} · ${esc(d.categories[n.category || "collection"] || n.category)}</div></div>
        <div class="row" style="gap:6px;flex:0 0 auto">${n.link ? `<button class="small" data-action="notif-open" data-id="${n.id}" data-link="${esc(n.link)}">Open</button>` : ""}
        ${n.read ? "" : `<button class="small ghost" data-action="notif-read" data-id="${n.id}">Mark read</button>`}</div></div>`).join("")
      : `<div class="empty">Nothing here yet. Mint results, scheduled tasks, readiness problems, offers and RPC issues will appear here.</div>`}</div>
    <div class="panel"><h2>Desktop pop-ups</h2><p class="tiny muted">Everything is always kept here. Choose which categories also pop up on your desktop (turn on desktop alerts in Settings first).</p>
      <div class="row" style="flex-wrap:wrap">${cats.map(([k, label]) => `<label class="radio"><input type="checkbox" data-notify-cat="${k}" ${notifyOn(k) ? "checked" : ""}> ${esc(label)}</label>`).join("")}</div></div>`;
  $("#n-unread").onchange = (e) => { q.unread = e.target.checked; render(); };
  $$("[data-notify-cat]").forEach((c) => { c.onchange = () => localStorageSet(`nftops.notify.${c.dataset.notifyCat}`, c.checked ? "1" : "0"); });
}
Object.assign(ACTIONS_EXTRA, {
  "notif-cat": (el) => { S6.notif.category = el.dataset.cat; render(); },
  "notif-read": (el) => guard(async () => { await api("/api/notifications/read", { ids: [Number(el.dataset.id)] }); render(); }),
  "notif-read-all": () => guard(async () => { await api("/api/notifications/read", {}); render(); }),
  "notif-open": (el) => guard(async () => {
    await api("/api/notifications/read", { ids: [Number(el.dataset.id)] });
    go(el.dataset.link.split(":")[0]);
  }),
});

// Desktop pop-ups respect the per-category choice; in-app records are never dropped.
window.onAlert = function onAlertS6(ev) {
  setBell(Number($("#bell-count").textContent || 0) + 1);
  toast(ev.message, ev.level === "warn" || ev.level === "bad");
  if (localStorageGet("nftops.sound") === "1") beep();
  if (localStorageGet("nftops.notify") === "1" && notifyOn(ev.category || "collection") && "Notification" in window && Notification.permission === "granted") {
    try { new Notification("NFT Ops", { body: ev.message, icon: "/static/icon.svg" }); } catch { /* ignore */ }
  }
  if (["overview", "notifications", "schedules"].includes(S.page)) scheduleRefresh();
};

// ---------------------------------------------------------------- overview: upcoming tasks
const overviewBase = PAGE_RENDER.overview;
PAGE_RENDER.overview = async () => {
  await overviewBase();
  let d; try { d = await api("/api/schedules"); } catch { return; }
  const open = d.tasks.filter((t) => ["scheduled", "monitoring", "awaiting_confirmation", "running", "paused"].includes(t.status));
  const last = d.tasks.find((t) => t.report);
  const el = document.createElement("section"); el.className = "panel";
  el.innerHTML = `<div class="row"><h2 style="margin:0">Scheduled mints</h2><span class="spacer"></span><button class="small" data-action="sched-new">+ Schedule</button><button class="small ghost" data-action="goto" data-page="schedules">Open</button></div>
    ${open.length ? `<div class="table-wrap"><table>${open.slice(0, 5).map((t) => `<tr><td><b>${esc(t.name)}</b></td><td class="tiny">${esc(inTz(t.phase_starts_at, t.timezone))}</td><td>${esc(t.mode_label)}</td><td class="tiny">${t.readiness ? `${t.readiness.ready}/${t.readiness.total} ready` : "not checked yet"}</td><td>${taskBadge(t.status)}</td></tr>`).join("")}</table></div>`
      : `<div class="empty">Nothing scheduled. Set up a mint once and let NFT Ops watch it for you.</div>`}
    ${last ? `<p class="tiny muted">Last result: <b>${esc(last.name)}</b> — ${esc(last.report.status)}: ${esc(last.report.summary)}</p>` : ""}`;
  const main = $("#main"); const head = $(".page-head", main);
  if (head) head.after(el); else main.prepend(el);
};
ACTIONS_EXTRA.goto = (el) => go(el.dataset.page);
Object.assign(PAGE_RENDER, { schedules: renderSchedules, notifications: renderNotifications });
