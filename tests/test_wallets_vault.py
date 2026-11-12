import pytest

from nft_ops.vault import Vault, VaultError
from nft_ops.wallets import WalletError

KEY = "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
ADDR = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"


def test_import_formats_and_rejects_bad_keys(env):
    r = env.wallets.import_keys(f"# comment\nfirst, 0x{KEY}\n")
    assert r["addresses"] == [ADDR]
    assert env.wallets.list()[0]["label"] == "first"
    with pytest.raises(WalletError):
        env.wallets.import_keys(f"too long, 0x{KEY}ab")         # 66 hex chars: never truncate
    with pytest.raises(WalletError):
        env.wallets.import_keys("x, 0x" + "0" * 64)              # zero key
    again = env.wallets.import_keys(KEY)                        # duplicate is not added twice
    assert again["new"] == 0 and len(env.wallets.list()) == 1


def test_vault_roundtrip_and_password_change(env, tmp_path):
    env.wallets.import_keys(KEY)
    path = env.vault._path
    fresh = Vault(path)
    with pytest.raises(VaultError):
        fresh.unlock("wrong password!!")
    fresh.unlock("correct horse battery")
    assert fresh.addresses() == [ADDR]
    fresh.change_password("correct horse battery", "a brand new password")
    other = Vault(path)
    other.unlock("a brand new password")
    assert other.signer(ADDR).identity.address == ADDR
    other.lock()
    with pytest.raises(VaultError):
        other.signer(ADDR)


def test_tampered_vault_entry_is_detected(env):
    import json
    env.wallets.import_keys(KEY)
    data = json.loads(env.vault._path.read_text())
    entry = data["keys"][ADDR]
    entry["ct"] = entry["ct"][:-4] + ("AAAA" if not entry["ct"].endswith("AAAA") else "BBBB")
    env.vault._path.write_text(json.dumps(data))
    with pytest.raises(VaultError):
        Vault(env.vault._path).unlock("correct horse battery")


def test_groups_and_selection(env):
    a = env.wallets.generate(3, group="fcfs")
    env.wallets.add_to_group(a[:1], "wl")
    assert env.wallets.resolve_selection(group="fcfs", exclude=[a[1]]) == [a[0], a[2]]
    assert env.wallets.resolve_selection(group="wl") == [a[0]]
    env.wallets.rename_group("wl", "whitelist")
    assert {g["name"]: g["size"] for g in env.wallets.groups()} == {"fcfs": 3, "whitelist": 1}
    with pytest.raises(WalletError):
        env.wallets.resolve_selection(addresses=["0x000000000000000000000000000000000000dEaD"])
