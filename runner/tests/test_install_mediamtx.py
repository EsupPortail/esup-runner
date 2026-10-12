"""Isolated checks for the Runner MediaMTX installer; no root or network required."""

import hashlib
import importlib.util
import io
import os
import pwd
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_mediamtx.py"
spec = importlib.util.spec_from_file_location("install_mediamtx", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep home-local defaults away from the real service account's files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def config_options(tmp_path):
    return [
        "--instance",
        "runner",
        "--config-root",
        str(tmp_path / "config"),
        "--user",
        pwd.getpwuid(os.getuid()).pw_name,
    ]


def make_archive(path, content=b"#!/bin/sh\nprintf 'v1.21.1\\n'\n", member=None):
    """Build a disposable release archive, optionally with a deliberately unsafe member."""
    entry = member or tarfile.TarInfo("mediamtx")
    entry.size = len(content) if entry.isfile() else 0
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(entry, io.BytesIO(content) if entry.isfile() else None)


@pytest.fixture
def release(monkeypatch, tmp_path):
    archive = tmp_path / "release.tar.gz"
    make_archive(archive)
    content = archive.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    downloads = []

    def download(url, destination, limit):
        downloads.append(url)
        if url.endswith("checksums.sha256"):
            destination.write_text(f"{digest} *mediamtx_v1.21.1_linux_amd64.tar.gz\n")
        else:
            destination.write_bytes(content)

    monkeypatch.setattr(installer, "download", download)
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    return downloads


def test_install_verified_binary_and_reuse(tmp_path, isolated_home, release):
    target = tmp_path / "bin" / "mediamtx"
    options = ["install", "--binary", str(target)]
    assert installer.main(options) == 0
    assert target.stat().st_mode & 0o777 == 0o755
    assert subprocess.check_output([target, "--version"], text=True).strip() == "v1.21.1"
    inode = target.stat().st_ino
    assert installer.main(options) == 0
    assert target.stat().st_ino == inode
    assert len(release) == 2
    assert all("/releases/download/v1.21.1/" in url for url in release)
    assert not (isolated_home / ".local").exists()


@pytest.mark.parametrize("version", ["1.21.1", "1.21.2", "1.21.10", "2.0.0"])
def test_reuse_path_binary_without_download(tmp_path, isolated_home, monkeypatch, capsys, version):
    """A PATH installation becomes usable at the default unit's executable path."""
    target = tmp_path / "existing-mediamtx"
    target.write_text(f"#!/bin/sh\necho v{version}\n")
    target.chmod(0o755)
    original = (target.read_bytes(), target.stat().st_ino)
    monkeypatch.setattr(installer.shutil, "which", lambda _: str(target))
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    assert installer.main(["install"]) == 0
    assert str(target) in capsys.readouterr().out
    local = isolated_home / ".local/bin/mediamtx"
    assert local.is_symlink()
    assert local.readlink() == target
    assert subprocess.check_output([local, "--version"], text=True).strip() == f"v{version}"
    inode = local.lstat().st_ino
    assert installer.main(["install"]) == 0
    assert local.lstat().st_ino == inode
    assert (target.read_bytes(), target.stat().st_ino) == original


@pytest.mark.parametrize("problem", ["dry_run", "version", "symlink_parent", "concurrent"])
def test_reuse_path_link_failures_preserve_files(
    tmp_path, isolated_home, monkeypatch, capsys, problem
):
    """Preview and failed reuse must not replace executables or follow redirected parents."""
    target = tmp_path / "existing-mediamtx"
    version = "1.20.0" if problem == "version" else "1.21.1"
    target.write_text(f"#!/bin/sh\necho v{version}\n")
    target.chmod(0o755)
    original = (target.read_bytes(), target.stat().st_ino)
    monkeypatch.setattr(installer.shutil, "which", lambda _: str(target))
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    local = isolated_home / ".local/bin/mediamtx"
    if problem == "symlink_parent":
        (isolated_home / ".local").symlink_to(tmp_path)
    elif problem == "concurrent":
        original_symlink = installer.Path.symlink_to

        def create_competing_file(path, destination):
            path.write_text("keep concurrent installation")
            original_symlink(path, destination)

        monkeypatch.setattr(installer.Path, "symlink_to", create_competing_file)
    options = ["install", "--dry-run"] if problem == "dry_run" else ["install"]
    assert installer.main(options) == (0 if problem == "dry_run" else 1)
    assert (target.read_bytes(), target.stat().st_ino) == original
    if problem == "concurrent":
        assert local.read_text() == "keep concurrent installation"
        assert not local.is_symlink()
    else:
        assert not local.exists()
        assert not local.is_symlink()
        assert not local.parent.exists()
    if problem == "dry_run":
        assert "Would link" in capsys.readouterr().out


def test_existing_different_version_is_preserved(tmp_path, capsys):
    target = tmp_path / "mediamtx"
    target.write_text("#!/bin/sh\necho v1.20.0\n")
    target.chmod(0o755)
    original = target.read_bytes()
    assert installer.main(["install", "--binary", str(target)]) == 1
    assert target.read_bytes() == original
    assert "left unchanged" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["1.21.1", "1.21.2", "1.21.10", "1.22.0", "2.0.0"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_supported_existing_version_is_reused(
    tmp_path, isolated_home, monkeypatch, capsys, version, dry_run
):
    """Keep any supported stable release, even when another download version is selected."""
    target = tmp_path / "mediamtx"
    target.write_text(f"#!/bin/sh\necho v{version}\n")
    target.chmod(0o755)
    original = (target.read_bytes(), target.stat().st_ino)
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    options = ["install", "--binary", str(target), "--version", "1.21.2"]
    if dry_run:
        options.append("--dry-run")
    assert installer.main(options) == 0
    assert f"Reusing MediaMTX {version}:" in capsys.readouterr().out
    assert (target.read_bytes(), target.stat().st_ino) == original
    assert not (isolated_home / ".local").exists()


@pytest.mark.parametrize("reported", ["v1.21.0", "v1.9.99", "v1.21.2-rc1", "v1.21", "devel", ""])
def test_unsupported_existing_version_is_preserved(tmp_path, monkeypatch, capsys, reported):
    """Reject old or unrecognized releases before any download or replacement."""
    target = tmp_path / "mediamtx"
    target.write_text(f"#!/bin/sh\nprintf '%s\\n' '{reported}'\n")
    target.chmod(0o755)
    original = (target.read_bytes(), target.stat().st_ino)
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    assert installer.main(["install", "--binary", str(target)]) == 1
    assert "left unchanged" in capsys.readouterr().err
    assert (target.read_bytes(), target.stat().st_ino) == original


@pytest.mark.parametrize("command", ["install", "setup"])
@pytest.mark.parametrize("version", ["1.21.0", "1.9.99", "0.99.99"])
def test_requested_version_below_minimum_is_rejected(
    tmp_path, config_options, monkeypatch, capsys, command, version
):
    """Refuse an unsupported requested release before configuration or downloads."""
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    options = [command, "--version", version, "--binary", str(tmp_path / "mediamtx")]
    if command == "setup":
        options.extend(config_options)
    with pytest.raises(SystemExit) as error:
        installer.main(options)
    assert error.value.code == 2
    assert "must be at least 1.21.1" in capsys.readouterr().err
    assert not (tmp_path / "mediamtx").exists()
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize(
    ("requested", "reported"),
    [
        ("1.21.2", "1.21.2"),
        ("1.21.10", "1.21.10"),
        ("1.22.0", "1.22.0"),
        ("2.0.0", "2.0.0"),
        ("1.21.2", "1.21.1"),
        ("1.21.2", "1.21.3"),
        ("1.21.2", "1.21.2-rc1"),
    ],
)
def test_selected_download_requires_exact_version(tmp_path, monkeypatch, requested, reported):
    """Install the chosen release only when the verified archive reports that exact version."""
    archive = tmp_path / "release.tar.gz"
    make_archive(archive, content=f"#!/bin/sh\necho v{reported}\n".encode())
    content = archive.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    archive_name = f"mediamtx_v{requested}_linux_amd64.tar.gz"
    downloads = []

    def download(url, destination, limit):
        downloads.append(url)
        if url.endswith("checksums.sha256"):
            destination.write_text(f"{digest} *{archive_name}\n")
        else:
            destination.write_bytes(content)

    monkeypatch.setattr(installer, "download", download)
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    target = tmp_path / "bin/mediamtx"
    assert installer.main(["install", "--version", requested, "--binary", str(target)]) == (
        0 if requested == reported else 1
    )
    assert len(downloads) == 2
    assert all(f"/releases/download/v{requested}/" in url for url in downloads)
    if requested == reported:
        assert subprocess.check_output([target, "--version"], text=True).strip() == f"v{requested}"
    else:
        assert not target.parent.exists()


def test_setup_dry_run_has_no_downloads_or_writes(tmp_path, config_options, monkeypatch, capsys):
    target = tmp_path / "bin" / "mediamtx"
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Unexpected download"))
    monkeypatch.setattr(
        installer.secrets, "token_hex", lambda *_: pytest.fail("Generated a secret")
    )
    assert installer.main(["setup", *config_options, "--binary", str(target), "--dry-run"]) == 0
    assert not target.parent.exists()
    assert not (tmp_path / "config").exists()
    assert "MTX_RTMPADDRESS=127.0.0.1:2935" in capsys.readouterr().out


def test_configure_generates_private_distinct_secrets_and_preserves_them(
    tmp_path, config_options, monkeypatch, capsys
):
    role = "runner"
    password_count = 2
    original_umask = os.umask(0o077)
    try:
        assert (
            installer.main(["configure", *config_options, "--rtmp-address", "0.0.0.0:21935"]) == 0
        )
    finally:
        os.umask(original_umask)
    directory = tmp_path / "config" / "runner"
    yaml_path, env_path = directory / "mediamtx.yml", directory / "mediamtx.env"
    content = yaml_path.read_text()
    passwords = [line.strip().split()[1] for line in content.splitlines() if "pass:" in line]
    assert len(set(passwords)) == password_count
    assert all(len(password) == 48 for password in passwords)
    output = capsys.readouterr().out
    assert not any(password in output for password in passwords)
    assert "CHANGE_ME_" not in content
    if role == "manager":
        assert "overridePublisher: true" in content
    assert "MTX_RTMPADDRESS=0.0.0.0:21935" in env_path.read_text()
    for path, mode in (
        (yaml_path, 0o600),
        (env_path, 0o640),
        (directory, 0o750),
        (directory.parent, 0o750),
    ):
        assert path.stat().st_mode & 0o777 == mode
    before = {path: (path.read_bytes(), path.stat().st_ino) for path in (yaml_path, env_path)}
    monkeypatch.setattr(installer.secrets, "token_hex", lambda *_: pytest.fail("Rotated a secret"))
    assert installer.main(["configure", *config_options, "--rtmp-address", "0.0.0.0:22935"]) == 0
    assert before == {path: (path.read_bytes(), path.stat().st_ino) for path in before}
    assert "were not reapplied" in capsys.readouterr().out


def test_setup_installs_and_configures(tmp_path, config_options, release):
    assert installer.main(["setup", *config_options, "--binary", str(tmp_path / "mediamtx")]) == 0
    assert (tmp_path / "mediamtx").is_file()
    assert (tmp_path / "config/runner/mediamtx.yml").is_file()


def test_setup_defaults_work_without_ownership_changes(isolated_home, release, monkeypatch):
    """A service user can install/configure in its home without chown or sudo."""
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer.os, "chown", lambda *_: pytest.fail("Unexpected chown"))
    monkeypatch.setattr(installer.os, "fchown", lambda *_: pytest.fail("Unexpected fchown"))
    assert installer.main(["setup", "--instance", "runner"]) == 0
    binary = isolated_home / ".local/bin/mediamtx"
    config = isolated_home / ".config/esup-runner/runner/mediamtx.yml"
    assert binary.is_file()
    assert config.stat().st_uid == os.geteuid()
    assert config.stat().st_mode & 0o777 == 0o600
    # The local binary remains reusable even when ~/.local/bin is absent from PATH.
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/other/mediamtx")
    assert installer.main(["setup", "--instance", "runner"]) == 0
    assert len(release) == 2


def test_other_account_rejected_before_download(config_options, release, monkeypatch):
    """An unprivileged caller cannot accidentally prepare files for another account."""
    account = pwd.getpwuid(os.geteuid())
    other = pwd.struct_passwd(
        ("other", "x", account.pw_uid + 1, account.pw_gid, "", "/other", "/bin/sh")
    )
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda _: other)
    monkeypatch.setattr(installer.os, "geteuid", lambda: account.pw_uid or 1001)
    assert installer.main(["setup", *config_options]) == 1
    assert not release


@pytest.mark.parametrize("kind", ["incomplete", "symlink_file", "symlink_directory"])
def test_unsafe_existing_configuration_is_preserved(tmp_path, config_options, kind):
    directory = tmp_path / "config/runner"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("must survive")
    if kind == "symlink_file":
        (directory / "mediamtx.yml").symlink_to(outside)
        (directory / "mediamtx.env").write_text("existing settings")
    elif kind == "symlink_directory":
        directory.rmdir()
        directory.symlink_to(tmp_path)
    assert installer.main(["configure", *config_options]) == 1
    assert outside.read_text() == "must survive"


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0:0", "localhost:9997", "127.0.0.1:65536", "::1:9997", "127.0.0.1:9997\nINJECT=value"],
)
def test_invalid_addresses_fail_before_install(tmp_path, config_options, address, release):
    assert (
        installer.main(
            [
                "setup",
                *config_options,
                "--api-address",
                address,
                "--binary",
                str(tmp_path / "mediamtx"),
            ]
        )
        == 1
    )
    assert not release
    assert not (tmp_path / "config").exists()


def test_distinct_ports_required(config_options):
    assert installer.main(["configure", *config_options, "--api-address", "127.0.0.1:2935"]) == 1


def test_ipv6_listener(config_options, tmp_path):
    assert installer.main(["configure", *config_options, "--api-address", "[::1]:10997"]) == 0
    assert "MTX_APIADDRESS=[::1]:10997" in (tmp_path / "config/runner/mediamtx.env").read_text()


@pytest.mark.parametrize(
    "options",
    [
        ["install", "--version", "latest"],
        ["install", "--binary", "relative"],
        ["configure", "--instance", "../runner"],
        ["configure", "--instance", "runner", "--config-root", "/tmp/../etc"],
    ],
)
def test_invalid_cli_arguments(options):
    with pytest.raises(SystemExit) as error:
        installer.parse_args(options)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "invalid *release.tar.gz\n",
        "{digest} *release.tar.gz\n{digest} *release.tar.gz\n",
        "{wrong} *release.tar.gz\n",
    ],
)
def test_bad_checksums_rejected(tmp_path, entry):
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"example archive")
    checksums = tmp_path / "checksums.sha256"
    checksums.write_text(
        entry.format(digest=hashlib.sha256(archive.read_bytes()).hexdigest(), wrong="0" * 64)
    )
    with pytest.raises(ValueError):
        installer.verify_checksum(archive, checksums)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "traversal", "empty", "duplicate"])
def test_unsafe_archive_rejected(tmp_path, kind):
    archive = tmp_path / "release.tar.gz"
    member = tarfile.TarInfo("../mediamtx" if kind == "traversal" else "mediamtx")
    if kind in ("symlink", "hardlink"):
        member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
        member.linkname = "/etc/passwd"
    make_archive(archive, content=b"" if kind == "empty" else b"binary", member=member)
    if kind == "duplicate":
        with tarfile.open(archive, "w:gz") as output:
            output.addfile(member, io.BytesIO(b"binary"))
            output.addfile(member, io.BytesIO(b"binary"))
    with pytest.raises(ValueError):
        installer.unpack_binary(archive, tmp_path / "executable")
    assert not (tmp_path / "executable").exists()


def test_corrupt_download_does_not_install(tmp_path, release, monkeypatch):
    def corrupt(*_):
        raise ValueError("SHA-256 mismatch")

    monkeypatch.setattr(installer, "verify_checksum", corrupt)
    target = tmp_path / "bin/mediamtx"
    assert installer.main(["install", "--binary", str(target)]) == 1
    assert not target.exists()


def test_missing_account_fails_before_download(config_options, release, monkeypatch):
    def missing(_):
        raise KeyError("account")

    monkeypatch.setattr(installer.pwd, "getpwnam", missing)
    assert installer.main(["setup", *config_options]) == 1
    assert not release


@pytest.mark.parametrize("system,machine", [("Darwin", "x86_64"), ("Linux", "unsupported")])
def test_unsupported_platform(tmp_path, monkeypatch, system, machine):
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    assert installer.main(["install", "--binary", str(tmp_path / "mediamtx"), "--dry-run"]) == 1


def test_existing_config_root_permissions_preserved(tmp_path, config_options):
    root = tmp_path / "config"
    root.mkdir(mode=0o755)
    assert installer.main(["configure", *config_options]) == 0
    assert root.stat().st_mode & 0o777 == 0o755


def test_download_limit_and_https_redirect(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        def geturl(self):
            return redirected

    redirected = "https://release.example/asset"
    monkeypatch.setattr(
        installer.urllib.request, "urlopen", lambda *_a, **_kw: Response(b"oversized")
    )
    installer.download("https://example.test", tmp_path / "success", 100)
    assert (tmp_path / "success").read_bytes() == b"oversized"
    with pytest.raises(ValueError, match="limit"):
        installer.download("https://example.test", tmp_path / "download", 2)
    redirected = "http://release.example/asset"
    with pytest.raises(ValueError, match="HTTPS"):
        installer.download("https://example.test", tmp_path / "redirect", 100)


def test_failed_configuration_publication_leaves_no_partial_instance(
    tmp_path, config_options, monkeypatch
):
    def fail(*_):
        raise PermissionError("Simulated filesystem failure")

    monkeypatch.setattr(installer.os, "replace", fail)
    assert installer.main(["configure", *config_options]) == 1
    assert list((tmp_path / "config").iterdir()) == []


def test_concurrent_install_preserves_other_binary(tmp_path, release, monkeypatch):
    target = tmp_path / "mediamtx"
    original_link = installer.os.link

    def publish_other_binary(source, destination):
        target.write_bytes(b"existing concurrent installation")
        original_link(source, destination)

    monkeypatch.setattr(installer.os, "link", publish_other_binary)
    assert installer.main(["install", "--binary", str(target)]) == 1
    assert target.read_bytes() == b"existing concurrent installation"


@pytest.mark.parametrize("problem", ["missing", "missing_address", "existing_secrets"])
def test_incomplete_checkout_or_modified_template_is_rejected(
    tmp_path, config_options, monkeypatch, problem
):
    source = installer.ROOT / "production/mediamtx-live.yml"
    content = source.read_text()
    template = tmp_path / "checkout/production/mediamtx-live.yml"
    if problem != "missing":
        template.parent.mkdir(parents=True)
        if problem == "missing_address":
            content = content.replace("apiAddress: 127.0.0.1:10997\n", "")
        else:
            content = content.replace("CHANGE_ME_CONTROL_PASSWORD", "existing-control-secret")
            content = content.replace("CHANGE_ME_READ_PASSWORD", "existing-read-secret")
        template.write_text(content)
    monkeypatch.setattr(installer, "ROOT", tmp_path / "checkout")
    assert installer.main(["configure", *config_options]) == 1
    assert not (tmp_path / "config").exists()


def test_component_role_is_implicit_and_other_role_rejected():
    """The local installer selects its own component without a positional role."""
    assert installer.parse_args(["configure", "--instance", "custom"]).role == "runner"
    with pytest.raises(SystemExit):
        installer.parse_args(["configure", "manager", "--instance", "custom"])
