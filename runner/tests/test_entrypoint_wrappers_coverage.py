"""Coverage-focused tests for thin task entrypoint wrappers."""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


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


def _exercise_core_eviction(entrypoint: ModuleType, monkeypatch, tmp_path) -> None:
    monkeypatch.delitem(sys.modules, "core", raising=False)

    entrypoint._evict_mismatched_core_package()

    assert "core" not in sys.modules

    matching_core = ModuleType("core")
    matching_core.__file__ = str(entrypoint._CORE_DIR / "__init__.py")
    monkeypatch.setitem(sys.modules, "core", matching_core)

    entrypoint._evict_mismatched_core_package()

    assert sys.modules["core"] is matching_core

    stale_core = ModuleType("core")
    stale_core.__file__ = str(tmp_path / "other" / "core" / "__init__.py")
    stale_child = ModuleType("core.child")
    monkeypatch.setitem(sys.modules, "core", stale_core)
    monkeypatch.setitem(sys.modules, "core.child", stale_child)

    entrypoint._evict_mismatched_core_package()

    assert "core" not in sys.modules
    assert "core.child" not in sys.modules


def test_task_entrypoints_support_package_import_and_core_eviction(monkeypatch, tmp_path):
    """Validate entrypoint wrappers package import and stale core eviction."""
    module_names = [
        "app.task_handlers.encoding.encoding",
        "app.task_handlers.transcription.transcription",
        "app.task_handlers.studio.studio",
    ]

    for module_name in module_names:
        entrypoint = _assert_package_entrypoint(module_name)
        _exercise_core_eviction(entrypoint, monkeypatch, tmp_path)


def test_studio_entrypoint_supports_direct_script_import(monkeypatch):
    """Validate Studio entrypoint direct script import branch."""
    monkeypatch.delitem(sys.modules, "core", raising=False)
    script_dir = str(Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "studio")
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != script_dir])

    module = _load_entrypoint_from_path(
        "app/task_handlers/studio/studio.py",
        "studio_entrypoint_direct_coverage",
    )

    assert callable(module.main)
    assert callable(module.parse_args)
    assert module.__all__ == ["main", "parse_args"]
