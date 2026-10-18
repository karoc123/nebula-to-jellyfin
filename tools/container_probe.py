"""Verify an image with isolated state and media; never uses the deployment project."""

import argparse
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="nebula-rebuild-verification:local")
    args = parser.parse_args()
    name = "nebula-container-probe-" + uuid4().hex[:10]
    root = Path(tempfile.mkdtemp(prefix="nebula-container-"))
    (root / "state").mkdir()
    (root / "media").mkdir()
    # The image's default UID is 1000, independent of the probe operator's UID.
    (root / "state").chmod(0o777)
    (root / "media").chmod(0o777)
    root.chmod(0o755)

    def base():
        port = run("docker", "port", name, "8000/tcp").rsplit(":", 1)[1]
        return f"http://127.0.0.1:{port}"

    def api(path, method="GET"):
        request = urllib.request.Request(
            base() + path, method=method, data=b"" if method == "POST" else None
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read()

    def ready():
        for _ in range(60):
            try:
                if json.loads(api("/api/health"))["ok"]:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("Container did not become healthy")

    try:
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::8000",
            "-v",
            f"{root}/state:/data",
            "-v",
            f"{root}/media:/media",
            args.image,
        )
        ready()
        for path in ("/", "/settings", "/logs", "/static/app.js", "/static/style.css"):
            assert api(path)
        job = json.loads(api("/api/settings/test-cookie", "POST"))["job_id"]
        for _ in range(20):
            status = json.loads(api(f"/api/jobs/{job}"))
            if status["status"] == "error":
                break
            time.sleep(1)
        assert status["status"] == "error", status
        assert "No cookie file" in status["error"]
        run("docker", "restart", name)
        ready()
        assert json.loads(api(f"/api/jobs/{job}"))["status"] == "error"
        run("docker", "exec", name, "ffmpeg", "-version")
        result = {
            "health": json.loads(api("/api/health")),
            "pages_and_assets": "passed",
            "background_worker": "passed",
            "restart_persistence": "passed",
            "ffmpeg": "passed",
        }
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        print(f"Probe artifacts: {root}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
