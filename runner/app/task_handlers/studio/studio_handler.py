"""
Studio encoding task handler.
Step 1: Run internal studio script to generate a base video (mp4) from the mediapackage XML (with optional SMIL cut and layout).
Step 2: Run existing encoding script on the generated video to produce full outputs (mp4, m3u8, thumbnails, overview...), honoring GPU configuration.
"""

import re
from pathlib import Path
from typing import Any, Dict

from app.core.config import config
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest
from app.task_handlers.base_handler import BaseTaskHandler


class StudioEncodingHandler(BaseTaskHandler):
    task_type = "studio"

    def __init__(self):
        super().__init__()
        self.scripts_dir = Path(__file__).parent / "scripts"

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        # Allow optional overrides like presenter layout, rendition, etc.
        return True

    def execute_task(self, task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
        try:
            self.logger.info(f"Starting studio encoding task {task_id} {task_request}")

            # Non-blocking diagnostic: Studio sources are frequently WebM (VP8/VP9/AV1).
            self.log_ffmpeg_build_warnings(for_webm=True)

            self.workspace_dir = Path(storage_manager.base_path) / task_id
            workspace = self.prepare_workspace()
            work_dir = "output"
            output_dir = workspace / work_dir
            base_video_name = "studio_base.mp4"

            studio_result, base_video_path = self._generate_base_video(
                task_request, workspace, work_dir, base_video_name
            )
            if not studio_result.get("success"):
                return studio_result

            if not base_video_path.exists():
                return {
                    "success": False,
                    "task_type": self.task_type,
                    "error": f"Base studio video not found: {base_video_path}",
                    "script_output": studio_result,
                }

            prepare_error = self._prepare_encoding_input(
                base_video_path, workspace, base_video_name, studio_result
            )
            if prepare_error:
                return prepare_error

            enc_result = self._run_encoding(task_request, workspace, base_video_name, work_dir)
            results = self._build_results(enc_result, studio_result, output_dir, base_video_path)

            self.save_task_metadata(task_id, results, output_dir)
            return results

        except Exception as e:
            self.logger.error(f"Studio task {task_id} execution failed: {e}")
            return {"success": False, "error": str(e), "task_type": self.task_type}

    def _generate_base_video(
        self,
        task_request: TaskRequest,
        workspace: Path,
        work_dir: str,
        output_file: str,
    ) -> tuple[Dict[str, Any], Path]:
        studio_script = self.scripts_dir / "studio.py"
        base_video_path = workspace / work_dir / output_file

        studio_args = self._build_studio_args(
            xml_url=task_request.source_url,
            base_dir=str(workspace),
            work_dir=work_dir,
            output_file=output_file,
            presenter=task_request.parameters.get("presenter"),
            parameters=task_request.parameters,
        )

        self.logger.info(f"Run studio script: {studio_script} with args: {studio_args}")
        studio_result = self.run_external_script(studio_script, studio_args, timeout=7200)

        self._log_studio_selected_mode(studio_result)

        # Persist studio generation logs alongside encoding logs (same file) so operators
        # can debug studio_base.mp4 creation.
        try:
            self._append_stage_log(
                workspace / work_dir / "encoding.log",
                stage="STUDIO GENERATION",
                script_path=studio_script,
                args=studio_args,
                result=studio_result,
            )
        except Exception:
            pass

        if studio_result.get("success"):
            return studio_result, base_video_path

        retry_result = self._retry_studio_cpu(
            studio_script, task_request, workspace, work_dir, output_file, studio_result
        )
        if retry_result:
            return retry_result, base_video_path

        failure_result = {
            "success": False,
            "task_type": self.task_type,
            "error": f"Studio generation failed rc={studio_result.get('returncode')} :: {self._summarize_output(studio_result)}",
            "script_output": studio_result,
        }
        return failure_result, base_video_path

    def _retry_studio_cpu(
        self,
        studio_script: Path,
        task_request: TaskRequest,
        workspace: Path,
        work_dir: str,
        output_file: str,
        studio_result: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        forced_cpu = self._get_bool_param(task_request.parameters.get("force_cpu", False))
        if config.ENCODING_TYPE != "GPU" or forced_cpu:
            return None
        retry_params = dict(task_request.parameters)
        retry_params["force_cpu"] = True
        retry_args = self._build_studio_args(
            xml_url=task_request.source_url,
            base_dir=str(workspace),
            work_dir=work_dir,
            output_file=output_file,
            presenter=retry_params.get("presenter"),
            parameters=retry_params,
        )
        self.logger.info(f"Studio script failed on GPU, retrying with CPU. Args: {retry_args}")
        studio_result_retry = self.run_external_script(studio_script, retry_args, timeout=7200)

        self._log_studio_selected_mode(studio_result_retry, context="(retry)")

        try:
            self._append_stage_log(
                workspace / work_dir / "encoding.log",
                stage="STUDIO GENERATION (RETRY CPU)",
                script_path=studio_script,
                args=retry_args,
                result=studio_result_retry,
            )
        except Exception:
            pass
        if studio_result_retry.get("success"):
            return studio_result_retry
        return {
            "success": False,
            "task_type": self.task_type,
            "error": f"Studio generation failed (retry CPU) rc={studio_result_retry.get('returncode')} :: {self._summarize_output(studio_result_retry)}",
            "script_output": {
                "studio_first": studio_result,
                "studio_retry": studio_result_retry,
            },
        }

    def _append_stage_log(
        self,
        log_path: Path,
        stage: str,
        script_path: Path,
        args: list[str],
        result: Dict[str, Any],
    ) -> None:
        """Append external script stdout/stderr into the task encoding.log."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rc = result.get("returncode")
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        cmd = " ".join([str(script_path)] + [str(a) for a in args])

        from datetime import datetime

        header = (
            "\n\n===== %s =====\n" % stage
            + "timestamp: %s\n" % datetime.now().isoformat()
            + "command: %s\n" % cmd
            + "returncode: %s\n" % ("" if rc is None else rc)
        )
        with open(log_path, "a") as f:
            f.write(header)
            if stdout.strip():
                f.write("\n--- stdout ---\n")
                f.write(stdout)
                if not stdout.endswith("\n"):
                    f.write("\n")
            if stderr.strip():
                f.write("\n--- stderr ---\n")
                f.write(stderr)
                if not stderr.endswith("\n"):
                    f.write("\n")

    def _log_studio_selected_mode(self, result: Dict[str, Any], context: str = "") -> None:
        """Log which studio pipeline mode was actually used.

        studio.py prints attempts as: [FULL_GPU] ..., [GPU_ENC_ONLY] ..., [CPU] ...
        The last printed label corresponds to the chosen mode (the process exits on success).
        """

        stdout = str(result.get("stdout") or "")
        matches = re.findall(r"^\[(FULL_GPU|GPU_ENC_ONLY|CPU)\]", stdout, flags=re.MULTILINE)
        if not matches:
            return
        chosen = matches[-1]
        suffix = f" {context}" if context else ""
        self.logger.info(f"Studio base generation mode{suffix}: {chosen}")

    def _prepare_encoding_input(
        self,
        base_video_path: Path,
        workspace: Path,
        base_video_name: str,
        studio_result: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        link_input_path = workspace / base_video_name
        try:
            if link_input_path.exists() or link_input_path.is_symlink():
                link_input_path.unlink()
            link_input_path.symlink_to(base_video_path)
            self.logger.info(f"Linked encoding input: {link_input_path} -> {base_video_path}")
            return None
        except Exception:
            try:
                import shutil

                shutil.copy2(base_video_path, link_input_path)
                self.logger.info(f"Copied encoding input: {link_input_path}")
                return None
            except Exception as copy_err:
                return {
                    "success": False,
                    "task_type": self.task_type,
                    "error": f"Failed to prepare encoding input: {copy_err}",
                    "script_output": studio_result,
                }

    def _run_encoding(
        self, task_request: TaskRequest, workspace: Path, base_video_name: str, work_dir: str
    ) -> Dict[str, Any]:
        self.logger.info(
            f"Base studio video generated at: {workspace / work_dir / base_video_name}. Proceeding to encoding."
        )
        encoding_script = Path(__file__).parent.parent / "encoding" / "scripts" / "encoding.py"
        enc_args = self._build_encoding_args(
            parameters=task_request.parameters,
            base_dir=str(workspace),
            input_file=base_video_name,
            work_dir=work_dir,
        )
        self.logger.info(f"Run encoding script: {encoding_script} with args: {enc_args}")
        return self.run_external_script(encoding_script, enc_args, timeout=7200)

    def _build_results(
        self,
        enc_result: Dict[str, Any],
        studio_result: Dict[str, Any],
        output_dir: Path,
        base_video_path: Path,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "success": enc_result.get("success", False),
            "task_type": self.task_type,
            "output_dir": str(output_dir),
            "base_video": str(base_video_path),
            "script_output": {
                "studio": studio_result,
                "encoding": enc_result,
            },
        }
        if not enc_result.get("success", False):
            results["error"] = enc_result.get("error", "Encoding failed")
        return results

    def _summarize_output(self, res: Dict[str, Any]) -> str:
        stderr = (res.get("stderr") or "").strip()
        stdout = (res.get("stdout") or "").strip()
        text = stderr or stdout
        return text[-800:] if len(text) > 800 else text

    def _build_studio_args(
        self,
        xml_url: str,
        base_dir: str,
        work_dir: str,
        output_file: str,
        presenter: Any,
        parameters: Dict[str, Any],
    ) -> list[str]:
        args: list[str] = []
        args.extend(["--xml-url", xml_url])
        args.extend(["--base-dir", base_dir])
        args.extend(["--work-dir", work_dir])
        args.extend(["--output-file", output_file])
        if presenter:
            args.extend(["--presenter", str(presenter)])
        # GPU/CPU configuration for base generation
        args.extend(["--encoding-type", config.ENCODING_TYPE])
        if config.ENCODING_TYPE == "GPU":
            args.extend(["--hwaccel-device", str(config.GPU_HWACCEL_DEVICE)])
            args.extend(["--cuda-visible-devices", config.GPU_CUDA_VISIBLE_DEVICES])
            args.extend(["--cuda-device-order", config.GPU_CUDA_DEVICE_ORDER])
            args.extend(["--cuda-path", config.GPU_CUDA_PATH])
        else:
            # When runner is in CPU mode, explicitly force CPU path in studio.py
            args.extend(["--force-cpu", "true"])
        # Optional override to force CPU path during studio generation
        if str(self._get_bool_param(parameters.get("force_cpu", False))).lower() == "true":
            args.extend(["--force-cpu", "true"])
        # Optional encoding tunables for studio generation
        crf = parameters.get("studio_crf", config.STUDIO_DEFAULT_CRF)
        preset = parameters.get("studio_preset", config.STUDIO_DEFAULT_PRESET)
        audio_bitrate = parameters.get("studio_audio_bitrate", config.STUDIO_DEFAULT_AUDIO_BITRATE)
        allow_nvenc = parameters.get("studio_allow_nvenc")
        if crf:
            args.extend(["--studio-crf", str(crf)])
        if preset:
            args.extend(["--studio-preset", str(preset)])
        if audio_bitrate:
            args.extend(["--studio-audio-bitrate", str(audio_bitrate)])
        if allow_nvenc is not None:
            args.extend(["--studio-allow-nvenc", str(allow_nvenc)])
        return args

    def _get_bool_param(self, val: Any) -> bool:
        try:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes")
            return bool(val)
        except Exception:
            return False

    def _build_encoding_args(
        self, parameters: Dict[str, Any], base_dir: str, input_file: str, work_dir: str
    ) -> list[str]:
        args: list[str] = []
        # Honor runner GPU/CPU config
        args.extend(["--encoding-type", config.ENCODING_TYPE])
        args.extend(["--base-dir", base_dir])
        args.extend(["--input-file", input_file])
        args.extend(["--work-dir", work_dir])
        args.extend(["--debug", str(config.DEBUG)])

        if config.ENCODING_TYPE == "GPU":
            args.extend(["--hwaccel-device", str(config.GPU_HWACCEL_DEVICE)])
            args.extend(["--cuda-visible-devices", config.GPU_CUDA_VISIBLE_DEVICES])
            args.extend(["--cuda-device-order", config.GPU_CUDA_DEVICE_ORDER])
            args.extend(["--cuda-path", config.GPU_CUDA_PATH])

        if "rendition" in parameters:
            args.extend(["--rendition", str(parameters["rendition"])])
        if "cut" in parameters:
            args.extend(["--cut", str(parameters["cut"])])

        # Pass-through extra parameters, excluding studio-only keys not supported by encoding.py
        exclude = {
            "rendition",
            "cut",
            "presenter",
            "studio_crf",
            "studio_preset",
            "studio_audio_bitrate",
            "force_cpu",
        }
        for k, v in parameters.items():
            if k not in exclude:
                args.extend([f"--{k}", str(v)])

        return args

    @classmethod
    def get_description(cls) -> str:
        return "Studio encoding handler"
