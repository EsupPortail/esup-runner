"""Actionable HLS storage diagnostics without writes or raw exception disclosure."""

import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import live_storage


@pytest.mark.parametrize("exists", [True, False])
def test_writable_output_or_parent_is_accepted_without_creating_files(tmp_path, exists):
    directory = tmp_path / "hls"
    if exists:
        directory.mkdir()
    live_storage.validate_hls_directory(str(directory / "instance-2"))
    assert not (directory / "instance-2").exists()
    assert directory.exists() is exists


def test_foreign_owned_hls_reports_account_owner_permissions_and_resolution(tmp_path, monkeypatch):
    directory = tmp_path / "hls"
    directory.mkdir(mode=0o755)
    real_stat = Path.stat

    def foreign_stat(path, **kwargs):
        if path == directory:
            info = real_stat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=2000, st_gid=2001)
        return real_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", foreign_stat)
    monkeypatch.setattr(live_storage.os, "geteuid", lambda: 3000)
    monkeypatch.setattr(live_storage.os, "getegid", lambda: 3001)
    monkeypatch.setattr(
        live_storage.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="mediamtx" if uid == 2000 else "esup-runner"),
    )
    monkeypatch.setattr(
        live_storage.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="mediamtx" if gid == 2001 else "esup-runner"),
    )

    def denied(path, mode, *, effective_ids):
        assert path == directory
        assert mode == live_storage.os.W_OK | live_storage.os.X_OK
        assert effective_ids
        return False

    monkeypatch.setattr(live_storage.os, "access", denied)
    with pytest.raises(ValueError) as error:
        live_storage.validate_hls_directory(str(directory))
    message = str(error.value)
    for detail in (
        str(directory),
        "Permission denied (EACCES)",
        "mode=0755",
        "owner=mediamtx:mediamtx (UID=2000, GID=2001)",
        "Runner account=esup-runner:esup-runner (UID=3000, GID=3001)",
        "HLS files are written by Runner/FFmpeg",
        "write/search permissions",
    ):
        assert detail in message


def test_inaccessible_parent_is_identified_for_missing_output(tmp_path, monkeypatch):
    directory = tmp_path / "hls" / "instance-2"
    monkeypatch.setattr(live_storage.os, "access", lambda *args, **kwargs: False)
    with pytest.raises(ValueError) as error:
        live_storage.validate_hls_directory(str(directory))
    assert f"Existing path {str(tmp_path)!r}" in str(error.value)
    assert not directory.exists()


@pytest.mark.parametrize("suffix", ["", "/instance-2"])
def test_file_in_output_path_is_reported(tmp_path, suffix):
    path = tmp_path / "file"
    path.write_text("preserve this file")
    with pytest.raises(ValueError, match=r"Not a directory \(ENOTDIR\)"):
        live_storage.validate_hls_directory(str(path) + suffix)
    assert path.read_text() == "preserve this file"


def test_configured_symlink_root_remains_supported(tmp_path):
    root = tmp_path / "hls"
    root.symlink_to(tmp_path, target_is_directory=True)
    live_storage.validate_hls_directory(str(root))


def test_unreadable_metadata_and_raw_os_errors_remain_safe(tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError(errno.EACCES, "private raw exception")

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(ValueError) as error:
        live_storage.validate_hls_directory(str(tmp_path))
    assert "Permission denied (EACCES)" in str(error.value)
    assert "could not be read" in str(error.value)
    assert "private raw exception" not in str(error.value)


def test_missing_filesystem_root_is_reported(tmp_path, monkeypatch):
    def missing(path):
        raise FileNotFoundError(errno.ENOENT, "private raw exception")

    monkeypatch.setattr(Path, "stat", missing)
    with pytest.raises(ValueError, match=r"No such file or directory \(ENOENT\)"):
        live_storage.validate_hls_directory(str(tmp_path))


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EROFS, None])
def test_os_failure_context_uses_numeric_identity_when_accounts_are_unknown(
    tmp_path, monkeypatch, code
):
    def unknown(identifier):
        raise KeyError(identifier)

    monkeypatch.setattr(live_storage.pwd, "getpwuid", unknown)
    monkeypatch.setattr(live_storage.grp, "getgrgid", unknown)
    error = OSError(code, "private raw exception") if code is not None else OSError("secret")
    message = live_storage.hls_directory_error(tmp_path / "missing", error)
    assert f"UID={live_storage.os.geteuid()}" in message
    assert "private raw exception" not in message and "secret" not in message
    assert (errno.errorcode[code] if code is not None else "Filesystem operation failed") in message
