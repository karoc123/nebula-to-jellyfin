import xml.etree.ElementTree as ET
from threading import Event
from urllib.error import URLError

import pytest

from app import library
from app.worker import Worker


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
    monkeypatch.setattr(
        "app.library.write_nfo",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("Storage write denied")),
    )
    worker.step()
    assert db.video(video["id"])["status"] == "error"
    assert "Storage write denied" in db.video(video["id"])["error"]


def test_conflicting_target_is_not_replaced(config, db, worker, video, download, monkeypatch):
    original = library.publish

    def conflict(cfg, record, channel, work):
        target = cfg.library_dir / record["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"existing file with a different size")
        return original(cfg, record, channel, work)

    monkeypatch.setattr("app.library.publish", conflict)
    worker.step()
    record = db.video(video["id"])
    assert record["status"] == "error"
    assert (
        config.library_dir / record["target_path"]
    ).read_bytes() == b"existing file with a different size"


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
