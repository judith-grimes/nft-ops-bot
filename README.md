# nft-ops-bot
this is just an experimental projects, me with my cooks. do not fork or replicate this without prior clarification or whasoever

---

**NFT Ops** is a dashboard that runs on your own computer and opens in your browser. It manages
many wallets, checks which of them can mint an OpenSea drop, and mints with all of them at once.
It is set up for **Robinhood Chain** first and also works on Ethereum, Base and other EVM chains
that OpenSea drops use.

The minting engine is [osnm-z](https://github.com/zunmax/osnm-z) (commit `b001c82`), copied in and
checked by a test so nobody can quietly change it. See [VENDORED.md](VENDORED.md).

## What it does

| Page | What you can do |
| --- | --- |
| **Overview** | Wallet count, balances per network, eligible wallets, upcoming phases with countdowns, running operations, recent transactions. |
| **Wallets** | Import keys, generate new wallets, add watch-only addresses, labels, groups, balances (native coin, ERC-20 tokens, NFTs held), CSV import/export (addresses only). |
| **Collections** | Add an OpenSea drop by link, slug or contract. See every phase, its times, price, per-wallet limit, how many of your wallets are eligible and how many NFTs are minted. **Watch** a collection to get alerts. |
| **Eligibility** | Scan all wallets (or a group). Each wallet signs in to OpenSea and gets OpenSea's own answer. Filter by status, export CSV. |
| **Mint** | Pick a phase and wallets, preview every wallet (eligibility, balance, gas, allocation, limits; public phases are simulated on-chain), type `MINT`, and all ready wallets mint in parallel with live status. |
| **Transfers** | **NFTs:** find your NFTs (ERC-721 and ERC-1155) and consolidate them into one wallet or distribute them to many. **Sweep:** collect ETH or a token from many wallets into one, keeping a gas reserve in each. **Distribute:** send ETH or a token to many addresses: same amount each, split a total equally or by percent, custom amounts, or many-to-many from a CSV. **Address book:** approved outside destinations and saved recipient lists. |
| **Contracts** | For mints that are **not** OpenSea drops: paste a contract and get a report (mint functions, price, supply, sale state, allowlist needs, confidence). Then mint with any of your wallets: every wallet is test-run first, can be scheduled for an exact start time, and the result is proven by the NFTs arriving. |
| **History** | Every mint and transfer with its transaction hash, amount, recipient, cost and result. CSV export. |
| **Networks** | Several RPC links per chain, with health, speed and automatic fallback. Built in: Robinhood Chain (default), Ethereum, Base, HyperEVM, Monad, Plasma, Arc, XRPL EVM and Elysium (Kinetiq) switched on; Arbitrum, Optimism, Polygon, ApeChain and Elysium (Vulcan Forged) ready to switch on. Any other EVM chain can be added. |
| **Settings** | Spending limits, mint defaults, scan speed, vault password, lock. |

The 🔔 bell shows alerts from watched collections: a phase opening soon, a phase opening (with how many of your wallets are eligible and funded), a closed phase, price, limit or time changes, and sell-outs. Turn on desktop alerts and sound in **Settings → Collection monitor**. The monitor never sends anything by itself.

The red **STOP** button is on every page. It stops anything that has not been sent yet,
immediately, and stays on (even after a restart) until you type `RESUME`.

## Safety, in plain words

- **Your private keys stay on your computer.** They are encrypted (AES-256-GCM) with your password
  in `data\vault.json`. They are never shown on screen, never written to logs or CSV files, and
  never sent anywhere. Signing happens locally.
- **Only your computer can open the dashboard.** It listens on `127.0.0.1`, and every request needs
  the login you get by typing your password.
- **Nothing is sent without your OK.** Every mint shows a full preview first and needs you to type
  `MINT`. Every transfer shows each "from → to" row first and needs you to type `SEND`. Only the
  rows shown as READY are sent, exactly as previewed.
- **Sweeps and NFT consolidation only go to your own wallets or the address book.** Payments to
  addresses that are not your wallets are marked "outside" and count toward the 24-hour limit.
- **Gas reserves are protected.** A sweep re-reads each wallet's balance right before sending and
  never touches the reserve you set.
- **One job per wallet.** A wallet that is minting can't be used for a transfer at the same time
  (and the other way round), so transactions never collide.
- **Spending limits are checked at the last moment**, right before each transaction is sent, not
  just in the preview. If a limit would be crossed, that transaction is not sent.
- **Eligibility is not the same as ready.** "Eligible" means OpenSea says the wallet qualifies.
  "Ready" (in the Mint preview) also checks balance, gas, timing, allocation and limits.
  "Unknown" means OpenSea did not answer. It does not mean "not eligible".
- **A sent transaction cannot be recalled.** STOP prevents new sends. Anything already sent keeps
  being tracked until it confirms or fails.
- Use **wallets made for minting**, not your main wallet.

## Install on Windows (one time)

You need **Git** and **uv**. If you already set up osnm-z, you have both. Otherwise, in
PowerShell:

```powershell
winget install --id Git.Git -e --source winget
```

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
```

Close PowerShell and open it again. Then:

1. Go to your folder:

   ```powershell
   cd "E:\OS Mints BOT"
   ```

2. Download NFT Ops:

   ```powershell
   git clone https://github.com/judith-grimes/nft-ops-bot.git
   ```

3. Go into it:

   ```powershell
   cd nft-ops-bot
   ```

4. Install it:

   ```powershell
   uv sync --frozen --python 3.12
   ```

## Start it

Double-click **`Start NFT Ops.bat`** in the `nft-ops-bot` folder. Or in PowerShell:

```powershell
uv run --frozen nft-ops
```

Your browser opens at `http://127.0.0.1:8787/`. Keep the black window open while you use it.
Closing it stops the app. Don't close it during a mint.

## First time

1. **Create a vault password** (10+ characters). Write it down somewhere safe. If you lose it,
   the stored keys cannot be recovered.
2. **Wallets → Import keys.** Paste one key per line, or `label, key` per line, for example
   `wallet1, 0xabc…`. If you used osnm-z before, these are the `WALLET_KEY` values from your
   `wallet1`, `wallet2`, … `.env` files. Optionally put them in a group.
3. **Networks → Robinhood Chain → Add RPC URL.** The public Robinhood RPC is already there. Add
   your own (dRPC, Alchemy, …) as a backup. Each link is tested before it's used.
4. **Wallets → Refresh balances** to see each wallet's ETH on Robinhood Chain.

## Each drop

1. **Collections →** paste the OpenSea link (for example
   `https://opensea.io/collection/name`) → **Add collection**.
2. **Eligibility →** choose the collection → **Scan eligibility**. Wait for the progress bar.
3. **Mint →** choose the collection and phase. "Wallets: Eligible for this phase" picks the
   wallets the scan confirmed. Set the quantity → **Preview**.
4. Read the preview. Only **READY** wallets will mint. Fix anything red (fund wallets, raise a
   limit, rescan).
5. Type `MINT` → **Start mint**. You can do this before the phase opens: each wallet waits with a
   countdown and sends right on time. Keep the window open.
6. Watch each wallet's status. Click a transaction hash to open it in the block explorer.

## Moving NFTs and funds (Transfers page)

Choose the network at the top right first. Every tab ends in a **Preview**: check each row, type
`SEND`, and watch the status of each transfer below.

- **NFTs:** pick the collection (or paste a contract address) → **Load** → **Find my NFTs**. Your
  mint records and the contract's own token list are checked first. If nothing shows up, enter a
  look-back (for example `2000000` blocks) to also scan transfer history, or **Add token IDs** by
  hand. Tick the NFTs, then choose **Consolidate** (all into one wallet) or **Distribute** (one
  address per line; they are dealt out in order, or use a CSV `address,token_id` to choose exactly).
- **Sweep funds:** choose ETH or a token, the wallets to collect from, the destination, and how much
  ETH to leave in each wallet for gas. To sweep a token, track it on the Wallets page first
  (**Track token**).
- **Distribute funds:** choose the paying wallet and how to split. Paste recipients (one per line,
  or CSV), or load a saved list. **Check list** validates every address and amount first.
- **Address book:** add outside addresses you trust (cold storage, exchange deposit). Only these
  and your own wallets can receive sweeps and NFT consolidations.

The **maximum native coin moved by one sweep or distribution** limit (Settings, default 1 ETH)
stops large moves by mistake. Raise it when you mean to move more.

## Mints that are not OpenSea drops (Contracts page)

1. Choose the network, paste the NFT contract address → **Analyse**.
2. Read the report. **High** confidence means verified source code was found; **medium** means
   the mint functions were recognised from the deployed code by their signatures. If you have the
   contract's ABI (from the project or the explorer), paste it for an exact report.
3. Click **Use** next to the mint function. Its inputs are pre-filled with placeholders:
   `{quantity}`, `{wallet}` (each wallet's own address) and `{proof}` (each wallet's allowlist
   proof, which you paste in JSON or as `address, proof1 proof2` lines). Check them.
4. Set the quantity, price per NFT (pre-filled from the contract when it can be read) and wallets.
   For a launch at a set time, fill in **Start time** and a **Gas limit** (for example 200000).
5. **Preview mint** test-runs every wallet on the chain. Type `MINT` to send.

Scheduled mints wait, refresh 3 seconds before, run the test once more at the start time, and only
send if it passes, so a mint that would fail costs nothing. **Fast mode** skips that last test to
save a fraction of a second; a mint that then fails still costs gas.

## If an RPC fails during a mint

Each mint or transfer starts on the fastest healthy RPC link. If that link stops answering in the
middle, requests switch to the next healthy link of the same network automatically and you get a
notice. Sending the same signed transaction to a second link can never create a second
transaction. Add a second RPC link for each network you mint on (**Networks → Add RPC URL**).

## Speed tools (Stage 4)

- **Send the moment the sale opens** (Contracts page → *When to send* → *The moment the sale
  opens*): for drops with no fixed start time. One wallet's mint is test-run on every new block;
  the instant it would succeed, every wallet sends. Set how long to wait (default 60 minutes).
  If it never opens, or you press STOP, nothing is sent.
- **Every RPC at once** (Settings → Security & backup, on by default): each signed transaction is
  sent through all healthy RPC links of the network at the same time. The first one to accept it
  wins. It cannot create duplicates; it is the same signed transaction.
- **Speed up / Cancel** (History): a transaction stuck as *submitted* or *pending* gets two
  buttons. *Speed up* sends the same transaction again with about 15% more gas; *Cancel* replaces
  it with an empty transfer to yourself. Type `REPLACE` to confirm. Only one of the two can ever
  confirm.
- **Fund short wallets**: when a preview marks wallets as short of funds, the *Fund N short
  wallets* button opens Distribute with each wallet and exactly what it is missing (+2%). Choose the
  wallet that pays, preview, and type `SEND`.
- **Add from my wallets** (Distribute and NFT distribution): pick recipients from your wallets,
  groups or address book instead of pasting addresses.
- **Struct inputs**: mint functions with bundled inputs (for example thirdweb's `claim`) work on
  the Contracts page. Fill bundled inputs as a JSON list; `{wallet}`, `{quantity}`, `{proof}`,
  `{price_wei}` and `{native}` placeholders work inside it.

## Wallet groups and notifications (Stage 6.5.2)

- **Roles** (Wallets → Groups → Role & rules): *Minting* (the Eligibility page starts with this
  group), *Funding* (where top-ups are paid from), *Vault* (safe storage).
- **After a successful mint** a group can **send the minted NFTs to the vault wallet**
  automatically (OpenSea drops). It is a normal previewed transfer: spending limits, STOP and
  on-chain ownership checks apply, and the result appears in notifications.
- **Smart groups** (names start with ★) update themselves: ready to mint, low on gas, holds
  NFTs, eligible for a collection phase. Use them anywhere you pick a group.
- **Group cards** show wallets, balance, ready and low-gas counts, with **Top up…** (prepares a
  Distribute preview for exactly what each wallet is missing).
- **Split a group** into batches (prefix-1, prefix-2, …) and **move or copy** selected wallets
  between groups.
- The bell opens a **notification dropdown** (tabs, mark all read, Open). The full list and
  pop-up settings are behind the ⚙ icon.
- **Portfolio**: USD prices now work on every chain OpenSea prices (ETH, Base, HyperEVM, ApeChain,
  …). **Spent** includes NFTs bought on OpenSea, and **Received** uses your OpenSea sales.

## New layout (Stage 6.5)

- **Sidebar navigation** grouped into Workspace, NFT operations, Wallet management, Market
  operations, Activity and Configuration. Collapse it with « at the bottom; on a phone it opens
  from the ☰ button. The status at the bottom of the sidebar shows whether the scheduler and
  RPCs are healthy.
- **Overview**: wallets, collections, scheduled tasks, successes and items that need attention,
  what is coming up next, wallet readiness, balances, system health and recent activity.
- **Eligibility**: summary cards, scan controls with progress, filters, a sortable table and a
  wallet panel (click a row) that explains the result and what to do next. Every scan is kept in
  **Scan history** so you can compare with earlier scans.
- **Readiness (0–100)**: click **Check readiness** on the Eligibility page. Each wallet is scored
  from real checks: key, eligibility, phase, allocation, funds for the price, funds for fees,
  RPC, spending limits and pending transactions. Ready / Almost ready / Not ready / Needs
  attention, with the reason and fix for every failed check. Mints always re-check; an old score
  is never trusted.
- **Mint calendar**: month, week and agenda views of official phases (blue) and the bot's
  scheduled runs (green), with filters and a timezone switch (Asia/Jakarta by default).
- **Emergency stop** now explains what it halts before stopping, and says clearly that sent
  transactions cannot be recalled.

## Scheduled mints and notifications (Stage 6)

Set a mint up once and let NFT Ops watch it. Open **Scheduled → Schedule a mint** (or, on the
Mint page, click **Preview** and then **Schedule instead**). Choose the collection, phase, wallets,
quantity, a **maximum total spend** and a mode:

| Mode | What it does |
|------|--------------|
| **Monitor only** | Checks readiness and reports. Never sends a transaction. |
| **Notify & confirm** | Prepares everything, notifies you, and mints only after you click **Mint now**. |
| **Automatic mint** | Mints by itself when the phase opens, within your maximum spend. |

- Tasks are saved in the database and survive restarts and page refreshes.
- About 30 minutes before the start, the bot checks eligibility for the task's own wallets and
  every wallet's readiness (balance, gas, allocation, limits).
- At run time it checks everything again before sending, and the normal spending limits and
  STOP always apply. A task can never run twice.
- If the official phase time changes, the task pauses and asks you to confirm (**Resume**).
- If NFT Ops was closed or the PC was off when a task was due, it is marked **Missed** with the
  reason. Nothing is sent late without you.
- Keep NFT Ops running and unlocked while an automatic task is waiting; the vault does not
  auto-lock then.
- Every task ends with a **report**: per wallet, what happened and why, transaction links,
  NFTs minted, spend and gas.

The bell opens the **Notifications** page: mint results, scheduled task events, wallets that
need attention, interrupted operations and collection alerts, grouped by category, with
read/unread and **Open** links. Choose which categories also pop up on your desktop.

After a restart, operations that were running are marked **interrupted**. Wallets that had not
sent anything are recorded as "not sent". Transactions that were already sent are checked
on-chain until they confirm or fail.

Sending is click-to-confirm: **Mint now** and **Send** show a summary dialog instead of asking
you to type a word. Giving a marketplace permission over your NFTs (approval) and arming
FULL AUTO offers still ask you to type a word on purpose.

## Offer Manager (auto-accept) and Portfolio

Needs an **OpenSea API key** (Settings → OpenSea & offers; stored encrypted in your vault).

The **Offers** page watches offers on every NFT in every registered wallet (collection offers,
offers on a single NFT, and trait offers when the NFT's traits are known), across all enabled
networks OpenSea supports. For every offer it shows: gross offer → OpenSea fee → creator earnings
→ gas → **what you receive**, with a ✓/✗ list of every check and the reason.

- **Fees are read from the offer itself** (its Seaport order), so the OpenSea fee and any creator
  earnings are exactly what will be paid. On Robinhood Chain that is 1% OpenSea fee; optional
  creator earnings are not added.
- **Default rules** (Default rules button; each collection can override them):
  minimum = **95% of floor**; what you receive must be **≥ 100% of that minimum** after all fees
  and gas; **pause** automatic acceptance if the floor **falls more than 10%** within 60 minutes
  or drifts more than 50% from recent sales; stablecoin offers (e.g. USDG on Robinhood) are
  accepted, converted at OpenSea's rate minus a 1% safety margin; at most 10 sales and 5 ETH per
  collection per 24 h; at most 3 at the same time. A collection can use a **fixed** minimum or
  **floor + premium** instead, and custom rules can target a token, trait, wallet, offer type or
  currency.
- **Execution modes**: *Safe* (recommend only), *Semi* (you click Accept and type `ACCEPT`),
  *Full* (automatic). Full is the default mode, but a collection only sells automatically after
  you **Arm** it and type `AUTO-ACCEPT`. Switch it off any time.
- **One-time approval**: before an offer can be accepted, each wallet must allow OpenSea's
  Seaport transfer contract to move NFTs of that collection. The Offers page shows an **Approve**
  button; it goes through the normal preview and you type `APPROVE`. It covers only that
  collection in that wallet and can be revoked.
- **Before every sale** (manual or automatic) everything is checked again with fresh data:
  offer still valid, NFT still yours, approval, floor, fees, your rules, and OpenSea's
  transaction is verified — it must be exactly the offer that was evaluated, the only thing
  leaving your wallet must be that one NFT, and the payment to you must match — then it is
  simulated on-chain. Anything that fails is skipped with the reason. An NFT is never sold twice,
  STOP blocks everything, and 3 failures in a row pause the collection.
- **Activity** and **Export CSV** record every accepted, skipped and failed offer with gross,
  fees, gas, net, floor, transaction and reason.

The **Portfolio** page shows, per collection: NFTs held, floor next to the median of recent
sales, 24-hour change, a 7-day floor chart, value at floor, what you spent minting, what you
received from sales, and profit/loss.

## Auto-lock

The vault locks itself after 30 minutes without activity (change it or turn it off in Settings).
It never locks while a mint, transfer or scan is running. Unlock with your password.

## Back up

**Settings → Security & backup → Download backup** saves a zip with your encrypted vault and
your database (it asks for your password first). Keep it somewhere safe, for example a USB stick,
and remember your password. To restore it:

```powershell
uv run --frozen nft-ops --restore "E:\path\to\nft-ops-backup-....zip"
```

The backup is checked first; your current `data` folder is moved aside (not deleted) before the
backup is unpacked. Then start NFT Ops and unlock with the password the backup was made with.

## Update to a new version

```powershell
cd "E:\OS Mints BOT\nft-ops-bot"
```

```powershell
git pull
```

```powershell
uv sync --frozen --python 3.12
```

Your `data` folder is not touched by updates.

## Known limits

- **EVM chains only.** Solana, Bitcoin, the XRP Ledger itself, Monero and Aptos use different
  addresses, signatures and RPC formats, so they cannot be added as networks here. For XRP, the
  **XRPL EVM** sidechain is included.

- The **Mint** page handles OpenSea SeaDrop drops; the **Contracts** page handles other mint
  contracts. Mint functions that need a per-wallet signature from the project, or that take
  struct (tuple) inputs, are recognised but cannot be used for custom mints yet.
- Verified source code is looked up on Sourcify and the network's Blockscout explorer. Some
  explorers block automated requests; then paste the ABI yourself.
- Transfers send one transaction per NFT (ERC-721) or per sender-recipient pair (ERC-1155 batches
  several IDs together), and one per payment. There is no multi-send contract, so each transfer
  pays its own small gas fee.
- Finding NFTs by transfer history depends on your RPC allowing log searches; some public RPCs
  limit them. Adding token IDs by hand always works.
- "Fast" is not a guarantee. All wallets send at the same moment, already prepared, but on a busy
  FCFS mint other people may pay more gas, and the network decides who gets in first.
- Every wallet signs in to OpenSea to check eligibility. With many wallets, OpenSea may slow you
  down. Lower the scan speed in Settings if you see many "UNKNOWN" results. Many projects and
  OpenSea's terms do not allow one person to use many wallets to get around per-wallet limits.
- Allowlist mint data comes from OpenSea about 2 seconds before launch (osnm-z's timing), so
  allowlist wallets cannot be test-run on-chain in advance. Public phases are simulated in the
  preview once open.
- A mint operation uses the fastest healthy RPC at the moment it starts. If that RPC fails in the
  middle of a mint, it does not switch.

## For developers

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

The tests run the real minting engine against a fake chain and a fake OpenSea, with no network
access. `uv run python tests/demo_server.py` starts the dashboard against the same fakes, so you
can click through it safely.
