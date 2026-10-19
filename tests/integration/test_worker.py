import errno
import json
import shutil
import xml.etree.ElementTree as ET
from threading import Event
from urllib.error import URLError

import pytest

from app import library
from app.worker import Worker


def test_cookie_test_requires_active_subscription(config, db, worker, monkeypatch):
    config.cookie_path.write_text(
        "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\tnebula_auth.apiToken\tsecret\n"
    )
    monkeypatch.setattr(
        "app.nebula.authorization_claims",
        lambda token: {"is_anonymous": False, "is_subscribed": False},
    )
    job_id = db.enqueue("cookie_test", {})
    worker.step()
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "error"
    assert "no active subscription" in job["error"]


def test_cookie_test_accepts_claims_without_subscription_flags(config, db, worker, monkeypatch):
    config.cookie_path.write_text(
        "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\tnebula_auth.apiToken\tsecret\n"
    )
    monkeypatch.setattr("app.nebula.authorization_claims", lambda token: {})
    monkeypatch.setattr(
        "app.ytdlp.fetch_info", lambda cfg, url: {"title": "Finale", "channel": "Jet Lag"}
    )
    job_id = db.enqueue("cookie_test", {})
    worker.step()
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"


def test_cookie_test_checks_subscription_and_exclusive_video(config, db, worker, monkeypatch):
    config.cookie_path.write_text(
        "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\tnebula_auth.apiToken\tsecret\n"
    )
    tokens, urls = [], []
    monkeypatch.setattr(
        "app.nebula.authorization_claims",
        lambda token: tokens.append(token) or {"is_anonymous": False, "is_subscribed": True},
    )
    monkeypatch.setattr(
        "app.ytdlp.fetch_info",
        lambda cfg, url: urls.append(url) or {"title": "Finale", "channel": "Jet Lag"},
    )
    job_id = db.enqueue("cookie_test", {})
    worker.step()
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job["status"] == "done"
    assert tokens == ["secret"]
    assert urls == ["https://nebula.tv/videos/jetlag-s19e8"]
    assert json.loads(job["result"]) == {
        "subscription": "active",
        "test video": "Finale",
        "channel": "Jet Lag",
    }


def test_publish_sidecars_and_stable_paths(config, db, worker, video, download):
    worker.step()
    result = db.video(video["id"])
    assert result["status"] == "done"
    target = config.library_dir / result["target_path"]
    assert target.read_bytes() == b"completed media"
    nfo = target.with_suffix(".nfo")
    parsed = ET.parse(nfo).getroot()
    assert parsed.findtext("title") == video["title"]
    assert parsed.findtext("season") == "2026"
    assert parsed.findtext("aired") == "2026-10-14"
    assert parsed.find("episode") is None
    assert parsed.findtext("lockdata") == "true"
    show = target.parent.parent / "tvshow.nfo"
    assert ET.parse(show).getroot().findtext("lockdata") == "false"
    assert target.with_suffix(".en.srt").exists()
    assert (config.data_dir / "metadata" / f"{video['id']}.json").exists()
    assert len(download) == 1
    assert not (config.work_dir / video["id"]).exists()
    assert not worker.step()
    assert len(download) == 1


def serve_images(monkeypatch, bodies):
    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit=-1):
            return self.body

    def urlopen(request, timeout=None):
        return Response(bodies[request.full_url])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)


def test_channel_artwork_replaces_stale_files(config, db, worker, video, download, monkeypatch):
    db.execute(
        "UPDATE channels SET image_url=?,banner_url=? WHERE id='probe'",
        ("https://example.org/avatar.jpg", "https://example.org/banner.jpg"),
    )
    show = config.library_dir / db.channel("probe")["directory"]
    show.mkdir(parents=True)
    (show / "poster.jpg").write_bytes(b"episode thumbnail")
    (show / "fanart.jpg").write_bytes(b"episode thumbnail")
    serve_images(
        monkeypatch,
        {
            "https://example.org/avatar.jpg": b"channel avatar",
            "https://example.org/banner.jpg": b"channel banner",
        },
    )
    worker.step()
    assert (show / "poster.jpg").read_bytes() == b"channel avatar"
    assert (show / "fanart.jpg").read_bytes() == b"channel banner"


def test_episode_thumbnail_is_not_show_poster(
    config, db, worker, video, download, monkeypatch, caplog
):
    db.update_video(video["id"], thumb_url="https://example.org/episode.jpg")
    serve_images(monkeypatch, {"https://example.org/episode.jpg": b"episode image"})
    worker.step()
    show = config.library_dir / db.channel("probe")["directory"]
    assert not (show / "poster.jpg").exists()
    assert not (show / "fanart.jpg").exists()
    assert "Artwork absent at source: poster.jpg" in caplog.text
    target = config.library_dir / db.video(video["id"])["target_path"]
    assert target.with_name(target.stem + "-thumb.jpg").read_bytes() == b"episode image"


@pytest.mark.parametrize("failure", ["after_receipt", "after_move", "before_done"])
def test_interrupted_completion_never_downloads_again(
    config, db, worker, video, download, monkeypatch, failure
):
    original = db.update_video
    publish = library.publish

    class Crash(BaseException):
        pass

    def update(identity, **fields):
        if (failure == "after_receipt" and fields.get("status") == "publishing") or (
            failure == "before_done" and fields.get("status") == "done"
        ):
            raise Crash()
        original(identity, **fields)

    def publish_crash(*args):
        target = publish(*args)
        if failure == "after_move":
            raise Crash()
        return target

    monkeypatch.setattr(db, "update_video", update)
    monkeypatch.setattr("app.library.publish", publish_crash)
    with pytest.raises(Crash):
        worker.step()
    monkeypatch.setattr(db, "update_video", original)
    monkeypatch.setattr("app.library.publish", publish)
    db.recover()
    restarted = Worker(config, db, Event())
    restarted.step()
    assert db.video(video["id"])["status"] == "done"
    assert len(download) == 1
    assert len(list(config.library_dir.rglob("*.mp4"))) == 1
    assert list(config.library_dir.rglob("*.nfo"))


def test_partial_files_survive_and_retry_uses_current_options(
    config, db, worker, video, monkeypatch
):
    attempts = []

    def fail(config, video, work, options, progress, stop):
        attempts.append(options)
        partial = work / "video.mp4.part"
        if len(attempts) == 1:
            partial.write_bytes(b"partial")
            raise RuntimeError("Network failed")
        assert partial.read_bytes() == b"partial"
        (work / "video.mp4").write_bytes(b"completed media")

    monkeypatch.setattr("app.ytdlp.download", fail)
    worker.step()
    assert db.video(video["id"])["status"] == "error"
    assert not worker.step()
    db.recover()
    assert db.video(video["id"])["status"] == "error"
    db.set_setting("options", {"ratelimit": 1234})
    assert db.retry(video["id"])
    worker.step()
    assert db.video(video["id"])["status"] == "done"
    assert attempts == [{}, {"ratelimit": 1234}]


def test_metadata_failure_on_video_visible(db, worker, video, monkeypatch):
    db.update_video(video["id"], metadata_ready=0)
    monkeypatch.setattr(
        "app.ytdlp.fetch_info",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401 invalid cookies")),
    )
    worker.step()
    assert db.video(video["id"])["status"] == "error"
    assert "401" in db.video(video["id"])["error"]


def test_source_absent_is_warning_but_disk_error_is_failure(
    config, db, worker, video, download, monkeypatch, caplog
):
    db.update_video(video["id"], thumb_url="https://example.org/art.jpg")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(URLError("missing"))
    )
    worker.step()
    assert db.video(video["id"])["status"] == "done"
    assert "Optional artwork unavailable" in caplog.text
    db.update_video(video["id"], status="queued")
    # Use a fresh isolated work receipt, then fail an actual sidecar write.
    work = config.work_dir / video["id"]
    work.mkdir()
    (work / "video.mp4").write_bytes(b"completed media")
    library.prepare_receipt(work)
    # Remove the earlier publication so this attempt reaches the failing sidecar write.
    (config.library_dir / db.video(video["id"])["target_path"]).unlink()
    monkeypatch.setattr(
        "app.library.write_nfo",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("Storage write denied")),
    )
    worker.step()
    assert db.video(video["id"])["status"] == "error"
    assert "Storage write denied" in db.video(video["id"])["error"]


@pytest.mark.parametrize("content", [b"existing file with a different size", b"different media"])
def test_conflicting_target_is_not_replaced(
    config, db, worker, video, download, monkeypatch, content
):
    original = library.publish

    def conflict(cfg, record, channel, work):
        target = cfg.library_dir / record["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.with_suffix(".nfo").write_text("Existing metadata")
        return original(cfg, record, channel, work)

    monkeypatch.setattr("app.library.publish", conflict)
    worker.step()
    record = db.video(video["id"])
    assert record["status"] == "error"
    target = config.library_dir / record["target_path"]
    assert target.read_bytes() == content
    assert target.with_suffix(".nfo").read_text() == "Existing metadata"
    assert (config.work_dir / video["id"] / "video.mp4").read_bytes() == b"completed media"


def test_missing_sidecars_are_warnings(config, db, worker, video, monkeypatch, caplog):
    def minimal(cfg, record, work, *args):
        (work / "video.mkv").write_bytes(b"video")

    monkeypatch.setattr("app.ytdlp.download", minimal)
    worker.step()
    assert db.video(video["id"])["status"] == "done"
    assert "No optional subtitles" in caplog.text
    assert "Artwork absent at source" in caplog.text


def test_new_metadata_keeps_persisted_filename(
    config, db, worker, video, download, monkeypatch, info
):
    db.update_video(video["id"], path_stem="Probe [probe]/Season 2026/stable", metadata_ready=0)
    info["title"] = "Changed title"
    monkeypatch.setattr("app.ytdlp.fetch_info", lambda *a, **k: info)
    worker.step()
    assert db.video(video["id"])["target_path"].endswith("/stable.mp4")


def test_stop_leaves_work_recoverable(config, db, worker, video, monkeypatch):
    def stop(cfg, record, work, *args):
        (work / "video.mp4.part").write_bytes(b"partial")
        worker.stop.set()
        raise RuntimeError("Stopped")

    monkeypatch.setattr("app.ytdlp.download", stop)
    worker.step()
    assert db.video(video["id"])["status"] == "downloading"
    db.recover()
    assert db.video(video["id"])["status"] == "queued"
    assert (config.work_dir / video["id"] / "video.mp4.part").read_bytes() == b"partial"


def test_storage_error_pauses_queue_without_failing_videos(
    config, db, worker, video, info, monkeypatch
):
    from app.ingest import store_video

    store_video(db, {**info, "id": "video-456"}, "probe", info["url"], selected=True, full=True)
    attempts = []

    def unreachable(cfg, record, work, *args):
        attempts.append(record["id"])
        (work / "video.mp4.part").write_bytes(b"partial")
        try:
            raise OSError(errno.EHOSTDOWN, "Host is down")
        except OSError as exc:
            # yt-dlp reports file errors through its own exception types.
            raise RuntimeError("Unable to download video: [Errno 112] Host is down") from exc

    monkeypatch.setattr("app.ytdlp.download", unreachable)
    worker.step()
    assert db.video(video["id"])["status"] == "queued"
    assert db.video(video["id"])["error"] is None
    assert "Host is down" in db.setting("paused")["reason"]
    # Paused: further videos wait, but background jobs still run.
    job_id = db.enqueue("cookie_test", {})
    assert worker.step()
    assert db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "error"
    assert not worker.step()
    assert attempts == [video["id"]]
    assert db.video("video-456")["status"] == "queued"
    assert (config.work_dir / video["id"] / "video.mp4.part").read_bytes() == b"partial"
    db.clear_setting("paused")
    worker.step()
    assert len(attempts) == 2


def test_missing_library_pauses_before_download(config, db, worker, video, download):
    shutil.rmtree(config.library_dir)
    worker.step()
    assert "Library directory is not accessible" in db.setting("paused")["reason"]
    assert db.video(video["id"])["status"] == "queued"
    assert download == []


def test_network_error_still_fails_video(db, worker, video, monkeypatch):
    def unreachable(*args):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr("app.ytdlp.download", unreachable)
    worker.step()
    assert db.video(video["id"])["status"] == "error"
    assert db.setting("paused") is None
