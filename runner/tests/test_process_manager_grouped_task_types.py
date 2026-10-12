"""Validates grouped task type specification application across multiple runner instances."""

import importlib
import os
import sys
import types

import pytest

import app.core.config as config_module
import app.managers.process_manager as process_manager_module


@pytest.mark.parametrize(
    "spec,count,instance,expected",
    [
        ("[10x(encoding,studio),2x(transcription)]", "12", 10, {"transcription"}),
        ("[1x(encoding,studio),1x(live)]", "2", 1, {"live"}),
        ("[1x(encoding,studio),1x(live)]", "2", 0, {"encoding", "studio"}),
        ("[1x(encoding,studio),2x(live)]", "3", 2, {"live"}),
        ("[2x(encoding,live),1x(transcription)]", "3", 1, {"encoding", "live"}),
        (
            "[1x(encoding,studio,transcription,live)]",
            "1",
            0,
            {"encoding", "studio", "transcription", "live"},
        ),
    ],
)
def test_run_uvicorn_instance_applies_grouped_task_types_per_instance(
    monkeypatch, tmp_path, spec, count, instance, expected
):
    """Validate Run uvicorn instance applies grouped task types per instance."""
    cfg = importlib.reload(config_module)
    pm = importlib.reload(process_manager_module)

    captured = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(*args, **kwargs):
        captured["task_types"] = set(cfg.config.RUNNER_TASK_TYPES)
        captured["host"] = kwargs.get("host")
        captured["port"] = kwargs.get("port")
        captured["public_hls_base_url"] = cfg.config.LIVE.public_hls_base_url
        captured["hls_directory"] = cfg.config.LIVE.hls_directory

    fake_uvicorn.run = fake_run

    with monkeypatch.context() as m:
        m.setenv("RUNNER_TASK_TYPES", spec)
        m.setenv("RUNNER_INSTANCES", count)
        m.setenv("LIVE_UPSTREAM_RTMP_URL", "rtmp://manager:1935")
        m.setenv("LIVE_PUBLIC_HLS_BASE_URL", "http://runner:{port}/hls")
        m.setenv("LIVE_HLS_DIRECTORY", str(tmp_path / "hls" / "instance-{instance_id}"))
        m.setenv("DEBUG", "false")
        m.setenv("RUNNER_TOKEN", "test-runner-token")
        m.delenv("RUNNER_INSTANCE_ID", raising=False)
        m.delenv("RUNNER_PORT", raising=False)
        m.delenv("RUNNER_INSTANCE_URL", raising=False)

        cfg.reload_config_from_env()
        assert cfg.config.RUNNER_TASK_TYPES == set().union(
            *cfg.config.RUNNER_TASK_TYPES_BY_INSTANCE
        )

        m.setitem(sys.modules, "uvicorn", fake_uvicorn)
        pm.run_uvicorn_instance(instance, 9100)

        assert captured["task_types"] == expected
        assert captured["host"] == cfg.config.RUNNER_HOST
        assert captured["port"] == 9100
        assert captured["public_hls_base_url"] == "http://runner:9100/hls"
        assert captured["hls_directory"] == str(tmp_path / "hls" / f"instance-{instance}")

    # Restore config after monkeypatch context cleanup.
    os.environ.pop("RUNNER_INSTANCE_ID", None)
    os.environ.pop("RUNNER_PORT", None)
    os.environ.pop("RUNNER_INSTANCE_URL", None)
    with monkeypatch.context() as m:
        m.setenv("DEBUG", "false")
        m.setenv("RUNNER_TOKEN", "test-runner-token")
        cfg.reload_config_from_env()
