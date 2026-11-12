# nft-ops-bot
this is just an experimental projects, me with my cooks. do not fork or replicate this without prior clarification or whasoever

---

**NFT Ops** is a dashboard that runs on your own computer and opens in your browser. It manages
many wallets, checks which of them can mint an OpenSea drop, and mints with all of them at once.
It is set up for **Robinhood Chain** first and also works on Ethereum, Base and other EVM chains
that OpenSea drops use.

The minting engine is [osnm-z](https://github.com/zunmax/osnm-z) (commit `b001c82`), copied in and
checked by a test so nobody can quietly change it. See [VENDORED.md](VENDORED.md).

## What it does (Stage 1)

| Page | What you can do |
| --- | --- |
| **Overview** | Wallet count, balances per network, eligible wallets, upcoming phases with countdowns, running operations, recent transactions. |
| **Wallets** | Import keys, generate new wallets, add watch-only addresses, labels, groups, balances (native coin, ERC-20 tokens, NFTs held), CSV import/export (addresses only). |
| **Collections** | Add an OpenSea drop by link, slug or contract. See every phase, its times, price, per-wallet limit and how many of your wallets are eligible. |
| **Eligibility** | Scan all wallets (or a group). Each wallet signs in to OpenSea and gets OpenSea's own answer. Filter by status, export CSV. |
| **Mint** | Pick a phase and wallets, preview every wallet (eligibility, balance, gas, allocation, limits; public phases are simulated on-chain), type `MINT`, and all ready wallets mint in parallel with live status. |
| **History** | Every mint attempt with its transaction hash, cost and result. CSV export. |
| **Networks** | Several RPC links per chain, with health, speed and automatic fallback. Robinhood Chain (4663) is set up by default. |
| **Settings** | Spending limits, mint defaults, scan speed, vault password, lock. |

The red **STOP** button is on every page. It stops anything that has not been sent yet,
immediately, and stays on (even after a restart) until you type `RESUME`.

## Safety, in plain words

- **Your private keys stay on your computer.** They are encrypted (AES-256-GCM) with your password
  in `data\vault.json`. They are never shown on screen, never written to logs or CSV files, and
  never sent anywhere. Signing happens locally.
- **Only your computer can open the dashboard.** It listens on `127.0.0.1`, and every request needs
  the login you get by typing your password.
- **Nothing is sent without your OK.** Every mint shows a full preview first and needs you to type
  `MINT`.
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

## Back up

Copy the **`data`** folder (it has `vault.json` and `nft-ops.db`) somewhere safe, for example a
USB stick, and remember your password. That is everything needed to restore your wallets.

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

- Stage 1 mints **OpenSea SeaDrop ERC-721 drops** (the standard OpenSea drop contract) on any
  configured chain. Other contracts, batch NFT transfers, fund sweeping and fund distribution come
  in later stages.
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
