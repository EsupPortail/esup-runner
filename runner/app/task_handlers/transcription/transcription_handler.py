"""
Transcription task handler using FFmpeg whisper filter.
Generates subtitles (VTT) from a video source using ffmpeg 8 + whisper.
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

    def __init__(self):
        super().__init__()
        self.scripts_dir = Path(__file__).parent / "scripts"

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate transcription parameters.

        Optional supported parameters:
        - language: language code or 'auto'
        - format: output subtitle format (vtt|srt), default vtt
        - model: logical whisper model (small|medium|large|turbo)
        """
        # No required params for transcription
        return True

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
            script_result = self.run_external_script(
                script_path,
                args,
                timeout=config.EXTERNAL_SCRIPT_TIMEOUT_SECONDS,
            )

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

        # Language
        language = str(parameters.get("language", config.WHISPER_LANGUAGE))
        args.extend(["--language", language])

        # Model selection: allow per-task override or fallback to config
        logical_model = str(parameters.get("model", config.WHISPER_MODEL)).lower()
        args.extend(["--model", logical_model])

        # GPU hints
        use_gpu = "true" if config.whisper_use_gpu() else "false"
        args.extend(["--use-gpu", use_gpu])
        args.extend(["--gpu-device", str(config.GPU_HWACCEL_DEVICE)])

        # Normalize MP3 (optional, default false)
        normalize = str(parameters.get("normalize", False)).lower() in ("true", "1", "yes")
        args.extend(["--normalize", "true" if normalize else "false"])

        # Debug
        args.extend(["--debug", str(config.DEBUG)])

        return args

    @classmethod
    def get_description(cls) -> str:
        return "Transcription handler"
