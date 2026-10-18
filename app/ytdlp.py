from __future__ import annotations

import logging
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from yt_dlp import YoutubeDL

from .config import FORMAT, Config, validate_options

log = logging.getLogger(__name__)
# Subscriber-only video. The subscription check is authoritative; this URL proves that the
# saved cookies also unlock Nebula originals.
TEST_URL = "https://nebula.tv/videos/jetlag-s19e8"


class Stopped(Exception):
    """The worker is stopping; leave partial files and persisted work intact."""


class Logger:
    def debug(self, message):
        if not message.startswith("[debug]"):
            log.info("%s", message)

    def info(self, message):
        log.info("%s", message)

    def warning(self, message):
        log.warning("%s", message)

    def error(self, message):
        log.error("%s", message)


def base_options(config: Config) -> dict:
    options = {
        "logger": Logger(),
        "noprogress": True,
        "quiet": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 5,
        "cachedir": str(config.data_dir / "cache"),
        "noplaylist": True,
        "mark_watched": False,
    }
    return options


@contextmanager
def session(config: Config, options: dict):
    # yt-dlp saves its cookie jar on exit. Use a private snapshot so an in-flight
    # operation cannot overwrite cookies imported through Settings meanwhile.
    with tempfile.TemporaryDirectory(prefix="auth-", dir=config.data_dir) as directory:
        if config.cookie_path.exists():
            snapshot = Path(directory) / "cookies.txt"
            shutil.copyfile(config.cookie_path, snapshot)
            snapshot.chmod(0o600)
            options["cookiefile"] = str(snapshot)
        with YoutubeDL(options) as downloader:
            yield downloader


def fetch_info(config: Config, url: str, *, flat: bool = False) -> dict:
    options = base_options(config)
    options.update({"extract_flat": "in_playlist" if flat else False, "skip_download": True})
    with session(config, options) as downloader:
        info = downloader.extract_info(url, download=False)
        if not info:
            raise RuntimeError("No metadata returned by Nebula")
        if flat:
            info["entries"] = list(info.get("entries") or [])
        return info


def download(config: Config, video: dict, work: Path, options: dict, progress, stop: Event) -> None:
    last_update = 0.0

    def hook(state):
        nonlocal last_update
        if stop.is_set():
            raise Stopped("Application is stopping")
        stamp = time.monotonic()
        if stamp - last_update < 1 and state.get("status") != "finished":
            return
        last_update = stamp
        total = state.get("total_bytes") or state.get("total_bytes_estimate") or 0
        percent = min(99, int(100 * state.get("downloaded_bytes", 0) / total)) if total else 0
        progress(percent)

    parameters = base_options(config)
    parameters.update(
        {
            "format": FORMAT,
            "merge_output_format": "mp4",
            "writesubtitles": True,
            "subtitleslangs": ["en.*", "de.*"],
            "subtitlesformat": "srt/vtt/best",
        }
    )
    parameters.update(validate_options(options))
    parameters.update(
        {
            "outtmpl": str(work / "video.%(ext)s"),
            "continuedl": True,
            "overwrites": False,
            "skip_unavailable_fragments": False,
            "writeinfojson": True,
            "updatetime": False,
            "progress_hooks": [hook],
            "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
        }
    )
    with session(config, parameters) as downloader:
        if downloader.download([video["url"]]) != 0:
            raise RuntimeError("yt-dlp download failed; see logs for details")
