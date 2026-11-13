"""Notification center: one place for everything worth telling the user.

Stored in the existing ``alerts`` table (so older alerts from the collection monitor show up
too), with a category, a severity level, a short title, the message and an optional link to
the page that lets the user act on it. Nothing is ever dropped: failures are always stored,
whatever the desktop-notification preferences say.
"""

from __future__ import annotations

import time

from .db import Database
from .events import EventBus

CATEGORIES = {
    "mint": "Mints",
    "schedule": "Scheduled mints",
    "readiness": "Wallet readiness",
    "eligibility": "Eligibility",
    "funds": "Balances & gas",
    "offers": "Offers",
    "system": "RPC & system",
    "security": "Security",
    "collection": "Collections",
}
LEVELS = ("info", "good", "warn", "bad")


class Notifier:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db, self.bus = db, bus

    def send(self, category: str, level: str, title: str, message: str, *,
             link: str | None = None, slug: str | None = None, kind: str | None = None) -> int:
        category = category if category in CATEGORIES else "system"
        level = level if level in LEVELS else "info"
        cur = self.db.execute(
            "INSERT INTO alerts(created_at, slug, kind, level, message, read, category, title, link) "
            "VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (time.time(), slug, kind or category, level, message, category, title, link))
        nid = int(cur.lastrowid)
        self.bus.publish("alert", id=nid, slug=slug, alert_kind=kind or category, level=level,
                         message=f"{title}: {message}" if title else message, category=category,
                         link=link)
        return nid

    def list(self, category: str | None = None, unread_only: bool = False, limit: int = 200) -> dict:
        where, args = [], []
        if category:
            where.append("COALESCE(category, 'collection') = ?")
            args.append(category)
        if unread_only:
            where.append("read = 0")
        sql = "SELECT * FROM alerts" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.db.all(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        counts = {r["c"] or "collection": r["n"] for r in self.db.all(
            "SELECT category AS c, COUNT(*) AS n FROM alerts WHERE read = 0 GROUP BY category")}
        unread = self.db.one("SELECT COUNT(*) AS n FROM alerts WHERE read = 0")["n"]
        return {"notifications": rows, "unread": unread, "unread_by_category": counts,
                "categories": CATEGORIES}

    def mark_read(self, ids: list[int] | None = None) -> None:
        if ids:
            marks = ",".join("?" * len(ids))
            self.db.execute(f"UPDATE alerts SET read = 1 WHERE id IN ({marks})", tuple(ids))
        else:
            self.db.execute("UPDATE alerts SET read = 1 WHERE read = 0")
