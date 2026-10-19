from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


# Download list filters. "available" videos are catalog entries, not downloads.
DOWNLOAD_STATUSES = {
    "": ("queued", "downloading", "publishing", "done", "error"),
    "active": ("queued", "downloading", "publishing"),
    "done": ("done",),
    "error": ("error",),
}
JOB_RETENTION = timedelta(days=7)


def like(query: str) -> str:
    escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def downloads_filter(channel: str = "", query: str = "") -> tuple[str, list]:
    """WHERE clause for visible downloads; column names are unique to the videos table."""
    clauses, args = ["dismissed=0", "status!='available'"], []
    if channel:
        clauses.append("channel_id=?")
        args.append(channel)
    if query.strip():
        clauses.append("title LIKE ? ESCAPE '\\'")
        args.append(like(query))
    return " AND ".join(clauses), args


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def migrate(self, directory: Path | None = None) -> None:
        directory = directory or Path(__file__).parent / "migrations"
        migrations = sorted(directory.glob("[0-9]*.sql"))
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and tables:
                raise RuntimeError("Unversioned database; PoC databases are not imported")
            if version > len(migrations):
                raise RuntimeError("Database is newer than this application; downgrade refused")
            conn.execute("PRAGMA journal_mode=WAL")
            for expected, file in enumerate(migrations, start=1):
                if int(file.name.split("_")[0]) != expected:
                    raise RuntimeError("Migration versions must be contiguous")
                if expected > version:
                    try:
                        conn.executescript(
                            f"BEGIN IMMEDIATE;\n{file.read_text()}\n"
                            f"PRAGMA user_version={expected};\nCOMMIT;"
                        )
                    except Exception:
                        conn.rollback()
                        raise

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> None:
        with self.connect() as conn:
            conn.execute(sql, args)

    def setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO settings VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def clear_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key=?", (key,))

    def pause(self, reason: str) -> None:
        self.set_setting("paused", {"reason": reason, "at": now()})

    def enqueue(self, kind: str, payload: dict, key: str | None = None) -> int:
        serialized = json.dumps(payload, sort_keys=True)
        key = key or f"{kind}:{serialized}"
        stamp = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(kind,payload,dedupe_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (kind, serialized, key, stamp, stamp),
            )
            row = conn.execute(
                "SELECT id FROM jobs WHERE dedupe_key=? ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            return row["id"]

    def recover(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            conn.execute("UPDATE videos SET status='queued' WHERE status='downloading'")
            # Publishing jobs retain their target path and continue without downloading.

    def update_video(self, video_id: str, **fields) -> None:
        allowed = {
            "title",
            "description",
            "published_at",
            "duration",
            "thumb_url",
            "metadata_ready",
            "status",
            "progress",
            "error",
            "path_stem",
            "target_path",
        }
        if not fields or fields.keys() - allowed:
            raise ValueError("Invalid video update fields")
        fields["updated_at"] = now()
        columns = ",".join(f"{name}=?" for name in fields)
        self.execute(f"UPDATE videos SET {columns} WHERE id=?", (*fields.values(), video_id))

    def video(self, identity: str) -> dict | None:
        return self.one("SELECT * FROM videos WHERE id=?", (identity,))

    def channel(self, identity: str) -> dict | None:
        return self.one("SELECT * FROM channels WHERE id=?", (identity,))

    def select_videos(self, channel_id: str, identities: list[str]) -> None:
        with self.connect() as conn:
            for identity in identities:
                row = conn.execute(
                    "SELECT status FROM videos WHERE id=? AND channel_id=?", (identity, channel_id)
                ).fetchone()
                if row is None:
                    raise ValueError(f"Video does not belong to channel: {identity}")
                conn.execute(
                    "UPDATE videos SET status='queued',updated_at=? WHERE id=? "
                    "AND status='available'",
                    (now(), identity),
                )

    def retry(self, identity: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "UPDATE videos SET status='queued',error=NULL,progress=0,updated_at=? "
                    "WHERE id=? AND status='error'",
                    (now(), identity),
                ).rowcount
                == 1
            )

    def remove(self, identity: str) -> bool:
        """Failed videos return to the catalog; finished videos stay done but are hidden."""
        stamp = now()
        with self.connect() as conn:
            failed = conn.execute(
                "UPDATE videos SET status='available',error=NULL,progress=0,updated_at=? "
                "WHERE id=? AND status='error'",
                (stamp, identity),
            ).rowcount
            done = conn.execute(
                "UPDATE videos SET dismissed=1,updated_at=? WHERE id=? AND status='done'",
                (stamp, identity),
            ).rowcount
            return failed + done == 1

    def bulk(self, action: str, channel: str = "", query: str = "") -> int:
        where, args = downloads_filter(channel, query)
        statement = {
            "retry-errors": "status='queued',error=NULL,progress=0",
            "remove-errors": "status='available',error=NULL,progress=0",
            "remove-done": "dismissed=1",
        }[action]
        status = "done" if action == "remove-done" else "error"
        with self.connect() as conn:
            return conn.execute(
                f"UPDATE videos SET {statement},updated_at=? WHERE {where} AND status=?",
                (now(), *args, status),
            ).rowcount

    def prune_jobs(self) -> None:
        cutoff = (datetime.now(UTC) - JOB_RETENTION).isoformat()
        self.execute("DELETE FROM jobs WHERE status='done' AND updated_at<?", (cutoff,))
