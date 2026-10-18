from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor


@pytest.mark.parametrize("missing", [False, True])
def test_fragment_failure_cannot_publish_an_incomplete_video(
    config, db, worker, video, monkeypatch, missing
):
    requested = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            content = b"media fragment"
            self.send_response(404 if missing and self.path == "/2" else 200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    class FragmentIE(InfoExtractor):
        _VALID_URL = r"https://nebula\.tv/videos/(?P<id>[\w-]+)"

        def _real_extract(self, url):
            return {
                "id": video["id"],
                "title": video["title"],
                "url": base,
                "protocol": "http_dash_segments",
                "ext": "mp4",
                "height": 180,
                "vcodec": "avc1",
                "acodec": "aac",
                "fragments": [{"url": f"{base}/{i}"} for i in range(1, 4)],
            }

    def downloader(options):
        result = YoutubeDL(options, auto_init=False)
        result.add_info_extractor(FragmentIE())
        return result

    monkeypatch.setattr("app.ytdlp.YoutubeDL", downloader)
    db.set_setting("options", {"fragment_retries": 0, "retries": 0, "writesubtitles": False})
    try:
        worker.step()
        assert "/2" in requested
        record = db.video(video["id"])
        if missing:
            assert record["status"] == "error"
            assert not list(config.library_dir.rglob("*.mp4"))
            assert not (config.work_dir / video["id"] / "completed.json").exists()
            missing = False
            assert db.retry(video["id"])
            worker.step()
            record = db.video(video["id"])
        assert record["status"] == "done"
        assert (config.library_dir / record["target_path"]).read_bytes() == b"media fragment" * 3
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
