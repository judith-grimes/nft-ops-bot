"use strict";
// Stage 6.6: Scatter (Archetype) drops get their own Mint tab, next to OpenSea drops.
// Phases are read from the contract; allowlist proofs are built from a pasted allowlist (or
// pasted proofs) and verified against the on-chain root; minting goes through the normal
// transaction preview (test run, spending limits, STOP).

const S66 = { info: null, address: localStorageGet("nftops.scatter.address") || "", chain: Number(localStorageGet("nftops.scatter.chain") || 4663),
  phase: "", source: "all", group: "", qty: 1, schedule: true };

function mintTabsHTML() {
  const t = S651.mintTab;
  return `<div class="tabs">
    <button class="${t === "opensea" ? "on" : ""}" data-action="mn-tab" data-tab="opensea">OpenSea drops</button>
    <button class="${t === "scatter" ? "on" : ""}" data-action="mn-tab" data-tab="scatter">Scatter drops</button>
    <button class="${t === "contract" ? "on" : ""}" data-action="mn-tab" data-tab="contract">Any contract <span class="n">LaunchMyNFT · Kalos · others</span></button></div>`;
}

// Any contract: if the pasted contract turns out to be a Scatter drop, offer to open it there.
const analyse66 = ACTIONS_EXTRA["c-analyze"];
ACTIONS_EXTRA["c-analyze"] = (el) => {
  const address = (($("#c-address") || {}).value || S.c.address || "").trim(), chain = Number(($("#c-chain") || {}).value || S.chainId);
  analyse66(el);
  api("/api/scatter/inspect", { chain_id: chain, address }).then((info) => {
    if (!info.is_scatter) return;
    S66.detected = { address, chain };
    if (S.page === "mint") render();
  }).catch(() => {});
};

async function loadScatter() {
  if (!S66.address) { S66.info = null; return; }
  S66.info = await api("/api/scatter/inspect", { chain_id: S66.chain, address: S66.address });
  if (S66.info.is_scatter && !S66.info.phases.some((p) => p.key === S66.phase)) {
    const live = S66.info.phases.filter((p) => p.state !== "ended");
    S66.phase = (live[0] || S66.info.phases[0] || {}).key || "";
  }
}

const mint66 = PAGE_RENDER.mint;
PAGE_RENDER.mint = async () => {
  if (S651.mintTab === "scatter") return renderScatterTab();
  await mint66();
  const tabs = $("#main .tabs"); if (tabs) tabs.outerHTML = mintTabsHTML();
  const d = S66.detected;
  if (S651.mintTab === "contract" && d && d.address.toLowerCase() === (S.c.address || "").toLowerCase()) {
    const t = $("#main .tabs");
    if (t) t.insertAdjacentHTML("afterend", `<div class="notice info">This contract is a <b>Scatter drop</b>. The Scatter tab reads its phases, prices and allowlist directly.
      <button class="small primary" data-action="sc-open" data-address="${esc(d.address)}" data-chain="${d.chain}">Open in Scatter drops</button></div>`);
  }
};

async function renderScatterTab() {
  try { if (S66.address && (!S66.info || S66.info.contract?.toLowerCase() !== S66.address.toLowerCase())) await loadScatter(); }
  catch (e) { S66.error = e.message; S66.info = null; }
  const info = S66.info;
  $("#main").innerHTML = `
    <div class="page-head"><h1>Mint</h1></div>
    ${mintTabsHTML()}
    <section class="panel">
      <div class="source-box">
        <label style="flex:0 0 auto">Network${chainSelect("sc-chain", S66.chain)}</label>
        <label>Scatter drop contract address<input id="sc-address" class="mono" placeholder="0x…" value="${esc(S66.address)}"></label>
        <button class="primary" data-action="sc-load">Load drop</button></div>
      <div class="tiny muted" style="margin-top:8px">Find the contract on the drop's scatter.art page (it links to the block explorer). Everything — phases, prices, limits, allowlist root — is read from the contract itself.</div>
      ${S66.error ? `<div class="notice bad">${esc(S66.error)}</div>` : ""}
      ${info && !info.is_scatter ? `<div class="notice warn">This contract is not a Scatter (Archetype) drop. Try the <b>Any contract</b> tab.</div>` : ""}
    </section>
    <div id="sc-root"></div>
    <div id="c-preview">${S.tx.preview && S.tx.preview.kind === "custom_mint" && S.tx.preview.title === "Scatter mint" ? txPreviewHTML(S.tx.preview) : ""}</div>
    <div id="txjobs"></div>`;
  S66.error = "";
  if (info && info.is_scatter) await drawScatterPanel(info);
  else if (!S66.address) $("#sc-root").innerHTML = `<div class="panel empty"><b>Paste a Scatter drop's contract address above.</b><br>Public and allowlist phases, price (with Scatter's fee) and per-wallet limits appear here.</div>`;
  await renderTxJobs();
}

async function drawScatterPanel(info) {
  const w = await api(`/api/wallets?chain_id=${S66.chain}`);
  const sym = symbol(S66.chain), tz = tzName();
  const ph = info.phases.find((p) => p.key === S66.phase);
  $("#sc-root").innerHTML = `
    <section class="panel">
      <div class="section-head"><h2>${esc(info.name || "Scatter drop")} <span class="muted" style="font-weight:400">· ${esc(network(S66.chain)?.name || "")}</span></h2>
        <span class="badge good">Scatter · read from the contract</span>${info.total_supply != null ? `<span class="tiny muted">${info.total_supply} minted so far</span>` : ""}</div>
      ${info.events_ok ? "" : `<div class="notice warn">This RPC would not return the drop's phase history, so only public phases were found. Add another RPC on the Networks page if an allowlist phase is missing.</div>`}
      <div class="phases">${info.phases.map((p) => `<button class="phase-card ${p.key === S66.phase ? "on" : ""}" data-action="sc-phase" data-key="${p.key}">
        <span class="row" style="justify-content:space-between;align-items:center"><span class="t">${esc(p.name)}</span>${badge(p.state)}</span>
        <span class="tiny muted">${p.public ? "Open to every wallet" : "Allowlist"} · ${p.native ? `${esc(weiToStr(p.price_wei))} ${esc(sym)}` : "paid in a token"} · max ${p.limit || "—"}/wallet</span>
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
    </section>`;
  const bind = (id, fn) => { const e = $(id); if (e) e.onchange = fn; };
  bind("#sc-src", (e) => { S66.source = e.target.value; render(); });
  bind("#sc-grp", (e) => { S66.group = e.target.value; });
  bind("#sc-qty", (e) => { S66.qty = Number(e.target.value) || 1; });
  bind("#sc-sched", (e) => { S66.schedule = e.target.checked; });
}

function scSelection() {
  if (S66.source === "group") return { group: S66.group || ($("#sc-grp") || {}).value };
  if (S66.source === "selected") return { addresses: [...S.selected] };
  return {};
}
Object.assign(ACTIONS_EXTRA, {
  "sc-load": (el) => guard(() => withBusy(el, "Reading contract…", async () => {
    S66.address = $("#sc-address").value.trim(); S66.chain = Number($("#sc-chain").value);
    localStorageSet("nftops.scatter.address", S66.address); localStorageSet("nftops.scatter.chain", String(S66.chain));
    S66.info = null; S66.phase = ""; S.tx.preview = null;
    await render();
  })),
  "sc-open": (el) => { S66.address = el.dataset.address; S66.chain = Number(el.dataset.chain); S66.info = null; S66.phase = ""; S651.mintTab = "scatter"; render(); },
  "sc-phase": (el) => { S66.phase = el.dataset.key; render(); },
  "sc-allowlist": (el) => guard(() => withBusy(el, "Checking…", async () => {
    const r = await api("/api/scatter/allowlist", { chain_id: S66.chain, address: S66.address, key: S66.phase, text: $("#sc-list").value });
    toast(`Allowlist matches the contract: ${r.verified} proof(s) verified, ${r.yours} of your wallets are on it.`);
    await loadScatter(); render();
  })),
  "sc-preview": (el) => guard(() => withBusy(el, "Checking wallets…", async () => {
    S66.qty = Number(($("#sc-qty") || {}).value || S66.qty);
    const p = await api("/api/scatter/preview", { chain_id: S66.chain, address: S66.address, key: S66.phase, quantity: S66.qty,
      schedule: !!($("#sc-sched") ? $("#sc-sched").checked : false), ...scSelection() });
    p.created = Date.now() / 1000; S.tx.preview = p;
    const box = $("#c-preview"); if (box) { box.innerHTML = txPreviewHTML(p); box.scrollIntoView({ behavior: "smooth", block: "start" }); }
  })),
});
