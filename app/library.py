from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .config import VIDEO_EXTENSIONS, Config

log = logging.getLogger(__name__)


def sanitize(value: str, maxbytes: int = 100) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    value = value.encode("utf-8")[:maxbytes].decode("utf-8", "ignore").strip(" .-")
    if value.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10)),
    }:
        value = "_" + value
    return value or "Unknown"


def path_stem(channel: dict, video: dict) -> str:
    date = datetime.fromisoformat(video["published_at"]).date().isoformat()
    return str(
        Path(channel["directory"])
        / f"Season {date[:4]}"
        / f"{date} - {sanitize(video['title'])} [{video['id']}]"
    )


def atomic_bytes(path: Path, content: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_nfo(path: Path, kind: str, fields: dict, identity: str) -> None:
    root = ET.Element(kind)
    for key, value in fields.items():
        if value is not None:
            # XML 1.0 rejects control characters even when escaped.
            ET.SubElement(root, key).text = re.sub(
                r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(value)
            )
    ET.SubElement(root, "uniqueid", {"type": "nebula", "default": "true"}).text = identity
    ET.SubElement(root, "lockdata").text = "false" if kind == "tvshow" else "true"
    ET.indent(root)
    atomic_bytes(path, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def fetch_image(url: str | None, dest: Path) -> bool:
    if dest.exists():
        return True
    if not url:
        log.warning("Artwork absent at source: %s", dest.name)
        return False
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "nebula-to-jellyfin/0.1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(20_000_001)
    except (urllib.error.URLError, TimeoutError) as exc:
        log.warning("Optional artwork unavailable: %s", exc)
        return False
    if not content or len(content) > 20_000_000:
        log.warning("Optional artwork is empty or too large")
        return False
    # Filesystem failures intentionally propagate to the affected video.
    atomic_bytes(dest, content)
    return True


def prepare_receipt(work: Path) -> dict:
    receipt = work / "completed.json"
    if receipt.exists():
        return json.loads(receipt.read_text())
    media = [
        p
        for p in work.glob("video.*")
        if p.suffix.lower() in VIDEO_EXTENSIONS and not re.search(r"\.f\d+\.", p.name)
    ]
    if len(media) != 1 or media[0].stat().st_size == 0:
        raise RuntimeError("Expected one nonempty completed video file")
    payload = {"media": media[0].name, "size": media[0].stat().st_size}
    atomic_bytes(receipt, json.dumps(payload).encode())
    return payload


def publish(config: Config, video: dict, channel: dict, work: Path) -> Path:
    """Repeat publication after any interruption; the database already owns the target path."""
    receipt = json.loads((work / "completed.json").read_text())
    source = work / receipt["media"]
    target = config.library_dir / video["target_path"]
    if target.exists() and (source.exists() or target.stat().st_size != receipt["size"]):
        raise RuntimeError("Published target conflicts with completed media; refusing replacement")
    target.parent.mkdir(parents=True, exist_ok=True)
    stem = target.with_suffix("")
    show = config.library_dir / channel["directory"]
    write_nfo(
        show / "tvshow.nfo",
        "tvshow",
        {
            "title": channel["name"],
            "plot": channel["description"],
            "studio": "Nebula",
        },
        channel["id"],
    )
    poster = show / "poster.jpg"
    if fetch_image(channel.get("image_url") or video.get("thumb_url"), poster):
        if not (show / "fanart.jpg").exists():
            shutil.copyfile(poster, show / "fanart.jpg")
    thumb = stem.with_name(stem.name + "-thumb.jpg")
    fetch_image(video.get("thumb_url"), thumb)
    subtitles = list(work.glob("video.*.srt")) + list(work.glob("video.*.vtt"))
    if not subtitles:
        log.warning("[%s] No optional subtitles supplied by source", video["id"])
    for subtitle in subtitles:
        suffix = subtitle.name[len("video") :]
        shutil.copyfile(subtitle, stem.with_name(stem.name + suffix))
    date = datetime.fromisoformat(video["published_at"])
    write_nfo(
        stem.with_name(stem.name + ".nfo"),
        "episodedetails",
        {
            "title": video["title"],
            "showtitle": channel["name"],
            "season": date.year,
            "plot": video["description"],
            "aired": date.date().isoformat(),
            "premiered": date.date().isoformat(),
            "dateadded": date.strftime("%Y-%m-%d %H:%M:%S"),
            "sorttitle": f"{video['published_at']} {video['id']}",
            "runtime": round((video.get("duration") or 0) / 60),
        },
        video["id"],
    )
    info = work / "video.info.json"
    if info.exists():
        metadata = config.data_dir / "metadata"
        metadata.mkdir(exist_ok=True)
        atomic_bytes(metadata / f"{video['id']}.json", info.read_bytes())
    if not target.exists():
        if not source.exists() or source.stat().st_size != receipt["size"]:
            raise RuntimeError("Completed media is missing or changed")
        # Same filesystem, atomic publication. A crash now leaves the receipt for recovery.
        source.replace(target)
    stamp = date.timestamp()
    for path in [target, stem.with_name(stem.name + ".nfo"), thumb]:
        if path.exists():
            os.utime(path, (stamp, stamp))
    return target
