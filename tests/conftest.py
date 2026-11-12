from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import CHAIN_ID, FakeChain

from nft_ops.db import Database
from nft_ops.events import EventBus
from nft_ops.networks import RpcPool, seed_networks
from nft_ops.safety import Safety
from nft_ops.vault import Vault
from nft_ops.wallets import WalletService

FAKE_RPC = "http://127.0.0.1:8545"
PASSWORD = "correct horse battery"


class Env:
    def __init__(self, tmp: Path) -> None:
        self.chain = FakeChain()
        self.db = Database(tmp / "nft-ops.db")
        seed_networks(self.db)
        self.db.execute("DELETE FROM rpc_endpoints WHERE chain_id = ?", (CHAIN_ID,))
        self.db.execute("INSERT INTO rpc_endpoints(chain_id, url, priority) VALUES(?, ?, 0)",
                        (CHAIN_ID, FAKE_RPC))
        self.vault = Vault(tmp / "vault.json")
        self.vault.create(PASSWORD)
        self.bus = EventBus()
        self.safety = Safety(self.db, self.bus)
        self.pool = RpcPool(self.db, client=self.chain.client())
        self.wallets = WalletService(self.db, self.vault)

    def gateway_factory(self):
        from osnm_z.chain import ChainGateway

        def make(timeout: float, conns: int) -> ChainGateway:
            gateway = ChainGateway(timeout, max_connections=conns)
            gateway._client = self.chain.client()
            return gateway
        return make


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)
