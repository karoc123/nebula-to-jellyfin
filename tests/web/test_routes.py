from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import ingest
from app.main import create_app
from app.worker import Worker


@pytest.mark.parametrize("page", ["/", "/settings", "/logs", "/api/status", "/api/health"])
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
    status = client.get("/api/status").json()
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["status"] == "queued"
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
    assert client.get("/api/status").json()["jobs"] == []


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
        client.get("/api/status").json()["videos"][0]["error"] == "Detailed source error\nNext line"
    )
    assert client.post(f"/api/videos/{info['id']}/retry").status_code == 200
    assert db.video(info["id"])["error"] is None
    db.update_video(info["id"], status="done")
    assert client.post(f"/api/videos/{info['id']}/retry").status_code == 409
    assert client.post("/api/videos/missing/retry").status_code == 404
    assert client.get("/api/status?offset=-1").status_code == 422
    assert client.get("/api/status?limit=501").status_code == 422


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
    for i in range(30):
        db.enqueue("video", {"url": f"https://nebula.tv/videos/later-{i}"})
    assert len(client.get("/api/status").json()["jobs"]) == 30
    older = client.get("/api/status?job_offset=30").json()["jobs"]
    assert [job["id"] for job in older] == [failed]
    assert older[0]["error"] == "Import failed"
    assert client.post(f"/api/jobs/{failed}/retry").status_code == 200
    assert client.get("/api/status?job_offset=-1").status_code == 422
