#!/usr/bin/env python3
"""Set the release version and update versioned image references."""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
VERSIONED_FILES = (ROOT / "docker-compose.yml", ROOT / "README.md")
IMAGE_TAG = re.compile(r"(ghcr\.io/karoc123/nebula-to-jellyfin:)v?\d+\.\d+\.\d+")
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(value: str) -> str:
    value = value.strip().removeprefix("v")
    if SEMVER.fullmatch(value) is None:
        raise ValueError(f"Not a MAJOR.MINOR.PATCH version: {value}")
    return value


def tag_exists(version: str) -> bool:
    result = subprocess.run(
        ["git", "tag", "--list", f"v{version}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def update_pyproject(version: str) -> None:
    text = PYPROJECT.read_text()
    updated, count = re.subn(
        r'(?m)^version = "\d+\.\d+\.\d+"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find the project version in pyproject.toml")
    PYPROJECT.write_text(updated)
    if tomllib.loads(updated)["project"]["version"] != version:
        raise RuntimeError("pyproject.toml does not contain the new version")


def update_image_tags(version: str) -> None:
    for path in VERSIONED_FILES:
        if not path.exists():
            continue
        text = path.read_text()
        updated, count = IMAGE_TAG.subn(rf"\g<1>v{version}", text)
        if count:
            path.write_text(updated)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="Version to release, for example 0.2.0")
    args = parser.parse_args()

    version = parse_version(args.version)
    if tag_exists(version):
        raise SystemExit(f"Tag v{version} already exists; choose a new version")

    update_pyproject(version)
    update_image_tags(version)
    print(version)


if __name__ == "__main__":
    main()
