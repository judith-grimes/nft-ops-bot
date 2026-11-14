"use strict";
// Stage 6.5.1: collections inside Eligibility, any-marketplace Mint (links / contracts),
// per-wallet Offers and Portfolio in USD, two-tone page titles, nav counts.

const S651 = { wallet: localStorageGet("nftops.wallet") || "", mintTab: "opensea", linkMsg: "" };
const TITLE_PARTS = { overview: ["Overview", "Dashboard"], eligibility: ["Eligibility", "Checker"], mint: ["Mint", "Center"],
  schedules: ["Scheduled", "Mints"], calendar: ["Mint", "Calendar"], offers: ["Offer", "Manager"], portfolio: ["Portfolio", "Value"],
  wallets: ["Wallet", "Manager"], transfers: ["Transfers", ""], history: ["Transaction", "History"], notifications: ["Notifications", ""],
  networks: ["Networks", ""], settings: ["Settings", ""], collections: ["Collections", ""] };
const fmtUSD = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—"
  : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: Math.abs(v) < 1 ? 4 : 2 })}`);
const AVATAR_COLORS = ["#3b2f5c", "#3d3a1f", "#1f3a2c", "#2b3245", "#45282c", "#203b3f"];
function avatar(text) {
  const t = String(text || "?"), i = [...t].reduce((a, c) => a + c.charCodeAt(0), 0) % AVATAR_COLORS.length;
  return `<span class="avatar" style="background:${AVATAR_COLORS[i]}">${esc(t.replace(/^0x/, "").slice(0, 1).toUpperCase())}</span>`;
}

// ---------------------------------------------------------------- shared render hook
const render651Base = render;
render = async function render651() {
  await render651Base();
  const parts = TITLE_PARTS[S.page], h1 = $("#main .page-head h1");
  if (parts && h1) h1.innerHTML = `${esc(parts[0])}${parts[1] ? ` <span class="muted">${esc(parts[1])}</span>` : ""}`;
  api("/api/schedules").then((d) => {
    const n = d.tasks.filter((t) => ["scheduled", "monitoring", "awaiting_confirmation", "running", "paused"].includes(t.status)).length;
    const c = $("#nav-count-schedules"); if (c) { c.textContent = n; c.classList.toggle("hidden", !n); }
  }).catch(() => {});
};

// ---------------------------------------------------------------- link / slug / contract router
const ADDRESS_RE = /0x[0-9a-fA-F]{40}/;
function classifySource(text) {
  const t = text.trim();
  if (!t) return { kind: "empty" };
  if (/opensea\.io/i.test(t) || /^[a-z0-9][a-z0-9-_]{1,80}$/i.test(t) && !t.startsWith("0x")) return { kind: "opensea", locator: t };
  const m = t.match(ADDRESS_RE);
  if (m) return { kind: "contract", address: m[0] };
  if (/^https?:\/\//i.test(t)) {
    const host = (() => { try { return new URL(t).hostname.replace(/^www\./, ""); } catch { return "this site"; } })();
    return { kind: "nolink", host };
  }
  return { kind: "unknown" };
}
const NOLINK_HELP = (host) => `${host} links don't contain the contract address. Open the mint page, find the contract (usually a link to the block explorer, or in the page details) and paste that 0x… address here.`;

// ---------------------------------------------------------------- ELIGIBILITY: collections live here now
function sourcePanel(prefix, hint) {
  return `<section class="panel"><div class="section-head"><h2>Add a collection</h2><button class="small ghost" data-action="goto" data-page="collections">Manage collections</button></div>
    <div class="source-box">
      <label>OpenSea link, collection slug, or contract address<input id="${prefix}-src" placeholder="https://opensea.io/collection/…  ·  my-collection  ·  0x…" value=""></label>
      <label style="flex:0 0 auto">Network (for a contract address)${chainSelect(`${prefix}-chain`, S.chainId)}</label>
      <button class="primary" data-action="${prefix}-load">Load phases</button></div>
    <div class="tiny muted" style="margin-top:8px">${hint}</div><div id="${prefix}-msg"></div></section>`;
}
const elig65 = PAGE_RENDER.eligibility;
PAGE_RENDER.eligibility = async () => {
  await elig65();
  const main = $("#main"), head = $(".page-head", main);
  const cols = S.collections || [];
  if (!cols.length) {
    const empty = $(".panel.empty", main); if (empty) empty.remove();
    head.insertAdjacentHTML("afterend", sourcePanel("el", "Paste the drop you want to check. Its phases and network are detected automatically, then scan your wallets."));
    return;
  }
  const col = cols.find((c) => c.slug === S.elig.slug) || cols[0];
  const rows = S65.eligRows || [];
  const elig = (i) => new Set(rows.filter((r) => r.stage_index === i && r.status === "ELIGIBLE").map((r) => r.address)).size;
  const phases = `<section class="panel"><div class="section-head"><h2>${esc(col.name || col.slug)} <span class="muted" style="font-weight:400">· ${esc(col.chain_name)}</span></h2>
      <a class="tiny" href="${esc(col.opensea_url)}" target="_blank" rel="noopener">Open on OpenSea ↗</a></div>
    <div class="phases">${col.stages.map((s) => `<button class="phase-card ${String(S65.elig.stage) === String(s.index) ? "on" : ""}" data-action="el-phase" data-stage="${s.index}">
      <span class="row" style="justify-content:space-between;align-items:center"><span class="t">${esc(s.name)}</span>${badge(s.state)}</span>
      <span class="tiny muted">${esc(phaseType(s.type))} · ${s.price_wei == null ? "—" : `${esc(weiToStr(s.price_wei))} ${esc(symbol(col.chain_id))}`} · max ${s.max_per_wallet ?? "—"}/wallet</span>
      <span class="tiny muted">${s.state === "upcoming" ? `opens in ${countdown(s.starts_at)}` : esc(inTz(s.starts_at, tzName()))}</span>
      <span><span class="eligible">${elig(s.index)}</span> <span class="tiny muted">eligible wallet${elig(s.index) === 1 ? "" : "s"} — click to list them</span></span></button>`).join("")}</div></section>`;
  head.insertAdjacentHTML("afterend", sourcePanel("el", "Add another drop here; pick it in the collection list below.") + phases);
};
async function loadCollection(prefix, onDone) {
  const src = classifySource($(`#${prefix}-src`).value), msg = $(`#${prefix}-msg`);
  msg.innerHTML = "";
  if (src.kind === "nolink") { msg.innerHTML = `<div class="notice warn">${esc(NOLINK_HELP(src.host))}</div>`; return; }
  if (src.kind === "empty" || src.kind === "unknown") { msg.innerHTML = `<div class="notice warn">Paste an OpenSea link, a collection slug or a 0x… contract address.</div>`; return; }
  const locator = src.kind === "contract" ? src.address : src.locator;
  try {
    const r = await api("/api/collections", { locator, chain_id: Number($(`#${prefix}-chain`).value) });
    toast(`Loaded ${r.collection.slug}: ${r.collection.stages.length} phase${r.collection.stages.length === 1 ? "" : "s"}`);
    await onDone(r.collection);
  } catch (e) {
    if (src.kind === "contract") {
      msg.innerHTML = `<div class="notice warn">This contract is not an OpenSea drop (${esc(e.message)}). You can still mint it from <b>Mint → Any contract</b>.
        <button class="small" data-action="mint-contract-from" data-address="${esc(src.address)}" data-chain="${esc($(`#${prefix}-chain`).value)}">Open in Mint</button></div>`;
    } else msg.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}
Object.assign(ACTIONS_EXTRA, {
  "el-load": (el) => guard(() => withBusy(el, "Loading…", () => loadCollection("el", async (c) => { S.elig.slug = c.slug; S65.elig.stage = "ALL"; S65.elig.status = "ALL"; render(); }))),
  "el-phase": (el) => { S65.elig.stage = el.dataset.stage; S65.elig.status = "ELIGIBLE"; S65.elig.page = 0; render().then(() => { const t = $("#main table"); if (t) t.scrollIntoView({ behavior: "smooth", block: "center" }); }); },
});

// ---------------------------------------------------------------- MINT: OpenSea drops + any contract
const mintBase = PAGE_RENDER.mint, contractsBase = PAGE_RENDER.contracts;
PAGE_RENDER.mint = async () => {
  if (S651.mintTab === "contract") await contractsBase(); else await mintBase();
  const head = $("#main .page-head");
  if (!head) return;
  const h1 = $("h1", head); if (h1) h1.textContent = "Mint";
  head.insertAdjacentHTML("afterend", `
    <div class="tabs"><button class="${S651.mintTab === "opensea" ? "on" : ""}" data-action="mn-tab" data-tab="opensea">OpenSea drops</button>
      <button class="${S651.mintTab === "contract" ? "on" : ""}" data-action="mn-tab" data-tab="contract">Any contract <span class="n">LaunchMyNFT · Kalos · Scatter · others</span></button></div>`);
  S651.linkMsg = "";
  if (S651.autoAnalyse && S651.mintTab === "contract") { S651.autoAnalyse = false; const b = $("[data-action=c-analyze]"); if (b) b.click(); }
};
PAGE_RENDER.contracts = async () => { S651.mintTab = "contract"; S.page = "mint"; history.replaceState(null, "", "#mint"); await render(); };
function openContract(address, chainId) {
  S.c.address = address; S.c.report = null;
  if (chainId) { S.chainId = Number(chainId); localStorageSet("nftops.chain", String(chainId)); }
  S651.mintTab = "contract"; S651.autoAnalyse = true; closeModal(); go("mint");
}
Object.assign(ACTIONS_EXTRA, {
  "mn-tab": (el) => { S651.mintTab = el.dataset.tab; render(); },
  "mint-contract-from": (el) => openContract(el.dataset.address, el.dataset.chain),
  "mn-go": (el) => guard(() => withBusy(el, "Checking…", async () => {
    const src = classifySource($("#mn-src").value), chain = Number($("#mn-chain").value);
    if (src.kind === "opensea") {
      const r = await api("/api/collections", { locator: src.locator, chain_id: chain });
      S.mint.slug = r.collection.slug; S.mint.stage = ""; S.mint.preview = null; S651.mintTab = "opensea";
      toast(`Loaded ${r.collection.slug}`); return render();
    }
    if (src.kind === "contract") {
      try {   // an OpenSea drop by contract? use the drop flow; otherwise the generic contract flow
        const r = await api("/api/collections", { locator: src.address, chain_id: chain });
        S.mint.slug = r.collection.slug; S.mint.stage = ""; S651.mintTab = "opensea"; toast(`OpenSea drop found: ${r.collection.slug}`); return render();
      } catch { return openContract(src.address, chain); }
    }
    S651.linkMsg = src.kind === "nolink" ? NOLINK_HELP(src.host) : "Paste a mint link or a 0x… contract address.";
    render();
  })),
});

// ---------------------------------------------------------------- per-wallet tabs (Offers + Portfolio)
function walletTabs(wallets, action) {
  if (!wallets.length) return "";
  if (S651.wallet && !wallets.some((w) => w.address === S651.wallet)) S651.wallet = "";
  return `<div class="tabs" style="flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px">
    <button class="${S651.wallet ? "" : "on"}" data-action="${action}" data-wallet="">All wallets</button>
    ${wallets.map((w) => `<button class="${S651.wallet === w.address ? "on" : ""}" data-action="${action}" data-wallet="${esc(w.address)}" title="${esc(w.address)}">${esc(w.label || short(w.address))}<span class="n">${w.nfts} NFT${w.nfts === 1 ? "" : "s"}</span></button>`).join("")}</div>`;
}
const pickWallet = (el) => { S651.wallet = el.dataset.wallet; localStorageSet("nftops.wallet", S651.wallet); render(); };
ACTIONS_EXTRA["wallet-tab"] = pickWallet;

const offersBase = PAGE_RENDER.offers;
PAGE_RENDER.offers = async () => {
  if (S.ofFilter) S.ofFilter.wallet = "";
  const apiOrig = api;
  api = (p, b, m) => apiOrig(p === "/api/offers" && S651.wallet ? `/api/offers?wallet=${encodeURIComponent(S651.wallet)}` : p, b, m);
  try { await offersBase(); } finally { api = apiOrig; }
  const d = S.offers || {}, head = $("#main .page-head");
  if (!head) return;
  const wsel = $("#of-wallet"); if (wsel) wsel.closest("label").remove();
  head.insertAdjacentHTML("afterend", `${walletTabs(d.wallets || [], "wallet-tab")}
    <div class="tiny muted" style="margin:-6px 0 14px">${S651.wallet ? `Offers on NFTs held by <b>${esc(((d.wallets || []).find((w) => w.address === S651.wallet) || {}).label || short(S651.wallet))}</b>.` : "Showing every wallet. Pick a wallet to focus on its NFTs."}
      USD values use OpenSea's live price where available.</div>`);
  const usd = d.usd || {};
  $$("#main table td.num b").forEach((b) => {   // add a USD line under "You receive"
    const tr = b.closest("tr"); const key = tr && tr.querySelector("[data-key]");
    const o = key && (d.offers || []).find((x) => ofKey(x) === key.dataset.key);
    const price = o && usd[String(o.chain_id)];
    if (price && o.net != null) b.insertAdjacentHTML("afterend", `<div class="tiny muted">${fmtUSD(o.net * price)}</div>`);
  });
};

// Portfolio, rebuilt: per wallet, every value in USD (stable) instead of mixed native coins.
async function renderPortfolio651() {
  const d = await api(`/api/portfolio${S651.wallet ? `?wallet=${encodeURIComponent(S651.wallet)}` : ""}`);
  const rows = d.collections, missing = rows.filter((r) => r.chain_id && d.usd[String(r.chain_id)] == null);
  const sum = (k) => rows.reduce((a, r) => a + (r[`${k}_usd`] || 0), 0);
  const value = sum("value"), spent = sum("spent"), received = sum("received"), pnl = received + value - spent;
  const pct = (a, b) => (b ? ((a - b) / b) * 100 : null);
  const ch = (v) => (v == null ? `<span class="muted">—</span>` : `<span class="${v >= 0 ? "up" : "down"}">${v >= 0 ? "↑" : "↓"} ${Math.abs(v).toFixed(1)}%</span>`);
  $("#main").innerHTML = `
    <div class="page-head"><h1>Portfolio</h1><button class="primary" data-action="pf-refresh">Update from OpenSea</button></div>
    ${d.api_key ? "" : `<div class="notice warn">Add your OpenSea API key in Settings to see holdings, floor prices and USD values.</div>`}
    ${walletTabs(d.wallets || [], "wallet-tab")}
    <div class="kpis">
      <div class="kpi"><div class="k">Value at floor</div><div class="v">${fmtUSD(value)}</div><div class="s">${rows.reduce((a, r) => a + r.held, 0)} NFTs held</div></div>
      <div class="kpi"><div class="k">Spent minting</div><div class="v">${fmtUSD(spent)}</div><div class="s">mint price + gas</div></div>
      <div class="kpi"><div class="k">Received from sales</div><div class="v">${fmtUSD(received)}</div><div class="s">accepted offers, after fees</div></div>
      <div class="kpi ${pnl >= 0 ? "good" : "bad"}"><div class="k">Profit / loss</div><div class="v">${fmtUSD(pnl)}</div><div class="s">${spent ? ch(pct(received + value, spent)) + " vs spent" : "received + value − spent"}</div></div>
    </div>
    ${missing.length ? `<div class="notice warn">No USD price is available for ${esc([...new Set(missing.map((r) => network(r.chain_id)?.name || r.chain_id))].join(", "))}, so those collections show “—” in USD and are not in the totals.</div>` : ""}
    <section class="panel"><div class="section-head"><h2>Collections</h2><span class="tiny muted">All values in USD at today's price. Floor is shown next to recent sales, because one cheap listing can make the floor misleading.</span></div>
    ${rows.length ? `<div class="table-wrap"><table class="responsive"><thead><tr><th>Collection</th><th class="num">Held</th><th class="num">Floor</th><th class="num">Recent sales</th><th class="num">24 h</th><th>7-day floor</th><th class="num">Value</th><th class="num">Spent</th><th class="num">Received</th><th class="num">P&amp;L</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td><div class="row" style="align-items:center;flex-wrap:nowrap">${avatar(r.name || r.slug)}<div><b>${esc(r.name || r.slug)}</b><div class="tiny muted">${esc(network(r.chain_id)?.name || "")}</div></div></div></td>
        <td class="num" data-label="Held">${r.held}</td><td class="num" data-label="Floor">${fmtUSD(r.floor_usd)}</td><td class="num" data-label="Sales">${fmtUSD(r.sales_median_usd)}</td>
        <td class="num" data-label="24h">${ch(r.change_24h)}</td><td>${spark(r.history || [])}</td>
        <td class="num" data-label="Value"><b>${fmtUSD(r.value_usd)}</b></td><td class="num" data-label="Spent">${fmtUSD(r.spent_usd)}</td>
        <td class="num" data-label="Received">${fmtUSD(r.received_usd)}</td><td class="num" data-label="P&L"><span class="${(r.pnl_usd || 0) >= 0 ? "up" : "down"}">${fmtUSD(r.pnl_usd)}</span></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><b>No NFTs found${S651.wallet ? " in this wallet" : ""}.</b><br>Click <b>Update from OpenSea</b> to read your holdings.</div>`}
    </section>`;
}
PAGE_RENDER.portfolio = renderPortfolio651;
