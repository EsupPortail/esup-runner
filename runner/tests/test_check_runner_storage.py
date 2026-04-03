from scripts import check_runner_storage as crs


def test_resolve_whisper_min_free_gb_known_model():
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("small")
    assert min_free_gb == 1.5
    assert ref == "small"


def test_resolve_whisper_min_free_gb_large_variant():
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("large-v2")
    assert min_free_gb == 5.0
    assert ref == "large"


def test_resolve_whisper_min_free_gb_unknown_model():
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("custom-model")
    assert min_free_gb == 3.0
    assert ref == "unknown"


def test_build_rules_mentions_max_file_age_days():
    class DummyCfg:
        LOG_DIRECTORY = "/var/log/esup-runner"
        STORAGE_DIR = "/tmp/esup-runner/storage"
        HUGGINGFACE_MODELS_DIR = "/tmp/hf"
        WHISPER_MODELS_DIR = "/tmp/whisper"
        WHISPER_MODEL = "turbo"
        MAX_FILE_AGE_DAYS = 3

    rules = crs._build_rules(DummyCfg())
    assert "MAX_FILE_AGE_DAYS=3" in rules["STORAGE_DIR"].note
    assert rules["HUGGINGFACE_MODELS_DIR"].min_free_gb == 2.0
    assert rules["WHISPER_MODELS_DIR"].min_free_gb == 3.0


def test_evaluate_rule_model_cache_uses_required_minus_used(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="WHISPER_MODELS_DIR",
        path="/tmp/whisper",
        min_free_gb=3.0,
        description="Whisper models cache",
    )

    monkeypatch.setattr(crs.Path, "exists", lambda _self: True)
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(crs.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 2.0))
    monkeypatch.setattr(crs, "_directory_size_bytes", lambda _path: int(3.0 * 1024**3))

    status = crs._evaluate_rule(rule)

    # Required additional free = 3.0 - 3.0 = 0.0 GB, filesystem free = 2.0 GB => OK
    assert status.ok is True


def test_evaluate_rule_storage_dir_uses_required_minus_used(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/storage",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(crs.Path, "exists", lambda _self: True)
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(crs.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 5.0))
    monkeypatch.setattr(crs, "_directory_size_bytes", lambda _path: int(18.0 * 1024**3))

    status = crs._evaluate_rule(rule)

    # Required additional free = 15.0 - 18.0 = 0.0 GB, filesystem free = 5.0 GB => OK
    assert status.ok is True


def test_evaluate_rule_storage_dir_not_ok_when_required_additional_exceeds_free(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/storage",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(crs.Path, "exists", lambda _self: True)
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(crs.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 1.0))
    monkeypatch.setattr(crs, "_directory_size_bytes", lambda _path: int(18.5 * 1024**3))

    status = crs._evaluate_rule(rule)

    # Required additional free = 15.0 - 18.5 = 0.0 GB, filesystem free = 1.0 GB => OK
    assert status.ok is True


def test_evaluate_rule_storage_dir_not_ok_when_even_free_is_zero(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/storage",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(crs.Path, "exists", lambda _self: True)
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(crs.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 0.0))
    monkeypatch.setattr(crs, "_directory_size_bytes", lambda _path: int(12.0 * 1024**3))

    status = crs._evaluate_rule(rule)

    # Required additional free = 15.0 - 12.0 = 3.0 GB, filesystem free = 0.0 GB => NOT OK
    assert status.ok is False
