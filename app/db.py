from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


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
