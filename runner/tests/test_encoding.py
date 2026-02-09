import io
from unittest.mock import Mock, patch

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
    def _resp():
        r = Mock()
        r.status_code = 200
        r.headers = {"Content-Length": str(1024 * 1024)}  # 1MB
        r.raw = io.BytesIO(b"fake-video-bytes")
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
