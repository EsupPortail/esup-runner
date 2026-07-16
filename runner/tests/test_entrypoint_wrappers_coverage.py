"""Coverage-focused tests for thin task entrypoint wrappers."""

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_entrypoint_from_path(relative_path: str, module_name: str) -> ModuleType:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_package_entrypoint(module_name: str) -> ModuleType:
    module = importlib.reload(importlib.import_module(module_name))
    assert callable(module.main)
    assert callable(module.parse_args)
    assert module.__all__ == ["main", "parse_args"]
    return module


@pytest.mark.parametrize(
    ("handler_name", "main_module"),
    [
        ("encoding", "app.task_handlers.encoding.core.main_runtime_utils"),
        ("studio", "app.task_handlers.studio.core.main_runtime_utils"),
        ("transcription", "app.task_handlers.transcription.core.main_runtime_utils"),
    ],
)
def test_task_entrypoint_supports_normal_package_import(
    monkeypatch, handler_name: str, main_module: str
):
    """Package imports leave unrelated modules and the import path untouched."""
    unrelated_core = ModuleType("core")
    monkeypatch.setitem(sys.modules, "core", unrelated_core)
    original_path = list(sys.path)

    entrypoint = _assert_package_entrypoint(f"app.task_handlers.{handler_name}.{handler_name}")

    assert sys.modules["core"] is unrelated_core
    assert sys.path == original_path
    assert entrypoint.main.__module__ == main_module
    assert entrypoint.parse_args.__module__ == (
        f"app.task_handlers.{handler_name}.core.runtime_args_utils"
    )


@pytest.mark.parametrize("handler_name", ["encoding", "studio", "transcription"])
def test_task_entrypoint_supports_direct_script_import(monkeypatch, handler_name: str):
    """Validate each entrypoint direct-script import branch."""
    runner_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != runner_root])

    module = _load_entrypoint_from_path(
        f"app/task_handlers/{handler_name}/{handler_name}.py",
        f"{handler_name}_entrypoint_direct_coverage",
    )

    assert callable(module.main)
    assert callable(module.parse_args)
    assert module.__all__ == ["main", "parse_args"]
    assert sys.path[0] == runner_root


@pytest.mark.parametrize("handler_name", ["encoding", "studio", "transcription"])
def test_task_entrypoint_supports_direct_script_execution(tmp_path, handler_name: str):
    """Run each script directly from outside the project and exercise its CLI."""
    runner_root = Path(__file__).resolve().parents[1]
    script_path = runner_root / "app" / "task_handlers" / handler_name / f"{handler_name}.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
