"""Operator Live configuration and fixed technical defaults."""

import pytest

from app.core.live_config import ENVIRONMENT_FIELDS, LiveSettings


def test_technical_limits_are_not_environment_overrides(monkeypatch):
    for name in set(LiveSettings.model_fields) - set(ENVIRONMENT_FIELDS):
        monkeypatch.setenv("LIVE_" + name.upper(), "invalid-ignored-value")
    monkeypatch.setenv("LIVE_ENABLED", "true")
    monkeypatch.setenv("LIVE_MEDIAMTX_API_PASSWORD", "installation-secret")
    settings = LiveSettings.from_environment()
    settings.validate_enabled()
    assert settings.enabled
    assert settings.mediamtx_api_user == "control"
    assert settings.mediamtx_api_password == "installation-secret"
    assert (
        settings.mediamtx_timeout,
        settings.poll_interval,
        settings.runner_timeout,
    ) == (5, 5, 15)


def test_operator_choices_remain_validated(monkeypatch):
    monkeypatch.setenv("LIVE_AUTO_START", "false")
    monkeypatch.setenv("LIVE_AUTO_ETAB_NAME", "Campus")
    monkeypatch.setenv("LIVE_SOURCES", '{"hall":"live/hall"}')
    settings = LiveSettings.from_environment()
    assert not settings.auto_start
    assert settings.auto_etab_name == "Campus"
    assert settings.sources == {"hall": "live/hall"}
    monkeypatch.setenv("LIVE_ENABLED", "invalid")
    with pytest.raises(ValueError):
        LiveSettings.from_environment()


def test_automatic_establishment_defaults_to_unknown():
    assert LiveSettings().auto_etab_name == "unknown"


@pytest.mark.parametrize(
    "settings, message",
    [
        (
            LiveSettings(enabled=True, sources={"invalid source": "live/key"}),
            "source identifier",
        ),
        (
            LiveSettings(enabled=True, sources={"hall": "live/another-name"}),
            "match broadcaster names",
        ),
        (
            LiveSettings(enabled=True, mediamtx_api_url="https://media.example:invalid"),
            "invalid port",
        ),
        (
            LiveSettings(enabled=True, mediamtx_api_url="https://user:secret@media.example"),
            "credentials",
        ),
    ],
)
def test_live_validation_rejects_invalid_source_and_control_urls(settings, message):
    """Reject unsafe source identifiers and MediaMTX control URLs."""
    with pytest.raises(ValueError, match=message):
        settings.validate_enabled()
