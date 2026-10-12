"""Live paths stay below the configured root without following child symlinks."""

from pathlib import Path

import pytest

from app.services.live_paths import contained_path


def test_contained_path_accepts_existing_and_missing_children(tmp_path):
    root = tmp_path / "hls"
    root.mkdir()
    file = root / "session" / "high" / "index.m3u8"
    assert contained_path(root, "session/high/index.m3u8") == file
    file.parent.mkdir(parents=True)
    file.write_text("#EXTM3U\n")
    assert contained_path(root, "session/high/index.m3u8").read_text() == "#EXTM3U\n"


@pytest.mark.parametrize(
    "relative",
    ["", ".", "..", "../hls-other/file", "session/../file", "./file", "/etc/passwd"],
)
def test_contained_path_rejects_root_traversal_and_absolute_paths(tmp_path, relative):
    root = tmp_path / "hls"
    root.mkdir()
    with pytest.raises(ValueError):
        contained_path(root, relative)


@pytest.mark.parametrize("target_kind", ["outside", "inside", "missing", "loop"])
@pytest.mark.parametrize("nested", [False, True])
def test_contained_path_rejects_child_and_parent_symlinks(tmp_path, target_kind, nested):
    root = tmp_path / "hls"
    root.mkdir()
    link = root / "link"
    targets = {
        "outside": tmp_path / "target",
        "inside": root / "target",
        "missing": tmp_path / "missing",
        "loop": link,
    }
    target = targets[target_kind]
    if target_kind in {"outside", "inside"}:
        if nested:
            target.mkdir()
            (target / "index.m3u8").write_text("private data")
        else:
            target.write_text("private data")
    link.symlink_to(target, target_is_directory=nested)
    with pytest.raises((ValueError, OSError)):
        contained_path(root, "link/index.m3u8" if nested else "link")
    if target_kind in {"outside", "inside"}:
        assert (target / "index.m3u8" if nested else target).read_text() == "private data"


def test_contained_path_allows_a_configured_root_symlink(tmp_path):
    target = tmp_path / "output"
    target.mkdir()
    root = tmp_path / "hls"
    root.symlink_to(target, target_is_directory=True)
    assert contained_path(root, "session/master.m3u8") == target / "session/master.m3u8"


def test_contained_path_handles_a_filesystem_root():
    assert contained_path(Path("/"), "tmp") == Path("/tmp")
