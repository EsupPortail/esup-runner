#!/usr/bin/env python3
"""Install or reuse MediaMTX >= 1.21.1 and prepare this Runner Live instance.

Run from runner/ as the service account, without sudo; defaults use its home directory.
Uses only the Python standard library. Run with --help for installation,
configuration-only (including Docker), and combined setup commands. Existing
executables and configurations are never overwritten; services are not started.
"""

import argparse
import hashlib
import ipaddress
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.21.1"  # Minimum supported release and default download.
MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
ADDRESS_KEYS = {
    "rtmp_address": ("rtmpAddress", "MTX_RTMPADDRESS"),
    "api_address": ("apiAddress", "MTX_APIADDRESS"),
    "metrics_address": ("metricsAddress", "MTX_METRICSADDRESS"),
}


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
            for option in ADDRESS_KEYS:
                child.add_argument(
                    "--" + option.replace("_", "-"),
                    help="IP:port (IPv6: [IP]:port); defaults to this component's YAML template",
                )
    args = parser.parse_args(argv)
    if args.command != "configure" and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("--version must be a fixed release such as 1.21.1")
    if args.command != "configure" and tuple(map(int, args.version.split("."))) < tuple(
        map(int, VERSION.split("."))
    ):
        parser.error(f"--version must be at least {VERSION}")
    if args.command != "install" and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.instance
    ):
        parser.error("--instance must contain 1-64 letters, digits, underscores or hyphens")
    for option in ("binary", "config_root"):
        path = getattr(args, option, None)
        if path is not None and (not path.is_absolute() or ".." in path.parts):
            parser.error(f"--{option.replace('_', '-')} must be absolute without '..'")
    return args


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
        print(f"Reusing MediaMTX {installed}: {target}")
        local = Path.home() / ".local/bin/mediamtx"
        if args.binary is None and target != local:
            reject_symlinks(local.parent)
            if args.dry_run:
                print(f"Would link the user service executable: {local} -> {target}")
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.symlink_to(target)
                print(f"Linked the user service executable: {local} -> {target}")
        return target
    architecture = linux_architecture()
    reject_symlinks(target.parent)
    if args.dry_run:
        print(f"Would download and verify MediaMTX {args.version} (linux/{architecture}): {target}")
        return target
    archive_name = f"mediamtx_v{args.version}_linux_{architecture}.tar.gz"
    release = f"https://github.com/bluenviron/mediamtx/releases/download/v{args.version}"
    with tempfile.TemporaryDirectory(prefix="esup-mediamtx-download-") as temporary:
        directory = Path(temporary)
        archive = directory / archive_name
        checksums = directory / "checksums.sha256"
        print(f"Downloading MediaMTX {args.version} (linux/{architecture})")
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
    print(f"Installed MediaMTX {args.version}: {target}")
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
    """Read the role template and validate account, destination and listener overrides."""
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
        print(f"Existing configuration retained: {directory}")
        print(
            "Requested role, account and addresses were not reapplied; review these files to change them."
        )
        return
    if args.dry_run:
        print(f"Would create {args.role} configuration: {directory} (service account: {args.user})")
        print(environment, end="")
        print("Would generate separate random passwords; none are generated during a dry run.")
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
    print(f"Created {args.role} configuration: {directory}")
    print(environment, end="")
    print(
        "Passwords are stored in mediamtx.yml. Connect the application without copying them manually:"
    )
    print(
        f"uv run scripts/configure_live.py --instance {args.instance} "
        f"--config-root {shlex.quote(str(args.config_root))}"
    )


def main(argv: list[str] | None = None) -> int:
    """Execute the CLI action, returning a concise error instead of a traceback on failure."""
    args = parse_args(argv)
    try:
        if platform.system() != "Linux":
            raise ValueError("This installer supports Linux only")
        if args.command != "install":
            configuration_plan(args)
            existing_configuration(args.config_root / args.instance)
        if args.command != "configure":
            install_binary(args)
        if args.command != "install":
            configure_instance(args)
        print(
            "No service was started or restarted. Follow MEDIAMTX_SETUP.md for service management."
        )
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
