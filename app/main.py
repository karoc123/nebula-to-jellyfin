from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http.cookiejar import LoadError, MozillaCookieJar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event, Lock, Thread

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ingest, library, scheduler, updates, worker
from .config import OPTION_TYPES, Config, validate_options, validate_schedule
from .db import DOWNLOAD_STATUSES, JOB_RETENTION, Database, downloads_filter, like

BASE_DIR = Path(__file__).parent
log = logging.getLogger("app")
COOKIE_LOCK = Lock()
# Channel catalog filters, including videos that were never selected.
CATALOG_STATUSES = {
    "": (),
    "available": ("available",),
    "active": ("queued", "downloading", "publishing"),
    "done": ("done",),
    "error": ("error",),
}


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
    with COOKIE_LOCK:
        if config.cookie_path.exists():
            backup = config.cookie_path.with_suffix(".txt.bak")
            library.atomic_bytes(backup, config.cookie_path.read_bytes(), mode=0o600)
        if not content.strip():
            config.cookie_path.unlink(missing_ok=True)
            return
        library.atomic_bytes(config.cookie_path, (content.strip() + "\n").encode(), mode=0o600)


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
                        database.prune_jobs()
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

    @application.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin != str(request.base_url).rstrip("/")
            ):
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, 403)
        return await call_next(request)

    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def template_context(request: Request):
        cfg = getattr(request.app.state, "config", None)
        db = getattr(request.app.state, "db", None)
        return {
            "cookies_saved": bool(cfg and cfg.cookie_path.exists()),
            "paused": db.setting("paused") if db else None,
        }

    templates = Jinja2Templates(
        directory=BASE_DIR / "templates", context_processors=[template_context]
    )

    def database(request: Request) -> Database:
        return request.app.state.db

    def render(request: Request, name: str, **context):
        return templates.TemplateResponse(request, name, context)

    def channel_list(db: Database) -> list[dict]:
        return db.rows(
            "SELECT c.*,count(v.id) AS total,"
            "coalesce(sum(v.status='done'),0) AS done,"
            "coalesce(sum(v.status='error'),0) AS failed,"
            "coalesce(sum(v.status IN ('queued','downloading','publishing')),0) AS active "
            "FROM channels c LEFT JOIN videos v ON v.channel_id=c.id GROUP BY c.id "
            "ORDER BY c.name COLLATE NOCASE"
        )

    def next_check(request: Request) -> dict:
        cfg = request.app.state.config
        schedule = database(request).setting(
            "schedule", {"time": "04:00", "timezone": cfg.timezone}
        )
        _, next_run = scheduler.schedule_window(
            datetime.now(UTC), schedule["time"], schedule["timezone"]
        )
        return {"next_check": next_run.isoformat(), "schedule": schedule}

    @application.get("/", response_class=HTMLResponse)
    def index(request: Request):
        channels = database(request).rows(
            "SELECT id,name FROM channels ORDER BY name COLLATE NOCASE"
        )
        return render(request, "index.html", channels=channels)

    @application.get("/channels", response_class=HTMLResponse)
    def channels(request: Request):
        return render(
            request,
            "channels.html",
            channels=channel_list(database(request)),
            **next_check(request),
        )

    @application.get("/activity", response_class=HTMLResponse)
    def activity(request: Request):
        return render(request, "activity.html", retention=JOB_RETENTION.days)

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
        return RedirectResponse("/activity?queued=1", 303)

    @application.get("/channels/{channel_id}", response_class=HTMLResponse)
    def channel(
        request: Request,
        channel_id: str,
        offset: int = Query(0, ge=0),
        status: str = Query(""),
        q: str = Query("", max_length=200),
    ):
        db = database(request)
        record = db.channel(channel_id)
        if record is None:
            raise HTTPException(404, "Channel not found")
        if status not in CATALOG_STATUSES:
            raise HTTPException(400, "Unknown status filter")
        counts = {
            r["status"]: r["n"]
            for r in db.rows(
                "SELECT status,count(*) AS n FROM videos WHERE channel_id=? GROUP BY status",
                (channel_id,),
            )
        }
        where, args = "channel_id=?", [channel_id]
        if status:
            statuses = CATALOG_STATUSES[status]
            where += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if q.strip():
            where += " AND title LIKE ? ESCAPE '\\'"
            args.append(like(q))
        videos = db.rows(
            f"SELECT * FROM videos WHERE {where} "
            "ORDER BY published_at DESC,created_at DESC,id LIMIT 100 OFFSET ?",
            (*args, offset),
        )
        return render(
            request,
            "channel.html",
            channel=record,
            videos=videos,
            offset=offset,
            counts=counts,
            status=status,
            q=q,
        )

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

    @application.post("/api/videos/{video_id}/remove")
    def remove(request: Request, video_id: str):
        db = database(request)
        if db.video(video_id) is None:
            raise HTTPException(404, "Video not found")
        if not db.remove(video_id):
            raise HTTPException(409, "Only failed or finished videos can be removed")
        return {"ok": True}

    @application.post("/api/downloads/{action}")
    def bulk(
        request: Request,
        action: str,
        channel: str = Form(""),
        q: str = Form("", max_length=200),
    ):
        if action not in {"retry-errors", "remove-errors", "remove-done"}:
            raise HTTPException(404, "Unknown action")
        return {"count": database(request).bulk(action, channel, q)}

    @application.post("/api/queue/pause")
    def pause(request: Request):
        database(request).pause("Paused manually")
        return {"ok": True}

    @application.post("/api/queue/resume")
    def resume(request: Request):
        database(request).clear_setting("paused")
        return {"ok": True}

    @application.post("/api/jobs/clear")
    def clear_jobs(request: Request, status: str = Form(...)):
        if status not in {"done", "error"}:
            raise HTTPException(400, "Only finished or failed jobs can be cleared")
        database(request).execute("DELETE FROM jobs WHERE status=?", (status,))
        return {"ok": True}

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
    def status(request: Request):
        db = database(request)
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
        return {"updates": versions, "paused": db.setting("paused")}

    @application.get("/api/downloads")
    def downloads(
        request: Request,
        status: str = Query(""),
        channel: str = Query(""),
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        if status not in DOWNLOAD_STATUSES:
            raise HTTPException(400, "Unknown status filter")
        db = database(request)
        where, args = downloads_filter(channel, q)
        counts = {
            r["status"]: r["n"]
            for r in db.rows(
                f"SELECT status,count(*) AS n FROM videos WHERE {where} GROUP BY status", args
            )
        }
        statuses = DOWNLOAD_STATUSES[status]
        where += f" AND status IN ({','.join('?' * len(statuses))})"
        return {
            "videos": db.rows(
                "SELECT v.*,c.name AS channel_name FROM videos v "
                f"JOIN channels c ON c.id=v.channel_id WHERE {where} "
                # Running work first, then the most recent changes.
                "ORDER BY CASE status WHEN 'downloading' THEN 0 WHEN 'publishing' THEN 0 "
                "ELSE 1 END,v.updated_at DESC,v.id LIMIT ? OFFSET ?",
                (*args, *statuses, limit, offset),
            ),
            "total": sum(counts.get(name, 0) for name in statuses),
            "counts": counts,
            "paused": db.setting("paused"),
        }

    @application.get("/api/channels")
    def channels_status(request: Request):
        return {"channels": channel_list(database(request)), **next_check(request)}

    @application.get("/api/jobs")
    def jobs(request: Request, offset: int = Query(0, ge=0)):
        return {
            "jobs": database(request).rows(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 50 OFFSET ?", (offset,)
            )
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
