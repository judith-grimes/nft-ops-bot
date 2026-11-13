"""After-mint rule (group option 'Send to the vault wallet').

When a mint operation finishes, every wallet that minted successfully and belongs to a group with
``after_mint = 'vault'`` sends the NFTs it just minted to that group's vault wallet. This is a
normal NFT transfer: it is previewed (ownership checked on-chain), then sent through the
transaction engine, so spending limits, STOP and the pre-broadcast gate all apply. The vault must
be one of your own wallets. Results are reported in the notification center.
"""

from __future__ import annotations

from typing import Any

from . import groups, transfers
from .notify import Notifier


async def run(state: Any, job: Any) -> None:
    meta = job.preview.metadata
    chain_id, contract = meta.network_id, meta.address
    plans: dict[str, list[dict]] = {}
    for run_ in job.runs.values():
        if run_.phase != "confirmed":
            continue
        target = groups.after_mint_target(state.db, run_.address)
        if not target:
            continue
        ids = [r["token_id"] for r in state.db.all(
            "SELECT token_id FROM nft_tokens WHERE chain_id = ? AND contract = ? AND owner = ?",
            (chain_id, contract, run_.address))]
        if ids:
            plans.setdefault(target, []).extend({"owner": run_.address, "token_id": t} for t in ids)
    note: Notifier = state.notifier
    for vault, selection in plans.items():
        try:
            items, notes = await transfers.plan_nft(state.db, state.pool, chain_id, contract, "erc721",
                                                    selection, "consolidate", vault)
            preview = await state.tx.preview("nft_transfer", chain_id, items, "After-mint: send to vault", "", notes)
            if not preview.get("token"):
                raise RuntimeError("; ".join(preview.get("blocks") or ["nothing was ready to send"]))
            job_id = await state.tx.start(preview["token"], preview.get("confirm_word", "SEND"))
            note.send("mint", "info", "Sending minted NFTs to the vault",
                      f"{len(selection)} NFT(s) from {meta.slug} are being sent to {vault[:10]}… "
                      f"(transfer operation #{job_id}).", link="history", slug=meta.slug)
        except Exception as error:  # noqa: BLE001 - always reported, never silent
            note.send("mint", "bad", "After-mint transfer not sent",
                      f"{meta.slug}: the minted NFTs stay in their wallets. Reason: {str(error)[:200]}",
                      link="transfers", slug=meta.slug)
