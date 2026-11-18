"""Check Live output access and describe filesystem failures without media secrets."""

import errno
import grp
import os
import pwd
import stat
from pathlib import Path


def _identity(uid: int, gid: int) -> str:
    """Describe a local account, retaining numeric IDs when names are unavailable."""
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return f"{user}:{group} (UID={uid}, GID={gid})"


def _existing_parent(directory: Path) -> tuple[Path, os.stat_result]:
    """Find the output path or its nearest existing ancestor without creating it."""
    for candidate in (directory, *directory.parents):
        try:
            return candidate, candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))


def hls_directory_error(directory: Path, error: OSError) -> str:
    """Describe HLS storage, effective account and OS error without echoing raw exceptions."""
    account = _identity(os.geteuid(), os.getegid())
    reason = (
        f"{os.strerror(error.errno)} ({errno.errorcode.get(error.errno, error.errno)})"
        if error.errno is not None
        else "Filesystem operation failed"
    )
    try:
        existing, info = _existing_parent(directory)
        metadata = (
            f"Existing path {str(existing)!r}: owner={_identity(info.st_uid, info.st_gid)}, "
            f"mode={stat.S_IMODE(info.st_mode):04o}."
        )
    except OSError:
        metadata = "Directory ownership and permissions could not be read."
    return (
        f"LIVE_HLS_DIRECTORY {str(directory)!r}: {reason}. Runner account={account}. "
        f"{metadata} HLS files are written by Runner/FFmpeg. "
        "Check write/search permissions for this account, parent directories and available space, "
        "or choose a writable LIVE_HLS_DIRECTORY."
    )


def validate_hls_directory(directory: str) -> None:
    """Reject inaccessible HLS output or parents before startup, without modifying storage."""
    path = Path(directory)
    try:
        existing, info = _existing_parent(path)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
        if not os.access(existing, os.W_OK | os.X_OK, effective_ids=True):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    except OSError as error:
        raise ValueError(hls_directory_error(path, error)) from None
