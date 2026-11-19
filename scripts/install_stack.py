#!/usr/bin/env python3
"""Create private stack configuration and verify an installed native stack.

This helper runs as the service account. The standalone shell installer handles
system packages, source checkout, directory ownership and systemd services.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import bcrypt
from dotenv import dotenv_values

ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.@-]*)\s*=")
ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]*$", re.ASCII)
TOKEN = re.compile(r"^[A-Za-z0-9_~.+/=-]+$", re.ASCII)
HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", re.ASCII)


def validate_host(value: str) -> str:
    """Accept an advertised DNS name or IP, rejecting wildcard and unsafe input."""
    if not value or value != value.strip() or any(ord(char) < 33 for char in value):
        raise ValueError("Hosts must be DNS names or IP addresses without whitespace.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.rstrip(".").split(".")
        if (
            len(value) > 253
            or not all(HOST_LABEL.fullmatch(label) for label in labels)
            or all(char in "0123456789." for char in value)
        ):
            raise ValueError("Hosts must be valid DNS names or IP addresses.") from None
    else:
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Advertised hosts must not be wildcard or multicast addresses.")
    return value


def host_for_url(host: str) -> str:
    """Bracket IPv6 literals when forming an HTTP URL."""
    return f"[{host}]" if ":" in host else host


def validate_manager_url(value: str) -> str:
    """Validate a private Manager base URL without credentials or extra URL parts."""
    if any(ord(char) < 33 for char in value):
        raise ValueError("Manager URL must not contain whitespace or control characters.")
    try:
        parts = urlsplit(value)
        host = validate_host(parts.hostname or "")
        port = parts.port
    except ValueError:
        raise ValueError("Manager URL must contain a valid host and port.") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise ValueError("Manager URL must be an HTTP(S) base URL without credentials or path.")
    authority = host_for_url(host)
    if port is not None:
        authority += f":{port}"
    return f"{parts.scheme}://{authority}"


def read_env(path: Path) -> dict[str, str]:
    """Read dotenv values without interpolation, refusing symlinked configuration."""
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise ValueError("Configuration paths must not contain symbolic links.")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Configuration must be a regular file no larger than 1 MiB.")
    return {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}


def manager_url(env: dict[str, str]) -> str:
    """Compute the private Manager URL using its declared network settings."""
    host = validate_host(env.get("MANAGER_HOST", "127.0.0.1"))
    return validate_manager_url(
        f"{env.get('MANAGER_PROTOCOL', 'http')}://{host_for_url(host)}:"
        f"{env.get('MANAGER_PORT', '8081')}"
    )


def validate_secret(value: str, label: str, *, token: bool = False) -> str:
    """Reject missing, example or unsafe credentials without echoing their value."""
    if (
        not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "CHANGE_ME" in value.upper()
        or "CHANGE-ME" in value.upper()
        or (token and not TOKEN.fullmatch(value))
    ):
        raise ValueError(f"{label} is missing, unsafe, or still an example value.")
    return value


def authorized_token(env: dict[str, str]) -> str:
    """Select an existing Manager token, preferring the dedicated runner token."""
    values = [env.get("AUTHORIZED_TOKENS__runners", "")]
    values.extend(value for key, value in env.items() if key.startswith("AUTHORIZED_TOKENS__"))
    for value in values:
        try:
            return validate_secret(value, "Manager runner token", token=True)
        except ValueError:
            continue
    raise ValueError("The existing Manager configuration has no usable API token.")


def render_env(example: str, overrides: dict[str, str]) -> str:
    """Keep example comments while replacing defaults and removing sample credentials."""
    remaining = dict(overrides)
    lines = []
    for line in example.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            key = match.group(1)
            if key in overrides:
                if key in remaining:
                    lines.append(f"{key}={remaining.pop(key)}")
                continue
            if key.startswith(("AUTHORIZED_TOKENS__", "ADMIN_USERS__")):
                continue
        lines.append(line)
    if remaining:
        lines.extend(["", "# Settings generated by install-stack.sh"])
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def create_env(path: Path, content: str) -> None:
    """Atomically create a mode-0600 configuration without replacing existing files."""
    descriptor, temporary = tempfile.mkstemp(prefix=".install-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def secret_input(values: dict[str, str], key: str, label: str, interactive: bool) -> str:
    """Read a supplied secret or prompt privately when interactive input is allowed."""
    value = values.get(key, "")
    if not value and interactive:
        value = getpass.getpass(f"{label}: ")
        if key == "INSTALL_ADMIN_PASSWORD" and getpass.getpass("Confirm password: ") != value:
            raise ValueError("Administrator passwords do not match.")
    return validate_secret(value, label, token=key == "INSTALL_RUNNER_TOKEN")


def selected_components(component: str) -> tuple[str, ...]:
    """Expand the requested stack scope into component names."""
    return ("manager", "runner") if component == "both" else (component,)


def existing_configuration(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Load both roles and verify ownership for the selected configuration files."""
    existing: dict[str, dict[str, str]] = {}
    for component in ("manager", "runner"):
        directory = args.root / component
        if directory.is_symlink():
            raise ValueError("Component directories must not be symbolic links.")
        existing[component] = read_env(directory / ".env")
        if (directory / ".env").exists() and component in selected_components(args.component):
            if not existing[component]:
                raise ValueError("An existing component configuration is empty.")
            owner = existing[component].get("SERVICE_USER", "esup-runner")
            if owner != args.service_user:
                raise ValueError("Existing SERVICE_USER differs from the installation account.")
    return existing


def validate_existing_options(
    args: argparse.Namespace, existing: dict[str, dict[str, str]]
) -> None:
    """Reject feature switches that would require rewriting an existing deployment."""
    selected = selected_components(args.component)
    if "runner" in selected and existing["runner"]:
        tasks = existing["runner"].get("RUNNER_TASK_TYPES", "")
        if args.with_transcription and "transcription" not in tasks:
            raise ValueError(
                "Configure transcription in the existing runner/.env before installing."
            )
        if args.with_live and "live" not in tasks:
            raise ValueError("Configure Live in the existing runner/.env before installing.")
    if "manager" in selected and existing["manager"] and args.with_live:
        if existing["manager"].get("LIVE_ENABLED", "false").lower() != "true":
            raise ValueError("Configure Live in the existing manager/.env before installing.")


def connection_url(
    args: argparse.Namespace, existing: dict[str, dict[str, str]], missing: list[str]
) -> tuple[str, str]:
    """Resolve the intended Manager and its local counterpart without changing configuration."""
    local_manager = existing["manager"]
    local_url = manager_url(local_manager) if local_manager else ""
    if "manager" in missing:
        host = validate_host(args.manager_host)
        local_url = f"http://{host_for_url(host)}:8081"
    target_url = validate_manager_url(args.manager_url) if args.manager_url else local_url
    if not target_url:
        raise ValueError("A remote Runner requires --manager-url.")
    if "manager" in selected_components(args.component) and target_url != local_url:
        raise ValueError("--manager-url must match the Manager installed on this host.")
    return target_url, local_url


def connection_token(
    args: argparse.Namespace,
    existing: dict[str, dict[str, str]],
    missing: list[str],
    supplied: dict[str, str],
    target_url: str,
    local_url: str,
) -> str:
    """Reuse local credentials or privately obtain the remote Manager token."""
    if "manager" in missing:
        runner_env = existing["runner"]
        if runner_env and validate_manager_url(runner_env.get("MANAGER_URL", "")) == target_url:
            return validate_secret(runner_env.get("RUNNER_TOKEN", ""), "Runner token", token=True)
        if runner_env and "runner" in selected_components(args.component):
            raise ValueError("The existing Runner points to a different Manager.")
        return secrets.token_urlsafe(32)
    if existing["manager"] and target_url == local_url:
        return authorized_token(existing["manager"])
    if "runner" in missing:
        return secret_input(
            supplied,
            "INSTALL_RUNNER_TOKEN",
            "Manager runner token",
            not args.non_interactive,
        )
    return ""


def validate_live_secret(args: argparse.Namespace, supplied: dict[str, str]) -> None:
    """Require unattended remote Live input before creating any application files."""
    selected = selected_components(args.component)
    if not args.with_live or "runner" not in selected or "manager" in selected:
        return
    if live_configured(args.root, "runner"):
        return
    manager = read_env(args.root / "manager/.env")
    runner = read_env(args.root / "runner/.env")
    target = args.manager_url or runner.get("MANAGER_URL", "")
    local = bool(manager and (not target or validate_manager_url(target) == manager_url(manager)))
    if local:
        if manager.get("LIVE_ENABLED", "false").lower() != "true":
            raise ValueError("Configure Live on the local Manager before installing a Live Runner.")
        if (Path(args.service_home) / ".config/esup-runner/manager/mediamtx.yml").is_file():
            return
    if args.non_interactive:
        validate_secret(
            supplied.get("INSTALL_LIVE_RELAY_PASSWORD", ""),
            "Manager Live relay password",
        )


def manager_values(
    args: argparse.Namespace, supplied: dict[str, str], runner_token: str
) -> dict[str, str]:
    """Generate private Manager credentials and configure the selected address."""
    password = secret_input(
        supplied, "INSTALL_ADMIN_PASSWORD", "Administrator password", not args.non_interactive
    )
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Administrator passwords must be at most 72 UTF-8 bytes.")
    manager_host = validate_host(args.manager_host)
    try:
        address = ipaddress.ip_address(manager_host)
        bind_host = (
            manager_host if address.is_loopback else ("::" if address.version == 6 else "0.0.0.0")
        )
    except ValueError:
        bind_host = "127.0.0.1" if manager_host.lower().rstrip(".") == "localhost" else "0.0.0.0"
    return {
        "MANAGER_HOST": manager_host,
        "MANAGER_BIND_HOST": bind_host,
        "AUTHORIZED_TOKENS__runners": runner_token,
        "AUTHORIZED_TOKENS__app": secrets.token_urlsafe(32),
        "OPENAPI_COOKIE_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERS__admin": bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(),
        "LIVE_ENABLED": "true" if args.with_live else "false",
    }


def runner_values(args: argparse.Namespace, target_url: str, runner_token: str) -> dict[str, str]:
    """Prepare CPU Runner tasks, preserving separate capacity for optional Live."""
    runner_host = validate_host(args.runner_host)
    tasks = "encoding,studio" + (",transcription" if args.with_transcription else "")
    groups = f"1x({tasks})" + (",1x(live)" if args.with_live else "")
    return {
        "RUNNER_HOST": runner_host,
        "RUNNER_TASK_TYPES": f"[{groups}]",
        "RUNNER_INSTANCES": "2" if args.with_live else "1",
        "MANAGER_URL": target_url,
        "RUNNER_TOKEN": runner_token,
    }


def configure(args: argparse.Namespace, supplied: dict[str, str]) -> None:
    """Create missing component configuration, preserving existing settings verbatim."""
    if not ACCOUNT.fullmatch(args.service_user):
        raise ValueError("Invalid service account name.")
    home = Path(args.service_home)
    if not home.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(home)):
        raise ValueError("The service home must be an absolute path without shell metacharacters.")
    existing = existing_configuration(args)
    validate_existing_options(args, existing)
    validate_live_secret(args, supplied)
    missing = [
        name
        for name in selected_components(args.component)
        if not (args.root / name / ".env").exists()
    ]
    if not missing:
        print("Existing configuration preserved.")
        return
    if any(":" in getattr(args, name + "_host") for name in missing):
        raise ValueError(
            "New native services require a DNS name or IPv4 address; "
            "remote Manager URLs may use IPv6."
        )
    target_url, local_url = connection_url(args, existing, missing)
    runner_token = connection_token(args, existing, missing, supplied, target_url, local_url)
    cache_dir = home / ".cache/esup-runner"
    common = {
        "SERVICE_USER": args.service_user,
        "CACHE_DIR": str(cache_dir),
        "UV_CACHE_DIR": str(cache_dir / "uv"),
    }
    overrides = {}
    if "manager" in missing:
        overrides["manager"] = common | manager_values(args, supplied, runner_token)
    if "runner" in missing:
        overrides["runner"] = common | runner_values(args, target_url, runner_token)

    # Resolve all inputs before writing either role, so input errors leave no files.
    rendered = {
        name: render_env((args.root / name / ".env.example").read_text(), values)
        for name, values in overrides.items()
    }
    for component, content in rendered.items():
        create_env(args.root / component / ".env", content)
        print(f"Created {component}/.env (private permissions).")


def inspect_manager(env: dict[str, str]) -> dict[str, Any]:
    """Describe Manager addresses without exposing credential-bearing URL parts."""
    private = manager_url(env)
    public_url = env.get("MANAGER_PUBLIC_URL") or private
    public = urlsplit(public_url)
    if (
        public.scheme not in {"http", "https"}
        or not public.netloc
        or public.username is not None
        or public.password is not None
        or public.query
        or public.fragment
        or any(ord(char) < 33 for char in public_url)
    ):
        raise ValueError("Manager public URL is not a valid administration address.")
    validate_host(public.hostname or "")
    return {
        "url": private,
        "admin_url": public_url.rstrip("/") + "/admin",
        "live": env.get("LIVE_ENABLED", "false").lower() == "true",
    }


def runner_urls(env: dict[str, str]) -> list[str]:
    """Compute each configured Runner instance URL from the grouped task syntax."""
    tasks = env.get("RUNNER_TASK_TYPES", "encoding,studio")
    groups = re.findall(r"(\d+)\s*x\s*\(", tasks)
    try:
        count = sum(map(int, groups)) if groups else int(env.get("RUNNER_INSTANCES", "1"))
        port = int(env.get("RUNNER_BASE_PORT", "8082"))
    except ValueError:
        raise ValueError("Runner instance count and base port must be integers.") from None
    if not 1 <= count <= 1024 or not 1 <= port <= 65535 - count + 1:
        raise ValueError("Runner instance count or ports are outside the supported range.")
    host = host_for_url(validate_host(env.get("RUNNER_HOST", "127.0.0.1")))
    scheme = env.get("RUNNER_PROTOCOL", "http")
    if scheme not in {"http", "https"}:
        raise ValueError("Runner protocol must be http or https.")
    return [f"{scheme}://{host}:{port + index}" for index in range(count)]


def inspect_runner(env: dict[str, str]) -> dict[str, Any]:
    """Describe Runner addresses and dependency profile without its API token."""
    tasks = env.get("RUNNER_TASK_TYPES", "encoding,studio")
    mode = "base"
    if "transcription" in tasks:
        mode = (
            "transcription-gpu"
            if env.get("ENCODING_TYPE", "CPU").upper() == "GPU"
            else "transcription-cpu"
        )
    return {
        "manager_url": validate_manager_url(env.get("MANAGER_URL", "")),
        "urls": runner_urls(env),
        "sync_mode": mode,
        "live": "live" in tasks,
    }


def inspect_configuration(root: Path, component: str) -> dict[str, Any]:
    """Return deployment details suitable for logs, excluding all credentials."""
    result: dict[str, Any] = {}
    for name in selected_components(component):
        env = read_env(root / name / ".env")
        if not env:
            raise ValueError(f"{name}/.env is missing or empty.")
        data = inspect_manager(env) if name == "manager" else inspect_runner(env)
        data["service_user"] = env.get("SERVICE_USER", "esup-runner")
        result[name] = data
    return result


def is_local_manager(root: Path) -> bool:
    """Return whether the Runner targets the Manager installed in this checkout."""
    manager = read_env(root / "manager/.env")
    runner = read_env(root / "runner/.env")
    return bool(
        manager
        and runner
        and validate_manager_url(runner.get("MANAGER_URL", "")) == manager_url(manager)
    )


def live_configured(root: Path, component: str) -> bool:
    """Recognize complete existing Live connections so retries do not rewrite them."""
    env = read_env(root / component / ".env")
    enabled = (
        env.get("LIVE_ENABLED", "false").lower() == "true"
        if component == "manager"
        else "live" in env.get("RUNNER_TASK_TYPES", "")
    )
    keys = ["LIVE_MEDIAMTX_API_PASSWORD"]
    if component == "runner":
        keys.extend(["LIVE_UPSTREAM_RTMP_URL", "LIVE_LOCAL_RTMP_URL"])
    try:
        for key in keys:
            validate_secret(env.get(key, ""), "Live connection setting")
    except ValueError:
        return False
    return enabled


def check_ports(root: Path, component: str) -> None:
    """Ensure each stopped application's configured listening port can bind locally."""
    for name in selected_components(component):
        env = read_env(root / name / ".env")
        if name == "manager":
            host = env.get("MANAGER_BIND_HOST", env.get("MANAGER_HOST", "127.0.0.1"))
            try:
                port = int(env.get("MANAGER_PORT", "8081"))
            except ValueError:
                raise ValueError("Manager port must be an integer.") from None
            addresses = [(host, port)]
        else:
            addresses = []
            for url in runner_urls(env):
                parsed = urlsplit(url)
                addresses.append((parsed.hostname or "", parsed.port or 0))
        for host, port in addresses:
            try:
                family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][0]
                with socket.create_server((host, port), family=family):
                    pass
            except (OSError, ValueError, OverflowError, IndexError):
                raise ValueError(
                    f"{name.title()} cannot bind its configured local address/port; "
                    "check local DNS, interface addresses and occupied ports."
                ) from None


class NoRedirect(HTTPRedirectHandler):
    """Keep API credentials on the configured origin by refusing redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject every redirect rather than forwarding authentication headers."""
        return None


def request_json(url: str, token: str = "", timeout: float = 5.0) -> dict[str, Any]:
    """Read a bounded API response without redirects or response-body diagnostics."""
    headers = {"Accept": "application/json"}
    if token:
        headers["X-API-Token"] = token
    with build_opener(NoRedirect()).open(
        Request(url, headers=headers), timeout=timeout
    ) as response:
        result = json.loads(response.read(1024 * 1024))
    if not isinstance(result, dict):
        raise ValueError("The service returned an unexpected response.")
    return result


def request_before(deadline: float, url: str, token: str = "") -> dict[str, Any]:
    """Limit each request to the remaining installation health-check deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("Service health-check deadline expired.")
    return request_json(url, token, min(5.0, remaining))


def verify_online_runners(target: str, token: str, urls: list[str], deadline: float) -> None:
    """Match local Runner health identifiers to online Manager registry entries."""
    listed = request_before(deadline, target + "/api/runners", token)
    if not isinstance(listed.get("runners"), list):
        raise ValueError("The Manager returned an unexpected Runner list.")
    online = {
        item.get("id"): item.get("url", "").rstrip("/")
        for item in listed["runners"]
        if isinstance(item, dict) and item.get("status") == "online"
    }
    for url in urls:
        state = request_before(deadline, url + "/runner/health")
        if (
            state.get("status") != "healthy"
            or state.get("registered") is not True
            or not state.get("runner_id")
            or online.get(state.get("runner_id")) != url
        ):
            raise ValueError("Runner health or registration is not ready.")


def check_runtime(args: argparse.Namespace) -> None:
    """Wait for service health and actual Runner registration, using private tokens."""
    configuration = inspect_configuration(args.root, args.component)
    manager = read_env(args.root / "manager" / ".env") if "manager" in configuration else {}
    runner = read_env(args.root / "runner" / ".env") if "runner" in configuration else {}
    target = configuration.get("runner", {}).get("manager_url") or manager_url(manager)
    token = runner.get("RUNNER_TOKEN", "") or authorized_token(manager)
    urls = configuration.get("runner", {}).get("urls", [])
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            health = request_before(deadline, target + "/api/health", token)
            if health.get("status") != "healthy":
                raise ValueError("Manager health is not ready.")
            if urls:
                verify_online_runners(target, token, urls, deadline)
            print(
                "Health checks passed" + ("; all Runner instances are registered." if urls else ".")
            )
            return
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError, AttributeError):
            if time.monotonic() >= deadline:
                raise ValueError(
                    "Service health or Runner registration did not become ready; "
                    "check systemd logs, reachable hostnames, tokens and version compatibility."
                ) from None
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse configuration, inspection and runtime requests from the shell wrapper."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "configure",
        "inspect",
        "check",
        "summary",
        "sync-mode",
        "manager-host",
        "local-manager",
        "live-enabled",
        "live-configured",
        "check-ports",
    ):
        subparser = commands.add_parser(command)
        subparser.add_argument("--root", required=True, type=Path)
        subparser.add_argument(
            "--component",
            required=command
            in {
                "configure",
                "inspect",
                "check",
                "summary",
                "live-enabled",
                "live-configured",
                "check-ports",
            },
            default="runner",
            choices=(
                ("manager", "runner")
                if command in {"live-enabled", "live-configured"}
                else ("manager", "runner", "both")
            ),
        )
        if command == "configure":
            subparser.add_argument("--service-user", default="esup-runner")
            subparser.add_argument("--service-home", default="/home/esup-runner")
            subparser.add_argument("--manager-host", default="127.0.0.1")
            subparser.add_argument("--runner-host", default="127.0.0.1")
            subparser.add_argument("--manager-url", default="")
            subparser.add_argument("--with-transcription", action="store_true")
            subparser.add_argument("--with-live", action="store_true")
            subparser.add_argument("--non-interactive", action="store_true")
            subparser.add_argument("--secrets-stdin", action="store_true")
        elif command == "check":
            subparser.add_argument("--timeout", type=float, default=60)
    live = commands.add_parser("live-password")
    live.add_argument("--secrets-stdin", action="store_true", required=True)
    live.add_argument(
        "--output-fd", type=int, required=True, help="Dedicated pipe descriptor (at least 3)"
    )
    return parser.parse_args(argv)


def input_secrets() -> dict[str, str]:
    """Read installer-only credentials from a private stdin stream without interpolation."""
    return {
        key: value or ""
        for key, value in dotenv_values(stream=sys.stdin, interpolate=False).items()
    }


def send_live_password(output_fd: int) -> None:
    """Send only the validated relay password to a dedicated pipe, never standard output.

    Reject standard streams and non-pipe descriptors; leave the caller's descriptor open.
    """
    if output_fd < 3 or not stat.S_ISFIFO(os.fstat(output_fd).st_mode):
        raise ValueError("The Live relay password requires a dedicated pipe descriptor.")
    password = validate_secret(
        input_secrets().get("INSTALL_LIVE_RELAY_PASSWORD", ""),
        "Manager Live relay password",
    )
    with os.fdopen(output_fd, "w", encoding="utf-8", closefd=False) as pipe:
        pipe.write(password + "\n")


def print_summary(root: Path, component: str) -> None:
    """Display useful addresses and a status command while keeping credentials private."""
    data = inspect_configuration(root, component)
    if "manager" in data:
        print("Administration: " + data["manager"]["admin_url"])
    for url in data.get("runner", {}).get("urls", []):
        print("Runner: " + url)
    print("Private credentials: selected component .env files under " + str(root))
    selected = selected_components(component)
    account = data[selected[0]]["service_user"]
    units = " ".join(f"esup-runner-{name}.service" for name in selected)
    print(f"Log in as {account}, then: systemctl --user status {units}")


def run_command(args: argparse.Namespace) -> None:
    """Dispatch a parsed operation using the same helper contract as install-stack.sh."""
    if args.command == "configure":
        configure(args, input_secrets() if args.secrets_stdin else {})
    elif args.command == "check":
        if args.timeout <= 0:
            raise ValueError("The health-check timeout must be positive.")
        check_runtime(args)
    elif args.command == "live-password":
        send_live_password(args.output_fd)
    else:
        read_command(args)


def read_command(args: argparse.Namespace) -> None:
    """Handle read-only deployment details and the pre-start port probe."""
    if args.command == "sync-mode":
        print(inspect_configuration(args.root, "runner")["runner"]["sync_mode"])
    elif args.command == "manager-host":
        env = read_env(args.root / args.component / ".env")
        value = manager_url(env) if args.component == "manager" else env.get("MANAGER_URL", "")
        print(urlsplit(validate_manager_url(value)).hostname)
    elif args.command == "local-manager":
        print(str(is_local_manager(args.root)).lower())
    elif args.command == "live-enabled":
        print(str(inspect_configuration(args.root, args.component)[args.component]["live"]).lower())
    elif args.command == "live-configured":
        print(str(live_configured(args.root, args.component)).lower())
    elif args.command == "check-ports":
        check_ports(args.root, args.component)
    elif args.command == "inspect":
        print(json.dumps(inspect_configuration(args.root, args.component)))
    else:
        print_summary(args.root, args.component)


def main(argv: list[str] | None = None) -> int:
    """Report CLI failures without logging secrets or unsafe filesystem error details."""
    args = parse_args(argv)
    try:
        run_command(args)
    except (ValueError, OSError) as error:
        # OSError paths may contain credential-bearing input; keep diagnostics generic.
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Cannot read or create installation files."
        )
        print("ERROR: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
