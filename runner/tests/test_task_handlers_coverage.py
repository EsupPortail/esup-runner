"""Validates task handler manager discovery, registration, and base handler abstract interface."""

import io
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.config as config_module
import app.task_handlers as task_handlers_pkg
import app.task_handlers.base_handler as base_handler_module
from app.core.config import config
from app.core.media_denylist import MediaDeniedError
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


class _DownloadRetryExtensionResponse:
    def __init__(self, payload: bytes):
        self.status_code = 200
        self.headers = {
            "Content-Length": "0" if not payload else str(len(payload)),
            "Content-Type": "text/html; charset=utf-8" if not payload else "audio/mpeg",
            "Last-Modified": "Tue, 03 Mar 2026 08:02:13 GMT",
        }
        self._payload = payload

    def iter_content(self, chunk_size: int = 8192):
        stream = io.BytesIO(self._payload)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _SessionSourceReadyOnSecondTry:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _DownloadRetryExtensionResponse(b"")
        return _DownloadRetryExtensionResponse(b"payload")


class _LoggerCapture:
    def __init__(self):
        self.info_messages: list[str] = []

    def info(self, message, *args):
        self.info_messages.append(message % args if args else str(message))

    @staticmethod
    def warning(*_args, **_kwargs):
        return None


def test_task_handler_manager_handles_import_error_and_empty_accessors(monkeypatch, capsys):
    """Validate Task handler manager handles import error and empty accessors."""
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
    """Validate Task handler manager registers discovered handler."""

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
    """Validate Base handler abstract defaults and description."""
    handler = _ConcreteBaseHandler()
    request = _task_request("dummy", "https://example.org/video.mp4")

    assert handler.get_invalid_parameters({"unexpected": 1}) == []
    assert handler.validate_parameters({}) is None
    assert handler.execute_task("task-id", request) is None
    assert _ConcreteBaseHandler.get_description() == "Base task handler for dummy tasks"


def test_ffmpeg_buildconf_text_handles_exception(monkeypatch):
    """Validate Ffmpeg buildconf text handles exception."""
    _ffmpeg_buildconf_text.cache_clear()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ffmpeg unavailable")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert _ffmpeg_buildconf_text() == ""


def test_ffmpeg_buildconf_text_returns_stdout(monkeypatch):
    """Validate Ffmpeg buildconf text returns stdout."""
    _ffmpeg_buildconf_text.cache_clear()

    class _Result:
        stdout = "--enable-libvpx"

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: _Result())

    assert _ffmpeg_buildconf_text() == "--enable-libvpx"


def test_base_handler_cleanup_workspace_when_directory_exists(tmp_path):
    """Validate Base handler cleanup workspace when directory exists."""
    handler = _ConcreteBaseHandler()
    handler.workspace_dir = tmp_path / "workspace"
    handler.workspace_dir.mkdir(parents=True)

    handler.cleanup_workspace()
    handler.cleanup_workspace()

    assert not handler.workspace_dir.exists()


def test_base_handler_run_external_script_paths(monkeypatch, tmp_path):
    """Validate Base handler run external script paths."""
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
    """Validate Base handler log ffmpeg build warnings."""
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


def test_base_handler_validate_downloaded_media_against_denylist(monkeypatch, tmp_path):
    """Validate Base handler validate downloaded media against denylist."""
    handler = _ConcreteBaseHandler()
    media = tmp_path / "sample.avi"
    media.write_bytes(b"RIFF" + b"\x40\x00\x00\x00" + b"AVI " + b"\x00" * 12 + b"MAGY")

    monkeypatch.setattr(base_handler_module.config, "MEDIA_CODEC_DENYLIST", [])
    handler.validate_downloaded_media_against_denylist(media)

    monkeypatch.setattr(base_handler_module.config, "MEDIA_CODEC_DENYLIST", ["magicyuv"])

    with pytest.raises(MediaDeniedError, match="MagicYUV codec"):
        handler.validate_downloaded_media_against_denylist(media)


def test_base_handler_build_execution_env_applies_cuda_and_handles_errors(monkeypatch):
    """Validate Base handler build execution env applies cuda and handles errors."""
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
    """Validate Base handler download source file error paths."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        def __init__(self, status_code: int, headers: dict[str, str], payload: bytes = b"payload"):
            self.status_code = status_code
            self.headers = headers
            self._payload = payload

        def iter_content(self, chunk_size: int = 8192):
            stream = io.BytesIO(self._payload)
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk

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
    monkeypatch.setattr(base_handler_module.time, "sleep", lambda *_args, **_kwargs: None)
    crashed = handler.download_source_file("https://example.org/a.mp4", str(dest_file))
    assert crashed["success"] is False
    assert "Impossible to download" in crashed["error"]


def test_base_handler_download_source_file_retries_then_succeeds(monkeypatch, tmp_path):
    """Validate Base handler download source file retries then succeeds."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        def __init__(self, status_code: int, headers: dict[str, str], payload: bytes):
            self.status_code = status_code
            self.headers = headers
            self._payload = payload

        def iter_content(self, chunk_size: int = 8192):
            stream = io.BytesIO(self._payload)
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _FlakySession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return _Response(200, {"Content-Length": "7"}, b"")
            return _Response(200, {"Content-Length": "7"}, b"payload")

    session = _FlakySession()
    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_DELAY_SECONDS", 0.5)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_BACKOFF_FACTOR", 3.0)
    sleep_delays: list[float] = []
    monkeypatch.setattr(base_handler_module.time, "sleep", sleep_delays.append)

    result = handler.download_source_file("https://example.org/a.mp4", str(dest_file))

    assert result["success"] is True
    assert session.calls == 2
    assert sleep_delays == [0.5]
    assert dest_file.read_bytes() == b"payload"


def test_base_handler_download_source_file_caps_retry_delay(monkeypatch, tmp_path):
    """Validate Base handler download source file applies retry delay cap."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        status_code = 200
        headers = {
            "Content-Length": "7",
            "Content-Type": "audio/mpeg",
            "Last-Modified": "Wed, 27 May 2026 11:13:17 GMT",
        }

        @staticmethod
        def iter_content(chunk_size: int = 8192):
            _ = chunk_size
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _AlwaysEmptySession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return _Response()

    session = _AlwaysEmptySession()
    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 6)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_DELAY_SECONDS", 2.0)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_BACKOFF_FACTOR", 3.0)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_MAX_DELAY_SECONDS", 5.0)

    sleep_delays: list[float] = []
    monkeypatch.setattr(base_handler_module.time, "sleep", sleep_delays.append)

    result = handler.download_source_file("https://example.org/a.mp4", str(dest_file))

    assert result["success"] is False
    assert session.calls == 6
    assert sleep_delays == [2.0, 5.0, 5.0, 5.0, 5.0]


def test_base_handler_download_source_file_extends_attempts_for_empty_html(monkeypatch, tmp_path):
    """Validate empty HTML placeholders get an extended retry budget."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        status_code = 200
        headers = {
            "Content-Length": "0",
            "Content-Type": "text/html; charset=utf-8",
            "Last-Modified": "Tue, 03 Mar 2026 08:02:13 GMT",
        }

        @staticmethod
        def iter_content(chunk_size: int = 8192):
            _ = chunk_size
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _PlaceholderSession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return _Response()

    session = _PlaceholderSession()
    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS_WHEN_SOURCE_NOT_READY", 5)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_DELAY_SECONDS", 1.0)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_BACKOFF_FACTOR", 2.0)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_RETRY_MAX_DELAY_SECONDS", 10.0)

    sleep_delays: list[float] = []
    monkeypatch.setattr(base_handler_module.time, "sleep", sleep_delays.append)

    result = handler.download_source_file("https://example.org/a.mp3", str(dest_file))

    assert result["success"] is False
    assert session.calls == 5
    assert sleep_delays == [1.0, 2.0, 4.0, 8.0]
    assert "Downloaded file is empty" in result["error"]


def test_base_handler_download_source_file_logs_retry_extension(monkeypatch, tmp_path):
    """Validate retry-budget extension emits an explicit info log entry."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    session = _SessionSourceReadyOnSecondTry()
    logger_capture = _LoggerCapture()

    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS_WHEN_SOURCE_NOT_READY", 2)
    monkeypatch.setattr(base_handler_module.time, "sleep", lambda *_args, **_kwargs: None)
    handler.logger = logger_capture

    result = handler.download_source_file("https://example.org/a.mp3", str(dest_file))

    assert result["success"] is True
    assert dest_file.read_bytes() == b"payload"
    assert len(logger_capture.info_messages) == 1
    assert "at attempt 1/1" in logger_capture.info_messages[0]
    assert "extending download retry budget from 1 to 2 attempts" in logger_capture.info_messages[0]


def test_base_handler_download_source_file_retries_and_fails_on_truncated_payload(
    monkeypatch, tmp_path
):
    """Validate Base handler download source file retries and fails on truncated payload."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        def __init__(self, status_code: int, headers: dict[str, str], payload: bytes):
            self.status_code = status_code
            self.headers = headers
            self._payload = payload

        def iter_content(self, chunk_size: int = 8192):
            stream = io.BytesIO(self._payload)
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _AlwaysTruncatedSession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return _Response(200, {"Content-Length": "7"}, b"short")

    session = _AlwaysTruncatedSession()
    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 3)

    result = handler.download_source_file("https://example.org/a.mp4", str(dest_file))

    assert result["success"] is False
    assert session.calls == 3
    assert "Incomplete download" in result["error"]
    assert not dest_file.exists()


def test_base_handler_download_source_file_reports_empty_response_context(monkeypatch, tmp_path):
    """Validate empty downloads include response context and normalized punctuation."""
    handler = _ConcreteBaseHandler()
    dest_file = tmp_path / "download.bin"

    class _Response:
        status_code = 200
        headers = {
            "Content-Length": "123",
            "Content-Type": "audio/mpeg",
            "Last-Modified": "Wed, 27 May 2026 11:13:17 GMT",
        }

        @staticmethod
        def iter_content(chunk_size: int = 8192):
            _ = chunk_size
            return iter(())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _EmptySession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return _Response()

    session = _EmptySession()
    monkeypatch.setattr(base_handler_module.requests, "Session", lambda: session)
    monkeypatch.setattr(base_handler_module.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(base_handler_module, "_DOWNLOAD_MAX_ATTEMPTS", 2)

    result = handler.download_source_file("https://example.org/a.mp3", str(dest_file))

    assert result["success"] is False
    assert session.calls == 2
    assert "Downloaded file is empty" in result["error"]
    assert "Content-Length=123" in result["error"]
    assert "Content-Type=audio/mpeg" in result["error"]
    assert "Last-Modified=Wed, 27 May 2026 11:13:17 GMT" in result["error"]
    assert not result["error"].endswith("..")


def test_base_handler_download_helpers_cover_remaining_branches(tmp_path):
    """Validate Base handler download helpers cover remaining branches."""
    handler = _ConcreteBaseHandler()

    class _BrokenPath:
        @staticmethod
        def exists():
            raise RuntimeError("cannot stat")

        @staticmethod
        def unlink():
            raise RuntimeError("cannot unlink")

    handler._cleanup_partial_download_file(_BrokenPath())  # type: ignore[arg-type]

    class _ResponseWithEmptyChunk:
        @staticmethod
        def iter_content(chunk_size: int = 8192):
            _ = chunk_size
            yield b""
            yield b"abc"

    target = tmp_path / "download.part"
    bytes_written = handler._stream_response_to_file(_ResponseWithEmptyChunk(), target, 8)
    assert bytes_written == 3
    assert target.read_bytes() == b"abc"

    assert handler._parse_expected_download_size(None) is None
    assert handler._validate_expected_download_size(None) is None


def test_base_handler_download_source_file_once_handles_non_404_status(tmp_path):
    """Validate Base handler download source file once handles non 404 status."""
    handler = _ConcreteBaseHandler()
    part_path = tmp_path / "download.part"

    class _Response:
        status_code = 500
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Session:
        @staticmethod
        def get(*_args, **_kwargs):
            return _Response()

    result = handler._download_source_file_once(
        session=_Session(),  # type: ignore[arg-type]
        source_url="https://example.org/a.mp4",
        part_path=part_path,
        chunk_size=1024,
    )
    assert result["success"] is False
    assert "HTTP 500" in result["error"]


def test_encoding_handler_execute_task_error_paths(monkeypatch, tmp_path):
    """Validate Encoding handler execute task error paths."""
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
    assert called == []

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )
    handler.entrypoints_dir = tmp_path / "missing-scripts"
    handler.entrypoints_dir.mkdir(parents=True, exist_ok=True)
    res_webm_missing_script = handler.execute_task("encoding-webm-missing-script", webm_req)
    assert res_webm_missing_script["success"] is False
    assert "No script available" in res_webm_missing_script["error"]
    assert called == [True]

    mp4_req = _task_request("encoding", "https://example.org/video.mp4", {})
    res_missing_script = handler.execute_task("encoding-missing-script", mp4_req)
    assert res_missing_script["success"] is False
    assert "No script available" in res_missing_script["error"]

    monkeypatch.setattr(
        handler,
        "validate_downloaded_media_against_denylist",
        lambda _input_path: (_ for _ in ()).throw(MediaDeniedError("Media rejected: denied")),
    )
    res_denied = handler.execute_task("encoding-denied", mp4_req)
    assert res_denied["success"] is False
    assert res_denied["error"] == "Media rejected: denied"

    monkeypatch.setattr(
        handler, "prepare_workspace", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res_exception = handler.execute_task("encoding-exception", mp4_req)
    assert res_exception["success"] is False
    assert res_exception["error"] == "boom"


def test_encoding_handler_fills_stdout_from_encoding_log_when_streams_are_empty(
    monkeypatch, tmp_path
):
    """Validate Encoding handler fills stdout from encoding log when streams are empty."""
    handler = VideoEncodingHandler()
    task_id = "encoding-log-fallback"

    monkeypatch.setattr(
        "app.task_handlers.encoding.encoding_handler.storage_manager.base_path", str(tmp_path)
    )

    entrypoints_dir = tmp_path / "scripts"
    entrypoints_dir.mkdir(parents=True, exist_ok=True)
    (entrypoints_dir / "encoding.py").write_text("print('unused')\n", encoding="utf-8")
    handler.entrypoints_dir = entrypoints_dir

    workspace = tmp_path / task_id
    output_dir = workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "encoding.log").write_text(
        "Encoding file: sample.mp4\n- End of encoding\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        handler,
        "download_source_file",
        lambda source_url, dest_file: {"success": True, "file_path": dest_file},
    )
    monkeypatch.setattr(
        handler,
        "run_external_script_for_task",
        lambda *args, **kwargs: {"success": True, "returncode": 0, "stdout": "", "stderr": ""},
    )

    request = _task_request("encoding", "https://example.org/sample.mp4", {})
    result = handler.execute_task(task_id, request)

    assert result["success"] is True
    assert "Encoding file: sample.mp4" in result["script_output"]["stdout"]
    assert result["script_output"]["stderr"] == ""


def test_encoding_handler_fill_empty_streams_from_encoding_log_fallback_branches(tmp_path):
    """Validate Encoding handler fill empty streams from encoding log fallback branches."""
    handler = VideoEncodingHandler()
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    non_dict_result = "not-a-dict"
    assert (
        handler._fill_empty_streams_from_encoding_log(non_dict_result, output_dir)
        == non_dict_result
    )

    already_has_stdout = {"success": True, "stdout": "already-captured", "stderr": ""}
    assert (
        handler._fill_empty_streams_from_encoding_log(already_has_stdout, output_dir)
        == already_has_stdout
    )

    (output_dir / "encoding.log").write_text(
        "ERROR ENCODING mp4 FOR FILE iphone.mov\n",
        encoding="utf-8",
    )
    failed_with_stdout = {
        "success": False,
        "returncode": 1,
        "stdout": "Loaded environment variables from: /opt/esup-runner/runner/.env",
        "stderr": (
            "2026-07-16 - runner - INFO - "
            "[storage_manager:_ensure_directory_exists:40] - "
            "Storage directory initialized: /tmp/esup-runner\n"
            "Encoding failed"
        ),
    }
    enriched_failure = handler._fill_empty_streams_from_encoding_log(failed_with_stdout, output_dir)
    assert "Loaded environment variables" not in enriched_failure["stdout"]
    assert "===== encoding.log =====" not in enriched_failure["stdout"]
    assert "ERROR ENCODING mp4" in enriched_failure["stdout"]
    assert "Storage directory initialized" not in enriched_failure["stderr"]
    assert enriched_failure["stderr"] == "Encoding failed"

    meaningful_stdout = {
        "success": True,
        "returncode": 0,
        "stdout": "FFmpeg preflight completed",
        "stderr": "",
    }
    enriched_meaningful_stdout = handler._fill_empty_streams_from_encoding_log(
        meaningful_stdout, output_dir
    )
    assert enriched_meaningful_stdout["stdout"].startswith("FFmpeg preflight completed")
    assert "===== encoding.log =====" in enriched_meaningful_stdout["stdout"]
    assert "ERROR ENCODING mp4" in enriched_meaningful_stdout["stdout"]

    (output_dir / "encoding.log").write_text(
        "Encoding file: sample.mp4\n- End of encoding\n",
        encoding="utf-8",
    )
    successful_with_startup_output = {
        "success": True,
        "returncode": 0,
        "stdout": (
            "Loaded environment variables from: /opt/esup-runner/runner/.env\n"
            "2026-07-16 - runner - INFO - "
            "[storage_manager:_ensure_directory_exists:40] - "
            "Storage directory initialized: /tmp/esup-runner"
        ),
        "stderr": "",
    }
    enriched_success = handler._fill_empty_streams_from_encoding_log(
        successful_with_startup_output, output_dir
    )
    assert "Loaded environment variables" not in enriched_success["stdout"]
    assert "Storage directory initialized" not in enriched_success["stdout"]
    assert "===== encoding.log =====" not in enriched_success["stdout"]
    assert "Encoding file: sample.mp4" in enriched_success["stdout"]

    already_merged = {
        **successful_with_startup_output,
        "stdout": enriched_success["stdout"],
    }
    assert (
        handler._fill_empty_streams_from_encoding_log(already_merged, output_dir) == already_merged
    )

    (output_dir / "encoding.log").unlink()
    empty_streams_result = {"success": True, "stdout": "", "stderr": ""}
    assert (
        handler._fill_empty_streams_from_encoding_log(empty_streams_result, output_dir)
        == empty_streams_result
    )

    startup_without_encoding_log = {
        "success": False,
        "stdout": "Loaded environment variables from: /runner/.env",
        "stderr": "A useful startup error",
    }
    assert handler._fill_empty_streams_from_encoding_log(
        startup_without_encoding_log, output_dir
    ) == {
        "success": False,
        "stdout": "",
        "stderr": "A useful startup error",
    }


def test_encoding_handler_build_script_arguments_gpu_and_error_extractors(monkeypatch):
    """Validate Encoding handler build script arguments gpu and error extractors."""
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
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": "--> get_info_video\nstream fps estimate: 50.000\n",
                "returncode": 1,
            }
        )
        == "Encoding failed (exit code 1)"
    )
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": "////////////////////\nERROR ENCODING m3u8 FOR FILE input.mp4\n",
                "returncode": 1,
            }
        )
        == "ERROR ENCODING m3u8 FOR FILE input.mp4"
    )
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": "",
                "returncode": "not-a-number",
            }
        )
        == "Encoding failed (exit code not-a-number)"
    )
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": "--> get_info_video\nstream fps estimate: 50.000\n",
                "returncode": -9,
            }
        )
        == "Encoding process was terminated by SIGKILL (return code -9)"
    )
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": "",
                "returncode": -999,
            }
        )
        == "Encoding process was terminated by signal 999 (return code -999)"
    )
    assert (
        handler._extract_script_error(
            {
                "stderr": "",
                "stdout": (
                    "--> get_info_video\n"
                    "stream fps estimate: 50.000\n"
                    "Error: Encoding aborted: input video duration is 0 seconds.\n"
                ),
                "returncode": 1,
            }
        )
        == "Encoding aborted: input video duration is 0 seconds."
    )

    assert VideoEncodingHandler.get_description() == "Video encoding handler"


def test_transcription_handler_execute_task_error_paths(monkeypatch, tmp_path):
    """Validate Transcription handler execute task error paths."""
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
    monkeypatch.setattr(
        handler,
        "_validate_input_media_with_ffprobe",
        lambda _input_path: "Input media pre-check failed",
    )
    res_invalid_input = handler.execute_task("transcription-invalid-input", mp4_req)
    assert res_invalid_input["success"] is False
    assert "Input media pre-check failed" in res_invalid_input["error"]

    ffprobe_called = {"value": False}
    monkeypatch.setattr(
        handler,
        "validate_downloaded_media_against_denylist",
        lambda _input_path: (_ for _ in ()).throw(MediaDeniedError("Media rejected: denied")),
    )
    monkeypatch.setattr(
        handler,
        "_validate_input_media_with_ffprobe",
        lambda _input_path: ffprobe_called.__setitem__("value", True),
    )
    res_denied = handler.execute_task("transcription-denied", mp4_req)
    assert res_denied["success"] is False
    assert res_denied["error"] == "Media rejected: denied"
    assert ffprobe_called["value"] is False

    monkeypatch.setattr(handler, "validate_downloaded_media_against_denylist", lambda _path: None)
    monkeypatch.setattr(handler, "_validate_input_media_with_ffprobe", lambda _input_path: None)
    handler.entrypoints_dir = tmp_path / "missing-scripts"
    handler.entrypoints_dir.mkdir(parents=True, exist_ok=True)
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


def test_transcription_handler_reclassifies_loading_weights_from_stderr():
    """Validate Transcription handler reclassifies loading weights from stderr."""
    handler = TranscriptionHandler()

    script_result = {
        "success": True,
        "returncode": 0,
        "stdout": "VTT written to: /tmp/output/subtitles.vtt",
        "stderr": (
            "Loading weights:   0%|          | 0/256 [00:00<?, ?it/s]\n"
            "Loading weights: 100%|##########| 256/256 [00:00<00:00, 682.86it/s]\n"
            "real warning line"
        ),
    }

    normalized = handler._reclassify_non_error_stderr(script_result)

    assert "VTT written to" in normalized["stdout"]
    assert "Loading weights:" in normalized["stdout"]
    assert "real warning line" not in normalized["stdout"]
    assert "real warning line" in normalized["stderr"]
    assert "Loading weights:" not in normalized["stderr"]


def test_transcription_handler_validate_input_media_with_ffprobe_paths(monkeypatch, tmp_path):
    """Validate Transcription handler validate input media with ffprobe paths."""
    handler = TranscriptionHandler()
    input_path = tmp_path / "input.mp3"
    input_path.write_bytes(b"fake")

    class _ProbeOK:
        returncode = 0
        stdout = "42.0\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: _ProbeOK())
    assert handler._validate_input_media_with_ffprobe(input_path) is None

    class _ProbeFail:
        returncode = 1
        stdout = ""
        stderr = "Invalid argument"

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: _ProbeFail())
    assert "Invalid argument" in (handler._validate_input_media_with_ffprobe(input_path) or "")

    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert handler._validate_input_media_with_ffprobe(input_path) is None

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ffprobe", timeout=15)
        ),
    )
    assert "timed out" in (handler._validate_input_media_with_ffprobe(input_path) or "")

    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert "boom" in (handler._validate_input_media_with_ffprobe(input_path) or "")

    class _ProbeFailNoDetails:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: _ProbeFailNoDetails())
    assert handler._validate_input_media_with_ffprobe(input_path) == (
        f"Input media pre-check failed for {input_path}"
    )


def test_transcription_handler_reclassify_keeps_non_loading_stderr_unchanged():
    """Validate Transcription handler reclassify keeps non loading stderr unchanged."""
    handler = TranscriptionHandler()
    script_result = {
        "success": True,
        "returncode": 0,
        "stdout": "VTT written to: /tmp/output/subtitles.vtt",
        "stderr": "real warning line",
    }

    normalized = handler._reclassify_non_error_stderr(script_result)

    assert normalized is script_result
    assert normalized["stdout"] == "VTT written to: /tmp/output/subtitles.vtt"
    assert normalized["stderr"] == "real warning line"


def test_transcription_handler_reclassify_moves_loading_weights_when_stdout_empty():
    """Validate Transcription handler reclassify moves loading weights when stdout empty."""
    handler = TranscriptionHandler()
    script_result = {
        "success": True,
        "returncode": 0,
        "stdout": "",
        "stderr": "Loading weights: 100%|##########| 256/256 [00:00<00:00, 682.86it/s]",
    }

    normalized = handler._reclassify_non_error_stderr(script_result)

    assert normalized["stdout"].startswith("Loading weights:")
    assert normalized["stderr"] == ""


def test_studio_handler_execute_task_error_paths(monkeypatch, tmp_path):
    """Validate Studio handler execute task error paths."""
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
    """Validate Studio handler generation retry and logging paths."""
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
        studio_script=handler_retry_failure.entrypoints_dir / "studio.py",
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
    """Validate Studio handler helpers cover remaining branches."""
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

    encoding_log = tmp_path / "encoding-stage.log"
    encoding_log.write_text("encoding stage output", encoding="utf-8")
    enriched = handler._fill_empty_encoding_stream_from_log(
        {"success": True, "stdout": "", "stderr": ""},
        encoding_log,
    )
    assert enriched["stdout"] == "encoding stage output"

    untouched = handler._fill_empty_encoding_stream_from_log(
        {"success": True, "stdout": "already", "stderr": ""},
        encoding_log,
    )
    assert untouched["stdout"] == "already"

    passthrough = handler._fill_empty_encoding_stream_from_log("not-a-dict", encoding_log)
    assert passthrough == "not-a-dict"

    empty_log = tmp_path / "encoding-empty.log"
    empty_log.write_text("   ", encoding="utf-8")
    unchanged = handler._fill_empty_encoding_stream_from_log(
        {"success": True, "stdout": "", "stderr": ""},
        empty_log,
    )
    assert unchanged["stdout"] == ""

    assert StudioEncodingHandler.get_description() == "Studio encoding handler"
