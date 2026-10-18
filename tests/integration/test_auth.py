from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.main import save_cookies
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


def test_concurrent_cookie_imports_preserve_complete_files_and_backup(config):
    contents = [
        f"# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\ttoken\tvalue-{i}\n"
        for i in range(16)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda content: save_cookies(config, content), contents))
    backup = config.cookie_path.with_suffix(".txt.bak")
    assert config.cookie_path.read_text() in contents
    assert backup.read_text() in contents
    assert backup.read_text() != config.cookie_path.read_text()
    assert config.cookie_path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
