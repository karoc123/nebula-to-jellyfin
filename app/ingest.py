from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import library, ytdlp
from .config import Config
from .db import Database, now

SLUG = re.compile(r"[A-Za-z0-9_-]+")


def normalize_url(value: str, kind: str | None = None) -> str:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"nebula.tv", "www.nebula.tv"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError("Only nebula.tv video and channel URLs are supported")
    except ValueError as exc:
        raise ValueError("Invalid Nebula URL") from exc
    parts = parsed.path.strip("/").split("/")
    is_video = len(parts) == 2 and parts[0] == "videos" and SLUG.fullmatch(parts[1])
    is_channel = (
        len(parts) == 1
        and SLUG.fullmatch(parts[0])
        and parts[0] not in {"videos", "library", "myshows", "settings", "login"}
    )
    if (
        not (is_video or is_channel)
        or (kind == "video" and not is_video)
        or (kind == "channel" and not is_channel)
    ):
        raise ValueError("Expected a Nebula video URL or channel URL of the selected type")
    return "https://nebula.tv/" + "/".join(parts)


def publication(info: dict) -> str:
    for key in ("release_timestamp", "timestamp"):
        if info.get(key) is not None:
            try:
                return datetime.fromtimestamp(float(info[key]), UTC).isoformat()
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError("Invalid publication timestamp") from exc
    for key in ("release_date", "upload_date"):
        if info.get(key):
            value = str(info[key])
            if not re.fullmatch(r"\d{8}", value):
                raise ValueError("Invalid source publication date")
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC).isoformat()
    raise ValueError("Source publication date is missing; download date is never substituted")


def identity(value) -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value) or len(value) > 80:
        raise ValueError("Missing or invalid Nebula identity")
    return value


def upsert_channel(
    db: Database,
    channel_id: str,
    name: str,
    url: str,
    description: str = "",
    image_url: str | None = None,
) -> None:
    identity(channel_id)
    directory = f"{library.sanitize(name, 90)} [{channel_id}]"
    db.execute(
        "INSERT INTO channels(id,name,url,directory,description,image_url) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,"
        "image_url=COALESCE(excluded.image_url,channels.image_url)",
        (channel_id, name, url, directory, description or "", image_url),
    )


def store_video(
    db: Database, info: dict, channel_id: str, url: str, *, selected: bool, full: bool = False
) -> None:
    video_id = identity(info.get("id"))
    stamp = now()
    published = publication(info) if full else None
    title = info.get("title") or video_id
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO videos(id,channel_id,url,title,description,published_at,"
            "duration,thumb_url,metadata_ready,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                video_id,
                channel_id,
                normalize_url(url, "video"),
                title,
                info.get("description") or "",
                published,
                info.get("duration"),
                info.get("thumbnail"),
                int(full),
                "queued" if selected else "available",
                stamp,
                stamp,
            ),
        )
        if selected:
            conn.execute(
                "UPDATE videos SET status='queued',updated_at=? WHERE id=? AND status='available'",
                (stamp, video_id),
            )


def import_video(config: Config, db: Database, url: str) -> dict:
    info = ytdlp.fetch_info(config, normalize_url(url, "video"))
    if info.get("_type") == "playlist":
        raise ValueError("Expected a single video")
    channel_id = identity(info.get("channel_id"))
    upsert_channel(
        db, channel_id, info.get("channel") or channel_id, f"https://nebula.tv/{channel_id}"
    )
    store_video(db, info, channel_id, url, selected=True, full=True)
    return {"video_id": info["id"]}


def import_channel(config: Config, db: Database, payload: dict, *, checking=False) -> dict:
    url = normalize_url(payload["url"], "channel")
    channel_id = url.rsplit("/", 1)[1]
    old = db.channel(channel_id)
    info = ytdlp.fetch_info(config, url, flat=True)
    if info.get("_type") != "playlist" or "entries" not in info:
        raise ValueError("Nebula did not return a channel catalog")
    upsert_channel(
        db,
        channel_id,
        info.get("title") or channel_id,
        url,
        info.get("description") or "",
        info.get("thumbnail"),
    )
    mode = payload.get("mode", "future")
    entries = info["entries"]
    # Nebula's channel extractor returns newest first. Baseline every entry before subscribing.
    # A transaction prevents a half-saved future-only baseline after a restart.
    with db.connect() as conn:
        for index, entry in enumerate(entries):
            video_id = identity(entry.get("id"))
            video_url = normalize_url(entry.get("webpage_url") or entry.get("url") or "", "video")
            known = conn.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
            selected = (checking and old is not None and old["initialized"] and known is None) or (
                not checking and (mode == "all" or (mode == "latest" and index < payload["latest"]))
            )
            stamp = now()
            conn.execute(
                "INSERT OR IGNORE INTO videos(id,channel_id,url,title,status,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    video_id,
                    channel_id,
                    video_url,
                    entry.get("title") or video_id,
                    "queued" if selected else "available",
                    stamp,
                    stamp,
                ),
            )
            if selected:
                conn.execute(
                    "UPDATE videos SET status='queued',updated_at=? "
                    "WHERE id=? "
                    "AND status='available'",
                    (stamp, video_id),
                )
        conn.execute(
            "UPDATE channels SET initialized=1,last_checked_at=?,last_error=NULL WHERE id=?",
            (now(), channel_id),
        )
        if not checking and "subscribed" in payload:
            conn.execute(
                "UPDATE channels SET subscribed=? WHERE id=?",
                (int(payload["subscribed"]), channel_id),
            )
    return {"catalogued": len(entries), "channel_id": channel_id}
