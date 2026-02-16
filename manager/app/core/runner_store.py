"""
Runner state store with optional shared persistence.

In production, the manager can run with multiple workers. Each worker has its own
process memory, so runner state must be shared across processes. This store uses a
JSON file protected by filelock in that mode.

In development, it falls back to an in-memory dictionary.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, MutableMapping, Optional, TypeVar, cast, overload

from filelock import FileLock, Timeout

from app.models.models import Runner

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class RunnerStore(MutableMapping[str, Runner]):
    """Dictionary-like runner store with optional shared file-backed mode."""

    def __init__(
        self,
        *,
        shared_enabled: bool,
        state_file: str = "data/runners_state.json",
        lock_timeout: int = 10,
    ):
        self.shared_enabled = shared_enabled
        self._memory: Dict[str, Runner] = {}
        self._state_file = Path(state_file)
        self._lock: Optional[FileLock] = None

        if self.shared_enabled:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._lock = FileLock(f"{self._state_file}.lock", timeout=lock_timeout)
            if not self._state_file.exists():
                self._write_disk({})
            logger.info(f"Runner store initialized in shared mode: {self._state_file}")
        else:
            logger.info("Runner store initialized in in-memory mode")

    def _with_lock(self, operation: Callable[[], _T]) -> _T:
        if not self.shared_enabled:
            return operation()

        assert self._lock is not None
        try:
            with self._lock:
                return operation()
        except Timeout:
            logger.error("Timeout while acquiring runner state lock")
            raise

    def _normalize_runner(self, value: Any) -> Runner:
        if isinstance(value, Runner):
            return value
        if isinstance(value, dict):
            return Runner(**value)
        raise TypeError(f"RunnerStore values must be Runner or dict, got {type(value)}")

    def _runner_to_dict(self, runner: Runner) -> Dict[str, Any]:
        # Keep JSON output stable and datetime-safe for cross-worker reloads.
        if hasattr(runner, "model_dump_json"):
            return cast(Dict[str, Any], json.loads(runner.model_dump_json()))
        if hasattr(runner, "json"):
            return cast(Dict[str, Any], json.loads(runner.json()))

        if hasattr(runner, "model_dump"):
            data = runner.model_dump()
        else:
            data = runner.dict()

        if isinstance(data.get("last_heartbeat"), datetime):
            data["last_heartbeat"] = data["last_heartbeat"].isoformat()
        return data

    def _read_disk(self) -> Dict[str, Runner]:
        if not self._state_file.exists():
            return {}

        try:
            with self._state_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            logger.error(f"Runner state JSON is invalid: {exc}")
            return {}
        except Exception as exc:
            logger.error(f"Failed to read runner state: {exc}")
            return {}

        if not isinstance(raw, dict):
            logger.error("Runner state JSON root must be an object")
            return {}

        runners_data: Dict[str, Runner] = {}
        for runner_id, payload in raw.items():
            if not isinstance(payload, dict):
                logger.warning(f"Skipping invalid runner payload for {runner_id}")
                continue
            try:
                runners_data[runner_id] = Runner(**payload)
            except Exception as exc:
                logger.warning(f"Skipping invalid runner {runner_id}: {exc}")
        return runners_data

    def _write_disk(self, data: Dict[str, Runner]) -> None:
        serialized = {runner_id: self._runner_to_dict(r) for runner_id, r in data.items()}
        tmp_path = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=2, ensure_ascii=False)
        tmp_path.replace(self._state_file)

    def __getitem__(self, key: str) -> Runner:
        if not self.shared_enabled:
            return self._memory[key]

        def _operation() -> Runner:
            data = self._read_disk()
            return data[key]

        return self._with_lock(_operation)

    def __setitem__(self, key: str, value: Runner) -> None:
        runner = self._normalize_runner(value)

        if not self.shared_enabled:
            self._memory[key] = runner
            return

        def _operation() -> None:
            data = self._read_disk()
            data[key] = runner
            self._write_disk(data)

        self._with_lock(_operation)

    def __delitem__(self, key: str) -> None:
        if not self.shared_enabled:
            del self._memory[key]
            return

        def _operation() -> None:
            data = self._read_disk()
            del data[key]
            self._write_disk(data)

        self._with_lock(_operation)

    def __iter__(self) -> Iterator[str]:
        if not self.shared_enabled:
            return iter(self._memory)

        def _operation() -> list[str]:
            data = self._read_disk()
            return list(data.keys())

        keys = self._with_lock(_operation)
        return iter(keys)

    def __len__(self) -> int:
        if not self.shared_enabled:
            return len(self._memory)

        def _operation() -> int:
            data = self._read_disk()
            return len(data)

        return self._with_lock(_operation)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False

        if not self.shared_enabled:
            return key in self._memory

        def _operation() -> bool:
            data = self._read_disk()
            return key in data

        return self._with_lock(_operation)

    def clear(self) -> None:
        if not self.shared_enabled:
            self._memory.clear()
            return

        self._with_lock(lambda: self._write_disk({}))

    def update(self, *args: Any, **kwargs: Any) -> None:
        updates = dict(*args, **kwargs)

        if not self.shared_enabled:
            for key, value in updates.items():
                self._memory[key] = self._normalize_runner(value)
            return

        def _operation() -> None:
            data = self._read_disk()
            for key, value in updates.items():
                data[key] = self._normalize_runner(value)
            self._write_disk(data)

        self._with_lock(_operation)

    @overload
    def get(self, key: str, default: None = None) -> Optional[Runner]: ...

    @overload
    def get(self, key: str, default: _T) -> Runner | _T: ...

    def get(self, key: str, default: _T | None = None) -> Runner | _T | None:
        if not self.shared_enabled:
            return self._memory.get(key, default)

        def _operation() -> Runner | _T | None:
            data = self._read_disk()
            return data.get(key, default)

        return self._with_lock(_operation)

    def items(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._memory.items())

        def _operation():
            data = self._read_disk()
            return list(data.items())

        return self._with_lock(_operation)

    def keys(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._memory.keys())

        def _operation():
            data = self._read_disk()
            return list(data.keys())

        return self._with_lock(_operation)

    def values(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._memory.values())

        def _operation():
            data = self._read_disk()
            return list(data.values())

        return self._with_lock(_operation)
