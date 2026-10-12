"""Normalize and confine Live filesystem access before using user-derived paths."""

import os
from pathlib import Path


def contained_path(root: Path, relative: str) -> Path:
    """Return a normalized child of root without traversing child symlinks.

    The configured root may itself be a symlink. Reject absolute paths, the
    root itself, traversal components and symlinks below it with ValueError.
    Missing files are allowed so the same checks protect output creation.
    """
    if os.path.isabs(relative):
        raise ValueError("Invalid Live path")
    base = os.path.realpath(root)
    candidate = os.path.join(base, relative)
    normalized = os.path.realpath(candidate)
    if not normalized.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError("Live path escapes its output directory")
    if normalized != candidate:
        raise ValueError("Live path contains traversal or a symlink")
    path = Path(normalized)
    for component in (path, *path.parents):
        if component == Path(base):
            break
        if component.is_symlink():
            raise ValueError("Live path is a symlink")
    return path
