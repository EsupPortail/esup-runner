"""Validate Live Compose storage and media-log mounts against startup requirements."""

from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_media_logs_share_only_their_application_log_volume():
    services = yaml.safe_load((ROOT / "deploy/live/compose.yml").read_text())["services"]
    for service in services.values():
        targets = [mount.split(":")[1] for mount in service.get("volumes", [])]
        assert len(targets) == len(set(targets))
    for role in ("manager", "runner"):
        application, media = services[role], services[f"{role}-media"]
        log_dir = PurePosixPath(application["environment"]["LOG_DIR"])
        mount = f"{role}-logs:{log_dir}"
        assert mount in application["volumes"] and mount in media["volumes"]
        assert media["environment"]["MTX_LOGFILE"] == str(log_dir / f"mediamtx_{role}.log")
        assert media["environment"]["MTX_LOGDESTINATIONS"] == "stdout,file"
        assert media["environment"]["MTX_LOGLEVEL"] == "info"
        assert media["environment"]["MTX_LOGSTRUCTURED"] == "false"
        assert media["volumes"] == [f"./{role}-mediamtx.yml:/mediamtx.yml:ro", mount]


def test_compose_keeps_media_control_private_and_hls_ephemeral():
    services = yaml.safe_load((ROOT / "deploy/live/compose.yml").read_text())["services"]
    assert "ports" not in services["runner-media"]
    assert all(port.endswith(":1935") for port in services["manager-media"]["ports"])
    assert services["runner"]["environment"]["LIVE_HLS_DIRECTORY"] == "/dev/shm/hls"
    assert services["runner"]["shm_size"] == "256mb"
    assert services["runner"]["stop_grace_period"] == "45s"
    assert services["manager"]["stop_grace_period"] == "45s"
