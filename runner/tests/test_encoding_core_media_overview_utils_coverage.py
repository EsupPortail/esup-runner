"""Validates media probe duration extraction and sprite sheet generation via ImageMagick."""

import importlib
import types

import pytest


def _load_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def test_media_probe_utils_missing_lines_coverage():
    """Validate Media probe utils missing lines coverage."""
    media_probe = _load_core_module("media_probe_utils")

    assert media_probe.seconds_from_timestamp("invalid") == 0.0
    assert media_probe.seconds_from_timestamp("aa:bb") == 0.0
    assert media_probe.seconds_from_timestamp("01:30") == 90.0

    assert media_probe.duration_seconds_from_value(7) == 7.0
    assert media_probe.duration_seconds_from_value(" ") == 0.0

    assert media_probe.extract_duration_from_probe("not-a-dict") == 0
    assert media_probe.extract_duration_from_probe({"streams": ["invalid-stream"]}) == 0
    assert (
        media_probe.extract_primary_video_duration_from_probe(
            "not-a-dict",
            image_codecs=["png"],
        )
        == 0.0
    )
    assert (
        media_probe.extract_primary_video_duration_from_probe(
            {"streams": "not-a-list"},
            image_codecs=["png"],
        )
        == 0.0
    )
    assert (
        media_probe.extract_primary_video_duration_from_probe(
            {
                "streams": [
                    "invalid-stream",
                    {"codec_type": "audio", "codec_name": "aac", "duration": "7"},
                    {"codec_type": "video", "codec_name": "png", "duration": "7"},
                ]
            },
            image_codecs=["png"],
        )
        == 0.0
    )

    probe_info = {
        "format": {"duration": "5.604333"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "duration": "0.533333"},
            {"codec_type": "audio", "codec_name": "aac", "duration": "5.601333"},
        ],
    }
    assert media_probe.extract_duration_from_probe(probe_info) == 5
    assert (
        media_probe.extract_primary_video_duration_from_probe(
            probe_info,
            image_codecs=["png", "mjpeg"],
        )
        == 0.533333
    )


def test_overview_utils_missing_lines_coverage(monkeypatch, tmp_path):
    """Validate Overview utils missing lines coverage."""
    overview = _load_core_module("overview_utils")

    ok, msg = overview.try_sprite_imagemagick_append(
        temp_thumb_dir=str(tmp_path),
        num_thumbnails=1,
        sprite_path=str(tmp_path / "overview.png"),
    )
    assert ok is False
    assert "skipped" in msg

    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    (thumb_dir / "thumb_0001.png").write_bytes(b"1")
    (thumb_dir / "thumb_0002.png").write_bytes(b"2")

    sprite_path = tmp_path / "overview.png"

    def _run_success(*_a, **_k):
        sprite_path.write_bytes(b"png")
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(overview.subprocess, "run", _run_success)
    ok, msg = overview.try_sprite_imagemagick_append(
        temp_thumb_dir=str(thumb_dir),
        num_thumbnails=2,
        sprite_path=str(sprite_path),
    )
    assert ok is True
    assert "Sprite sheet created via ImageMagick" in msg

    monkeypatch.setattr(
        overview.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=9, stdout="convert failed"),
    )
    ok, msg = overview.try_sprite_imagemagick_append(
        temp_thumb_dir=str(thumb_dir),
        num_thumbnails=2,
        sprite_path=str(tmp_path / "overview.png"),
    )
    assert ok is False
    assert "convert failed" in msg

    monkeypatch.setattr(
        overview.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("convert")),
    )
    ok, msg = overview.try_sprite_imagemagick_append(
        temp_thumb_dir=str(thumb_dir),
        num_thumbnails=2,
        sprite_path=str(tmp_path / "overview.png"),
    )
    assert ok is False
    assert "convert command not found" in msg

    monkeypatch.setattr(
        overview.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ok, msg = overview.try_sprite_imagemagick_append(
        temp_thumb_dir=str(thumb_dir),
        num_thumbnails=2,
        sprite_path=str(tmp_path / "overview.png"),
    )
    assert ok is False
    assert "exception" in msg

    with pytest.raises(ValueError, match="Invalid overview thumbnail dimensions"):
        overview.get_overview_max_single_row_thumbnails(
            0,
            90,
            max_sprite_width=1000,
            max_sprite_height=1000,
        )

    with pytest.raises(ValueError, match="exceeds max sprite size"):
        overview.get_overview_max_single_row_thumbnails(
            2000,
            90,
            max_sprite_width=1000,
            max_sprite_height=1000,
        )

    class _WeirdMaxWidth:
        def __lt__(self, _other):
            return False

        def __floordiv__(self, _other):
            return 0

        def __str__(self):
            return "1"

    with pytest.raises(ValueError, match="too large for max sprite width"):
        overview.get_overview_max_single_row_thumbnails(
            1,
            1,
            max_sprite_width=_WeirdMaxWidth(),
            max_sprite_height=10,
        )

    ok, msg, generated = overview.build_overview_generation_result_msg(
        str(tmp_path), expected_count=4
    )
    assert ok is False
    assert generated == 0
    assert "generated no thumbnails" in msg
