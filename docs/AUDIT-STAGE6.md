# NFT Ops — Stage 6 audit (Critical and High findings)

Scope: the code on `main` after Stage 5 (commit 03a3d0b). I traced the real execution flow of
mint, transfer, eligibility and offers from the button click to the transaction. This audit
covers only Critical and High findings, as agreed. Medium and Low items are listed briefly at
the end.

Layers: **FE** = frontend (static/app.js), **BE** = backend (Python), **DB** = database,
**BC** = blockchain layer.

## What already works (kept as is)

- Private keys stay in the encrypted vault (scrypt + AES-GCM). They are never sent to the
  browser, logs or OpenSea. Signing happens locally.
- Spending limits and the emergency STOP are enforced in the backend, inside a gate that runs
  right before each broadcast (`safety.py`). The browser cannot bypass them.
- Every send starts with a server-side preview. It gives a single-use token that expires
  after 3 minutes, and only the previewed READY rows can be sent.
- Each wallet runs in its own task, so one failing wallet does not stop the others
  (`mint.py::_run_wallet`).
- Nonces are managed per source wallet. A wallet cannot be in two operations at once.
- RPC failover and broadcasting to every RPC work during a mint (`failover.py`).
- Eligibility is checked per wallet, using OpenSea's own signed-in answer.
- Open public phases are simulated on-chain in the preview.
- Offers are revalidated before acceptance, with a per-NFT lock.

## Critical

| # | File | What happens now | Why it matters | Fix (layer) |
|---|------|------------------|----------------|-------------|
| C1 | `mint.py` (MintJob), `txengine.py` (scheduled sends, sale-open watcher) | A mint started before its phase opens waits **in memory**. If the app or PC restarts, it disappears silently. The `jobs` row stays `running` forever, and its history rows stay `queued`/`waiting`. | You believe a mint is armed when it is not. Nothing records that the window was missed. | On startup, mark interrupted jobs as such and record every unsent wallet as "not sent: NFT Ops was closed". Move "mint later" into a persistent scheduler (BE, DB). |
| C2 | `mint.py::_track_later`, `txengine.py` | A transaction broadcast just before shutdown is tracked by an in-memory task only. After a restart it stays `submitted`/`pending` forever. | The History page and spending totals never learn the real outcome (minted, or reverted with gas spent). | Background receipt tracker for pending history rows, run on startup and every minute (BE, BC). |

## High

| # | File | What happens now | Why it matters | Fix (layer) |
|---|------|------------------|----------------|-------------|
| H1 | `app.js` (mint, transfers, speed-up/cancel) | You must type `MINT`, `SEND` or `REPLACE` into a box before a send. | This is unnecessary friction (the issue you reported). The real protection is already the server-side single-use preview token and backend limits. | Replace typing with a **Confirm** dialog that summarises wallets, quantity, maximum spend and network. Standing permissions (NFT approvals, arming FULL AUTO) keep their typed word on purpose (FE). |
| H2 | `mint.py` preview/start | Mint later only works by previewing, then starting a job that waits in memory. There is no saved task, no modes, and no pause, resume or cancel. | You must be at the PC to start it, and the task is lost on restart (C1). | Persistent **Scheduled mints** with Monitor Only, Notify & Confirm and Automatic modes. Includes a claim lock (runs once), missed-window detection, and readiness recheck at execution (BE, DB, FE). |
| H3 | `mint.py` | There is no outcome report. Results are scattered across History rows and toasts. | You cannot see at a glance what happened and why. | Per-task **mint report**: per-wallet result and reason, transaction hashes, spend, gas and totals (BE, FE). |
| H4 | `monitor.py` alerts | Only phase changes from the collection monitor create alerts. Mint failures, missed tasks, wallets that are eligible but not ready, and interrupted jobs are not notified. There is only "mark all read". | Important failures can go unnoticed. | **Notification center** built on the existing alerts table: categories, severity, links, per-item read, filters, desktop notifications (BE, DB, FE). |
| H5 | `mint.py::_readiness` + eligibility replace-on-scan | Allowlist readiness depends on the last manual eligibility scan, which now only covers the last scanned group. | Wallets outside that group show "eligibility unknown" at mint time. | Scheduled tasks rescan their own wallets about 30 minutes before launch (BE). |
| H6 | `server.py` auto-lock | The vault auto-locks after 30 idle minutes. | A future automatic task could not sign after the vault locks. | The vault no longer auto-locks while an Automatic or Notify & Confirm task is pending. If the vault is locked anyway, the task reports "never attempted: vault locked" instead of failing silently (BE). |

## Medium (not in this stage)

- Readiness exists per preview but has no 0–100 score and no standalone "check readiness"
  page.
- Twelve top-level tabs; a grouped sidebar would be cleaner.
- Some raw library errors reach the UI as-is (e.g. "OpenSea returned HTTP status 403").
- No mint calendar view (month/week/agenda).

## Low

- Some labels are technical (`INSUFFICIENT_GAS`, `not_submitted`).
- Small helper text in places.
