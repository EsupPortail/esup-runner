#!/usr/bin/env python3
"""ESUP-Runner release-stack helper.

Purpose:
    Prepare and optionally publish Manager and Runner releases from the
    monorepo root.

Design:
    This script intentionally orchestrates existing component commands instead
    of reimplementing component-specific release logic. Version changes still go
    through each component's ``scripts/manage_version.py`` helper, dependency
    work still goes through the component Makefiles, and release notes are read
    from the component changelogs.

Safety model:
    The default command prepares local files only. Git commits, branch pushes,
    tag creation, and tag pushes all require explicit flags. The script also
    checks the branch, working tree, local tags, and remote tags before doing
    publishing work.

Typical usage:
    Preview:
        ``uv run scripts/release_stack.py prepare 1.1.1 --dry-run``

    Prepare local files:
        ``uv run scripts/release_stack.py prepare 1.1.1``

    Publish after review:
        ``uv run scripts/release_stack.py prepare 1.1.1 --commit --push --create-tags --push-tags``

Notes:
    Pushing ``manager-vX.Y.Z`` or ``runner-vX.Y.Z`` tags triggers the GitHub
    release workflow, which publishes Docker images and creates GitHub Releases.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
HEADING_RE = re.compile(r"^## \[([^\]]+)\](?: - ([0-9]{4}-[0-9]{2}-[0-9]{2}))?\s*$", re.M)


@dataclass(frozen=True)
class Component:
    """Release configuration for one monorepo component.

    The command tuples are intentionally stored as data so the orchestration
    path can stay generic for ``manager`` and ``runner`` while still preserving
    their different lock/sync commands.
    """

    name: str
    title: str
    version_command: tuple[str, ...]
    ci_command: tuple[str, ...]
    lock_command: tuple[str, ...]
    sync_command: tuple[str, ...]
    tag_prefix: str
    tag_message_prefix: str

    @property
    def directory(self) -> Path:
        """Return the component project directory."""
        return REPO_ROOT / self.name

    @property
    def changelog(self) -> Path:
        """Return the component changelog path."""
        return self.directory / "docs" / "CHANGELOG.md"

    @property
    def release_paths(self) -> tuple[Path, ...]:
        """Return files expected to change during a normal release prepare."""
        return (
            self.directory / "app" / "__version__.py",
            self.directory / "VERSION",
            self.directory / "pyproject.toml",
            self.directory / "uv.lock",
            self.changelog,
        )


COMPONENTS: dict[str, Component] = {
    "manager": Component(
        name="manager",
        title="Manager",
        version_command=("uv", "run", "scripts/manage_version.py", "set"),
        ci_command=("make", "ci"),
        lock_command=("make", "lock-upgrade", "EXTRAS=dev"),
        sync_command=("make", "sync-dev"),
        tag_prefix="manager-v",
        tag_message_prefix="Manager release",
    ),
    "runner": Component(
        name="runner",
        title="Runner",
        version_command=("uv", "run", "scripts/manage_version.py", "set"),
        ci_command=("make", "ci"),
        lock_command=("make", "lock-all"),
        sync_command=("make", "sync-all"),
        tag_prefix="runner-v",
        tag_message_prefix="Runner release",
    ),
}


class ReleaseError(RuntimeError):
    """Raised for expected release-preparation failures."""


def log(message: str) -> None:
    """Print an operator-facing progress message."""
    print(f"==> {message}")


def warn(message: str) -> None:
    """Print an operator-facing warning message."""
    print(f"warning: {message}", file=sys.stderr)


def format_command(command: Sequence[str]) -> str:
    """Format a command sequence for logs and error messages."""
    return " ".join(command)


def run(
    command: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    dry_run: bool = False,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command from a repository-relative working directory.

    When ``dry_run`` is true, the command is logged and a successful
    ``CompletedProcess`` is returned without spawning a process.
    """

    relative_cwd = cwd.relative_to(REPO_ROOT) if cwd != REPO_ROOT else "."
    if dry_run:
        log(f"dry-run: ({relative_cwd}) {format_command(command)}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    log(f"run: ({relative_cwd}) {format_command(command)}")
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def run_stdout(command: Sequence[str], *, cwd: Path = REPO_ROOT) -> str:
    """Run a command and return stripped stdout."""
    completed = run(command, cwd=cwd, capture=True)
    return completed.stdout.strip()


def validate_version(version: str) -> None:
    """Validate that a release version follows ``MAJOR.MINOR.PATCH``."""
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError("version must follow MAJOR.MINOR.PATCH, for example 1.1.1")


def parse_components(raw_components: str) -> list[Component]:
    """Parse a comma-separated component list into configured components."""
    names = [name.strip() for name in raw_components.split(",") if name.strip()]
    if not names:
        raise ReleaseError("at least one component is required")

    unknown = [name for name in names if name not in COMPONENTS]
    if unknown:
        valid = ", ".join(COMPONENTS)
        raise ReleaseError(f"unknown component(s): {', '.join(unknown)}. Valid values: {valid}")

    seen: set[str] = set()
    components: list[Component] = []
    for name in names:
        if name not in seen:
            components.append(COMPONENTS[name])
            seen.add(name)
    return components


def ensure_git_available() -> None:
    """Fail early when git is not available."""
    run(("git", "--version"), capture=True)


def ensure_branch(expected_branch: str, *, skip_branch_check: bool) -> None:
    """Ensure the current branch matches the expected release branch."""
    if skip_branch_check:
        return

    branch = run_stdout(("git", "branch", "--show-current"))
    if branch != expected_branch:
        raise ReleaseError(
            f"current branch is {branch or 'detached HEAD'}, expected {expected_branch}. "
            "Use --skip-branch-check to override."
        )


def ensure_clean_worktree(*, allow_dirty: bool, dry_run: bool) -> None:
    """Ensure the repository has no pending changes before release work."""
    if allow_dirty or dry_run:
        if allow_dirty:
            warn("continuing with a dirty worktree because --allow-dirty was provided")
        return

    status = run_stdout(("git", "status", "--porcelain"))
    if status:
        raise ReleaseError(
            "working tree is not clean. Commit/stash existing changes or use --allow-dirty."
        )


def tag_name(component: Component, version: str) -> str:
    """Build the release tag name for a component/version pair."""
    return f"{component.tag_prefix}{version}"


def ensure_local_tags_do_not_exist(components: Iterable[Component], version: str) -> None:
    """Ensure release tags do not already exist locally."""
    for component in components:
        tag = tag_name(component, version)
        completed = run(
            ("git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"),
            check=False,
            capture=True,
        )
        if completed.returncode == 0:
            raise ReleaseError(f"local tag already exists: {tag}")


def ensure_remote_tags_do_not_exist(
    components: Iterable[Component],
    version: str,
    *,
    remote: str,
    skip_remote_tag_check: bool,
    dry_run: bool,
) -> None:
    """Ensure release tags do not already exist on the configured remote."""
    if skip_remote_tag_check or dry_run:
        return

    for component in components:
        tag = tag_name(component, version)
        completed = run(
            ("git", "ls-remote", "--exit-code", "--tags", remote, tag),
            check=False,
            capture=True,
        )
        if completed.returncode == 0:
            raise ReleaseError(f"remote tag already exists on {remote}: {tag}")
        if completed.returncode not in (0, 2):
            stderr = completed.stderr.strip()
            raise ReleaseError(f"failed to check remote tag {tag}: {stderr}")


def update_uv(*, dry_run: bool) -> None:
    """Update uv using Astral's installer, or show the command in dry-run mode."""
    run(("uv", "--version"), dry_run=dry_run, check=False)

    if dry_run:
        log("dry-run: curl -LsSf https://astral.sh/uv/install.sh | sh")
        return

    log("run: update uv via Astral installer")
    curl = subprocess.Popen(
        ("curl", "-LsSf", "https://astral.sh/uv/install.sh"),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=False,
    )
    shell = subprocess.run(("sh",), cwd=REPO_ROOT, stdin=curl.stdout)
    if curl.stdout is not None:
        curl.stdout.close()
    curl_returncode = curl.wait()
    if curl_returncode != 0 or shell.returncode != 0:
        raise ReleaseError("uv installer failed")


def find_changelog_section(content: str, section_name: str) -> tuple[re.Match[str], int, int] | None:
    """Find a Keep a Changelog ``## [section]`` block in markdown content.

    Returns the heading match plus the byte offsets delimiting the section body.
    """

    matches = list(HEADING_RE.finditer(content))
    for index, match in enumerate(matches):
        if match.group(1) == section_name:
            body_start = match.end()
            body_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            return match, body_start, body_end
    return None


def extract_changelog_section(changelog_path: Path, version: str) -> str:
    """Return release notes for one version from a component changelog."""
    content = changelog_path.read_text(encoding="utf-8")
    section = find_changelog_section(content, version)
    if section is None:
        raise ReleaseError(f"{changelog_path}: missing changelog section [{version}]")

    _match, body_start, body_end = section
    body = content[body_start:body_end].strip()
    if not body:
        raise ReleaseError(f"{changelog_path}: changelog section [{version}] is empty")
    return body + "\n"


def promote_changelog(
    component: Component,
    version: str,
    release_date: str,
    *,
    allow_empty_changelog: bool,
    dry_run: bool,
) -> None:
    """Promote ``[Unreleased]`` changelog entries to a dated release section."""
    path = component.changelog
    content = path.read_text(encoding="utf-8")

    if find_changelog_section(content, version) is not None:
        raise ReleaseError(f"{path}: changelog already contains [{version}]")

    section = find_changelog_section(content, "Unreleased")
    if section is None:
        raise ReleaseError(f"{path}: missing [Unreleased] section")

    match, body_start, body_end = section
    body = content[body_start:body_end].strip("\n")
    if not body.strip() and not allow_empty_changelog:
        raise ReleaseError(
            f"{path}: [Unreleased] is empty. Use --allow-empty-changelog to release anyway."
        )

    replacement = f"{match.group(0)}\n\n"
    replacement += f"## [{version}] - {release_date}\n"
    if body:
        replacement += f"\n{body.strip()}\n"
    replacement += "\n"

    updated = content[: match.start()] + replacement + content[body_end:].lstrip("\n")
    if dry_run:
        log(
            f"dry-run: promote {path.relative_to(REPO_ROOT)} "
            f"[Unreleased] to [{version}] - {release_date}"
        )
        return

    path.write_text(updated, encoding="utf-8")
    log(f"updated {path.relative_to(REPO_ROOT)}")


def prepare_component(
    component: Component,
    version: str,
    release_date: str,
    *,
    skip_version: bool,
    skip_ci: bool,
    skip_lock: bool,
    skip_sync: bool,
    skip_changelog: bool,
    allow_empty_changelog: bool,
    dry_run: bool,
) -> None:
    """Run the local release-preparation workflow for one component."""
    log(f"prepare {component.title} {version}")

    if not skip_version:
        run((*component.version_command, version), cwd=component.directory, dry_run=dry_run)
    else:
        warn(f"skipping {component.name} version update")

    if not skip_ci:
        run(component.ci_command, cwd=component.directory, dry_run=dry_run)
    else:
        warn(f"skipping {component.name} CI")

    if not skip_lock:
        run(component.lock_command, cwd=component.directory, dry_run=dry_run)
    else:
        warn(f"skipping {component.name} lock refresh")

    if not skip_sync:
        run(component.sync_command, cwd=component.directory, dry_run=dry_run)
    else:
        warn(f"skipping {component.name} dependency sync")

    if not skip_changelog:
        promote_changelog(
            component,
            version,
            release_date,
            allow_empty_changelog=allow_empty_changelog,
            dry_run=dry_run,
        )
    else:
        warn(f"skipping {component.name} changelog promotion")


def git_add_release_paths(components: Iterable[Component], *, dry_run: bool) -> None:
    """Stage the files that normally change during release preparation."""
    paths = [
        str(path.relative_to(REPO_ROOT))
        for component in components
        for path in component.release_paths
    ]
    run(("git", "add", *paths), dry_run=dry_run)


def commit_release(components: Sequence[Component], version: str, *, dry_run: bool) -> None:
    """Commit staged release changes for the selected components."""
    git_add_release_paths(components, dry_run=dry_run)

    if dry_run:
        log("dry-run: git diff --cached --quiet || git commit ...")
        return

    diff = run(("git", "diff", "--cached", "--quiet"), check=False, capture=True)
    if diff.returncode == 0:
        raise ReleaseError("no staged release changes to commit")
    if diff.returncode != 1:
        raise ReleaseError("failed to inspect staged release changes")

    names = " and ".join(component.name for component in components)
    run(("git", "commit", "-m", f"chore: release {names} {version}"))


def push_branch(remote: str, branch: str, *, dry_run: bool) -> None:
    """Push the current HEAD to the target branch on the configured remote."""
    if not branch:
        raise ReleaseError("cannot push from detached HEAD")
    run(("git", "push", remote, f"HEAD:{branch}"), dry_run=dry_run)


def create_tags(components: Iterable[Component], version: str, *, dry_run: bool) -> None:
    """Create annotated component tags on the last commit touching each component."""
    for component in components:
        tag = tag_name(component, version)
        sha = (
            run_stdout(("git", "rev-list", "-1", "HEAD", "--", component.name))
            if not dry_run
            else "<component-sha>"
        )
        run(
            (
                "git",
                "tag",
                "-a",
                tag,
                sha,
                "-m",
                f"{component.tag_message_prefix} {version}",
            ),
            dry_run=dry_run,
        )


def push_tags(components: Iterable[Component], version: str, *, remote: str, dry_run: bool) -> None:
    """Push component release tags to the configured remote."""
    for component in components:
        run(("git", "push", remote, tag_name(component, version)), dry_run=dry_run)


def print_release_notes(components: Iterable[Component], version: str) -> None:
    """Print release notes for selected components to stdout."""
    for component in components:
        notes = extract_changelog_section(component.changelog, version)
        print(f"# {component.title} v{version}")
        print()
        print(notes.rstrip())
        print()


def command_notes(args: argparse.Namespace) -> int:
    """Handle the ``notes`` subcommand."""
    validate_version(args.version)
    component = COMPONENTS[args.component]
    notes = extract_changelog_section(component.changelog, args.version)
    sys.stdout.write(notes)
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    """Handle the ``prepare`` subcommand."""
    validate_version(args.version)
    components = parse_components(args.components)

    ensure_git_available()
    ensure_branch(args.branch, skip_branch_check=args.skip_branch_check)
    ensure_clean_worktree(allow_dirty=args.allow_dirty, dry_run=args.dry_run)

    if args.create_tags or args.push_tags:
        ensure_local_tags_do_not_exist(components, args.version)
        ensure_remote_tags_do_not_exist(
            components,
            args.version,
            remote=args.remote,
            skip_remote_tag_check=args.skip_remote_tag_check,
            dry_run=args.dry_run,
        )

    if args.push and not args.commit:
        warn("--push was provided without --commit; the current HEAD will be pushed")
    if args.push_tags and not args.create_tags:
        warn("--push-tags was provided without --create-tags; existing local tags will be pushed")

    if args.update_uv:
        update_uv(dry_run=args.dry_run)

    for component in components:
        prepare_component(
            component,
            args.version,
            args.release_date,
            skip_version=args.skip_version,
            skip_ci=args.skip_ci,
            skip_lock=args.skip_lock,
            skip_sync=args.skip_sync,
            skip_changelog=args.skip_changelog,
            allow_empty_changelog=args.allow_empty_changelog,
            dry_run=args.dry_run,
        )

    if args.print_release_notes and not args.skip_changelog:
        print_release_notes(components, args.version)

    if args.commit:
        commit_release(components, args.version, dry_run=args.dry_run)

    if args.push:
        branch = run_stdout(("git", "branch", "--show-current")) if not args.dry_run else args.branch
        push_branch(args.remote, branch, dry_run=args.dry_run)

    if args.create_tags:
        create_tags(components, args.version, dry_run=args.dry_run)

    if args.push_tags:
        push_tags(components, args.version, remote=args.remote, dry_run=args.dry_run)

    if not args.push_tags:
        log("tags were not pushed; GitHub release workflow will not run until tags are pushed")

    log("release preparation finished")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build and return the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Prepare ESUP-Runner manager/runner releases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="prepare a manager and/or runner release",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    prepare.add_argument("version", help="release version, for example 1.1.1")
    prepare.add_argument(
        "--components",
        default="manager,runner",
        help="comma-separated components to release: manager, runner, or both",
    )
    prepare.add_argument("--branch", default="main", help="expected git branch")
    prepare.add_argument("--remote", default="origin", help="git remote used for pushes")
    prepare.add_argument(
        "--release-date",
        default=date.today().isoformat(),
        help="date written to changelog release headings",
    )
    prepare.add_argument("--dry-run", action="store_true", help="print commands without changing files")
    prepare.add_argument("--allow-dirty", action="store_true", help="allow a dirty worktree")
    prepare.add_argument("--skip-branch-check", action="store_true", help="do not require --branch")
    prepare.add_argument("--skip-remote-tag-check", action="store_true", help="do not query remote tags")
    prepare.add_argument("--update-uv", action="store_true", help="update uv with the Astral installer")
    prepare.add_argument("--skip-version", action="store_true", help="skip manage_version.py set")
    prepare.add_argument("--skip-ci", action="store_true", help="skip make ci")
    prepare.add_argument("--skip-lock", action="store_true", help="skip lock refresh")
    prepare.add_argument("--skip-sync", action="store_true", help="skip dependency sync")
    prepare.add_argument("--skip-changelog", action="store_true", help="skip changelog promotion")
    prepare.add_argument(
        "--allow-empty-changelog",
        action="store_true",
        help="allow release when [Unreleased] has no entries",
    )
    prepare.add_argument(
        "--print-release-notes",
        action="store_true",
        help="print generated release notes after changelog promotion",
    )
    prepare.add_argument("--commit", action="store_true", help="commit release file changes")
    prepare.add_argument("--push", action="store_true", help="push the current branch")
    prepare.add_argument("--create-tags", action="store_true", help="create annotated component tags")
    prepare.add_argument("--push-tags", action="store_true", help="push component tags")
    prepare.set_defaults(func=command_prepare)

    notes = subparsers.add_parser(
        "notes",
        help="print release notes for one component from its changelog",
    )
    notes.add_argument("component", choices=sorted(COMPONENTS), help="component name")
    notes.add_argument("version", help="release version, for example 1.1.1")
    notes.set_defaults(func=command_notes)

    return parser


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """Keep the convenient old shape: release_stack.py 1.1.1 ..."""
    if argv and VERSION_RE.fullmatch(argv[0]):
        return ["prepare", *argv]
    return list(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(normalize_argv(argv if argv is not None else sys.argv[1:]))

    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed ({exc.returncode}): {format_command(exc.cmd)}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
