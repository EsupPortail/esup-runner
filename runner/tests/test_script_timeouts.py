from pathlib import Path
from typing import Any, Dict

from app.core.config import Config, config
from app.models.models import TaskRequest
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler
from app.task_handlers.studio.studio_handler import StudioEncodingHandler
from app.task_handlers.transcription.transcription_handler import TranscriptionHandler


def _make_task_request(task_type: str, source_url: str, parameters: Dict[str, Any]) -> TaskRequest:
    return TaskRequest(
        task_id=f"task-{task_type}-timeout",
        etab_name="UM",
        app_name="TestApp",
        task_type=task_type,
        source_url=source_url,
        parameters=parameters,
        notify_url="http://manager/callback",
    )


def test_external_script_timeout_default(monkeypatch):
    monkeypatch.delenv("EXTERNAL_SCRIPT_TIMEOUT_SECONDS", raising=False)
    cfg = Config()
    assert cfg.EXTERNAL_SCRIPT_TIMEOUT_SECONDS == 18000


def test_external_script_timeout_env_override(monkeypatch):
    monkeypatch.setenv("EXTERNAL_SCRIPT_TIMEOUT_SECONDS", "3600")
    cfg = Config()
    assert cfg.EXTERNAL_SCRIPT_TIMEOUT_SECONDS == 3600


def test_external_script_timeout_non_positive_uses_default(monkeypatch):
    monkeypatch.setenv("EXTERNAL_SCRIPT_TIMEOUT_SECONDS", "0")
    cfg = Config()
    assert cfg.EXTERNAL_SCRIPT_TIMEOUT_SECONDS == 18000


def test_encoding_handler_uses_configured_external_script_timeout(monkeypatch):
    timeout_value = 1234
    monkeypatch.setattr(config, "EXTERNAL_SCRIPT_TIMEOUT_SECONDS", timeout_value)

    handler = VideoEncodingHandler()
    recorded: dict[str, int] = {}

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )

    def fake_run_external_script(script_path: Path, args: list[str], timeout: int = 0):
        recorded["timeout"] = timeout
        return {"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(handler, "run_external_script", fake_run_external_script)

    request = _make_task_request("encoding", "https://example.org/sample.mp4", {})
    result = handler.execute_task(request.task_id, request)

    assert result["success"] is True
    assert recorded["timeout"] == timeout_value


def test_transcription_handler_uses_configured_external_script_timeout(monkeypatch):
    timeout_value = 2345
    monkeypatch.setattr(config, "EXTERNAL_SCRIPT_TIMEOUT_SECONDS", timeout_value)

    handler = TranscriptionHandler()
    recorded: dict[str, int] = {}

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )

    def fake_run_external_script(script_path: Path, args: list[str], timeout: int = 0):
        recorded["timeout"] = timeout
        return {"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(handler, "run_external_script", fake_run_external_script)

    request = _make_task_request("transcription", "https://example.org/sample.mp4", {})
    result = handler.execute_task(request.task_id, request)

    assert result["success"] is True
    assert recorded["timeout"] == timeout_value


def test_studio_handler_uses_configured_external_script_timeout(monkeypatch):
    timeout_value = 3456
    monkeypatch.setattr(config, "EXTERNAL_SCRIPT_TIMEOUT_SECONDS", timeout_value)

    handler = StudioEncodingHandler()
    recorded: list[int] = []

    def fake_run_external_script(script_path: Path, args: list[str], timeout: int = 0):
        recorded.append(timeout)
        if script_path.name == "studio.py":
            base_dir = Path(args[args.index("--base-dir") + 1])
            work_dir = args[args.index("--work-dir") + 1]
            output_file = args[args.index("--output-file") + 1]
            output_path = base_dir / work_dir / output_file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"studio-base")
        return {"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(handler, "run_external_script", fake_run_external_script)

    request = _make_task_request("studio", "https://example.org/mediapackage.xml", {})
    result = handler.execute_task(request.task_id, request)

    assert result["success"] is True
    assert recorded == [timeout_value, timeout_value]


def test_studio_retry_cpu_uses_configured_external_script_timeout(monkeypatch, tmp_path):
    timeout_value = 4567
    monkeypatch.setattr(config, "ENCODING_TYPE", "GPU")
    monkeypatch.setattr(config, "EXTERNAL_SCRIPT_TIMEOUT_SECONDS", timeout_value)

    handler = StudioEncodingHandler()
    recorded: dict[str, int] = {}

    def fake_run_external_script(script_path: Path, args: list[str], timeout: int = 0):
        recorded["timeout"] = timeout
        return {"success": True, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(handler, "run_external_script", fake_run_external_script)

    request = _make_task_request("studio", "https://example.org/mediapackage.xml", {})
    retry_result = handler._retry_studio_cpu(
        studio_script=handler.scripts_dir / "studio.py",
        task_request=request,
        workspace=tmp_path,
        work_dir="output",
        output_file="studio_base.mp4",
        studio_result={"success": False, "returncode": 1, "stdout": "", "stderr": ""},
    )

    assert retry_result is not None
    assert retry_result["success"] is True
    assert recorded["timeout"] == timeout_value
