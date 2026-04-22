"""
Transcription task handler for Whisper-based subtitle generation.

The public API keeps a single `language` parameter:
- `auto` keeps subtitles in the detected spoken language
- an explicit language requests subtitles in that final language, which may
  trigger a translation step after source-language transcription

When a translation happens, only the final deliverable keeps the `.vtt`
extension. The preserved source-language sidecar is written with a non-`.vtt`
filename so client applications that pick the first VTT file do not consume the
wrong subtitle track.
"""

from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from app.core.config import config
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest
from app.task_handlers.base_handler import BaseTaskHandler


class TranscriptionHandler(BaseTaskHandler):
    """
    Handles transcription tasks using ffmpeg's whisper filter.

    Workflow:
    - Download/prepare input media
    - Run internal transcription script that calls ffmpeg whisper
    - Convert SRT to VTT if needed and package results
    """

    task_type = "transcription"
    possible_params = {
        "language",
        "format",
        "model",
        "model_type",
        "duration",
        "normalize",
        "video_id",
        "video_slug",
        "video_title",
    }

    def __init__(self):
        super().__init__()
        self.scripts_dir = Path(__file__).parent / "scripts"

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate transcription parameters.

        Optional supported parameters:
        - language: final subtitle language code or 'auto'
        - format: output subtitle format (vtt|srt), default vtt
        - model: logical whisper model (small|medium|large|turbo)
        - duration/model_type: legacy compatibility metadata from manager payloads
        - video_id/video_slug/video_title: optional tracking metadata
        """
        self.last_invalid_parameters = self.get_invalid_parameters(parameters)

        # Check for unknown parameters
        if self.last_invalid_parameters:
            self.logger.error("Parameters not allowed: " + ", ".join(self.last_invalid_parameters))
            return False

        return True

    def get_invalid_parameters(self, parameters: Dict[str, Any]) -> List[str]:
        """Return unsupported parameter names for transcription requests."""
        return sorted([param for param in parameters if param not in self.possible_params])

    def execute_task(self, task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
        try:
            self.logger.info(f"Starting transcription task {task_id}")

            # Prepare workspace directory, one per task
            self.workspace_dir = Path(storage_manager.base_path) / task_id
            workspace = self.prepare_workspace()
            work_dir = "output"
            output_dir = workspace / work_dir

            # Resolve input filename from source_url
            parsed = urlparse(task_request.source_url)
            source_name = Path(unquote(parsed.path)).name
            if not source_name:
                raise Exception(
                    f"Source URL does not contain a valid filename: {task_request.source_url}"
                )

            if not self.is_video_file(source_name):
                raise Exception(
                    f"Source URL does not point to a valid media file: {task_request.source_url}"
                )

            filename = source_name
            input_path = workspace / filename

            # Download source file locally
            dl = self.download_source_file(task_request.source_url, str(input_path))
            if not dl.get("success"):
                raise Exception(dl.get("error", "Unable to download input"))

            # Determine script path
            script_path = self.scripts_dir / "transcription.py"
            if not script_path.exists():
                return {"success": False, "error": f"Script not found: {script_path}"}

            # Build script arguments
            args = self._build_script_arguments(
                parameters=task_request.parameters,
                base_dir=str(workspace),
                input_file=filename,
                work_dir=work_dir,
            )

            self.logger.info(f"Run transcription script: {script_path} with args: {args}")
            script_result = self.run_external_script_for_task(
                script_path,
                args,
                timeout=config.EXTERNAL_SCRIPT_TIMEOUT_SECONDS,
                task_id=task_id,
            )
            script_result = self._reclassify_non_error_stderr(script_result)

            # Prepare results summary
            results: Dict[str, Any] = {
                "success": script_result.get("success", False),
                "task_type": self.task_type,
                "input_path": str(input_path),
                "output_dir": str(output_dir),
                "script_output": script_result,
            }

            if not script_result.get("success", False):
                results["error"] = script_result.get("error") or "Transcription failed"

            # Save metadata
            self.save_task_metadata(task_id, results, output_dir)
            return results

        except Exception as e:
            self.logger.error(f"Transcription task {task_id} execution failed: {e}")
            return {"success": False, "error": str(e), "task_type": self.task_type}

    def _reclassify_non_error_stderr(self, script_result: Dict[str, Any]) -> Dict[str, Any]:
        """Move known non-error progress lines from stderr to stdout."""
        stderr_text = str(script_result.get("stderr") or "")
        if not stderr_text.strip():
            return script_result

        progress_lines: List[str] = []
        error_lines: List[str] = []
        for line in stderr_text.splitlines():
            if line.lstrip().startswith("Loading weights:"):
                progress_lines.append(line)
            else:
                error_lines.append(line)

        if not progress_lines:
            return script_result

        stdout_text = str(script_result.get("stdout") or "")
        progress_text = "\n".join(progress_lines).strip()

        merged_stdout = stdout_text.rstrip()
        if merged_stdout:
            merged_stdout += "\n\n" + progress_text
        else:
            merged_stdout = progress_text

        merged_stderr = "\n".join(error_lines).strip()
        normalized_result = dict(script_result)
        normalized_result["stdout"] = merged_stdout
        normalized_result["stderr"] = merged_stderr
        return normalized_result

    def _build_script_arguments(
        self, parameters: Dict[str, Any], base_dir: str, input_file: str, work_dir: str
    ) -> List[str]:
        args: List[str] = []

        # Base I/O
        args.extend(["--base-dir", base_dir])
        args.extend(["--input-file", input_file])
        args.extend(["--work-dir", work_dir])

        # Output format default to VTT
        fmt = str(parameters.get("format", "vtt")).lower()
        args.extend(["--format", fmt])

        # Final subtitle language. The script will keep the detected spoken
        # language when this stays on `auto`, or translate after transcription
        # when an explicit target language differs from the source language.
        language = str(parameters.get("language", config.WHISPER_LANGUAGE))
        args.extend(["--language", language])

        # Model selection: allow per-task override or fallback to config
        logical_model = str(parameters.get("model", config.WHISPER_MODEL)).lower()
        args.extend(["--model", logical_model])
        args.extend(["--whisper-models-dir", str(config.WHISPER_MODELS_DIR)])
        args.extend(["--huggingface-models-dir", str(config.HUGGINGFACE_MODELS_DIR)])

        # Optional video identification metadata (tracking only).
        if "video_id" in parameters:
            args.extend(["--video-id", str(parameters["video_id"])])
        if "video_slug" in parameters:
            args.extend(["--video-slug", str(parameters["video_slug"])])
        if "video_title" in parameters:
            args.extend(["--video-title", str(parameters["video_title"])])

        # GPU hints
        use_gpu = "true" if config.whisper_use_gpu() else "false"
        args.extend(["--use-gpu", use_gpu])
        args.extend(["--gpu-device", str(config.GPU_HWACCEL_DEVICE)])

        # Normalize MP3 (optional, default false)
        normalize = str(parameters.get("normalize", False)).lower() in (
            "true",
            "1",
            "yes",
        )
        args.extend(["--normalize", "true" if normalize else "false"])

        # Debug
        args.extend(["--debug", str(config.DEBUG)])

        return args

    @classmethod
    def get_description(cls) -> str:
        return "Transcription handler"
