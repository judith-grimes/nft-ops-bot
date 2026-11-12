"""Encrypted backup and restore of the data folder.

A backup zip holds:
  vault.json     your keys, still encrypted with your vault password (never decrypted here)
  nft-ops.db     a consistent snapshot of the database (wallet labels, groups, history, ...)
  manifest.json  version, time and a SHA-256 of each file, checked on restore
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__

FILES = ("vault.json", "nft-ops.db")


class BackupError(Exception):
    pass


def create(data_dir: Path, db) -> tuple[bytes, str]:
    """Return (zip bytes, file name)."""
    vault = data_dir / "vault.json"
    if not vault.is_file():
        raise BackupError("no vault to back up yet")
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "nft-ops.db"
        target = sqlite3.connect(snapshot)
        try:
            db.backup_to(target)            # consistent copy even while the app is running
        finally:
            target.close()
        contents = {"vault.json": vault.read_bytes(), "nft-ops.db": snapshot.read_bytes()}
    manifest = {
        "app": "nft-ops", "version": __version__, "created_at": time.time(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        "note": "vault.json is encrypted with your vault password. Keep this file private.",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
    name = time.strftime("nft-ops-backup-%Y%m%d-%H%M%S.zip")
    return buffer.getvalue(), name


def verify(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names or not set(FILES) <= names:
                raise BackupError("not an NFT Ops backup (files missing)")
            manifest = json.loads(zf.read("manifest.json"))
            for name in FILES:
                if hashlib.sha256(zf.read(name)).hexdigest() != manifest["files"].get(name):
                    raise BackupError(f"{name} in the backup is damaged (checksum mismatch)")
            vault = json.loads(zf.read("vault.json"))
            if vault.get("format") != "nft-ops-vault":
                raise BackupError("vault.json in the backup is not an NFT Ops vault")
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise BackupError(f"cannot read backup: {error}") from None
    return manifest


def restore(zip_path: Path, data_dir: Path) -> Path | None:
    """Check the backup, move the current data aside, then unpack. Returns the moved folder."""
    verify(zip_path)
    moved = None
    if data_dir.exists() and any(data_dir.iterdir()):
        moved = data_dir.with_name(f"{data_dir.name}-before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(str(data_dir), str(moved))
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in FILES:
            (data_dir / name).write_bytes(zf.read(name))
    return moved
