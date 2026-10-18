import base64
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from app import nebula
from app.nebula import channel_artwork as real_channel_artwork

COOKIE = "# Netscape HTTP Cookie File\n.nebula.tv\tTRUE\t/\tTRUE\t0\tnebula_auth.apiToken\tsecret\n"


def response(payload):
    class Body(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()
            return False

    return Body(json.dumps(payload).encode())


def test_channel_artwork_reads_public_api(monkeypatch):
    monkeypatch.setattr(nebula, "channel_artwork", real_channel_artwork)
    payload = {
        "images": {
            "avatar": {"src": "https://images.nebula.tv/avatar"},
            "banner": {"src": "https://images.nebula.tv/banner"},
            "hero": {"src": "https://images.nebula.tv/hero"},
        }
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: response(payload))
    assert nebula.channel_artwork("probe") == (
        "https://images.nebula.tv/avatar",
        "https://images.nebula.tv/banner",
    )


def test_channel_artwork_is_optional(monkeypatch, caplog):
    monkeypatch.setattr(nebula, "channel_artwork", real_channel_artwork)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    assert nebula.channel_artwork("probe") == (None, None)
    assert "Optional channel artwork metadata unavailable" in caplog.text


def test_channel_artwork_survives_a_dropped_connection(monkeypatch, caplog):
    monkeypatch.setattr(nebula, "channel_artwork", real_channel_artwork)

    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit=-1):
            raise ConnectionResetError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Body())
    assert nebula.channel_artwork("probe") == (None, None)
    assert "Optional channel artwork metadata unavailable" in caplog.text


def jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def test_cookie_token_reads_nebula_api_token(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(COOKIE)
    assert nebula.cookie_token(path) == "secret"


def test_cookie_token_requires_nebula_authentication(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n.example.org\tTRUE\t/\tTRUE\t0\tsession\tvalue\n")
    with pytest.raises(ValueError, match="no Nebula authentication token"):
        nebula.cookie_token(path)


def test_authorization_claims_read_subscription(monkeypatch):
    claims = {"is_anonymous": False, "is_subscribed": True}
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *args, **kwargs: response({"token": jwt(claims)})
    )
    assert nebula.authorization_claims("api-token") == claims


def test_authorization_claims_reject_expired_cookies(monkeypatch):
    def fail(*args, **kwargs):
        raise HTTPError(nebula.AUTHORIZATION_URL, 401, "Unauthorized", {}, io.BytesIO())

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ValueError, match="rejected the saved cookies"):
        nebula.authorization_claims("expired")
