"use strict";
// Stage 6.6: Scatter (Archetype) drops in Mint → Any contract. Phases are read from the
// contract; allowlist proofs are built from a pasted allowlist (or pasted proofs) and verified
// against the on-chain root; minting goes through the normal transaction preview.

const S66 = { info: {}, phase: "", source: "all", group: "", qty: 1, schedule: true, busy: false };
const scKey = () => `${S.chainId}:${(S.c.address || "").toLowerCase()}`;

const analyse66 = ACTIONS_EXTRA["c-analyze"];
ACTIONS_EXTRA["c-analyze"] = (el) => {
  const address = ($("#c-address") || {}).value || S.c.address, chain = Number(($("#c-chain") || {}).value || S.chainId);
  analyse66(el);
  api("/api/scatter/inspect", { chain_id: chain, address: address.trim() })
    .then((info) => {
      const k = `${chain}:${address.trim().toLowerCase()}`;
      if (!S66.info[k] || !(info.phases || []).some((p) => p.key === S66.phase)) S66.phase = "";
      S66.info[k] = info; if (S.page === "mint") render();
    })
    .catch(() => {});
};

async function scatterRefresh() {
  const info = await api("/api/scatter/inspect", { chain_id: S.chainId, address: S.c.address });
  S66.info[scKey()] = info;
}

const mint66 = PAGE_RENDER.mint;
PAGE_RENDER.mint = async () => {
  await mint66();
  const info = S66.info[scKey()];
  if (S651.mintTab !== "contract" || !info || !info.is_scatter) return;
  const tabs = $("#main .tabs"); if (!tabs) return;
  const anchor = (tabs.nextElementSibling && tabs.nextElementSibling.classList.contains("panel")) ? tabs.nextElementSibling : tabs;
  const w = await api(`/api/wallets?chain_id=${S.chainId}`);
  const sym = symbol(S.chainId), tz = tzName();
  const live = info.phases.filter((p) => p.state !== "ended");
  if (!info.phases.some((p) => p.key === S66.phase)) S66.phase = (live[0] || info.phases[0] || {}).key || "";
  const ph = info.phases.find((p) => p.key === S66.phase);
  anchor.insertAdjacentHTML("afterend", `
    <section class="panel">
      <div class="section-head"><h2>Scatter drop <span class="muted" style="font-weight:400">· ${esc(info.name || short(info.contract))}</span></h2>
        <span class="badge good">Detected from the contract</span>${info.total_supply != null ? `<span class="tiny muted">${info.total_supply} minted so far</span>` : ""}</div>
      ${info.events_ok ? "" : `<div class="notice warn">This RPC would not return the drop's phase history, so only public phases were found. Add another RPC on the Networks page if an allowlist phase is missing.</div>`}
      <div class="phases">${info.phases.map((p) => `<button class="phase-card ${p.key === S66.phase ? "on" : ""}" data-action="sc-phase" data-key="${p.key}">
        <span class="row" style="justify-content:space-between;align-items:center"><span class="t">${esc(p.name)}</span>${badge(p.state)}</span>
        <span class="tiny muted">${p.public ? "Open to every wallet" : "Allowlist (Merkle)"} · ${p.native ? `${esc(weiToStr(p.price_wei))} ${esc(sym)}` : "paid in a token"} · max ${p.limit || "—"}/wallet</span>
        <span class="tiny muted">${p.start ? (p.state === "upcoming" ? `opens in ${countdown(p.start)}` : `opened ${esc(inTz(p.start, tz))}`) : "no start time"}${p.end ? ` · ends ${esc(inTz(p.end, tz))}` : ""}</span>
        ${p.public ? "" : `<span class="tiny ${p.proofs_loaded ? "up" : "muted"}">${p.proofs_loaded ? `${p.proofs_loaded} verified proof(s) loaded` : "allowlist not loaded yet"}</span>`}
      </button>`).join("") || `<div class="empty">No phases found on this contract yet.</div>`}</div>
      ${ph ? `
      <div class="row" style="margin-top:16px">
        <label>Wallets<select id="sc-src"><option value="all" ${S66.source === "all" ? "selected" : ""}>All wallets with keys</option>
          <option value="group" ${S66.source === "group" ? "selected" : ""}>A group</option><option value="selected" ${S66.source === "selected" ? "selected" : ""}>Selected wallets (${S.selected.size})</option></select></label>
        ${S66.source === "group" ? `<label>Group<select id="sc-grp">${options(w.groups, S66.group, (g) => `${g.name} (${g.size})`, (g) => g.name)}</select></label>` : ""}
        <label>Quantity / wallet<input id="sc-qty" type="number" min="1" value="${esc(S66.qty)}" style="width:90px"></label>
        ${ph.state === "upcoming" ? `<label class="radio" style="align-self:center"><input type="checkbox" id="sc-sched" ${S66.schedule ? "checked" : ""}> Send at the start time</label>` : ""}
        <span class="spacer"></span>
        <button class="primary" data-action="sc-preview" ${ph.native && ph.state !== "ended" ? "" : "disabled"}>Preview mint</button></div>
      ${!ph.native ? `<div class="notice warn">This phase is paid in an ERC-20 token. That needs a token approval first and is not supported yet.</div>` : ""}
      ${ph.public ? "" : `<details ${ph.proofs_loaded ? "" : "open"} style="margin-top:12px"><summary>Load the allowlist for this phase</summary>
        <p class="tiny muted">Paste the full allowlist (all addresses, as the project published it) — NFT Ops rebuilds the Merkle tree and only accepts it if it matches the root stored in the contract. Or paste proofs as <span class="mono">address, 0xproof1 0xproof2 …</span> lines (each is checked too).</p>
        <textarea id="sc-list" rows="5" placeholder="0x… one per line, or a CSV export"></textarea>
        <div class="row" style="margin-top:8px"><button data-action="sc-allowlist">Check &amp; load</button></div></details>`}` : ""}
      <div class="tiny muted" style="margin-top:10px">Price shown is per NFT; the exact amount (including Scatter's protocol fee) is read from the contract in the preview. Each wallet is test-run before anything is sent.</div>
    </section>`);
  const bind = (id, fn) => { const e = $(id); if (e) e.onchange = fn; };
  bind("#sc-src", (e) => { S66.source = e.target.value; render(); });
  bind("#sc-grp", (e) => { S66.group = e.target.value; });
  bind("#sc-qty", (e) => { S66.qty = Number(e.target.value) || 1; });
  bind("#sc-sched", (e) => { S66.schedule = e.target.checked; });
  // The generic analyser cannot read Archetype's struct-based mint; fold its report away.
  for (const panel of document.querySelectorAll("#main .panel")) {
    if (panel.textContent.includes("Mint functions found") && !panel.dataset.folded) {
      panel.dataset.folded = "1"; panel.style.display = "none";
      panel.insertAdjacentHTML("beforebegin", `<div class="tiny muted sc-fold" style="margin:-6px 0 14px">The generic contract report is hidden because the Scatter adapter above handles this drop. <a href="#" data-action="sc-show-report">Show it</a></div>`);
    }
  }
};
function scSelection() {
  if (S66.source === "group") return { group: S66.group || ($("#sc-grp") || {}).value };
  if (S66.source === "selected") return { addresses: [...S.selected] };
  return {};
}
Object.assign(ACTIONS_EXTRA, {
  "sc-show-report": (el) => { const note = el.closest(".sc-fold"); if (note && note.nextElementSibling) note.nextElementSibling.style.display = ""; if (note) note.style.display = "none"; },
  "sc-phase": (el) => { S66.phase = el.dataset.key; render(); },
  "sc-allowlist": (el) => guard(() => withBusy(el, "Checking…", async () => {
    const r = await api("/api/scatter/allowlist", { chain_id: S.chainId, address: S.c.address, key: S66.phase, text: $("#sc-list").value });
    toast(`Allowlist matches the contract: ${r.verified} proof(s) verified, ${r.yours} of your wallets are on it.`);
    await scatterRefresh(); render();
  })),
  "sc-preview": (el) => guard(() => withBusy(el, "Checking wallets…", async () => {
    S66.qty = Number(($("#sc-qty") || {}).value || S66.qty);
    const p = await api("/api/scatter/preview", { chain_id: S.chainId, address: S.c.address, key: S66.phase, quantity: S66.qty,
      schedule: !!($("#sc-sched") ? $("#sc-sched").checked : false), ...scSelection() });
    p.created = Date.now() / 1000; S.tx.preview = p;
    const box = $("#c-preview"); if (box) { box.innerHTML = txPreviewHTML(p); box.scrollIntoView({ behavior: "smooth", block: "start" }); }
  })),
});
