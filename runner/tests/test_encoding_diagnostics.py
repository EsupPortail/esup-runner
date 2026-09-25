"""Exercise incomplete-output diagnostics through the CLI, callbacks and recovery."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routes import task as task_routes
from app.core import state
from app.core.encoding_diagnostics import incomplete_output_error, prepend_encoding_warning
from app.models.models import TaskRequest
from app.services import task_recovery
from app.task_handlers.encoding.core import ffmpeg_runtime_utils as ffmpeg_runtime
from app.task_handlers.encoding.core import main_orchestration_utils, runtime_flow_utils
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler
from app.task_handlers.studio.studio_handler import StudioEncodingHandler


@pytest.fixture
def incomplete_encoding(tmp_path, monkeypatch, capsys):
    """Run the real failure path with successful encoders producing truncated streams."""
    output_dir = tmp_path / "incomplete-task" / "output"
    output_dir.mkdir(parents=True)
    output = output_dir / "360p_timeout_clip.mp4"
    output.write_bytes(b"encoded")
    monkeypatch.setattr(runtime_flow_utils, "_VIDEOS_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(runtime_flow_utils, "_DEBUG", False)
    monkeypatch.setattr(runtime_flow_utils, "_CUT_CONFIG", {})
    monkeypatch.setattr(runtime_flow_utils, "EFFECTIVE_DURATION", 0)
    monkeypatch.setattr(runtime_flow_utils, "SUBTIME", " ")
    monkeypatch.setattr(runtime_flow_utils, "_VIDEO_IDENTIFICATION", {})
    monkeypatch.setattr(
        runtime_flow_utils, "_prepare_input_file", lambda _args: ("input.mov", "prepared\n")
    )
    monkeypatch.setattr(
        runtime_flow_utils,
        "get_info_video",
        lambda _file: {
            "duration": 283,
            "video_duration": 283.2,
            "audio_durations": [283.2],
            "has_stream_video": True,
            "has_stream_audio": True,
        },
    )
    monkeypatch.setattr(runtime_flow_utils, "launch_encode_video", lambda *_args: (True, True))
    monkeypatch.setattr(runtime_flow_utils, "launch_encode_audio", lambda *_args: (True, "audio\n"))
    monkeypatch.setattr(
        runtime_flow_utils, "generate_overview", lambda *_args: (True, "overview\n")
    )
    monkeypatch.setattr(
        runtime_flow_utils,
        "_video_output_validation_targets",
        lambda *_args: [(str(output), str(output))],
    )
    monkeypatch.setattr(
        ffmpeg_runtime,
        "probe_stream_durations",
        lambda *_args, **_kwargs: ({"video": [75.92], "audio": [76.927]}, ""),
    )
    runtime_flow_utils.encode_log("Initial encoding details")
    context = main_orchestration_utils.MainFlowContext(
        apply_cli_config_fn=lambda _args: "",
        process_encoding_fn=runtime_flow_utils._process_encoding,
        add_info_video_fn=runtime_flow_utils.add_info_video,
        encode_log_fn=runtime_flow_utils.encode_log,
        encoding_validation_error_type=runtime_flow_utils.EncodingValidationError,
    )
    with pytest.raises(SystemExit) as exit_info:
        main_orchestration_utils.run_main_flow(SimpleNamespace(), context=context)
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    handler = VideoEncodingHandler()
    script_result = handler._fill_empty_streams_from_encoding_log(
        {"success": False, "returncode": 1, "stdout": captured.out, "stderr": captured.err},
        output_dir,
    )
    results = {
        "success": False,
        "error": handler._extract_script_error(script_result),
        "script_output": script_result,
    }
    handler.save_task_metadata("incomplete-task", results, output_dir)
    return output_dir, results


def test_incomplete_outputs_fail_with_specific_error_and_chronological_logs(incomplete_encoding):
    output_dir, results = incomplete_encoding
    error = results["error"]
    assert error.startswith("Encoding incomplete: '360p_timeout_clip.mp4', video stream 0:")
    assert "75.920s produced, 283.000s expected, approximately 207.080s missing" in error
    assert "Check source integrity and FFmpeg errors" in error
    assert str(output_dir) not in error
    metadata = json.loads((output_dir / "info_video.json").read_text())
    assert metadata["encode_result"] is False
    assert metadata["error"] == error
    assert "_output_validation_error" not in metadata
    raw_log = (output_dir / "encoding.log").read_text()
    assert raw_log.lstrip().startswith("Initial encoding details")
    assert "WARNING: " + error in raw_log
    assert "audio stream 0: 76.927s produced" in raw_log


@pytest.mark.asyncio
async def test_incomplete_diagnostic_survives_callback_persistence_and_recovery(
    incomplete_encoding, tmp_path, monkeypatch
):
    _, results = incomplete_encoding
    status_file = tmp_path / "statuses.json"
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(status_file))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.setattr(state, "_RUNNER_STATE", {**state._RUNNER_STATE, "task_statuses": {}})
    monkeypatch.setattr(task_routes.storage_manager, "base_path", str(tmp_path))
    monkeypatch.setattr(
        task_routes.task_dispatcher, "dispatch_task", AsyncMock(return_value=results)
    )
    notify = AsyncMock()
    email = AsyncMock()
    monkeypatch.setattr(task_routes, "notify_completion", notify)
    monkeypatch.setattr(task_routes, "send_task_failure_email", email)
    request = TaskRequest(
        task_id="incomplete-task",
        etab_name="test",
        app_name="test",
        task_type="encoding",
        source_url="https://example.test/input.mov",
        notify_url="",
        completion_callback="https://manager.test/task/completion",
    )
    await task_routes.process_task(request.task_id, request)
    status = state.get_task_status(request.task_id)
    assert status["status"] == "failed"  # The filename contains 'timeout', not a timeout error.
    assert status["error_message"] == results["error"]
    assert status["script_output"].startswith(
        "WARNING: " + results["error"] + "\n\n[info_script.log]"
    )
    assert "Initial encoding details" in status["script_output"]
    assert notify.await_args.args[2:] == ("failed", results["error"], status["script_output"])
    assert email.await_args.kwargs["error_message"] == results["error"]
    assert email.await_args.kwargs["script_output"] == status["script_output"]

    # Recovery reconstructs the displayed summary from saved error + script results;
    # no logs or new warning fields are added to the compact status file.
    persisted = json.loads(status_file.read_text())
    assert results["error"] in status_file.read_text()
    assert "script_output" not in status_file.read_text()
    assert persisted
    state._RUNNER_STATE["task_statuses"].clear()
    recovered = task_recovery.infer_workspace_terminal_status(
        request.task_id, {}, runtime=task_routes
    )
    recovered_status, error, output = recovered
    await task_recovery.finalize_recovered_task(
        request.task_id,
        {"completion_callback": request.completion_callback},
        status=recovered_status,
        error_message=error,
        script_output=output,
        runtime=task_routes,
    )
    assert notify.await_args.args[2:] == ("failed", results["error"], status["script_output"])
    assert state.get_task_status(request.task_id)["script_output"] == status["script_output"]


def test_studio_keeps_incomplete_encoding_error(incomplete_encoding):
    output_dir, encoding = incomplete_encoding
    results = StudioEncodingHandler()._build_results(
        encoding["script_output"], {"success": True}, output_dir, output_dir / "base.mp4"
    )
    assert results["success"] is False
    assert results["error"] == encoding["error"]


@pytest.mark.parametrize("duration,valid", [(100, True), (95, True), (94.999, False)])
def test_warning_preserves_five_second_tolerance_and_clears_previous_error(
    tmp_path, monkeypatch, duration, valid
):
    output = tmp_path / "output.mp4"
    output.write_bytes(b"encoded")
    monkeypatch.setattr(runtime_flow_utils, "_CUT_CONFIG", {})
    monkeypatch.setattr(
        runtime_flow_utils,
        "_video_output_validation_targets",
        lambda *_args: [(str(output), str(output))],
    )
    monkeypatch.setattr(
        ffmpeg_runtime,
        "probe_stream_durations",
        lambda *_args, **_kwargs: ({"video": [duration], "audio": []}, ""),
    )
    info = {"duration": 100, "_output_validation_error": "previous error"}
    actual_valid, log = runtime_flow_utils.validate_video_outputs(info, "input.mov")
    assert actual_valid is valid
    assert ("WARNING: Encoding incomplete:" in log) is not valid
    assert bool(info.get("_output_validation_error")) is not valid


def test_incomplete_warning_only_uses_explicit_diagnostics():
    assert incomplete_output_error("Invalid NAL unit size\nPacket corrupt\n") == ""
    assert incomplete_output_error("Encoding incomplete: first\nEncoding incomplete: second") == (
        "Encoding incomplete: first"
    )
    assert incomplete_output_error("details\nWARNING: Encoding incomplete: short output\n") == (
        "Encoding incomplete: short output"
    )


@pytest.mark.parametrize("error", [None, "Encoding failed", "Script timeout after 30 seconds"])
def test_other_failures_keep_their_logs(error):
    assert prepend_encoding_warning(error, "original log") == "original log"
    assert prepend_encoding_warning(error, None) is None


def test_warning_summary_handles_empty_logs_and_is_idempotent():
    error = "Encoding incomplete: short output"
    summary = "WARNING: " + error
    assert prepend_encoding_warning(error, None) == summary
    assert prepend_encoding_warning(error, "") == summary
    assert prepend_encoding_warning(error, summary) == summary
    detailed = summary + "\n\n[info_script.log]\ndetails"
    assert prepend_encoding_warning(error, detailed) == detailed
