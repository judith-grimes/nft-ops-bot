from __future__ import annotations

import time

import pytest
from fakes import FakeOpenSea, allowlist_metadata
from fastapi.testclient import TestClient

from nft_ops.server import AppState, create_app

PORT = 8787
KEY = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"


@pytest.fixture
def client(env, tmp_path):
    metadata = allowlist_metadata(time.time() - 60, time.time() + 3600)
    state = AppState(tmp_path / "srv", opensea_factory=lambda: FakeOpenSea(metadata, set()),
                     pool=env.pool, gateway_factory=env.gateway_factory())
    c = TestClient(create_app(state, PORT), base_url=f"http://127.0.0.1:{PORT}")
    c.state_obj = state
    return c


def login(c) -> dict:
    r = c.post("/api/vault/create", json={"password": "a long enough password"})
    assert r.status_code == 200
    return {"X-Session": r.json()["session"]}


def test_rejects_foreign_host_and_origin(client):
    assert client.get("/api/state", headers={"Host": "evil.example:8787"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/state").status_code == 200


def test_api_requires_session(client):
    assert client.get("/api/wallets").status_code == 401
    assert client.get("/api/wallets", headers={"X-Session": "guess"}).status_code == 401
    h = login(client)
    assert client.get("/api/wallets", headers=h).status_code == 200


def test_short_and_wrong_passwords(client):
    assert client.post("/api/vault/create", json={"password": "short"}).status_code == 400
    login(client)
    client.state_obj.vault.lock()
    r = client.post("/api/vault/unlock", json={"password": "not the password!"})
    assert r.status_code == 400 and "wrong password" in r.json()["error"]


def test_keys_never_come_back(client):
    h = login(client)
    r = client.post("/api/wallets/import-keys", json={"text": f"main, 0x{KEY}", "group": "wl"},
                    headers=h)
    assert r.status_code == 200 and r.json()["new"] == 1
    for path in ("/api/wallets", "/api/wallets/export.csv", "/api/overview", "/api/history"):
        assert KEY not in client.get(path, headers=h).text.lower()
    vault_file = (client.state_obj.vault._path).read_text()
    assert KEY not in vault_file.lower()                       # encrypted at rest
    wallets = client.get("/api/wallets", headers=h).json()["wallets"]
    assert wallets[0]["label"] == "main" and wallets[0]["groups"] == ["wl"]


def test_csv_refuses_secrets_and_validates(client):
    h = login(client)
    bad = client.post("/api/wallets/csv/validate", headers=h,
                      json={"text": f"address,label\n0x{KEY},x"}).json()
    assert not bad["ok"] and "private key" in bad["problems"][0]
    seed = "abandon " * 11 + "about"
    assert not client.post("/api/wallets/csv/validate", headers=h,
                           json={"text": f"address,label\n{seed},x"}).json()["ok"]
    good = client.post("/api/wallets/csv/validate", headers=h, json={"text": (
        "address,label,groups\n0x000000000000000000000000000000000000dEaD,=cmd(),a;b\n"
        "0x000000000000000000000000000000000000dEaD,dup,\nnot-an-address,x,\n")}).json()
    assert not good["ok"] and len(good["problems"]) == 2
    ok = client.post("/api/wallets/csv/import", headers=h, json={"text": (
        "address,label,groups\n0x000000000000000000000000000000000000dEaD,=cmd(),a;b\n")})
    assert ok.status_code == 200
    exported = client.get("/api/wallets/export.csv", headers=h).text
    assert "'=cmd()" in exported                                 # formula neutralised


def test_stop_and_resume(client):
    h = login(client)
    assert client.post("/api/stop", json={}, headers=h).json()["stopped"] is True
    assert client.get("/api/state").json()["stopped"] is True
    assert client.post("/api/resume", json={"confirm": "yes"}, headers=h).status_code == 400
    assert client.post("/api/resume", json={"confirm": "RESUME"}, headers=h).status_code == 200


def test_security_headers_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../server.py").status_code in (400, 404)


def test_new_eligibility_scan_replaces_old_results(client):
    with client:
        h = login(client)
        other = "11" * 32
        client.post("/api/wallets/import-keys", json={"text": f"a1, 0x{KEY}", "group": "a"}, headers=h)
        client.post("/api/wallets/import-keys", json={"text": f"b1, 0x{other}", "group": "b"}, headers=h)
        groups = {w["label"]: w["address"] for w in client.get("/api/wallets", headers=h).json()["wallets"]}

        def scan(group: str) -> list[str]:
            r = client.post("/api/eligibility/scan", json={"slug": "fake-drop", "group": group}, headers=h)
            assert r.status_code == 200, r.text
            for _ in range(200):
                d = client.get("/api/eligibility?slug=fake-drop", headers=h).json()
                if not d["progress"]["running"]:
                    break
                time.sleep(0.05)
            assert d["last_scan"]["group"] == group and d["last_scan"]["total"] == 1
            return sorted({r["address"] for r in d["rows"]})

        assert scan("a") == [groups["a1"]]
        assert scan("b") == [groups["b1"]]            # group a's old results are gone
        assert client.post("/api/eligibility/clear", json={"slug": "fake-drop"}, headers=h).status_code == 200
        d = client.get("/api/eligibility?slug=fake-drop", headers=h).json()
        assert d["rows"] == [] and d["last_scan"] is None
