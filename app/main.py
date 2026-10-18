from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.cookiejar import LoadError, MozillaCookieJar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ingest, library, scheduler, updates, worker
from .config import OPTION_TYPES, Config, validate_options, validate_schedule
from .db import Database

BASE_DIR = Path(__file__).parent
log = logging.getLogger("app")


def save_cookies(config: Config, content: str) -> None:
    if len(content.encode()) > 1_000_000:
        raise ValueError("Cookie file must be smaller than 1 MB")
    if content.strip():
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.data_dir) as temp:
            temp.write(content.strip() + "\n")
            temp.flush()
            jar = MozillaCookieJar(temp.name)
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (LoadError, ValueError) as exc:
                raise ValueError("Expected a Netscape cookie file") from exc
            if not any(c.domain.lstrip(".") in {"nebula.tv", "www.nebula.tv"} for c in jar):
                raise ValueError("Cookie file has no Nebula cookies")
    if config.cookie_path.exists():
        backup = config.cookie_path.with_suffix(".txt.bak")
        library.atomic_bytes(backup, config.cookie_path.read_bytes(), mode=0o600)
        backup.chmod(0o600)
    if not content.strip():
        config.cookie_path.unlink(missing_ok=True)
        return
    # Set permissions before writing authentication material.
    temporary = config.cookie_path.with_suffix(".incoming")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content.strip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(config.cookie_path)


def create_app(config: Config | None = None, *, start_workers: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        cfg = config or Config.from_env()
        cfg.prepare()
        lock = (cfg.data_dir / "instance.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError(
                "Only one application instance may use this data directory"
            ) from None
        handler = RotatingFileHandler(cfg.log_path, maxBytes=2_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(handler.formatter)
        log.addHandler(console)
        stop = Event()
        threads = []
        try:
            database = Database(cfg.db_path)
            database.migrate()
            database.recover()
            application.state.config = cfg
            application.state.db = database

            def maintain():
                while not stop.is_set():
                    try:
                        scheduler.tick(database, cfg.timezone)
                        updates.check(database)
                    except Exception:
                        log.exception("Maintenance iteration failed")
                    stop.wait(30)

            if start_workers:
                threads = [
                    Thread(target=worker.Worker(cfg, database, stop).run, name="downloads"),
                    Thread(target=maintain, name="maintenance"),
                ]
                for thread in threads:
                    thread.start()
            log.info("Application started; library: %s", cfg.library_dir)
            yield
        finally:
            stop.set()
            for thread in threads:
                await asyncio.to_thread(thread.join)
            log.removeHandler(handler)
            log.removeHandler(console)
            handler.close()
            console.close()
            lock.close()

    application = FastAPI(title="Nebula to Jellyfin", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    def database(request: Request) -> Database:
        return request.app.state.db

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(request, name, context)

    @application.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(request, "index.html")

    @application.post("/add")
    def add(
        request: Request,
        urls: str = Form(...),
        mode: str = Form("video"),
        latest: int = Form(10),
        subscribed: bool = Form(False),
    ):
        if mode not in {"video", "future", "latest", "all"} or not 1 <= latest <= 10000:
            raise HTTPException(400, "Choose a valid selection and latest count (1–10000)")
        lines = [line.strip() for line in urls.splitlines() if line.strip()]
        if not 1 <= len(lines) <= 100:
            raise HTTPException(400, "Enter 1–100 Nebula URLs")
        kind = "video" if mode == "video" else "channel"
        try:
            normalized = [ingest.normalize_url(url, kind) for url in lines]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        for url in normalized:
            database(request).enqueue(
                kind,
                {
                    "url": url,
                    "mode": mode,
                    "latest": latest,
                    "subscribed": subscribed,
                },
            )
        return RedirectResponse("/?queued=1", 303)

    @application.get("/channels/{channel_id}", response_class=HTMLResponse)
    def channel(request: Request, channel_id: str, offset: int = Query(0, ge=0)):
        db = database(request)
        record = db.channel(channel_id)
        if record is None:
            raise HTTPException(404, "Channel not found")
        videos = db.rows(
            "SELECT * FROM videos WHERE channel_id=? "
            "ORDER BY created_at DESC,id LIMIT 100 OFFSET ?",
            (channel_id, offset),
        )
        return render(request, "channel.html", channel=record, videos=videos, offset=offset)

    @application.post("/channels/{channel_id}/select")
    def select(request: Request, channel_id: str, videos: list[str] = Form(...)):
        try:
            database(request).select_videos(channel_id, videos)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/channels/{channel_id}", 303)

    @application.post("/api/channels/{channel_id}/subscription")
    def subscribe(request: Request, channel_id: str, enabled: bool = Form(...)):
        db = database(request)
        channel = db.channel(channel_id)
        if channel is None:
            raise HTTPException(404, "Channel not found")
        if enabled and not channel["initialized"]:
            job_id = db.enqueue(
                "channel",
                {"url": channel["url"], "mode": "future", "subscribed": True},
                f"baseline:{channel_id}",
            )
            return {"job_id": job_id}
        db.execute("UPDATE channels SET subscribed=? WHERE id=?", (int(enabled), channel_id))
        return {"ok": True}

    @application.post("/api/check")
    def check(request: Request):
        return {"jobs": scheduler.enqueue_checks(database(request))}

    @application.post("/api/channels/{channel_id}/check")
    def check_channel(request: Request, channel_id: str):
        record = database(request).channel(channel_id)
        if record is None:
            raise HTTPException(404, "Channel not found")
        return {
            "job_id": database(request).enqueue(
                "check", {"url": record["url"]}, f"check:{channel_id}"
            )
        }

    @application.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        db = database(request)
        cfg = request.app.state.config
        return render(
            request,
            "settings.html",
            options=json.dumps(db.setting("options", {}), indent=2),
            schedule=db.setting("schedule", {"time": "04:00", "timezone": cfg.timezone}),
            cookie_present=cfg.cookie_path.exists(),
            supported=", ".join(OPTION_TYPES),
        )

    @application.post("/settings")
    def save_settings(
        request: Request,
        options: str = Form("{}"),
        at: str = Form("04:00"),
        timezone: str = Form("Europe/Berlin"),
    ):
        try:
            parsed = validate_options(options)
            validate_schedule(at, timezone)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with database(request).connect() as conn:
            for key, value in [
                ("options", parsed),
                ("schedule", {"time": at, "timezone": timezone}),
            ]:
                conn.execute(
                    "INSERT INTO settings VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
        return RedirectResponse("/settings?saved=1", 303)

    @application.post("/settings/cookies")
    async def cookies(
        request: Request, cookies: str = Form(""), file: UploadFile | None = File(None)
    ):
        try:
            if file and file.filename:
                cookies = (await file.read(1_000_001)).decode("utf-8")
            await asyncio.to_thread(save_cookies, request.app.state.config, cookies)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/settings?saved=1", 303)

    @application.post("/api/settings/test-cookie", status_code=202)
    def cookie_test(request: Request):
        return {"job_id": database(request).enqueue("cookie_test", {})}

    @application.post("/api/videos/{video_id}/retry")
    def retry(request: Request, video_id: str):
        db = database(request)
        if db.video(video_id) is None:
            raise HTTPException(404, "Video not found")
        if not db.retry(video_id):
            raise HTTPException(409, "Only failed videos can be retried")
        return {"ok": True}

    @application.post("/api/jobs/{job_id}/retry")
    def retry_job(request: Request, job_id: int):
        db = database(request)
        job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise HTTPException(404, "Job not found")
        if job["status"] != "error":
            raise HTTPException(409, "Only failed jobs can be retried")
        return {"job_id": db.enqueue(job["kind"], json.loads(job["payload"]), job["dedupe_key"])}

    @application.get("/api/jobs/{job_id}")
    def job_status(request: Request, job_id: int):
        job = database(request).one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @application.get("/api/status")
    def status(
        request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        db = database(request)
        cfg = request.app.state.config
        schedule = db.setting("schedule", {"time": "04:00", "timezone": cfg.timezone})
        _, next_run = scheduler.schedule_window(
            datetime.now(UTC), schedule["time"], schedule["timezone"]
        )
        cached = {r["component"]: r for r in db.rows("SELECT * FROM update_cache")}
        versions = []
        for name, version in updates.installed().items():
            record = cached.get(
                name, {"component": name, "latest": None, "status": "unknown", "succeeded_at": None}
            )
            if record.get("installed") != version:
                record["status"] = "unknown"
            record["installed"] = version
            versions.append(record)
        return {
            "channels": db.rows(
                "SELECT c.*,count(v.id) AS total,"
                "sum(CASE WHEN v.status='done' THEN 1 ELSE 0 END) AS done "
                "FROM channels c LEFT JOIN videos v ON v.channel_id=c.id GROUP BY c.id "
                "ORDER BY c.name"
            ),
            "videos": db.rows(
                "SELECT v.*,c.name AS channel_name FROM videos v "
                "JOIN channels c ON c.id=v.channel_id WHERE v.status!='available' "
                "ORDER BY v.updated_at DESC,v.id LIMIT ? OFFSET ?",
                (limit, offset),
            ),
            "jobs": db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 30"),
            "counts": {
                r["status"]: r["n"]
                for r in db.rows("SELECT status,count(*) AS n FROM videos GROUP BY status")
            },
            "updates": versions,
            "next_check": next_run.isoformat(),
            "schedule": schedule,
        }

    @application.get("/logs", response_class=HTMLResponse)
    def logs(request: Request):
        return render(request, "logs.html")

    @application.get("/api/logs", response_class=PlainTextResponse)
    def log_tail(request: Request, lines: int = Query(400, ge=1, le=2000)):
        path = request.app.state.config.log_path
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 500_000))
                return "\n".join(handle.read().decode("utf-8", "replace").splitlines()[-lines:])
        except FileNotFoundError:
            return ""

    @application.get("/api/health")
    def health(request: Request):
        database(request).one("SELECT 1")
        return {"ok": True, **updates.installed()}

    return application


app = create_app()
