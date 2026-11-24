"""
Runner state store with optional shared persistence.

In production, the manager can run with multiple workers. Each worker has its own
process memory, so runner state must be shared across processes. This store uses a
JSON file protected by filelock in that mode.

In development, it falls back to an in-memory dictionary.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterator, MutableMapping, Optional, TypeVar, cast, overload
from uuid import uuid4

from filelock import FileLock, Timeout

from app.models.models import Runner

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_MANAGER_ROOT = Path(__file__).resolve().parents[2]


class RunnerStore(MutableMapping[str, Runner]):
    """Dictionary-like runner store with optional shared file-backed mode."""

    def __init__(
        self,
        *,
        shared_enabled: bool,
        state_file: str = "data/runners_state.json",
        lock_timeout: int = 10,
        is_token_allowed: Callable[[str], bool] | None = None,
    ):
        self.shared_enabled = shared_enabled
        self._memory: Dict[str, Runner] = {}
        self._memory_owners: dict[str, str] = {}
        self._memory_lock = RLock()
        self._state_file = self._resolve_state_file(state_file)
        self._owners_file = self._state_file.with_suffix(".owners.json")
        self._is_token_allowed = is_token_allowed
        self._lock: Optional[FileLock] = None

        if self.shared_enabled:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._lock = FileLock(f"{self._state_file}.lock", timeout=lock_timeout)
            self._initialize_state_file()
            logger.info(f"Runner store initialized in shared mode: {self._state_file}")
        else:
            logger.info("Runner store initialized in in-memory mode")

    @staticmethod
    def _resolve_state_file(state_file: str) -> Path:
        """
        Resolve state file path.
        Relative paths are anchored to manager project root to avoid cwd-dependent
        writes (e.g. creating /opt/esup-runner/data when launched from monorepo root).
        """
        path = Path(state_file)
        if path.is_absolute():
            return path
        return _MANAGER_ROOT / path

    def _with_lock(self, operation: Callable[[], _T]) -> _T:
        if not self.shared_enabled:
            with self._memory_lock:
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

    def _initialize_state_file(self) -> None:
        """
        Initialize shared state file once, under lock.

        On multi-worker startup, several processes can create RunnerStore at the
        same time. The existence check must happen while holding the file lock to
        avoid concurrent writes on the same temporary path.
        """
        if self._state_file.exists():
            return

        def _operation() -> None:
            if not self._state_file.exists():
                self._write_disk({})

        self._with_lock(_operation)

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
        self._write_json(self._state_file, serialized)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        """Replace one registry file atomically while holding the store lock."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _read_owners(self) -> dict[str, str]:
        """Read expired registrations under the same lock as active entries."""
        if not self.shared_enabled:
            return self._memory_owners
        try:
            with self._owners_file.open(encoding="utf-8") as f:
                owners = json.load(f)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            raise OSError("Could not read expired Runner registrations") from exc
        if not isinstance(owners, dict) or any(
            not isinstance(value, str) for value in owners.values()
        ):
            raise OSError("Invalid expired Runner registrations")
        return cast(dict[str, str], owners)

    def _visible(self, data: Dict[str, Runner]) -> Dict[str, Runner]:
        """Hide registrations whose secrets no longer have the Runner role."""
        if self._is_token_allowed is None:
            return data
        return {key: runner for key, runner in data.items() if self._eligible(runner)}

    def _eligible(self, runner: Runner) -> bool:
        """Check whether a registered Runner is still authorized."""
        allowed = self._is_token_allowed
        return allowed is None or bool(runner.token and allowed(runner.token))

    def __getitem__(self, key: str) -> Runner:
        def _operation() -> Runner:
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data[key]
            if not self._eligible(runner):
                raise KeyError(key)
            return runner

        return self._with_lock(_operation)

    def __setitem__(self, key: str, value: Runner) -> None:
        runner = self._normalize_runner(value)

        if not self.shared_enabled:
            existing = self._memory.get(key)
            runner.live_task_id = existing.live_task_id if existing else None
            if runner.live_task_id:
                runner.availability = "busy"
            self._memory[key] = runner
            return

        def _operation() -> None:
            data = self._read_disk()
            existing = data.get(key)
            runner.live_task_id = existing.live_task_id if existing else None
            if runner.live_task_id:
                runner.availability = "busy"
            data[key] = runner
            self._write_disk(data)

        self._with_lock(_operation)

    def register(self, runner: Runner) -> bool:
        """Register or refresh a runner only with its existing owner's token.

        Ownership verification and the write share one lock, including across
        manager workers. A rejected registration leaves the stored entry intact.
        Internal state updates continue to use the mapping interface.
        """

        def _operation() -> bool:
            data = self._read_disk() if self.shared_enabled else self._memory
            existing = data.get(runner.id)
            if (
                not runner.token
                or not self._eligible(runner)
                or (
                    existing is not None
                    and (
                        not existing.token or not hmac.compare_digest(existing.token, runner.token)
                    )
                )
            ):
                return False
            if existing is None:
                owner = self._read_owners().get(runner.id)
                if owner is not None and not hmac.compare_digest(
                    owner, hashlib.sha256(runner.token.encode()).hexdigest()
                ):
                    return False
            runner.live_task_id = existing.live_task_id if existing else None
            if runner.live_task_id:
                runner.availability = "busy"
            data[runner.id] = runner
            if self.shared_enabled:
                self._write_disk(data)
            return True

        return self._with_lock(_operation)

    def expire_inactive(self, key: str, cutoff: datetime) -> bool:
        """Remove stale capacity while retaining its identity or its Live reservation."""

        def operation() -> bool:
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data.get(key)
            if runner is None or runner.last_heartbeat >= cutoff:
                return False
            if runner.live_task_id:
                runner.status = "offline"
            else:
                if runner.token:
                    owners = self._read_owners()
                    owners[key] = hashlib.sha256(runner.token.encode()).hexdigest()
                    if self.shared_enabled:
                        self._write_json(self._owners_file, owners)
                del data[key]
            if self.shared_enabled:
                self._write_disk(data)
            return not runner.live_task_id

        return self._with_lock(operation)

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
            return iter(self._visible(self._memory))

        def _operation() -> list[str]:
            data = self._read_disk()
            return list(self._visible(data))

        keys = self._with_lock(_operation)
        return iter(keys)

    def __len__(self) -> int:
        if not self.shared_enabled:
            return len(self._visible(self._memory))

        def _operation() -> int:
            data = self._read_disk()
            return len(self._visible(data))

        return self._with_lock(_operation)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            self[key]
        except KeyError:
            return False
        return True

    def clear(self) -> None:
        if not self.shared_enabled:
            self._memory.clear()
            self._memory_owners.clear()
            return

        def operation() -> None:
            self._write_disk({})
            self._owners_file.unlink(missing_ok=True)

        self._with_lock(operation)

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
        def _operation() -> Runner | _T | None:
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data.get(key)
            return runner if runner is not None and self._eligible(runner) else default

        return self._with_lock(_operation)

    def items(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._visible(self._memory).items())

        def _operation():
            data = self._read_disk()
            return list(self._visible(data).items())

        return self._with_lock(_operation)

    def keys(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._visible(self._memory))

        def _operation():
            data = self._read_disk()
            return list(self._visible(data))

        return self._with_lock(_operation)

    def values(self):  # type: ignore[override]
        if not self.shared_enabled:
            return list(self._visible(self._memory).values())

        def _operation():
            data = self._read_disk()
            return list(self._visible(data).values())

        return self._with_lock(_operation)

    def try_reserve(self, key: str) -> Optional[Runner]:
        """Atomically reserve a runner by switching availability to ``busy``.

        Returns the updated runner when reservation succeeds, otherwise ``None``
        (runner missing or already busy).
        """

        def _operation() -> Optional[Runner]:
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data.get(key)
            if runner is None or not self._eligible(runner) or runner.availability != "available":
                return None
            runner.availability = "busy"
            data[key] = runner
            if self.shared_enabled:
                self._write_disk(data)
            return runner

        return self._with_lock(_operation)

    def reserve_live(
        self, key: str, task_id: str, *, confirmed_running: bool = False
    ) -> Optional[Runner]:
        """Reserve one whole Live Runner and return it, or None if unavailable.

        The Runner must declare Live, be online and unowned, with a heartbeat
        under one minute old. Set confirmed_running only after verifying that
        this session is already running, allowing recovery to adopt a busy or
        stale but unowned Runner. Heartbeat updates cannot clear the reservation.
        """

        def operation() -> Optional[Runner]:
            """Assign the session owner while holding the Runner store lock."""
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data.get(key)
            if (
                runner is None
                or not self._eligible(runner)
                or "live" not in runner.task_types
                or runner.status != "online"
            ):
                return None
            if not confirmed_running and datetime.now() - runner.last_heartbeat >= timedelta(
                minutes=1
            ):
                return None
            if (
                runner.availability != "available" and not confirmed_running
            ) or runner.live_task_id:
                return None
            runner.live_task_id = task_id
            runner.availability = "busy"
            data[key] = runner
            if self.shared_enabled:
                self._write_disk(data)
            return runner

        return self._with_lock(operation)

    def release_live(self, key: str, task_id: str) -> bool:
        """Release a matching session and return whether its reservation was cleared.

        A missing Runner or a different session owner returns False, preventing
        stale completion events from releasing a newer assignment.
        """

        def operation() -> bool:
            """Clear a matching session owner while holding the Runner store lock."""
            data = self._read_disk() if self.shared_enabled else self._memory
            runner = data.get(key)
            if runner is None or runner.live_task_id != task_id:
                return False
            runner.live_task_id = None
            runner.availability = "available"
            data[key] = runner
            if self.shared_enabled:
                self._write_disk(data)
            return True

        return self._with_lock(operation)
