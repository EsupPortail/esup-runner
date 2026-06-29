"""Validates application-level media codec denylist helpers."""

import pytest

from app.core.media_denylist import (
    MAGICYUV_DENY_MESSAGE,
    MediaDeniedError,
    detect_denied_media,
    has_magicyuv_signature,
    normalize_media_codec_denylist,
    validate_media_against_denylist,
)
from app.task_handlers.studio.core import download_runtime_utils, main_orchestration_utils


def test_magicyuv_signature_detects_riff_avi_fourcc():
    """Validate MagicYUV signature detects riff avi fourcc."""
    payload = b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 32 + b"MAGY"

    assert has_magicyuv_signature(payload) is True


def test_magicyuv_signature_detects_textual_codec_marker():
    """Validate MagicYUV signature detects textual codec marker."""
    assert has_magicyuv_signature(b"container metadata: MagicYUV codec") is True


def test_magicyuv_signature_ignores_plain_text_without_container_hint():
    """Validate MagicYUV signature ignores plain text without container hint."""
    assert has_magicyuv_signature(b"not a media MAGY marker") is False


def test_detect_denied_media_returns_configured_message(tmp_path):
    """Validate Detect denied media returns configured message."""
    media = tmp_path / "sample.avi"
    media.write_bytes(b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 12 + b"MAGY")

    match = detect_denied_media(media, ["MagicYUV"])

    assert match is not None
    assert match.codec == "magicyuv"
    assert match.message == MAGICYUV_DENY_MESSAGE


def test_validate_media_against_denylist_raises_for_magicyuv(tmp_path):
    """Validate media denylist raises for magicyuv."""
    media = tmp_path / "sample.mkv"
    media.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 16 + b"MAGY")

    with pytest.raises(MediaDeniedError, match="MagicYUV codec is temporarily denied"):
        validate_media_against_denylist(media, ["magicyuv"])


def test_validate_media_against_denylist_ignores_disabled_or_unknown_items(tmp_path):
    """Validate media denylist ignores disabled or unknown items."""
    media = tmp_path / "sample.avi"
    media.write_bytes(b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 12 + b"MAGY")

    assert normalize_media_codec_denylist([" MagicYUV ", "", "future-codec"]) == {
        "magicyuv",
        "future-codec",
    }
    validate_media_against_denylist(media, [])
    validate_media_against_denylist(media, ["future-codec"])


def test_studio_materialize_rejects_local_denied_media(monkeypatch, tmp_path, capsys):
    """Validate studio materialize rejects local denied media before ffprobe."""
    media = tmp_path / "sample.avi"
    media.write_bytes(b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 12 + b"MAGY")
    monkeypatch.setattr(download_runtime_utils.config, "MEDIA_CODEC_DENYLIST", ["magicyuv"])

    result = download_runtime_utils.materialize_source(str(media), str(tmp_path), "presentation")

    assert result is None
    assert "Media rejected: MagicYUV codec" in capsys.readouterr().out


def test_studio_download_http_source_rejects_existing_denied_media(monkeypatch, tmp_path):
    """Validate Studio download rejects an existing cached denied source."""
    local_path = tmp_path / "presentation.mp4"
    local_path.write_bytes(b"cached")
    monkeypatch.setattr(download_runtime_utils, "source_matches_media_denylist", lambda _path: True)
    parsed = download_runtime_utils.urllib.parse.urlparse("https://example.org/presentation.mp4")

    result = download_runtime_utils.download_http_source(
        "https://example.org/presentation.mp4",
        str(tmp_path),
        "presentation",
        parsed,
    )

    assert result is None


def test_studio_download_http_source_rejects_downloaded_denied_media(monkeypatch, tmp_path, capsys):
    """Validate Studio download rejects a newly downloaded denied source."""

    class _Response:
        @staticmethod
        def read():
            return b"downloaded"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _raise_denied(*_args, **_kwargs):
        raise MediaDeniedError("Media rejected: denied")

    monkeypatch.setattr(
        download_runtime_utils.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response()
    )
    monkeypatch.setattr(download_runtime_utils, "validate_media_against_denylist", _raise_denied)
    parsed = download_runtime_utils.urllib.parse.urlparse("https://example.org/downloaded.mp4")

    result = download_runtime_utils.download_http_source(
        "https://example.org/downloaded.mp4",
        str(tmp_path),
        "presentation",
        parsed,
    )

    assert result is None
    assert "Media rejected: denied" in capsys.readouterr().out


def test_studio_main_flow_fails_when_existing_source_is_rejected(tmp_path, capsys):
    """Validate studio main flow fails when a declared source is rejected."""
    args = type(
        "Args",
        (),
        {
            "base_dir": str(tmp_path),
            "work_dir": "output",
            "studio_allow_nvenc": "",
            "output_file": "studio_base.mp4",
            "studio_audio_bitrate": None,
        },
    )()
    context = main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=lambda _args: ("presentation.avi", None, "mid", None),
        load_clip_times_fn=lambda _smil_url: (None, None),
        materialize_source_fn=lambda _source, *_args: None,
        is_webm_input_source_fn=lambda _source: False,
        build_input_args_fn=lambda *_args: ("", 0, 0),
        build_subtime_fn=lambda _start, _end: "",
        run_pipelines_fn=lambda **_kwargs: 0,
    )

    assert main_orchestration_utils.run_main_flow(args, context=context) == 1
    assert "Presentation media source was rejected or unavailable" in capsys.readouterr().out


def test_studio_main_flow_fails_when_presenter_source_is_rejected(tmp_path, capsys):
    """Validate studio main flow fails when a declared presenter source is rejected."""
    args = type(
        "Args",
        (),
        {
            "base_dir": str(tmp_path),
            "work_dir": "output",
            "studio_allow_nvenc": "",
            "output_file": "studio_base.mp4",
            "studio_audio_bitrate": None,
        },
    )()

    def _materialize(source, *_args):
        return None if source == "presenter.avi" else source

    context = main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=lambda _args: (
            "presentation.avi",
            "presenter.avi",
            "mid",
            None,
        ),
        load_clip_times_fn=lambda _smil_url: (None, None),
        materialize_source_fn=_materialize,
        is_webm_input_source_fn=lambda _source: False,
        build_input_args_fn=lambda *_args: ("", 0, 0),
        build_subtime_fn=lambda _start, _end: "",
        run_pipelines_fn=lambda **_kwargs: 0,
    )

    assert main_orchestration_utils.run_main_flow(args, context=context) == 1
    assert "Presenter media source was rejected or unavailable" in capsys.readouterr().out
