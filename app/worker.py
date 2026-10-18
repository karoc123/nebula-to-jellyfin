from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from threading import Event

from . import ingest, library, nebula, ytdlp
from .config import Config
from .db import Database, now

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config, db: Database, stop: Event):
        self.config, self.db, self.stop = config, db, stop

    def job(self, job: dict) -> None:
        self.db.execute(
            "UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(), job["id"])
        )
        payload = json.loads(job["payload"])
        try:
            if job["kind"] == "video":
                result = ingest.import_video(self.config, self.db, payload["url"])
            elif job["kind"] == "cookie_test":
                if not self.config.cookie_path.exists():
                    raise ValueError("No cookie file has been imported")
                claims = nebula.authorization_claims(nebula.cookie_token(self.config.cookie_path))
                if claims.get("is_anonymous") or claims.get("is_subscribed") is False:
                    raise ValueError(
                        "Cookies are valid but the Nebula account has no active subscription"
                    )
                info = ytdlp.fetch_info(self.config, ytdlp.TEST_URL)
                result = {
                    "subscription": "active",
                    "test video": info.get("title") or ytdlp.TEST_URL,
                    "channel": info.get("channel"),
                }
            else:
                result = ingest.import_channel(
                    self.config, self.db, payload, checking=job["kind"] == "check"
                )
            self.db.execute(
                "UPDATE jobs SET status='done',result=?,error=NULL,updated_at=? WHERE id=?",
                (json.dumps(result), now(), job["id"]),
            )
        except Exception as exc:
            if self.stop.is_set():
                return
            log.exception("Background job %s failed", job["id"])
            self.db.execute(
                "UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=?",
                (str(exc), now(), job["id"]),
            )
            if job["kind"] in {"channel", "check"}:
                channel_id = payload["url"].rsplit("/", 1)[1]
                self.db.execute(
                    "UPDATE channels SET last_error=? WHERE id=?", (str(exc), channel_id)
                )

    def video(self, video: dict) -> None:
        identity = video["id"]
        work = self.config.work_dir / identity
        try:
            work.mkdir(parents=True, exist_ok=True)
            channel = self.db.channel(video["channel_id"])
            if channel is None:
                raise ValueError("Channel is missing")
            receipt_path = work / "completed.json"
            if not receipt_path.exists():
                self.db.update_video(identity, status="downloading", error=None)
                if not video["metadata_ready"]:
                    info = ytdlp.fetch_info(self.config, video["url"])
                    if info.get("id") != identity:
                        raise ValueError("Nebula returned a different video identity")
                    self.db.update_video(
                        identity,
                        title=info.get("title") or identity,
                        description=info.get("description") or "",
                        published_at=ingest.publication(info),
                        duration=info.get("duration"),
                        thumb_url=info.get("thumbnail"),
                        metadata_ready=1,
                    )
                    video = self.db.video(identity)
                if not video["path_stem"]:
                    self.db.update_video(identity, path_stem=library.path_stem(channel, video))
                    video = self.db.video(identity)
                log.info("[%s] Downloading %s", identity, video["title"])
                ytdlp.download(
                    self.config,
                    video,
                    work,
                    self.db.setting("options", {}),
                    lambda progress: self.db.update_video(identity, progress=progress),
                    self.stop,
                )
                if self.stop.is_set():
                    return
                receipt = library.prepare_receipt(work)
            else:
                receipt = json.loads(receipt_path.read_text())
            if not video["target_path"]:
                target = video["path_stem"] + Path(receipt["media"]).suffix.lower()
                self.db.update_video(identity, target_path=target, status="publishing")
                video = self.db.video(identity)
            else:
                self.db.update_video(identity, status="publishing")
            library.publish(self.config, video, channel, work)
            # Keep the receipt until this commit succeeds.
            self.db.update_video(identity, status="done", progress=100, error=None)
            log.info("[%s] Published %s", identity, video["target_path"])
        except Exception as exc:
            if self.stop.is_set():
                return
            log.exception("[%s] Video failed", identity)
            self.db.update_video(identity, status="error", error=str(exc))
            return
        try:
            shutil.rmtree(work)
        except OSError:
            log.warning("[%s] Could not clean completed work directory", identity, exc_info=True)

    def step(self) -> bool:
        job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        if job:
            self.job(job)
        if self.stop.is_set():
            return bool(job)
        video = self.db.one(
            "SELECT * FROM videos WHERE status IN ('queued','publishing') "
            "ORDER BY CASE status WHEN 'publishing' THEN 0 ELSE 1 END, updated_at, id LIMIT 1"
        )
        if video:
            self.video(video)
        return bool(job or video)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                busy = self.step()
            except Exception:
                log.exception("Worker iteration failed")
                busy = False
            if not busy:
                self.stop.wait(1)
