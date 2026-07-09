"""Validates Whisper model cache sizing and disk usage rule evaluation for storage directories."""

from app.core import storage_checks
from scripts import check_runner_storage as crs


def test_resolve_whisper_min_free_gb_known_model():
    """Validate Resolve whisper min free gb known model."""
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("small")
    assert min_free_gb == 1.5
    assert ref == "small"


def test_resolve_whisper_min_free_gb_large_variant():
    """Validate Resolve whisper min free gb large variant."""
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("large-v2")
    assert min_free_gb == 5.0
    assert ref == "large"


def test_resolve_whisper_min_free_gb_unknown_model():
    """Validate Resolve whisper min free gb unknown model."""
    min_free_gb, ref = crs._resolve_whisper_min_free_gb("custom-model")
    assert min_free_gb == 3.0
    assert ref == "unknown"


def test_build_rules_mentions_max_file_age_days():
    """Validate Build rules mentions max file age days."""

    class DummyCfg:
        LOG_DIRECTORY = "/var/log/esup-runner"
        STORAGE_DIR = "/tmp/esup-runner"
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
    """Validate Build rules keeps unitary checks when cache paths are not grouped."""

    class DummyCfg:
        LOG_DIRECTORY = "/var/log/esup-runner"
        STORAGE_DIR = "/tmp/esup-runner"
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
    """Validate Resolve uv cache dir defaults to cache dir."""
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    assert crs._resolve_uv_cache_dir("/home/esup-runner/.cache/esup-runner") == (
        "/home/esup-runner/.cache/esup-runner/uv"
    )


def test_resolve_uv_cache_dir_prefers_uv_cache_dir_env(monkeypatch):
    """Validate Resolve uv cache dir prefers uv cache dir env."""
    monkeypatch.setenv("UV_CACHE_DIR", "/var/cache/custom-uv")

    assert crs._resolve_uv_cache_dir("/tmp/cache-root") == "/var/cache/custom-uv"


def test_collect_disk_usage_payload_includes_output_and_cache_paths(monkeypatch):
    """Validate Collect disk usage payload includes output and cache paths."""

    class DummyCfg:
        STORAGE_DIR = "/tmp/esup-runner"
        CACHE_DIR = "/tmp/cache-root"
        WHISPER_MODELS_DIR = "/tmp/cache-root/whisper-models"
        HUGGINGFACE_MODELS_DIR = "/tmp/cache-root/huggingface"
        UV_CACHE_DIR = "/tmp/cache-root/uv"

    rule = storage_checks.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/esup-runner",
        min_free_gb=15.0,
        description="Generated media workspace",
    )
    status = storage_checks.DirectoryStatus(
        rule=rule,
        exists=True,
        is_dir=True,
        writable=True,
        total_gb=100.0,
        used_gb=10.0,
        free_gb=90.0,
        ok=True,
        detail="OK",
    )

    monkeypatch.setattr(storage_checks, "_build_rules", lambda _cfg: {"STORAGE_DIR": rule})
    monkeypatch.setattr(storage_checks, "_evaluate_rule", lambda _rule: status)

    payload = storage_checks.collect_disk_usage(DummyCfg())

    assert payload["ok"] is True
    assert payload["output_dir_pattern"] == "/tmp/esup-runner/<task_id>/output"
    assert payload["paths"]["whisper_models_dir"] == "/tmp/cache-root/whisper-models"
    assert payload["paths"]["huggingface_models_dir"] == "/tmp/cache-root/huggingface"
    assert payload["directories"]["STORAGE_DIR"]["free_gb"] == 90.0


def test_storage_checks_directory_size_ignores_unreadable_files(monkeypatch, tmp_path):
    """Validate storage check directory sizing ignores files that cannot be stat'ed."""
    monkeypatch.setattr(
        storage_checks.os,
        "walk",
        lambda _path: [(str(tmp_path), [], ["missing.bin"])],
    )

    assert storage_checks._directory_size_bytes(tmp_path) == 0


def test_storage_checks_directory_size_returns_zero_when_walk_fails(monkeypatch, tmp_path):
    """Validate storage check directory sizing returns zero when walking fails."""

    def _raise_walk(_path):
        raise OSError("cannot walk")

    monkeypatch.setattr(storage_checks.os, "walk", _raise_walk)

    assert storage_checks._directory_size_bytes(tmp_path) == 0


def test_storage_checks_find_existing_parent_branches(monkeypatch, tmp_path):
    """Validate storage check existing parent resolution covers fallbacks."""
    assert storage_checks._find_existing_parent(tmp_path) == tmp_path
    assert storage_checks._find_existing_parent(tmp_path / "missing" / "leaf") == tmp_path

    monkeypatch.setattr(storage_checks.Path, "exists", lambda _self: False)

    assert storage_checks._find_existing_parent(storage_checks.Path("/missing")) is None


def test_storage_checks_disk_usage_handles_missing_target_and_oserror(monkeypatch, tmp_path):
    """Validate storage check disk usage handles missing targets and OS errors."""
    monkeypatch.setattr(storage_checks, "_find_existing_parent", lambda _path: None)
    assert storage_checks._disk_usage_for_path(tmp_path / "missing") == (0.0, 0.0, 0.0)

    monkeypatch.setattr(storage_checks, "_find_existing_parent", lambda path: path)

    def _raise_disk_usage(_path):
        raise OSError("disk usage failed")

    monkeypatch.setattr(storage_checks.shutil, "disk_usage", _raise_disk_usage)

    assert storage_checks._disk_usage_for_path(tmp_path) == (0.0, 0.0, 0.0)


def test_storage_checks_disk_usage_returns_filesystem_values(monkeypatch, tmp_path):
    """Validate storage check disk usage returns filesystem values in GB."""

    class Usage:
        total = 10 * 1024**3
        used = 3 * 1024**3
        free = 7 * 1024**3

    monkeypatch.setattr(storage_checks.shutil, "disk_usage", lambda _path: Usage())

    assert storage_checks._disk_usage_for_path(tmp_path) == (10.0, 3.0, 7.0)


def test_storage_checks_build_rules_mentions_unlimited_retention():
    """Validate storage check rules mention unlimited retention when max age is zero."""

    class DummyCfg:
        LOG_DIR = "/var/log/esup-runner"
        STORAGE_DIR = "/tmp/esup-runner"
        CACHE_DIR = "/tmp/cache-root"
        WHISPER_MODEL = "small"
        MAX_FILE_AGE_DAYS = 0

    rules = storage_checks._build_rules(DummyCfg())

    assert "MAX_FILE_AGE_DAYS=0" in rules["STORAGE_DIR"].note


def test_storage_checks_evaluate_rule_reports_required_directory_missing(monkeypatch):
    """Validate storage check evaluate rule reports required missing directories."""
    rule = storage_checks.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/missing-storage",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(storage_checks.Path, "exists", lambda _self: False)
    monkeypatch.setattr(storage_checks.Path, "is_dir", lambda _self: False)
    monkeypatch.setattr(storage_checks, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 20.0))
    monkeypatch.setattr(storage_checks, "_directory_size_bytes", lambda _path: 0)

    status = storage_checks._evaluate_rule(rule)

    assert status.ok is False
    assert status.detail == "Directory does not exist (run 'sudo make init' to create it)."


def test_storage_checks_evaluate_rule_reports_file_instead_of_directory(monkeypatch):
    """Validate storage check evaluate rule reports paths that are not directories."""
    rule = storage_checks.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/storage-file",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(storage_checks.Path, "exists", lambda _self: True)
    monkeypatch.setattr(storage_checks.Path, "is_dir", lambda _self: False)
    monkeypatch.setattr(storage_checks, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 20.0))
    monkeypatch.setattr(storage_checks, "_directory_size_bytes", lambda _path: 0)

    status = storage_checks._evaluate_rule(rule)

    assert status.ok is False
    assert status.detail == "Path exists but is not a directory."


def test_storage_checks_evaluate_rule_reports_unwritable_directory(monkeypatch):
    """Validate storage check evaluate rule reports unwritable directories."""
    rule = storage_checks.DirectoryRule(
        env_key="STORAGE_DIR",
        path="/tmp/storage",
        min_free_gb=15.0,
        description="Generated media workspace",
    )

    monkeypatch.setattr(storage_checks.Path, "exists", lambda _self: True)
    monkeypatch.setattr(storage_checks.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(storage_checks.os, "access", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(storage_checks, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 20.0))
    monkeypatch.setattr(storage_checks, "_directory_size_bytes", lambda _path: 0)

    status = storage_checks._evaluate_rule(rule)

    assert status.ok is False
    assert status.detail == "Directory is not writable by current user."


def test_storage_checks_evaluate_rule_uses_plain_min_free_threshold(monkeypatch):
    """Validate storage check evaluate rule uses plain free-space threshold for logs."""
    rule = storage_checks.DirectoryRule(
        env_key="LOG_DIR",
        path="/tmp/logs",
        min_free_gb=0.5,
        description="Log output directory",
    )

    monkeypatch.setattr(storage_checks.Path, "exists", lambda _self: True)
    monkeypatch.setattr(storage_checks.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(storage_checks.os, "access", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(storage_checks, "_disk_usage_for_path", lambda _path: (100.0, 10.0, 0.25))
    monkeypatch.setattr(storage_checks, "_directory_size_bytes", lambda _path: 10 * 1024**3)

    status = storage_checks._evaluate_rule(rule)

    assert status.ok is False
    assert status.detail == "Insufficient free space for recommended threshold."


def test_evaluate_rule_model_cache_uses_required_minus_used(monkeypatch):
    """Validate Evaluate rule model cache uses required minus used."""
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
    """Validate Evaluate rule storage dir uses required minus used."""
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


def test_evaluate_rule_storage_dir_not_ok_when_required_additional_exceeds_free(
    monkeypatch,
):
    """Validate Evaluate rule storage dir not ok when required additional exceeds free."""
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
    """Validate Evaluate rule storage dir not ok when even free is zero."""
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
    """Validate Evaluate rule uv cache missing directory is ok if parent writable and has space."""
    rule = crs.DirectoryRule(
        env_key="UV_CACHE_DIR",
        path="/home/esup-runner/.cache/esup-runner/uv",
        min_free_gb=5.0,
        description="uv package cache",
        must_exist=False,
    )

    monkeypatch.setattr(
        crs.Path,
        "exists",
        lambda self: str(self) == "/home/esup-runner/.cache/esup-runner",
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
    """Validate Evaluate rule uv cache not ok when parent free space is too low."""
    rule = crs.DirectoryRule(
        env_key="UV_CACHE_DIR",
        path="/home/esup-runner/.cache/esup-runner/uv",
        min_free_gb=5.0,
        description="uv package cache",
        must_exist=False,
    )

    monkeypatch.setattr(
        crs.Path,
        "exists",
        lambda self: str(self) == "/home/esup-runner/.cache/esup-runner",
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
    """Validate Evaluate rule cache dir uses aggregate used space."""
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
