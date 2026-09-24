"""Download limits apply to received bytes and cannot be bypassed by cache or fallback."""

import io
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.core.config import config
from app.core.download_limits import DownloadSizeExceeded, save_download
from app.task_handlers import base_handler
from app.task_handlers.encoding.core import dressing_runtime_utils as dressing
from app.task_handlers.studio.core import download_runtime_utils as studio
from app.task_handlers.studio.studio_handler import StudioEncodingHandler


@pytest.mark.parametrize("content_length", [None, "1"])
def test_main_download_stops_oversized_body_without_retry(monkeypatch, tmp_path, content_length):
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 3 / 1024**3)
    monkeypatch.setattr(base_handler.time, "sleep", lambda _: pytest.fail("Must not retry"))
    received = []
    calls = []
    closed = []

    class Response:
        status_code = 200
        headers = {} if content_length is None else {"Content-Length": content_length}

        def iter_content(self, chunk_size):
            for chunk in (b"ab", b"cd", b"never-read"):
                received.append(chunk)
                yield chunk

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append("response")

    class Session:
        def get(self, *_args, **_kwargs):
            calls.append(1)
            return Response()

        def close(self):
            closed.append("session")

    monkeypatch.setattr(base_handler.requests, "Session", Session)
    destination = tmp_path / "source.mp4"
    destination.write_bytes(b"previous-complete-file")
    result = StudioEncodingHandler().download_source_file(
        "https://example.org/video.mp4", str(destination)
    )
    assert result["success"] is False
    assert "maximum allowed size" in result["error"]
    assert len(calls) == 1
    assert received == [b"ab", b"cd"]
    assert closed == ["response", "session"]
    assert destination.read_bytes() == b"previous-complete-file"
    assert not destination.with_name(destination.name + ".part").exists()


def _download(kind, directory):
    url = "https://example.org/video.mp4"
    if kind == "studio":
        return studio.download_http_source(url, str(directory), "presentation", urlparse(url))
    return dressing.download_url_to_dir(
        url, str(directory), "opening", sanitize_filename_fn=lambda name: name
    )


class ChunkedResponse(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024, "Reads must have a bounded size"
        return super().read(min(size, 2))


@pytest.mark.parametrize("kind", ["studio", "dressing"])
@pytest.mark.parametrize("limit", [0, 3, 4, 5])
def test_specialized_download_limits_and_unlimited_mode(monkeypatch, tmp_path, kind, limit):
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", limit / 1024**3)
    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    monkeypatch.delenv("DOWNLOAD_ALLOWED_HOSTS", raising=False)
    response = ChunkedResponse(b"abcd")
    monkeypatch.setattr(studio.urllib.request, "urlopen", lambda *_a, **_k: response)
    if limit == 3 and kind == "dressing":
        with pytest.raises(DownloadSizeExceeded):
            _download(kind, tmp_path)
    else:
        path = _download(kind, tmp_path)
        if limit == 3:
            assert path is None  # Never hand the remote URL to FFmpeg on size rejection.
        else:
            assert Path(path).read_bytes() == b"abcd"
    assert response.closed
    assert not list(tmp_path.glob("*.part"))
    if limit == 3:
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["studio", "dressing"])
def test_cached_media_respects_lowered_limit(monkeypatch, tmp_path, kind):
    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    monkeypatch.delenv("DOWNLOAD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 0)
    monkeypatch.setattr(
        studio.urllib.request, "urlopen", lambda *_a, **_k: ChunkedResponse(b"abcd")
    )
    cached = _download(kind, tmp_path)
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 3 / 1024**3)
    monkeypatch.setattr(
        studio.urllib.request,
        "urlopen",
        lambda *_a, **_k: pytest.fail("Cache must not be downloaded again"),
    )
    if kind == "studio":
        assert _download(kind, tmp_path) is None
    else:
        with pytest.raises(DownloadSizeExceeded):
            _download(kind, tmp_path)
    assert Path(cached).read_bytes() == b"abcd"


def test_interrupted_stream_does_not_publish_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 0)
    destination = tmp_path / "asset.mp4"
    destination.write_bytes(b"complete")

    def interrupted_chunks():
        yield b"partial"
        raise OSError("connection interrupted")

    with pytest.raises(OSError, match="interrupted"):
        save_download(interrupted_chunks(), str(destination))
    assert destination.read_bytes() == b"complete"
    assert not destination.with_name(destination.name + ".part").exists()
