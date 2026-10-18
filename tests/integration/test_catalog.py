from datetime import UTC, datetime

import pytest

from app import ingest, scheduler


def catalog(monkeypatch, entries):
    payload = {"_type": "playlist", "title": "Channel", "entries": entries}
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *args, **kwargs: payload)
    return payload


def entry(identity):
    return {
        "id": identity,
        "title": identity,
        "url": f"https://nebula.tv/videos/{identity}#smuggled",
    }


@pytest.mark.parametrize("mode,count", [("future", 0), ("latest", 2), ("all", 3)])
def test_selection_baseline_and_dedup(config, db, monkeypatch, mode, count):
    source = catalog(monkeypatch, [entry("newest"), entry("middle"), entry("oldest")])
    payload = {"url": "https://nebula.tv/probe", "mode": mode, "latest": 2, "subscribed": True}
    ingest.import_channel(config, db, payload)
    ingest.import_channel(config, db, payload)
    assert len(db.rows("SELECT * FROM videos")) == 3
    assert len(db.rows("SELECT * FROM videos WHERE status='queued'")) == count
    assert db.channel("probe")["initialized"]
    assert db.channel("probe")["subscribed"]
    source["entries"].insert(0, entry("brand-new"))
    ingest.import_channel(config, db, {"url": payload["url"]}, checking=True)
    assert db.video("brand-new")["status"] == "queued"
    db.update_video("brand-new", status="error", error="keep")
    ingest.import_channel(config, db, payload)
    assert db.video("brand-new")["status"] == "error"
    assert db.video("brand-new")["error"] == "keep"


def test_baseline_transaction_rolls_back(config, db, monkeypatch):
    catalog(monkeypatch, [entry("valid"), {"id": "broken", "url": "https://evil.example/video"}])
    with pytest.raises(ValueError):
        ingest.import_channel(
            config, db, {"url": "https://nebula.tv/probe", "mode": "future", "subscribed": True}
        )
    assert db.rows("SELECT * FROM videos") == []
    assert not db.channel("probe")["initialized"]
    assert not db.channel("probe")["subscribed"]


def test_older_selection_is_atomic_and_failed_stays_failed(config, db, monkeypatch):
    catalog(monkeypatch, [entry("old"), entry("failed")])
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe", "mode": "future"})
    db.update_video("failed", status="error")
    with pytest.raises(ValueError):
        db.select_videos("probe", ["old", "unknown"])
    assert db.video("old")["status"] == "available"
    db.select_videos("probe", ["old", "failed"])
    assert db.video("old")["status"] == "queued"
    assert db.video("failed")["status"] == "error"


def test_single_import_preserves_done_paths(config, db, monkeypatch, info):
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *args, **kwargs: info)
    ingest.import_video(config, db, info["url"])
    db.update_video(
        info["id"], status="done", path_stem="stable/path", target_path="stable/path.mp4"
    )
    info["title"] = "New title"
    ingest.import_video(config, db, info["url"])
    assert db.video(info["id"])["status"] == "done"
    assert db.video(info["id"])["path_stem"] == "stable/path"


def test_scheduler_catches_up_once_and_deduplicates(db, video):
    db.execute("UPDATE channels SET subscribed=1,initialized=1")
    current = datetime(2026, 10, 18, 12, tzinfo=UTC)
    scheduler.tick(db, "Europe/Berlin", current)
    scheduler.tick(db, "Europe/Berlin", current)
    assert len(db.rows("SELECT * FROM jobs")) == 1
    db.execute("UPDATE jobs SET status='done'")
    scheduler.tick(db, "Europe/Berlin", current)
    assert len(db.rows("SELECT * FROM jobs")) == 1
    scheduler.tick(db, "Europe/Berlin", datetime(2026, 10, 19, 12, tzinfo=UTC))
    assert len(db.rows("SELECT * FROM jobs")) == 2


def test_channel_failure_isolated(config, db, worker, monkeypatch):
    def fetch(config, url, **kwargs):
        if url.endswith("/bad"):
            raise RuntimeError("Network unavailable")
        return {"_type": "playlist", "entries": [], "title": "Good"}

    monkeypatch.setattr("app.ytdlp.fetch_info", fetch)
    db.enqueue("channel", {"url": "https://nebula.tv/bad", "mode": "future"})
    db.enqueue("channel", {"url": "https://nebula.tv/good", "mode": "future"})
    worker.step()
    worker.step()
    assert [row["status"] for row in db.rows("SELECT * FROM jobs ORDER BY id")] == ["error", "done"]


def test_channel_artwork_is_stored_and_written(config, db, monkeypatch):
    catalog(monkeypatch, [])
    monkeypatch.setattr(
        "app.nebula.channel_artwork",
        lambda slug: ("https://example.org/avatar.jpg", "https://example.org/banner.jpg"),
    )

    def save(url, dest):
        dest.write_bytes(url.encode())
        return True

    monkeypatch.setattr("app.library.fetch_image", save)
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe", "mode": "future"})
    channel = db.channel("probe")
    assert channel["image_url"] == "https://example.org/avatar.jpg"
    assert channel["banner_url"] == "https://example.org/banner.jpg"
    show = config.library_dir / channel["directory"]
    assert (show / "poster.jpg").read_bytes() == b"https://example.org/avatar.jpg"
    assert (show / "fanart.jpg").read_bytes() == b"https://example.org/banner.jpg"


def test_single_import_fills_missing_channel_artwork(config, db, monkeypatch, info):
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *args, **kwargs: info)
    monkeypatch.setattr(
        "app.nebula.channel_artwork", lambda slug: ("https://example.org/avatar.jpg", None)
    )
    ingest.import_video(config, db, info["url"])
    assert db.channel("probe")["image_url"] == "https://example.org/avatar.jpg"
    assert db.channel("probe")["banner_url"] is None


def test_channel_rename_keeps_directory(config, db, monkeypatch):
    source = catalog(monkeypatch, [entry("video")])
    payload = {"url": "https://nebula.tv/probe", "mode": "future"}
    ingest.import_channel(config, db, payload)
    directory = db.channel("probe")["directory"]
    source["title"] = "Renamed channel"
    ingest.import_channel(config, db, payload)
    assert db.channel("probe")["name"] == "Renamed channel"
    assert db.channel("probe")["directory"] == directory


def test_single_import_preserves_channel_description(config, db, monkeypatch, info):
    source = catalog(monkeypatch, [])
    source["description"] = "Channel description"
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe", "mode": "future"})
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *args, **kwargs: info)
    ingest.import_video(config, db, info["url"])
    assert db.channel("probe")["description"] == "Channel description"
    # An explicit catalog edit can still clear the description.
    source["description"] = ""
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *args, **kwargs: source)
    ingest.import_channel(config, db, {"url": "https://nebula.tv/probe", "mode": "future"})
    assert db.channel("probe")["description"] == ""


def test_catalog_refresh_fills_dates_without_changing_downloaded_metadata(config, db, monkeypatch):
    source = catalog(monkeypatch, [entry("video")])
    payload = {"url": "https://nebula.tv/probe", "mode": "future"}
    ingest.import_channel(config, db, payload)
    assert db.video("video")["published_at"] is None
    source["entries"][0]["timestamp"] = 1_700_000_000
    ingest.import_channel(config, db, payload)
    published = db.video("video")["published_at"]
    assert published == ingest.publication(source["entries"][0])
    source["entries"][0]["timestamp"] += 3600
    ingest.import_channel(config, db, payload)
    assert db.video("video")["published_at"] == published
