import importlib.util
from pathlib import Path


def _load_transcription_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (
        repo_root / "app" / "task_handlers" / "transcription" / "scripts" / "transcription.py"
    )
    spec = importlib.util.spec_from_file_location("transcription_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalize_vtt_accepts_truncated_stem_from_whisper_cli(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    whisper_cli_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.vtt"
    whisper_cli_vtt.write_text("WEBVTT\n\n")

    rc = tr._finalize_vtt(audio_src, work_dir)

    expected_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.189832.vtt"
    assert rc == 0
    assert expected_vtt.exists()
    assert not whisper_cli_vtt.exists()


def test_finalize_vtt_fails_when_no_vtt_found(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    rc = tr._finalize_vtt(audio_src, work_dir)

    assert rc == 5
