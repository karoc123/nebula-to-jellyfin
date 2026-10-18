from __future__ import annotations

import base64
import http.client
import json
import logging
import urllib.error
import urllib.request
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path

log = logging.getLogger(__name__)

CHANNEL_URL = "https://content.api.nebula.app/content/{}/"
AUTHORIZATION_URL = "https://users.api.nebula.app/api/v1/authorization/"
TOKEN_COOKIE = "nebula_auth.apiToken"


def cookie_token(path: Path) -> str:
    """Return the Nebula API token stored in a Netscape cookie file."""
    jar = MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (LoadError, ValueError) as exc:
        raise ValueError("Expected a Netscape cookie file") from exc
    for cookie in jar:
        if cookie.name == TOKEN_COOKIE and cookie.domain.lstrip(".") in {
            "nebula.tv",
            "www.nebula.tv",
        }:
            return cookie.value
    raise ValueError("Cookie file has no Nebula authentication token")


def _claims(token: str) -> dict:
    # The token is received directly from Nebula over TLS. Decoding is only used to
    # report subscription state, never to authorize local actions.
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Nebula returned an invalid authorization token") from exc
    if not isinstance(claims, dict):
        raise ValueError("Nebula returned an invalid authorization token")
    return claims


def authorization_claims(token: str) -> dict:
    """Exchange an API token for a Nebula authorization token and return its claims."""
    request = urllib.request.Request(
        AUTHORIZATION_URL,
        data=b"",
        headers={"Authorization": f"Token {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("Nebula rejected the saved cookies; import fresh cookies") from exc
        raise
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str):
        raise ValueError("Nebula returned an invalid authorization response")
    return _claims(token)


def channel_artwork(slug: str) -> tuple[str | None, str | None]:
    """Return the public avatar and banner of a Nebula channel.

    Artwork is optional. Network and payload failures are logged and never fail a catalog import.
    """
    try:
        request = urllib.request.Request(
            CHANNEL_URL.format(slug), headers={"User-Agent": "nebula-to-jellyfin/0.1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        log.warning("Optional channel artwork metadata unavailable: %s", exc)
        return None, None
    images = payload.get("images") if isinstance(payload, dict) else None
    if not isinstance(images, dict):
        return None, None

    def source(name: str) -> str | None:
        image = images.get(name)
        value = image.get("src") if isinstance(image, dict) else None
        return value if isinstance(value, str) and value.startswith("https://") else None

    return source("avatar"), source("banner")
