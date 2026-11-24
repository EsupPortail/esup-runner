"""Integration tests for the repository-level stack update helper."""

import os
import shlex
import shutil
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UPDATE_STACK_SCRIPT = REPOSITORY_ROOT / "update-stack.sh"


def _git(repository: Path, *args: str) -> str:
    """Run local Git commands in a disposable test repository."""
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_files(repository: Path, files: dict[str, str | None]) -> str:
    """Commit fixture files and return their revision, without relying on user config."""
    for name, content in files.items():
        path = repository / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        "user.name=Stack update tests",
        "-c",
        "user.email=stack-tests@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Configuration fixture",
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def configuration_repository(tmp_path):
    """Create a repository whose local environment files stay outside Git."""
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--initial-branch=main")
    _commit_files(repository, {".gitignore": ".env\n"})
    return repository


@pytest.fixture
def configuration_detector():
    """Load the self-contained detector from the actual update script."""
    source = UPDATE_STACK_SCRIPT.read_text(encoding="utf-8")
    code = source.split("<<'PY_CONFIG_CHANGES'\n", 1)[1].split("\nPY_CONFIG_CHANGES\n", 1)[0]
    definitions = code.split("\nrepository, before, after, scope = sys.argv[1:]", 1)[0]
    namespace: dict[str, Any] = {}
    exec(compile(definitions, "update-stack-config-changes", "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ("", "NEW=value\n# OPTIONAL=value\n", ["- Parameters added: NEW, OPTIONAL"]),
        ("OLD=value\n# OPTIONAL=value\n", "", ["- Parameters removed: OLD, OPTIONAL"]),
        ("VALUE=before\n", "VALUE=after\n", ["- Example values changed: VALUE"]),
        ("# VALUE=before\n", "# VALUE=after\n", ["- Example values changed: VALUE"]),
        (
            "# VALUE=same\n",
            "VALUE=same\n",
            ["- Example assignments enabled/disabled: VALUE"],
        ),
        (
            "VALUE=same\n",
            "# VALUE=same\n",
            ["- Example assignments enabled/disabled: VALUE"],
        ),
        (
            "OLD=value\n",
            "NEW=value\n",
            ["- Parameters added: NEW", "- Parameters removed: OLD"],
        ),
        ("A=1\nB=2\n", "B=2\nA=1\n", []),
        ("# Description\nA=1\n", "# Different description\nA=1\n", []),
        ("A=1 # Old comment\n", "  export A = '1' # New comment\n", []),
        ("# A=1\n", '  # A = "1" # Optional parameter\n', []),
        ("A=\n", 'A=""\n', []),
        ("A=\n", "A= # Empty value\n", []),
        ("A=a#b\n", 'A="a#b" # Description\n', []),
        ("A='hello world'\n", "A=hello world\n", []),
        ("A=first\nA=second\n", "A=second\n", []),
        ("A=active\n# A=optional\n", "# A=optional\nA=active\n", []),
        ("# A=first\n# A=second\n", "# A=second\n", []),
        ("A=abc\n", 'A="abc"\n', []),
    ],
)
def test_example_parameter_changes(configuration_detector, before, after, expected):
    """Report meaningful assignment changes, keeping formatting and values private."""
    assert configuration_detector["example_changes"](before, after) == expected


@pytest.mark.parametrize("component", ["manager", "runner"])
@pytest.mark.parametrize("filename", ["config.py", "live_config.py"])
def test_configuration_code_changes_are_reported(
    configuration_detector, configuration_repository, component, filename
):
    """Detect changed defaults or rules even when the environment example is unchanged."""
    repository = configuration_repository
    path = f"{component}/app/core/{filename}"
    before = _commit_files(repository, {path: "LIMIT = 1\n"})
    after = _commit_files(repository, {path: "LIMIT = 2\n"})
    (repository / component / ".env").write_text("LOCAL_SECRET=private\n", encoding="utf-8")
    configuration_detector["repository"] = str(repository)

    report = configuration_detector["configuration_report"](before, after, "auto")

    assert f"{component.title()} configuration:" in report
    assert f"Configuration defaults or validation rules changed: {path}" in report
    assert f"{component}/docs/PARAMETERS.md" in report
    assert "private" not in report
    assert "LIMIT" not in report


@pytest.mark.parametrize("before_exists", [False, True])
def test_configuration_example_can_be_added_or_removed(
    configuration_detector, configuration_repository, before_exists
):
    """Handle a tracked configuration example missing in either Git revision."""
    repository = configuration_repository
    path = "runner/.env.example"
    before = _commit_files(
        repository, {path: "EXAMPLE=hidden-value\n"} if before_exists else {"README.md": "old\n"}
    )
    after = _commit_files(repository, {path: None if before_exists else "EXAMPLE=hidden-value\n"})
    (repository / "runner" / ".env").write_text("LOCAL_SECRET=private\n", encoding="utf-8")
    configuration_detector["repository"] = str(repository)

    report = configuration_detector["configuration_report"](before, after, "runner")

    action = "removed" if before_exists else "added"
    assert f"Parameters {action}: EXAMPLE" in report
    assert "hidden-value" not in report
    assert "private" not in report


@pytest.mark.parametrize(
    "scope,installed,expected_components",
    [
        ("auto", ("manager", "runner"), ("manager", "runner")),
        ("manager", ("manager", "runner"), ("manager",)),
        ("runner", ("manager", "runner"), ("runner",)),
        ("auto", ("manager",), ("manager",)),
        ("auto", (), ()),
    ],
)
def test_configuration_report_respects_installation_and_selection(
    configuration_detector, configuration_repository, scope, installed, expected_components
):
    """Keep alerts scoped to installed components selected by the updater."""
    repository = configuration_repository
    before = _commit_files(
        repository, {f"{component}/.env.example": "" for component in ("manager", "runner")}
    )
    after = _commit_files(
        repository,
        {f"{component}/.env.example": "NEW=value\n" for component in ("manager", "runner")},
    )
    for component in installed:
        (repository / component / ".env").write_text("", encoding="utf-8")
    configuration_detector["repository"] = str(repository)

    report = configuration_detector["configuration_report"](before, after, scope)

    for component in ("manager", "runner"):
        assert (f"{component.title()} configuration:" in report) == (
            component in expected_components
        )


def _prepare_update(tmp_path, repository):
    """Clone the old revision and install mocks for Make, Python, and SMTP."""
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(repository), str(checkout)], check=True
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "make").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "make").chmod(0o755)
    (fake_bin / "python3").write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8"
    )
    (fake_bin / "python3").chmod(0o755)
    # The runner FFmpeg is resolved from the service PATH, which starts with ~/.local/bin.
    service_home = tmp_path / "home"
    (service_home / ".local/bin").mkdir(parents=True)
    (fake_bin / "getent").write_text(
        f"#!/bin/sh\nprintf 'esup-runner:x:1001:1001::%s:/bin/sh\\n' {shlex.quote(str(service_home))}\n",
        encoding="utf-8",
    )
    (fake_bin / "getent").chmod(0o755)
    fake_ffmpeg = service_home / ".local/bin/ffmpeg"
    fake_ffmpeg.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$TEST_FFMPEG_VERSION"\nexit "${TEST_FFMPEG_EXIT_CODE:-0}"\n',
        encoding="utf-8",
    )
    fake_ffmpeg.chmod(0o755)
    smtp_module = tmp_path / "smtp_mock"
    smtp_module.mkdir()
    (smtp_module / "smtplib.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "class SMTP:\n"
        "    def __init__(self, *args, **kwargs): pass\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): pass\n"
        "    def send_message(self, message):\n"
        "        Path(os.environ['TEST_EMAIL_PATH']).write_bytes(message.as_bytes())\n",
        encoding="utf-8",
    )
    email_path = tmp_path / "update.eml"
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        PYTHONPATH=str(smtp_module),
        TEST_EMAIL_PATH=str(email_path),
        TEST_FFMPEG_VERSION="ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers",
        NO_COLOR="1",
        GIT_TERMINAL_PROMPT="0",
    )
    for component in ("manager", "runner"):
        component_dir = checkout / component
        component_dir.mkdir(exist_ok=True)
        (component_dir / ".env").write_text(
            "LOCAL_SECRET=local-private-value\n"
            "SMTP_SERVER=smtp.example.invalid\n"
            "MANAGER_EMAIL=operator@example.invalid\n",
            encoding="utf-8",
        )
    return checkout, env, email_path


def _run_update(checkout, env, *options, run_test=False):
    """Run the real update workflow against a local Git remote and mocked services."""
    if not run_test:
        options = ("--skip-test", *options)
    return subprocess.run(
        [
            "bash",
            str(UPDATE_STACK_SCRIPT),
            "--root-dir",
            str(checkout),
            "--skip-uv-update",
            "--runner-sync-mode",
            "base",
            "--no-restart",
            *options,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=20,
    )


def _email_content(message, subtype):
    """Require the requested MIME body before checking its text."""
    body = message.get_body((subtype,))
    assert body is not None
    return body.get_content()


@pytest.mark.parametrize("scope", ["auto", "manager", "runner"])
def test_update_stack_reports_no_updates_when_git_revision_is_unchanged(
    tmp_path, configuration_repository, scope
):
    """Report an unchanged stack explicitly while retaining installed versions."""
    _commit_files(
        configuration_repository,
        {"manager/VERSION": "1.2.3\n", "runner/VERSION": "1.2.4\n"},
    )
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)
    before = _git(checkout, "rev-parse", "HEAD")
    options = () if scope == "auto" else (f"--{scope}-only",)

    result = _run_update(checkout, env, *options)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == before
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "SUCCESS - No updates" in message["Subject"]
    for subtype in ("plain", "html"):
        body = _email_content(message, subtype)
        assert "No updates were applied. The stack is already up to date." in body
        for component, version in (("manager", "1.2.3"), ("runner", "1.2.4")):
            if scope in ("auto", component):
                assert version in message["Subject"]
                assert version in body
        assert ("FFmpeg MagicYUV" in body) == (scope != "manager")
    assert "Updated components   : none" in _email_content(message, "plain")
    assert ">none</td>" in _email_content(message, "html")


def _summary_value(output, label):
    """Read one row of the console update summary."""
    return (
        next(line for line in output.splitlines() if f"{label}:" in line).split(":", 1)[1].strip()
    )


@pytest.mark.parametrize(
    "version_output,exit_code,denylist,expected_denylist,expected_status",
    [
        (
            "ffmpeg version 8.1.1 Copyright (c) 2000-2026",
            0,
            "MEDIA_CODEC_DENYLIST=\n",
            "MEDIA_CODEC_DENYLIST=magicyuv\n",
            "FFmpeg 8.1.1 vulnerable; magicyuv added to MEDIA_CODEC_DENYLIST",
        ),
        (
            "ffmpeg version 7.1.5-0+deb13u1 Copyright (c) 2000-2026",
            0,
            "MEDIA_CODEC_DENYLIST='other'\n",
            "MEDIA_CODEC_DENYLIST=magicyuv,other\n",
            "FFmpeg 7.1.5 vulnerable; magicyuv added to MEDIA_CODEC_DENYLIST",
        ),
        (
            "ffmpeg version n8.1 Copyright (c) 2000-2026",
            0,
            "MEDIA_CODEC_DENYLIST=other # note\n",
            "MEDIA_CODEC_DENYLIST=magicyuv,other # note\n",
            "FFmpeg 8.1 vulnerable; magicyuv added to MEDIA_CODEC_DENYLIST",
        ),
        (
            "ffmpeg version 8.1.2 Copyright (c) 2000-2026",
            1,
            "# MEDIA_CODEC_DENYLIST=magicyuv\nMEDIA_CODEC_DENYLIST=\n",
            "# MEDIA_CODEC_DENYLIST=magicyuv\nMEDIA_CODEC_DENYLIST=magicyuv\n",
            "FFmpeg version unknown; magicyuv added to MEDIA_CODEC_DENYLIST",
        ),
        (
            "ffmpeg version N-121000-g0123abcd Copyright (c) 2000-2026",
            0,
            " MEDIA_CODEC_DENYLIST = other, MagicYUV \n",
            " MEDIA_CODEC_DENYLIST = other, MagicYUV \n",
            "FFmpeg version unknown; magicyuv denied",
        ),
        (
            "ffmpeg version 8.1.2 Copyright (c) 2000-2026",
            0,
            "",
            "",
            "FFmpeg 8.1.2 fixed; magicyuv can be removed from MEDIA_CODEC_DENYLIST",
        ),
        (
            "ffmpeg version n9.0.2-3-g0123abcd Copyright (c) 2000-2026",
            0,
            "MEDIA_CODEC_DENYLIST=\n",
            "MEDIA_CODEC_DENYLIST=\n",
            "FFmpeg 9.0.2 fixed",
        ),
    ],
)
def test_update_stack_checks_runner_ffmpeg_magicyuv_fix(
    tmp_path,
    configuration_repository,
    version_output,
    exit_code,
    denylist,
    expected_denylist,
    expected_status,
):
    """Deny MagicYUV until FFmpeg 8.1.2, then only suggest removing it from the denylist."""
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)
    env.update(TEST_FFMPEG_VERSION=version_output, TEST_FFMPEG_EXIT_CODE=str(exit_code))
    runner_env = checkout / "runner/.env"
    base_env = runner_env.read_text(encoding="utf-8")
    runner_env.write_text(base_env + denylist, encoding="utf-8")
    runner_env.chmod(0o640)

    result = _run_update(checkout, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert runner_env.read_text(encoding="utf-8") == base_env + expected_denylist
    assert runner_env.stat().st_mode & 0o777 == 0o640
    assert _summary_value(result.stdout, "FFmpeg MagicYUV") == expected_status
    assert ("You can remove magicyuv" in result.stdout) == expected_status.endswith(
        "can be removed from MEDIA_CODEC_DENYLIST"
    )
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    for subtype in ("plain", "html"):
        assert expected_status in _email_content(message, subtype)
    # A denylist change requires a runner restart, so it is not reported as "No updates".
    assert ("No updates" in message["Subject"]) == (denylist == expected_denylist)


@pytest.mark.parametrize("failure", ["dry-run", "mktemp"])
def test_update_stack_keeps_runner_env_when_denylist_is_not_written(
    tmp_path, configuration_repository, failure
):
    """Preview the denylist change in dry-run mode and report write failures."""
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)
    env.update(TEST_FFMPEG_VERSION="ffmpeg version 8.1.1 Copyright (c) 2000-2026")
    runner_env = checkout / "runner/.env"
    with runner_env.open("a", encoding="utf-8") as stream:
        stream.write("MEDIA_CODEC_DENYLIST=\n")
    original_env = runner_env.read_bytes()
    options = ("--dry-run",) if failure == "dry-run" else ()
    if failure == "mktemp":
        mktemp = tmp_path / "bin/mktemp"
        mktemp.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        mktemp.chmod(0o755)

    result = _run_update(checkout, env, *options)

    assert runner_env.read_bytes() == original_env
    assert not list(runner_env.parent.glob(".env.??????"))
    if failure == "dry-run":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "DRY-RUN: update MEDIA_CODEC_DENYLIST" in result.stdout
        assert _summary_value(result.stdout, "FFmpeg MagicYUV") == "dry-run"
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "set MEDIA_CODEC_DENYLIST=magicyuv manually" in result.stderr
        assert _summary_value(result.stdout, "FFmpeg MagicYUV") == "failed"
        message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
        assert "COMPLETED WITH WARNINGS" in message["Subject"]


@pytest.mark.parametrize("changed_file", ["manager/app/code.py", "runner/app/code.py", "README.md"])
def test_update_stack_does_not_report_no_updates_after_git_changes(
    tmp_path, configuration_repository, changed_file
):
    """Treat new sources as an update even when component versions stay the same."""
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)
    after = _commit_files(configuration_repository, {changed_file: "Updated content\n"})

    result = _run_update(checkout, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == after
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "No updates" not in message["Subject"]
    assert "SUCCESS" in message["Subject"]
    for subtype in ("plain", "html"):
        assert "No updates were applied" not in _email_content(message, subtype)


@pytest.mark.parametrize("profile", ["cuda12", "latest"])
def test_update_stack_does_not_report_no_updates_after_gpu_lock_refresh(
    tmp_path, configuration_repository, profile
):
    """Keep explicit GPU dependency refreshes visible with unchanged Git sources."""
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)

    result = _run_update(
        checkout,
        env,
        "--runner-only",
        "--runner-sync-mode",
        "transcription-gpu",
        "--gpu-lock-profile",
        profile,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Runner lock refresh" in result.stdout
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "No updates" not in message["Subject"]
    assert "Updated components   : runner" in _email_content(message, "plain")
    for subtype in ("plain", "html"):
        assert "No updates were applied" not in _email_content(message, subtype)


@pytest.mark.parametrize(
    "task_types,encoding_type,installed_cuda12,mode,profile,target",
    [
        ("encoding", "CPU", False, "auto", "none", "make sync"),
        ("transcription", "CPU", False, "transcription-cpu", "none", "make sync-transcription-cpu"),
        ("transcription", "GPU", False, "auto", "none", "make sync-transcription-gpu"),
        ("transcription", "CPU", True, "auto", "none", "make sync-transcription-gpu-cuda12"),
        (
            "transcription",
            "GPU",
            False,
            "transcription-gpu-cuda12",
            "none",
            "make sync-transcription-gpu-cuda12",
        ),
        ("transcription", "GPU", True, "auto", "latest", "make sync-transcription-gpu"),
        ("transcription", "GPU", True, "transcription-cpu", "none", "make sync-transcription-cpu"),
    ],
)
def test_update_selects_dependency_profile_without_environment_override(
    tmp_path,
    configuration_repository,
    task_types,
    encoding_type,
    installed_cuda12,
    mode,
    profile,
    target,
):
    """Select profiles automatically, preserving CUDA 12 and explicit CLI overrides."""
    checkout, environment, _ = _prepare_update(tmp_path, configuration_repository)
    with (checkout / "runner/.env").open("a") as stream:
        stream.write(
            f"RUNNER_TASK_TYPES={task_types}\nENCODING_TYPE={encoding_type}\n"
            "RUNNER_SYNC_MODE=base\n"  # An obsolete dotenv entry must not override detection.
        )
    if installed_cuda12:
        (checkout / "runner/.venv/lib/python3.12/site-packages/torch-2.13.0+cu126.dist-info").mkdir(
            parents=True
        )
    result = _run_update(
        checkout,
        environment,
        "--runner-only",
        "--skip-git-update",
        "--skip-email",
        "--runner-sync-mode",
        mode,
        "--gpu-lock-profile",
        profile,
        "--dry-run",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target in result.stdout
    if target == "make sync-transcription-gpu":
        assert "make sync-transcription-gpu-cuda12" not in result.stdout


@pytest.fixture
def smoke_update(tmp_path, configuration_repository):
    """Enable the smoke test with mocked task execution, delay, and SMTP."""
    checkout, env, email_path = _prepare_update(tmp_path, configuration_repository)
    with (checkout / "manager/.env").open("a", encoding="utf-8") as manager_env:
        manager_env.write("AUTHORIZED_TOKENS__app=test-app-token\n")
    with (checkout / "runner/.env").open("a", encoding="utf-8") as runner_env:
        runner_env.write("RUNNER_TOKEN=test-token\nMANAGER_URL=http://manager.example.invalid\n")
    for command, content in {
        "uv": '#!/bin/sh\n[ "$RUNNER_API_TOKEN" = "test-app-token" ] || exit 1\n'
        'printf "%s\\n" "$TEST_SMOKE_OUTPUT" >&2\n'
        'exit "$TEST_SMOKE_EXIT_CODE"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }.items():
        path = tmp_path / "bin" / command
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    env.update(
        TEST_SMOKE_OUTPUT="⚠ WARNING: Task submission skipped (encoding): No runners available",
        TEST_SMOKE_EXIT_CODE="2",
    )
    return checkout, env, email_path


@pytest.mark.parametrize(
    "smoke_exit_code,smoke_output,expected_status,expected_exit_code",
    [
        (0, "Encoding check completed", "ok", 0),
        (
            2,
            "⚠ WARNING: Task submission skipped (encoding): No runners available",
            "skipped (No runners available)",
            0,
        ),
        (
            2,
            "Encoding check completed\n"
            "⚠ WARNING: Task submission skipped (transcription): No runners available",
            "skipped (No runners available)",
            0,
        ),
        (1, "HTTP 401: Unauthorized", "failed", 1),
        (1, "Cannot connect to manager", "failed", 1),
        (2, "error: unrecognized arguments", "failed", 1),
        (1, "Task execution failed: No runners available", "failed", 1),
        (
            1,
            "⚠ WARNING: Task submission skipped (encoding): No runners available",
            "failed",
            1,
        ),
    ],
)
def test_update_stack_reports_smoke_result_in_console_and_email(
    smoke_update,
    smoke_exit_code,
    smoke_output,
    expected_status,
    expected_exit_code,
):
    """Skip unavailable capacity while preserving real failures and both email formats."""
    checkout, env, email_path = smoke_update
    env.update(TEST_SMOKE_OUTPUT=smoke_output, TEST_SMOKE_EXIT_CODE=str(smoke_exit_code))

    result = _run_update(checkout, env, run_test=True)

    assert result.returncode == expected_exit_code, result.stdout + result.stderr
    summary = next(line for line in result.stdout.splitlines() if "Post-update test:" in line)
    assert summary.split(":", 1)[1].strip() == expected_status
    assert smoke_output in result.stdout
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    email_status = (
        expected_status if expected_status.startswith("skipped") else expected_status.upper()
    )
    for subtype in ("plain", "html"):
        assert email_status in _email_content(message, subtype)
    if expected_status.startswith("skipped"):
        assert "[WARN] Post-update test skipped (No runners available)." in result.stderr
        assert "Post-update test failed" not in result.stdout + result.stderr
        assert "FAILED" not in str(message)
        assert "SUCCESS" in message["Subject"]
    elif expected_status == "failed":
        assert "Post-update test failed" in result.stderr
        assert "COMPLETED WITH WARNINGS" in message["Subject"]
        assert "No updates" in message["Subject"]


@pytest.mark.parametrize("failure", ["configuration", "mktemp", "tee"])
def test_unavailable_runners_do_not_hide_other_update_failures(
    tmp_path, configuration_repository, smoke_update, failure
):
    """Keep earlier errors and output-capture failures visible in the final result."""
    checkout, env, email_path = smoke_update
    if failure == "configuration":
        _commit_files(configuration_repository, {"runner/.env.example": 'A="invalid\n'})
    else:
        command = tmp_path / "bin" / failure
        content = "cat >/dev/null\n" if failure == "tee" else ""
        command.write_text(f"#!/bin/sh\n{content}exit 1\n", encoding="utf-8")
        command.chmod(0o755)

    result = _run_update(checkout, env, run_test=True)

    assert result.returncode == 1, result.stdout + result.stderr
    expected_status = "skipped (No runners available)" if failure == "configuration" else "failed"
    summary = next(line for line in result.stdout.splitlines() if "Post-update test:" in line)
    assert summary.split(":", 1)[1].strip() == expected_status
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "COMPLETED WITH WARNINGS" in message["Subject"]
    for subtype in ("plain", "html"):
        body = _email_content(message, subtype)
        assert (expected_status if failure == "configuration" else "FAILED") in body
        if failure == "configuration":
            assert "Configuration comparison failed" in body


def test_update_stack_reports_configuration_in_console_and_email(
    tmp_path, configuration_repository
):
    """Exercise Git update, detection, and both email formats without real SMTP."""
    repository = configuration_repository
    _commit_files(
        repository,
        {
            "runner/.env.example": "REMOVED=private-before\n# CHANGED=private-before\n",
            "manager/.env.example": "MANAGER_VALUE=1\n",
            "manager/app/core/config.py": "LIMIT = 1\n",
        },
    )
    checkout, env, email_path = _prepare_update(tmp_path, repository)
    original_env = (checkout / "runner/.env").read_bytes()
    _commit_files(
        repository,
        {
            "runner/.env.example": "# CHANGED=private-after\n# ADDED=private-after\n",
            "manager/.env.example": "MANAGER_VALUE=2\n",
            "manager/app/core/config.py": "LIMIT = 2\n",
        },
    )

    result = _run_update(checkout, env)

    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        "Runner configuration:",
        "Parameters added: ADDED",
        "Parameters removed: REMOVED",
        "Example values changed: CHANGED",
        "Manager configuration:",
        "Example values changed: MANAGER_VALUE",
        "Configuration defaults or validation rules changed: manager/app/core/config.py",
    )
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "configuration review required" in message["Subject"]
    for output in (
        result.stdout,
        _email_content(message, "plain"),
        _email_content(message, "html"),
    ):
        for text in expected:
            assert text in output
        for secret in ("private-before", "private-after", "local-private-value"):
            assert secret not in output
    assert (checkout / "runner/.env").read_bytes() == original_env

    # Running again at the same revision must not repeat the configuration alert.
    repeated = _run_update(checkout, env)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    repeated_email = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "configuration review required" not in repeated_email["Subject"]
    assert "Configuration changes to review" not in _email_content(repeated_email, "html")


@pytest.mark.parametrize(
    "changed_file,content",
    [
        ("runner/.env.example", "# New description\n B = '2' # comment\nexport A=1\n"),
        ("runner/app/unrelated.py", "DEFAULT = 2\n"),
    ],
)
def test_update_stack_omits_configuration_alert_for_unrelated_changes(
    tmp_path, configuration_repository, changed_file, content
):
    """Keep regular update emails free of cosmetic or unrelated configuration alerts."""
    repository = configuration_repository
    _commit_files(repository, {"runner/.env.example": "A=1\nB=2\n"})
    checkout, env, email_path = _prepare_update(tmp_path, repository)
    _commit_files(repository, {changed_file: content})

    result = _run_update(checkout, env)

    assert result.returncode == 0, result.stdout + result.stderr
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "configuration review required" not in message["Subject"]
    assert "Configuration changes to review" not in _email_content(message, "plain")
    assert "Configuration changes to review" not in _email_content(message, "html")


@pytest.mark.parametrize("option", ["--skip-git-update", "--dry-run", "--send-test-email"])
def test_update_stack_does_not_compare_configuration_when_update_is_skipped(
    tmp_path, configuration_repository, option
):
    """Keep previews and skipped Git updates from reporting unapplied changes."""
    repository = configuration_repository
    before = _commit_files(repository, {"runner/.env.example": "A=1\n"})
    checkout, env, email_path = _prepare_update(tmp_path, repository)
    _commit_files(repository, {"runner/.env.example": "A=2\n"})

    result = _run_update(checkout, env, option)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert "Configuration changes detected" not in result.stdout
    if option == "--dry-run":
        assert not email_path.exists()
    else:
        message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
        assert "configuration review required" not in message["Subject"]
        assert "No updates" not in message["Subject"]
        for subtype in ("plain", "html"):
            assert "No updates were applied" not in _email_content(message, subtype)


def test_update_stack_logs_configuration_when_email_is_disabled(tmp_path, configuration_repository):
    """Retain the review report for operators who disable email notifications."""
    repository = configuration_repository
    _commit_files(repository, {"runner/.env.example": "A=1\n"})
    checkout, env, email_path = _prepare_update(tmp_path, repository)
    _commit_files(repository, {"runner/.env.example": "A=2\n"})

    result = _run_update(checkout, env, "--skip-email")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Example values changed: A" in result.stdout
    assert not email_path.exists()


@pytest.mark.parametrize(
    "failure",
    ["invalid-example", "invalid-suffix", "python-failure", "git-failure", "revision-failure"],
)
def test_update_stack_reports_comparison_failure_and_continues(
    tmp_path, configuration_repository, failure
):
    """Send an explicit review warning when detection fails, without exposing values."""
    repository = configuration_repository
    _commit_files(repository, {"runner/.env.example": "A=1\n"})
    checkout, env, email_path = _prepare_update(tmp_path, repository)
    content = {
        "invalid-example": 'A="invalid-private-value\n',
        "invalid-suffix": 'A="1" invalid-private-value\n',
    }.get(failure, "A=2\n")
    _commit_files(repository, {"runner/.env.example": content})
    if failure == "python-failure":
        python = tmp_path / "bin/python3"
        python.write_text(
            '#!/bin/sh\nif [ "$#" -eq 5 ]; then exit 1; fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n',
            encoding="utf-8",
        )
    elif failure in ("git-failure", "revision-failure"):
        real_git = shutil.which("git")
        assert real_git is not None
        git = tmp_path / "bin/git"
        condition = (
            '[ "$3" = "show" ]'
            if failure == "git-failure"
            else '[ "$3" = "rev-parse" ] && [ "$4" = "HEAD" ]'
        )
        git.write_text(
            f"#!/bin/sh\nif {condition}; then exit 1; fi\n" f'exec {shlex.quote(real_git)} "$@"\n',
            encoding="utf-8",
        )
        git.chmod(0o755)

    result = _run_update(checkout, env)

    assert result.returncode == 1
    expected = (
        "Configuration comparison unavailable"
        if failure == "revision-failure"
        else "Configuration comparison failed"
    )
    assert expected in result.stdout + result.stderr
    assert "Update runner component" in result.stdout
    message = BytesParser(policy=policy.default).parsebytes(email_path.read_bytes())
    assert "configuration review required" in message["Subject"]
    assert expected in _email_content(message, "plain")
    assert expected in _email_content(message, "html")
    assert "invalid-private-value" not in result.stdout + result.stderr + str(message)


@pytest.mark.parametrize("component", ["manager", "runner"])
def test_update_stack_passes_component_cache_settings_to_make(
    tmp_path: Path, component: str
) -> None:
    repository = tmp_path / "repository"
    component_dir = repository / component
    fake_bin = tmp_path / "bin"
    component_dir.mkdir(parents=True)
    fake_bin.mkdir()

    subprocess.run(["git", "init", "-q", str(repository)], check=True)

    service_user = f"{component}-service"
    cache_dir = f"/srv/{component}/cache"
    uv_cache_dir = f"/mnt/{component}/uv-cache"
    (component_dir / ".env").write_text(
        f"SERVICE_USER={service_user}\nCACHE_DIR={cache_dir}\nUV_CACHE_DIR={uv_cache_dir}\n",
        encoding="utf-8",
    )

    fake_make = fake_bin / "make"
    fake_make.write_text(
        "#!/usr/bin/env bash\n"
        'printf "SERVICE_USER=%s\\n" "${SERVICE_USER:-}"\n'
        'printf "CACHE_DIR=%s\\n" "${CACHE_DIR:-}"\n'
        'printf "UV_CACHE_DIR=%s\\n" "${UV_CACHE_DIR:-}"\n',
        encoding="utf-8",
    )
    fake_make.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env.pop("SERVICE_USER", None)
    env.pop("CACHE_DIR", None)
    env.pop("UV_CACHE_DIR", None)

    result = subprocess.run(
        [
            "bash",
            str(UPDATE_STACK_SCRIPT),
            "--root-dir",
            str(repository),
            f"--{component}-only",
            "--skip-uv-update",
            "--skip-git-update",
            "--no-restart",
            "--skip-test",
            "--skip-email",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SERVICE_USER={service_user}" in result.stdout
    assert f"CACHE_DIR={cache_dir}" in result.stdout
    assert f"UV_CACHE_DIR={uv_cache_dir}" in result.stdout
