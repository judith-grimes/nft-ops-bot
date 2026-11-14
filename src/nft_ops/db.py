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
CREATE TABLE IF NOT EXISTS scheduled_mints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    stage_index     INTEGER NOT NULL,
    stage_name      TEXT,
    stage_type      TEXT,
    chain_id        INTEGER,
    wallet_source   TEXT NOT NULL,      -- all | group | addresses
    group_name      TEXT,
    addresses       TEXT NOT NULL DEFAULT '[]',
    quantity        INTEGER NOT NULL DEFAULT 1,
    max_total       TEXT NOT NULL DEFAULT '',
    mode            TEXT NOT NULL,      -- monitor | confirm | auto
    phase_starts_at REAL,
    phase_ends_at   REAL,
    run_at          REAL NOT NULL,      -- when the bot starts (phase start - lead)
    lead_seconds    INTEGER NOT NULL DEFAULT 120,
    timezone        TEXT NOT NULL DEFAULT 'Asia/Jakarta',
    status          TEXT NOT NULL,
    status_detail   TEXT,
    paused_from     TEXT,
    job_id          INTEGER,
    readiness       TEXT,
    checked_at      REAL,
    eligibility_checked INTEGER NOT NULL DEFAULT 0,
    report          TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_readiness (
    slug        TEXT NOT NULL,
    stage_index INTEGER NOT NULL,
    address     TEXT NOT NULL,
    score       INTEGER NOT NULL,
    status      TEXT NOT NULL,
    checks      TEXT NOT NULL,
    checked_at  REAL NOT NULL,
    PRIMARY KEY (slug, stage_index, address)
);
CREATE TABLE IF NOT EXISTS eligibility_scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    label       TEXT,
    total       INTEGER NOT NULL,
    counts      TEXT NOT NULL,
    results     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS os_trades (
    chain_id    INTEGER NOT NULL,
    address     TEXT NOT NULL,
    tx_hash     TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    slug        TEXT,
    side        TEXT NOT NULL,          -- buy | sell
    amount      REAL,                   -- price in the chain's native coin (None if unknown)
    symbol      TEXT,
    ts          REAL NOT NULL,
    PRIMARY KEY (tx_hash, token_id, address, side)
);
CREATE TABLE IF NOT EXISTS scatter_proofs (
    chain_id    INTEGER NOT NULL,
    contract    TEXT NOT NULL,
    key         TEXT NOT NULL,          -- phase key = allowlist Merkle root
    address     TEXT NOT NULL,
    proof       TEXT NOT NULL,          -- JSON list of 0x hashes, verified against the root
    created_at  REAL NOT NULL,
    PRIMARY KEY (chain_id, contract, key, address)
);
CREATE TABLE IF NOT EXISTS os_holdings (
    chain_id    INTEGER NOT NULL,
    address     TEXT NOT NULL,          -- owner wallet
    contract    TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    slug        TEXT NOT NULL,
    name        TEXT,
    standard    TEXT,
    quantity    TEXT NOT NULL DEFAULT '1',
    traits      TEXT,                   -- JSON list or NULL when unknown
    image_url   TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (chain_id, address, contract, token_id)
);
CREATE TABLE IF NOT EXISTS floor_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL,
    ts            REAL NOT NULL,
    floor_native  REAL,                 -- NULL when there is no listing
    floor_symbol  TEXT,
    sales_median  REAL,                 -- median sale price (native) over the sales window
    sales_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS floor_history_slug ON floor_history(slug, ts);
CREATE TABLE IF NOT EXISTS market_collections (
    slug        TEXT PRIMARY KEY,
    chain_id    INTEGER,
    contract    TEXT,
    name        TEXT,
    info        TEXT,                   -- JSON: fees, pricing currencies
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS offer_rules (
    slug        TEXT PRIMARY KEY,       -- '*' = defaults for every collection
    rules       TEXT NOT NULL,          -- JSON
    armed       INTEGER NOT NULL DEFAULT 0,
    armed_at    REAL,
    paused      TEXT,                   -- reason, NULL when not paused
    paused_at   REAL,
    fail_streak INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS offers_current (
    order_hash  TEXT NOT NULL,
    chain_id    INTEGER NOT NULL,
    contract    TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    wallet      TEXT NOT NULL,
    slug        TEXT NOT NULL,
    evaluation  TEXT NOT NULL,          -- JSON
    qualifies   INTEGER NOT NULL DEFAULT 0,
    net_native  REAL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (order_hash, contract, token_id, wallet)
);
CREATE TABLE IF NOT EXISTS offer_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    slug          TEXT,
    nft_name      TEXT,
    contract      TEXT,
    token_id      TEXT,
    wallet        TEXT,
    offerer       TEXT,
    order_hash    TEXT,
    offer_type    TEXT,
    currency      TEXT,
    gross         TEXT,                 -- in offer currency
    gross_native  REAL,
    market_fee    REAL,                 -- native
    creator_fee   REAL,
    gas_native    REAL,
    net_native    REAL,
    floor_native  REAL,
    tx_hash       TEXT,
    status        TEXT NOT NULL,        -- accepted | failed | skipped | rejected | pending | awaiting
    reason        TEXT,
    mode          TEXT
);
CREATE INDEX IF NOT EXISTS offer_log_ts ON offer_log(ts);
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
        logcols = {r[1] for r in self._conn.execute("PRAGMA table_info(offer_log)").fetchall()}
        if "chain_id" not in logcols:
            self._conn.execute("ALTER TABLE offer_log ADD COLUMN chain_id INTEGER")
        gcols = {r[1] for r in self._conn.execute("PRAGMA table_info(groups)").fetchall()}
        for column in ("role TEXT NOT NULL DEFAULT ''", "after_mint TEXT NOT NULL DEFAULT 'keep'", "vault TEXT"):
            if column.split()[0] not in gcols:
                self._conn.execute(f"ALTER TABLE groups ADD COLUMN {column}")
        acols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)").fetchall()}
        for column in ("category TEXT NOT NULL DEFAULT 'collection'", "title TEXT", "link TEXT"):
            if column.split()[0] not in acols:
                self._conn.execute(f"ALTER TABLE alerts ADD COLUMN {column}")
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
