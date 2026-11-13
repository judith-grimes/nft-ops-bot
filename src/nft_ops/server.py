"""Local HTTP API and dashboard. Binds to 127.0.0.1 only.

Protection against other websites in the same browser:
  * every /api call except state/create/unlock needs the X-Session token returned by unlock;
    a custom header cannot be sent cross-site without a CORS preflight, which is never granted;
  * the Host header must be 127.0.0.1/localhost on our port (blocks DNS-rebinding);
  * requests carrying a foreign Origin are rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

from osnm_z.opensea_protocol import ProtocolError

from . import (
    __version__,
    addressbook,
    backup,
    balances,
    collections,
    contracts,
    custommint,
    eligibility,
    market,
    nfts,
    transfers,
)
from .balances import format_units
from .collections import CollectionError, OpenSeaFactory, default_opensea_factory
from .contracts import ContractError
from .db import Database
from .events import EventBus
from .mint import DEFAULT_MINT_SETTINGS, MintEngine, MintEngineError, MintParams
from .monitor import Monitor
from .networks import RpcError, RpcPool, seed_networks, validate_rpc_url
from .nfts import NftError
from .offers import OfferEngine, OfferError
from .opensea_api import OpenSeaAPI, OpenSeaError
from .os_signin import SHARED_LIMITER
from .replace import ReplaceError, Replacer
from .safety import LIMIT_LABELS, Safety, from_wei
from .seaport import SeaportError
from .txengine import TxEngine, TxEngineError
from .vault import Vault, VaultError
from .wallets import WalletError, WalletService, checksum, safe_cell

STATIC = Path(__file__).parent / "static"
USER_ERRORS = (WalletError, VaultError, CollectionError, MintEngineError, TxEngineError, NftError,
               ContractError, ReplaceError, backup.BackupError, OfferError, OpenSeaError,
               SeaportError,
               RpcError, ValueError,
               ProtocolError, KeyError)
OPEN_PATHS = {"/api/state", "/api/vault/create", "/api/vault/unlock"}


class AppState:
    def __init__(self, data_dir: Path, opensea_factory: OpenSeaFactory | None = None,
                 pool: RpcPool | None = None, gateway_factory=None,
                 opensea_api: OpenSeaAPI | None = None) -> None:
        self.db = Database(data_dir / "nft-ops.db")
        seed_networks(self.db)
        self.vault = Vault(data_dir / "vault.json")
        self.bus = EventBus()
        self.safety = Safety(self.db, self.bus)
        self.pool = pool or RpcPool(self.db)
        self.wallets = WalletService(self.db, self.vault)
        self.opensea_factory = opensea_factory or default_opensea_factory()
        self.mint = MintEngine(self.db, self.vault, self.pool, self.bus, self.safety,
                               self.opensea_factory, gateway_factory)
        self.sessions: set[str] = set()
        self.scan_task: asyncio.Task | None = None
        self.scan_progress: dict[str, Any] = {}
        self.data_dir = data_dir
        self.last_activity = time.time()
        self.tx = TxEngine(self.db, self.vault, self.pool, self.bus, self.safety)
        self.replacer = Replacer(self.tx)
        self.opensea = opensea_api or OpenSeaAPI(lambda: self.vault.get_secret("opensea_api_key"))
        self.offers = OfferEngine(self.db, self.vault, self.pool, self.bus, self.safety, self.tx, self.opensea)
        # A wallet can be in a mint or a transfer operation, never both (nonce safety).
        self.tx.external_busy = self.mint.active_wallets.get
        self.mint.external_busy = self.tx.busy_sources.get
        SHARED_LIMITER.on_pause = lambda seconds: self.bus.publish("signin_pause", seconds=round(seconds))
        self.monitor = Monitor(self.db, self.pool, self.bus, self.opensea_factory, self.auto_rescan)

    def busy(self) -> bool:
        """Anything running that must not be interrupted by an automatic lock."""
        return bool(self.mint.active_wallets or self.tx.busy_sources or self.offers.locks
                    or (self.scan_task and not self.scan_task.done()))

    def auto_lock_minutes(self) -> int:
        return int((self.db.get_setting("security", {}) or {}).get("auto_lock_minutes", 30))

    def maybe_auto_lock(self, now: float | None = None) -> bool:
        minutes = self.auto_lock_minutes()
        now = time.time() if now is None else now
        if (minutes <= 0 or not self.vault.unlocked or self.busy()
                or now - self.last_activity < minutes * 60):
            return False
        self.vault.lock()
        self.sessions.clear()
        self.bus.publish("locked", reason=f"locked after {minutes} min without activity")
        return True

    async def auto_rescan(self, slug: str) -> dict | None:
        """Monitor-triggered eligibility re-check (skipped if a manual scan is running)."""
        if self.scan_task and not self.scan_task.done():
            return None
        meta = collections.get(self.db, slug)
        addresses = [r["address"] for r in self.db.all(
            "SELECT DISTINCT address FROM eligibility WHERE slug = ?", (slug,))] or [
            r["address"] for r in self.db.all("SELECT address FROM wallets WHERE has_key = 1")]
        settings = self.db.get_setting("eligibility", {}) or {}
        return await eligibility.scan(self.db, self.opensea_factory, self.vault, meta, addresses,
                                      concurrency=int(settings.get("concurrency", 2)),
                                      spacing_seconds=float(settings.get("spacing_seconds", 1.0)),
                                      should_stop=lambda: self.safety.stopped)


def create_app(state: AppState, port: int) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        async def health_loop() -> None:
            while True:
                for net in state.db.all("SELECT chain_id FROM networks WHERE enabled = 1"):
                    try:
                        await state.pool.check(net["chain_id"])
                    except Exception:  # noqa: BLE001 - health loop must never die
                        pass
                await asyncio.sleep(60)
        task = asyncio.create_task(health_loop())
        monitor_task = asyncio.create_task(state.monitor.run_forever())

        async def lock_loop() -> None:
            while True:
                await asyncio.sleep(15)
                try:
                    state.maybe_auto_lock()
                except Exception:  # noqa: BLE001 - never let the loop die
                    pass
        lock_task = asyncio.create_task(lock_loop())
        offers_task = asyncio.create_task(state.offers.run_forever())
        try:
            yield
        finally:
            task.cancel()
            monitor_task.cancel()
            lock_task.cancel()
            offers_task.cancel()

    app = FastAPI(title="nft-ops", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("forbidden host", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return PlainTextResponse("forbidden origin", status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_PATHS:
            token = request.headers.get("x-session", "")
            if not token or token not in state.sessions:
                return JSONResponse({"error": "locked", "locked": True}, status_code=401)
            if path not in ("/api/events", "/api/alerts"):   # background polling is not activity
                state.last_activity = time.time()
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return response

    async def user_error(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"error": str(error) or type(error).__name__}, status_code=400)

    for error_type in USER_ERRORS:
        app.add_exception_handler(error_type, user_error)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise ValueError("invalid JSON body") from None
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def ok(**data: Any) -> dict:
        return {"ok": True, **data}

    # ----- pages -----------------------------------------------------------------------
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    async def static(name: str) -> FileResponse:
        if name not in {"app.js", "style.css", "icon.svg"}:
            raise KeyError("not found")
        return FileResponse(STATIC / name)

    # ----- vault & session -------------------------------------------------------------
    @app.get("/api/state")
    async def api_state() -> dict:
        return {"vault_exists": state.vault.exists(), "unlocked": state.vault.unlocked,
                "stopped": state.safety.stopped, "version": __version__}

    def new_session() -> str:
        token = secrets.token_urlsafe(32)
        state.sessions.add(token)
        return token

    @app.post("/api/vault/create")
    async def vault_create(request: Request) -> dict:
        data = await body(request)
        state.vault.create(str(data.get("password", "")))
        return ok(session=new_session())

    @app.post("/api/vault/unlock")
    async def vault_unlock(request: Request) -> dict:
        data = await body(request)
        await asyncio.sleep(0.3)  # slow down password guessing
        await asyncio.to_thread(state.vault.unlock, str(data.get("password", "")))
        return ok(session=new_session())

    @app.post("/api/vault/lock")
    async def vault_lock() -> dict:
        if state.mint.active_wallets or state.tx.busy_sources:
            raise ValueError("an operation is running; stop it or wait before locking")
        state.vault.lock()
        state.sessions.clear()
        return ok()

    @app.post("/api/backup")
    async def make_backup(request: Request) -> Response:
        data = await body(request)
        if not await asyncio.to_thread(state.vault.verify, str(data.get("password", ""))):
            raise VaultError("wrong password")
        content, name = await asyncio.to_thread(backup.create, state.data_dir, state.db)
        return Response(content, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/vault/password")
    async def vault_password(request: Request) -> dict:
        data = await body(request)
        await asyncio.to_thread(state.vault.change_password, str(data.get("old", "")),
                                str(data.get("new", "")))
        return ok()

    # ----- overview --------------------------------------------------------------------
    @app.get("/api/overview")
    async def overview() -> dict:
        db = state.db
        wallets = db.one("SELECT COUNT(*) AS n, SUM(has_key) AS k FROM wallets")
        totals = []
        for net in db.all("SELECT chain_id, name, native_symbol FROM networks WHERE enabled = 1 AND "
                          "(chain_id = 4663 OR chain_id IN (SELECT DISTINCT chain_id FROM balances)) "
                          "ORDER BY chain_id != 4663, name"):
            amount = sum(int(r["amount"]) for r in db.all(
                "SELECT amount FROM balances WHERE chain_id = ? AND asset = 'native'",
                (net["chain_id"],)))
            nfts = sum(int(r["amount"]) for r in db.all(
                "SELECT amount FROM balances WHERE chain_id = ? AND asset LIKE 'nft:%'",
                (net["chain_id"],)))
            totals.append({**net, "native": format_units(amount), "nfts": nfts})
        since = time.time() - 86_400
        counts = {r["status"]: r["n"] for r in db.all(
            "SELECT status, COUNT(*) AS n FROM history WHERE created_at >= ? GROUP BY status",
            (since,))}
        upcoming = []
        now = time.time()
        for c in collections.list_all(db):
            for s in c["stages"]:
                if s["state"] != "ended":
                    upcoming.append({"slug": c["slug"], "chain_name": c["chain_name"], **s})
        upcoming.sort(key=lambda s: s["starts_at"])
        eligible = db.one("SELECT COUNT(DISTINCT address) AS n FROM eligibility "
                          "WHERE status = 'ELIGIBLE'")["n"]
        return {
            "wallets": wallets["n"] or 0, "with_keys": wallets["k"] or 0, "totals": totals,
            "eligible_wallets": eligible, "last24h": counts, "upcoming": upcoming[:10],
            "active_jobs": [j for j in state.mint.list_jobs() if j["status"] == "running"],
            "active_tx_jobs": [j for j in state.tx.list_jobs() if j["status"] == "running"],
            "recent": db.all("SELECT * FROM history ORDER BY id DESC LIMIT 10"),
            "stopped": state.safety.stopped, "now": now,
        }

    # ----- wallets & groups ------------------------------------------------------------
    @app.get("/api/wallets")
    async def wallets_list(chain_id: int = 4663) -> dict:
        table = balances.balance_table(state.db, chain_id)
        tokens = state.db.all("SELECT * FROM tokens WHERE chain_id = ?", (chain_id,))
        rows = state.wallets.list()
        for w in rows:
            b = table.get(w["address"], {})
            w["native"] = format_units(b.get("native")) if "native" in b else None
            w["tokens"] = {t["symbol"]: format_units(b[t["address"]], t["decimals"])
                           for t in tokens if t["address"] in b}
            w["nfts"] = sum(int(v) for k, v in b.items() if k.startswith("nft:"))
        return {"wallets": rows, "groups": state.wallets.groups(), "tokens": tokens}

    @app.post("/api/wallets/import-keys")
    async def wallets_import(request: Request) -> dict:
        data = await body(request)
        return ok(**state.wallets.import_keys(str(data.get("text", "")), data.get("group") or None))

    @app.post("/api/wallets/generate")
    async def wallets_generate(request: Request) -> dict:
        data = await body(request)
        made = state.wallets.generate(int(data.get("count", 1)), str(data.get("prefix") or "gen"),
                                      data.get("group") or None)
        return ok(addresses=made)

    @app.post("/api/wallets/watch")
    async def wallets_watch(request: Request) -> dict:
        data = await body(request)
        return ok(address=state.wallets.add_watch_only(str(data.get("address", "")),
                                                       str(data.get("label", ""))))

    @app.post("/api/wallets/label")
    async def wallets_label(request: Request) -> dict:
        data = await body(request)
        state.wallets.set_label(str(data["address"]), str(data.get("label", "")))
        return ok()

    @app.post("/api/wallets/remove")
    async def wallets_remove(request: Request) -> dict:
        data = await body(request)
        if str(data.get("confirm", "")).strip().upper() != "REMOVE":
            raise ValueError("type REMOVE to confirm; removed keys are deleted from the vault")
        busy = [a for a in data.get("addresses", []) if checksum(a) in state.mint.active_wallets
                or checksum(a) in state.tx.busy_sources]
        if busy:
            raise ValueError(f"{busy[0]} is in a running operation")
        return ok(removed=state.wallets.remove(list(data.get("addresses", []))))

    @app.get("/api/wallets/export.csv")
    async def wallets_csv() -> PlainTextResponse:
        return _csv_response(state.wallets.export_csv(), "wallets.csv")

    @app.post("/api/wallets/csv/validate")
    async def wallets_csv_validate(request: Request) -> dict:
        return state.wallets.validate_csv(str((await body(request)).get("text", "")))

    @app.post("/api/wallets/csv/import")
    async def wallets_csv_import(request: Request) -> dict:
        return ok(**state.wallets.import_csv(str((await body(request)).get("text", ""))))

    @app.post("/api/groups")
    async def groups_action(request: Request) -> dict:
        data = await body(request)
        action = data.get("action")
        if action == "create":
            state.wallets.create_group(str(data["name"]))
        elif action == "rename":
            state.wallets.rename_group(str(data["name"]), str(data["new_name"]))
        elif action == "delete":
            state.wallets.delete_group(str(data["name"]))
        elif action == "add":
            state.wallets.add_to_group(list(data["addresses"]), str(data["name"]))
        elif action == "remove":
            state.wallets.remove_from_group(list(data["addresses"]), str(data["name"]))
        else:
            raise ValueError("unknown group action")
        return ok(groups=state.wallets.groups())

    def selection(data: dict) -> list[str]:
        return state.wallets.resolve_selection(data.get("addresses") or None,
                                               data.get("group") or None,
                                               data.get("exclude") or None)

    # ----- balances & tokens -----------------------------------------------------------
    @app.post("/api/balances/scan")
    async def balances_scan(request: Request) -> dict:
        data = await body(request)
        chain_id = int(data.get("chain_id", 4663))
        nfts = [c["address"] for c in state.db.all(
            "SELECT address FROM collections WHERE chain_id = ?", (chain_id,))]
        return ok(**await balances.scan(state.db, state.pool, chain_id, selection(data), nfts))

    @app.post("/api/tokens")
    async def tokens_add(request: Request) -> dict:
        data = await body(request)
        return ok(token=await balances.add_token(state.db, state.pool, int(data["chain_id"]),
                                                 checksum(str(data["address"]))))

    # ----- networks --------------------------------------------------------------------
    @app.get("/api/networks")
    async def networks_list() -> dict:
        nets = state.db.all("SELECT * FROM networks ORDER BY enabled DESC, chain_id = 4663 DESC, name")
        for n in nets:
            n["rpcs"] = state.db.all("SELECT * FROM rpc_endpoints WHERE chain_id = ? ORDER BY "
                                     "priority, id", (n["chain_id"],))
        return {"networks": nets}

    @app.post("/api/networks")
    async def networks_action(request: Request) -> dict:
        data = await body(request)
        action = data.get("action")
        chain_id = int(data["chain_id"])
        if action == "add":
            name = " ".join(str(data.get("name", "")).split())[:40]
            symbol = str(data.get("symbol", "ETH")).strip()[:8]
            if not name or not symbol:
                raise ValueError("name and native symbol are required")
            state.db.execute("INSERT INTO networks(chain_id, name, native_symbol, explorer_url) "
                             "VALUES(?, ?, ?, ?)", (chain_id, name, symbol,
                                                     str(data.get("explorer", ""))[:200]))
        elif action == "toggle":
            state.db.execute("UPDATE networks SET enabled = 1 - enabled WHERE chain_id = ?",
                             (chain_id,))
        elif action == "add_rpc":
            url = validate_rpc_url(str(data.get("url", "")))
            state.db.execute("INSERT OR IGNORE INTO rpc_endpoints(chain_id, url, priority) "
                             "VALUES(?, ?, ?)", (chain_id, url, int(data.get("priority", 50))))
        elif action == "remove_rpc":
            state.db.execute("DELETE FROM rpc_endpoints WHERE id = ? AND chain_id = ?",
                             (int(data["rpc_id"]), chain_id))
        elif action == "toggle_rpc":
            state.db.execute("UPDATE rpc_endpoints SET enabled = 1 - enabled WHERE id = ?",
                             (int(data["rpc_id"]),))
        elif action != "check":
            raise ValueError("unknown network action")
        return ok(rpcs=await state.pool.check(chain_id))

    # ----- collections & eligibility ---------------------------------------------------
    @app.get("/api/collections")
    async def collections_list() -> dict:
        return {"collections": collections.list_all(state.db)}

    @app.post("/api/collections")
    async def collections_add(request: Request) -> dict:
        data = await body(request)
        chain_id = data.get("chain_id")
        meta = await collections.load(state.db, state.opensea_factory, str(data["locator"]),
                                      int(chain_id) if chain_id else None)
        return ok(collection=collections.describe(meta))

    @app.post("/api/collections/delete")
    async def collections_delete(request: Request) -> dict:
        slug = str((await body(request))["slug"])
        state.db.execute("DELETE FROM collections WHERE slug = ?", (slug,))
        state.db.execute("DELETE FROM eligibility WHERE slug = ?", (slug,))
        return ok()

    @app.post("/api/eligibility/scan")
    async def eligibility_scan(request: Request) -> dict:
        data = await body(request)
        if state.scan_task and not state.scan_task.done():
            raise ValueError("an eligibility scan is already running")
        slug = str(data["slug"])
        meta = await collections.load(state.db, state.opensea_factory, slug, None)
        addresses = selection(data)
        settings = state.db.get_setting("eligibility", {}) or {}
        state.scan_progress = {"slug": slug, "total": len(addresses), "done": 0, "running": True,
                               "rate_limited": False}

        def on_result(address: str, results: list) -> None:
            state.scan_progress["done"] += 1
            state.bus.publish("eligibility", slug=slug, address=address,
                              done=state.scan_progress["done"], total=len(addresses),
                              statuses=[r.status for r in results])

        async def run() -> None:
            try:
                counts = await eligibility.scan(
                    state.db, state.opensea_factory, state.vault, meta, addresses,
                    concurrency=int(settings.get("concurrency", 2)),
                    spacing_seconds=float(settings.get("spacing_seconds", 1.0)),
                    on_result=on_result, should_stop=lambda: state.safety.stopped)
                state.scan_progress["rate_limited"] = bool(counts.pop("rate_limited", False))
                state.bus.publish("eligibility_done", slug=slug, counts=counts,
                                  rate_limited=state.scan_progress["rate_limited"])
            finally:
                state.scan_progress["running"] = False
        state.scan_task = asyncio.create_task(run())
        return ok(total=len(addresses))

    @app.get("/api/eligibility")
    async def eligibility_list(slug: str) -> dict:
        meta = collections.get(state.db, slug)
        return {"rows": eligibility.table(state.db, slug, meta),
                "collection": collections.describe(meta), "progress": state.scan_progress,
                "signin": SHARED_LIMITER.status()}

    @app.get("/api/eligibility.csv")
    async def eligibility_csv(slug: str) -> PlainTextResponse:
        meta = collections.get(state.db, slug)
        rows = eligibility.table(state.db, slug, meta)
        cols = ["address", "label", "stage_name", "stage_type", "status", "detail", "max_quantity",
                "minted", "price_wei", "source", "checked_at"]
        return _csv_response(_to_csv(rows, cols), f"eligibility-{slug}.csv")

    # ----- mint ------------------------------------------------------------------------
    @app.post("/api/mint/preview")
    async def mint_preview(request: Request) -> dict:
        data = await body(request)
        defaults = {**DEFAULT_MINT_SETTINGS, **(state.db.get_setting("mint", {}) or {})}
        params = MintParams(
            slug=str(data["slug"]), stage_index=int(data["stage_index"]),
            addresses=selection(data), quantity=int(data.get("quantity", 1)),
            gas_limit=int(data.get("gas_limit") or defaults["gas_limit"]),
            fee_mode=str(data.get("fee_mode") or defaults["fee_mode"]),
            max_fee_gwei=str(data.get("max_fee_gwei") or defaults["max_fee_gwei"] or ""),
            priority_fee_gwei=str(data.get("priority_fee_gwei") or defaults["priority_fee_gwei"] or ""),
            public_offset_ms=int(data.get("public_offset_ms") or defaults["public_offset_ms"]),
            operation_cap=str(data.get("operation_cap") or ""),
            pending_timeout_seconds=int(defaults["pending_timeout_seconds"]),
        )
        return await state.mint.preview(params)

    @app.post("/api/mint/start")
    async def mint_start(request: Request) -> dict:
        data = await body(request)
        return ok(job_id=await state.mint.start(str(data.get("token", "")),
                                                str(data.get("confirm", ""))))

    @app.get("/api/mint/jobs")
    async def mint_jobs() -> dict:
        return {"jobs": state.mint.list_jobs()}

    # ----- safety & settings -----------------------------------------------------------
    @app.post("/api/stop")
    async def stop() -> dict:
        state.safety.stop("STOP pressed in dashboard")
        return ok(stopped=True)

    @app.post("/api/resume")
    async def resume(request: Request) -> dict:
        state.safety.resume(str((await body(request)).get("confirm", "")))
        return ok(stopped=False)

    @app.get("/api/settings")
    async def settings_get() -> dict:
        return {"limits": state.safety.limits_text(), "limit_labels": LIMIT_LABELS,
                "monitor": state.monitor.settings(),
                "security": {"auto_lock_minutes": state.auto_lock_minutes()},
                "offers": state.offers.settings(), "opensea_key": state.opensea.configured,
                "network": {"broadcast_all": bool((state.db.get_setting("network", {}) or {})
                                                  .get("broadcast_all", True))},
                "mint": {**DEFAULT_MINT_SETTINGS, **(state.db.get_setting("mint", {}) or {})},
                "eligibility": {"concurrency": 2, "spacing_seconds": 1.0,
                                **(state.db.get_setting("eligibility", {}) or {})}}

    @app.post("/api/settings")
    async def settings_set(request: Request) -> dict:
        data = await body(request)
        if "limits" in data:
            state.safety.set_limits(dict(data["limits"]))
        if "mint" in data:
            mint = {**DEFAULT_MINT_SETTINGS, **(state.db.get_setting("mint", {}) or {})}
            for key in DEFAULT_MINT_SETTINGS:
                if key in data["mint"]:
                    mint[key] = data["mint"][key]
            MintParams("x", 0, ["x"], 1, int(mint["gas_limit"]), str(mint["fee_mode"]),
                       str(mint["max_fee_gwei"] or ""), str(mint["priority_fee_gwei"] or ""),
                       int(mint["public_offset_ms"]),
                       pending_timeout_seconds=int(mint["pending_timeout_seconds"])).validate()
            if not 1 <= int(mint["setup_concurrency"]) <= 50:
                raise ValueError("setup concurrency must be 1-50")
            state.db.set_setting("mint", mint)
        if "offers" in data:
            o = data["offers"]
            scan, hold = float(o.get("scan_seconds", 60)), float(o.get("holdings_minutes", 10))
            if not 15 <= scan <= 3600 or not 1 <= hold <= 1440:
                raise ValueError("offer scan every 15-3600 s, wallet NFT refresh every 1-1440 min")
            state.db.set_setting("offers", {"enabled": bool(o.get("enabled", True)), "scan_seconds": scan,
                                            "holdings_minutes": hold, "floor_seconds": 120})
        if "security" in data:
            minutes = int(data["security"].get("auto_lock_minutes", 30))
            if not 0 <= minutes <= 1440:
                raise ValueError("auto-lock must be 0 (off) to 1440 minutes")
            state.db.set_setting("security", {"auto_lock_minutes": minutes})
        if "network" in data:
            state.db.set_setting("network", {"broadcast_all": bool(data["network"].get("broadcast_all", True))})
        if "eligibility" in data:
            e = data["eligibility"]
            concurrency, spacing = int(e.get("concurrency", 2)), float(e.get("spacing_seconds", 1.0))
            if not 1 <= concurrency <= 10 or not 0 <= spacing <= 10:
                raise ValueError("eligibility concurrency 1-10, spacing 0-10 seconds")
            state.db.set_setting("eligibility", {"concurrency": concurrency,
                                                 "spacing_seconds": spacing})
        return ok()

    # ----- contracts (analyser + custom mints) -----------------------------------------
    @app.get("/api/contracts")
    async def contracts_list() -> dict:
        return {"reports": contracts.reports(state.db)}

    @app.post("/api/contracts/analyze")
    async def contracts_analyze(request: Request) -> dict:
        data = await body(request)
        report = await contracts.analyze(state.db, state.pool, int(data["chain_id"]),
                                         checksum(str(data["address"])), str(data.get("abi") or ""))
        report.pop("abi", None)
        return ok(report=report)

    @app.post("/api/contracts/mint/preview")
    async def contracts_mint_preview(request: Request) -> dict:
        data = await body(request)
        chain_id = int(data["chain_id"])
        start_at = float(data["start_at"]) if data.get("start_at") else None
        if start_at and start_at < time.time() - 5:
            raise ValueError("start time is in the past")
        wallets = [a for a in selection(data) if state.vault.has(a)]
        if not wallets:
            raise ValueError("no selected wallet has a key")
        items, notes = custommint.plan(
            state.db, chain_id, str(data["address"]), contracts.canonical(str(data["signature"])),
            [str(a) for a in data.get("args", [])], int(data.get("quantity", 1)),
            str(data.get("price", "0")), wallets, str(data.get("proofs", "")), start_at,
            int(data.get("gas_limit") or 0), bool(data.get("resimulate", True)),
            str(data.get("label", ""))[:30], float(data.get("wait_for_open_minutes") or 0))
        return await state.tx.preview("custom_mint", chain_id, items, "Custom mint",
                                      str(data.get("cap") or ""), notes)

    # ----- OpenSea key, offers, portfolio --------------------------------------------------
    @app.post("/api/opensea/key")
    async def opensea_key(request: Request) -> dict:
        key = str((await body(request)).get("key", "")).strip()
        if key and not (8 <= len(key) <= 200) or any(c.isspace() for c in key):
            raise ValueError("that does not look like an OpenSea API key")
        state.vault.set_secret("opensea_api_key", key)
        return ok(configured=bool(key))

    @app.get("/api/offers")
    async def offers_dashboard() -> dict:
        return state.offers.dashboard()

    @app.post("/api/offers/scan")
    async def offers_scan(request: Request) -> dict:
        data = await body(request)
        return ok(**await state.offers.scan(sync_holdings=bool(data.get("sync", True))))

    @app.post("/api/offers/rules")
    async def offers_rules(request: Request) -> dict:
        data = await body(request)
        return ok(rules=state.offers.save_rules(str(data.get("slug") or "*"), dict(data.get("rules") or {})))

    @app.post("/api/offers/control")
    async def offers_control(request: Request) -> dict:
        data = await body(request)
        slug, action = str(data["slug"]), str(data.get("action"))
        if action == "arm":
            state.offers.arm(slug, str(data.get("confirm", "")))
        elif action == "disarm":
            state.offers.disarm(slug)
        elif action == "resume":
            state.offers.resume(slug)
        elif action == "pause":
            state.offers.pause(slug, "paused by you")
        else:
            raise ValueError("unknown action")
        return ok()

    @app.post("/api/offers/accept")
    async def offers_accept(request: Request) -> dict:
        data = await body(request)
        items = data.get("items") or [data]
        if str(data.get("confirm", "")).strip().upper() != "ACCEPT":
            raise OfferError("type ACCEPT to accept")
        if len(items) > 50:
            raise OfferError("accept at most 50 offers at once")
        results = await asyncio.gather(*(state.offers.accept(
            str(i["order_hash"]), str(i["contract"]), str(i["token_id"]), str(i["wallet"]), "manual", "ACCEPT")
            for i in items), return_exceptions=True)
        return ok(results=[r if isinstance(r, dict) else {"status": "skipped", "reason": str(r)} for r in results])

    @app.post("/api/offers/approval")
    async def offers_approval(request: Request) -> dict:
        data = await body(request)
        chain_id = int(data["chain_id"])
        items = state.offers.approval_items(chain_id, checksum(str(data["wallet"])), checksum(str(data["contract"])),
                                            checksum(str(data["operator"])), bool(data.get("approve", True)))
        note = ("This lets OpenSea's Seaport transfer contract move NFTs of this one collection from this one "
                "wallet when you accept an offer. It does not touch other collections or your coins. You can "
                "revoke it at any time.")
        return await state.tx.preview("approval", chain_id, items, "Approval", "", [note])

    @app.get("/api/offers/log.csv")
    async def offers_log_csv() -> PlainTextResponse:
        rows = state.db.all("SELECT * FROM offer_log ORDER BY id")
        for r in rows:
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])) + " UTC"
        cols = ["ts", "slug", "nft_name", "token_id", "contract", "wallet", "offerer", "offer_type", "currency",
                "gross_native", "market_fee", "creator_fee", "gas_native", "net_native", "floor_native",
                "tx_hash", "status", "reason", "mode", "order_hash"]
        return _csv_response(_to_csv(rows, cols), "offers-report.csv")

    @app.get("/api/portfolio")
    async def portfolio() -> dict:
        rows = market.portfolio(state.db)
        for r in rows:
            r["history"] = market.history(state.db, r["slug"], 7)
        return {"collections": rows, "api_key": state.opensea.configured}

    @app.post("/api/portfolio/refresh")
    async def portfolio_refresh() -> dict:
        return ok(**await state.offers.scan(sync_holdings=True))

    # ----- monitor & alerts ------------------------------------------------------------
    @app.get("/api/alerts")
    async def alerts_list() -> dict:
        return state.monitor.alerts()

    @app.post("/api/alerts/read")
    async def alerts_read() -> dict:
        state.monitor.mark_read()
        return ok()

    @app.post("/api/collections/watch")
    async def collections_watch(request: Request) -> dict:
        data = await body(request)
        state.monitor.set_watch(str(data["slug"]), bool(data.get("watch", True)))
        return ok()

    @app.post("/api/collections/check")
    async def collections_check(request: Request) -> dict:
        data = await body(request)
        return ok(snapshot=await state.monitor.check(str(data["slug"])))

    @app.post("/api/monitor/settings")
    async def monitor_settings(request: Request) -> dict:
        return ok(settings=state.monitor.set_settings(await body(request)))

    # ----- address book & recipient lists ----------------------------------------------
    @app.get("/api/addressbook")
    async def addressbook_list() -> dict:
        return {"entries": addressbook.entries(state.db), "lists": addressbook.lists(state.db)}

    @app.post("/api/addressbook")
    async def addressbook_action(request: Request) -> dict:
        data = await body(request)
        action = data.get("action")
        if action == "add":
            addressbook.add(state.db, str(data["address"]), str(data.get("label", "")))
        elif action == "remove":
            addressbook.remove(state.db, str(data["address"]))
        elif action == "parse_list":
            return addressbook.parse_recipients(str(data.get("text", "")))
        elif action == "save_list":
            parsed = addressbook.parse_recipients(str(data.get("text", "")))
            if not parsed["ok"]:
                raise ValueError("; ".join(parsed["problems"][:5]) or "list is empty")
            addressbook.save_list(state.db, str(data.get("name", "")), parsed["rows"])
        elif action == "delete_list":
            addressbook.delete_list(state.db, str(data["name"]))
        else:
            raise ValueError("unknown address book action")
        return ok(entries=addressbook.entries(state.db), lists=addressbook.lists(state.db))

    # ----- transfers: sweep, distribute, NFTs --------------------------------------------
    def recipients_from(data: dict) -> list[dict]:
        if data.get("list"):
            match = [x for x in addressbook.lists(state.db) if x["name"] == data["list"]]
            if not match:
                raise ValueError("saved list not found")
            return match[0]["rows"]
        parsed = addressbook.parse_recipients(str(data.get("text", "")))
        if not parsed["ok"]:
            raise ValueError("; ".join(parsed["problems"][:5]) or "add recipients")
        return parsed["rows"]

    @app.post("/api/sweep/preview")
    async def sweep_preview(request: Request) -> dict:
        data = await body(request)
        chain_id = int(data["chain_id"])
        items, notes = await transfers.plan_sweep(
            state.db, state.pool, chain_id, selection(data), str(data["destination"]),
            str(data.get("asset") or "native"), str(data.get("reserve") or "0"),
            str(data.get("min_balance") or "0"))
        return await state.tx.preview("sweep", chain_id, items, "Sweep", str(data.get("cap") or ""),
                                      notes)

    @app.post("/api/distribute/preview")
    async def distribute_preview(request: Request) -> dict:
        data = await body(request)
        chain_id = int(data["chain_id"])
        mode = str(data.get("mode", "equal_each"))
        items, notes = await transfers.plan_distribute(
            state.db, state.pool, chain_id, mode, str(data.get("asset") or "native"),
            recipients_from(data), data.get("source") or None, str(data.get("total") or ""),
            str(data.get("each") or ""))
        return await state.tx.preview("distribute", chain_id, items, "Distribution",
                                      str(data.get("cap") or ""), notes)

    @app.get("/api/nfts")
    async def nfts_list(chain_id: int, contract: str) -> dict:
        contract = checksum(contract)
        standard = await nfts.detect_standard(state.pool, chain_id, contract)
        return {"contract": contract, "standard": standard,
                "holdings": nfts.holdings(state.db, chain_id, contract)}

    @app.post("/api/nfts/discover")
    async def nfts_discover(request: Request) -> dict:
        data = await body(request)
        chain_id, contract = int(data["chain_id"]), checksum(str(data["contract"]))
        standard = await nfts.detect_standard(state.pool, chain_id, contract)
        lookback = int(data.get("lookback_blocks") or 0)
        if not 0 <= lookback <= 50_000_000:
            raise ValueError("look-back must be 0-50,000,000 blocks")
        return ok(**await nfts.discover(state.db, state.pool, chain_id, contract, standard,
                                        selection(data), lookback))

    @app.post("/api/nfts/manual")
    async def nfts_manual(request: Request) -> dict:
        data = await body(request)
        chain_id, contract = int(data["chain_id"]), checksum(str(data["contract"]))
        standard = await nfts.detect_standard(state.pool, chain_id, contract)
        ids = [int(x) for x in str(data.get("ids", "")).replace(",", " ").split() if x.strip()]
        if not ids or len(ids) > 1000:
            raise ValueError("enter 1-1000 token IDs")
        mine = {r["address"].lower() for r in state.db.all("SELECT address FROM wallets")}
        return ok(**await nfts.add_manual(state.db, state.pool, chain_id, contract, standard,
                                          ids, mine))

    @app.post("/api/nfts/preview")
    async def nfts_preview(request: Request) -> dict:
        data = await body(request)
        chain_id, contract = int(data["chain_id"]), checksum(str(data["contract"]))
        standard = await nfts.detect_standard(state.pool, chain_id, contract)
        mode = str(data.get("mode", "consolidate"))
        recipients = []
        if mode == "distribute":
            recipients = [r["address"] for r in recipients_from(data)]
        items, notes = await transfers.plan_nft(
            state.db, state.pool, chain_id, contract, standard, list(data.get("selection", [])),
            mode, str(data.get("destination") or ""), recipients)
        return await state.tx.preview("nft_transfer", chain_id, items, "NFT transfer", "", notes)

    @app.post("/api/tx/start")
    async def tx_start(request: Request) -> dict:
        data = await body(request)
        return ok(job_id=await state.tx.start(str(data.get("token", "")),
                                              str(data.get("confirm", ""))))

    @app.post("/api/tx/replace/preview")
    async def replace_preview(request: Request) -> dict:
        data = await body(request)
        return await state.replacer.preview(int(data["history_id"]), str(data.get("mode", "speedup")))

    @app.post("/api/tx/replace/send")
    async def replace_send(request: Request) -> dict:
        data = await body(request)
        return ok(**await state.replacer.execute(str(data.get("token", "")), str(data.get("confirm", ""))))

    @app.get("/api/tx/jobs")
    async def tx_jobs() -> dict:
        return {"jobs": state.tx.list_jobs()}

    # ----- history ---------------------------------------------------------------------
    @app.get("/api/history")
    async def history(limit: int = 200) -> dict:
        rows = state.db.all("SELECT * FROM history ORDER BY id DESC LIMIT ?", (min(limit, 2000),))
        for r in rows:
            for key in ("value_wei", "max_cost_wei", "gas_cost_wei"):
                r[key.replace("_wei", "")] = from_wei(int(r[key])) if r[key] else None
        return {"rows": rows}

    @app.get("/api/history.csv")
    async def history_csv() -> PlainTextResponse:
        rows = state.db.all("SELECT * FROM history ORDER BY id")
        cols = ["created_at", "updated_at", "job_id", "operation", "chain_id", "address", "slug",
                "stage_name", "quantity", "asset", "token_id", "amount", "tx_hash", "nonce",
                "value_wei", "spend_wei", "max_cost_wei", "gas_used", "gas_cost_wei", "status",
                "error", "recipient"]
        for r in rows:
            for key in ("created_at", "updated_at"):
                r[key] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r[key])) + " UTC"
        return _csv_response(_to_csv(rows, cols), "history.csv")

    # ----- live events -----------------------------------------------------------------
    @app.get("/api/events")
    async def events() -> StreamingResponse:
        queue = state.bus.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                state.bus.unsubscribe(queue)
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _to_csv(rows: list[dict], cols: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(cols)
    for r in rows:
        writer.writerow([safe_cell(r.get(c)) for c in cols])
    return out.getvalue()


def _csv_response(text: str, name: str) -> PlainTextResponse:
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
