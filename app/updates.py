from __future__ import annotations

import json
import logging
import urllib.request
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

from packaging.version import Version

from .db import Database

log = logging.getLogger(__name__)
REPOSITORIES = {"application": "karoc123/nebula-to-jellyfin", "yt-dlp": "yt-dlp/yt-dlp"}


def installed() -> dict[str, str]:
    return {"application": version("nebula-to-jellyfin"), "yt-dlp": version("yt-dlp")}


def fetch_release(repository: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nebula-to-jellyfin"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if payload.get("prerelease") or payload.get("draft"):
        raise ValueError("No stable release available")
    return str(payload["tag_name"]).removeprefix("v")


def check(db: Database, current: datetime | None = None) -> None:
    current = current or datetime.now(UTC)
    for component, local in installed().items():
        old = db.one("SELECT * FROM update_cache WHERE component=?", (component,))
        if old and current - datetime.fromisoformat(old["attempted_at"]) < timedelta(days=1):
            if old["installed"] != local:
                db.execute(
                    "UPDATE update_cache SET installed=?,status='unknown' WHERE component=?",
                    (local, component),
                )
            continue
        latest = old["latest"] if old else None
        succeeded = old["succeeded_at"] if old else None
        # Persist the attempt before network I/O so a restart cannot repeat it immediately.
        db.execute(
            "INSERT INTO update_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
            "installed=excluded.installed,status='unknown',attempted_at=excluded.attempted_at,"
            "error=NULL",
            (component, local, latest, "unknown", current.isoformat(), succeeded, None),
        )
        status, error = "unknown", None
        try:
            latest = fetch_release(REPOSITORIES[component])
            status = "available" if Version(latest) > Version(local) else "current"
            succeeded = current.isoformat()
        except Exception as exc:
            error = str(exc)
            latest = old["latest"] if old else None
            log.warning("Update check failed for %s: %s", component, error)
        db.execute(
            "INSERT INTO update_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
            "installed=excluded.installed,latest=excluded.latest,status=excluded.status,"
            "attempted_at=excluded.attempted_at,succeeded_at=excluded.succeeded_at,error=excluded.error",
            (component, local, latest, status, current.isoformat(), succeeded, error),
        )
