"""Backup / restore round trip and automatic locking."""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from nft_ops import backup
from nft_ops.server import AppState, create_app

PASSWORD = "a long enough password"
KEY = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


def _client(env, folder):
    state = AppState(folder, pool=env.pool, gateway_factory=env.gateway_factory())
    return TestClient(create_app(state, 8787), base_url="http://127.0.0.1:8787"), state


def test_backup_restore_round_trip(env, tmp_path):
    client, state = _client(env, tmp_path / "data")
    session = client.post("/api/vault/create", json={"password": PASSWORD}).json()["session"]
    h = {"X-Session": session}
    client.post("/api/wallets/import-keys", json={"text": f"main, {KEY}", "group": "vip"}, headers=h)
    assert client.post("/api/backup", json={"password": "wrong password!"}, headers=h).status_code == 400
    r = client.post("/api/backup", json={"password": PASSWORD}, headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert KEY not in r.content.hex() and KEY.encode() not in r.content    # keys stay encrypted
    archive = tmp_path / "b.zip"
    archive.write_bytes(r.content)
    assert backup.verify(archive)["app"] == "nft-ops"

    # Restore into a folder that already has data: it is moved aside, not deleted.
    target = tmp_path / "restored"
    target.mkdir()
    (target / "old.txt").write_text("keep me")
    moved = backup.restore(archive, target)
    assert (moved / "old.txt").read_text() == "keep me"
    client2, state2 = _client(env, target)
    session2 = client2.post("/api/vault/unlock", json={"password": PASSWORD}).json()["session"]
    wallets = client2.get("/api/wallets", headers={"X-Session": session2}).json()["wallets"]
    assert [(w["label"], w["groups"], w["has_key"]) for w in wallets] == [("main", ["vip"], True)]


def test_damaged_backup_is_refused(tmp_path, env):
    client, state = _client(env, tmp_path / "data")
    session = client.post("/api/vault/create", json={"password": PASSWORD}).json()["session"]
    content = client.post("/api/backup", json={"password": PASSWORD},
                          headers={"X-Session": session}).content
    src = zipfile.ZipFile(io.BytesIO(content))
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as out:
        for name in src.namelist():
            data = src.read(name)
            out.writestr(name, data + b" " if name == "nft-ops.db" else data)
    with pytest.raises(backup.BackupError, match="damaged"):
        backup.restore(bad, tmp_path / "x")
    assert not (tmp_path / "x").exists()                 # nothing was touched
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(backup.BackupError):
        backup.verify(tmp_path / "junk.zip")


def test_auto_lock_after_idle_but_never_mid_operation(env, tmp_path):
    client, state = _client(env, tmp_path / "data")
    session = client.post("/api/vault/create", json={"password": PASSWORD}).json()["session"]
    h = {"X-Session": session}
    client.post("/api/settings", json={"security": {"auto_lock_minutes": 5}}, headers=h)
    now = time.time()
    assert not state.maybe_auto_lock(now + 60)                         # not idle long enough
    state.mint.active_wallets["0xabc"] = 1                             # a mint is running
    assert not state.maybe_auto_lock(now + 3600)
    state.mint.active_wallets.clear()
    assert state.maybe_auto_lock(now + 3600)
    assert not state.vault.unlocked and client.get("/api/wallets", headers=h).status_code == 401
    # 0 = off
    session = client.post("/api/vault/unlock", json={"password": PASSWORD}).json()["session"]
    client.post("/api/settings", json={"security": {"auto_lock_minutes": 0}},
                headers={"X-Session": session})
    assert not state.maybe_auto_lock(now + 10 ** 6)
