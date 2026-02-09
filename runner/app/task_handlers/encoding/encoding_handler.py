# runner/app/task_handlers/encoding/encoding_handler.py
"""
Video encoding task handler using FFmpeg.
Handles various video encoding tasks through specialized scripts.
"""

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
    specialized scripts in the encoding/scripts directory.
    """

    task_type = "encoding"

    def __init__(self):
        """Initialize video encoding handler."""
        super().__init__()
        self.scripts_dir = Path(__file__).parent / "scripts"

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate video encoding parameters.

        Args:
            parameters: Encoding parameters including resolution, cut informations, etc.

        Returns:
            bool: True if parameters are valid
        """
        required_params: List[str] = []
        possible_params = ["rendition", "cut", "dressing"]

        # Check required parameters
        for param in required_params:
            if param not in parameters:
                self.logger.error(f"Missing required parameter: {param}")
                return False

        # Check for unknown parameters
        for param in parameters:
            if param not in possible_params:
                self.logger.error(f"Parameter not allowed: {param}")
                return False

        return True

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
            # Workspace path
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

            # Validate that the filename corresponds to a video file
            if not self.is_video_file(source_name):
                raise Exception(
                    f"Source URL does not point to a valid video file: {task_request.source_url}"
                )

            extension = self.get_extension(source_name)
            self.logger.info(
                f"Source URL points to a video file: {task_request.source_url} (ext: {extension})"
            )

            # Non-blocking diagnostic: WebM inputs depend on libvpx for robust VP8/VP9 decode.
            if extension == "webm":
                self.log_ffmpeg_build_warnings(for_webm=True)

            # Use the actual source filename for the workspace input file
            filename = source_name
            input_path = workspace / filename
            self.logger.info(f"Input video will be saved to: {str(input_path)}")

            # Download source file
            download_result = self.download_source_file(
                source_url=task_request.source_url, dest_file=str(input_path)
            )
            if download_result["success"]:
                self.logger.info(f"Downloaded source file to: {str(input_path)}")
            else:
                raise Exception(download_result.get("error", "Unknown download error"))

            # Determine which script to use
            script_path = self.scripts_dir / "encoding.py"

            if not script_path:
                return {"success": False, "error": f"No script available: {script_path}"}

            # Build script arguments
            args = self._build_script_arguments(
                parameters=task_request.parameters,
                base_dir=str(workspace),
                input_file=filename,
                work_dir=work_dir,
            )

            self.logger.info(f"Run external script: {script_path} with args: {args}")

            # Execute encoding script
            script_result = self.run_external_script(script_path, args, timeout=7200)

            # Collect results
            results = {
                "success": script_result["success"],
                "task_type": self.task_type,
                "input_path": str(input_path),
                "output_dir": str(output_dir),
                "script_output": script_result,
            }

            # Check for errors
            if not script_result["success"]:
                results["error"] = script_result.get("error", "Encoding failed")

            # Save metadata
            self.save_task_metadata(task_id, results, output_dir)

            if script_result["success"]:
                self.logger.info(f"Encoding task {task_id} completed successfully")
            else:
                self.logger.error(
                    f"Encoding task {task_id} failed: {script_result.get('error', 'Unknown error')}"
                )

            return results

        except Exception as e:
            self.logger.error(f"Encoding task {task_id} execution failed: {e}")
            return {"success": False, "error": str(e), "task_type": self.task_type}
        finally:
            pass
            # Cleanup workspace after task completion
            # self.cleanup_workspace()

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

        # Encoding type can be CPU or GPU
        args.extend(["--encoding-type", config.ENCODING_TYPE])

        # # # Common settings for CPU or GPU encoding # # #
        # Base directory for input files
        args.extend(["--base-dir", base_dir])
        # Name of input file to encode
        args.extend(["--input-file", input_file])
        # Work directory for output files
        args.extend(["--work-dir", work_dir])
        # Run script in debug mode
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

        # Add rendition configuration parameter
        if "rendition" in parameters:
            args.extend(["--rendition", str(parameters["rendition"])])

        # Add cut configuration parameter
        if "cut" in parameters:
            args.extend(["--cut", str(parameters["cut"])])

        # Add dressing configuration parameter
        if "dressing" in parameters:
            args.extend(["--dressing", str(parameters["dressing"])])

        # Add any additional parameters (excluding already processed ones)
        # Potentially useful for future extensions
        # for key, value in parameters.items():
        #    if key not in ["rendition", "cut", "dressing"]:
        #        args.extend([f"--{key}", str(value)])

        return args

    @classmethod
    def get_description(cls) -> str:
        """
        Get description of video encoding handler.

        Returns:
            str: Handler description
        """
        return "Video encoding handler"
