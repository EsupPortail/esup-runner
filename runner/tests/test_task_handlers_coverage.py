import io
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app.core.config as config_module
import app.task_handlers as task_handlers_pkg
import app.task_handlers.base_handler as base_handler_module
from app.core.config import config
from app.models.models import TaskRequest
from app.task_handlers import TaskHandlerManager
from app.task_handlers.base_handler import BaseTaskHandler, _ffmpeg_buildconf_text
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler
from app.task_handlers.studio.studio_handler import StudioEncodingHandler
from app.task_handlers.transcription.transcription_handler import TranscriptionHandler


class _ConcreteBaseHandler(BaseTaskHandler):
    task_type = "dummy"

    def validate_parameters(self, parameters: dict[str, Any]) -> bool:
        return BaseTaskHandler.validate_parameters(self, parameters)  # type: ignore[return-value]

    def execute_task(self, task_id: str, task_request: TaskRequest) -> dict[str, Any]:
        return BaseTaskHandler.execute_task(self, task_id, task_request)  # type: ignore[return-value]


def _task_request(
    task_type: str, source_url: str, parameters: dict[str, Any] | None = None
) -> TaskRequest:
    return TaskRequest(
        task_id=f"task-{task_type}",
        etab_name="UM",
        app_name="TestApp",
        task_type=task_type,
        source_url=source_url,
        parameters=parameters or {},
        notify_url="http://manager/callback",
    )


def test_task_handler_manager_handles_import_error_and_empty_accessors(monkeypatch, capsys):
    monkeypatch.setattr(
        task_handlers_pkg.pkgutil,
        "iter_modules",
        lambda _path: [(None, "broken_handler", True), (None, "plain_module", False)],
    )

    def _raise_import_error(_name: str, _package: str):
        raise ImportError("import failed")

    monkeypatch.setattr(task_handlers_pkg.importlib, "import_module", _raise_import_error)

    manager = TaskHandlerManager()

    assert manager.get_handler("missing") is None
    assert manager.list_handlers() == {}
    assert "Failed to load handler broken_handler" in capsys.readouterr().out


def test_task_handler_manager_registers_discovered_handler(monkeypatch):
    class DummyHandler:
        task_type = "dummy"

        @classmethod
        def get_description(cls) -> str:
            return "Dummy handler"

    dummy_module = SimpleNamespace(get_handler=lambda: DummyHandler)

    monkeypatch.setattr(
        task_handlers_pkg.pkgutil,
        "iter_modules",
        lambda _path: [(None, "dummy_pkg", True)],
    )
    monkeypatch.setattr(
        task_handlers_pkg.importlib, "import_module", lambda _name, _package: dummy_module
    )

    manager = TaskHandlerManager()

    assert manager.get_handler("dummy") is DummyHandler
    assert manager.list_handlers() == {"dummy": "Dummy handler"}


def test_base_handler_abstract_defaults_and_description():
    handler = _ConcreteBaseHandler()
    request = _task_request("dummy", "https://example.org/video.mp4")

    assert handler.get_invalid_parameters({"unexpected": 1}) == []
    assert handler.validate_parameters({}) is None
    assert handler.execute_task("task-id", request) is None
    assert _ConcreteBaseHandler.get_description() == "Base task handler for dummy tasks"


def test_ffmpeg_buildconf_text_handles_exception(monkeypatch):
    _ffmpeg_buildconf_text.cache_clear()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert _ffmpeg_buildconf_text() == ""


def test_base_handler_cleanup_workspace_when_directory_exists(tmp_path):
    handler = _ConcreteBaseHandler()
    handler.workspace_dir = tmp_path / "workspace"
    handler.workspace_dir.mkdir(parents=True)

    handler.cleanup_workspace()
    handler.cleanup_workspace()

    assert not handler.workspace_dir.exists()


def test_base_handler_run_external_script_paths(monkeypatch, tmp_path):
    handler = _ConcreteBaseHandler()
    script_path = tmp_path / "script.py"
    script_path.write_text("print('ok')", encoding="utf-8")

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: _Done())
    ok = handler.run_external_script(script_path, args=["--x", 1], timeout=5)
    assert ok["success"] is True

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    timeout = handler.run_external_script(script_path, args=[], timeout=1)
    assert timeout["success"] is False
    assert "timeout" in timeout["error"].lower()

    def _raise_error(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(subprocess, "run", _raise_error)
    failed = handler.run_external_script(script_path, args=[], timeout=1)
    assert failed["success"] is False
    assert "execution failed" in failed["error"]

    missing = handler.run_external_script(tmp_path / "missing.py", args=[], timeout=1)
    assert missing["success"] is False
    assert "Script not found" in missing["error"]


def test_base_handler_log_ffmpeg_build_warnings(monkeypatch):
    handler = _ConcreteBaseHandler()
    warnings: list[str] = []
    monkeypatch.setattr(handler.logger, "warning", lambda msg: warnings.append(msg))

    monkeypatch.setattr(base_handler_module, "_ffmpeg_buildconf_text", lambda: "")
    handler.log_ffmpeg_build_warnings(for_webm=True)
    assert warnings == []

    monkeypatch.setattr(base_handler_module, "_ffmpeg_buildconf_text", lambda: "--disable-x86asm")
    handler.log_ffmpeg_build_warnings(for_webm=True)

    assert any("libvpx" in msg for msg in warnings)
    assert any("--disable-x86asm" in msg for msg in warnings)


def test_base_handler_build_execution_env_applies_cuda_and_handles_errors(monkeypatch):
    handler = _ConcreteBaseHandler()
    fake_cfg = SimpleNamespace(
        ENCODING_TYPE="GPU",
        GPU_CUDA_VISIBLE_DEVICES="0,1",
        GPU_CUDA_DEVICE_ORDER="PCI_BUS_ID",
        GPU_CUDA_PATH="/usr/local/cuda-test",
    )
    monkeypatch.setattr(config_module, "config", fake_cfg)

    env = handler._build_execution_env()
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["PATH"].startswith("/usr/local/cuda-test/bin:")

    def _raise_apply(_env: dict[str, str], _cfg: Any) -> None:
        raise RuntimeError("cuda env failed")

    monkeypatch.setattr(handler, "_apply_cuda_environment", _raise_apply)
    env_with_error = handler._build_execution_env()
    assert isinstance(env_with_error, dict)


def test_base_handler_download_source_file_error_paths(monkeypatch, tmp_path):
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        def __init__(self, status_code: int, headers: dict[str, str]):
            self.status_code = status_code
            self.headers = headers
            self.raw = io.BytesIO(b"payload")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Session404:
        def get(self, *_args, **_kwargs):
            return _Response(404, {})

    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: _Session404())
    not_found = handler.download_source_file("https://example.org/a.mp4", str(dest_file))
    assert not_found["success"] is False
    assert "was not found" in not_found["error"]

    class _SessionTooLarge:
        def get(self, *_args, **_kwargs):
            size_bytes = 2 * 1024 * 1024 * 1024
            return _Response(200, {"Content-Length": str(size_bytes)})

    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: _SessionTooLarge())
    monkeypatch.setattr(config, "MAX_VIDEO_SIZE_GB", 1)
    too_large = handler.download_source_file("https://example.org/a.mp4", str(dest_file))
    assert too_large["success"] is False
    assert "exceeds the maximum allowed size" in too_large["error"]

    class _SessionCrash:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: _SessionCrash())
    crashed = handler.download_source_file("https://example.org/a.mp4", str(dest_file))
    assert crashed["success"] is False
    assert "Impossible to download" in crashed["error"]


def test_encoding_handler_execute_task_error_paths(monkeypatch, tmp_path):
    handler = VideoEncodingHandler()

    missing_name = _task_request("encoding", "https://example.org", {})
    res_missing_name = handler.execute_task("encoding-missing-name", missing_name)
    assert res_missing_name["success"] is False
    assert "valid filename" in res_missing_name["error"]

    not_video = _task_request("encoding", "https://example.org/file.txt", {})
    res_not_video = handler.execute_task("encoding-not-video", not_video)
    assert res_not_video["success"] is False
    assert "valid video file" in res_not_video["error"]

    called: list[bool] = []
    monkeypatch.setattr(
        handler, "log_ffmpeg_build_warnings", lambda *, for_webm=False: called.append(for_webm)
    )
    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": False, "error": "download failed"},
    )
    webm_req = _task_request("encoding", "https://example.org/video.webm", {})
    res_download_error = handler.execute_task("encoding-download-error", webm_req)
    assert res_download_error["success"] is False
    assert "download failed" in res_download_error["error"]
    assert called == [True]

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )
    handler.scripts_dir = tmp_path / "missing-scripts"
    handler.scripts_dir.mkdir(parents=True, exist_ok=True)
    mp4_req = _task_request("encoding", "https://example.org/video.mp4", {})
    res_missing_script = handler.execute_task("encoding-missing-script", mp4_req)
    assert res_missing_script["success"] is False
    assert "No script available" in res_missing_script["error"]

    monkeypatch.setattr(
        handler, "prepare_workspace", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res_exception = handler.execute_task("encoding-exception", mp4_req)
    assert res_exception["success"] is False
    assert res_exception["error"] == "boom"


def test_encoding_handler_build_script_arguments_gpu_and_error_extractors(monkeypatch):
    handler = VideoEncodingHandler()
    monkeypatch.setattr(config, "ENCODING_TYPE", "GPU")
    monkeypatch.setattr(config, "GPU_HWACCEL_DEVICE", 2)
    monkeypatch.setattr(config, "GPU_CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(config, "GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    monkeypatch.setattr(config, "GPU_CUDA_PATH", "/usr/local/cuda-test")

    args = handler._build_script_arguments(
        parameters={"video_id": "x"},
        base_dir="/tmp/base",
        input_file="in.mp4",
        work_dir="output",
    )
    assert "--hwaccel-device" in args
    assert "--cuda-visible-devices" in args
    assert "--cuda-device-order" in args
    assert "--cuda-path" in args

    assert (
        handler._extract_script_error({"stderr": "", "stdout": "", "returncode": 7})
        == "Encoding failed (exit code 7)"
    )
    assert (
        handler._extract_script_error({"stderr": " \n", "stdout": " \n", "returncode": 0})
        == "Encoding failed"
    )
    assert (
        handler._extract_script_error({"stderr": "\nline1\nline2\n", "stdout": "", "returncode": 1})
        == "line2"
    )

    assert VideoEncodingHandler.get_description() == "Video encoding handler"


def test_transcription_handler_execute_task_error_paths(monkeypatch, tmp_path):
    handler = TranscriptionHandler()

    missing_name = _task_request("transcription", "https://example.org", {})
    res_missing_name = handler.execute_task("transcription-missing-name", missing_name)
    assert res_missing_name["success"] is False
    assert "valid filename" in res_missing_name["error"]

    not_media = _task_request("transcription", "https://example.org/file.txt", {})
    res_not_media = handler.execute_task("transcription-not-media", not_media)
    assert res_not_media["success"] is False
    assert "valid media file" in res_not_media["error"]

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": False, "error": "cannot download"},
    )
    mp4_req = _task_request("transcription", "https://example.org/video.mp4", {})
    res_download_error = handler.execute_task("transcription-download-error", mp4_req)
    assert res_download_error["success"] is False
    assert "cannot download" in res_download_error["error"]

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )
    handler.scripts_dir = tmp_path / "missing-scripts"
    handler.scripts_dir.mkdir(parents=True, exist_ok=True)
    res_missing_script = handler.execute_task("transcription-missing-script", mp4_req)
    assert res_missing_script["success"] is False
    assert "Script not found" in res_missing_script["error"]

    monkeypatch.setattr(
        handler, "prepare_workspace", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res_exception = handler.execute_task("transcription-exception", mp4_req)
    assert res_exception["success"] is False
    assert res_exception["error"] == "boom"

    assert TranscriptionHandler.get_description() == "Transcription handler"


def test_studio_handler_execute_task_error_paths(monkeypatch, tmp_path):
    handler = StudioEncodingHandler()
    request = _task_request("studio", "https://example.org/mediapackage.xml", {})

    workspace = tmp_path / "workspace"
    (workspace / "output").mkdir(parents=True)

    missing_base_video = workspace / "output" / "studio_base.mp4"
    monkeypatch.setattr(handler, "prepare_workspace", lambda: workspace)
    monkeypatch.setattr(
        handler,
        "_generate_base_video",
        lambda *args, **kwargs: ({"success": True}, missing_base_video),
    )
    res_missing_base = handler.execute_task("studio-missing-base", request)
    assert res_missing_base["success"] is False
    assert "Base studio video not found" in res_missing_base["error"]

    missing_base_video.write_bytes(b"base-video")
    monkeypatch.setattr(
        handler,
        "_prepare_encoding_input",
        lambda *args, **kwargs: {
            "success": False,
            "error": "prepare failed",
            "task_type": "studio",
        },
    )
    res_prepare_error = handler.execute_task("studio-prepare-error", request)
    assert res_prepare_error["success"] is False
    assert res_prepare_error["error"] == "prepare failed"

    monkeypatch.setattr(
        handler, "prepare_workspace", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res_exception = handler.execute_task("studio-exception", request)
    assert res_exception["success"] is False
    assert res_exception["error"] == "boom"


def test_studio_handler_generation_retry_and_logging_paths(monkeypatch, tmp_path):
    handler = StudioEncodingHandler()
    request = _task_request("studio", "https://example.org/mediapackage.xml", {})
    workspace = tmp_path / "workspace"
    (workspace / "output").mkdir(parents=True)

    monkeypatch.setattr(
        handler,
        "run_external_script",
        lambda *args, **kwargs: {"success": True, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        handler,
        "_append_stage_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log failed")),
    )
    success_result, _ = handler._generate_base_video(
        request, workspace, "output", "studio_base.mp4"
    )
    assert success_result["success"] is True

    monkeypatch.setattr(
        handler,
        "run_external_script",
        lambda *args, **kwargs: {
            "success": False,
            "returncode": 12,
            "stdout": "",
            "stderr": "fail",
        },
    )
    monkeypatch.setattr(
        handler, "_retry_studio_cpu", lambda *args, **kwargs: {"success": True, "retry": True}
    )
    retry_result, _ = handler._generate_base_video(request, workspace, "output", "studio_base.mp4")
    assert retry_result["success"] is True
    assert retry_result["retry"] is True

    handler_retry_failure = StudioEncodingHandler()
    monkeypatch.setattr(config, "ENCODING_TYPE", "GPU")
    monkeypatch.setattr(
        handler_retry_failure,
        "run_external_script",
        lambda *args, **kwargs: {
            "success": False,
            "returncode": 9,
            "stdout": "",
            "stderr": "retry failed",
        },
    )
    monkeypatch.setattr(
        handler_retry_failure,
        "_append_stage_log",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log failed")),
    )
    failed_retry = handler_retry_failure._retry_studio_cpu(
        studio_script=handler_retry_failure.scripts_dir / "studio.py",
        task_request=request,
        workspace=workspace,
        work_dir="output",
        output_file="studio_base.mp4",
        studio_result={"success": False, "returncode": 8, "stdout": "", "stderr": "first failed"},
    )
    assert failed_retry is not None
    assert failed_retry["success"] is False
    assert "studio_first" in failed_retry["script_output"]
    assert "studio_retry" in failed_retry["script_output"]


def test_studio_handler_helpers_cover_remaining_branches(monkeypatch, tmp_path):
    handler = StudioEncodingHandler()

    log_path = tmp_path / "output" / "encoding.log"
    handler._append_stage_log(
        log_path=log_path,
        stage="TEST",
        script_path=Path("/tmp/studio.py"),
        args=["--x", "1"],
        result={"returncode": 1, "stdout": "", "stderr": "boom"},
    )
    assert "--- stderr ---" in log_path.read_text(encoding="utf-8")

    info_logs: list[str] = []
    monkeypatch.setattr(handler.logger, "info", lambda msg: info_logs.append(msg))
    handler._log_studio_selected_mode(
        {"stdout": "[GPU_ENC_ONLY] trying\n[CPU] selected\n"}, context="(retry)"
    )
    assert any("CPU" in msg for msg in info_logs)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    base_video = workspace / "base.mp4"
    base_video.write_bytes(b"base")
    existing_link = workspace / "studio_base.mp4"
    existing_link.write_text("old", encoding="utf-8")
    assert (
        handler._prepare_encoding_input(base_video, workspace, "studio_base.mp4", {"success": True})
        is None
    )
    assert existing_link.is_symlink()

    broken_link_path = workspace / "studio_base_copy.mp4"

    def _symlink_fail(self, target, target_is_directory=False):  # type: ignore[no-untyped-def]
        raise OSError("symlink disabled")

    monkeypatch.setattr(Path, "symlink_to", _symlink_fail)
    monkeypatch.setattr(
        shutil, "copy2", lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes())
    )
    copy_result = handler._prepare_encoding_input(
        base_video, workspace, "studio_base_copy.mp4", {"success": True}
    )
    assert copy_result is None
    assert broken_link_path.exists()

    monkeypatch.setattr(
        shutil, "copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed"))
    )
    copy_error = handler._prepare_encoding_input(
        base_video, workspace, "studio_base_copy_error.mp4", {"success": True}
    )
    assert copy_error is not None
    assert copy_error["success"] is False
    assert "Failed to prepare encoding input" in copy_error["error"]

    result_with_error = handler._build_results(
        enc_result={"success": False},
        studio_result={"success": True},
        output_dir=tmp_path,
        base_video_path=base_video,
    )
    assert result_with_error["success"] is False
    assert result_with_error["error"] == "Encoding failed"

    monkeypatch.setattr(config, "ENCODING_TYPE", "CPU")
    args = handler._build_studio_args(
        xml_url="https://example.org/mp.xml",
        base_dir="/tmp/base",
        work_dir="output",
        output_file="studio_base.mp4",
        presenter="presenter-track",
        parameters={"studio_allow_nvenc": False},
    )
    assert "--presenter" in args
    assert "--studio-allow-nvenc" in args
    assert "--force-cpu" in args

    class _BadBool:
        def __bool__(self):
            raise RuntimeError("bool failed")

    assert handler._get_bool_param("no") is False
    assert handler._get_bool_param(_BadBool()) is False

    monkeypatch.setattr(config, "ENCODING_TYPE", "GPU")
    monkeypatch.setattr(config, "GPU_HWACCEL_DEVICE", 0)
    monkeypatch.setattr(config, "GPU_CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(config, "GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    monkeypatch.setattr(config, "GPU_CUDA_PATH", "/usr/local/cuda-test")
    enc_args = handler._build_encoding_args(
        parameters={"custom_flag": "abc"},
        base_dir="/tmp/base",
        input_file="in.mp4",
        work_dir="output",
    )
    assert "--hwaccel-device" in enc_args
    assert "--cuda-visible-devices" in enc_args
    assert "--custom_flag" in enc_args

    assert StudioEncodingHandler.get_description() == "Studio encoding handler"
