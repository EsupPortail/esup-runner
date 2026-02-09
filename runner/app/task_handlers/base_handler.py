# runner/app/task_handlers/base_handler.py
"""
Base task handler defining the interface for all task processors.
"""

import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import requests  # type: ignore[import-untyped]

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.models.models import TaskRequest

logger = setup_default_logging()


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
        import shutil

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

    def run_external_script(
        self, script_path: Path, args: List[str], timeout: int = 3600
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
        import sys

        if not script_path.exists():
            return {"success": False, "error": f"Script not found: {script_path}"}

        # Ensure every argument is a string to avoid join/subprocess type errors
        cmd = [str(sys.executable), str(script_path)] + [str(arg) for arg in args]
        self.logger.info(f"Executing script: {' '.join(cmd)}")

        try:
            env = self._build_execution_env()
            result = subprocess.run(
                cmd,
                timeout=timeout,
                capture_output=True,
                text=True,
                cwd=self.workspace_dir,
                env=env,
            )

            return {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Script timeout after {timeout} seconds"}
        except Exception as e:
            return {"success": False, "error": f"Script execution failed: {e}"}

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
            env["PATH"] = f"{cuda_bin}:{env.get('PATH','')}"

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

    def download_source_file(self, source_url: str, dest_file: str) -> Dict[str, Any]:
        """Download source file.

        Args:
            source_url: Source file URL
            dest_file: Destination file path

        Returns:
            Dict containing download results
        """
        # Check if video file exists
        try:
            # Session useful to achieve requests (and keep cookies between), if necessary
            session = requests.Session()
            with session.get(source_url, timeout=(10, 180), stream=True) as response:
                # Can be useful to debug
                # print(session.cookies.get_dict())
                if response.status_code != 200:
                    return {
                        "success": False,
                        "error": f"The {source_url} file was not found on the server.",
                    }

                # Check content length
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    file_size = int(content_length)
                    # Convert file size to GB
                    file_size_gb = round(file_size / (1024 * 1024 * 1024), 2)
                    # Maximum size check
                    max_size_gb = config.MAX_VIDEO_SIZE_GB
                    if max_size_gb > 0 and file_size > (max_size_gb * 1024 * 1024 * 1024):
                        return {
                            "success": False,
                            "error": f"The file size ({file_size_gb} GB) exceeds the maximum allowed size of {max_size_gb} GB.",
                        }

                # Write to destination file
                with open(dest_file, "wb+") as file:
                    # Download in chunks
                    shutil.copyfileobj(response.raw, file)

            return {"success": True, "file_path": dest_file}
        except Exception as e:
            return {
                "success": False,
                "error": f"Impossible to download {source_url} file, with error: {e}.",
            }

    @classmethod
    def get_description(cls) -> str:
        """
        Get human-readable description of this handler.

        Returns:
            str: Handler description
        """
        return f"Base task handler for {cls.task_type} tasks"
