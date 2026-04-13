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
        CACHE_DIR = "/tmp/cache-root"
        WHISPER_MODEL = "turbo"
        MAX_FILE_AGE_DAYS = 3

    rules = crs._build_rules(DummyCfg())
    assert "MAX_FILE_AGE_DAYS=3" in rules["STORAGE_DIR"].note
    assert "CACHE_DIR" in rules
    assert "HUGGINGFACE_MODELS_DIR" not in rules
    assert "WHISPER_MODELS_DIR" not in rules
    assert "UV_CACHE_DIR" not in rules
    assert rules["CACHE_DIR"].path == "/tmp/cache-root"
    assert rules["CACHE_DIR"].min_free_gb == 10.0


def test_build_rules_keeps_unitary_checks_when_cache_paths_are_not_grouped():
    class DummyCfg:
        LOG_DIRECTORY = "/var/log/esup-runner"
        STORAGE_DIR = "/tmp/esup-runner/storage"
        CACHE_DIR = "/tmp/cache-root"
        WHISPER_MODELS_DIR = "/tmp/custom-whisper"
        HUGGINGFACE_MODELS_DIR = "/tmp/custom-hf"
        UV_CACHE_DIR = "/tmp/custom-uv"
        WHISPER_MODEL = "turbo"
        MAX_FILE_AGE_DAYS = 3

    rules = crs._build_rules(DummyCfg())

    assert "CACHE_DIR" not in rules
    assert rules["HUGGINGFACE_MODELS_DIR"].path == "/tmp/custom-hf"
    assert rules["WHISPER_MODELS_DIR"].path == "/tmp/custom-whisper"
    assert rules["UV_CACHE_DIR"].path == "/tmp/custom-uv"


def test_resolve_uv_cache_dir_defaults_to_cache_dir(monkeypatch):
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    assert crs._resolve_uv_cache_dir("/home/esup-runner/.cache/esup-runner") == (
        "/home/esup-runner/.cache/esup-runner/uv"
    )


def test_resolve_uv_cache_dir_prefers_uv_cache_dir_env(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/var/cache/custom-uv")

    assert crs._resolve_uv_cache_dir("/tmp/cache-root") == "/var/cache/custom-uv"


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


def test_evaluate_rule_uv_cache_missing_directory_is_ok_if_parent_writable_and_has_space(
    monkeypatch,
):
    rule = crs.DirectoryRule(
        env_key="UV_CACHE_DIR",
        path="/home/esup-runner/.cache/esup-runner/uv",
        min_free_gb=5.0,
        description="uv package cache",
        must_exist=False,
    )

    monkeypatch.setattr(
        crs.Path, "exists", lambda self: str(self) == "/home/esup-runner/.cache/esup-runner"
    )
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(
        crs,
        "_find_existing_parent",
        lambda _path: crs.Path("/home/esup-runner/.cache/esup-runner"),
    )
    monkeypatch.setattr(
        crs.os,
        "access",
        lambda path, *_args, **_kwargs: str(path) == "/home/esup-runner/.cache/esup-runner",
    )
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 95.0, 5.5))

    status = crs._evaluate_rule(rule)

    assert status.ok is True
    assert status.exists is False
    assert status.writable is True


def test_evaluate_rule_uv_cache_not_ok_when_parent_free_space_is_too_low(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="UV_CACHE_DIR",
        path="/home/esup-runner/.cache/esup-runner/uv",
        min_free_gb=5.0,
        description="uv package cache",
        must_exist=False,
    )

    monkeypatch.setattr(
        crs.Path, "exists", lambda self: str(self) == "/home/esup-runner/.cache/esup-runner"
    )
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(
        crs,
        "_find_existing_parent",
        lambda _path: crs.Path("/home/esup-runner/.cache/esup-runner"),
    )
    monkeypatch.setattr(
        crs.os,
        "access",
        lambda path, *_args, **_kwargs: str(path) == "/home/esup-runner/.cache/esup-runner",
    )
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 96.0, 4.5))

    status = crs._evaluate_rule(rule)

    assert status.ok is False


def test_evaluate_rule_cache_dir_uses_aggregate_used_space(monkeypatch):
    rule = crs.DirectoryRule(
        env_key="CACHE_DIR",
        path="/tmp/cache-root",
        min_free_gb=10.0,
        description="Shared cache root",
        aggregate_paths=(
            "/tmp/cache-root/whisper-models",
            "/tmp/cache-root/huggingface",
            "/tmp/cache-root/uv",
        ),
    )

    monkeypatch.setattr(crs.Path, "exists", lambda _self: True)
    monkeypatch.setattr(crs.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(crs.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(crs, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 2.0))

    used_map = {
        "/tmp/cache-root": int(20.0 * 1024**3),
        "/tmp/cache-root/whisper-models": int(1.5 * 1024**3),
        "/tmp/cache-root/huggingface": int(2.0 * 1024**3),
        "/tmp/cache-root/uv": int(5.0 * 1024**3),
    }
    monkeypatch.setattr(crs, "_directory_size_bytes", lambda path: used_map.get(str(path), 0))

    status = crs._evaluate_rule(rule)

    # Aggregate required additional free = 10.0 - (1.5 + 2.0 + 5.0) = 1.5 GB, filesystem free = 2.0 GB => OK
    assert status.ok is True
    assert status.used_gb == 8.5
