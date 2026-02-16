"""Unit coverage for shared/in-memory runner store behavior."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

import pytest
from filelock import Timeout

from app.core.runner_store import RunnerStore
from app.models.models import Runner


def _runner(runner_id: str) -> Runner:
    return Runner(
        id=runner_id,
        url=f"http://{runner_id}.example",
        task_types=["encoding"],
        status="online",
        availability="available",
        last_heartbeat=datetime.now(),
        token="tok",
        version="1.0.0",
    )


def test_shared_store_is_visible_across_instances(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store_a = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)
    store_b = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    store_a["r1"] = _runner("r1")
    assert "r1" in store_b
    assert store_b["r1"].id == "r1"

    updated = store_b["r1"]
    updated.availability = "busy"
    store_b["r1"] = updated
    assert store_a["r1"].availability == "busy"

    del store_a["r1"]
    assert "r1" not in store_b


def test_in_memory_store_is_not_shared_between_instances(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store_a = RunnerStore(shared_enabled=False, state_file=str(state_path))
    store_b = RunnerStore(shared_enabled=False, state_file=str(state_path))

    store_a["r1"] = _runner("r1")
    assert "r1" in store_a
    assert "r1" not in store_b


def test_in_memory_store_mapping_helpers(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=False, state_file=str(state_path))

    store["r1"] = _runner("r1")
    assert store["r1"].id == "r1"
    assert store._with_lock(lambda: "ok") == "ok"
    assert len(store) == 1
    assert list(iter(store)) == ["r1"]
    assert "r1" in store
    assert 123 not in store

    store.update({"r2": _runner("r2")})
    assert set(store.keys()) == {"r1", "r2"}
    assert len(store.values()) == 2
    assert len(store.items()) == 2

    assert store.get("missing") is None
    default_runner = _runner("default")
    assert store.get("missing", default_runner).id == "default"

    del store["r2"]
    assert "r2" not in store

    store.clear()
    assert len(store) == 0


def test_normalize_runner_accepts_dict_and_rejects_other_types(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=False, state_file=str(state_path))

    data = _runner("r1").model_dump()
    store["r1"] = cast(Any, data)
    assert store["r1"].id == "r1"

    with pytest.raises(TypeError):
        store["bad"] = cast(Any, object())


def test_with_lock_timeout_raises(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    class TimeoutLock:
        def __enter__(self):
            raise Timeout("lock timeout")

        def __exit__(self, exc_type, exc, tb):
            return False

    store._lock = cast(Any, TimeoutLock())

    with pytest.raises(Timeout):
        store._with_lock(lambda: None)


def test_runner_to_dict_json_and_legacy_branches(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=False, state_file=str(state_path))

    class JsonOnlyRunner:
        def json(self):
            return json.dumps({"id": "r-json", "last_heartbeat": "2026-02-16T10:00:00"})

    class LegacyRunner:
        def dict(self):
            return {"id": "r-legacy", "last_heartbeat": datetime(2026, 2, 16, 10, 0, 0)}

    class ModelDumpRunner:
        def model_dump(self):
            return {"id": "r-model", "last_heartbeat": datetime(2026, 2, 16, 10, 0, 0)}

    json_dict = store._runner_to_dict(cast(Any, JsonOnlyRunner()))
    legacy_dict = store._runner_to_dict(cast(Any, LegacyRunner()))
    model_dump_dict = store._runner_to_dict(cast(Any, ModelDumpRunner()))

    assert json_dict["id"] == "r-json"
    assert isinstance(legacy_dict["last_heartbeat"], str)
    assert isinstance(model_dump_dict["last_heartbeat"], str)


def test_read_disk_error_paths_and_invalid_payloads(tmp_path, monkeypatch):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    # Missing state file.
    state_path.unlink()
    assert store._read_disk() == {}

    # Invalid JSON.
    state_path.write_text("{broken", encoding="utf-8")
    assert store._read_disk() == {}

    # Generic read failure.
    path_cls = type(state_path)
    original_open = path_cls.open

    def fail_open(path_obj, *args, **kwargs):
        if path_obj == state_path:
            raise OSError("boom")
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(path_cls, "open", fail_open)
    assert store._read_disk() == {}
    monkeypatch.setattr(path_cls, "open", original_open)

    # JSON root is not an object.
    state_path.write_text(json.dumps(["bad-root"]), encoding="utf-8")
    assert store._read_disk() == {}

    # Invalid payload types and invalid runner model.
    state_path.write_text(
        json.dumps({"bad-payload": "text", "bad-runner": {"id": "missing-required-fields"}}),
        encoding="utf-8",
    )
    assert store._read_disk() == {}


def test_shared_store_keys_values_items_and_get(tmp_path):
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    store["r1"] = _runner("r1")
    store["r2"] = _runner("r2")

    assert set(store.keys()) == {"r1", "r2"}
    assert sorted(list(iter(store))) == ["r1", "r2"]
    assert sorted(r.id for r in store.values()) == ["r1", "r2"]
    assert sorted(k for k, _ in store.items()) == ["r1", "r2"]
    assert store.get("missing") is None
