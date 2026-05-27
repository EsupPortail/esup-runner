"""Validates encoding core module loading and main orchestration flow handling."""

import importlib
import sys
import types
from pathlib import Path

import pytest


def _core_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "encoding" / "core"


def _load_core_module(module_name: str, *, force_insert_branch: bool = False):
    core_dir = str(_core_dir())
    if force_insert_branch:
        while core_dir in sys.path:
            sys.path.remove(core_dir)
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def test_main_runtime_utils_loads_core_relative_modules(monkeypatch):
    """Validate Main runtime utils loads core relative modules."""
    transcription_core_dir = (
        Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "transcription" / "core"
    )
    wrong_runtime_args_path = transcription_core_dir / "runtime_args_utils.py"
    wrong_main_orchestration_path = transcription_core_dir / "main_orchestration_utils.py"

    fake_runtime_args_module = types.ModuleType("runtime_args_utils")
    fake_runtime_args_module.__file__ = str(wrong_runtime_args_path)
    monkeypatch.setitem(sys.modules, "runtime_args_utils", fake_runtime_args_module)

    fake_main_orchestration_module = types.ModuleType("main_orchestration_utils")
    fake_main_orchestration_module.__file__ = str(wrong_main_orchestration_path)
    monkeypatch.setitem(sys.modules, "main_orchestration_utils", fake_main_orchestration_module)

    _load_core_module("main_runtime_utils", force_insert_branch=True)

    loaded_runtime_args = sys.modules.get("app.task_handlers.encoding.core.runtime_args_utils")
    assert loaded_runtime_args is not None
    assert Path(getattr(loaded_runtime_args, "__file__", "")).resolve().parent == _core_dir()

    loaded_main_orchestration = sys.modules.get(
        "app.task_handlers.encoding.core.main_orchestration_utils"
    )
    assert loaded_main_orchestration is not None
    assert Path(getattr(loaded_main_orchestration, "__file__", "")).resolve().parent == _core_dir()


def test_main_orchestration_run_main_flow_success():
    """Validate Main orchestration run main flow success."""
    main_orchestration_utils = _load_core_module("main_orchestration_utils")

    calls = {"apply": 0, "process": 0, "add_info": 0, "log": 0}

    def _apply_cli_config(_args):
        calls["apply"] += 1
        return "configured\n"

    def _process_encoding(_args):
        calls["process"] += 1
        return "processed\n"

    def _add_info_video(_key, _value):
        calls["add_info"] += 1

    def _encode_log(_msg):
        calls["log"] += 1

    context = main_orchestration_utils.MainFlowContext(
        apply_cli_config_fn=_apply_cli_config,
        process_encoding_fn=_process_encoding,
        add_info_video_fn=_add_info_video,
        encode_log_fn=_encode_log,
        encoding_validation_error_type=RuntimeError,
    )

    rc = main_orchestration_utils.run_main_flow(types.SimpleNamespace(), context=context)

    assert rc == 0
    assert calls["apply"] == 1
    assert calls["process"] == 1
    assert calls["add_info"] == 0
    assert calls["log"] == 1


def test_main_orchestration_run_main_flow_validation_error(capsys):
    """Validate Main orchestration run main flow validation error."""
    main_orchestration_utils = _load_core_module("main_orchestration_utils")

    class _ValidationError(RuntimeError):
        pass

    recorded = {"errors": [], "logs": []}

    def _apply_cli_config(_args):
        return "configured\n"

    def _process_encoding(_args):
        raise _ValidationError("invalid media")

    def _add_info_video(key, value):
        recorded["errors"].append((key, value))

    def _encode_log(msg):
        recorded["logs"].append(msg)

    context = main_orchestration_utils.MainFlowContext(
        apply_cli_config_fn=_apply_cli_config,
        process_encoding_fn=_process_encoding,
        add_info_video_fn=_add_info_video,
        encode_log_fn=_encode_log,
        encoding_validation_error_type=_ValidationError,
    )

    with pytest.raises(SystemExit) as exc:
        main_orchestration_utils.run_main_flow(types.SimpleNamespace(), context=context)

    assert exc.value.code == 1
    assert recorded["errors"] == [("error", "invalid media")]
    assert recorded["logs"]
    assert "invalid media" in capsys.readouterr().err


def test_main_runtime_utils_main_delegates_to_run_main_flow(monkeypatch):
    """Validate Main runtime utils main delegates to run main flow."""
    main_runtime_utils = _load_core_module("main_runtime_utils", force_insert_branch=True)

    monkeypatch.setattr(main_runtime_utils, "parse_args", lambda: "args")
    monkeypatch.setattr(
        main_runtime_utils.main_orchestration_utils,
        "run_main_flow",
        lambda *_a, **_k: 9,
    )

    assert main_runtime_utils.main() == 9
