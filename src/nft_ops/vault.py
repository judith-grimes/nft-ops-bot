"""Encrypted local key vault.

One JSON file holds every private key, each encrypted with AES-256-GCM under a key derived
from the user's password with scrypt. The password is never stored. Decrypted keys exist only
in memory while the vault is unlocked and are never returned by any API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from eth_utils.address import to_checksum_address

from osnm_z.signing import WalletSigner, WalletSignerError

FORMAT = "nft-ops-vault"
VERSION = 1
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD_LENGTH = 10
_VERIFIER_PLAINTEXT = b"nft-ops-vault-ok"


class VaultError(Exception):
    """Vault is missing, locked, or the password/key material is invalid."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=512 * 1024 * 1024, dklen=32
    )


def parse_private_key(text: str) -> bytes:
    """Accept a 64-hex-character key with or without 0x; never echo the input in errors."""
    value = text.strip()
    if value.startswith(("0x", "0X")):
        value = value[2:]
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise VaultError("not a valid private key (expected 64 hex characters)")
    raw = bytes.fromhex(value)
    if int.from_bytes(raw, "big") == 0:
        raise VaultError("not a valid private key")
    return raw


def address_of(raw_key: bytes) -> str:
    return to_checksum_address(Account.from_key(raw_key).address)


class Vault:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._aes: AESGCM | None = None
        self._signers: dict[str, WalletSigner] = {}
        self._data: dict | None = None

    # ----- state -----------------------------------------------------------------------
    def exists(self) -> bool:
        return self._path.is_file()

    @property
    def unlocked(self) -> bool:
        return self._aes is not None

    def addresses(self) -> list[str]:
        with self._lock:
            self._require_unlocked()
            return sorted(self._signers)

    def has(self, address: str) -> bool:
        with self._lock:
            return to_checksum_address(address) in self._signers

    def signer(self, address: str) -> WalletSigner:
        with self._lock:
            self._require_unlocked()
            try:
                return self._signers[to_checksum_address(address)]
            except KeyError:
                raise VaultError(f"no private key stored for {address}") from None

    # ----- lifecycle -------------------------------------------------------------------
    def create(self, password: str) -> None:
        with self._lock:
            if self.exists():
                raise VaultError("a vault already exists")
            _check_password(password)
            salt = secrets.token_bytes(16)
            key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
            aes = AESGCM(key)
            nonce = secrets.token_bytes(12)
            data = {
                "format": FORMAT,
                "version": VERSION,
                "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                        "salt": _b64(salt)},
                "verifier": {"nonce": _b64(nonce),
                             "ct": _b64(aes.encrypt(nonce, _VERIFIER_PLAINTEXT, b"verifier"))},
                "keys": {},
            }
            self._write(data)
            self._data, self._aes, self._signers = data, aes, {}

    def unlock(self, password: str) -> None:
        with self._lock:
            data = self._read()
            kdf = data["kdf"]
            key = _derive(password, _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
            aes = AESGCM(key)
            try:
                plain = aes.decrypt(_unb64(data["verifier"]["nonce"]),
                                    _unb64(data["verifier"]["ct"]), b"verifier")
            except InvalidTag:
                raise VaultError("wrong password") from None
            if plain != _VERIFIER_PLAINTEXT:
                raise VaultError("wrong password")
            signers: dict[str, WalletSigner] = {}
            for address, entry in data["keys"].items():
                try:
                    raw = aes.decrypt(_unb64(entry["nonce"]), _unb64(entry["ct"]),
                                      address.lower().encode())
                    signer = WalletSigner.from_private_key(raw.hex())
                except (InvalidTag, WalletSignerError):
                    raise VaultError(f"vault entry for {address} is corrupted") from None
                if signer.identity.address != address:
                    raise VaultError(f"vault entry for {address} does not match its key")
                signers[address] = signer
            self._data, self._aes, self._signers = data, aes, signers

    def verify(self, password: str) -> bool:
        """Check a password without changing what is unlocked."""
        data = self._read()
        kdf = data["kdf"]
        aes = AESGCM(_derive(password, _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"]))
        try:
            return aes.decrypt(_unb64(data["verifier"]["nonce"]), _unb64(data["verifier"]["ct"]),
                               b"verifier") == _VERIFIER_PLAINTEXT
        except InvalidTag:
            return False

    def lock(self) -> None:
        with self._lock:
            self._aes = None
            self._signers = {}
            self._data = None

    def change_password(self, old: str, new: str) -> None:
        with self._lock:
            _check_password(new)
            self.unlock(old)
            raw_keys = {a: bytes.fromhex(self._raw_hex(a)) for a in self._signers}
            saved = {n: self.get_secret(n) for n in (self._data or {}).get("secrets", {})}
            salt = secrets.token_bytes(16)
            aes = AESGCM(_derive(new, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P))
            nonce = secrets.token_bytes(12)
            data = {
                "format": FORMAT,
                "version": VERSION,
                "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                        "salt": _b64(salt)},
                "verifier": {"nonce": _b64(nonce),
                             "ct": _b64(aes.encrypt(nonce, _VERIFIER_PLAINTEXT, b"verifier"))},
                "keys": {a: self._encrypt_entry(aes, a, k) for a, k in raw_keys.items()},
                "secrets": {n: self._encrypt_secret(aes, n, v) for n, v in saved.items() if v},
            }
            self._write(data)
            self._data, self._aes = data, aes

    # ----- keys ------------------------------------------------------------------------
    def add_keys(self, raw_keys: list[bytes]) -> list[tuple[str, bool]]:
        """Encrypt and store keys. Returns (address, newly_added) for each input."""
        with self._lock:
            self._require_unlocked()
            assert self._data is not None and self._aes is not None
            results: list[tuple[str, bool]] = []
            for raw in raw_keys:
                address = address_of(raw)
                if address in self._signers:
                    results.append((address, False))
                    continue
                self._data["keys"][address] = self._encrypt_entry(self._aes, address, raw)
                self._signers[address] = WalletSigner.from_private_key(raw.hex())
                results.append((address, True))
            self._write(self._data)
            return results

    def generate(self, count: int) -> list[str]:
        if not 1 <= count <= 500:
            raise VaultError("generate between 1 and 500 wallets at a time")
        return [a for a, _ in self.add_keys([secrets.token_bytes(32) for _ in range(count)])]

    def remove(self, addresses: list[str]) -> int:
        with self._lock:
            self._require_unlocked()
            assert self._data is not None
            removed = 0
            for address in addresses:
                checksum = to_checksum_address(address)
                if self._data["keys"].pop(checksum, None) is not None:
                    self._signers.pop(checksum, None)
                    removed += 1
            self._write(self._data)
            return removed

    # ----- other secrets (e.g. the OpenSea API key) ----------------------------------------
    def set_secret(self, name: str, value: str) -> None:
        with self._lock:
            self._require_unlocked()
            assert self._data is not None and self._aes is not None
            store = self._data.setdefault("secrets", {})
            if value:
                store[name] = self._encrypt_secret(self._aes, name, value)
            else:
                store.pop(name, None)
            self._write(self._data)

    def get_secret(self, name: str) -> str | None:
        with self._lock:
            if self._aes is None or self._data is None:
                return None
            entry = self._data.get("secrets", {}).get(name)
            if entry is None:
                return None
            try:
                return self._aes.decrypt(_unb64(entry["nonce"]), _unb64(entry["ct"]),
                                         b"secret:" + name.encode()).decode()
            except InvalidTag:
                raise VaultError(f"stored secret {name} is corrupted") from None

    def has_secret(self, name: str) -> bool:
        with self._lock:
            return bool(self._data and name in self._data.get("secrets", {}))

    @staticmethod
    def _encrypt_secret(aes: AESGCM, name: str, value: str) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        return {"nonce": _b64(nonce), "ct": _b64(aes.encrypt(nonce, value.encode(), b"secret:" + name.encode()))}

    # ----- internals -------------------------------------------------------------------
    def _raw_hex(self, address: str) -> str:
        assert self._data is not None and self._aes is not None
        entry = self._data["keys"][address]
        return self._aes.decrypt(
            _unb64(entry["nonce"]), _unb64(entry["ct"]), address.lower().encode()
        ).hex()

    @staticmethod
    def _encrypt_entry(aes: AESGCM, address: str, raw: bytes) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        return {"nonce": _b64(nonce), "ct": _b64(aes.encrypt(nonce, raw, address.lower().encode()))}

    def _require_unlocked(self) -> None:
        if self._aes is None:
            raise VaultError("vault is locked")

    def _read(self) -> dict:
        if not self.exists():
            raise VaultError("no vault exists yet")
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT or data.get("version") != VERSION:
            raise VaultError("unsupported vault file")
        return data

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise VaultError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
