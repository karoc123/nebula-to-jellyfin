from pathlib import Path

from app.ytdlp import fetch_info


def test_extraction_cannot_overwrite_newly_imported_cookies(config, monkeypatch):
    config.cookie_path.write_text("original cookies")
    snapshots = []

    class Source:
        def __init__(self, options):
            self.cookie = Path(options["cookiefile"])
            snapshots.append(self.cookie)

        def __enter__(self):
            assert self.cookie.read_text() == "original cookies"
            assert self.cookie.stat().st_mode & 0o777 == 0o600
            return self

        def extract_info(self, url, download):
            # Simulate a settings import while the source operation is in flight.
            config.cookie_path.write_text("newly imported cookies")
            return {"id": "video"}

        def __exit__(self, *args):
            self.cookie.write_text("cookie jar saved by yt-dlp")

    monkeypatch.setattr("app.ytdlp.YoutubeDL", Source)
    assert fetch_info(config, "https://nebula.tv/videos/probe")["id"] == "video"
    assert config.cookie_path.read_text() == "newly imported cookies"
    assert not snapshots[0].exists()
