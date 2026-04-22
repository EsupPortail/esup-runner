import json
from pathlib import Path
from types import SimpleNamespace

import app.core.config as config_module
import app.core.state as state_module


def test_state_normalization_helpers_cover_string_and_instance_scoping(monkeypatch):
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


def test_state_resolve_task_status_file_covers_runtime_config_and_fallbacks(monkeypatch):
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
        lambda: (_ for _ in ()).throw(RuntimeError("cannot read config")),
    )
    assert state_module._resolve_task_status_file() == Path(
        "/tmp/storage-from-env/runner_task_statuses.json"
    )

    monkeypatch.delenv("STORAGE_DIR", raising=False)
    assert state_module._resolve_task_status_file() == Path(
        "/tmp/esup-runner/runner_task_statuses.json"
    )


def test_state_persist_and_load_cover_error_branches(tmp_path, monkeypatch):
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
