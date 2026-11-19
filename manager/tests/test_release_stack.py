"""Regression checks for shared release orchestration without Git publication."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def release_tool(monkeypatch):
    """Load the shared script with isolated Git and publication operations."""
    path = Path(__file__).resolve().parents[2] / "scripts/release_stack.py"
    spec = importlib.util.spec_from_file_location("release_stack_tests", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ensure_git_available", lambda: None)
    monkeypatch.setattr(module, "ensure_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "ensure_clean_worktree", lambda **kwargs: None)
    return module


def test_dry_run_notes_need_no_generated_release(release_tool, tmp_path, monkeypatch, capsys):
    """A preview reads Unreleased without expecting a section it has not written."""
    monkeypatch.setattr(release_tool, "REPO_ROOT", tmp_path)
    changelog = tmp_path / "manager/docs/CHANGELOG.md"
    changelog.parent.mkdir(parents=True)
    content = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Fix work.\n"
    changelog.write_text(content)
    result = release_tool.main(
        ["prepare", "1.2.3", "--components", "manager", "--dry-run", "--print-release-notes"]
    )
    assert result == 0
    assert changelog.read_text() == content
    assert "print release notes after changelog promotion" in capsys.readouterr().out


def test_push_existing_tags_does_not_require_absent_local_tags(release_tool, monkeypatch):
    """Existing tags can be pushed without passing the creation-only guard."""
    local_guard = Mock(side_effect=AssertionError("Existing tags must be allowed"))
    remote_guard, push = Mock(), Mock()
    monkeypatch.setattr(release_tool, "ensure_local_tags_do_not_exist", local_guard)
    monkeypatch.setattr(release_tool, "ensure_remote_tags_do_not_exist", remote_guard)
    monkeypatch.setattr(release_tool, "prepare_component", Mock())
    monkeypatch.setattr(release_tool, "push_tags", push)
    assert release_tool.main(["prepare", "1.2.3", "--push-tags"]) == 0
    local_guard.assert_not_called()
    remote_guard.assert_called_once()
    push.assert_called_once()


def test_reviewed_preparation_can_be_committed_without_repeating_steps(release_tool, monkeypatch):
    """The documented resume flags keep prepared files and checks intact."""
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(release_tool, "run", run)
    monkeypatch.setattr(release_tool, "commit_release", Mock())
    assert (
        release_tool.main(
            [
                "prepare",
                "1.2.3",
                "--allow-dirty",
                "--skip-version",
                "--skip-lock",
                "--skip-sync",
                "--skip-ci",
                "--skip-changelog",
                "--commit",
            ]
        )
        == 0
    )
    run.assert_not_called()
    release_tool.commit_release.assert_called_once()
