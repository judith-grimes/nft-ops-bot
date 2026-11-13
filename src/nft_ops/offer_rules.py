"""Offer rules and evaluation: is this offer good enough to accept, and why (not)?

Money is always judged on NET proceeds: gross offer - OpenSea fee - creator earnings - gas,
with fees taken from the offer's own Seaport order (what will really be paid), never assumed.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from .db import Database
from .market import to_native
from .seaport import _norm

OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1f0df003000390027140000faa719"
DEFAULT_RULES: dict[str, Any] = {
    "mode": "floor_pct",          # fixed | floor_pct | floor_premium (user choice: floor % by default)
    "fixed_min": "",              # native coin; used when mode is fixed
    "floor_pct": 95,              # mode floor_pct: accept at or above this % of floor
    "premium_pct": 5,             # mode floor_premium: accept at or above floor + this %
    "net_pct_of_min": 100,        # what you receive must be >= this % of the minimum price
    "min_net": "",                # optional absolute minimum net (native)
    "min_profit_pct": "",         # optional: net vs what this NFT cost to mint
    "max_fee_pct": 15,            # fees (marketplace + creator) above this % of gross: skip
    "pause_drop_pct": 10,         # floor drops more than this within the window: pause
    "drop_window_minutes": 60,
    "max_floor_vs_sales_pct": 50, # floor this far from recent sales median: suspicious, pause
    "max_per_day": 10,            # accepted offers per collection per 24 h
    "max_value_per_day": "5",     # native coin per collection per 24 h
    "max_concurrent": 3,          # simultaneous executions per collection
    "max_gas": "0.01",            # native coin per acceptance
    "allow_stablecoins": True,    # user choice: stablecoin offers allowed, converted
    "stable_haircut_pct": 1,      # value stablecoin offers this % lower, as a safety margin
    "execution_mode": "full",     # safe | semi | full (full only acts once armed per collection)
    "optional": [],               # checks allowed to fail (only from OPTIONAL_ALLOWED)
    "overrides": [],              # custom rules: [{"match": {...}, "set": {...}}]
}
# Protections that can never be made optional (asset safety).
MANDATORY = {"active", "not_expired", "owned", "currency", "min_price", "net_min", "gas",
             "floor_stable", "approved", "not_self"}
OPTIONAL_ALLOWED = {"fee_pct", "min_net_abs", "profit", "floor_sane"}
MIN_SECONDS_LEFT = 60


def rules_for(db: Database, slug: str) -> dict:
    base = dict(DEFAULT_RULES)
    row = db.one("SELECT rules FROM offer_rules WHERE slug = '*'")
    if row:
        base.update(json.loads(row["rules"]))
    row = db.one("SELECT rules FROM offer_rules WHERE slug = ?", (slug,))
    if row and slug != "*":
        base.update(json.loads(row["rules"]))
    return base


def validate_rules(values: dict) -> dict:
    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key not in DEFAULT_RULES:
            raise ValueError(f"unknown rule {key}")
        default = DEFAULT_RULES[key]
        if key == "mode" and value not in ("fixed", "floor_pct", "floor_premium"):
            raise ValueError("mode must be fixed, floor_pct or floor_premium")
        if key == "execution_mode" and value not in ("safe", "semi", "full"):
            raise ValueError("execution mode must be safe, semi or full")
        if key == "optional":
            bad = set(value) - OPTIONAL_ALLOWED
            if bad:
                raise ValueError(f"these protections can never be optional: {', '.join(sorted(bad))}")
        if key == "overrides":
            if not isinstance(value, list):
                raise ValueError("overrides must be a list")
            for o in value:
                if not isinstance(o, dict) or not isinstance(o.get("match"), dict) or not isinstance(o.get("set"), dict):
                    raise ValueError("each override needs a 'match' and a 'set'")
                validate_rules({k: v for k, v in o["set"].items()})
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            value = float(value)
            if value < 0:
                raise ValueError(f"{key} cannot be negative")
        elif isinstance(default, str) and key not in ("mode", "execution_mode"):
            value = str(value).strip()
            if value:
                Decimal(value)  # must be a number
        clean[key] = value
    if clean.get("floor_pct", 95) > 1000 or clean.get("net_pct_of_min", 100) > 1000:
        raise ValueError("percentages look wrong")
    return clean


def apply_overrides(rules: dict, ctx: dict) -> tuple[dict, list[str]]:
    """Mode D: custom rules by token, trait, wallet, offer type or currency."""
    applied = []
    out = dict(rules)
    for o in rules.get("overrides") or []:
        m = o["match"]
        ok = all([
            not m.get("token_id") or str(m["token_id"]) == str(ctx["token_id"]),
            not m.get("wallet") or m["wallet"].lower() == ctx["wallet"].lower(),
            not m.get("offer_type") or m["offer_type"] == ctx["offer_type"],
            not m.get("currency") or m["currency"].upper() == ctx["currency"].upper(),
            not m.get("trait") or m["trait"] in ctx.get("traits", []),
        ])
        if ok:
            out.update(o["set"])
            applied.append(o.get("name") or json.dumps(m))
    return out, applied


# ----- offer parsing ------------------------------------------------------------------
def parse_offer(offer: dict) -> dict:
    p = offer["protocol_data"]["parameters"]
    price = offer.get("price_per_item") or offer["price"]
    offer_items = p["offer"]
    currency_token = offer_items[0]["token"].lower() if offer_items else ""
    total_offer = sum(_norm(i["startAmount"]) for i in offer_items if i["token"].lower() == currency_token)
    nft_items = [c for c in p["consideration"] if _norm(c["itemType"]) in (2, 3, 4, 5)]
    nft = nft_items[0] if nft_items else None
    units = sum(_norm(c["startAmount"]) for c in nft_items) or 1
    criteria = offer.get("criteria") or {}
    if criteria.get("traits") or criteria.get("numeric_traits"):
        kind = "trait"
    elif nft and _norm(nft["itemType"]) in (2, 3):
        kind = "item"
    else:
        kind = "collection"
    fees = {"market": 0, "creator": 0}
    for c in p["consideration"]:
        if c["token"].lower() == currency_token and c["recipient"].lower() != p["offerer"].lower():
            key = "market" if c["recipient"].lower() == OPENSEA_FEE_RECIPIENT else "creator"
            fees[key] += _norm(c["startAmount"])
    per_item = int(price["value"])
    ratio = Decimal(per_item) / Decimal(total_offer) if total_offer else Decimal(0)
    return {
        "order_hash": offer.get("order_hash"), "chain": offer.get("chain"),
        "protocol_address": offer.get("protocol_address"), "status": offer.get("status"),
        "offerer": p["offerer"], "type": kind, "currency": price["currency"],
        "decimals": int(price["decimals"]), "currency_token": currency_token,
        "gross": per_item, "units": units, "remaining": int(offer.get("remaining_quantity") or 0),
        "market_fee": int(fees["market"] * ratio), "creator_fee": int(fees["creator"] * ratio),
        "start": _norm(p["startTime"]), "end": _norm(p["endTime"]),
        "contract": (nft or {}).get("token", "").lower(),
        "token_id": str(_norm(nft["identifierOrCriteria"])) if nft and kind == "item" else None,
        "traits": [f"{t.get('type')}:{t.get('value')}" for t in (criteria.get("traits") or [])],
        "conduit_key": p.get("conduitKey"),
    }


def offer_applies(parsed: dict, holding: dict) -> bool:
    if parsed["contract"] and parsed["contract"] != holding["contract"].lower():
        return False
    if parsed["type"] == "item":
        return parsed["token_id"] == str(holding["token_id"])
    if parsed["type"] == "trait":
        # Only when the offer's traits are readable AND this NFT's traits are known and match.
        traits = holding.get("traits_list")
        return bool(parsed["traits"]) and bool(traits) and all(t in traits for t in parsed["traits"])
    return True


# ----- evaluation ---------------------------------------------------------------------
def evaluate(db: Database, rules: dict, parsed: dict, holding: dict, market: dict | None,
             gas_native: float, own_wallets: set[str], cost_basis: float | None = None,
             approved: bool | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    chain_id = holding["chain_id"]
    ctx = {"token_id": holding["token_id"], "wallet": holding["address"], "offer_type": parsed["type"],
           "currency": parsed["currency"], "traits": holding.get("traits_list") or []}
    rules, applied = apply_overrides(rules, ctx)
    info = market.get("info") if market else None
    gross_native, how = to_native(db, chain_id, parsed["currency"], parsed["gross"], parsed["decimals"], info)
    is_stable = how.startswith("OpenSea rate")
    if gross_native is not None and is_stable:
        gross_native *= 1 - float(rules["stable_haircut_pct"]) / 100
    scale = (gross_native / parsed["gross"]) if gross_native and parsed["gross"] else 0
    market_fee = parsed["market_fee"] * scale
    creator_fee = parsed["creator_fee"] * scale
    net = None if gross_native is None else gross_native - market_fee - creator_fee - gas_native
    floor = market.get("floor") if market else None
    mode = rules["mode"]
    if mode == "fixed" and str(rules["fixed_min"]).strip():
        min_price, basis = float(rules["fixed_min"]), "fixed minimum"
    elif mode == "fixed":
        min_price, basis = None, "fixed mode but no fixed minimum is set"
    elif floor:
        pct = float(rules["floor_pct"]) if mode == "floor_pct" else 100 + float(rules["premium_pct"])
        min_price, basis = floor * pct / 100, f"{pct:g}% of floor {floor:.6g}"
    else:
        min_price, basis = None, "no floor price available"

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "optional": name in set(rules.get("optional") or []) & OPTIONAL_ALLOWED})

    check("active", parsed["status"] == "ACTIVE" and parsed["remaining"] > 0 and parsed["start"] <= now,
          f"status {parsed['status']}, {parsed['remaining']} left")
    check("not_expired", parsed["end"] - now > MIN_SECONDS_LEFT,
          f"expires in {int((parsed['end'] - now) / 60)} min" if parsed["end"] > now else "expired")
    check("owned", bool(holding.get("owned", True)), "held by this wallet" if holding.get("owned", True)
          else "no longer held by this wallet")
    check("not_self", parsed["offerer"].lower() not in own_wallets, "offer is from another person"
          if parsed["offerer"].lower() not in own_wallets else "offer is from one of your own wallets")
    currency_ok = gross_native is not None and (not is_stable or rules["allow_stablecoins"])
    check("currency", currency_ok, how if currency_ok else
          (f"{parsed['currency']} offers are not allowed" if gross_native is not None else how))
    check("min_price", min_price is not None and gross_native is not None and gross_native >= min_price,
          f"offer {gross_native:.6g} vs minimum {min_price:.6g} ({basis})" if min_price and gross_native
          else basis)
    need_net = None if min_price is None else min_price * float(rules["net_pct_of_min"]) / 100
    check("net_min", need_net is not None and net is not None and net >= need_net,
          f"you receive {net:.6g} vs required {need_net:.6g}" if need_net is not None and net is not None
          else "cannot compute net proceeds")
    fee_pct = ((market_fee + creator_fee) / gross_native * 100) if gross_native else 0
    check("fee_pct", fee_pct <= float(rules["max_fee_pct"]), f"fees {fee_pct:.2f}% of the offer")
    if str(rules["min_net"]).strip():
        check("min_net_abs", net is not None and net >= float(rules["min_net"]),
              f"net {net or 0:.6g} vs at least {rules['min_net']}")
    check("gas", gas_native <= float(rules["max_gas"]) and (net is None or net > 0),
          f"gas about {gas_native:.6g}")
    drop, sane = (market or {}).get("change"), (market or {}).get("vs_sales")
    floor_based = mode != "fixed"
    stable = drop is None or drop > -float(rules["pause_drop_pct"])
    check("floor_stable", stable or not floor_based,
          f"floor changed {drop:.1f}% in {rules['drop_window_minutes']:g} min" if drop is not None
          else "no recent floor history (treated as stable)")
    check("floor_sane", sane is None or abs(sane) <= float(rules["max_floor_vs_sales_pct"]),
          f"floor is {sane:+.1f}% vs recent sales" if sane is not None else "no recent sales to compare")
    if str(rules["min_profit_pct"]).strip():
        ok = cost_basis is not None and net is not None and net >= cost_basis * (1 + float(rules["min_profit_pct"]) / 100)
        check("profit", ok, f"net {net or 0:.6g} vs cost {cost_basis:.6g}" if cost_basis is not None
              else "mint cost unknown")
    if approved is not None:
        check("approved", approved, "OpenSea may transfer this NFT" if approved
              else "needs a one-time approval for this collection (Offers page → Approve)")
    failed = [c for c in checks if not c["ok"] and not c["optional"]]
    return {
        "order_hash": parsed["order_hash"], "type": parsed["type"], "offerer": parsed["offerer"],
        "currency": parsed["currency"], "gross": str(parsed["gross"]), "decimals": parsed["decimals"],
        "gross_native": gross_native, "market_fee": market_fee, "creator_fee": creator_fee,
        "gas_native": gas_native, "net": net, "floor": floor, "min_price": min_price,
        "expires": parsed["end"], "checks": checks, "qualifies": not failed,
        "reason": "; ".join(c["detail"] for c in failed) or "meets every rule",
        "overrides": applied, "rules_mode": mode,
    }
