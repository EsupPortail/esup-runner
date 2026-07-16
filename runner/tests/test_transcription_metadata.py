"""Tests for transcription languages and metadata."""

import sys
from pathlib import Path
from typing import Any

from transcription_test_helpers import load_transcription_core_module as _load_core_module
from transcription_test_helpers import transcription_core_dir as _core_dir


def test_language_utils_extra_branches():
    """Validate Language utils extra branches."""
    language_utils = _load_core_module("language_utils")

    assert language_utils.map_language_name_to_code("") is None
    assert language_utils.normalize_language_code("   ") is None
    assert language_utils.normalize_language_code("French") == "fr"


def test_metadata_utils_extra_branches(tmp_path, capsys):
    """Validate Metadata utils extra branches."""
    metadata_utils = _load_core_module("metadata_utils")

    metadata_utils.write_info_video_metadata(tmp_path, {}, debug=True)
    assert not (tmp_path / "info_video.json").exists()

    (tmp_path / "info_video.json").write_text("{not-valid-json", encoding="utf-8")
    metadata_utils.write_info_video_metadata(tmp_path, {"a": 1}, debug=True)
    written = (tmp_path / "info_video.json").read_text(encoding="utf-8")
    assert '"a": 1' in written
    assert "Task metadata written to:" in capsys.readouterr().out

    calls = []

    def writer(work_dir: Path, payload: dict[str, Any], debug: bool) -> None:
        calls.append((work_dir, payload, debug))

    metadata_utils.write_video_identification_metadata(tmp_path, {}, writer, debug=True)
    assert calls == []

    metadata_utils.write_video_identification_metadata(
        tmp_path, {"video_id": "42"}, writer, debug=True
    )
    assert len(calls) == 1

    translation_metadata = metadata_utils.build_translation_metadata(
        applied=True,
        backend="local",
        source_language="en",
        target_language="fr",
        model="model",
        use_gpu=False,
        normalize_language=lambda value: value,
        execution_backend="whisper_python",
    )
    assert translation_metadata["execution_backend"] == "whisper_python"


def test_metadata_runtime_utils_does_not_change_sys_path():
    """Validate metadata runtime imports without changing the import path."""
    metadata_runtime_utils = _load_core_module("metadata_runtime_utils", force_insert_branch=True)
    assert str(_core_dir()) not in sys.path

    metadata = metadata_runtime_utils.build_transcription_runtime_metadata(
        requested_language="fr",
        detected_language="en",
        final_language="fr",
        whisper_model="small",
        use_gpu=False,
        translation={"applied": False},
    )
    assert metadata["transcription"]["final_subtitle_language"] == "fr"
