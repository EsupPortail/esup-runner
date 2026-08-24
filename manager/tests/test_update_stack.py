"""Integration tests for the repository-level stack update helper."""

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UPDATE_STACK_SCRIPT = REPOSITORY_ROOT / "update-stack.sh"


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
