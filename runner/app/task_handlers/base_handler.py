# runner/app/task_handlers/base_handler.py
"""
Base task handler defining the interface for all task processors.
"""

import json
import os
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests  # type: ignore[import-untyped]

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.models.models import TaskRequest

logger = setup_default_logging()

_STDERR_ERROR_TOKENS = (
    "error",
    "warning",
    "warn",
    "fail",
    "invalid",
    "unable",
    "cannot",
    "can't",
    "exception",
    "traceback",
    "fatal",
    "critical",
    "denied",
    "forbidden",
    "not found",
    "no such file",
    "unknown",
    "deprecated",
    "non-monotonous",
)

# Internal technical download retry policy (not exposed through .env)
_DOWNLOAD_MAX_ATTEMPTS = 5
_DOWNLOAD_RETRY_DELAY_SECONDS = 2.0
_DOWNLOAD_RETRY_BACKOFF_FACTOR = 2.0
_DOWNLOAD_RETRY_MAX_DELAY_SECONDS = 30.0
_DOWNLOAD_MAX_ATTEMPTS_WHEN_SOURCE_NOT_READY = 12


class _SourceTemporarilyUnavailableError(ValueError):
    """Raised when the source endpoint returns an empty placeholder response."""


@lru_cache(maxsize=1)
def _ffmpeg_buildconf_text() -> str:
    """Return `ffmpeg -buildconf` output (cached per process).

    Used for non-blocking runtime warnings in runner logs.
    """
    import subprocess

    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-buildconf"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        return res.stdout or ""
    except Exception:
        return ""


class BaseTaskHandler(ABC):
    """
    Abstract base class for all task handlers.

    Defines the common interface and provides utility methods
    for task processing, file management, and result handling.
    """

    # Must be defined by subclasses
    task_type: str = "base"

    def __init__(self):
        """
        Initialize task handler with common configuration.
        """
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="task_"))
        self.logger = setup_default_logging()
        self.last_invalid_parameters: List[str] = []

    def get_invalid_parameters(self, parameters: Dict[str, Any]) -> List[str]:
        """Return the list of invalid parameter names for the current payload."""
        return []

    @abstractmethod
    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate task parameters before execution.

        Args:
            parameters: Task-specific parameters

        Returns:
            bool: True if parameters are valid
        """
        pass

    @abstractmethod
    def execute_task(self, task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
        """
        Execute the main task logic.

        Args:
            task_id: Unique task identifier
            parameters: Task-specific parameters
            input_files: List of input file paths
            output_dir: Directory for output files

        Returns:
            Dict containing task results and metadata
        """
        pass

    def prepare_workspace(self) -> Path:
        """
        Prepare workspace directory.

        Returns:
            Path to workspace directory
        """
        workspace = self.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)

        # Create output directory inside workspace
        work_dir = "output"
        output_dir = workspace / work_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        return workspace

    def cleanup_workspace(self) -> None:
        """
        Clean up temporary workspace directory.
        """
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir)
            self.logger.info(f"Cleaned up workspace: {self.workspace_dir}")

    def save_task_metadata(self, task_id: str, results: Dict[str, Any], output_dir: Path) -> Path:
        """
        Save task execution metadata as JSON.

        Args:
            task_id: Task identifier
            results: Task execution results
            output_dir: Directory to save metadata

        Returns:
            Path to metadata file
        """
        metadata = {
            "task_id": task_id,
            "task_type": self.task_type,
            "timestamp": self._get_timestamp(),
            "results": results,
        }

        metadata_file = output_dir / "task_metadata.json"
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=2)

        return metadata_file

    def _get_timestamp(self) -> str:
        """
        Get current timestamp in ISO format.

        Returns:
            str: ISO formatted timestamp
        """
        from datetime import datetime

        return datetime.now().isoformat()

    def _build_script_command(self, script_path: Path, args: List[str]) -> List[str]:
        """Build external script command with normalized string arguments."""
        import sys

        return [str(sys.executable), str(script_path)] + [str(arg) for arg in args]

    def _register_script_process(self, task_id: str | None, process_pid: int) -> None:
        """Persist process metadata for restart recovery."""
        if not task_id:
            return

        try:
            from app.core.state import set_task_metadata

            set_task_metadata(task_id, process_pid=process_pid)
        except Exception:
            pass

    def _terminate_external_process(self, process: Any) -> None:
        """Forcefully terminate an external process started in a new session."""
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()

        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def _wait_external_process(
        self, process: Any, timeout: int
    ) -> tuple[Optional[int], Optional[str]]:
        """Wait for process completion and handle timeout cleanup."""
        import subprocess

        try:
            return process.wait(timeout=timeout), None
        except subprocess.TimeoutExpired:
            self._terminate_external_process(process)
            return None, f"Script timeout after {timeout} seconds"

    def _build_script_result(
        self, returncode: int, stdout_log: Path, stderr_log: Path
    ) -> Dict[str, Any]:
        """Build normalized script execution result payload."""
        stdout_text = self._read_log_tail(stdout_log)
        stderr_text = self._read_log_tail(stderr_log)

        return {
            "success": returncode == 0,
            "returncode": returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    def _run_external_script_basic(
        self, cmd: List[str], timeout: int, env: Dict[str, str]
    ) -> Dict[str, Any]:
        """Run a script without recovery metadata support (legacy-compatible path)."""
        import subprocess

        try:
            completed = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                start_new_session=True,
            )
            return {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Script timeout after {timeout} seconds"}
        except Exception as e:
            return {"success": False, "error": f"Script execution failed: {e}"}

    def run_external_script_for_task(
        self,
        script_path: Path,
        args: List[str],
        timeout: int = 3600,
        *,
        task_id: str | None = None,
    ) -> Dict[str, Any]:
        """Call `run_external_script` while tolerating monkeypatched callables without `task_id`."""
        if task_id is None:
            return self.run_external_script(script_path, args, timeout=timeout)

        try:
            return self.run_external_script(script_path, args, timeout=timeout, task_id=task_id)
        except TypeError as exc:
            if "task_id" not in str(exc):
                raise
            return self.run_external_script(script_path, args, timeout=timeout)

    def run_external_script(
        self,
        script_path: Path,
        args: List[str],
        timeout: int = 3600,
        *,
        task_id: str | None = None,
    ) -> Dict[str, Any]:
        """
        Run external Python script with timeout.

        Args:
            script_path: Path to Python script
            args: Command line arguments for script
            timeout: Maximum execution time in seconds

        Returns:
            Dict containing script execution results
        """
        import subprocess

        if not script_path.exists():
            return {"success": False, "error": f"Script not found: {script_path}"}

        cmd = self._build_script_command(script_path, args)
        self.logger.info(f"Executing script: {' '.join(cmd)}")

        env = self._build_execution_env()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Legacy-compatible path used by tests/patches and callers that do not
        # need process_pid persistence for restart recovery.
        if task_id is None:
            return self._run_external_script_basic(cmd, timeout, env)

        stdout_log = self.workspace_dir / "info_script.log"
        stderr_log = self.workspace_dir / "error_script.log"

        try:
            with (
                open(stdout_log, "a", encoding="utf-8") as stdout_file,
                open(stderr_log, "a", encoding="utf-8") as stderr_file,
            ):
                process = subprocess.Popen(
                    cmd,
                    cwd=self.workspace_dir,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )

                self._register_script_process(task_id, process.pid)
                returncode, timeout_error = self._wait_external_process(process, timeout)
                if timeout_error is not None:
                    return {"success": False, "error": timeout_error}

            if returncode is None:
                return {
                    "success": False,
                    "error": "Script terminated without return code",
                }

            self._reclassify_success_stderr(stdout_log, stderr_log, returncode)
            return self._build_script_result(returncode, stdout_log, stderr_log)
        except Exception as e:
            return {"success": False, "error": f"Script execution failed: {e}"}

    def _is_probable_error_stderr_line(self, line: str) -> bool:
        """Return whether a stderr line likely represents an error/warning."""
        normalized = (line or "").strip().lower()
        if not normalized:
            return False
        if normalized.startswith("traceback"):
            return True
        return any(token in normalized for token in _STDERR_ERROR_TOKENS)

    def _reclassify_success_stderr(
        self,
        stdout_log: Path,
        stderr_log: Path,
        returncode: int,
    ) -> None:
        """Move non-error stderr lines to stdout when script exited successfully."""
        if returncode != 0:
            return

        stderr_lines = self._read_log_lines(stderr_log)
        if not stderr_lines:
            return

        moved_lines, kept_lines = self._partition_stderr_lines(stderr_lines)
        if not moved_lines:
            return

        try:
            self._append_lines_to_stdout_log(stdout_log, moved_lines)
            self._write_log_lines(stderr_log, kept_lines)
        except Exception:
            return

    def _read_log_lines(self, log_path: Path) -> list[str]:
        """Read a text log file and return lines preserving line endings."""
        try:
            return log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except Exception:
            return []

    def _partition_stderr_lines(self, stderr_lines: list[str]) -> tuple[list[str], list[str]]:
        """Split stderr lines into moved (non-error) and kept (error-like) buckets."""
        moved_lines: list[str] = []
        kept_lines: list[str] = []
        for line in stderr_lines:
            if self._is_probable_error_stderr_line(line):
                kept_lines.append(line)
            else:
                moved_lines.append(line)
        return moved_lines, kept_lines

    def _append_lines_to_stdout_log(self, stdout_log: Path, moved_lines: list[str]) -> None:
        """Append lines to stdout log, keeping line boundaries clean."""
        stdout_prefix = ""
        existing_stdout = ""
        if stdout_log.exists():
            existing_stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
        if existing_stdout and not existing_stdout.endswith("\n"):
            stdout_prefix = "\n"

        with open(stdout_log, "a", encoding="utf-8") as stdout_file:
            if stdout_prefix:
                stdout_file.write(stdout_prefix)
            stdout_file.writelines(moved_lines)

    def _write_log_lines(self, log_path: Path, lines: list[str]) -> None:
        """Overwrite a log file with provided lines."""
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.writelines(lines)

    def _read_log_tail(self, file_path: Path, max_chars: int = 200000) -> str:
        """Read a text file and keep only the tail to bound payload size."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

        if len(content) <= max_chars:
            return content
        return content[-max_chars:]

    def log_ffmpeg_build_warnings(self, *, for_webm: bool = False) -> None:
        """Log non-blocking warnings about FFmpeg build configuration.

        `for_webm=True` enables checks that matter for VP8/VP9 inputs (libvpx).
        """

        text = _ffmpeg_buildconf_text()
        if not text:
            return

        low = text.lower()
        if for_webm and "--enable-libvpx" not in low:
            self.logger.warning(
                "FFmpeg appears to be built without --enable-libvpx; WebM VP8/VP9 decoding may be unreliable (green/pink corruption possible)."
            )
        if "--disable-x86asm" in low:
            self.logger.warning(
                "FFmpeg appears to be built with --disable-x86asm; performance and pixel-format conversions may be degraded (can trigger corruption on some pipelines)."
            )

    def _build_execution_env(self) -> Dict[str, str]:
        """Prepare environment variables for external script execution."""
        env = os.environ.copy()
        try:
            from app.core.config import config as _cfg

            if _cfg.ENCODING_TYPE == "GPU":
                self._apply_cuda_environment(env, _cfg)
        except Exception:
            pass
        return env

    def _apply_cuda_environment(self, env: Dict[str, str], cfg: Any) -> None:
        if getattr(cfg, "GPU_CUDA_VISIBLE_DEVICES", None):
            env["CUDA_VISIBLE_DEVICES"] = str(cfg.GPU_CUDA_VISIBLE_DEVICES)
        if getattr(cfg, "GPU_CUDA_DEVICE_ORDER", None):
            env["CUDA_DEVICE_ORDER"] = str(cfg.GPU_CUDA_DEVICE_ORDER)
        if getattr(cfg, "GPU_CUDA_PATH", None):
            cuda_bin = os.path.join(str(cfg.GPU_CUDA_PATH), "bin")
            env["PATH"] = f"{cuda_bin}:{env.get('PATH', '')}"

    def is_video_file(self, filename: str) -> bool:
        """Check if the file is a video based on its extension.
        Args:
            filename: Name of the file
        Returns:
            bool: True if the file is a video, False otherwise
        """
        video_allowed_extensions = (
            "3gp",
            "avi",
            "divx",
            "flv",
            "m2p",
            "m4v",
            "mkv",
            "mov",
            "mp4",
            "mpeg",
            "mpg",
            "mts",
            "wmv",
            "mp3",
            "ogg",
            "wav",
            "wma",
            "webm",
            "ts",
        )
        return filename.lower().endswith(video_allowed_extensions)

    def get_extension(self, filename: str) -> str:
        """Get the file extension from a filename.

        Args:
            filename: Name of the file
        Returns:
            str: File extension without the dot
        """
        return Path(filename).suffix.lstrip(".").lower()

    def _cleanup_partial_download_file(self, part_path: Path) -> None:
        """Best-effort cleanup of temporary download file."""
        try:
            if part_path.exists():
                part_path.unlink()
        except Exception:
            pass

    def _stream_response_to_file(self, response: Any, part_path: Path, chunk_size: int) -> int:
        """Write streamed response body to a temporary file and return written bytes."""
        bytes_written = 0
        with open(part_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                file.write(chunk)
                bytes_written += len(chunk)
        return bytes_written

    def _parse_expected_download_size(self, raw_content_length: Optional[str]) -> Optional[int]:
        """Return parsed Content-Length, or None when not provided."""
        if raw_content_length is None:
            return None
        return int(raw_content_length)

    def _validate_expected_download_size(self, expected_size: Optional[int]) -> Optional[str]:
        """Validate declared source size against configured max and return an error when rejected."""
        if expected_size is None:
            return None

        file_size_gb = round(expected_size / (1024 * 1024 * 1024), 2)
        max_size_gb = config.MAX_VIDEO_SIZE_GB
        if max_size_gb > 0 and expected_size > (max_size_gb * 1024 * 1024 * 1024):
            return (
                f"The file size ({file_size_gb} GB) exceeds the maximum allowed size of "
                f"{max_size_gb} GB."
            )
        return None

    def _download_source_file_once(
        self,
        session: requests.Session,
        source_url: str,
        part_path: Path,
        chunk_size: int,
    ) -> Dict[str, Any]:
        """Execute one HTTP download attempt into part_path."""
        with session.get(source_url, timeout=(10, 180), stream=True) as response:
            if response.status_code != 200:
                if response.status_code == 404:
                    return {
                        "success": False,
                        "error": f"The {source_url} file was not found on the server.",
                    }
                return {
                    "success": False,
                    "error": (
                        f"Impossible to download {source_url} file, "
                        f"server returned HTTP {response.status_code}."
                    ),
                }

            expected_size = self._parse_expected_download_size(
                response.headers.get("Content-Length")
            )
            size_error = self._validate_expected_download_size(expected_size)
            if size_error is not None:
                return {"success": False, "error": size_error}

            bytes_written = self._stream_response_to_file(response, part_path, chunk_size)
            if bytes_written <= 0:
                declared_size = expected_size if expected_size is not None else "unknown"
                content_type = response.headers.get("Content-Type") or "unknown"
                last_modified = response.headers.get("Last-Modified") or "unknown"
                error_message = (
                    "Downloaded file is empty "
                    f"(0 bytes; Content-Length={declared_size}; "
                    f"Content-Type={content_type}; Last-Modified={last_modified})"
                )
                if self._is_source_not_ready_placeholder_response(expected_size, content_type):
                    raise _SourceTemporarilyUnavailableError(error_message)
                raise ValueError(error_message)
            if expected_size is not None and bytes_written != expected_size:
                raise ValueError(f"Incomplete download ({bytes_written}/{expected_size} bytes).")

            return {"success": True}

    def _download_failure_message(self, source_url: str, error: str) -> str:
        """Build a normalized download failure message."""
        normalized_error = str(error).rstrip(".")
        return f"Impossible to download {source_url} file, with error: {normalized_error}."

    def _is_source_not_ready_placeholder_response(
        self, expected_size: Optional[int], content_type: str
    ) -> bool:
        """Return whether response resembles a temporary placeholder page."""
        normalized_content_type = str(content_type).split(";", 1)[0].strip().lower()
        is_placeholder_content_type = normalized_content_type in {
            "text/html",
            "application/xhtml+xml",
        }
        return is_placeholder_content_type and expected_size in (None, 0)

    def download_source_file(self, source_url: str, dest_file: str) -> Dict[str, Any]:
        """Download source file.

        Args:
            source_url: Source file URL
            dest_file: Destination file path

        Returns:
            Dict containing download results
        """
        max_attempts = _DOWNLOAD_MAX_ATTEMPTS
        base_retry_delay_seconds = _DOWNLOAD_RETRY_DELAY_SECONDS
        retry_backoff_factor = _DOWNLOAD_RETRY_BACKOFF_FACTOR
        chunk_size = 1024 * 1024
        destination = Path(dest_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_name(destination.name + ".part")
        last_error = "Unknown download error"
        session = requests.Session()
        attempt = 0

        try:
            while attempt < max_attempts:
                attempt += 1
                self._cleanup_partial_download_file(part_path)
                try:
                    attempt_result = self._download_source_file_once(
                        session=session,
                        source_url=source_url,
                        part_path=part_path,
                        chunk_size=chunk_size,
                    )
                    if not attempt_result.get("success"):
                        return attempt_result

                    os.replace(part_path, destination)
                    return {"success": True, "file_path": str(destination)}
                except Exception as e:
                    last_error = str(e)
                    if isinstance(e, _SourceTemporarilyUnavailableError):
                        previous_max_attempts = max_attempts
                        max_attempts = max(
                            max_attempts,
                            _DOWNLOAD_MAX_ATTEMPTS_WHEN_SOURCE_NOT_READY,
                        )
                        if max_attempts > previous_max_attempts:
                            self.logger.info(
                                "Source %s appears temporarily unavailable (empty HTML placeholder) at attempt %s/%s; extending download retry budget from %s to %s attempts",
                                source_url,
                                attempt,
                                previous_max_attempts,
                                previous_max_attempts,
                                max_attempts,
                            )
                    if attempt < max_attempts:
                        retry_delay = base_retry_delay_seconds * (
                            retry_backoff_factor ** (attempt - 1)
                        )
                        retry_delay = min(retry_delay, _DOWNLOAD_RETRY_MAX_DELAY_SECONDS)
                        self.logger.warning(
                            "Download attempt %s/%s failed for %s: %s; retrying in %.1fs",
                            attempt,
                            max_attempts,
                            source_url,
                            last_error,
                            retry_delay,
                        )
                        time.sleep(retry_delay)

            self._cleanup_partial_download_file(part_path)
            return {
                "success": False,
                "error": self._download_failure_message(source_url, last_error),
            }
        finally:
            self._cleanup_partial_download_file(part_path)
            close = getattr(session, "close", None)
            if callable(close):
                close()

    @classmethod
    def get_description(cls) -> str:
        """
        Get human-readable description of this handler.

        Returns:
            str: Handler description
        """
        return f"Base task handler for {cls.task_type} tasks"
