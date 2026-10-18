from app import library


def test_nebula_images_are_requested_as_jpeg():
    assert library.jpeg_source("https://images.nebula.tv/abc") == "https://images.nebula.tv/abc.jpg"
    assert (
        library.jpeg_source("https://images.nebula.tv/abc.png")
        == "https://images.nebula.tv/abc.png"
    )
    assert library.jpeg_source("https://example.org/abc") == "https://example.org/abc"


def test_fetch_image_survives_a_dropped_connection(monkeypatch, tmp_path):
    class Body:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit=-1):
            raise ConnectionResetError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Body())
    dest = tmp_path / "poster.jpg"
    assert library.fetch_image("https://example.org/poster.jpg", dest) is False
    assert not dest.exists()
