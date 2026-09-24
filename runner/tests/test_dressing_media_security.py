"""Dressing assets are checked before any probe or media transformation."""

import io
from functools import partial

import pytest

from app.core.config import config
from app.core.media_denylist import MediaDeniedError
from app.task_handlers.encoding.core import dressing_runtime_utils as dressing

DENIED_MEDIA = b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 12 + b"MAGY"


@pytest.fixture
def download(monkeypatch):
    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    monkeypatch.delenv("DOWNLOAD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 0)
    monkeypatch.setattr(config, "MEDIA_CODEC_DENYLIST", ["magicyuv"])
    return partial(dressing.download_url_to_dir, sanitize_filename_fn=lambda name: name)


@pytest.mark.parametrize("prefix", ["watermark", "opening", "ending"])
@pytest.mark.parametrize("cached", [False, True])
def test_denied_asset_never_reaches_media_processing(
    download, monkeypatch, tmp_path, prefix, cached
):
    url = "https://example.org/asset.mp4"
    calls = []

    def urlopen(*_args, **_kwargs):
        calls.append(1)
        return io.BytesIO(DENIED_MEDIA)

    monkeypatch.setattr(dressing.urllib.request, "urlopen", urlopen)
    if cached:
        monkeypatch.setattr(config, "MEDIA_CODEC_DENYLIST", [])
        download(url, str(tmp_path), prefix)
        monkeypatch.setattr(config, "MEDIA_CODEC_DENYLIST", ["magicyuv"])
        monkeypatch.setattr(
            dressing.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("Must use cache")
        )

    def forbidden_processing(*_args, **_kwargs):
        pytest.fail("The denied asset must not reach ffprobe or FFmpeg")

    common = dict(
        current_main_path="main.mp4",
        base="main",
        assets_dir=str(tmp_path),
        videos_dir=str(tmp_path),
        download_url_to_dir_fn=download,
    )
    if prefix == "watermark":
        path, message = dressing.apply_watermark_for_dressing(
            **common,
            dressing_config={"watermark": url},
            create_watermarked_intermediate_fn=forbidden_processing,
        )
    else:
        path, message = dressing.apply_credits_for_dressing(
            **common,
            dressing_config={f"{prefix}_credits_video": url},
            create_credits_concat_intermediate_fn=forbidden_processing,
        )
    assert path == "main.mp4"
    assert "Media rejected: MagicYUV" in message
    assert len(calls) == 1
    with pytest.raises(MediaDeniedError):
        download(url, str(tmp_path), prefix)
    assert len(calls) == 1


@pytest.mark.parametrize("denylist,payload", [(["magicyuv"], b"safe-media"), ([], DENIED_MEDIA)])
def test_allowed_assets_are_available_from_download_and_cache(
    download, monkeypatch, tmp_path, denylist, payload
):
    monkeypatch.setattr(config, "MEDIA_CODEC_DENYLIST", denylist)
    monkeypatch.setattr(dressing.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(payload))
    url = "https://example.org/watermark.png"
    path = download(url, str(tmp_path), "watermark")
    monkeypatch.setattr(
        dressing.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("Must use cache")
    )
    assert download(url, str(tmp_path), "watermark") == path
