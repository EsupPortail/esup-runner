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


def _run_update(checkout, env, *options):
    """Run the real update workflow against a local Git remote and mocked services."""
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
            "--skip-test",
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
