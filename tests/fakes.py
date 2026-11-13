"""In-memory fake chain (JSON-RPC over httpx.MockTransport) and fake OpenSea client.

Nothing here touches the network. The fake chain decodes and verifies every signed
transaction it receives, so tests prove the real signing path ran.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
import rlp
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
SEAPORT_FAKE = "0x0000000000000068f116a894984e2db1123eb395"
CONTROLLER_FAKE = "0x00000000f9490004c11cef243f5400493c00ad63"
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
    erc20: dict[str, dict[str, int]] = field(default_factory=dict)      # token -> owner -> units
    erc721: dict[str, dict[int, str]] = field(default_factory=dict)     # contract -> id -> owner
    erc1155: dict[str, dict[tuple[str, int], int]] = field(default_factory=dict)
    gas_price: int = 11 * 10**8
    supply: dict[str, tuple[int, int]] = field(default_factory=dict)  # contract -> (total, max)
    down_ports: set[int] = field(default_factory=set)   # simulate dead RPC endpoints
    custom: dict = field(default_factory=dict)   # address -> CustomDrop (non-SeaDrop mint contract)
    approvals: dict = field(default_factory=dict)  # (contract, owner, operator) -> bool
    custom_seaport: object = None                  # FakeOpenSeaAPI, when offer tests run
    calls_by_port: dict[int, int] = field(default_factory=dict)
    sends_by_port: dict[int, int] = field(default_factory=dict)
    mining: bool = True                                   # False = transactions stay pending
    pending: dict = field(default_factory=dict)           # (sender, nonce) -> tx dict
    txs: dict = field(default_factory=dict)               # hash -> tx dict (for getTransactionByHash)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        port = request.url.port or 0
        self.calls_by_port[port] = self.calls_by_port.get(port, 0) + 1
        if port in self.down_ports:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content)
        for item in body if isinstance(body, list) else [body]:
            if item.get("method") == "eth_sendRawTransaction":
                self.sends_by_port[port] = self.sends_by_port.get(port, 0) + 1
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
            return hex(0x100 + int(time.time() * 4))     # ~4 blocks per second
        if method == "eth_getBlockByNumber":
            return {"number": "0x100", "timestamp": "0x6800000", "baseFeePerGas": hex(10**9)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(10**8)
        if method == "eth_getBalance":
            return hex(self.balances.get(params[0].lower(), 0))
        if method == "eth_getTransactionCount":
            who = params[0].lower()
            count = self.nonces.get(who, 0)
            if params[1] == "latest":
                count -= sum(1 for (s_, _n) in self.pending if s_ == who)
            return hex(count)
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_sendRawTransaction":
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            raw = bytes.fromhex(params[0][2:])
            sender = Account.recover_transaction(raw).lower()
            tx_hash = "0x" + keccak(raw).hex()
            if tx_hash in self.receipts:
                raise ValueError("already known")      # what real nodes answer to a re-send
            fields = rlp.decode(raw[1:])
            nonce = int.from_bytes(fields[1], "big")
            to = "0x" + fields[5].hex()
            value = int.from_bytes(fields[6], "big")
            data = bytes(fields[7])
            tx = {"hash": tx_hash, "from": sender, "to": to, "value": hex(value), "input": "0x" + data.hex(),
                  "nonce": hex(nonce), "gas": hex(int.from_bytes(fields[4], "big")),
                  "maxPriorityFeePerGas": hex(int.from_bytes(fields[2], "big")),
                  "maxFeePerGas": hex(int.from_bytes(fields[3], "big"))}
            if not self.mining:
                key = (sender, nonce)
                if key in self.pending:
                    old = self.pending[key]
                    for fee in ("maxFeePerGas", "maxPriorityFeePerGas"):
                        if int(tx[fee], 16) * 10 < int(old[fee], 16) * 11:
                            raise ValueError("replacement transaction underpriced")
                    self.txs.pop(old["hash"], None)
                elif nonce != self.nonces.get(sender, 0):
                    raise ValueError(f"nonce too low/high: got {nonce}, want {self.nonces.get(sender, 0)}")
                else:
                    self.nonces[sender] = nonce + 1
                self.pending[key] = tx
                self.txs[tx_hash] = tx
                return tx_hash
            self.txs[tx_hash] = tx
            if nonce != self.nonces.get(sender, 0):
                raise ValueError(f"nonce too low/high: got {nonce}, want {self.nonces.get(sender, 0)}")
            if self.balances.get(sender, 0) < value:
                raise ValueError("insufficient funds for gas * price + value")
            self.nonces[sender] = nonce + 1
            self.sent.append({"from": sender, "hash": tx_hash, "to": to, "value": value, "data": data})
            if to.lower() == OPENSEA_SEADROP_ADDRESS.lower():
                return self._mint_receipt(sender, tx_hash)
            if self.custom_seaport is not None and to.lower() == SEAPORT_FAKE:
                problem = self.custom_seaport.execute(sender, data, dry=False)
                self.balances[sender] -= 200_000 * self.gas_price
                self.receipts[tx_hash] = {
                    "transactionHash": tx_hash, "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
                    "status": "0x0" if problem else "0x1", "logs": [], "gasUsed": hex(200_000),
                    "effectiveGasPrice": hex(self.gas_price)}
                return tx_hash
            if to.lower() in self.custom:
                drop = self.custom[to.lower()]
                problem = drop.check(sender, value, data)
                logs = [] if problem else drop.mint(sender, data, tx_hash)
                self.balances[sender] -= (0 if problem else value) + 120_000 * self.gas_price
                self.receipts[tx_hash] = {
                    "transactionHash": tx_hash, "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
                    "status": "0x0" if problem else "0x1", "logs": logs, "gasUsed": hex(120_000),
                    "effectiveGasPrice": hex(self.gas_price)}
                return tx_hash
            ok = self._apply(sender, to, value, data)
            gas_used = 21_000 if not data else 60_000
            if ok:
                self.balances[sender] = self.balances.get(sender, 0) - value - gas_used * self.gas_price
            else:
                self.balances[sender] = self.balances.get(sender, 0) - gas_used * self.gas_price
            self.receipts[tx_hash] = {
                "transactionHash": tx_hash, "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
                "status": "0x1" if ok else "0x0", "logs": [], "gasUsed": hex(gas_used),
                "effectiveGasPrice": hex(self.gas_price)}
            return tx_hash
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            return self._call(params[0])
        if method == "eth_estimateGas":
            call = params[0]
            sender = call.get("from", "").lower()
            data = bytes.fromhex(call.get("data", "0x")[2:])
            value = int(call.get("value", "0x0"), 16)
            if self.custom_seaport is not None and call["to"].lower() == SEAPORT_FAKE:
                problem = self.custom_seaport.execute(sender, data, dry=True)
                if problem:
                    raise ValueError(f"execution reverted: {problem}")
                return hex(200_000)
            if call["to"].lower() == OPENSEA_SEADROP_ADDRESS.lower():
                raise ValueError("not supported by fake chain")
            if self.balances.get(sender, 0) < value:
                raise ValueError("insufficient funds for transfer")
            drop = self.custom.get(call["to"].lower())
            if drop:
                problem = drop.check(sender, value, data)
                if problem:
                    raise ValueError(f"execution reverted: {problem}")
                return hex(120_000)
            if data and not self._apply(sender, call["to"].lower(), value, data, dry=True):
                raise ValueError("execution reverted")
            return hex(21_000 if not data else 60_000)
        if method == "eth_getLogs":
            return []
        if method == "eth_getCode":
            drop = self.custom.get(params[0].lower())
            return "0x" + drop.bytecode().hex() if drop else "0x"
        if method == "eth_getStorageAt":
            return "0x" + "00" * 32
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


def _addr(word: bytes) -> str:
    return "0x" + word[12:32].hex()


def _u(word: bytes) -> int:
    return int.from_bytes(word[:32], "big")


def _call_impl(self: FakeChain, call: dict) -> str:
    to = call["to"].lower()
    data = bytes.fromhex(call.get("data", call.get("input", "0x"))[2:])
    if to in self.custom:
        return self.custom[to].read(data[:4].hex())
    if self.custom_seaport is not None:
        if to == SEAPORT_FAKE:
            out = self.custom_seaport.seaport_call(data)
            if out:
                return out
        if to == CONTROLLER_FAKE:
            return self.custom_seaport.controller_call(data)
    sel, args = data[:4].hex(), data[4:]
    word = lambda n: args[32 * n: 32 * n + 32]
    if sel == "01ffc9a7":  # supportsInterface
        iface = args[:4].hex()
        is721, is1155 = to in self.erc721, to in self.erc1155
        return "0x" + (1 if (iface == "80ac58cd" and is721) or (iface == "d9b67a26" and is1155)
                       else 0).to_bytes(32, "big").hex()
    if sel == "70a08231":  # balanceOf(address) — ERC-20 or ERC-721
        owner = _addr(word(0))
        if to in self.erc20:
            return "0x" + self.erc20[to].get(owner, 0).to_bytes(32, "big").hex()
        count = sum(1 for o in self.erc721.get(to, {}).values() if o == owner)
        return "0x" + count.to_bytes(32, "big").hex()
    if sel == "e985e9c5":  # isApprovedForAll(owner, operator)
        return "0x" + int(bool(self.approvals.get((to, _addr(word(0)), _addr(word(1)))))).to_bytes(32, "big").hex()
    if sel == "6352211e":  # ownerOf
        owner = self.erc721.get(to, {}).get(_u(word(0)))
        if owner is None:
            raise ValueError("execution reverted: nonexistent token")
        return "0x" + "00" * 12 + owner[2:]
    if sel == "2f745c59":  # tokenOfOwnerByIndex
        owner, index = _addr(word(0)), _u(word(1))
        ids = sorted(i for i, o in self.erc721.get(to, {}).items() if o == owner)
        if index >= len(ids):
            raise ValueError("execution reverted")
        return "0x" + ids[index].to_bytes(32, "big").hex()
    if sel == "00fdd58e":  # balanceOf(address,uint256)
        return "0x" + self.erc1155.get(to, {}).get((_addr(word(0)), _u(word(1))), 0).to_bytes(32, "big").hex()
    if sel in ("18160ddd", "d5abeb01") and to in self.supply:
        total, maximum = self.supply[to]
        return "0x" + (total if sel == "18160ddd" else maximum).to_bytes(32, "big").hex()
    if sel == "313ce567":
        return "0x" + (6).to_bytes(32, "big").hex()
    if sel == "95d89b41":
        from eth_abi import encode
        return "0x" + encode(["string"], ["USDX"]).hex()
    raise ValueError("execution reverted: unknown call")


def _apply_impl(self: FakeChain, sender: str, to: str, value: int, data: bytes, dry: bool = False) -> bool:
    from eth_abi import decode
    to = to.lower()
    if not data:
        if not dry:
            self.balances[to] = self.balances.get(to, 0) + value
        return True
    sel, args = data[:4].hex(), data[4:]
    if sel == "a9059cbb" and to in self.erc20:
        recipient, amount = decode(["address", "uint256"], args)
        book = self.erc20[to]
        if book.get(sender, 0) < amount:
            return False
        if not dry:
            book[sender] -= amount
            book[recipient.lower()] = book.get(recipient.lower(), 0) + amount
        return True
    if sel == "a22cb465" and to in self.erc721:        # setApprovalForAll(operator, approved)
        operator, approved = decode(["address", "bool"], args)
        if not dry:
            self.approvals[(to, sender, operator.lower())] = approved
        return True
    if sel == "42842e0e" and to in self.erc721:
        frm, recipient, token_id = decode(["address", "address", "uint256"], args)
        owners = self.erc721[to]
        if owners.get(token_id) != sender or frm.lower() != sender:
            return False
        if not dry:
            owners[token_id] = recipient.lower()
        return True
    if sel in ("f242432a", "2eb2c2d6") and to in self.erc1155:
        if sel == "f242432a":
            frm, recipient, token_id, amount, _ = decode(["address", "address", "uint256", "uint256", "bytes"], args)
            moves = [(token_id, amount)]
        else:
            frm, recipient, ids, amounts, _ = decode(["address", "address", "uint256[]", "uint256[]", "bytes"], args)
            moves = list(zip(ids, amounts))
        book = self.erc1155[to]
        if frm.lower() != sender or any(book.get((sender, i), 0) < a for i, a in moves):
            return False
        if not dry:
            for i, a in moves:
                book[(sender, i)] -= a
                book[(recipient.lower(), i)] = book.get((recipient.lower(), i), 0) + a
        return True
    return False


def _mint_receipt_impl(self: FakeChain, sender: str, tx_hash: str) -> str:
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
        self.erc721.setdefault(NFT.lower(), {})[self.next_token] = sender
        self.next_token += 1
    self.receipts[tx_hash] = {
        "transactionHash": tx_hash, "blockHash": "0x" + "ab" * 32, "blockNumber": "0x101",
        "status": "0x0" if self.revert else "0x1", "logs": [] if self.revert else logs,
        "gasUsed": hex(150_000), "effectiveGasPrice": hex(self.gas_price),
    }
    return tx_hash


FakeChain._call = _call_impl
FakeChain._apply = _apply_impl
FakeChain._mint_receipt = _mint_receipt_impl


async def _mine_impl(self: FakeChain) -> None:
    """Include every pending transaction (only the latest one per nonce), then mine normally."""
    pending, self.pending = sorted(self.pending.items()), {}
    self.mining = True
    for (sender, nonce), tx in pending:
        self.nonces[sender] = nonce            # let the normal path accept it once
        to, value, data = tx["to"], int(tx["value"], 16), bytes.fromhex(tx["input"][2:])
        self.receipts.pop(tx["hash"], None)
        ok = self._apply(sender, to, value, data)
        gas_used = 21_000 if not data else 60_000
        self.balances[sender] = self.balances.get(sender, 0) - (value if ok else 0) - gas_used * self.gas_price
        self.nonces[sender] = nonce + 1
        self.sent.append({"from": sender, "hash": tx["hash"], "to": to, "value": value, "data": data})
        self.receipts[tx["hash"]] = {
            "transactionHash": tx["hash"], "blockHash": "0x" + "ab" * 32, "blockNumber": "0x102",
            "status": "0x1" if ok else "0x0", "logs": [], "gasUsed": hex(gas_used),
            "effectiveGasPrice": hex(self.gas_price)}


FakeChain.mine = _mine_impl


def _sel(signature: str) -> bytes:
    return keccak(text=signature)[:4]


@dataclass
class CustomDrop:
    """A typical hand-written ERC-721 drop: public mint + Merkle-style presale."""

    address: str
    price: int = 2 * 10**15
    max_supply: int = 100
    sale_start: float = 0.0
    total: int = 0
    proofs: dict[str, str] = field(default_factory=dict)   # wallet -> valid proof hash

    SIGS = ("mint(uint256)", "presaleMint(uint256,bytes32[])", "cost()", "maxSupply()",
            "totalSupply()", "saleActive()", "name()", "supportsInterface(bytes4)")

    def bytecode(self) -> bytes:
        # Shaped like a Solidity dispatcher: PUSH4 <selector> EQ PUSH2 <dest> JUMPI ...
        out = b"\x60\x80\x60\x40\x52"
        for sig in self.SIGS:
            out += b"\x63" + _sel(sig) + b"\x14\x61\x00\x10\x57"
        return out + b"\x00"

    def read(self, sel: str) -> str:
        from eth_abi import encode as enc
        values = {
            _sel("cost()").hex(): enc(["uint256"], [self.price]),
            _sel("maxSupply()").hex(): enc(["uint256"], [self.max_supply]),
            _sel("totalSupply()").hex(): enc(["uint256"], [self.total]),
            _sel("saleActive()").hex(): enc(["bool"], [time.time() >= self.sale_start]),
            _sel("name()").hex(): enc(["string"], ["Custom Drop"]),
            "01ffc9a7": enc(["bool"], [True]),
        }
        if sel not in values:
            raise ValueError("execution reverted")
        return "0x" + values[sel].hex()

    def check(self, sender: str, value: int, data: bytes) -> str | None:
        from eth_abi import decode as dec
        sel, args = data[:4], data[4:]
        if sel == _sel("mint(uint256)"):
            (qty,) = dec(["uint256"], args)
        elif sel == _sel("presaleMint(uint256,bytes32[])"):
            qty, proof = dec(["uint256", "bytes32[]"], args)
            if self.proofs.get(sender) not in ["0x" + p.hex() for p in proof]:
                return "invalid proof"
        else:
            return "unknown function"
        if time.time() < self.sale_start:
            return "sale not active"
        if value != self.price * qty:
            return "wrong payment"
        if self.total + qty > self.max_supply:
            return "sold out"
        return None

    def mint(self, sender: str, data: bytes, tx_hash: str) -> list[dict]:
        from eth_abi import decode as dec
        (qty,) = dec(["uint256"], data[4:36])
        logs = []
        for _ in range(qty):
            self.total += 1
            logs.append({"address": self.address, "data": "0x", "topics": [
                "0x" + TRANSFER.hex(), "0x" + "00" * 32, "0x" + "00" * 12 + sender[2:],
                "0x" + self.total.to_bytes(32, "big").hex()], "transactionHash": tx_hash})
        return logs
