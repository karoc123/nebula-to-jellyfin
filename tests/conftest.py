from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.ingest import store_video, upsert_channel
from app.main import create_app
from app.worker import Worker


@pytest.fixture
def config(tmp_path):
    cfg = Config(tmp_path / "state", tmp_path / "share/library", tmp_path / "share/work")
    cfg.prepare()
    return cfg


@pytest.fixture
def db(config):
    database = Database(config.db_path)
    database.migrate()
    return database


@pytest.fixture
def info():
    return {
        "id": "video-123",
        "title": "A title. With dots & XML",
        "description": "A <description>",
        "channel_id": "probe",
        "channel": "Probe",
        "upload_date": "20261014",
        "duration": 120,
        "url": "https://nebula.tv/videos/probe-example",
    }


@pytest.fixture
def video(db, info):
    upsert_channel(db, "probe", "Probe", "https://nebula.tv/probe")
    store_video(db, info, "probe", info["url"], selected=True, full=True)
    return db.video(info["id"])


@pytest.fixture
def worker(config, db):
    return Worker(config, db, Event())


@pytest.fixture
def client(config):
    with TestClient(create_app(config, start_workers=False)) as client:
        yield client


@pytest.fixture
def download(monkeypatch):
    calls = []

    def fake(config, video, work, options, progress, stop):
        calls.append(options)
        (work / "video.mp4").write_bytes(b"completed media")
        (work / "video.en.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nExample\n")
        (work / "video.info.json").write_text('{"id":"video-123"}')
        progress(90)

    monkeypatch.setattr("app.ytdlp.download", fake)
    return calls
