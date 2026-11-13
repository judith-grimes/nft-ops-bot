"""Run the dashboard against the fake chain and fake OpenSea (no real network, no real funds).

    uv run python tests/demo_server.py [port]

Used for UI testing: every wallet is funded on the fake chain; the first half are on the
allowlist of a fake drop whose allowlist phase opens 90 seconds after start.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).parent))
from conftest import Env
from fake_opensea_api import WETH, FakeOffer, FakeOpenSeaAPI
from fakes import CustomDrop, FakeOpenSea, allowlist_metadata

from nft_ops.opensea_api import OpenSeaAPI

from nft_ops.server import AppState, create_app


class DemoOpenSea(FakeOpenSea):
    """Allowlist = every wallet whose label ends in an odd number."""

    def __init__(self, metadata, state):
        super().__init__(metadata, set())
        self._state = state

    async def eligibility(self, slug, wallet):
        labels = {w["address"].lower(): w["label"] for w in self._state.wallets.list()}
        label = labels.get(wallet.lower(), "")
        self.allowlist = {wallet.lower()} if label[-1:].isdigit() and int(label[-1]) % 2 else set()
        return await super().eligibility(slug, wallet)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    tmp = Path(tempfile.mkdtemp(prefix="nftops-demo-"))
    env = Env(tmp / "env")
    original = env.chain._dispatch

    async def rich(method, params):  # every wallet starts with 1 ETH on the fake chain
        if method == "eth_getBalance" and params[0].lower() not in env.chain.balances:
            env.chain.balances[params[0].lower()] = 10**18
        return await original(method, params)
    env.chain._dispatch = rich
    delay = float(os.environ.get("DEMO_START_DELAY", "90"))
    metadata = allowlist_metadata(time.time() + delay, time.time() + 3600)
    custom = "0x4444444444444444444444444444444444444444"
    env.chain.custom[custom] = CustomDrop(custom, sale_start=time.time() + float(os.environ.get("DEMO_SALE_DELAY", "0")))
    env.chain.mining = os.environ.get("DEMO_NO_MINING") != "1"   # 1 = transactions stay pending
    # Simulated OpenSea offers: every wallet that asks gets two NFTs with offers on them.
    nft = "0x7777777777777777777777777777777777777777"
    buyer = "0x00000000000000000000000000000000000000b1"
    env.chain.erc20[WETH] = {buyer: 1000 * 10**18}
    market = FakeOpenSeaAPI(env.chain, nft)
    original_handle = market._handle
    counter = {"next": 1}

    def demo_handle(request):
        path = request.url.path
        if "/account/" in path:
            owner = path.split("/account/")[1].split("/")[0].lower()
            tokens = env.chain.erc721.setdefault(nft, {})
            if owner not in tokens.values():
                for _ in range(2):
                    tid = counter["next"]
                    counter["next"] += 1
                    tokens[tid] = owner
                    market.offers.append(FakeOffer(f"0x{tid:064x}", buyer, int(0.12 * 10**18), nft, tid))
                    market.offers.append(FakeOffer(f"0x{tid + 5000:064x}", buyer, int(0.0955 * 10**18), nft, tid))
        return original_handle(request)
    market._handle = demo_handle
    holder: dict = {}
    state = AppState(tmp / "data", opensea_factory=lambda: DemoOpenSea(metadata, holder["s"]),
                     pool=env.pool, gateway_factory=env.gateway_factory(),
                     opensea_api=OpenSeaAPI(lambda: "test-key", client=market.client(), requests_per_second=1000))
    holder["s"] = state
    state.db.execute("DELETE FROM rpc_endpoints WHERE chain_id = 4663")
    state.db.execute("INSERT INTO rpc_endpoints(chain_id, url, priority) VALUES(4663, ?, 0)",
                     ("http://127.0.0.1:8545",))
    uvicorn.run(create_app(state, port), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
