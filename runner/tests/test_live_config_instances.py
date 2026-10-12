"""Resolve shared Live settings and per-instance HLS routes from one environment."""

import pytest

from app.core.config import Config, ConfigValidationError
from app.core.live_config import LiveSettings


@pytest.fixture
def grouped_live(monkeypatch, tmp_path):
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.delenv("RUNNER_PORT", raising=False)
    monkeypatch.delenv("RUNNER_INSTANCES", raising=False)
    monkeypatch.setenv("RUNNER_TASK_TYPES", "[1x(encoding,studio),2x(live)]")
    monkeypatch.setenv("RUNNER_BASE_PORT", "8082")
    monkeypatch.setenv("RUNNER_TOKEN", "test-runner-token")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("ENCODING_TYPE", "CPU")
    monkeypatch.setenv("LIVE_UPSTREAM_RTMP_URL", "rtmp://relay:secret@manager:1935")
    monkeypatch.setenv("LIVE_LOCAL_RTMP_URL", "rtmp://reader:secret@localhost:2935")
    monkeypatch.setenv("LIVE_MEDIAMTX_API_URL", "http://localhost:10997")
    monkeypatch.setenv("LIVE_MEDIAMTX_API_USER", "control")
    monkeypatch.setenv("LIVE_MEDIAMTX_API_PASSWORD", "private-password")
    monkeypatch.setenv("LIVE_PUBLIC_HLS_BASE_URL", "http://runner:{port}/hls")
    monkeypatch.setenv("LIVE_HLS_DIRECTORY", str(tmp_path / "hls" / "instance-{instance_id}"))
    return tmp_path


@pytest.mark.parametrize("instance,port", [(None, 8083), ("0", 8082), ("1", 8083), ("2", 8084)])
@pytest.mark.parametrize("transcoder", ["cpu", "nvenc"])
def test_grouped_hls_resolution_keeps_all_encoding_and_media_settings_shared(
    grouped_live, monkeypatch, instance, port, transcoder
):
    if instance is not None:
        monkeypatch.setenv("RUNNER_INSTANCE_ID", instance)
    monkeypatch.setenv("LIVE_TRANSCODER", transcoder)
    monkeypatch.setenv("LIVE_CPU_FALLBACK", "false")
    monkeypatch.setenv("LIVE_NVDEC", "true")
    cfg = Config()
    cfg.validate_configuration()
    assert cfg.RUNNER_INSTANCES == 3
    assert cfg.LIVE.public_hls_base_url == f"http://runner:{port}/hls"
    assert cfg.LIVE.hls_directory == str(grouped_live / "hls" / f"instance-{instance or '1'}")
    assert cfg.LIVE.mediamtx_api_url == "http://localhost:10997"
    assert cfg.LIVE.mediamtx_api_password == "private-password"
    assert cfg.LIVE.local_rtmp_url == "rtmp://reader:secret@localhost:2935"
    assert cfg.LIVE.upstream_rtmp_url == "rtmp://relay:secret@manager:1935"
    assert cfg.LIVE.transcoder == transcoder
    assert not cfg.LIVE.cpu_fallback
    assert cfg.LIVE.nvdec


@pytest.mark.parametrize("encoding_type", ["CPU", "GPU"])
def test_live_defaults_to_cpu_for_any_vod_encoding_type(grouped_live, monkeypatch, encoding_type):
    monkeypatch.delenv("LIVE_TRANSCODER", raising=False)
    monkeypatch.setenv("ENCODING_TYPE", encoding_type)
    monkeypatch.setenv("GPU_CUDA_PATH", str(grouped_live))
    cfg = Config()
    cfg.validate_configuration()
    assert cfg.LIVE.transcoder == "cpu"
    assert cfg.ENCODING_TYPE == encoding_type


def test_actual_port_updates_url_without_moving_recovery_directory(grouped_live, monkeypatch):
    monkeypatch.setenv("RUNNER_INSTANCE_ID", "2")
    original = Config().LIVE
    monkeypatch.setenv("RUNNER_PORT", "9123")
    moved = Config()
    moved.validate_configuration()
    assert moved.LIVE.public_hls_base_url == "http://runner:9123/hls"
    assert moved.LIVE.hls_directory == original.hls_directory


def test_reverse_proxy_route_uses_global_instance_index(grouped_live, monkeypatch):
    monkeypatch.setenv("RUNNER_INSTANCE_ID", "2")
    monkeypatch.setenv("LIVE_PUBLIC_HLS_BASE_URL", "https://live.example/runner-{instance_id}/hls")
    cfg = Config()
    cfg.validate_configuration()
    assert cfg.LIVE.public_hls_base_url == "https://live.example/runner-2/hls"


@pytest.mark.parametrize("instance", [None, "0", "2"])
def test_ambiguous_hls_url_rejected_before_any_group_starts(grouped_live, monkeypatch, instance):
    if instance is not None:
        monkeypatch.setenv("RUNNER_INSTANCE_ID", instance)
    monkeypatch.setenv("LIVE_PUBLIC_HLS_BASE_URL", "https://live.example/hls")
    with pytest.raises(ConfigValidationError, match="must differ for each Live instance"):
        Config().validate_configuration()


def test_shared_hls_root_remains_valid_with_uuid_session_directories(grouped_live, monkeypatch):
    monkeypatch.setenv("LIVE_HLS_DIRECTORY", str(grouped_live / "hls"))
    cfg = Config()
    cfg.validate_configuration()
    assert cfg.LIVE.hls_directory == str(grouped_live / "hls")


@pytest.mark.parametrize(
    "field,url",
    [
        ("mediamtx_api_url", "http://manager.example"),
        ("upstream_rtmp_url", "rtmp://relay:secret@manager.example"),
        ("local_rtmp_url", "rtmp://reader:secret@runner.example"),
        ("public_hls_base_url", "https://runner.example"),
    ],
)
@pytest.mark.parametrize("port", ["invalid", "65536", "-1"])
def test_live_urls_reject_invalid_ports_without_exposing_credentials(field, url, port):
    settings = LiveSettings(
        upstream_rtmp_url="rtmp://manager.example/live",
        public_hls_base_url="https://runner.example/hls",
    ).model_copy(update={field: f"{url}:{port}"})

    with pytest.raises(ValueError) as error:
        settings.validate_enabled()

    assert str(error.value) == f"LIVE_{field.upper()} has an invalid port"


@pytest.mark.parametrize(
    "url",
    [
        "https://:secret@runner.example/hls",
        "https://@runner.example/hls",
    ],
)
def test_public_hls_url_rejects_empty_username_credentials(url):
    settings = LiveSettings(
        upstream_rtmp_url="rtmp://manager.example/live",
        local_rtmp_url="rtmp://runner.example/live",
        public_hls_base_url=url,
    )
    with pytest.raises(ValueError, match="must not contain credentials"):
        settings.validate_enabled()


@pytest.mark.parametrize(
    "field,value",
    [
        ("public_hls_base_url", "http://runner:{unknown}/hls"),
        ("public_hls_base_url", "http://runner:{port.__class__}/hls"),
        ("public_hls_base_url", "http://runner:{port:04d}/hls"),
        ("public_hls_base_url", "http://runner:{port/hls"),
        ("hls_directory", "/tmp/hls/{port}"),
    ],
)
def test_template_fields_are_explicit_and_do_not_evaluate_expressions(field, value):
    settings = LiveSettings(**{field: value})
    with pytest.raises(ValueError, match=f"LIVE_{field.upper()} only supports placeholders"):
        settings.for_instance(2, 8084)


def test_csv_live_declaration_resolves_multiple_instances(grouped_live, monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_TYPES", "encoding,live")
    monkeypatch.setenv("RUNNER_INSTANCES", "3")
    monkeypatch.setenv("RUNNER_INSTANCE_ID", "2")
    cfg = Config()
    cfg.validate_configuration()
    assert cfg.RUNNER_TASK_TYPES_BY_INSTANCE == [{"encoding", "live"}] * 3
    assert cfg.LIVE.public_hls_base_url == "http://runner:8084/hls"


@pytest.mark.parametrize("value", ["bad", "65536", "-1"])
def test_invalid_actual_port_is_reported(grouped_live, monkeypatch, value):
    monkeypatch.setenv("RUNNER_PORT", value)
    with pytest.raises(ConfigValidationError, match="RUNNER_PORT"):
        Config().validate_configuration()


def test_launcher_rejects_ambiguous_hls_routes_before_spawning(grouped_live, monkeypatch):
    import launcher

    monkeypatch.setenv("LIVE_PUBLIC_HLS_BASE_URL", "https://live.example/hls")
    monkeypatch.setattr(launcher, "config", Config())
    monkeypatch.setattr(
        launcher,
        "UvicornProcessManager",
        lambda **kwargs: pytest.fail("Invalid Live routes must be rejected before spawning"),
    )
    with pytest.raises(ConfigValidationError, match="must differ for each Live instance"):
        launcher.main()


@pytest.mark.parametrize(
    "variable,value",
    [
        ("LIVE_TRANSCODER", "invalid"),
        ("LIVE_TRANSCODER", "auto"),
        ("LIVE_HLS_DIRECTORY", "/tmp/hls/{unknown}"),
    ],
)
def test_invalid_shared_settings_are_reported_without_leaking_secrets(
    grouped_live, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigValidationError, match="Invalid LIVE configuration") as error:
        Config().validate_configuration()
    assert "private-password" not in str(error.value)
    assert "relay:secret" not in str(error.value)


@pytest.mark.parametrize(
    "host,expected",
    [("runner.example.org", "runner.example.org"), ("2001:db8::1", "[2001:db8::1]")],
)
def test_minimal_live_uses_installed_ports_and_per_worker_hls(
    grouped_live, monkeypatch, host, expected
):
    for name in (
        "LIVE_MEDIAMTX_API_URL",
        "LIVE_MEDIAMTX_API_USER",
        "LIVE_PUBLIC_HLS_BASE_URL",
        "LIVE_HLS_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RUNNER_HOST", host)
    monkeypatch.setenv("RUNNER_PROTOCOL", "http")
    config = Config()
    config.validate_configuration()
    assert config.LIVE.mediamtx_api_url == "http://127.0.0.1:10997"
    assert config.LIVE.mediamtx_api_user == "control"
    assert config.LIVE.hls_directory == "/dev/shm/hls"
    first = config._live_template.for_instance(1, 8083)
    second = config._live_template.for_instance(2, 9084)
    assert first.public_hls_base_url == f"http://{expected}:8083/hls"
    assert second.public_hls_base_url == f"http://{expected}:9084/hls"


def test_technical_live_settings_are_internal(monkeypatch):
    from app.core.live_config import ENVIRONMENT_FIELDS

    internal = set(LiveSettings.model_fields) - set(ENVIRONMENT_FIELDS)
    assert internal
    for name in internal:
        monkeypatch.setenv("LIVE_" + name.upper(), "invalid-ignored-value")
    settings = LiveSettings.from_environment()
    defaults = LiveSettings()
    for name in internal:
        assert getattr(settings, name) == getattr(defaults, name)


def test_pod_broadcaster_settings_are_loaded_as_a_complete_optional_group():
    settings = LiveSettings(
        pod_url="https://pod.example/base",
        pod_username="api-user",
        pod_password="api-password",
        pod_establishment_name="Université de Montpellier",
        upstream_rtmp_url="rtmp://manager.example/live",
        local_rtmp_url="rtmp://runner.example/live",
        public_hls_base_url="https://runner.example/hls",
        hls_directory="/tmp/hls",
    )
    settings.validate_enabled()

    assert (
        settings.pod_url,
        settings.pod_username,
        settings.pod_password,
        settings.pod_establishment_name,
    ) == (
        "https://pod.example/base",
        "api-user",
        "api-password",
        "Université de Montpellier",
    )


@pytest.mark.parametrize(
    "settings, message",
    [
        (
            LiveSettings(
                pod_url="https://pod.example",
                upstream_rtmp_url="rtmp://manager.example/live",
                local_rtmp_url="rtmp://runner.example/live",
                public_hls_base_url="https://runner.example/hls",
                hls_directory="/tmp/hls",
            ),
            "configured together",
        ),
        (
            LiveSettings(
                pod_url="https://user:password@pod.example",
                pod_username="u",
                pod_password="p",
                upstream_rtmp_url="rtmp://manager.example/live",
                local_rtmp_url="rtmp://runner.example/live",
                public_hls_base_url="https://runner.example/hls",
                hls_directory="/tmp/hls",
            ),
            "credentials",
        ),
        (
            LiveSettings(
                pod_url="https://pod.example?token=secret",
                pod_username="u",
                pod_password="p",
                upstream_rtmp_url="rtmp://manager.example/live",
                local_rtmp_url="rtmp://runner.example/live",
                public_hls_base_url="https://runner.example/hls",
                hls_directory="/tmp/hls",
            ),
            "without credentials",
        ),
        (
            LiveSettings(
                pod_url="https://pod.example:invalid",
                pod_username="u",
                pod_password="p",
                upstream_rtmp_url="rtmp://manager.example/live",
                local_rtmp_url="rtmp://runner.example/live",
                public_hls_base_url="https://runner.example/hls",
                hls_directory="/tmp/hls",
            ),
            "invalid port",
        ),
    ],
)
def test_pod_broadcaster_settings_reject_incomplete_or_unsafe_urls(settings, message):
    with pytest.raises(ValueError, match=message):
        settings.validate_enabled()


def test_operator_can_still_select_custom_renditions(grouped_live, monkeypatch):
    import json

    variants = [item.model_dump() for item in LiveSettings().variants[:2]]
    variants[0]["video_bitrate"] = 2500000
    monkeypatch.setenv("LIVE_VARIANTS", json.dumps(variants))
    config = Config()
    config.validate_configuration()
    assert len(config.LIVE.variants) == 2
    assert config.LIVE.variants[0].video_bitrate == 2500000
