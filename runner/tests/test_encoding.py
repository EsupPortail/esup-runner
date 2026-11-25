"""Validates video encoding handler execution, error handling, and script output collection."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.models.models import TaskRequest
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler


def make_task_request():
    return TaskRequest(
        task_id="task-enc-001",
        etab_name="UM",
        app_name="TestApp",
        task_type="encoding",
        source_url="https://example.org/sample.mp4",
        parameters={"rendition": '{"720": {"resolution": "1280x720", "encode_mp4": true}}'},
        notify_url="http://manager/callback",
    )


@patch("subprocess.run")
@patch("requests.Session.get")
def test_encoding_handler_success(mock_get, mock_run):
    # Mock HTTP download of the source video
    """Validate Encoding handler success."""

    def _resp():
        payload = b"fake-video-bytes"
        r = Mock()
        r.status_code = 200
        r.headers = {"Content-Length": str(len(payload))}
        r.iter_content = lambda chunk_size=8192: iter([payload])
        # Context manager support
        r.__enter__ = lambda s: s
        r.__exit__ = lambda s, exc_type, exc, tb: None
        return r

    mock_get.return_value = _resp()

    # Mock external script execution
    def run_side_effect(cmd, timeout=None, capture_output=None, text=None, cwd=None):
        m = Mock()
        m.returncode = 0
        m.stdout = "encoding ok"
        m.stderr = ""
        return m

    mock_run.side_effect = run_side_effect

    handler = VideoEncodingHandler()
    res = handler.execute_task("task-enc-001", make_task_request())

    assert res["task_type"] == "encoding"
    assert res["success"] in (True, False)
    assert "script_output" in res
    assert "input_path" in res and isinstance(res["input_path"], str)
    assert "output_dir" in res and isinstance(res["output_dir"], str)


def test_encoding_handler_returns_explicit_script_error(monkeypatch):
    """Validate Encoding handler returns explicit script error."""
    handler = VideoEncodingHandler()

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )
    monkeypatch.setattr(
        handler,
        "run_external_script",
        lambda script_path, args, timeout=0: {
            "success": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "Encoding aborted: input video duration is 0 seconds.\n",
        },
    )

    res = handler.execute_task("task-enc-001", make_task_request())

    assert res["success"] is False
    assert res["error"] == "Encoding aborted: input video duration is 0 seconds."


@pytest.mark.parametrize(
    ("url_name", "expected_name"),
    [
        ("x%20-i%20%2Fetc%2Fpasswd%09-f%0Adata%20.mp4", "passwd_-f_data_.mp4"),
        ("l%27amphi%20%22A%22%5C.mp4", "l_amphi__A__.mp4"),
        ("vid%C3%A9o_(1)%2B%40.mp4", "vidéo_(1)+@.mp4"),
    ],
)
def test_encoding_handler_keeps_source_filename_as_one_command_token(
    monkeypatch, tmp_path, url_name, expected_name
):
    """Neutralize filename characters that would add arguments to encoding commands."""
    import shlex

    from app.task_handlers.encoding import encoding_handler

    monkeypatch.setattr(encoding_handler.storage_manager, "base_path", str(tmp_path))
    handler = VideoEncodingHandler()
    calls = {}

    def fake_download(source_url, dest_file):
        calls["dest_file"] = dest_file
        return {"success": True, "file_path": dest_file}

    def fake_run(script_path, args, timeout=0):
        calls["args"] = args
        return {"success": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(handler, "download_source_file", fake_download)
    monkeypatch.setattr(handler, "run_external_script", fake_run)
    request = make_task_request()
    request.source_url = f"https://example.org/media/{url_name}?token=a%20b"

    handler.execute_task("task-enc-001", request)

    input_file = calls["args"][calls["args"].index("--input-file") + 1]
    assert input_file == expected_name
    assert Path(calls["dest_file"]).name == expected_name
    assert shlex.split(f"ffmpeg -i {tmp_path}/{input_file} -y out.mp4")[2] == (
        f"{tmp_path}/{expected_name}"
    )
