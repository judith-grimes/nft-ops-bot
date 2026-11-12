"""SQLite store. Holds no secrets: private keys live only in the encrypted vault file."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address     TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    has_key     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS wallet_groups (
    address   TEXT NOT NULL REFERENCES wallets(address) ON DELETE CASCADE,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (address, group_id)
);
CREATE TABLE IF NOT EXISTS networks (
    chain_id       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    native_symbol  TEXT NOT NULL,
    explorer_url   TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rpc_endpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id       INTEGER NOT NULL REFERENCES networks(chain_id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 100,
    enabled        INTEGER NOT NULL DEFAULT 1,
    healthy        INTEGER,
    latency_ms     REAL,
    block_number   INTEGER,
    last_error     TEXT,
    checked_at     REAL,
    UNIQUE (chain_id, url)
);
CREATE TABLE IF NOT EXISTS tokens (
    chain_id  INTEGER NOT NULL,
    address   TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    decimals  INTEGER NOT NULL,
    PRIMARY KEY (chain_id, address)
);
CREATE TABLE IF NOT EXISTS balances (
    chain_id    INTEGER NOT NULL,
    address     TEXT NOT NULL,
    asset       TEXT NOT NULL,          -- 'native', token address, or 'nft:<contract>'
    amount      TEXT NOT NULL,          -- integer string (wei / token units / NFT count)
    updated_at  REAL NOT NULL,
    PRIMARY KEY (chain_id, address, asset)
);
CREATE TABLE IF NOT EXISTS collections (
    slug        TEXT PRIMARY KEY,
    address     TEXT NOT NULL,
    chain_id    INTEGER NOT NULL,
    chain_name  TEXT NOT NULL,
    drop_kind   TEXT NOT NULL,
    data        TEXT NOT NULL,          -- JSON CollectionMetadata
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS eligibility (
    slug          TEXT NOT NULL,
    address       TEXT NOT NULL,
    stage_index   INTEGER NOT NULL,
    status        TEXT NOT NULL,        -- ELIGIBLE | NOT_ELIGIBLE | UNKNOWN | UNVERIFIED
    detail        TEXT NOT NULL DEFAULT '',
    max_quantity  INTEGER,
    minted        INTEGER,
    price_wei     TEXT,
    source        TEXT NOT NULL,
    checked_at    REAL NOT NULL,
    PRIMARY KEY (slug, address, stage_index)
);
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,         -- mint | eligibility
    slug         TEXT,
    stage_index  INTEGER,
    stage_name   TEXT,
    chain_id     INTEGER,
    params       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL,         -- running | finished | stopped
    created_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    job_id          INTEGER,
    operation       TEXT NOT NULL,      -- mint
    chain_id        INTEGER,
    address         TEXT NOT NULL,
    slug            TEXT,
    stage_name      TEXT,
    quantity        INTEGER,
    tx_hash         TEXT,
    nonce           INTEGER,
    value_wei       TEXT,
    max_cost_wei    TEXT,              -- worst case reserved at broadcast
    gas_used        INTEGER,
    gas_cost_wei    TEXT,
    status          TEXT NOT NULL,      -- not_submitted|queued|submitted|pending|confirmed|failed|cancelled|blocked
    error           TEXT,
    recipient       TEXT
);
CREATE INDEX IF NOT EXISTS history_created ON history(created_at);
CREATE INDEX IF NOT EXISTS history_job ON history(job_id);
CREATE TABLE IF NOT EXISTS address_book (
    address     TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recipient_lists (
    name        TEXT PRIMARY KEY,
    rows        TEXT NOT NULL,          -- JSON [{address, amount?, percent?, label?}]
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nft_tokens (
    chain_id    INTEGER NOT NULL,
    contract    TEXT NOT NULL,
    token_id    TEXT NOT NULL,          -- decimal string
    owner       TEXT NOT NULL,
    standard    TEXT NOT NULL,          -- erc721 | erc1155
    amount      TEXT NOT NULL DEFAULT '1',
    source      TEXT NOT NULL,          -- mint | enumerable | logs | manual
    updated_at  REAL NOT NULL,
    PRIMARY KEY (chain_id, contract, token_id, owner)
);
CREATE TABLE IF NOT EXISTS contract_reports (
    chain_id    INTEGER NOT NULL,
    address     TEXT NOT NULL,
    report      TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (chain_id, address)
);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    slug        TEXT,
    kind        TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',   -- info | good | warn
    message     TEXT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after Stage 1 to existing databases."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(collections)").fetchall()}
        for column in ("watch INTEGER NOT NULL DEFAULT 1", "snapshot TEXT", "next_check REAL"):
            if column.split()[0] not in cols:
                self._conn.execute(f"ALTER TABLE collections ADD COLUMN {column}")
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(history)").fetchall()}
        for column in ("asset TEXT", "token_id TEXT", "amount TEXT", "spend_wei TEXT"):
            if column.split()[0] not in have:
                self._conn.execute(f"ALTER TABLE history ADD COLUMN {column}")

    def close(self) -> None:
        self._conn.close()

    def backup_to(self, target: sqlite3.Connection) -> None:
        with self._lock:
            self._conn.backup(target)

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ----- settings --------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ----- history ---------------------------------------------------------------------
    def add_history(self, **fields: Any) -> int:
        now = time.time()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(f"INSERT INTO history({names}) VALUES({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def update_history(self, row_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE history SET {sets} WHERE id = ?", (*fields.values(), row_id))
