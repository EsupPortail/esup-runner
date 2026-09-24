"""Unit coverage for shared/in-memory runner store behavior."""

from __future__ import annotations

import json
import multiprocessing
from datetime import datetime
from pathlib import Path
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
    """Validate Shared store is visible across instances."""
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


def test_shared_store_clear_is_visible_across_instances_and_restart(tmp_path):
    """Keep counts and cleared state consistent across workers and restarts."""
    state_path = tmp_path / "runners_state.json"
    store_a = RunnerStore(shared_enabled=True, state_file=str(state_path))
    store_b = RunnerStore(shared_enabled=True, state_file=str(state_path))

    assert len(store_a) == len(store_b) == 0
    store_a["r1"] = _runner("r1")
    store_b["r2"] = _runner("r2")
    assert len(store_a) == len(store_b) == 2

    store_a.clear()
    assert len(store_a) == len(store_b) == 0
    assert store_b.get("r1") is None
    assert store_b.get("r2") is None
    assert json.loads(state_path.read_text()) == {}

    restarted = RunnerStore(shared_enabled=True, state_file=str(state_path))
    assert len(restarted) == 0
    assert restarted.register(_runner("r3")) is True
    assert len(store_a) == len(store_b) == 1
    assert store_b["r3"].id == "r3"


def test_shared_store_initial_state_write_happens_under_lock(tmp_path, monkeypatch):
    """Validate Shared store initial state write happens under lock."""
    state_path = tmp_path / "runners_state.json"
    lock_states: list[bool] = []
    original_write_disk = RunnerStore._write_disk

    def _spy_write_disk(self, data):
        assert self._lock is not None
        lock_states.append(self._lock.is_locked)
        return original_write_disk(self, data)

    monkeypatch.setattr(RunnerStore, "_write_disk", _spy_write_disk)
    RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    assert state_path.exists()
    assert lock_states == [True]


def test_in_memory_store_is_not_shared_between_instances(tmp_path):
    """Validate In memory store is not shared between instances."""
    state_path = tmp_path / "runners_state.json"
    store_a = RunnerStore(shared_enabled=False, state_file=str(state_path))
    store_b = RunnerStore(shared_enabled=False, state_file=str(state_path))

    store_a["r1"] = _runner("r1")
    assert "r1" in store_a
    assert "r1" not in store_b


def test_in_memory_store_mapping_helpers(tmp_path):
    """Validate In memory store mapping helpers."""
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
    """Validate Normalize runner accepts dict and rejects other types."""
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=False, state_file=str(state_path))

    data = _runner("r1").model_dump()
    store["r1"] = cast(Any, data)
    assert store["r1"].id == "r1"

    with pytest.raises(TypeError):
        store["bad"] = cast(Any, object())


def test_with_lock_timeout_raises(tmp_path):
    """Validate With lock timeout raises."""
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
    """Validate Runner to dict json and legacy branches."""
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
    """Validate Read disk error paths and invalid payloads."""
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
    """Validate Shared store keys values items and get."""
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    store["r1"] = _runner("r1")
    store["r2"] = _runner("r2")
    store.update({"r3": _runner("r3")})

    assert set(store.keys()) == {"r1", "r2", "r3"}
    assert sorted(list(iter(store))) == ["r1", "r2", "r3"]
    assert sorted(r.id for r in store.values()) == ["r1", "r2", "r3"]
    assert sorted(k for k, _ in store.items()) == ["r1", "r2", "r3"]
    assert store.get("missing") is None


def test_try_reserve_shared_store_is_atomic_on_availability(tmp_path):
    """Validate Try reserve shared store updates availability atomically."""
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path), lock_timeout=1)

    store["r1"] = _runner("r1")

    reserved = store.try_reserve("r1")
    assert reserved is not None
    assert reserved.availability == "busy"
    assert store["r1"].availability == "busy"
    assert store.try_reserve("r1") is None


def test_try_reserve_in_memory_store_handles_missing_and_busy(tmp_path):
    """Validate Try reserve in memory store handles missing and busy."""
    state_path = tmp_path / "runners_state.json"
    store = RunnerStore(shared_enabled=False, state_file=str(state_path))

    assert store.try_reserve("missing") is None

    store["r1"] = _runner("r1")
    assert store.try_reserve("r1") is not None
    assert store["r1"].availability == "busy"
    assert store.try_reserve("r1") is None


def test_default_state_file_is_anchored_to_manager_root():
    """Validate Default state file is anchored to manager root."""
    store = RunnerStore(shared_enabled=False)
    expected = (Path(__file__).resolve().parents[1] / "data" / "runners_state.json").resolve()
    assert store._state_file.resolve() == expected


@pytest.mark.parametrize("shared_enabled", [False, True])
def test_register_preserves_owner_and_allows_refresh(tmp_path, shared_enabled):
    store = RunnerStore(shared_enabled=shared_enabled, state_file=str(tmp_path / "runners.json"))
    original = _runner("r1")
    assert store.register(original) is True
    before = store["r1"].model_dump()

    impostor = original.model_copy(update={"token": "other", "url": "http://other.example"})
    assert store.register(impostor) is False
    assert store["r1"].model_dump() == before

    refreshed = original.model_copy(update={"url": "http://new.example", "availability": "busy"})
    assert store.register(refreshed) is True
    assert store["r1"].model_dump() == refreshed.model_dump()


@pytest.mark.parametrize("shared_enabled", [False, True])
@pytest.mark.parametrize("token", [None, ""])
def test_register_rejects_missing_owner_token(tmp_path, shared_enabled, token):
    store = RunnerStore(shared_enabled=shared_enabled, state_file=str(tmp_path / "runners.json"))
    ownerless = _runner("r1").model_copy(update={"token": token})
    assert store.register(ownerless) is False
    assert "r1" not in store

    # An existing entry without a token must not be claimed by an API client.
    store["r1"] = ownerless
    assert store.register(_runner("r1")) is False
    assert store["r1"].token == token


def test_register_checks_owner_and_writes_under_same_lock(tmp_path, monkeypatch):
    from app.core import runner_store

    store = RunnerStore(shared_enabled=True, state_file=str(tmp_path / "runners.json"))
    store["r1"] = _runner("r1")
    events = []

    def locked_spy(name, function):
        def wrapped(*args):
            assert store._lock.is_locked
            events.append(name)
            return function(*args)

        return wrapped

    monkeypatch.setattr(store, "_read_disk", locked_spy("read", store._read_disk))
    monkeypatch.setattr(store, "_write_disk", locked_spy("write", store._write_disk))
    monkeypatch.setattr(
        runner_store.hmac,
        "compare_digest",
        locked_spy("compare", runner_store.hmac.compare_digest),
    )
    assert store.register(_runner("r1")) is True
    assert events == ["read", "compare", "write"]
    events.clear()
    assert store.register(_runner("r1").model_copy(update={"token": "other"})) is False
    assert events == ["read", "compare"]


def test_register_preserves_disk_state_when_persistence_fails(tmp_path, monkeypatch):
    state_path = tmp_path / "runners.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path))
    assert store.register(_runner("r1"))
    before = state_path.read_bytes()

    def fail_write(_data):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_disk", fail_write)
    with pytest.raises(OSError, match="disk full"):
        store.register(_runner("r1").model_copy(update={"url": "http://new.example"}))
    assert state_path.read_bytes() == before


def test_register_after_offline_administrative_token_rotation(tmp_path):
    state_path = tmp_path / "runners.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path))
    original = _runner("r1")
    assert store.register(original)

    # Simulate an administrator updating the token with all Manager workers stopped.
    data = json.loads(state_path.read_text())
    data["r1"]["token"] = "rotated"
    state_path.write_text(json.dumps(data))

    restarted = RunnerStore(shared_enabled=True, state_file=str(state_path))
    assert restarted.register(original) is False
    assert restarted.register(original.model_copy(update={"token": "rotated"})) is True
    assert restarted["r1"].token == "rotated"
    assert restarted["r1"].url == original.url


def _register_in_process(state_file, runner, connection):
    """Compete for an identifier in a separate manager worker process."""
    with connection:
        store = RunnerStore(shared_enabled=True, state_file=state_file)
        connection.send("ready")
        connection.recv()
        connection.send(store.register(runner))


@pytest.mark.parametrize("already_registered", [False, True])
def test_register_concurrent_workers_preserve_one_owner(tmp_path, already_registered):
    state_path = tmp_path / "runners.json"
    store = RunnerStore(shared_enabled=True, state_file=str(state_path))
    owner = _runner("r1")
    if already_registered:
        assert store.register(owner)
    candidates = [owner, owner.model_copy(update={"token": "other", "url": "http://other.example"})]
    context = multiprocessing.get_context("spawn")
    workers = []
    connections = []
    try:
        for candidate in candidates:
            parent, child = context.Pipe()
            worker = context.Process(
                target=_register_in_process, args=(str(state_path), candidate, child)
            )
            worker.start()
            child.close()
            workers.append(worker)
            connections.append(parent)
        for connection in connections:
            assert connection.poll(10), "Worker failed to start"
            assert connection.recv() == "ready"
        for connection in connections:
            connection.send("register")
        results = []
        for connection in connections:
            assert connection.poll(10), "Registration did not finish"
            results.append(connection.recv())
        assert results.count(True) == 1
        if already_registered:
            assert results == [True, False]
        winner = candidates[results.index(True)]

        # A fresh store simulates a manager restart with no cached ownership.
        restarted = RunnerStore(shared_enabled=True, state_file=str(state_path))
        assert restarted["r1"].model_dump() == winner.model_dump()
        assert restarted.register(winner) is True
        assert restarted.register(candidates[results.index(False)]) is False
    finally:
        for worker in workers:
            worker.join(timeout=2)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)
        for connection in connections:
            connection.close()
