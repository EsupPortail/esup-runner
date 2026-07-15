"""Validates task state persistence, file resolution, and state normalization helper functions."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import app.core.config as config_module
import app.core.state as state_module


class _TrackingLock:
    """Count state lock acquisitions made by read helpers."""

    def __init__(self):
        self.acquisitions = 0

    def __enter__(self):
        self.acquisitions += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def test_state_normalization_helpers_cover_string_and_instance_scoping(monkeypatch):
    """Validate State normalization helpers cover string and instance scoping."""
    assert state_module._normalize_task_request({"a": 1}) == {"a": 1}
    assert state_module._normalize_task_request("   ") is None
    assert state_module._normalize_task_request('{"a": 1}') == {"a": 1}
    assert state_module._normalize_task_request("not-json") is None
    assert state_module._normalize_task_request("[1, 2, 3]") is None

    file_path = Path("/tmp/runner_task_statuses.json")

    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    assert state_module._instance_scoped_status_file(file_path) == file_path

    monkeypatch.setenv("RUNNER_INSTANCE_ID", "bad")
    assert state_module._instance_scoped_status_file(file_path) == file_path

    monkeypatch.setenv("RUNNER_INSTANCE_ID", "2")
    assert state_module._instance_scoped_status_file(file_path) == Path(
        "/tmp/runner_task_statuses.instance-2.json"
    )
    assert state_module._instance_scoped_status_file(Path("/tmp/runner_state")) == Path(
        "/tmp/runner_state.instance-2"
    )


def test_state_sanitize_and_task_store_cover_invalid_inputs():
    """Validate State sanitize and task store cover invalid inputs."""
    assert state_module._sanitize_task_payload_for_persistence("task", "bad") is None
    assert state_module._sanitize_task_payload_for_persistence("", {"status": "running"}) is None

    snapshot = state_module._RUNNER_STATE.copy()
    try:
        state_module._RUNNER_STATE["task_statuses"] = []
        task_store = state_module._get_task_status_store()
        assert task_store == {}
        assert isinstance(state_module._RUNNER_STATE["task_statuses"], dict)
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_resolve_task_status_file_covers_runtime_config_and_fallbacks(monkeypatch, caplog):
    """Validate State resolve task status file covers runtime config and fallbacks."""
    monkeypatch.delenv("RUNNER_TASK_STATUS_FILE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.delenv("STORAGE_DIR", raising=False)
    monkeypatch.setattr(state_module, "sys", SimpleNamespace(modules={}))

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(RUNNER_TASK_STATUS_FILE="/tmp/from-config.json", STORAGE_DIR=""),
    )
    assert state_module._resolve_task_status_file() == Path("/tmp/from-config.json")

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: SimpleNamespace(RUNNER_TASK_STATUS_FILE="", STORAGE_DIR="/tmp/storage-from-config"),
    )
    assert state_module._resolve_task_status_file() == Path(
        "/tmp/storage-from-config/runner_task_statuses.json"
    )

    monkeypatch.setenv("STORAGE_DIR", "/tmp/storage-from-env")
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("cannot read sensitive config")),
    )
    with caplog.at_level(logging.WARNING, logger=state_module.__name__):
        assert state_module._resolve_task_status_file() == Path(
            "/tmp/storage-from-env/runner_task_statuses.json"
        )
    assert "Failed to resolve runner task status file" in caplog.text
    assert "sensitive" not in caplog.text

    monkeypatch.delenv("STORAGE_DIR", raising=False)
    assert state_module._resolve_task_status_file() == Path(
        "/tmp/esup-runner/runner_task_statuses.json"
    )


def test_state_load_task_statuses_from_disk_returns_when_missing(monkeypatch, tmp_path):
    """Validate State load task statuses from disk returns when file is missing."""
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "missing-status.json"))

    snapshot = state_module._RUNNER_STATE.copy()
    try:
        state_module._RUNNER_STATE["task_statuses"] = {"existing": {"status": "running"}}

        state_module._load_task_statuses_from_disk()

        assert state_module._RUNNER_STATE["task_statuses"] == {"existing": {"status": "running"}}
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_persistence_logs_disk_full_without_payload(tmp_path, monkeypatch, caplog):
    """Keep in-memory task state and log a safe warning when the disk is full."""
    snapshot = state_module._RUNNER_STATE.copy()
    snapshot["task_statuses"] = dict(state_module._RUNNER_STATE.get("task_statuses", {}))
    status_file = tmp_path / "runner_task_statuses.json"

    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(status_file))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.setattr(
        state_module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full: secret-token")),
    )

    try:
        state_module._RUNNER_STATE["task_statuses"] = {
            "task-secret": {
                "task_id": "task-secret",
                "status": "running",
                "task_request": {"token": "secret-token"},
            }
        }

        with caplog.at_level(logging.WARNING, logger=state_module.__name__):
            state_module._persist_task_statuses()

        assert "Failed to atomically persist runner task statuses" in caplog.text
        assert "secret-token" not in caplog.text
        assert "task-secret" not in caplog.text
        assert state_module._RUNNER_STATE["task_statuses"]["task-secret"]["status"] == "running"
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_load_logs_invalid_json_without_payload(tmp_path, monkeypatch, caplog):
    """Keep current state and log a safe warning when persisted JSON is invalid."""
    snapshot = state_module._RUNNER_STATE.copy()
    snapshot["task_statuses"] = dict(state_module._RUNNER_STATE.get("task_statuses", {}))
    status_file = tmp_path / "runner_task_statuses.json"

    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(status_file))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    status_file.write_text('{"token": "secret-token"', encoding="utf-8")

    try:
        state_module._RUNNER_STATE["task_statuses"] = {"existing": {"status": "running"}}

        with caplog.at_level(logging.WARNING, logger=state_module.__name__):
            state_module._load_task_statuses_from_disk()

        assert "Failed to read or decode runner task status file as JSON" in caplog.text
        assert "secret-token" not in caplog.text
        assert state_module._RUNNER_STATE["task_statuses"] == {"existing": {"status": "running"}}
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_load_logs_unreadable_file_without_details(tmp_path, monkeypatch, caplog):
    """Keep current state and log a safe warning when the status file cannot be read."""
    snapshot = state_module._RUNNER_STATE.copy()
    snapshot["task_statuses"] = dict(state_module._RUNNER_STATE.get("task_statuses", {}))
    status_file = tmp_path / "runner_task_statuses.json"
    status_file.write_text("{}", encoding="utf-8")
    real_open = open

    def unreadable_status_file(file, *args, **kwargs):
        if Path(file) == status_file:
            raise PermissionError("secret permission details")
        return real_open(file, *args, **kwargs)

    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(status_file))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.setattr(state_module, "open", unreadable_status_file, raising=False)

    try:
        state_module._RUNNER_STATE["task_statuses"] = {"existing": {"status": "running"}}

        with caplog.at_level(logging.WARNING, logger=state_module.__name__):
            state_module._load_task_statuses_from_disk()

        assert "Failed to read or decode runner task status file as JSON" in caplog.text
        assert "secret permission details" not in caplog.text
        assert str(status_file) not in caplog.text
        assert state_module._RUNNER_STATE["task_statuses"] == {"existing": {"status": "running"}}
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_persist_and_load_cover_error_branches(tmp_path, monkeypatch):
    """Validate State persist and load cover error branches."""
    snapshot = state_module._RUNNER_STATE.copy()
    snapshot["task_statuses"] = dict(state_module._RUNNER_STATE.get("task_statuses", {}))
    status_file = tmp_path / "runner_task_statuses.json"

    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(status_file))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)

    try:
        # Skip blank task IDs during persistence.
        state_module._RUNNER_STATE["task_statuses"] = {"   ": {"status": "running"}}
        state_module._persist_task_statuses()
        assert not status_file.exists()

        # Best-effort persistence when atomic replace fails.
        state_module._RUNNER_STATE["task_statuses"] = {
            "task-1": {"task_id": "task-1", "status": "running"}
        }
        monkeypatch.setattr(
            state_module.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        state_module._persist_task_statuses()

        # Invalid JSON payload.
        status_file.write_text("{", encoding="utf-8")
        state_module._load_task_statuses_from_disk()

        # JSON root is not an object.
        status_file.write_text("[]", encoding="utf-8")
        state_module._load_task_statuses_from_disk()

        # Non-dict task payload and filtered payload requiring rewrite.
        status_file.write_text(
            json.dumps(
                {
                    "task-bad-payload": "oops",
                    "task-terminal": {"task_id": "task-terminal", "status": "completed"},
                }
            ),
            encoding="utf-8",
        )
        state_module._load_task_statuses_from_disk()
        assert state_module.get_task_status("task-bad-payload") is None
        assert state_module.get_task_status("task-terminal") is None
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_set_task_metadata_and_running_status_cover_remaining_branches():
    """Validate State set task metadata and running status cover remaining branches."""
    snapshot = state_module._RUNNER_STATE.copy()
    snapshot["task_statuses"] = dict(state_module._RUNNER_STATE.get("task_statuses", {}))

    try:
        state_module._RUNNER_STATE["task_statuses"] = {}

        # set_task_metadata early return on empty task ID.
        state_module.set_task_metadata("", process_pid=1111)
        assert state_module._RUNNER_STATE["task_statuses"] == {}

        # Ignore blank metadata keys.
        state_module.set_task_metadata("task-meta-extra", **{"   ": "ignored", "process_pid": 2222})
        payload = state_module.get_task_status("task-meta-extra")
        assert payload is not None
        assert payload["process_pid"] == 2222
        assert "   " not in payload

        # get_running_task_statuses when store is non-dict.
        state_module._RUNNER_STATE["task_statuses"] = []
        assert state_module.get_task_status("task-meta-extra") is None
        assert state_module.get_running_task_statuses() == {}

        # get_running_task_statuses skips non-dict payload entries.
        state_module._RUNNER_STATE["task_statuses"] = {
            "task-bad": "oops",
            "task-good": {"status": "running", "task_id": "task-good"},
        }
        assert state_module.get_running_task_statuses() == {
            "task-good": {"status": "running", "task_id": "task-good"}
        }
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)


def test_state_task_status_read_helpers_acquire_state_lock(monkeypatch):
    """Protect every public task status read with the runner state lock."""
    tracking_lock = _TrackingLock()
    monkeypatch.setattr(state_module, "_RUNNER_STATE_LOCK", tracking_lock)
    monkeypatch.setitem(
        state_module._RUNNER_STATE,
        "task_statuses",
        {"task-locked": {"task_id": "task-locked", "status": "running"}},
    )

    assert state_module.get_task_status("task-locked") is not None
    assert "task-locked" in state_module.get_running_task_statuses()
    assert "task-locked" in state_module.get_runner_state()["task_statuses"]
    assert tracking_lock.acquisitions == 3


def test_get_runner_state_returns_independent_nested_dictionaries(monkeypatch):
    """Mutating a runner state snapshot must not mutate the global state."""
    task_statuses = {
        "task-snapshot": {
            "task_id": "task-snapshot",
            "status": "running",
            "task_request": {"params": {"codec": "h264"}},
        }
    }
    monkeypatch.setitem(state_module._RUNNER_STATE, "task_statuses", task_statuses)

    snapshot = state_module.get_runner_state()
    snapshot["task_statuses"]["task-snapshot"]["status"] = "failed"
    snapshot["task_statuses"]["task-snapshot"]["task_request"]["params"]["codec"] = "av1"
    snapshot["task_statuses"]["new-task"] = {"status": "running"}

    assert state_module._RUNNER_STATE["task_statuses"] == {
        "task-snapshot": {
            "task_id": "task-snapshot",
            "status": "running",
            "task_request": {"params": {"codec": "h264"}},
        }
    }
