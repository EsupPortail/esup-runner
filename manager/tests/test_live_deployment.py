"""Validate Live Compose storage and media-log mounts against startup requirements."""

from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_log_mounts_are_distinct_and_manager_logs_are_writable():
    services = yaml.safe_load((ROOT / "deploy/live/compose.yml").read_text())["services"]
    for service in services.values():
        targets = [mount.split(":")[1] for mount in service.get("volumes", [])]
        assert len(targets) == len(set(targets))
    manager = services["manager"]
    mounts = {mount.split(":")[0]: mount.split(":")[1:] for mount in manager["volumes"]}
    assert mounts["manager-logs"] == ["/var/log/esup-runner"]
    assert mounts["manager-media-logs"][-1] == "ro"
    media_file = PurePosixPath(manager["environment"]["LIVE_MEDIAMTX_LOG_FILE"])
    assert media_file.parent == PurePosixPath(mounts["manager-media-logs"][0])
    writer = PurePosixPath(services["manager-media"]["environment"]["MTX_LOGFILE"])
    assert media_file.name == writer.name


def test_compose_keeps_media_control_private_and_hls_ephemeral():
    services = yaml.safe_load((ROOT / "deploy/live/compose.yml").read_text())["services"]
    assert "ports" not in services["runner-media"]
    assert all(port.endswith(":1935") for port in services["manager-media"]["ports"])
    assert services["runner"]["environment"]["LIVE_HLS_DIRECTORY"] == "/dev/shm/hls"
    assert services["runner"]["shm_size"] == "256mb"
    assert services["runner"]["stop_grace_period"] == "45s"
    assert services["manager"]["stop_grace_period"] == "45s"
