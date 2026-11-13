#!/usr/bin/env python3
"""Install or reuse MediaMTX >= 1.21.1 and prepare this Runner Live instance.

Run from runner/ as the service account, without sudo; defaults use its home directory.
Uses only the Python standard library. Run with --help for installation,
configuration-only (including Docker), combined setup, service startup and explicit updates.
Only update replaces an existing executable; configurations and units are preserved.
"""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core._check_output import Severity, format_status

VERSION = "1.21.1"  # Minimum supported release and default download.
MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
ADDRESS_KEYS = {
    "rtmp_address": ("rtmpAddress", "MTX_RTMPADDRESS"),
    "api_address": ("apiAddress", "MTX_APIADDRESS"),
    "metrics_address": ("metricsAddress", "MTX_METRICSADDRESS"),
}


def print_status(message: str, *, level: Severity = "info") -> None:
    """Use the shared CLI status style, keeping errors on standard error."""
    print(format_status(message, level=level), file=sys.stderr if level == "error" else sys.stdout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the requested operation and reject unsafe names, versions and paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(role="runner")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("install", "Install or reuse a supported native executable"),
        ("configure", "Prepare one Runner instance; also suitable for Docker"),
        ("setup", "Install/reuse the executable, then prepare an instance"),
    ):
        child = commands.add_parser(command, help=help_text)
        child.add_argument(
            "--dry-run", action="store_true", help="Preview without downloads or writes"
        )
        if command != "configure":
            child.add_argument(
                "--version",
                default=VERSION,
                help=f"Release to download if absent (minimum/default: {VERSION}); reuse supported installs",
            )
            child.add_argument(
                "--binary",
                type=Path,
                help="Absolute executable path; otherwise install/link ~/.local/bin/mediamtx, reusing PATH",
            )
        if command != "install":
            child.add_argument("--instance", required=True, help="Instance name, e.g. runner")
            child.add_argument(
                "--config-root",
                type=Path,
                default=Path.home() / ".config/esup-runner",
                help="Configuration parent (default: ~/.config/esup-runner)",
            )
            child.add_argument(
                "--user",
                default=pwd.getpwuid(os.geteuid()).pw_name,
                help="File owner (default: current account; changing owner requires privileges)",
            )
            child.add_argument(
                "--log-dir",
                type=Path,
                default=Path(os.environ.get("LOG_DIR", "/var/log/esup-runner")),
                help="Log directory (default: exported LOG_DIR or /var/log/esup-runner); must be writable at runtime",
            )
            for option in ADDRESS_KEYS:
                child.add_argument(
                    "--" + option.replace("_", "-"),
                    help="IP:port (IPv6: [IP]:port); defaults to this component's YAML template",
                )
    start = commands.add_parser("start", help="Validate and start a prepared default user service")
    start.add_argument("--instance", required=True, help="Prepared instance name")
    start.add_argument("--dry-run", action="store_true", help="Preview without downloads or writes")
    start.set_defaults(binary=None, version=VERSION)
    update = commands.add_parser(
        "update", help="Update existing native services, preserving configuration"
    )
    update.add_argument("--version", required=True, help="Exact stable release to install")
    update.add_argument(
        "--dry-run", action="store_true", help="Preview without downloads or writes"
    )
    args = parser.parse_args(argv)
    if args.command != "configure" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("--version must be a fixed release such as 1.21.1")
    if args.command != "configure" and tuple(map(int, args.version.split("."))) < tuple(
        map(int, VERSION.split("."))
    ):
        parser.error(f"--version must be at least {VERSION}")
    if args.command not in ("install", "update") and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.instance
    ):
        parser.error("--instance must contain 1-64 letters, digits, underscores or hyphens")
    for option in ("binary", "config_root", "log_dir"):
        path = getattr(args, option, None)
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            parser.error(f"--{option.replace('_', '-')} must be absolute without '..'")
    return args


def service_environment(directory: Path) -> dict[str, str]:
    """Read the generated listener overrides without shell evaluation or secret output.

    Custom EnvironmentFile syntax or other overrides require manual service
    management. Discard caller MTX_* overrides to validate the prepared instance.
    """
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MTX_")}
    for line in (directory / "mediamtx.env").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {entry[1] for entry in ADDRESS_KEYS.values()}:
            raise ValueError("Custom mediamtx.env overrides require manual service management")
        try:
            validate_address(value)
        except ValueError as exc:
            raise ValueError("Invalid listener override in mediamtx.env") from exc
        environment[key] = value
    return environment


def validate_service_configuration(directory: Path, environment: dict[str, str]) -> None:
    """Validate YAML with the service's overrides, hiding potentially secret parser output."""
    binary = Path.home() / ".local/bin/mediamtx"
    try:
        subprocess.run(
            [str(binary), f"--validate-conf={directory / 'mediamtx.yml'}"],
            env=environment,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.SubprocessError as exc:
        raise ValueError(
            "MediaMTX validation failed; review the YAML and listener overrides"
        ) from exc


def start_service(args: argparse.Namespace) -> None:
    """Validate a standard home-local instance and enable its systemd user service.

    Reuse an identical unit, refuse customized or symlinked destinations, and
    never restart an active service. Validation uses the service's listener
    overrides; subprocess output is captured to keep credentials private.
    """
    directory = Path.home() / ".config/esup-runner" / args.instance
    reject_symlinks(directory)
    if not existing_configuration(directory):
        raise ValueError("Prepare this instance with setup or configure before starting it")
    environment = service_environment(directory)
    template = ROOT / "production/esup-runner-mediamtx@.service"
    content = template.read_text().rstrip() + "\n"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not config_home.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    unit = config_home / "systemd/user/esup-runner-mediamtx@.service"
    reject_symlinks(unit)
    if unit.exists() and unit.read_text().rstrip() + "\n" != content:
        raise ValueError(f"Customized unit retained: {unit}; manage this service manually")
    install_binary(args)
    if args.dry_run:
        print_status(f"Would validate {directory / 'mediamtx.yml'} with its listener overrides")
        print_status(
            f"Would install or reuse {unit}, reload systemd and enable/start {args.instance}"
        )
        return
    validate_service_configuration(directory, environment)
    try:
        subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.SubprocessError as exc:
        raise ValueError(
            "A working systemd user session is required; log in as the service account"
        ) from exc
    unit.parent.mkdir(parents=True, exist_ok=True)
    if not unit.exists():
        with unit.open("x") as output:
            output.write(content)
            os.fchmod(output.fileno(), 0o644)
    service = f"esup-runner-mediamtx@{args.instance}.service"
    for command in (
        ["daemon-reload"],
        ["enable", "--now", service],
        ["is-active", "--quiet", service],
    ):
        subprocess.run(
            ["systemctl", "--user", *command], check=True, capture_output=True, timeout=30
        )
    print_status(f"Service active: {service}. An already active service was not restarted.")


def reject_symlinks(path: Path) -> None:
    """Refuse symlinks in configuration/install destinations and their parents."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Refusing a symlink in the destination: {candidate}")


def linux_architecture() -> str:
    """Map the running machine to an upstream Linux release archive architecture."""
    architectures = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
    }
    machine = platform.machine()
    if machine not in architectures:
        raise ValueError(f"Unsupported Linux architecture: {machine}")
    return architectures[machine]


def read_version(binary: Path) -> str:
    """Read a stable executable version, rejecting unrecognized or prerelease output."""
    result = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True, text=True, timeout=15
    )
    reported = result.stdout.strip()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", reported):
        raise ValueError(
            f"{binary} does not report a stable MediaMTX version; it has been left unchanged."
        )
    return reported[1:]


def check_version(binary: Path, version: str) -> None:
    """Require the downloaded executable's exact requested version."""
    if read_version(binary) != version:
        raise ValueError(
            f"{binary} does not report v{version}; it has been left unchanged. "
            "Review the existing installation or select another --binary path."
        )


def binary_path(args: argparse.Namespace) -> Path:
    """Prefer the account's installation, then PATH, with a home-local fallback."""
    if args.binary is not None:
        return Path(args.binary)
    local = Path.home() / ".local/bin/mediamtx"
    if local.exists() or local.is_symlink():
        return local
    existing = shutil.which("mediamtx")
    return Path(existing).absolute() if existing else local


def download(url: str, destination: Path, limit: int) -> None:
    """Stream an official HTTPS release asset to a bounded temporary file."""
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("xb") as output:
        if not response.geturl().startswith("https://"):
            raise ValueError("Release asset redirected outside HTTPS")
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"Release asset exceeds the {limit}-byte limit")
            output.write(chunk)


def verify_checksum(archive: Path, checksums: Path) -> None:
    """Verify the selected archive against exactly one upstream SHA-256 entry."""
    matches = []
    for line in checksums.read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].removeprefix("*") == archive.name:
            matches.append(fields[0])
    if len(matches) != 1 or not re.fullmatch(r"[a-fA-F0-9]{64}", matches[0]):
        raise ValueError(f"Missing, duplicate or invalid checksum for {archive.name}")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != matches[0].lower():
        raise ValueError(f"SHA-256 mismatch for {archive.name}; nothing installed")


def unpack_binary(archive: Path, destination: Path) -> None:
    """Copy only a regular mediamtx archive member, rejecting links and oversized files."""
    with tarfile.open(archive, "r:gz") as source:
        members = [member for member in source.getmembers() if member.name == "mediamtx"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("Archive must contain exactly one regular mediamtx executable")
        if not 0 < members[0].size <= MAX_ARCHIVE_SIZE:
            raise ValueError("Invalid executable size in release archive")
        stream = source.extractfile(members[0])
        if stream is None:
            raise ValueError("Cannot read the mediamtx executable from the archive")
        with stream, destination.open("xb") as output:
            shutil.copyfileobj(stream, output)
    destination.chmod(0o700)


def install_binary(args: argparse.Namespace) -> Path:
    """Reuse or install a verified binary, linking PATH installs for the default user unit."""
    target = binary_path(args)
    if target.exists() or target.is_symlink():
        installed = read_version(target)
        if tuple(map(int, installed.split("."))) < tuple(map(int, VERSION.split("."))):
            raise ValueError(
                f"{target} reports v{installed}; MediaMTX >= {VERSION} is required. "
                "It has been left unchanged; upgrade it manually or select another --binary path."
            )
        print_status(f"Reusing MediaMTX {installed}: {target}")
        local = Path.home() / ".local/bin/mediamtx"
        if args.binary is None and target != local:
            reject_symlinks(local.parent)
            if args.dry_run:
                print_status(f"Would link the user service executable: {local} -> {target}")
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.symlink_to(target)
                print_status(f"Linked the user service executable: {local} -> {target}")
        return target
    architecture = linux_architecture()
    reject_symlinks(target.parent)
    if args.dry_run:
        print_status(
            f"Would download and verify MediaMTX {args.version} (linux/{architecture}): {target}"
        )
        return target
    archive_name = f"mediamtx_v{args.version}_linux_{architecture}.tar.gz"
    release = f"https://github.com/bluenviron/mediamtx/releases/download/v{args.version}"
    with tempfile.TemporaryDirectory(prefix="esup-mediamtx-download-") as temporary:
        directory = Path(temporary)
        archive = directory / archive_name
        checksums = directory / "checksums.sha256"
        print_status(f"Downloading MediaMTX {args.version} (linux/{architecture})")
        download(f"{release}/{archive_name}", archive, MAX_ARCHIVE_SIZE)
        download(f"{release}/checksums.sha256", checksums, 1024 * 1024)
        verify_checksum(archive, checksums)
        executable = directory / "mediamtx"
        unpack_binary(archive, executable)
        check_version(executable, args.version)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mediamtx-install-", dir=target.parent) as staging:
            staged = Path(staging) / "mediamtx"
            shutil.copyfile(executable, staged)
            staged.chmod(0o755)
            os.link(staged, target)
    print_status(f"Installed MediaMTX {args.version}: {target}")
    return target


def validate_address(value: str) -> None:
    """Require a literal IPv4/IPv6 listener address and a valid TCP port."""
    host, separator, port = value.rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"Invalid listener address: {value!r}; expected IP:port")
    ip = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    if ip.version == 6 and not (host.startswith("[") and host.endswith("]")):
        raise ValueError("IPv6 addresses must use [IP]:port notation")


def configuration_plan(args: argparse.Namespace) -> tuple[str, str, pwd.struct_passwd]:
    """Read the role template and validate account, paths, logging and listener overrides."""
    try:
        account = pwd.getpwnam(args.user)
    except KeyError as exc:
        raise ValueError(
            f"Create the service account {args.user!r} before configuring MediaMTX"
        ) from exc
    if os.geteuid() not in (0, account.pw_uid):
        raise ValueError(
            "Run the installer as the selected service account; ownership was not changed"
        )
    template = ROOT / "production" / "mediamtx-live.yml"
    if not template.is_file():
        raise ValueError(
            f"Missing {template}; include this component's production directory in the checkout"
        )
    content = template.read_text()
    log_file = args.log_dir / f"mediamtx_{args.instance}.log"
    if any(character in str(log_file) for character in "\x00\r\n"):
        raise ValueError("The MediaMTX log path must not contain control characters")
    content = re.sub(
        r"^logFile:.*$",
        lambda _: "logFile: " + json.dumps(str(log_file)),
        content,
        flags=re.MULTILINE,
    )
    env_lines = []
    ports = []
    for option, (yaml_key, env_key) in ADDRESS_KEYS.items():
        match = re.search(rf"^{yaml_key}: (.+)$", content, re.MULTILINE)
        if match is None:
            raise ValueError(f"Missing {yaml_key} in {template}")
        value = getattr(args, option) or match[1]
        validate_address(value)
        ports.append(value.rsplit(":", 1)[1])
        env_lines.append(f"{env_key}={value}\n")
    if len(set(ports)) != len(ports):
        raise ValueError("RTMP, API and metrics must use distinct ports")
    reject_symlinks(args.config_root / args.instance)
    return content, "".join(env_lines), account


def existing_configuration(directory: Path) -> bool:
    """Recognize a complete existing instance; refuse partial or symlinked configurations."""
    if not directory.exists():
        return False
    for name in ("mediamtx.yml", "mediamtx.env"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Incomplete or unsafe configuration at {directory}; left unchanged")
    return True


def write_private_file(path: Path, content: str, mode: int, uid: int, gid: int) -> None:
    """Create one private configuration file exclusively and assign its service ownership."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        current = os.fstat(output.fileno())
        if (current.st_uid, current.st_gid) != (uid, gid):
            os.fchown(output.fileno(), uid, gid)
        output.write(content)
        os.fchmod(output.fileno(), mode)


def prepare_config_root(path: Path, gid: int) -> None:
    """Create a traversable configuration root, preserving an existing directory's permissions."""
    try:
        path.mkdir(parents=True, mode=0o750)
    except FileExistsError:
        if not path.is_dir():
            raise ValueError(f"Configuration root is not a directory: {path}")
    else:
        if path.stat().st_gid != gid:
            os.chown(path, -1, gid)
        path.chmod(0o750)


def configure_instance(args: argparse.Namespace) -> None:
    """Create role-specific YAML and listener settings without changing existing secrets."""
    content, environment, account = configuration_plan(args)
    directory = args.config_root / args.instance
    if existing_configuration(directory):
        print_status(f"Existing configuration retained: {directory}")
        print_status(
            "Requested role, account and addresses were not reapplied; review these files to change them."
        )
        return
    if args.dry_run:
        print_status(
            f"Would create {args.role} configuration: {directory} (service account: {args.user})"
        )
        print(environment, end="")
        print_status(f"MediaMTX log: {args.log_dir / f'mediamtx_{args.instance}.log'}")
        print_status(
            "Would generate separate random passwords; none are generated during a dry run."
        )
        return
    placeholders = set(re.findall(r"CHANGE_ME_[A-Z_]+", content))
    if not placeholders:
        raise ValueError("Template has no password placeholders; refusing to copy existing secrets")
    for placeholder in placeholders:
        content = content.replace(placeholder, secrets.token_hex(24))
    prepare_config_root(args.config_root, account.pw_gid)
    with tempfile.TemporaryDirectory(prefix=".mediamtx-config-", dir=args.config_root) as temporary:
        staging = Path(temporary)
        write_private_file(staging / "mediamtx.yml", content, 0o600, account.pw_uid, account.pw_gid)
        write_private_file(
            staging / "mediamtx.env", environment, 0o640, os.geteuid(), account.pw_gid
        )
        if staging.stat().st_gid != account.pw_gid:
            os.chown(staging, -1, account.pw_gid)
        staging.chmod(0o750)
        # mkdir reserves the final name without replacing another instance.
        directory.mkdir(mode=0o750)
        try:
            os.replace(staging, directory)
        except OSError:
            directory.rmdir()
            raise
    print_status(f"Created {args.role} configuration: {directory}")
    print_status(f"MediaMTX log: {args.log_dir / f'mediamtx_{args.instance}.log'}")
    print(environment, end="")
    print_status(
        "Passwords are stored in mediamtx.yml. Connect the application without copying them manually:"
    )
    print(
        f"uv run scripts/configure_live.py --instance {args.instance} "
        f"--config-root {shlex.quote(str(args.config_root))}"
    )


def update_system_unit(instance: str, user: str, properties: dict[str, str]) -> Path:
    """Require the Manager installer to handle an account's privileged system units."""
    raise ValueError("System MediaMTX service detected; run update from manager/ instead")


class UpdateService(NamedTuple):
    """Describe one existing native service using the account-local executable."""

    command: list[str]
    name: str
    directory: Path
    unit: Path
    active: bool


def update_properties(command: list[str], service: str) -> dict[str, str]:
    """Read effective service state without exposing its environment or credentials."""
    result = subprocess.run(
        [
            *command,
            "show",
            service,
            "--property=LoadState,ActiveState,FragmentPath,DropInPaths,NeedDaemonReload,User,MainPID",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and properties.get("LoadState") != "not-found":
        raise ValueError(f"Cannot inspect MediaMTX service: {service}")
    return properties


def update_user_unit(properties: dict[str, str], service: str) -> Path:
    """Require the loaded user unit to match this component's standard template."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    unit = Path(properties.get("FragmentPath", ""))
    expected = (ROOT / "production/esup-runner-mediamtx@.service").read_text().strip()
    if not config_home.is_absolute() or unit not in (
        config_home / "systemd/user/esup-runner-mediamtx@.service",
        config_home / "systemd/user" / service,
    ):
        raise ValueError(f"Custom MediaMTX unit requires a manual update: {service}")
    reject_symlinks(unit)
    if unit.read_text().strip() != expected:
        raise ValueError(f"Custom MediaMTX unit requires a manual update: {service}")
    return unit


def update_service(
    command: list[str],
    name: str,
    account: pwd.struct_passwd,
) -> UpdateService | None:
    """Inspect one loaded or configured instance without enabling or starting it."""
    properties = update_properties(command, name)
    if properties.get("LoadState") == "not-found":
        return None
    if command == ["systemctl"] and properties.get("User") not in (
        account.pw_name,
        str(account.pw_uid),
    ):
        return None
    match = re.fullmatch(r"esup-runner-mediamtx@([A-Za-z0-9][A-Za-z0-9_-]{0,63})\.service", name)
    if not match or properties.get("LoadState") != "loaded":
        raise ValueError("Unsupported MediaMTX service; use a manual update")
    if properties.get("DropInPaths") or properties.get("NeedDaemonReload") != "no":
        raise ValueError(f"Custom or stale MediaMTX unit requires a manual update: {name}")
    state = properties.get("ActiveState")
    if state not in ("active", "inactive", "failed"):
        raise ValueError(f"MediaMTX service is changing state; retry later: {name}")
    instance = match[1]
    directory = Path.home() / ".config/esup-runner" / instance
    reject_symlinks(directory)
    if not existing_configuration(directory):
        raise ValueError(f"Incomplete MediaMTX configuration: {instance}")
    if "--user" in command:
        unit = update_user_unit(properties, name)
    else:
        unit = update_system_unit(instance, account.pw_name, properties)
    return UpdateService(command, name, directory, unit, state == "active")


def update_services() -> list[UpdateService]:
    """Discover both scopes, including configured instances not currently loaded."""
    account = pwd.getpwuid(os.geteuid())
    configured = {
        f"esup-runner-mediamtx@{path.parent.name}.service"
        for path in (Path.home() / ".config/esup-runner").glob("*/mediamtx.yml")
    }
    services = []
    for scope in ("user", "system"):
        command = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        result = subprocess.run(
            [*command, "list-units", "--all", "--output=json", "esup-runner-mediamtx@*.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        names = {entry["unit"] for entry in json.loads(result.stdout)} | configured
        for name in sorted(names):
            service = update_service(command, name, account)
            if service is not None:
                services.append(service)
    if not services:
        raise ValueError("No existing native MediaMTX services found; use install/setup first")
    return sorted(services, key=lambda service: (service.command, service.name))


def update_control(service: UpdateService, action: str) -> None:
    """Control only the discovered user unit without changing its enablement."""
    scope = "user" if "--user" in service.command else "system"
    print_status(f"Service action: {action} {service.name} ({scope}).")
    subprocess.run(
        [*service.command, action, service.name],
        check=True,
        capture_output=True,
        timeout=30,
    )


def update_process_matches(service: UpdateService, pid: str, binary: Path) -> bool:
    """Check the process inode when Linux permits access to its executable."""
    try:
        return os.path.samefile(Path("/proc") / pid / "exe", binary)
    except FileNotFoundError:
        return False
    except PermissionError:
        # CAP_NET_BIND_SERVICE can restrict /proc access even for the same UID.
        # System units use the validated ExecStart after an explicit stop/start.
        return service.command == ["systemctl"]


def update_check_running(service: UpdateService, binary: Path) -> None:
    """Require a stable active process, checking its executable where accessible."""
    previous_pid = None
    for _ in range(20):
        properties = update_properties(service.command, service.name)
        pid = properties.get("MainPID", "")
        ready = (
            properties.get("ActiveState") == "active"
            and pid.isdecimal()
            and int(pid) > 0
            and update_process_matches(service, pid, binary)
        )
        if ready and pid == previous_pid:
            return
        previous_pid = pid if ready else None
        time.sleep(0.5)
    raise ValueError(
        f"MediaMTX service did not start with the installed executable: {service.name}"
    )


def update_backup(binary: Path, services: list[UpdateService]) -> Path:
    """Keep the old binary, configurations and units in a private persistent directory."""
    backup = Path(tempfile.mkdtemp(prefix="mediamtx-backup-", dir=binary.parent))
    print_status(f"Backup directory: {backup}")
    shutil.copy2(binary, backup / "mediamtx", follow_symlinks=False)
    for service in services:
        scope = "user" if "--user" in service.command else "system"
        destination = backup / f"{scope}-{service.name}"
        destination.mkdir(mode=0o700)
        shutil.copy2(service.unit, destination / "unit.service")
        shutil.copytree(service.directory, destination / "configuration", symlinks=True)
    return backup


def update_restore_binary(binary: Path, backup: Path, staging: Path) -> None:
    """Restore the saved executable by an atomic rename on the installation filesystem."""
    try:
        restored = staging / "rollback"
        shutil.copy2(backup / "mediamtx", restored, follow_symlinks=False)
        os.replace(restored, binary)
    except OSError:
        raise ValueError(
            f"Rollback could not restore the executable; restore {backup} manually"
        ) from None


def update_restore(
    binary: Path,
    backup: Path,
    staging: Path,
    services: list[UpdateService],
    replaced: bool,
) -> None:
    """Restore the previous executable and attempt to recover every originally active unit."""
    errors = []
    if replaced:
        for service in services:
            try:
                update_control(service, "stop")
            except (OSError, subprocess.SubprocessError):
                errors.append(service.name)
        update_restore_binary(binary, backup, staging)
    for service in services:
        try:
            update_control(service, "reset-failed")
            update_control(service, "start")
            update_check_running(service, binary)
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append(service.name)
    if errors:
        raise ValueError(
            f"Rollback needs manual service recovery: {', '.join(sorted(set(errors)))}; backup: {backup}"
        )


def update_replace(
    binary: Path,
    candidate: Path,
    backup: Path,
    services: list[UpdateService],
) -> None:
    """Replace atomically, restarting active units and rolling back on a detected failure."""
    active = [service for service in services if service.active]
    replaced = False
    try:
        for service in active:
            update_control(service, "stop")
        os.replace(candidate, binary)
        replaced = True
        for service in active:
            update_control(service, "start")
            update_check_running(service, binary)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
        print_status(
            "MediaMTX activation failed; restoring the previous installation.", level="warning"
        )
        update_restore(binary, backup, candidate.parent, active, replaced)
        raise ValueError(
            f"MediaMTX update failed; previous installation recovered. Backup: {backup}"
        ) from None


def update_validate(candidate: Path, services: list[UpdateService]) -> None:
    """Validate each existing configuration with its listener overrides, hiding parser output."""
    for service in services:
        try:
            subprocess.run(
                [str(candidate), f"--validate-conf={service.directory / 'mediamtx.yml'}"],
                env=service_environment(service.directory),
                cwd=Path.home(),
                check=True,
                capture_output=True,
                timeout=30,
            )
        except subprocess.SubprocessError:
            raise ValueError(f"MediaMTX configuration validation failed: {service.name}") from None
        print_status(f"Configuration validated: {service.name}.")


def update_binary_state(binary: Path) -> tuple[tuple[int, ...], ...]:
    """Track both the account-local entry and its target without considering access times."""
    return tuple(
        (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)
        for metadata in (binary.lstat(), binary.stat())
    )


def print_update_plan(
    args: argparse.Namespace, binary: Path, installed: str, services: list[UpdateService]
) -> None:
    """Summarize versions, paths and affected services using the diagnostic CLI layout."""
    print("\nUpdate plan:")
    print(f"  Installed version : {installed}")
    print(f"  Target version    : {args.version}")
    print(f"  Executable        : {binary}")
    print(f"  Mode              : {'dry run' if args.dry_run else 'update'}")
    if binary.is_symlink():
        print(f"  Symlink target    : {binary.readlink()}")
        print_status("The account-local symlink will be replaced; its target stays unchanged.")
    print("\nMediaMTX services:")
    for service in services:
        scope = "user" if "--user" in service.command else "system"
        action = "restart" if service.active else "keep stopped"
        print(f"  {service.name} ({scope}): {action}")
    print_status("Existing configuration and credentials will be preserved.")
    print_status(
        "Restarting MediaMTX interrupts Live connections and removes temporary relay paths.",
        level="warning",
    )


def update_locked(args: argparse.Namespace, binary: Path) -> None:
    """Prepare and validate the requested release before stopping any existing service."""
    installed = read_version(binary)
    if installed == args.version:
        print("\nConclusion:")
        print_status(f"MediaMTX {installed} is already installed; no service was restarted.")
        return
    if tuple(map(int, args.version.split("."))) < tuple(map(int, installed.split("."))):
        raise ValueError("update does not downgrade MediaMTX; use the manual rollback procedure")
    services = update_services()
    # Validate the environment even for a preview; never evaluate it as shell code.
    for service in services:
        service_environment(service.directory)
    print_update_plan(args, binary, installed, services)
    if args.dry_run:
        print_status(
            "Would download, validate existing configurations, back up and replace the executable."
        )
        print("\nConclusion:")
        print_status("Dry run complete: no files changed or services restarted.")
        return
    print_status("Preparing MediaMTX update.")
    before = update_binary_state(binary)
    with tempfile.TemporaryDirectory(prefix=".mediamtx-update-", dir=binary.parent) as temporary:
        candidate = Path(temporary) / "mediamtx"
        install_binary(argparse.Namespace(binary=candidate, version=args.version, dry_run=False))
        update_validate(candidate, services)
        reject_symlinks(binary.parent)
        if update_binary_state(binary) != before:
            raise ValueError("MediaMTX executable changed during preparation; retry the update")
        if update_services() != services:
            raise ValueError("MediaMTX services changed during preparation; retry the update")
        backup = update_backup(binary, services)
        update_replace(binary, candidate, backup, services)
    print("\nConclusion:")
    print_status(f"MediaMTX updated to {args.version}.")
    print_status("Verify a test broadcast and its current HLS URL.")


def update_binary(args: argparse.Namespace) -> None:
    """Serialize account-local updates, replacing executable symlinks without changing targets."""
    if os.geteuid() == 0:
        raise ValueError("Run update as the non-root service account")
    binary = Path.home() / ".local/bin/mediamtx"
    reject_symlinks(binary.parent)
    if not binary.is_file() or binary.lstat().st_uid != os.geteuid():
        raise ValueError(
            "update requires an account-owned ~/.local/bin/mediamtx file or symlink to a file"
        )
    # A directory lock stays valid across the executable's atomic replacement.
    lock = os.open(binary.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another MediaMTX update is already running for this account"
            ) from None
        update_locked(args, binary)
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    """Execute the CLI action, returning a concise error instead of a traceback on failure."""
    args = parse_args(argv)
    print(f"=== MediaMTX {args.command} ===")
    try:
        if platform.system() != "Linux":
            raise ValueError("This installer supports Linux only")
        if args.command == "start":
            start_service(args)
            return 0
        if args.command == "update":
            update_binary(args)
            return 0
        if args.command != "install":
            configuration_plan(args)
            existing_configuration(args.config_root / args.instance)
        if args.command != "configure":
            install_binary(args)
        if args.command != "install":
            configure_instance(args)
        print_status(
            "No service was started or restarted. Follow MEDIAMTX_SETUP.md for service management."
        )
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        message = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            message = f"Command failed (exit code {exc.returncode}): {shlex.join(exc.cmd)}"
        print_status(message, level="error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
