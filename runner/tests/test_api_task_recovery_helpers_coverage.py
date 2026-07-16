"""Validates task recovery helper functions for process monitoring and log collection."""

import builtins
import json

import pytest
from fastapi import HTTPException

from app.api.routes import task as task_module
from app.services import task_recovery


@pytest.fixture(autouse=True)
def clear_recovery_monitors(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUNNER_TASK_STATUS_FILE",
        str(tmp_path / "runner_task_statuses.test.json"),
    )
    task_module._RECOVERY_MONITORS.clear()
    yield
    task_module._RECOVERY_MONITORS.clear()


@pytest.mark.asyncio
async def test_task_recovery_route_wrappers_delegate_with_compatibility_runtime(monkeypatch):
    """Validate recovery lifecycle names delegate while retaining route monkeypatch hooks."""
    calls: list[tuple[str, object]] = []

    async def _recover(*, runtime):
        calls.append(("recover", runtime))

    async def _stop(*, runtime):
        calls.append(("stop", runtime))

    monkeypatch.setattr(task_recovery, "recover_running_tasks_after_restart", _recover)
    monkeypatch.setattr(task_recovery, "stop_recovery_monitors", _stop)

    await task_module.recover_running_tasks_after_restart()
    await task_module.stop_recovery_monitors()

    assert calls == [("recover", task_module), ("stop", task_module)]
    assert task_module._RECOVERY_MONITORS is task_recovery.RECOVERY_MONITORS


def test_task_helpers_parse_pid_and_is_process_alive_branches(monkeypatch):
    """Validate Task helpers parse pid and is process alive branches."""
    assert task_module._parse_process_pid({}) is None
    assert task_module._parse_process_pid({"process_pid": "oops"}) is None
    assert task_module._parse_process_pid({"process_pid": 0}) is None
    assert task_module._parse_process_pgid({}) is None
    assert task_module._parse_process_pgid({"process_pgid": "2222"}) == 2222

    def _raise_lookup(_pid, _sig):
        raise ProcessLookupError()

    monkeypatch.setattr(task_module.os, "kill", _raise_lookup)
    assert task_module._is_process_alive(1234) is False

    def _raise_permission(_pid, _sig):
        raise PermissionError()

    monkeypatch.setattr(task_module.os, "kill", _raise_permission)
    assert task_module._is_process_alive(1234) is True

    def _raise_os_error(_pid, _sig):
        raise OSError("bad pid")

    monkeypatch.setattr(task_module.os, "kill", _raise_os_error)
    assert task_module._is_process_alive(1234) is False

    monkeypatch.setattr(task_module.os, "kill", lambda *_args, **_kwargs: None)
    assert task_module._is_process_alive(1234) is True


def test_task_is_stop_requested_payload_branches():
    """Validate stop-request payload helper handles non-dicts and cancellation marker."""
    assert task_module._is_stop_requested_payload(None) is False
    assert task_module._is_stop_requested_payload("oops") is False
    assert (
        task_module._is_stop_requested_payload(
            {
                "error_message": task_module._CANCELLED_BY_USER_ERROR,
            }
        )
        is True
    )


def test_task_terminate_running_task_processes_attempts_all_sources(monkeypatch):
    """Validate running task termination probes PGID, PID and stale process matches."""
    calls = {
        "groups": [],
        "pids": [],
        "stale": [],
    }

    monkeypatch.setattr(task_module, "_parse_process_pgid", lambda _payload: 3210)
    monkeypatch.setattr(task_module, "_parse_process_pid", lambda _payload: 4321)
    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: True)
    monkeypatch.setattr(task_module, "_find_task_process_pids", lambda _task_id: [5555])
    monkeypatch.setattr(
        task_module,
        "_terminate_process_group",
        lambda pgid: calls["groups"].append(pgid) or False,
    )
    monkeypatch.setattr(
        task_module,
        "_terminate_process_pid",
        lambda pid: calls["pids"].append(pid) or True,
    )
    monkeypatch.setattr(
        task_module,
        "_terminate_stale_task_processes",
        lambda task_id, _payload: calls["stale"].append(task_id) or False,
    )

    attempted, terminated = task_module._terminate_running_task_processes("task-run", {})

    assert attempted is True
    assert terminated is True
    assert calls["groups"] == [3210]
    assert calls["pids"] == [4321]
    assert calls["stale"] == ["task-run"]


def test_task_find_and_terminate_stale_task_processes(monkeypatch, tmp_path):
    """Validate Task find and terminate stale task processes."""
    task_root = tmp_path / "task-stale"
    task_root.mkdir(parents=True)

    proc_root = tmp_path / "proc"
    matching_proc = proc_root / "101"
    non_matching_proc = proc_root / "202"
    matching_proc.mkdir(parents=True)
    non_matching_proc.mkdir(parents=True)
    (matching_proc / "cmdline").write_bytes(
        b"ffmpeg\x00-i\x00" + str(task_root / "input.mp4").encode("utf-8")
    )
    (non_matching_proc / "cmdline").write_bytes(b"ffmpeg\x00-i\x00/tmp/other/input.mp4")
    (proc_root / "self").mkdir(parents=True)

    monkeypatch.setattr(task_module, "_resolve_task_root_if_exists", lambda _task_id: task_root)

    assert task_module._find_task_process_pids("task-stale", proc_root=proc_root) == [101]

    killed_groups: list[int] = []
    killed_pids: list[int] = []
    alive_pids = {101}

    monkeypatch.setattr(task_module.os, "getpgid", lambda pid: 300 if pid == 101 else pid)
    monkeypatch.setattr(
        task_module,
        "_find_task_process_pids",
        lambda _task_id: [101],
    )
    monkeypatch.setattr(
        task_module,
        "_terminate_process_group",
        lambda pgid: killed_groups.append(pgid) or True,
    )
    monkeypatch.setattr(
        task_module,
        "_is_process_alive",
        lambda pid: pid in alive_pids,
    )
    monkeypatch.setattr(
        task_module, "_terminate_process_pid", lambda pid: killed_pids.append(pid) or True
    )

    assert task_module._terminate_stale_task_processes("task-stale", {"process_pgid": 301}) is True
    assert killed_groups == [300, 301]
    assert killed_pids == [101]


def test_task_proc_and_termination_error_branches(monkeypatch, tmp_path):
    """Validate Task proc parsing and termination error branches."""
    missing_proc_root = tmp_path / "missing-proc"
    assert task_module._read_proc_cmdline(404, proc_root=missing_proc_root) == ""
    assert task_module._iter_proc_pids(proc_root=missing_proc_root) == []

    class _BadName:
        def isdigit(self):
            return True

        def __int__(self):
            raise ValueError("bad pid")

    class _BadEntry:
        name = _BadName()

    monkeypatch.setattr(task_module.Path, "iterdir", lambda _self: [_BadEntry()])
    assert task_module._iter_proc_pids(proc_root=tmp_path) == []

    assert task_module._terminate_process_group(0) is False
    monkeypatch.setattr(task_module.os, "getpgrp", lambda: 42)
    assert task_module._terminate_process_group(42) is False

    monkeypatch.setattr(
        task_module.os,
        "getpgrp",
        lambda: (_ for _ in ()).throw(OSError("no current pgid")),
    )
    monkeypatch.setattr(task_module.os, "killpg", lambda *_args, **_kwargs: None)
    assert task_module._terminate_process_group(41) is True

    monkeypatch.setattr(task_module.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(
        task_module.os,
        "killpg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert task_module._terminate_process_group(43) is False

    warnings: list[str] = []
    monkeypatch.setattr(
        task_module.os,
        "killpg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        task_module.logger, "warning", lambda *args, **_kwargs: warnings.append(str(args[0]))
    )
    assert task_module._terminate_process_group(44) is False
    assert warnings

    monkeypatch.setattr(task_module.os, "killpg", lambda *_args, **_kwargs: None)
    assert task_module._terminate_process_group(45) is True

    assert task_module._terminate_process_pid(0) is False
    monkeypatch.setattr(task_module.os, "getpid", lambda: 55)
    assert task_module._terminate_process_pid(55) is False

    monkeypatch.setattr(
        task_module.os,
        "kill",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert task_module._terminate_process_pid(56) is False

    monkeypatch.setattr(
        task_module.os,
        "kill",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert task_module._terminate_process_pid(57) is False

    monkeypatch.setattr(task_module.os, "kill", lambda *_args, **_kwargs: None)
    assert task_module._terminate_process_pid(58) is True

    monkeypatch.setattr(task_module, "_find_task_process_pids", lambda _task_id: [100])
    monkeypatch.setattr(
        task_module.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(OSError("gone")),
    )
    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)
    assert task_module._terminate_stale_task_processes("task-stale", {}) is False


def test_task_collect_recovery_script_output_truncates_and_ignores_blank_paths(
    monkeypatch, tmp_path
):
    """Validate Task collect recovery script output truncates and ignores blank paths."""
    monkeypatch.setattr(task_module, "_resolve_task_root_if_exists", lambda _task_id: None)

    short_log = tmp_path / "stderr-short.log"
    short_log.write_text("short", encoding="utf-8")
    short_output = task_module._collect_recovery_script_output(
        "task-recovery-short",
        {
            "script_stdout_path": "   ",
            "script_stderr_path": str(short_log),
        },
    )
    assert short_output is not None
    assert short_output.startswith("[stderr-short.log]")

    long_log = tmp_path / "stderr.log"
    long_log.write_text("x" * 120500, encoding="utf-8")

    output = task_module._collect_recovery_script_output(
        "task-recovery",
        {
            "script_stdout_path": "   ",
            "script_stderr_path": str(long_log),
        },
    )

    assert output is not None
    assert len(output) <= task_module._MAX_RECOVERY_LOG_CHARS


def test_task_resolve_output_dir_and_has_useful_output_branches(monkeypatch, tmp_path):
    """Validate Task resolve output dir and has useful output branches."""
    task_root = tmp_path / "task"
    task_root.mkdir(parents=True)
    output_dir = task_root / "output"
    output_dir.mkdir(parents=True)

    monkeypatch.setattr(task_module, "_resolve_task_root_if_exists", lambda _task_id: task_root)
    monkeypatch.setattr(
        task_module, "_find_direct_child_entry", lambda *_args, **_kwargs: output_dir
    )
    monkeypatch.setattr(
        task_module,
        "_resolve_within_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    assert task_module._resolve_task_output_dir_if_exists("task-a") is None

    not_a_directory = task_root / "output_file"
    not_a_directory.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        task_module, "_resolve_within_base", lambda *_args, **_kwargs: not_a_directory
    )
    assert task_module._resolve_task_output_dir_if_exists("task-b") is None

    nested_dir = output_dir / "subdir"
    nested_dir.mkdir(parents=True)
    (output_dir / "task_metadata.json").write_text("{}", encoding="utf-8")
    (output_dir / "encoding.log").write_text("log", encoding="utf-8")
    assert task_module._has_useful_output_files(output_dir) is False


def test_task_has_useful_output_files_ignores_gap_repairs(tmp_path):
    """Validate Task has useful output files ignores internal gap repair artifacts."""
    output_dir = tmp_path / "output"
    repair_dir = output_dir / "_gap_repairs"
    repair_dir.mkdir(parents=True)
    (repair_dir / "subtitle.vtt").write_text("WEBVTT\n", encoding="utf-8")

    assert task_module._has_useful_output_files(output_dir) is False

    (output_dir / "subtitle.vtt").write_text("WEBVTT\n", encoding="utf-8")
    assert task_module._has_useful_output_files(output_dir) is True


def test_task_read_recovery_task_results_error_branches(monkeypatch, tmp_path):
    """Validate Task read recovery task results error branches."""
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    metadata_path = output_dir / "task_metadata.json"

    monkeypatch.setattr(
        task_module, "_resolve_task_output_dir_if_exists", lambda _task_id: output_dir
    )
    monkeypatch.setattr(
        task_module,
        "_find_direct_child_entry",
        lambda *_args, **_kwargs: metadata_path,
    )
    monkeypatch.setattr(
        task_module,
        "_resolve_within_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    assert task_module._read_recovery_task_results("task-a") is None

    metadata_path.mkdir(parents=True)
    monkeypatch.setattr(
        task_module, "_resolve_within_base", lambda *_args, **_kwargs: metadata_path
    )
    assert task_module._read_recovery_task_results("task-b") is None

    metadata_path.rmdir()
    metadata_path.write_text("{", encoding="utf-8")
    assert task_module._read_recovery_task_results("task-c") is None

    metadata_path.write_text(json.dumps(["not-a-dict"]), encoding="utf-8")
    assert task_module._read_recovery_task_results("task-d") is None

    metadata_path.write_text(json.dumps({"results": "not-a-dict"}), encoding="utf-8")
    assert task_module._read_recovery_task_results("task-e") is None


def test_task_ensure_recovery_manifest_error_branches(monkeypatch, tmp_path):
    """Validate Task ensure recovery manifest error branches."""
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=500, detail="boom")),
    )
    assert task_module._ensure_recovery_manifest("task-a") is False

    output_dir = tmp_path / "task" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "task_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=404, detail="missing")),
    )
    monkeypatch.setattr(
        task_module, "_resolve_task_output_dir_if_exists", lambda _task_id: output_dir
    )
    monkeypatch.setattr(task_module, "_has_useful_output_files", lambda _out: False)
    assert task_module._ensure_recovery_manifest("task-b") is False

    monkeypatch.setattr(task_module, "_has_useful_output_files", lambda _out: True)
    original_open = builtins.open

    def _failing_open(*_args, **_kwargs):
        raise OSError("cannot write")

    monkeypatch.setattr(builtins, "open", _failing_open)
    assert task_module._ensure_recovery_manifest("task-c") is False
    monkeypatch.setattr(builtins, "open", original_open)


def test_task_ensure_recovery_manifest_excludes_gap_repairs(tmp_path):
    """Validate Task ensure recovery manifest excludes internal gap repair files."""
    task_id = "task-recovery-gap"
    task_module.storage_manager.base_path = str(tmp_path)
    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "subtitle.vtt").write_text("WEBVTT\n", encoding="utf-8")
    repair_dir = output_dir / "_gap_repairs"
    repair_dir.mkdir()
    (repair_dir / "subtitle.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (repair_dir / "window.mp3").write_text("audio", encoding="utf-8")

    assert task_module._ensure_recovery_manifest(task_id) is True

    manifest = json.loads((tmp_path / task_id / "manifest.json").read_text())
    assert manifest["files"] == ["subtitle.vtt"]


def test_task_infer_workspace_terminal_status_returns_none_when_manifest_creation_fails(
    monkeypatch,
):
    """Validate Task infer workspace terminal status returns none when manifest creation fails."""
    monkeypatch.setattr(
        task_module,
        "_read_recovery_task_results",
        lambda _task_id: {"success": True, "script_output": {"ok": True}},
    )
    monkeypatch.setattr(
        task_module, "_collect_recovery_script_output", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(task_module, "_ensure_recovery_manifest", lambda _task_id: False)

    assert task_module._infer_workspace_terminal_status("task-inf", {}) is None


def test_task_get_owned_statuses_and_restart_attempts_branches(monkeypatch):
    """Validate Task get owned statuses and restart attempts branches."""
    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")
    monkeypatch.setattr(task_module, "get_runner_state", lambda: {"task_statuses": []})
    assert task_module._get_owned_task_statuses({"running"}) == {}

    monkeypatch.setattr(
        task_module,
        "get_runner_state",
        lambda: {
            "task_statuses": {
                "task-non-dict": "oops",
                "   ": {"status": "running", "runner_id": "runner-a"},
                "task-ok": {"status": "running", "runner_id": "runner-a"},
            }
        },
    )
    assert task_module._get_owned_task_statuses({"running"}) == {
        "task-ok": {"status": "running", "runner_id": "runner-a"}
    }
    assert task_module._get_recovery_restart_attempts({"recovery_restart_attempts": "bad"}) == 0


def test_task_load_recovery_task_request_branches(monkeypatch):
    """Validate Task load recovery task request branches."""
    assert task_module._load_recovery_task_request("task-a", {"task_request": "{"}) is None

    request = task_module._load_recovery_task_request(
        "task-b",
        {
            "completion_callback": "http://manager/callback",
            "task_request": {
                "task_id": "task-b",
                "etab_name": "UM",
                "app_name": "Pod",
                "task_type": "encoding",
                "source_url": "https://example.org/video.mp4",
                "parameters": {},
                "notify_url": "http://notify",
            },
        },
    )
    assert request is not None
    assert request.completion_callback == "http://manager/callback"

    monkeypatch.setattr(
        task_module.TaskRequest,
        "model_validate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid")),
    )
    assert (
        task_module._load_recovery_task_request(
            "task-c",
            {
                "task_request": {
                    "task_id": "task-c",
                    "etab_name": "UM",
                    "app_name": "Pod",
                    "task_type": "encoding",
                    "source_url": "https://example.org/video.mp4",
                    "parameters": {},
                    "notify_url": "http://notify",
                }
            },
        )
        is None
    )


def test_task_schedule_failed_task_restart_when_attempt_limit_reached():
    """Validate Task schedule failed task restart when attempt limit reached."""
    payload = {"recovery_restart_attempts": task_module._RECOVERY_AUTO_RESTART_MAX_ATTEMPTS}
    assert task_module._schedule_failed_task_restart("task-max", payload) is False


@pytest.mark.asyncio
async def test_task_recover_failed_user_stopped_task_is_not_restarted(monkeypatch):
    """Validate user-stopped failed tasks are not auto-restarted during recovery."""
    monkeypatch.setattr(
        task_module,
        "_schedule_failed_task_restart",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("user-stopped tasks must not be restarted")
        ),
    )

    restarted = await task_module._recover_failed_task(
        "task-user-stopped",
        {"error_message": "Cancelled by user.", "status": "failed"},
    )

    assert restarted is False


def test_task_schedule_failed_task_restart_cleans_stale_processes(monkeypatch):
    """Validate Task schedule failed task restart cleans stale processes."""
    payload = {
        "task_request": {
            "task_id": "task-restart-clean",
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
        }
    }
    cleaned: list[str] = []
    created: list[bool] = []

    monkeypatch.setattr(
        task_module,
        "_terminate_stale_task_processes",
        lambda task_id, _payload: cleaned.append(task_id) or True,
    )

    def _fake_create_task(coro):
        created.append(True)
        coro.close()
        return object()

    monkeypatch.setattr(task_module.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    assert task_module._schedule_failed_task_restart("task-restart-clean", payload) is True
    assert cleaned == ["task-restart-clean"]
    assert created == [True]


@pytest.mark.asyncio
async def test_task_reconcile_recovered_task_non_404_is_raised(monkeypatch):
    """Validate Task reconcile recovered task non 404 is raised."""
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=500, detail="boom")),
    )
    with pytest.raises(HTTPException) as exc:
        await task_module._reconcile_recovered_task("task-err", {})
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_task_reconcile_recovered_task_fallback_failure_path(monkeypatch):
    """Validate Task reconcile recovered task fallback failure path."""
    captured = {}

    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=404, detail="missing")),
    )
    monkeypatch.setattr(task_module, "_parse_process_pid", lambda _payload: None)
    monkeypatch.setattr(
        task_module, "_infer_workspace_terminal_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        task_module, "_collect_recovery_script_output", lambda *_args, **_kwargs: "logs"
    )

    async def _fake_finalize(task_id, payload, *, status, error_message=None, script_output=None):
        captured["task_id"] = task_id
        captured["status"] = status
        captured["error_message"] = error_message
        captured["script_output"] = script_output

    monkeypatch.setattr(task_module, "_finalize_recovered_task", _fake_finalize)

    status = await task_module._reconcile_recovered_task("task-fallback", {"error_message": "boom"})

    assert status == "failed"
    assert captured["task_id"] == "task-fallback"
    assert captured["status"] == "failed"
    assert captured["error_message"] == "boom"
    assert captured["script_output"] == "logs"


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_exception_and_finally(monkeypatch):
    """Validate Task monitor recovered task exception and finally."""
    errors: list[str] = []

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "running"})

    async def _raise_reconcile(*_args, **_kwargs):
        raise RuntimeError("reconcile boom")

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _raise_reconcile)
    monkeypatch.setattr(
        task_module.logger, "error", lambda *args, **_kwargs: errors.append("logged")
    )

    task_module._RECOVERY_MONITORS["task-mon"] = object()
    await task_module._monitor_recovered_task("task-mon")

    assert "task-mon" not in task_module._RECOVERY_MONITORS
    assert errors == ["logged"]


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_returns_when_payload_missing(monkeypatch):
    """Validate Task monitor recovered task returns when payload missing."""

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: None)
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called when payload is missing")
        ),
    )

    task_module._RECOVERY_MONITORS["task-missing"] = object()
    await task_module._monitor_recovered_task("task-missing")
    assert "task-missing" not in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_returns_when_status_not_running(monkeypatch):
    """Validate Task monitor recovered task returns when status not running."""

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "failed"})
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called when task is not running")
        ),
    )

    task_module._RECOVERY_MONITORS["task-failed"] = object()
    await task_module._monitor_recovered_task("task-failed")
    assert "task-failed" not in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_refreshes_on_terminal_state(monkeypatch):
    """Validate Task monitor recovered task refreshes on terminal state."""
    refreshed = {"called": 0}

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "running"})
    monkeypatch.setattr(
        task_module,
        "_refresh_availability_from_recovered_state",
        lambda: refreshed.__setitem__("called", refreshed["called"] + 1),
    )

    async def _terminal_status(*_args, **_kwargs):
        return "completed"

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _terminal_status)

    task_module._RECOVERY_MONITORS["task-done"] = object()
    await task_module._monitor_recovered_task("task-done")

    assert refreshed["called"] == 1
    assert "task-done" not in task_module._RECOVERY_MONITORS


def test_task_schedule_recovery_monitor_skips_existing_and_creates_new(monkeypatch):
    """Validate Task schedule recovery monitor skips existing and creates new."""

    class _ExistingTask:
        def done(self):
            return False

    task_module._RECOVERY_MONITORS["task-existing"] = _ExistingTask()

    monkeypatch.setattr(
        task_module.asyncio,
        "create_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create task")),
    )
    task_module._schedule_recovery_monitor("task-existing")

    created = {}

    class _CreatedTask:
        def done(self):
            return False

    def _fake_create_task(coro):
        created["created"] = True
        coro.close()
        return _CreatedTask()

    monkeypatch.setattr(task_module.asyncio, "create_task", _fake_create_task)
    task_module._schedule_recovery_monitor("task-new")

    assert created["created"] is True
    assert "task-new" in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_recover_owned_running_and_failed_tasks_handle_exceptions(monkeypatch):
    """Validate Task recover owned running and failed tasks handle exceptions."""

    async def _raise_running(*_args, **_kwargs):
        raise RuntimeError("running recovery failed")

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _raise_running)
    await task_module._recover_owned_running_tasks({"task-r": {}})

    async def _raise_failed(*_args, **_kwargs):
        raise RuntimeError("failed recovery failed")

    monkeypatch.setattr(task_module, "_recover_failed_task", _raise_failed)
    restarted = await task_module._recover_owned_failed_tasks({"task-f": {}})
    assert restarted == 0


@pytest.mark.asyncio
async def test_task_recover_owned_running_tasks_returns_early_when_empty(monkeypatch):
    """Validate Task recover owned running tasks returns early when empty."""
    monkeypatch.setattr(
        task_module.logger,
        "info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("logger.info must not be called for empty running_tasks")
        ),
    )
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called for empty running_tasks")
        ),
    )
    monkeypatch.setattr(
        task_module,
        "_schedule_recovery_monitor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("monitor must not be scheduled for empty running_tasks")
        ),
    )

    await task_module._recover_owned_running_tasks({})
