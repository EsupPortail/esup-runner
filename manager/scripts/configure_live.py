#!/usr/bin/env python3
"""Connect an existing Manager .env to installed MediaMTX configurations.

Run with `uv run scripts/configure_live.py` from manager/.
Uses the component's Python environment and locked project dependencies.
Credentials are read from the local MediaMTX configuration, never printed.
Only application connection settings change; MediaMTX and services are untouched.
"""

import argparse
import io
import ipaddress
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Select this Manager's environment and installed MediaMTX instance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(role="manager")
    parser.add_argument(
        "--instance", default="manager", help="MediaMTX instance (default: manager)"
    )
    parser.add_argument("--config-root", type=Path, default=Path.home() / ".config/esup-runner")
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="Default: manager/.env"
    )
    parser.add_argument(
        "--no-journal",
        action="store_true",
        help="Do not add a systemd diagnostic source (e.g. MediaMTX in Docker)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report affected keys without writing"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.instance):
        parser.error("Instance names must contain 1-64 letters, digits, underscores or hyphens")
    return args


def read_private_input(path: Path) -> str:
    """Read a bounded regular file, rejecting symbolic links including parent directories."""
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError("Configuration paths must not contain symbolic links")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Configuration files must exist, be regular files and not exceed 1 MiB")
    return path.read_text(encoding="utf-8")


def load_media(directory: Path) -> dict:
    """Read installed YAML and listener overrides without evaluating dotenv interpolation.

    This helper supports the installer's plain RTMP/HTTP, internal-authentication
    profile. Nonstandard TLS/authentication or other environment overrides require
    explicit application settings instead of guessing connection credentials.
    """
    configuration = yaml.safe_load(read_private_input(directory / "mediamtx.yml"))
    environment = dotenv_values(
        stream=io.StringIO(read_private_input(directory / "mediamtx.env")),
        interpolate=False,
    )
    if not isinstance(configuration, dict):
        raise ValueError("MediaMTX configuration must be a YAML mapping")
    addresses = {
        "MTX_RTMPADDRESS": "rtmpAddress",
        "MTX_APIADDRESS": "apiAddress",
        "MTX_METRICSADDRESS": "metricsAddress",
    }
    if set(environment) - addresses.keys():
        raise ValueError(
            "Unsupported MediaMTX environment overrides; configure application connections manually"
        )
    for key, value in environment.items():
        configuration[addresses[key]] = value
    if (
        configuration.get("authMethod", "internal") != "internal"
        or configuration.get("apiEncryption", False)
        or configuration.get("rtmpEncryption", "no") != "no"
        or not configuration.get("api", False)
        or not configuration.get("rtmp", False)
    ):
        raise ValueError(
            "Automatic connection requires the installer's internal-auth, plain RTMP/HTTP profile"
        )
    return configuration


def credential(configuration: dict, username: str, action: str) -> str:
    """Find one named account with the required permission and a reusable cleartext password."""
    users = configuration.get("authInternalUsers", [])
    if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
        raise ValueError("Invalid MediaMTX internal users")
    matches = [user for user in users if user.get("user") == username]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one MediaMTX {username} account")
    user = matches[0]
    permissions = user.get("permissions", [])
    if not isinstance(permissions, list) or not any(
        isinstance(p, dict) and p.get("action") == action for p in permissions
    ):
        raise ValueError(f"MediaMTX {username} account lacks its required permission")
    password = user.get("pass")
    validate_password(password)
    return password


def validate_password(password: str) -> None:
    """Reject absent, placeholder, hashed or multiline passwords without revealing them."""
    if (
        not isinstance(password, str)
        or not password
        or password.startswith(("CHANGE_ME", "sha256:", "argon2:"))
        or any(c in password for c in "\r\n\x00")
    ):
        raise ValueError(
            "A nonempty cleartext password is required; placeholders and stored hashes cannot be reused"
        )


def endpoint(address: str) -> str:
    """Validate an installer IP:port listener and turn wildcard binds into local client addresses."""
    if not isinstance(address, str):
        raise ValueError("Missing MediaMTX listener address")
    parsed = urlsplit("http://" + address)
    host = ipaddress.ip_address(parsed.hostname or "")
    if not parsed.port or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Invalid MediaMTX listener address")
    if host.is_unspecified:
        host = ipaddress.ip_address("::1" if host.version == 6 else "127.0.0.1")
    return f"[{host}]:{parsed.port}" if host.version == 6 else f"{host}:{parsed.port}"


def connection_settings(args: argparse.Namespace, current: dict) -> dict[str, str]:
    """Derive Manager connection keys, preserving the operator's diagnostic source."""
    media = load_media(args.config_root / args.instance)
    values = {
        "LIVE_MEDIAMTX_API_URL": "http://" + endpoint(media.get("apiAddress")),
        "LIVE_MEDIAMTX_API_USER": "control",
        "LIVE_MEDIAMTX_API_PASSWORD": credential(media, "control", "api"),
        "LIVE_ENABLED": "true",
    }
    credential(media, "relay", "read")
    if (
        not args.no_journal
        and not current.get("LIVE_MEDIAMTX_LOG_FILE")
        and not current.get("LIVE_MEDIAMTX_LOG_UNIT")
    ):
        values["LIVE_MEDIAMTX_LOG_UNIT"] = f"esup-runner-mediamtx@{args.instance}.service"
    return values


def render_environment(original: str, values: dict[str, str], role: str) -> str:
    """Replace owned assignments, omit connection defaults and preserve unrelated text."""
    defaults = {
        "LIVE_MEDIAMTX_API_USER": "control",
        "LIVE_MEDIAMTX_API_URL": "http://127.0.0.1:" + ("9997" if role == "manager" else "10997"),
    }
    assignments = {}
    for key, value in values.items():
        if defaults.get(key) != value:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            # dotenv expands ${...} even in single quotes; refuse ambiguous secrets.
            if "${" in value:
                raise ValueError(
                    "Dotenv interpolation markers cannot be stored as literal credentials"
                )
            assignments[key] = f"{key}='{escaped}'\n"
    lines = []
    for binding in parse_stream(io.StringIO(original)):
        if binding.error:
            raise ValueError("Invalid application dotenv syntax")
        if binding.key in values:
            lines.append(assignments.pop(binding.key, ""))
        else:
            lines.append(binding.original.string)
    content = "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(assignments.values())
    return content


def replace_environment(path: Path, original: str, updated: str) -> None:
    """Back up privately and atomically replace an owned .env, rejecting concurrent edits."""
    if path.stat().st_uid != os.geteuid():
        raise ValueError("Run this helper as the owner of the application .env")
    if updated == original:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print("Application connection settings are already up to date.")
        return
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=path.name + ".live-backup-",
        dir=path.parent,
        delete=False,
    ) as backup:
        backup.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".env.live-",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        if read_private_input(path) != original:
            raise ValueError("Application .env changed during configuration; rerun the helper")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Updated {path} (mode 0600); private backup: {backup.name}")


def main(argv: list[str] | None = None) -> int:
    """Prepare and validate all changes before writing; never display configuration values."""
    args = parse_args(argv)
    try:
        original = read_private_input(args.env_file)
        current = dotenv_values(stream=io.StringIO(original), interpolate=False)
        values = connection_settings(args, current)
        updated = render_environment(original, values, args.role)
        print("Connection keys: " + ", ".join(sorted(values)))
        if args.dry_run:
            print("Dry run: no files changed; no secrets displayed.")
        else:
            replace_environment(args.env_file, original, updated)
        print("MediaMTX configuration and running services are unchanged.")
        return 0
    except (OSError, ValueError, TypeError, yaml.YAMLError, EOFError):
        # Parser exceptions can quote source lines containing passwords.
        print(
            "Connection failed; check file paths, permissions, listener addresses and MediaMTX accounts. Application .env left unchanged.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
