"""Run an isolated Jellyfin acceptance probe. Requires Docker; touches no user data."""

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from app import library
from app.config import Config

IMAGE = "jellyfin/jellyfin:10.11.10"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    root = Path(tempfile.mkdtemp(prefix="nebula-jellyfin-"))
    root.chmod(0o755)
    media = root / "media"
    media.mkdir()
    name = f"nebula-probe-{uuid4().hex[:10]}"
    token = None
    base = ""

    def api(method, path, data=None, raw=False):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Emby-Token"] = token
        else:
            headers["Authorization"] = (
                'MediaBrowser Client="Nebula probe", Device="Isolated probe", '
                'DeviceId="nebula-probe", Version="0.1.0"'
            )
        request = urllib.request.Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
        return content if raw else json.loads(content) if content else None

    try:
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::8096",
            "-v",
            f"{media}:/media",
            "-v",
            f"{root}/config:/config",
            "-v",
            f"{root}/cache:/cache",
            IMAGE,
        )
        port = run("docker", "port", name, "8096/tcp").rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        for _ in range(90):
            try:
                info = api("GET", "/System/Info/Public")
                api("GET", "/Startup/Configuration")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            raise RuntimeError("Jellyfin did not start")
        api(
            "POST",
            "/Startup/Configuration",
            {
                "UICulture": "en-US",
                "MetadataCountryCode": "US",
                "PreferredMetadataLanguage": "en",
            },
        )
        api("GET", "/Startup/User")
        api("POST", "/Startup/User", {"Name": "probe", "Password": "isolated-probe-only"})
        api(
            "POST",
            "/Startup/RemoteAccess",
            {
                "EnableRemoteAccess": True,
                "EnableAutomaticPortMapping": False,
            },
        )
        api("POST", "/Startup/Complete", {})
        auth = api(
            "POST",
            "/Users/AuthenticateByName",
            {
                "Username": "probe",
                "Pw": "isolated-probe-only",
            },
        )
        token = auth["AccessToken"]
        user = auth["User"]["Id"]
        api(
            "POST",
            "/Library/VirtualFolders?name=Nebula%20probe&collectionType=tvshows",
            {
                "LibraryOptions": {
                    "PathInfos": [{"Path": "/media"}],
                    "EnableRealtimeMonitor": False,
                    "EnableInternetProviders": False,
                    "EnableAutomaticSeriesGrouping": False,
                    "MetadataSavers": [],
                    "TypeOptions": [
                        {"Type": kind, "MetadataFetchers": [], "ImageFetchers": []}
                        for kind in ["Series", "Season", "Episode"]
                    ],
                },
            },
        )
        run(
            "docker",
            "exec",
            name,
            "/usr/lib/jellyfin-ffmpeg/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "600",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "/media/sample.mp4",
        )
        run(
            "docker",
            "exec",
            name,
            "/usr/lib/jellyfin-ffmpeg/ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180",
            "-frames:v",
            "1",
            "/media/sample.jpg",
        )
        cfg = Config(root / "state", media, root / "work")
        cfg.prepare()
        channel = {
            "id": "probe",
            "name": "Probe Channel",
            "directory": "Probe Channel [probe]",
            "description": "Probe channel description",
            "image_url": (media / "sample.jpg").as_uri(),
        }

        def fixture(day, title, identity):
            work = cfg.work_dir / identity
            work.mkdir()
            shutil.copyfile(media / "sample.mp4", work / "video.mp4")
            for language in ("en", "de"):
                (work / f"video.{language}.srt").write_text(
                    "1\n00:00:00,000 --> 00:00:04,000\nProbe subtitle\n"
                )
            video = {
                "id": identity,
                "title": title,
                "description": "Probe description",
                "published_at": f"{day}T12:00:00+00:00",
                "duration": 600,
                "thumb_url": (media / "sample.jpg").as_uri(),
            }
            video["target_path"] = library.path_stem(channel, video) + ".mp4"
            library.prepare_receipt(work)
            library.publish(cfg, video, channel, work)

        fixture("2026-10-14", "First", "probe-a")
        fixture("2026-10-14", "Second", "probe-b")

        def episodes(count):
            api("POST", "/Library/Refresh", {})
            for _ in range(90):
                items = api(
                    "GET",
                    f"/Users/{user}/Items?Recursive=true&IncludeItemTypes=Episode"
                    "&Fields=ProviderIds,Overview,MediaStreams,SortName,Path&SortBy=SortName",
                )
                if len(items["Items"]) == count and all(
                    item.get("ProviderIds", {}).get("nebula") for item in items["Items"]
                ):
                    tasks = api("GET", "/ScheduledTasks")
                    if all(t["State"] != "Running" for t in tasks if t["Key"] == "RefreshLibrary"):
                        return items["Items"]
                time.sleep(1)
            raise AssertionError(f"Expected {count} episodes: {items}")

        first = episodes(2)
        assert {item["Name"] for item in first} == {"First", "Second"}, first
        ids = {item["ProviderIds"]["nebula"]: item["Id"] for item in first}
        fixture("2026-10-01", "Backfill", "probe-old")
        items = episodes(3)
        assert [item["Name"] for item in items] == ["Backfill", "First", "Second"], items
        assert all(item["ParentIndexNumber"] == 2026 for item in items), items
        assert all(item["Overview"] == "Probe description" for item in items)
        for item in items:
            assert "Primary" in item.get("ImageTags", {}), item
            assert {
                s.get("Language")
                for s in item["MediaStreams"]
                if s["Type"] == "Subtitle" and s.get("IsExternal")
            } in ({"eng", "deu"}, {"eng", "ger"}), item["MediaStreams"]
            if item["ProviderIds"]["nebula"] in ids:
                assert item["Id"] == ids[item["ProviderIds"]["nebula"]]
        target = items[1]["Id"]
        api(
            "POST",
            "/Sessions/Playing",
            {"ItemId": target, "CanSeek": True, "PlayMethod": "DirectPlay"},
        )
        api(
            "POST",
            "/Sessions/Playing/Progress",
            {
                "ItemId": target,
                "PositionTicks": 2000000000,
                "IsPaused": False,
                "PlayMethod": "DirectPlay",
            },
        )
        api("POST", "/Sessions/Playing/Stopped", {"ItemId": target, "PositionTicks": 2000000000})
        resumed = api("GET", f"/Users/{user}/Items/Resume?IncludeItemTypes=Episode")
        assert any(item["Id"] == target for item in resumed["Items"]), resumed
        payload = api("GET", f"/Videos/{target}/stream?static=true", raw=True)
        assert len(payload) > 1000
        api("POST", f"/Users/{user}/PlayedItems/{target}", {})
        next_up = api("GET", f"/Shows/NextUp?UserId={user}")
        assert [item["Name"] for item in next_up["Items"]] == ["Second"], next_up
        shows = api("GET", f"/Users/{user}/Items?Recursive=true&IncludeItemTypes=Series")
        assert [item["Name"] for item in shows["Items"]] == ["Probe Channel"]
        result = {
            "jellyfin": info["Version"],
            "image": IMAGE,
            "checks": [
                "show and year season",
                "same-day separate episodes",
                "backfill ordering",
                "stable identity",
                "NFO title and description",
                "images",
                "external subtitles",
                "playable stream",
                "resume",
                "next up",
            ],
            "next_up": [{"name": i["Name"], "id": i["Id"]} for i in next_up["Items"]],
            "episodes": [
                {k: item.get(k) for k in ["Name", "ParentIndexNumber", "IndexNumber", "SortName"]}
                for item in items
            ],
        }
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        print(f"Probe artifacts: {root}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


if __name__ == "__main__":
    main()
