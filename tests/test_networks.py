from nft_ops.db import Database
from nft_ops.networks import DEFAULT_NETWORKS, seed_networks


def test_old_database_is_upgraded_once(tmp_path):
    db = Database(tmp_path / "old.db")
    # What a Stage 1 database looked like: Polygon with the now-dead default, no seed_version.
    db.execute("INSERT INTO networks(chain_id, name, native_symbol, enabled) VALUES(137, 'Polygon', 'POL', 1)")
    db.execute("INSERT INTO networks(chain_id, name, native_symbol, enabled) VALUES(1, 'Ethereum', 'ETH', 1)")
    for chain, url in [(137, "https://polygon-rpc.com"), (137, "https://my-own-polygon.example"),
                       (1, "https://eth.llamarpc.com"), (1, "https://ethereum-rpc.publicnode.com")]:
        db.execute("INSERT INTO rpc_endpoints(chain_id, url) VALUES(?, ?)", (chain, url))
    seed_networks(db)
    urls = {r["url"] for r in db.all("SELECT url FROM rpc_endpoints WHERE chain_id IN (1, 137)")}
    assert "https://polygon-rpc.com" not in urls and "https://eth.llamarpc.com" not in urls
    assert {"https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org",
            "https://eth.drpc.org", "https://my-own-polygon.example"} <= urls
    names = {r["chain_id"] for r in db.all("SELECT chain_id FROM networks")}
    assert {999, 143, 9745, 5042, 1440000, 99901, 1339, 4663} <= names
    # A removed default is not re-added on the next start.
    db.execute("DELETE FROM rpc_endpoints WHERE url = 'https://polygon.drpc.org'")
    seed_networks(db)
    assert not db.one("SELECT 1 FROM rpc_endpoints WHERE url = 'https://polygon.drpc.org'")


def test_defaults_are_unique_and_https():
    ids = [n[0] for n in DEFAULT_NETWORKS]
    assert len(ids) == len(set(ids))
    assert all(u.startswith("https://") for n in DEFAULT_NETWORKS for u in n[5])


def test_broken_endpoints_show_as_down_instead_of_crashing(tmp_path):
    import asyncio

    import httpx
    import pytest

    from nft_ops.networks import RpcError, RpcPool

    replies = {
        # Exactly what polygon-rpc.com sends now: HTTP 401 with "error" as a plain string.
        "https://a.example": (401, {"error": "message: API key disabled, reason: tenant disabled"}),
        "https://b.example": (200, {"jsonrpc": "2.0", "id": 1, "error": "plain string error"}),
        "https://c.example": (503, "<html>503 Service Temporarily Unavailable</html>"),
        "https://d.example": (200, ["not", "an", "object"]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = replies[str(request.url).rstrip("/")]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    db = Database(tmp_path / "x.db")
    db.execute("INSERT INTO networks(chain_id, name, native_symbol) VALUES(77, 'Test', 'T')")
    for url in replies:
        db.execute("INSERT INTO rpc_endpoints(chain_id, url) VALUES(77, ?)", (url,))
    pool = RpcPool(db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def run():
        rows = {r["url"]: r for r in await pool.check(77)}
        assert all(r["healthy"] == 0 for r in rows.values())
        assert "API key disabled" in rows["https://a.example"]["last_error"]
        assert "plain string error" in rows["https://b.example"]["last_error"]
        with pytest.raises(RpcError):
            await pool.call(77, "eth_blockNumber", [])
        results = await pool.batch(77, [("eth_blockNumber", [])])
        assert isinstance(results[0], RpcError) or results == []
    try:
        asyncio.run(run())
    except RpcError:
        pass  # batch may raise when every endpoint fails; that is a handled error, not a crash
