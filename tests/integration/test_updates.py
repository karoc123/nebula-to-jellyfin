from datetime import UTC, datetime, timedelta

from app import updates


def test_update_versions_cache_and_failure(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.updates.installed", lambda: {"application": "0.1.0", "yt-dlp": "2026.8.19"}
    )

    def release(repo):
        calls.append(repo)
        return "0.2.0" if repo.startswith("karoc") else "2026.8.19"

    monkeypatch.setattr("app.updates.fetch_release", release)
    stamp = datetime(2026, 10, 18, tzinfo=UTC)
    updates.check(db, stamp)
    assert (
        db.one("SELECT status FROM update_cache WHERE component='application'")["status"]
        == "available"
    )
    assert db.one("SELECT status FROM update_cache WHERE component='yt-dlp'")["status"] == "current"
    updates.check(db, stamp + timedelta(hours=23))
    assert len(calls) == 2
    monkeypatch.setattr(
        "app.updates.fetch_release", lambda repo: (_ for _ in ()).throw(OSError("offline"))
    )
    updates.check(db, stamp + timedelta(days=1))
    rows = db.rows("SELECT * FROM update_cache")
    assert all(row["status"] == "unknown" for row in rows)
    assert all(row["error"] == "offline" for row in rows)
    assert all(row["succeeded_at"] == stamp.isoformat() for row in rows)
    updates.check(db, stamp + timedelta(days=1, hours=23))
    assert len(calls) == 2


def test_update_attempt_survives_interruption(db, monkeypatch):
    import pytest

    monkeypatch.setattr("app.updates.installed", lambda: {"application": "0.1.0"})

    def interrupted(repo):
        raise SystemExit("crash during request")

    monkeypatch.setattr("app.updates.fetch_release", interrupted)
    stamp = datetime(2026, 10, 18, tzinfo=UTC)
    with pytest.raises(SystemExit):
        updates.check(db, stamp)
    # A repeated request would raise again; the persisted attempt suppresses it.
    updates.check(db, stamp + timedelta(hours=1))
    assert db.one("SELECT status FROM update_cache")["status"] == "unknown"
