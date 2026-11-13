"use strict";
// Stage 6.5.2: notification dropdown (bell), lime accent visuals, wallet-group tools
// (roles, smart groups, health, top-up, move/split, after-mint rule), portfolio spend breakdown.

// ---------------------------------------------------------------- notification dropdown
const NP = { tab: "", open: false };
const NP_TABS = [["", "View all"], ["mint", "Mints"], ["schedule", "Scheduled"], ["readiness", "Readiness"], ["system", "System"]];
const CAT_COLOR = { mint: "#1f3a2c", schedule: "#2b3245", readiness: "#3d3a1f", eligibility: "#203b3f", funds: "#45282c",
  offers: "#3b2f5c", system: "#33333a", security: "#45282c", collection: "#203b3f" };
const CAT_LETTER = { mint: "M", schedule: "S", readiness: "R", eligibility: "E", funds: "$", offers: "O", system: "!", security: "⚑", collection: "C" };
function dayTime(ts) {
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString(undefined, { weekday: "long" })} ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}
async function drawPanel() {
  const panel = $("#np"); if (!panel) return;
  const d = await api(`/api/notifications?category=${encodeURIComponent(NP.tab)}`);
  setBell(d.unread);
  panel.innerHTML = `
    <div class="np-head"><b>Your notifications</b><span class="spacer"></span>
      <button class="ghost icon-btn" data-action="np-readall" title="Mark all as read">✓✓</button>
      <button class="ghost icon-btn" data-action="np-settings" title="Notification settings and full list">⚙</button></div>
    <div class="np-tabs">${NP_TABS.map(([k, l]) => `<button class="${NP.tab === k ? "on" : ""}" data-action="np-tab" data-tab="${k}">${l}${k === "" && d.unread ? ` <span class="n">${d.unread}</span>` : k && d.unread_by_category[k] ? ` <span class="n">${d.unread_by_category[k]}</span>` : ""}</button>`).join("")}</div>
    <div class="np-list">${d.notifications.slice(0, 30).map((n) => {
      const cat = n.category || "collection";
      return `<div class="np-item ${n.read ? "" : "unread"}">
        <span class="avatar np-av" style="background:${CAT_COLOR[cat] || "#33333a"}">${CAT_LETTER[cat] || "•"}<i class="lv ${esc(n.level)}"></i></span>
        <div class="np-body"><div class="np-title"><b>${esc(n.title || d.categories[cat] || "Update")}</b>${n.slug ? ` <span class="muted">in</span> <b>${esc(n.slug)}</b>` : ""}</div>
          <div class="np-meta"><span>${esc(dayTime(n.created_at))}</span><span>${esc(ago(n.created_at))}</span></div>
          <div class="np-msg">${esc(n.message)}</div>
          <div class="np-actions">${n.link ? `<button class="small" data-action="np-open" data-id="${n.id}" data-link="${esc(n.link)}">Open</button>` : ""}${n.read ? "" : `<button class="small ghost" data-action="np-read" data-id="${n.id}">Mark read</button>`}</div></div>
        ${n.read ? "" : `<span class="np-dot"></span>`}</div>`;
    }).join("") || `<div class="empty">You're all caught up.</div>`}</div>`;
}
function togglePanel(force) {
  NP.open = force ?? !NP.open;
  let panel = $("#np");
  if (!NP.open) { if (panel) panel.remove(); return; }
  if (!panel) { panel = document.createElement("div"); panel.id = "np"; panel.className = "np"; document.body.append(panel); }
  panel.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  guard(drawPanel);
}
{
  const old = $("#bell"), bell = old.cloneNode(true); old.replaceWith(bell);
  bell.addEventListener("click", (e) => { e.stopPropagation(); togglePanel(); });
  document.addEventListener("click", (e) => { if (NP.open && !e.target.closest("#np") && !e.target.closest("#bell")) togglePanel(false); });
}
Object.assign(ACTIONS_EXTRA, {
  "np-tab": (el) => { NP.tab = el.dataset.tab; guard(drawPanel); },
  "np-readall": () => guard(async () => { await api("/api/notifications/read", {}); await drawPanel(); }),
  "np-read": (el) => guard(async () => { await api("/api/notifications/read", { ids: [Number(el.dataset.id)] }); await drawPanel(); }),
  "np-open": (el) => guard(async () => { await api("/api/notifications/read", { ids: [Number(el.dataset.id)] }); togglePanel(false); go(el.dataset.link.split(":")[0]); }),
  "np-settings": () => { togglePanel(false); go("notifications"); },
});
const onAlert652 = window.onAlert;
window.onAlert = function onAlertNP(ev) { onAlert652(ev); if (NP.open) guard(drawPanel); };
TITLES.notifications = "Notifications";

// ---------------------------------------------------------------- visuals: big numbers, area sparklines
function bigMoney(text) {
  const m = String(text).match(/^(.*?)([.,]\d+)$/);
  return m ? `${esc(m[1])}<span class="dec">${esc(m[2])}</span>` : esc(text);
}
spark = function sparkArea(points) {
  const v = points.map((p) => p.floor_native).filter((x) => x !== null && x !== undefined);
  if (v.length < 2) return `<span class="muted tiny">—</span>`;
  const min = Math.min(...v), max = Math.max(...v), w = 130, h = 34;
  const xy = v.map((x, i) => [(i / (v.length - 1)) * w, max === min ? h / 2 : h - 2 - ((x - min) / (max - min)) * (h - 4)]);
  const up = v[v.length - 1] >= v[0], col = up ? "var(--accent)" : "var(--bad)", id = `g${Math.random().toString(36).slice(2, 8)}`;
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="7-day floor trend, ${up ? "up" : "down"}">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".35"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <polygon points="0,${h} ${xy.map((p) => p.join(",")).join(" ")} ${w},${h}" fill="url(#${id})"/>
    <polyline points="${xy.map((p) => p.join(",")).join(" ")}" fill="none" stroke="${col}" stroke-width="1.8"/>
    <circle cx="${xy[xy.length - 1][0] - 2}" cy="${xy[xy.length - 1][1]}" r="3" fill="${col}"/></svg>`;
};

// ---------------------------------------------------------------- portfolio: spend breakdown
const pf651 = PAGE_RENDER.portfolio;
PAGE_RENDER.portfolio = async () => {
  await pf651();
  const d = await api(`/api/portfolio${S651.wallet ? `?wallet=${encodeURIComponent(S651.wallet)}` : ""}`);
  const sum = (k) => d.collections.reduce((a, r) => a + (r[`${k}_usd`] || 0), 0);
  const kpis = $$("#main .kpi");
  if (kpis[1]) {
    kpis[1].querySelector(".s").innerHTML = `minting ${fmtUSD(sum("spent_mint"))} · buying ${fmtUSD(sum("spent_buy"))}`;
    kpis[1].querySelector(".k").textContent = "Spent";
  }
  if (kpis[2]) kpis[2].querySelector(".s").textContent = "OpenSea sales (incl. accepted offers)";
  kpis.forEach((k) => { const v = k.querySelector(".v"); if (v && v.textContent.startsWith("$") || v && v.textContent.startsWith("−$")) v.innerHTML = bigMoney(v.textContent); });
  const th = $$("#main thead th"); const spentIdx = th.findIndex((x) => x.textContent === "Spent");
  if (spentIdx >= 0) $$("#main tbody tr").forEach((tr, i) => {
    const r = d.collections[i], td = tr.children[spentIdx];
    if (r && td && (r.bought || r.spent_mint_usd)) td.insertAdjacentHTML("beforeend", `<div class="tiny muted">mint ${fmtUSD(r.spent_mint_usd)} · buy ${fmtUSD(r.spent_buy_usd)}</div>`);
  });
};

// ---------------------------------------------------------------- wallet groups
const G = { data: null };
const ROLE_BADGE = { minting: "good", funding: "info", vault: "warn" };
const wallets652 = PAGE_RENDER.wallets;
PAGE_RENDER.wallets = async () => {
  await wallets652();
  const head = $("#main .page-head"); if (!head) return;
  const d = await api(`/api/groups/health?chain_id=${S.chainId}`); G.data = d;
  const sym = symbol(S.chainId);
  head.insertAdjacentHTML("afterend", `
    <section class="panel"><div class="section-head"><h2>Groups</h2>
      <span class="tiny muted">Roles: <b>Minting</b> = default wallets for eligibility scans · <b>Funding</b> = pays top-ups · <b>Vault</b> = safe storage for minted NFTs</span>
      <button class="small" data-action="g-split" ${d.groups.length ? "" : "disabled"}>Split a group…</button>
      <button class="small" data-action="g-move" ${S.selected.size ? "" : "disabled"} title="Select wallets in the table below first">Move selected (${S.selected.size})…</button></div>
      ${d.groups.length ? `<div class="group-grid">${d.groups.map((g) => `
        <div class="group-card">
          <div class="row" style="align-items:center;gap:8px"><b class="gname">${esc(g.name)}</b>${g.role ? `<span class="badge ${ROLE_BADGE[g.role] || ""}">${esc(d.roles[g.role])}</span>` : ""}
            ${g.after_mint === "vault" ? `<span class="badge warn" title="Minted NFTs are sent to ${esc(g.vault || "")}">→ vault</span>` : ""}</div>
          <div class="gstats"><div><span class="k">Wallets</span><span>${g.size}${g.with_keys !== g.size ? ` <span class="muted tiny">(${g.with_keys} with key)</span>` : ""}</span></div>
            <div><span class="k">Balance</span><span>${g.balance_known ? `${esc(g.balance)} ${esc(sym)}` : `<span class="muted">refresh</span>`}</span></div>
            <div><span class="k">Ready</span><span>${g.ready}/${g.size}</span></div>
            <div><span class="k">Low gas</span><span class="${g.low_gas ? "down" : ""}">${g.balance_known ? g.low_gas : "—"}</span></div></div>
          <div class="row" style="gap:6px"><button class="small" data-action="g-topup" data-name="${esc(g.name)}">Top up…</button>
            <button class="small" data-action="g-options" data-name="${esc(g.name)}">Role &amp; rules…</button></div>
        </div>`).join("")}</div>` : `<div class="empty">No groups yet. Select wallets below and use <b>Add to group</b>.</div>`}
      <div class="section-head" style="margin:18px 0 8px"><h2 style="font-size:15px">Smart groups <span class="muted" style="font-weight:400">— update themselves; use them anywhere you pick a group</span></h2>
        <button class="small ghost" data-action="g-smart-settings">Low-gas threshold: ${esc(d.smart_settings.low_gas)} ${esc(symbol(d.smart_settings.chain_id))}</button></div>
      <div class="chips">${d.smart.map((g) => `<span class="chip smart">${esc(g.name)} <b>${g.size}</b></span>`).join("")}</div>
    </section>`);
};
function groupByName(name) { return G.data.groups.find((g) => g.name === name); }
Object.assign(ACTIONS_EXTRA, {
  "g-topup": (el) => {
    const g = groupByName(el.dataset.name), sym = symbol(S.chainId);
    const funding = G.data.groups.find((x) => x.role === "funding");
    modal(`<h2>Top up “${esc(g.name)}”</h2>
      <p class="muted">Every wallet below the target gets exactly what it is missing. Nothing is sent yet: this opens a Distribute preview you check first.</p>
      <label>Target balance per wallet (${esc(sym)})<input id="g-target" value="0.01"></label>
      ${funding ? `<div class="notice info">Pay from your Funding group: <b>${esc(funding.name)}</b>${funding.members[0] ? ` (e.g. ${esc(funding.members[0].label || short(funding.members[0].address))})` : ""}.</div>` : `<div class="tiny muted">Tip: give a group the Funding role to mark where top-ups are paid from.</div>`}
      ${g.balance_known < g.size ? `<div class="notice warn">${g.size - g.balance_known} wallet(s) have no recent balance. Click Refresh balances first.</div>` : ""}
      <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="g-topup-go" data-name="${esc(g.name)}">Prepare top-up</button></div>`);
  },
  "g-topup-go": (el) => {
    const g = groupByName(el.dataset.name), target = Number($("#g-target").value);
    if (!(target > 0)) return toast("Enter a target above zero", true);
    const lines = g.members.filter((m) => m.balance !== null && Number(m.balance) < target)
      .map((m) => `${m.address},${(Math.ceil((target - Number(m.balance)) * 1e6) / 1e6).toFixed(6)}`);
    if (!lines.length) return toast("Every wallet already has at least that much.");
    closeModal(); S.tx.tab = "distribute"; S.tx.preview = null;
    S.tx.dist = { asset: "native", mode: "custom", text: `address,amount\n${lines.join("\n")}` };
    go("transfers"); toast(`${lines.length} wallet(s) need a top-up. Pick the paying wallet, then Preview.`);
  },
  "g-options": (el) => {
    const g = groupByName(el.dataset.name);
    const all = (S.walletData?.wallets || []);
    guard(async () => {
      const w = await api(`/api/wallets?chain_id=${S.chainId}`);
      modal(`<h2>Role &amp; rules — ${esc(g.name)}</h2>
        <label>Role<select id="go-role">${options(Object.entries(G.data.roles), g.role, (r) => r[1], (r) => r[0])}</select></label>
        <label>Vault wallet for this group (optional — otherwise the first wallet of a Vault group)
          <select id="go-vault"><option value="">${G.data.groups.some((x) => x.role === "vault") ? "Use the Vault group" : "None"}</option>${options(w.wallets, g.own_vault || "", (x) => `${x.label || ""} ${short(x.address)}`, (x) => x.address)}</select></label>
        <label>After a successful mint<select id="go-after">${options(Object.entries(G.data.after_mint), g.after_mint, (r) => r[1], (r) => r[0])}</select></label>
        <div class="tiny muted">“Send to the vault wallet” transfers the NFTs each wallet in this group just minted (OpenSea drops) to the vault, automatically, as a normal previewed transfer. It costs a little gas per NFT. Your spending limits and STOP still apply.</div>
        <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="g-options-go" data-name="${esc(g.name)}">Save</button></div>`);
    });
    void all;
  },
  "g-options-go": (el) => guard(async () => {
    const after = $("#go-after").value, g = groupByName(el.dataset.name);
    if (after === "vault" && g.after_mint !== "vault" && !await askConfirm("Send minted NFTs to the vault automatically?",
      `<p>After every successful mint, wallets in <b>${esc(g.name)}</b> will send the NFTs they minted to the vault wallet without asking again.</p><p class="tiny muted">You can switch this back to “Keep” any time.</p>`, "Yes, send to vault")) return;
    await api("/api/groups", { action: "options", name: el.dataset.name, role: $("#go-role").value, vault: $("#go-vault").value, after_mint: after });
    closeModal(); toast("Saved"); render();
  }),
  "g-split": () => modal(`<h2>Split a group into batches</h2>
    <label>Group<select id="gs-name">${options([...G.data.groups.map((g) => g.name), ...G.data.smart.map((g) => g.name)], "")}</select></label>
    <label>Wallets per batch<input id="gs-size" type="number" min="1" value="25"></label>
    <label>Name prefix (new groups are prefix-1, prefix-2, …)<input id="gs-prefix" placeholder="batch"></label>
    <div class="tiny muted">Wallets are copied into the new groups; the original group stays as it is.</div>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="g-split-go">Split</button></div>`),
  "g-split-go": () => guard(async () => {
    const r = await api("/api/groups", { action: "split", name: $("#gs-name").value, size: Number($("#gs-size").value), prefix: $("#gs-prefix").value });
    closeModal(); toast(`Created ${r.created.join(", ")}`); render();
  }),
  "g-move": () => modal(`<h2>Move ${S.selected.size} selected wallet(s)</h2>
    <label>From group<select id="gm-from">${options(G.data.groups.map((g) => g.name), "")}</select></label>
    <label>To group (new name creates it)<input id="gm-to" list="gm-list"><datalist id="gm-list">${G.data.groups.map((g) => `<option value="${esc(g.name)}">`).join("")}</datalist></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button data-action="g-copy-go">Copy</button><button class="primary" data-action="g-move-go">Move</button></div>`),
  "g-move-go": () => guard(async () => {
    await api("/api/groups", { action: "move", addresses: [...S.selected], from: $("#gm-from").value, name: $("#gm-to").value });
    closeModal(); toast("Moved"); render();
  }),
  "g-copy-go": () => guard(async () => {
    await api("/api/groups", { action: "add", addresses: [...S.selected], name: $("#gm-to").value });
    closeModal(); toast("Copied"); render();
  }),
  "g-smart-settings": () => modal(`<h2>Low-gas threshold</h2>
    <p class="muted">Wallets below this native balance appear in “★ Low on gas” and count as low gas in group cards.</p>
    <label>Network${chainSelect("gss-chain", G.data.smart_settings.chain_id)}</label>
    <label>Threshold<input id="gss-low" value="${esc(G.data.smart_settings.low_gas)}"></label>
    <div class="row"><span class="spacer"></span><button data-action="close">Cancel</button><button class="primary" data-action="g-smart-go">Save</button></div>`),
  "g-smart-go": () => guard(async () => {
    await api("/api/groups/smart-settings", { chain_id: Number($("#gss-chain").value), low_gas: $("#gss-low").value });
    closeModal(); render();
  }),
});

// Minting role: the Eligibility page starts with that group selected (once).
const elig652 = PAGE_RENDER.eligibility;
PAGE_RENDER.eligibility = async () => {
  if (S65.elig.group === undefined) {
    S65.elig.group = "";
    try {
      const w = await api(`/api/wallets?chain_id=${S.chainId}`);
      const minting = Object.entries(w.roles || {}).find(([, r]) => r === "minting");
      if (minting) S65.elig.group = minting[0];
    } catch { /* keep default */ }
  }
  await elig652();
};
