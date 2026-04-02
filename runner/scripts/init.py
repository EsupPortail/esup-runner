#!/usr/bin/env python3
"""Initialize required directories from environment, .env, and defaults.

Creates LOG_DIRECTORY, STORAGE_DIR, WHISPER_MODELS_DIR and
HUGGINGFACE_MODELS_DIR, then assigns ownership to the invoking user/group.

Must be run with sudo to set ownership correctly, but will fall back to current user if not.

Usage:
    sudo make init
    or sudo uv run scripts/init.py
    or sudo python3 scripts/init.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Mapping

# Environment keys that define required directories.
# Keep translation-model cache handling aligned with Whisper so both caches can
# be provisioned up front by `make init`.
ENV_KEYS = (
    "LOG_DIRECTORY",
    "STORAGE_DIR",
    "WHISPER_MODELS_DIR",
    "HUGGINGFACE_MODELS_DIR",
)

# Keep these defaults aligned with app/core/config.py.
DEFAULT_DIRECTORY_VALUES = {
    "LOG_DIRECTORY": "/var/log/esup-runner",
    "STORAGE_DIR": "/tmp/esup-runner/storage",
    "WHISPER_MODELS_DIR": "/home/esup-runner/.cache/esup-runner/whisper-models",
    "HUGGINGFACE_MODELS_DIR": "/home/esup-runner/.cache/esup-runner/huggingface",
}


def _strip_quotes(value: str) -> str:
    # Remove optional surrounding quotes to support values like "…" or '…'.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def read_env_file(env_path: Path) -> Dict[str, str]:
    # Minimal .env parser: supports KEY=VALUE lines and ignores comments.
    data: Dict[str, str] = {}
    if not env_path.exists():
        return data
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        data[key] = value
    return data


def resolve_target_uid_gid() -> tuple[int, int]:
    # When called via sudo, prefer the original user's uid/gid.
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid and sudo_gid:
        try:
            return int(sudo_uid), int(sudo_gid)
        except ValueError:
            pass
    return os.getuid(), os.getgid()


def resolve_directory_value(
    key: str,
    env: Dict[str, str],
    environ: Mapping[str, str] | None = None,
) -> str:
    # Match config.py precedence: process environment, then .env, then code default.
    if environ is None:
        environ = os.environ

    value = environ.get(key)
    if value is not None:
        return value

    if key in env:
        return env[key]

    return DEFAULT_DIRECTORY_VALUES.get(key, "")


def collect_directories(
    env: Dict[str, str],
    environ: Mapping[str, str] | None = None,
) -> Iterable[Path]:
    # Build the list of directories to create from environment/.env/default values.
    dirs = []
    for key in ENV_KEYS:
        value = resolve_directory_value(key, env, environ)
        if value:
            dirs.append(Path(value).expanduser())
    return dirs


def ensure_directory(path: Path, uid: int, gid: int) -> None:
    # Create the directory tree and set ownership.
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)


def main() -> int:
    # Locate the project root and load .env from there.
    project_root = Path(__file__).resolve().parents[1]
    env_path = project_root / ".env"

    env = read_env_file(env_path)
    dirs = list(collect_directories(env))
    if not dirs:
        print(f"No directories found in {env_path}.")
        return 0

    # Apply ownership to the calling user/group (or current if not sudo).
    uid, gid = resolve_target_uid_gid()

    # Create directories and report status.
    for directory in dirs:
        ensure_directory(directory, uid, gid)
        print(f"OK: {directory}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
