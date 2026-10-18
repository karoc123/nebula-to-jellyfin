import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db import Database


def test_upgrade_preserves_records(config, tmp_path):
    migrations = Path(__file__).parents[2] / "app/migrations"
    previous = tmp_path / "previous"
    previous.mkdir()
    shutil.copyfile(migrations / "001_initial.sql", previous / "001_initial.sql")
    db = Database(config.db_path)
    db.migrate(previous)
    db.set_setting("preserved", {"value": "before upgrade"})
    db.execute("INSERT INTO channels(id,name,url,directory) VALUES('probe','Probe','url','Probe')")
    db.execute(
        "INSERT INTO videos(id,channel_id,url,title,status,path_stem,target_path,"
        "created_at,updated_at) "
        "VALUES('id','probe','url','Title','done','stable','stable.mp4','time','time')"
    )
    db.migrate()
    db.migrate()
    assert db.setting("preserved") == {"value": "before upgrade"}
    assert db.video("id")["target_path"] == "stable.mp4"
    assert db.video("id")["status"] == "done"
    assert db.one("PRAGMA user_version")["user_version"] == 2
    assert db.rows("SELECT * FROM update_cache") == []


def test_migration_rollback(config, tmp_path):
    broken = tmp_path / "migrations"
    broken.mkdir()
    (broken / "001_bad.sql").write_text("CREATE TABLE partial(id TEXT); INVALID SQL;")
    db = Database(config.db_path)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(broken)
    assert db.rows("SELECT name FROM sqlite_master WHERE type='table'") == []


def test_refuse_poc_and_downgrade(config):
    db = Database(config.db_path)
    db.execute("CREATE TABLE old_data (value TEXT)")
    with pytest.raises(RuntimeError, match="Unversioned"):
        db.migrate()
    assert db.rows("SELECT * FROM old_data") == []
    db.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate()


def test_job_deduplication_and_recovery(db, video):
    first = db.enqueue("video", {"url": "url"})
    assert db.enqueue("video", {"url": "url"}) == first
    db.execute("UPDATE jobs SET status='running'")
    db.update_video(video["id"], status="downloading")
    db.recover()
    assert db.one("SELECT status FROM jobs")["status"] == "queued"
    assert db.video(video["id"])["status"] == "queued"
    db.update_video(video["id"], status="error", error="retain me")
    db.recover()
    assert db.video(video["id"])["error"] == "retain me"
    assert db.video(video["id"])["status"] == "error"
