import importlib
import os
import sys
import types

import app.core.config as config_module
import app.managers.process_manager as process_manager_module


def test_run_uvicorn_instance_applies_grouped_task_types_per_instance(monkeypatch):
    cfg = importlib.reload(config_module)
    pm = importlib.reload(process_manager_module)

    captured = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(*args, **kwargs):
        captured["task_types"] = set(cfg.config.RUNNER_TASK_TYPES)
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")

    fake_uvicorn.run = fake_run

    with monkeypatch.context() as m:
        m.setenv("RUNNER_TASK_TYPES", "[10x(encoding,studio),2x(transcription)]")
        m.setenv("RUNNER_INSTANCES", "12")
        m.delenv("RUNNER_INSTANCE_ID", raising=False)
        m.delenv("RUNNER_PORT", raising=False)
        m.delenv("RUNNER_INSTANCE_URL", raising=False)

        cfg.reload_config_from_env()
        assert cfg.config.RUNNER_TASK_TYPES == {"encoding", "studio", "transcription"}

        m.setitem(sys.modules, "uvicorn", fake_uvicorn)
        pm.run_uvicorn_instance(10, 9100)

        assert captured["task_types"] == {"transcription"}
        assert captured["host"] == cfg.config.RUNNER_HOST
        assert captured["port"] == 9100

    # Restore config after monkeypatch context cleanup.
    os.environ.pop("RUNNER_INSTANCE_ID", None)
    os.environ.pop("RUNNER_PORT", None)
    os.environ.pop("RUNNER_INSTANCE_URL", None)
    cfg.reload_config_from_env()
