from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import ingest
from app.main import create_app
from app.worker import Worker


@pytest.mark.parametrize(
    "page",
    [
        "/",
        "/channels",
        "/activity",
        "/settings",
        "/logs",
        "/api/status",
        "/api/downloads",
        "/api/channels",
        "/api/jobs",
        "/api/health",
    ],
)
def test_pages(client, page):
    response = client.get(page)
    assert response.status_code == 200


def test_import_enqueues_without_fetching(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External metadata must not be fetched in the HTTP request")

    monkeypatch.setattr("app.ytdlp.fetch_info", forbidden)
    response = client.post(
        "/add",
        data={"urls": "https://nebula.tv/probe", "mode": "future", "subscribed": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/activity?queued=1"
    jobs = client.get("/api/jobs").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert client.post("/api/settings/test-cookie").status_code == 202


@pytest.mark.parametrize(
    "data",
    [
        {"urls": "https://evil.example/video"},
        {"urls": "https://nebula.tv/probe", "mode": "nonsense"},
        {"urls": "https://nebula.tv/probe", "mode": "latest", "latest": "-1"},
        {"urls": "\n"},
        {"urls": "https://nebula.tv/probe", "mode": "video"},
    ],
)
def test_invalid_import(client, data):
    assert client.post("/add", data=data).status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_settings_validation_and_atomic_save(client):
    response = client.post(
        "/settings", data={"options": '{"subtitleslangs":["en"]}', "at": "06:30", "timezone": "UTC"}
    )
    assert response.status_code == 200
    db = client.app.state.db
    assert db.setting("options") == {"subtitleslangs": ["en"]}
    assert db.setting("schedule") == {"time": "06:30", "timezone": "UTC"}
    assert client.post("/settings", data={"options": '{"outtmpl":"evil"}'}).status_code == 400
    assert db.setting("options") == {"subtitleslangs": ["en"]}
    assert client.post("/settings", data={"options": "{}", "at": "25:00"}).status_code == 400


def test_cookie_import_validation_backup_and_background_test(client, config):
    cookie = "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\ttoken\tsecret\n"
    assert client.post("/settings/cookies", data={"cookies": "bad input"}).status_code == 400
    assert not config.cookie_path.exists()
    assert (
        client.post("/settings/cookies", files={"file": ("cookies.txt", cookie)}).status_code == 200
    )
    assert config.cookie_path.stat().st_mode & 0o777 == 0o600
    assert "secret" not in client.get("/settings").text
    assert (
        client.post(
            "/settings/cookies", data={"cookies": cookie.replace("secret", "new")}
        ).status_code
        == 200
    )
    assert config.cookie_path.with_suffix(".txt.bak").read_text() == cookie
    assert config.cookie_path.with_suffix(".txt.bak").stat().st_mode & 0o777 == 0o600
    assert client.post("/api/settings/test-cookie").status_code == 202
    assert client.post("/settings/cookies", data={"cookies": ""}).status_code == 200
    assert not config.cookie_path.exists()


def test_cookie_state_is_visible_on_all_pages(client, config):
    cookie = "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\ttoken\tsecret\n"
    for page in ("/", "/settings", "/logs"):
        assert 'class="cookie-state missing"' in client.get(page).text
    assert "No cookie file saved" in client.get("/settings").text
    config.cookie_path.write_text(cookie)
    for page in ("/", "/settings", "/logs"):
        assert 'class="cookie-state ok"' in client.get(page).text
    settings = client.get("/settings").text
    assert "Cookie file saved" in settings
    assert "secret" not in settings


def test_retry_rejects_done_and_exposes_error(client, info):
    db = client.app.state.db
    ingest.upsert_channel(db, "probe", "Probe", "https://nebula.tv/probe")
    ingest.store_video(db, info, "probe", info["url"], selected=True, full=True)
    db.update_video(info["id"], status="error", error="Detailed source error\nNext line")
    assert (
        client.get("/api/downloads").json()["videos"][0]["error"]
        == "Detailed source error\nNext line"
    )
    assert client.post(f"/api/videos/{info['id']}/retry").status_code == 200
    assert db.video(info["id"])["error"] is None
    db.update_video(info["id"], status="done")
    assert client.post(f"/api/videos/{info['id']}/retry").status_code == 409
    assert client.post("/api/videos/missing/retry").status_code == 404
    assert client.get("/api/downloads?offset=-1").status_code == 422
    assert client.get("/api/downloads?limit=501").status_code == 422
    assert client.get("/api/downloads?status=available").status_code == 400


def test_catalog_selection_subscription_and_manual_check(client, info):
    db = client.app.state.db
    ingest.upsert_channel(db, "probe", "Probe", "https://nebula.tv/probe")
    ingest.store_video(db, info, "probe", info["url"], selected=False, full=True)
    assert client.get("/channels/probe").status_code == 200
    assert client.get("/channels/missing").status_code == 404
    assert client.post("/channels/probe/select", data={"videos": info["id"]}).status_code == 200
    assert db.video(info["id"])["status"] == "queued"
    # Subscribing an uninitialized channel must first establish a future-only baseline.
    response = client.post("/api/channels/probe/subscription", data={"enabled": "true"})
    assert response.status_code == 200
    assert not db.channel("probe")["subscribed"]
    db.execute("UPDATE channels SET initialized=1")
    assert (
        client.post("/api/channels/probe/subscription", data={"enabled": "true"}).status_code == 200
    )
    assert db.channel("probe")["subscribed"]
    assert client.post("/api/check").json()["jobs"]
    assert client.post("/api/channels/probe/check").status_code == 200


def test_cookie_job_failure_and_retry(client, config):
    job_id = client.post("/api/settings/test-cookie").json()["job_id"]
    Worker(config, client.app.state.db, Event()).step()
    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "error"
    assert "No cookie file" in status["error"]
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert client.post("/api/jobs/999/retry").status_code == 404


def test_instance_lock(client, config):
    with pytest.raises(RuntimeError, match="Only one"):
        with TestClient(create_app(config, start_workers=False)):
            pass


def test_logs_and_versions(client):
    assert client.get("/api/logs").status_code == 200
    status = client.get("/api/status").json()
    assert {row["component"] for row in status["updates"]} == {"application", "yt-dlp"}
    assert all(row["status"] == "unknown" for row in status["updates"])
    assert "Application started" in client.get("/api/logs").text


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://untrusted.example"},
        {"Origin": "null"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_posts_cannot_delete_cookies(client, config, headers):
    cookie = "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\ttoken\tsecret\n"
    config.cookie_path.write_text(cookie)
    response = client.post("/settings/cookies", headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert config.cookie_path.read_text() == cookie


def test_same_origin_post(client):
    assert (
        client.post(
            "/api/settings/test-cookie", headers={"Origin": "http://testserver"}
        ).status_code
        == 202
    )


def test_catalog_order_uses_source_dates(client, config, monkeypatch):
    entries = [
        {
            "id": identity,
            "title": identity,
            "timestamp": timestamp,
            "url": f"https://nebula.tv/videos/{identity}",
        }
        for identity, timestamp in [("newest", 300), ("middle", 200), ("oldest", 100)]
    ]
    monkeypatch.setattr(
        "app.ytdlp.fetch_info", lambda *a, **k: {"_type": "playlist", "entries": entries}
    )
    db = client.app.state.db
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe", "mode": "future"})
    page = client.get("/channels/probe").text
    assert (
        page.index('value="newest"') < page.index('value="middle"') < page.index('value="oldest"')
    )
    # Discovering an older video later must not move it above newer publications.
    entries.append({**entries[-1], "id": "backfill", "timestamp": 50})
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe"}, checking=True)
    page = client.get("/channels/probe").text
    assert page.index('value="oldest"') < page.index('value="backfill"')


def test_job_history_pagination_keeps_old_failures_accessible(client):
    db = client.app.state.db
    failed = db.enqueue("video", {"url": "https://nebula.tv/videos/failed"})
    db.execute("UPDATE jobs SET status='error',error='Import failed' WHERE id=?", (failed,))
    for i in range(50):
        db.enqueue("video", {"url": f"https://nebula.tv/videos/later-{i}"})
    assert len(client.get("/api/jobs").json()["jobs"]) == 50
    older = client.get("/api/jobs?offset=50").json()["jobs"]
    assert [job["id"] for job in older] == [failed]
    assert older[0]["error"] == "Import failed"
    assert client.post(f"/api/jobs/{failed}/retry").status_code == 200
    assert client.get("/api/jobs?offset=-1").status_code == 422


def add_downloads(db, statuses):
    ingest.upsert_channel(db, "probe", "Probe", "https://nebula.tv/probe")
    ingest.upsert_channel(db, "other", "Other", "https://nebula.tv/other")
    for index, (channel, status) in enumerate(statuses):
        identity = f"{channel}-{status}-{index}"
        ingest.store_video(
            db,
            {"id": identity, "title": f"Title {identity}"},
            channel,
            f"https://nebula.tv/videos/{identity}",
            selected=False,
        )
        db.update_video(identity, status=status, error="Failed" if status == "error" else None)


def test_download_filters_cover_all_rows(client):
    db = client.app.state.db
    add_downloads(db, [("probe", "error")] * 120 + [("other", "error"), ("probe", "done")])
    add_downloads(db, [("probe", "available")])
    data = client.get("/api/downloads?status=error&limit=100").json()
    assert data["total"] == 121
    assert len(data["videos"]) == 100
    assert data["counts"] == {"error": 121, "done": 1}
    assert client.get("/api/downloads?channel=other").json()["total"] == 1
    assert client.get("/api/downloads?q=probe-done").json()["total"] == 1
    # LIKE wildcards are matched literally.
    assert client.get("/api/downloads?q=%25").json()["total"] == 0
    assert client.get("/api/downloads?status=active").json()["total"] == 0


def test_bulk_actions_respect_channel_and_search(client):
    db = client.app.state.db
    add_downloads(
        db, [("probe", "error"), ("probe", "error"), ("other", "error"), ("probe", "done")]
    )
    response = client.post("/api/downloads/retry-errors", data={"channel": "probe"})
    assert response.json() == {"count": 2}
    assert db.video("probe-error-0")["status"] == "queued"
    assert db.video("probe-error-0")["error"] is None
    assert db.video("other-error-2")["status"] == "error"
    response = client.post("/api/downloads/remove-errors", data={"q": "other"})
    assert response.json() == {"count": 1}
    assert db.video("other-error-2")["status"] == "available"
    assert client.post("/api/downloads/remove-done").json() == {"count": 1}
    finished = db.video("probe-done-3")
    assert (finished["status"], finished["dismissed"]) == ("done", 1)
    assert client.get("/api/downloads?status=done").json()["total"] == 0
    assert client.post("/api/downloads/delete-everything").status_code == 404


def test_remove_single_video(client):
    db = client.app.state.db
    add_downloads(db, [("probe", "error"), ("probe", "done"), ("probe", "queued")])
    assert client.post("/api/videos/probe-error-0/remove").status_code == 200
    assert db.video("probe-error-0")["status"] == "available"
    assert client.post("/api/videos/probe-done-1/remove").status_code == 200
    assert db.video("probe-done-1")["dismissed"] == 1
    assert client.post("/api/videos/probe-queued-2/remove").status_code == 409
    assert client.post("/api/videos/missing/remove").status_code == 404


def test_pause_and_resume(client):
    assert client.get("/api/status").json()["paused"] is None
    assert 'id="pause-banner" class="banner" hidden' in client.get("/").text
    assert client.post("/api/queue/pause").status_code == 200
    assert client.get("/api/downloads").json()["paused"]["reason"] == "Paused manually"
    assert "Paused manually" in client.get("/channels").text
    assert client.post("/api/queue/resume").status_code == 200
    assert client.get("/api/status").json()["paused"] is None


def test_clear_and_prune_jobs(client):
    db = client.app.state.db
    done = db.enqueue("video", {"url": "https://nebula.tv/videos/done"})
    old = db.enqueue("video", {"url": "https://nebula.tv/videos/old"})
    failed = db.enqueue("video", {"url": "https://nebula.tv/videos/failed"})
    db.execute("UPDATE jobs SET status='done' WHERE id IN (?,?)", (done, old))
    db.execute("UPDATE jobs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old,))
    db.execute("UPDATE jobs SET status='error' WHERE id=?", (failed,))
    db.prune_jobs()
    assert [job["id"] for job in client.get("/api/jobs").json()["jobs"]] == [failed, done]
    assert client.post("/api/jobs/clear", data={"status": "running"}).status_code == 400
    assert client.post("/api/jobs/clear", data={"status": "done"}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs").json()["jobs"]] == [failed]
    assert client.post("/api/jobs/clear", data={"status": "error"}).status_code == 200
    assert client.get("/api/jobs").json()["jobs"] == []


def test_channel_pages_filter_catalog(client):
    db = client.app.state.db
    add_downloads(db, [("probe", "available"), ("probe", "done"), ("probe", "error")])
    page = client.get("/channels/probe?status=available").text
    assert 'value="probe-available-0"' in page
    assert 'value="probe-done-1"' not in page
    page = client.get("/channels/probe?q=error").text
    assert 'value="probe-error-2"' in page
    assert 'value="probe-available-0"' not in page
    assert client.get("/channels/probe?status=nonsense").status_code == 400
    overview = client.get("/channels").text
    assert "1 of 3 downloaded" in overview
    assert "1 failed" in overview
