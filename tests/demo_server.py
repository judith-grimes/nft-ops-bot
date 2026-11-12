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
from fakes import CustomDrop, FakeOpenSea, allowlist_metadata

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
    env.chain.custom[custom] = CustomDrop(custom)          # a non-OpenSea drop, sale open
    holder: dict = {}
    state = AppState(tmp / "data", opensea_factory=lambda: DemoOpenSea(metadata, holder["s"]),
                     pool=env.pool, gateway_factory=env.gateway_factory())
    holder["s"] = state
    state.db.execute("DELETE FROM rpc_endpoints WHERE chain_id = 4663")
    state.db.execute("INSERT INTO rpc_endpoints(chain_id, url, priority) VALUES(4663, ?, 0)",
                     ("http://127.0.0.1:8545",))
    uvicorn.run(create_app(state, port), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
