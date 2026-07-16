# runner/app/task_handlers/encoding/encoding_handler.py
"""
Video encoding task handler using FFmpeg.
Handles various video encoding tasks through specialized scripts.
"""

import signal
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from app.core.config import config
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest
from app.task_handlers.base_handler import BaseTaskHandler


class VideoEncodingHandler(BaseTaskHandler):
    """
    Handles video encoding tasks using FFmpeg.

    Supports multiple encoding parameters through
    the dedicated encoding entrypoint in this task handler directory.
    """

    task_type = "encoding"
    possible_params = {
        "rendition",
        "cut",
        "dressing",
        "video_id",
        "video_slug",
        "video_title",
    }

    def __init__(self):
        """Initialize video encoding handler."""
        super().__init__()
        self.entrypoints_dir = Path(__file__).parent

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate video encoding parameters.

        Args:
            parameters: Encoding parameters including resolution, cut informations, etc.

        Returns:
            bool: True if parameters are valid
        """
        self.last_invalid_parameters = self.get_invalid_parameters(parameters)

        if self.last_invalid_parameters:
            self.logger.error("Parameters not allowed: " + ", ".join(self.last_invalid_parameters))
            return False

        return True

    def get_invalid_parameters(self, parameters: Dict[str, Any]) -> List[str]:
        """Return unsupported parameter names for encoding requests."""
        return sorted([param for param in parameters if param not in self.possible_params])

    def execute_task(self, task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
        """
        Execute video encoding task.

        Args:
            task_id: Unique task identifier
            task_request: TaskRequest object containing task details

        Returns:
            Dict containing encoding results and metadata
        """
        try:
            self.logger.info(f"Starting video encoding task {task_id}")

            # Prepare workspace directory, one per task
            self.workspace_dir = Path(storage_manager.base_path) / task_id
            workspace = self.prepare_workspace()
            # Output directory inside workspace (already created in prepare_workspace)
            work_dir = "output"
            output_dir = workspace / work_dir

            # Resolve input filename from source_url (ignore query params)
            parsed = urlparse(task_request.source_url)
            source_name = Path(unquote(parsed.path)).name
            if not source_name:
                raise Exception(
                    f"Source URL does not contain a valid filename: {task_request.source_url}"
                )

            if not self.is_video_file(source_name):
                raise Exception(
                    f"Source URL does not point to a valid video file: {task_request.source_url}"
                )

            extension = self.get_extension(source_name)
            self.logger.info(
                f"Source URL points to a video file: {task_request.source_url} (ext: {extension})"
            )

            filename = source_name
            input_path = workspace / filename
            self.logger.info(f"Input video will be saved to: {str(input_path)}")

            download_result = self.download_source_file(
                source_url=task_request.source_url, dest_file=str(input_path)
            )
            if download_result["success"]:
                self.logger.info(f"Downloaded source file to: {str(input_path)}")
            else:
                raise Exception(download_result.get("error", "Unknown download error"))

            self.validate_downloaded_media_against_denylist(input_path)

            # Non-blocking diagnostic: WebM inputs depend on libvpx for robust VP8/VP9 decode.
            if extension == "webm":
                self.log_ffmpeg_build_warnings(for_webm=True)

            script_path = self.entrypoints_dir / "encoding.py"

            if not script_path.exists():
                return {"success": False, "error": f"No script available: {script_path}"}

            args = self._build_script_arguments(
                parameters=task_request.parameters,
                base_dir=str(workspace),
                input_file=filename,
                work_dir=work_dir,
            )

            self.logger.info(f"Run external script: {script_path} with args: {args}")

            script_result = self.run_external_script_for_task(
                script_path,
                args,
                timeout=config.EXTERNAL_SCRIPT_TIMEOUT_SECONDS,
                task_id=task_id,
            )
            script_result = self._fill_empty_streams_from_encoding_log(script_result, output_dir)

            results = {
                "success": script_result["success"],
                "task_type": self.task_type,
                "input_path": str(input_path),
                "output_dir": str(output_dir),
                "script_output": script_result,
            }

            if not script_result["success"]:
                results["error"] = self._extract_script_error(script_result)

            self.save_task_metadata(task_id, results, output_dir)

            if script_result["success"]:
                self.logger.info(f"Encoding task {task_id} completed successfully")
            else:
                self.logger.error(f"Encoding task {task_id} failed: {results['error']}")

            return results

        except Exception as e:
            self.logger.error(f"Encoding task {task_id} execution failed: {e}")
            return {"success": False, "error": str(e), "task_type": self.task_type}
        finally:
            pass

    def _fill_empty_streams_from_encoding_log(
        self, script_result: Dict[str, Any], output_dir: Path
    ) -> Dict[str, Any]:
        """Fallback to `encoding.log` when external script streams are empty."""
        if not isinstance(script_result, dict):
            return script_result

        stdout_text = str(script_result.get("stdout") or "").strip()
        if stdout_text:
            return script_result

        encoding_log = output_dir / "encoding.log"
        log_tail = self._read_log_tail(encoding_log).strip()
        if not log_tail:
            return script_result

        enriched_result = dict(script_result)
        enriched_result["stdout"] = log_tail
        return enriched_result

    def _build_script_arguments(
        self, parameters: Dict[str, Any], base_dir: str, input_file: str, work_dir: str
    ) -> List[str]:
        """
        Build command line arguments for encoding script.

        Args:
            parameters: Specifics parameters
            base_dir: Base directory
            input_file: File name to encode
            work_dir: Work output directory

        Returns:
            List of command line arguments
        """
        args = []

        args.extend(["--encoding-type", config.ENCODING_TYPE])

        # # # Common settings for CPU or GPU encoding # # #
        args.extend(["--base-dir", base_dir])
        args.extend(["--input-file", input_file])
        args.extend(["--work-dir", work_dir])
        args.extend(["--debug", str(config.DEBUG)])

        # # # Specifics settings for GPU encoding # # #
        if config.ENCODING_TYPE == "GPU":
            # HWACCEL_DEVICE parameter for GPU encoding (Ex: 0)
            args.extend(["--hwaccel-device", str(config.GPU_HWACCEL_DEVICE)])
            # CUDA_VISIBLE_DEVICES parameter for GPU encoding (Ex: 0,1)
            args.extend(["--cuda-visible-devices", str(config.GPU_CUDA_VISIBLE_DEVICES)])
            # CUDA_DEVICE_ORDER parameter for GPU encoding (Ex: PCI_BUS_ID)
            args.extend(["--cuda-device-order", str(config.GPU_CUDA_DEVICE_ORDER)])
            # CUDA_PATH parameter for GPU encoding (Ex: /usr/local/cuda-13.2)
            args.extend(["--cuda-path", str(config.GPU_CUDA_PATH)])

        if "rendition" in parameters:
            args.extend(["--rendition", str(parameters["rendition"])])

        if "cut" in parameters:
            args.extend(["--cut", str(parameters["cut"])])

        if "dressing" in parameters:
            args.extend(["--dressing", str(parameters["dressing"])])

        # Optional video identification metadata (tracking only).
        if "video_id" in parameters:
            args.extend(["--video-id", str(parameters["video_id"])])
        if "video_slug" in parameters:
            args.extend(["--video-slug", str(parameters["video_slug"])])
        if "video_title" in parameters:
            args.extend(["--video-title", str(parameters["video_title"])])

        return args

    def _extract_script_error(self, script_result: Dict[str, Any]) -> str:
        """Build a readable error message from an external script result."""
        error = str(script_result.get("error") or "").strip()
        if error:
            return error

        stderr_text = str(script_result.get("stderr") or "").strip()
        if stderr_text:
            lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
            if lines:
                return lines[-1]

        stdout_error = self._extract_error_line_from_log(str(script_result.get("stdout") or ""))
        if stdout_error:
            return stdout_error

        returncode = script_result.get("returncode")
        if returncode not in (None, 0):
            return self._format_returncode_error(returncode)
        return "Encoding failed"

    def _extract_error_line_from_log(self, text: str) -> str:
        """Return the most relevant error line from an encoding log tail."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            normalized = line.lower()
            if normalized.startswith("error:"):
                return line.split(":", 1)[1].strip() or line
            if (
                "error return code" in normalized
                or "error encoding" in normalized
                or normalized.startswith("runtime error:")
                or normalized.startswith("os error:")
                or normalized.startswith("unexpected error:")
                or normalized.startswith("error ")
            ):
                return line
        return ""

    def _format_returncode_error(self, returncode: Any) -> str:
        """Format script return codes, including signal-based terminations."""
        try:
            code = int(returncode)
        except (TypeError, ValueError):
            return f"Encoding failed (exit code {returncode})"

        if code < 0:
            signal_number = abs(code)
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"signal {signal_number}"
            return f"Encoding process was terminated by {signal_name} (return code {code})"

        return f"Encoding failed (exit code {code})"

    @classmethod
    def get_description(cls) -> str:
        """
        Get description of video encoding handler.

        Returns:
            str: Handler description
        """
        return "Video encoding handler"
