"""In-memory fake chain (JSON-RPC over httpx.MockTransport) and fake OpenSea client.

Nothing here touches the network. The fake chain decodes and verifies every signed
transaction it receives, so tests prove the real signing path ran.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
from eth_account import Account
from eth_utils.crypto import keccak

from osnm_z.opensea_protocol import (
    CollectionMetadata,
    EligibilitySnapshot,
    MintTransactionAction,
    ProtocolError,
    StageEligibility,
    StageMetadata,
)
from osnm_z.seadrop import OPENSEA_SEADROP_ADDRESS

CHAIN_ID = 4663
NFT = "0x93DAF9D3dd4b35D165A7a8Bcfc598437af6f5c2B"
TRANSFER = keccak(text="Transfer(address,address,uint256)")
PRICE = 10**15  # 0.001 ETH


@dataclass
class FakeChain:
    chain_id: int = CHAIN_ID
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    sent: list[dict] = field(default_factory=list)
    receipts: dict[str, dict] = field(default_factory=dict)
    quantity: int = 1
    send_delay: float = 0.0
    next_token: int = 1
    revert: bool = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            out = [await self._one(item) for item in body]
        else:
            out = await self._one(body)
        return httpx.Response(200, json=out)

    async def _one(self, req: dict) -> dict:
        method, params = req["method"], req.get("params", [])
        try:
            result = await self._dispatch(method, params)
            return {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except ValueError as error:
            return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": str(error)}}

    async def _dispatch(self, method: str, params: list):
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return "0x100"
        if method == "eth_getBlockByNumber":
            return {"number": "0x100", "timestamp": "0x6800000", "baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(10**8)
        if method == "eth_getBalance":
            return hex(self.balances.get(params[0].lower(), 0))
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(params[0].lower(), 0))
        if method == "eth_sendRawTransaction":
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            raw = bytes.fromhex(params[0][2:])
            sender = Account.recover_transaction(raw).lower()
            tx_hash = "0x" + keccak(raw).hex()
            self.nonces[sender] = self.nonces.get(sender, 0) + 1
            self.sent.append({"from": sender, "hash": tx_hash})
            logs = []
            for _ in range(self.quantity):
                logs.append({
                    "address": NFT,
                    "topics": ["0x" + TRANSFER.hex(), "0x" + "00" * 32,
                               "0x" + "00" * 12 + sender[2:], "0x" + self.next_token.to_bytes(32, "big").hex()],
                    "data": "0x", "logIndex": hex(len(logs)), "removed": False,
                    "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
                    "transactionHash": tx_hash, "transactionIndex": "0x0",
                })
                self.next_token += 1
            self.receipts[tx_hash] = {
                "transactionHash": tx_hash, "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
                "status": "0x0" if self.revert else "0x1", "logs": [] if self.revert else logs,
                "gasUsed": hex(150_000), "effectiveGasPrice": hex(11 * 10**8),
            }
            return tx_hash
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method in ("eth_call", "eth_estimateGas"):
            raise ValueError("not supported by fake chain")
        raise ValueError(f"unexpected method {method}")


def iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(int(ts), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def allowlist_metadata(start: float, end: float) -> CollectionMetadata:
    return CollectionMetadata(
        slug="fake-drop", address=NFT, chain_identifier="robinhood", network_id=CHAIN_ID,
        drop_kind="Erc721SeaDropV1", drop_address=OPENSEA_SEADROP_ADDRESS,
        stages=(StageMetadata(kind="SIGNED", label="Allowlist", stage_type="PRESALE",
                              stage_index=1, start_time=iso(start), end_time=iso(end),
                              max_total_mintable_by_wallet=5, native_price_wei=PRICE,
                              price_chain_identifier="robinhood"),),
    )


class FakeOpenSea:
    """WalletOpenSeaClient stand-in: one instance per wallet session, like the real one."""

    def __init__(self, metadata: CollectionMetadata, allowlist: set[str],
                 fail_auth: set[str] | None = None) -> None:
        self.metadata = metadata
        self.allowlist = {a.lower() for a in allowlist}
        self.fail_auth = {a.lower() for a in (fail_auth or set())}
        self.wallet: str | None = None

    async def collection_metadata(self, slug: str) -> CollectionMetadata:
        return self.metadata

    async def resolve_collection(self, locator, chain_id: int) -> CollectionMetadata:
        return self.metadata

    async def authenticate(self, signer, wallet: str, chain_id: int, slug: str) -> None:
        if wallet.lower() in self.fail_auth:
            from osnm_z.opensea_protocol import ErrorKind
            raise ProtocolError(ErrorKind.AUTHENTICATION, 403)
        assert signer.identity.address.lower() == wallet.lower()
        self.wallet = wallet

    async def eligibility(self, slug: str, wallet: str) -> EligibilitySnapshot:
        assert self.wallet and self.wallet.lower() == wallet.lower()
        ok = wallet.lower() in self.allowlist
        return EligibilitySnapshot("Erc721SeaDropV1", 0, tuple(
            StageEligibility(s.kind, s.stage_type, s.stage_index, ok, None, 5, 5 if ok else None,
                             PRICE if ok else None, "robinhood" if ok else None)
            for s in self.metadata.stages))

    async def mint_transaction_action(self, collection, wallet: str, quantity: int
                                      ) -> MintTransactionAction:
        stage = collection.stages[0].stage_index
        words = [bytes(32)] * 8 + [stage.to_bytes(32, "big")]
        calldata = bytes.fromhex("4b61cd6f") + b"".join(words)
        return MintTransactionAction(OPENSEA_SEADROP_ADDRESS, PRICE * quantity, calldata)

    async def warm_mint_data_connection(self, slug: str) -> None:
        return None

    async def config_change_transaction_hashes(self, slug: str) -> tuple[str, ...]:
        return ()

    async def aclose(self) -> None:
        return None
