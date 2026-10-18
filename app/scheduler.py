from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import validate_schedule
from .db import Database, now


def schedule_window(current: datetime, at: str, timezone: str) -> tuple[datetime, datetime]:
    """One run per local calendar day, including DST changes and a missed startup run."""
    validate_schedule(at, timezone)
    zone = ZoneInfo(timezone)
    local = current.astimezone(zone)
    hour, minute = map(int, at.split(":"))
    today = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    # Roundtrip normalizes a nonexistent spring-forward time to the next valid local time.
    today = today.astimezone(UTC).astimezone(zone)
    if current.astimezone(UTC) >= today.astimezone(UTC):
        due = today
        next_day = local.date() + timedelta(days=1)
        next_run = datetime.combine(next_day, datetime.min.time(), zone).replace(
            hour=hour, minute=minute
        )
    else:
        previous = local.date() - timedelta(days=1)
        due = datetime.combine(previous, datetime.min.time(), zone).replace(
            hour=hour, minute=minute
        )
        next_run = today
    return due.astimezone(UTC), next_run.astimezone(UTC)


def enqueue_checks(db: Database) -> list[int]:
    return [
        db.enqueue("check", {"url": c["url"]}, f"check:{c['id']}")
        for c in db.rows("SELECT * FROM channels WHERE subscribed=1 AND initialized=1")
    ]


def tick(db: Database, timezone: str, current: datetime | None = None) -> None:
    current = current or datetime.now(UTC)
    schedule = db.setting("schedule", {"time": "04:00", "timezone": timezone})
    due, _ = schedule_window(current, schedule["time"], schedule["timezone"])
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM settings WHERE key='last_scheduled'").fetchone()
        if row and datetime.fromisoformat(json.loads(row["value"])) >= due:
            return
        stamp = now()
        for channel in conn.execute("SELECT * FROM channels WHERE subscribed=1 AND initialized=1"):
            payload = json.dumps({"url": channel["url"]})
            conn.execute(
                "INSERT OR IGNORE INTO jobs(kind,payload,dedupe_key,created_at,updated_at) "
                "VALUES('check',?,?,?,?)",
                (payload, f"check:{channel['id']}", stamp, stamp),
            )
        conn.execute(
            "INSERT INTO settings VALUES('last_scheduled',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(due.isoformat()),),
        )
