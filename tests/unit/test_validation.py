import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from yt_dlp import YoutubeDL

from app.config import FORMAT, Config, validate_options, validate_schedule
from app.ingest import normalize_url, publication
from app.library import path_stem, sanitize, write_nfo
from app.scheduler import schedule_window


@pytest.mark.parametrize(
    "url",
    [
        "https://nebula.tv.evil/videos/test",
        "https://evil@nebula.tv/videos/test",
        "https://nebula.tv:55/probe",
        "file:///nebula.tv/probe",
        "https://nebula.tv/videos/../../test",
        "https://nebula.tv/myshows",
        "https://nebula.tv/probe/more",
        "https://[invalid",
    ],
)
def test_reject_urls(url):
    with pytest.raises(ValueError):
        normalize_url(url)


def test_normalize_urls():
    assert (
        normalize_url("http://www.nebula.tv/videos/test?x=1#data", "video")
        == "https://nebula.tv/videos/test"
    )
    assert normalize_url("https://nebula.tv/probe/", "channel") == "https://nebula.tv/probe"
    with pytest.raises(ValueError):
        normalize_url("https://nebula.tv/videos/test", "channel")


@pytest.mark.parametrize(
    "options",
    [
        "[]",
        "{",
        '{"outtmpl":"x"}',
        '{"exec_cmd":"x"}',
        '{"postprocessors":[]}',
        '{"cookiefile":"x"}',
        '{"ratelimit":true}',
        '{"subtitleslangs":[1]}',
        '{"retries":-1}',
        '{"format":""}',
    ],
)
def test_reject_options(options):
    with pytest.raises(ValueError):
        validate_options(options)


def test_valid_options():
    assert validate_options('{"subtitleslangs":["en","de"],"ratelimit":100}') == {
        "subtitleslangs": ["en", "de"],
        "ratelimit": 100,
    }


@pytest.mark.parametrize(
    "info", [{}, {"upload_date": "20260230"}, {"timestamp": "bad"}, {"upload_date": "2026101"}]
)
def test_invalid_publication(info):
    with pytest.raises(ValueError):
        publication(info)


def test_publication_utc_and_precedence():
    assert publication({"timestamp": 0}) == "1970-01-01T00:00:00+00:00"
    assert publication({"release_date": "20200102", "upload_date": "20261014"}).startswith(
        "2020-01-02"
    )


def test_filename_bytes_and_dots():
    name = sanitize("漢" * 150)
    assert len(name.encode()) <= 100
    assert sanitize("CON") == "_CON"
    assert "/" not in sanitize("../a/b")
    channel = {"directory": "Channel [slug]"}
    first = {"published_at": "2026-10-14T10:00:00+00:00", "title": "A. title", "id": "a"}
    second = {**first, "id": "b"}
    assert path_stem(channel, first) != path_stem(channel, second)
    assert path_stem(channel, first).startswith("Channel [slug]/Season 2026/2026-10-14")
    assert "S2026E" not in path_stem(channel, first)


def test_nfo_xml(tmp_path):
    path = tmp_path / "episode.nfo"
    write_nfo(path, "episodedetails", {"title": "<&>\x00", "season": 2026}, "identity")
    root = ET.parse(path).getroot()
    assert root.findtext("title") == "<&>"
    assert root.findtext("uniqueid") == "identity"
    assert root.find("episode") is None


@pytest.mark.parametrize("at,zone", [("24:00", "UTC"), ("4:00", "UTC"), ("04:00", "Unknown/Zone")])
def test_invalid_schedule(at, zone):
    with pytest.raises(ValueError):
        validate_schedule(at, zone)


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-18T01:59:00+00:00", "2026-10-18T02:00:00+00:00"),
        ("2026-10-18T02:00:00+00:00", "2026-10-19T02:00:00+00:00"),
        ("2026-03-29T00:00:00+00:00", "2026-03-29T02:00:00+00:00"),
        ("2026-10-25T00:00:00+00:00", "2026-10-25T03:00:00+00:00"),
    ],
)
def test_schedule(stamp, expected):
    current = datetime.fromisoformat(stamp)
    due, next_run = schedule_window(current, "04:00", "Europe/Berlin")
    assert due <= current < next_run
    assert next_run.isoformat() == expected


def test_dst_nonexistent_and_repeated_time():
    current = datetime(2026, 3, 29, 0, tzinfo=UTC)
    _, next_run = schedule_window(current, "02:30", "Europe/Berlin")
    assert next_run.hour == 1  # 03:30 local after the missing hour.
    first = datetime.fromisoformat("2026-10-25T00:30:00+00:00")
    second = datetime.fromisoformat("2026-10-25T01:30:00+00:00")
    assert (
        schedule_window(first, "02:30", "Europe/Berlin")[0]
        == schedule_window(second, "02:30", "Europe/Berlin")[0]
    )


def test_storage_layout(config, tmp_path):
    with pytest.raises(ValueError):
        Config(tmp_path / "data", tmp_path / "media", tmp_path / "media/work").prepare()


def test_import_has_no_side_effects(tmp_path):
    target = tmp_path / "absent"
    env = dict(os.environ, DATA_DIR=str(target), LIBRARY_DIR=str(target / "library"))
    subprocess.run([sys.executable, "-c", "import app.main"], env=env, check=True)
    assert not target.exists()


def test_format_selection_prefers_hevc_and_caps_resolution():
    formats = [
        {
            "format_id": "audio",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "aac",
            "url": "https://example.org/a",
        },
        *[
            {
                "format_id": name,
                "ext": "mp4",
                "vcodec": codec,
                "acodec": "none",
                "height": height,
                "width": height * 2,
                "url": "https://example.org/" + name,
            }
            for name, codec, height in [
                ("h264", "avc1", 1080),
                ("hevc", "hvc1", 720),
                ("4k", "hvc1", 2160),
            ]
        ],
    ]
    with YoutubeDL({"format": FORMAT, "quiet": True}) as ydl:
        selected = ydl.process_ie_result(
            {"id": "test", "title": "test", "formats": formats}, download=False
        )
    assert selected["requested_formats"][0]["format_id"] == "hevc"
    no_hevc = [f for f in formats if f.get("format_id") != "hevc"]
    with YoutubeDL({"format": FORMAT, "quiet": True}) as ydl:
        selected = ydl.process_ie_result(
            {"id": "test", "title": "test", "formats": no_hevc}, download=False
        )
    assert selected["requested_formats"][0]["format_id"] == "h264"
    assert json.dumps(selected)
