from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FORMAT = "bv*[vcodec~='^(hvc1|hev1|hevc)'][height<=1080]+ba/bv*[height<=1080]+ba/b[height<=1080]"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
# These options can affect output, execution, authentication, or completion semantics.
# Accept a useful, explicit subset of yt-dlp's Python options instead.
OPTION_TYPES = {
    "format": str,
    "subtitleslangs": list,
    "writesubtitles": bool,
    "writeautomaticsub": bool,
    "ratelimit": int,
    "retries": int,
    "fragment_retries": int,
    "socket_timeout": int,
    "concurrent_fragment_downloads": int,
    "http_chunk_size": int,
    "sleep_interval": int,
    "max_sleep_interval": int,
    "subtitlesformat": str,
    "merge_output_format": str,
}


def validate_options(value: str | dict) -> dict:
    try:
        options = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(options, dict):
        raise ValueError("Options must be a JSON object")
    for key, item in options.items():
        expected = OPTION_TYPES.get(key)
        if expected is None:
            raise ValueError(f"Option is not supported: {key}")
        if type(item) is not expected:
            raise ValueError(f"Invalid value for {key}")
        if expected is int and item < 0:
            raise ValueError(f"{key} must be nonnegative")
        if expected is str and not item.strip():
            raise ValueError(f"{key} must not be empty")
        if key == "subtitleslangs" and not all(isinstance(v, str) for v in item):
            raise ValueError("subtitleslangs must contain strings")
        if key == "merge_output_format" and any(
            f".{extension}" not in VIDEO_EXTENSIONS for extension in item.split("/")
        ):
            raise ValueError("Merge output formats must be mp4, mkv, webm or mov (separated by /)")
    return options


def validate_schedule(at: str, timezone: str) -> None:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("Check time must use HH:MM (24-hour time)")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown timezone") from exc


@dataclass(frozen=True)
class Config:
    data_dir: Path
    library_dir: Path
    work_dir: Path
    timezone: str = "Europe/Berlin"

    @classmethod
    def from_env(cls) -> Config:
        media = Path(os.environ.get("MEDIA_DIR", "/media"))
        return cls(
            Path(os.environ.get("DATA_DIR", "/data")),
            Path(os.environ.get("LIBRARY_DIR", str(media / "library"))),
            Path(os.environ.get("WORK_DIR", str(media / ".nebula-work"))),
            os.environ.get("TZ", "Europe/Berlin"),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nebula-v1.sqlite3"

    @property
    def cookie_path(self) -> Path:
        return self.data_dir / "cookies.txt"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "app.log"

    def prepare(self) -> None:
        validate_schedule("04:00", self.timezone)
        library, work = self.library_dir.resolve(), self.work_dir.resolve()
        if library == work or library in work.parents or work in library.parents:
            raise ValueError("Work and library directories must be separate sibling trees")
        for path in (self.data_dir, self.library_dir, self.work_dir, self.log_path.parent):
            path.mkdir(parents=True, exist_ok=True)
        if library.stat().st_dev != work.stat().st_dev:
            raise ValueError("Work and library directories must be on the same filesystem")
