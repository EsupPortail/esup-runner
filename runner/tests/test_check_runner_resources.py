"""Validates memory reading, GPU VRAM requirements, and task configuration evaluation."""

import builtins
import io
from types import SimpleNamespace

import pytest

from app.core.config import _parse_grouped_task_types_spec, _parse_task_types_csv
from app.core.live_config import LiveSettings
from scripts import check_runner_resources as crr


def _task_sets(*, encoding: int, transcription: int):
    task_sets = [set(["encoding", "studio"]) for _ in range(encoding)]
    task_sets.extend(set(["transcription"]) for _ in range(transcription))
    return task_sets


def test_read_meminfo_gb_from_linux_proc(monkeypatch):
    """Validate Read meminfo gb from linux proc."""
    mem_kb = 32 * 1024 * 1024  # 32 GiB in kB
    fake_meminfo = f"MemTotal:       {mem_kb} kB\nMemFree:        1234 kB\n"

    def fake_open(path, mode="r", encoding=None):
        assert path == "/proc/meminfo"
        return io.StringIO(fake_meminfo)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(crr, "_run", lambda *_args, **_kwargs: (127, ""))

    assert crr._read_meminfo_gb() == 32.0


def test_read_meminfo_gb_uses_macos_sysctl_fallback(monkeypatch):
    """Validate Read meminfo gb uses macos sysctl fallback."""

    def failing_open(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(builtins, "open", failing_open)
    monkeypatch.setattr(crr, "_run", lambda *_args, **_kwargs: (0, "17179869184\n"))  # 16 GiB

    assert crr._read_meminfo_gb() == 16.0


def test_gpu_known_models_ignore_vram_formula_and_use_model_caps():
    """Validate Gpu known models ignore vram formula and use model caps."""
    resources = crr.ResourceInfo(
        vcpu=64,
        ram_gb=188.6,
        gpus=[
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
        ],
    )
    task_sets = _task_sets(encoding=14, transcription=4)
    counts = crr._get_task_counts(task_sets)
    recommended = crr._recommend_for_gpus(resources.gpus)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=recommended,
        )
    )

    assert recommended.encoding_instances == 14
    assert recommended.transcription_instances == 4
    assert required_vram == 56.0
    assert gpu_ok is True
    assert config_ok is True


def test_gpu_known_models_reject_over_model_cap_even_if_vram_is_enough():
    """Validate Gpu known models reject over model cap even if vram is enough."""
    resources = crr.ResourceInfo(
        vcpu=64,
        ram_gb=188.6,
        gpus=[
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
        ],
    )
    task_sets = _task_sets(encoding=15, transcription=0)
    counts = crr._get_task_counts(task_sets)
    recommended = crr._recommend_for_gpus(resources.gpus)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=recommended,
        )
    )

    assert required_vram == 30.0  # <= 45.0 total VRAM, so VRAM alone would pass.
    assert recommended.encoding_instances == 14
    assert gpu_ok is False
    assert config_ok is False


def test_gpu_unknown_models_keep_vram_based_validation():
    """Validate Gpu unknown models keep vram based validation."""
    resources = crr.ResourceInfo(
        vcpu=32,
        ram_gb=64.0,
        gpus=[crr.GPUInfo(name="Unknown GPU", vram_gb=10.0)],
    )
    task_sets = _task_sets(encoding=6, transcription=0)
    counts = crr._get_task_counts(task_sets)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=crr.Recommendation(
                encoding_instances=999,
                transcription_instances=999,
                total_instances=1998,
                runner_task_types="",
                rationale="",
            ),
        )
    )

    assert required_vram == 12.0
    assert gpu_ok is False
    assert config_ok is False


def _evaluate_live(task_sets, resources, *, live=None, gpu_mode=False):
    """Evaluate a task layout using the detected GPU capacity as its VOD baseline."""
    return crr._evaluate_configuration(
        resources=resources,
        counts=crr._get_task_counts(task_sets),
        task_sets=task_sets,
        gpu_mode=gpu_mode,
        recommended=crr._recommend_for_gpus(resources.gpus),
        live=live,
    )


def test_live_instances_are_counted_in_dedicated_and_mixed_groups():
    counts = crr._get_task_counts(
        [{"live"}, {"encoding", "studio", "transcription", "live"}, {"encoding"}]
    )
    assert counts.total_instances == 3
    assert counts.live_instances == 2
    assert counts.encoding_instances == 2
    assert counts.transcription_instances == 1


@pytest.mark.parametrize(
    "task_sets,live,gpu_mode,expected",
    [
        ([{"live"}, {"live"}], crr.LiveBudget(viewers=4000), False, (6, 4.0, 0.0)),
        (
            [{"encoding", "studio", "transcription", "live"}],
            crr.LiveBudget(viewers=2000),
            False,
            (9, 10.0, 0.0),
        ),
        (
            [{"encoding"}, {"live"}],
            crr.LiveBudget(transcoder="nvenc", cpu_fallback=False),
            False,
            (9, 4.0, 1.0),
        ),
        (
            [{"encoding", "live"}],
            crr.LiveBudget(transcoder="nvenc", cpu_fallback=False),
            True,
            (1, 2.0, 2.0),
        ),
        (
            [{"live"}],
            crr.LiveBudget(transcoder="nvenc", cpu_fallback=True),
            True,
            (2, 2.0, 1.0),
        ),
        ([{"live"}], crr.LiveBudget(variant_count=6), False, (4, 4.0, 0.0)),
        (
            [{"live"}],
            crr.LiveBudget(transcoder="nvenc", cpu_fallback=False, variant_count=4),
            False,
            (2, 8 / 3, 4 / 3),
        ),
        ([{"live"}], crr.LiveBudget(variant_count=2), False, (2, 2.0, 0.0)),
        ([{"encoding"}], crr.LiveBudget(viewers=4000), False, (8, 2.0, 0.0)),
    ],
)
def test_live_requirements_use_instance_maxima_and_add_viewers_once(
    task_sets, live, gpu_mode, expected
):
    assert crr._required_resources(task_sets, live=live, gpu_mode=gpu_mode) == expected


@pytest.mark.parametrize(
    "viewers,expected_vcpu", [(0, 0), (1, 1), (1999, 1), (2000, 1), (2001, 2), (4000, 2)]
)
def test_viewer_delivery_rounds_up_per_host(viewers, expected_vcpu):
    assert crr.LiveBudget(viewers=viewers).viewer_vcpu() == expected_vcpu


@pytest.mark.parametrize("vcpu,ram", [(1, 4.0), (4, 1.0)])
def test_cpu_live_rejects_insufficient_cpu_or_ram(vcpu, ram):
    result = _evaluate_live([{"live"}], crr.ResourceInfo(vcpu, ram, []))
    assert result[0] is False
    assert result[-1] is False


def test_cpu_live_does_not_require_a_gpu_when_encoding_type_is_gpu():
    result = _evaluate_live([{"live"}], crr.ResourceInfo(2, 2.0, []), gpu_mode=True)
    assert result[0:3] == (True, True, True)
    assert result[-1] is True


def test_nvenc_live_requires_a_gpu_even_when_encoding_type_is_cpu():
    result = _evaluate_live(
        [{"live"}], crr.ResourceInfo(8, 8.0, []), live=crr.LiveBudget(transcoder="nvenc")
    )
    assert result[0] is True
    assert result[1] is False
    assert result[-1] is False


@pytest.mark.parametrize("gpu_mode", [False, True])
@pytest.mark.parametrize("fallback,expected", [(False, True), (True, False)])
def test_nvenc_live_reserves_cpu_fallback_independently_of_vod(gpu_mode, fallback, expected):
    resources = crr.ResourceInfo(1, 4.0, [crr.GPUInfo("Tesla T4", 16.0)])
    result = _evaluate_live(
        [{"live"}],
        resources,
        live=crr.LiveBudget(transcoder="nvenc", cpu_fallback=fallback),
        gpu_mode=gpu_mode,
    )
    assert result[0] is expected
    assert result[1] is True
    assert result[-1] is expected


@pytest.mark.parametrize("vcpu,ram", [(2, 8.0), (8, 3.0)])
def test_gpu_vod_does_not_bypass_live_host_resource_checks(vcpu, ram):
    resources = crr.ResourceInfo(vcpu, ram, [crr.GPUInfo("NVIDIA L4", 24.0)])
    result = _evaluate_live(
        [{"encoding"}, {"live"}],
        resources,
        live=crr.LiveBudget(viewers=2000),
        gpu_mode=True,
    )
    assert result[0] is False
    assert result[1] is True
    assert result[-1] is False


@pytest.mark.parametrize("sessions,expected", [(2, True), (3, False)])
def test_nvenc_counts_each_rendition_against_known_model_capacity(sessions, expected):
    resources = crr.ResourceInfo(32, 64.0, [crr.GPUInfo("NVIDIA L4", 24.0)])
    result = _evaluate_live(
        [{"live"}] * sessions, resources, live=crr.LiveBudget(transcoder="nvenc")
    )
    assert result[1] is expected
    assert result[-1] is expected


@pytest.mark.parametrize("extra_encodings,expected", [(1, True), (2, False)])
def test_nvenc_and_vod_share_encoding_capacity_without_double_counting_mixed_instance(
    extra_encodings, expected
):
    resources = crr.ResourceInfo(16, 32.0, [crr.GPUInfo("Tesla T4", 16.0)])
    result = _evaluate_live(
        [{"encoding", "live"}] + [{"encoding"}] * extra_encodings,
        resources,
        live=crr.LiveBudget(transcoder="nvenc"),
        gpu_mode=True,
    )
    assert result[1] is expected
    assert result[-1] is expected


@pytest.mark.parametrize("device,expected", [(-1, False), (0, False), (1, True), (2, False)])
def test_live_vram_must_fit_the_configured_device_not_the_gpu_pool(device, expected):
    resources = crr.ResourceInfo(
        16, 32.0, [crr.GPUInfo("Unknown small GPU", 0.5), crr.GPUInfo("Unknown GPU", 16.0)]
    )
    result = _evaluate_live(
        [{"live"}], resources, live=crr.LiveBudget(transcoder="nvenc", gpu_device=device)
    )
    assert result[1] is expected
    assert result[-1] is expected


def test_live_nvenc_capacity_is_not_pooled_across_known_gpus():
    resources = crr.ResourceInfo(16, 32.0, [crr.GPUInfo("Tesla T4", 16.0)] * 2)
    result = _evaluate_live(
        [{"live"}] * 2, resources, live=crr.LiveBudget(transcoder="nvenc", gpu_device=1)
    )
    assert result[1] is False  # Six encoders target one T4, whose planning budget is four.
    assert result[-1] is False


def test_live_combined_vram_is_checked_even_with_a_known_gpu():
    resources = crr.ResourceInfo(16, 32.0, [crr.GPUInfo("Tesla T4", 2.5)])
    result = _evaluate_live(
        [{"encoding"}, {"live"}],
        resources,
        live=crr.LiveBudget(transcoder="nvenc"),
        gpu_mode=True,
    )
    assert result[1] is False  # Model budget fits, but the combined 3 GB VRAM does not.
    assert result[-1] is False


@pytest.mark.parametrize("viewers,expected", [(0, 4), (4000, 3), (20000, 0)])
def test_live_only_recommendation_preserves_live_and_reserves_viewers(viewers, expected):
    recommended = crr._recommend_with_live(
        crr.ResourceInfo(8, 8.0, []), [{"live"}], False, crr.LiveBudget(viewers=viewers)
    )
    assert recommended.live_instances == expected
    assert recommended.total_instances == expected
    assert recommended.encoding_instances == 0
    assert recommended.transcription_instances == 0
    assert recommended.runner_task_types == (f"[{expected}x(live)]" if expected else "")


def test_nvenc_recommendation_obeys_target_device_rendition_capacity():
    resources = crr.ResourceInfo(32, 64.0, [crr.GPUInfo("NVIDIA L4", 24.0)] * 2)
    live = crr.LiveBudget(transcoder="nvenc", gpu_device=1)
    recommended = crr._recommend_with_live(resources, [{"live"}], False, live)
    assert recommended.live_instances == 2
    assert recommended.runner_task_types == "[2x(live)]"


def test_mixed_recommendation_keeps_groups_and_handles_transcription_first():
    resources = crr.ResourceInfo(32, 32.0, [])
    configured = [{"transcription"}, {"encoding", "studio", "live"}, {"live"}]
    live = crr.LiveBudget(viewers=2000)
    recommended = crr._recommend_with_live(resources, configured, False, live)
    suggested = _parse_grouped_task_types_spec(recommended.runner_task_types)
    assert suggested is not None
    assert {frozenset(types) for types in suggested} == {frozenset(types) for types in configured}
    assert recommended.transcription_instances > 0
    assert recommended.live_instances > 0
    assert recommended.total_instances == len(suggested)
    assert _evaluate_live(suggested, resources, live=live)[-1] is True


@pytest.mark.parametrize(
    "live,gpus,expected_types",
    [
        (crr.LiveBudget(), [], "encoding,studio,transcription,live"),
        (crr.LiveBudget(viewers=1), [], "encoding,studio,transcription"),
        (crr.LiveBudget(variant_count=16), [], "encoding,studio,transcription"),
        (crr.LiveBudget(transcoder="nvenc"), [], "encoding,studio,transcription"),
        (
            crr.LiveBudget(transcoder="nvenc"),
            [crr.GPUInfo("NVIDIA L4", 24.0)],
            "encoding,studio,transcription,live",
        ),
        (
            crr.LiveBudget(transcoder="nvenc"),
            [crr.GPUInfo("Unknown GPU", 0.5)],
            "encoding,studio,transcription",
        ),
    ],
)
def test_live_can_share_the_only_vod_instance_when_a_dedicated_instance_cannot_fit(
    live, gpus, expected_types
):
    resources = crr.ResourceInfo(8, 15.6, gpus)
    configured = [{"encoding", "studio", "transcription"}] * 2 + [{"live"}]
    initial = [set(types) for types in configured]
    recommended = crr._recommend_with_live(resources, configured, False, live)
    assert recommended.runner_task_types == f"[1x({expected_types})]"
    assert recommended.total_instances == 1
    assert recommended.live_instances == int("live" in expected_types.split(","))
    assert configured == initial
    suggested = _parse_grouped_task_types_spec(recommended.runner_task_types)
    assert _evaluate_live(suggested, resources, live=live)[-1] is True


def test_mixed_live_fallback_tries_each_retained_group_until_the_ram_budget_fits():
    resources = crr.ResourceInfo(16, 12.0, [])
    configured = [{"encoding", "studio"}, {"transcription"}, {"live"}]
    live = crr.LiveBudget(variant_count=6)
    recommended = crr._recommend_with_live(resources, configured, False, live)
    assert recommended.runner_task_types == "[1x(encoding,studio),1x(transcription,live)]"
    assert recommended.total_instances == 2
    assert recommended.live_instances == 1
    suggested = _parse_grouped_task_types_spec(recommended.runner_task_types)
    assert _evaluate_live(suggested, resources, live=live)[-1] is True


def test_mixed_live_fallback_does_not_enable_live_when_it_was_not_configured():
    recommended = crr._recommend_with_live(
        crr.ResourceInfo(8, 10.0, []),
        [{"encoding", "studio", "transcription"}],
        False,
        crr.LiveBudget(),
    )
    assert recommended.runner_task_types == "[1x(encoding,studio,transcription)]"
    assert recommended.live_instances == 0


def test_main_recommends_one_mixed_instance_including_live_on_the_test_host(monkeypatch, capsys):
    cfg = SimpleNamespace(
        RUNNER_INSTANCES=3,
        ENCODING_TYPE="CPU",
        LIVE=LiveSettings(),
        GPU_HWACCEL_DEVICE=0,
    )
    task_sets = [{"encoding", "studio", "transcription"}] * 2 + [{"live"}]
    monkeypatch.setattr(crr, "_load_config", lambda: (cfg, None, None))
    monkeypatch.setattr(crr, "_get_task_sets", lambda *_: task_sets)
    monkeypatch.setattr(crr, "_get_resources", lambda: crr.ResourceInfo(8, 15.6, []))
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py"])
    assert crr.main() == 2  # The current three-instance configuration still exceeds capacity.
    output = capsys.readouterr().out
    assert "Recommended RUNNER_INSTANCES:\n  1" in output
    assert "Recommended RUNNER_TASK_TYPES:\n  [1x(encoding,studio,transcription,live)]" in output
    assert "Rationale:" not in output


def test_unmodeled_groups_do_not_grow_forever():
    recommended = crr._recommend_with_live(
        crr.ResourceInfo(4, 4.0, []), [set(), {"live"}], False, crr.LiveBudget()
    )
    assert recommended.runner_task_types == "[2x(live)]"


@pytest.mark.parametrize("raw", ["-1", "1.5", "invalid"])
def test_cli_rejects_invalid_viewer_counts(monkeypatch, raw):
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py", "--live-viewers", raw])
    with pytest.raises(SystemExit) as exc:
        crr._parse_args()
    assert exc.value.code == 2


def test_cli_defaults_to_no_viewer_delivery(monkeypatch):
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py"])
    assert crr._parse_args().live_viewers == 0


@pytest.mark.parametrize(
    "encoding_type,transcoder,gpus,viewers,expected_code",
    [
        ("CPU", "cpu", [], 4000, 0),
        ("GPU", "cpu", [], 0, 0),
        ("CPU", "nvenc", [crr.GPUInfo("Tesla T4", 16.0)], 2000, 0),
        ("CPU", "nvenc", [], 0, 2),
        ("GPU", "nvenc", [crr.GPUInfo("Tesla T4", 16.0)], 6000, 2),
    ],
)
def test_main_reports_live_assumptions_and_uses_independent_transcoder(
    monkeypatch, capsys, encoding_type, transcoder, gpus, viewers, expected_code
):
    cfg = SimpleNamespace(
        RUNNER_INSTANCES=1,
        ENCODING_TYPE=encoding_type,
        LIVE=LiveSettings(transcoder=transcoder),
        GPU_HWACCEL_DEVICE=0,
    )
    monkeypatch.setattr(crr, "_load_config", lambda: (cfg, None, None))
    monkeypatch.setattr(crr, "_get_task_sets", lambda *_: [{"live"}])
    monkeypatch.setattr(crr, "_get_resources", lambda: crr.ResourceInfo(4, 8.0, gpus))
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py", "--live-viewers", str(viewers)])
    assert crr.main() == expected_code
    output = capsys.readouterr().out
    assert "Live-capable instances: 1" in output
    assert f"LIVE_TRANSCODER: {transcoder}" in output
    assert f"Total concurrent viewers on this host: {viewers}" in output
    assert "Live network impact:" in output
    assert "Per viewer (one rendition, video + audio): 0.344 - 1.228 Mbps" in output
    assert ("GPU/VRAM adequate:" in output) is (transcoder == "nvenc")
    if viewers == 0:
        assert "Viewer delivery is excluded" in output
    if transcoder == "nvenc":
        assert "NVENC sessions per Live: 3" in output
        assert "CPU fallback reserved: YES" in output
    if not expected_code:
        assert "x(live)" in output
    elif not gpus:
        assert "No feasible configuration" in output


@pytest.mark.parametrize("gpu_mode", [False, True])
@pytest.mark.parametrize("credentials_present", [False, True])
@pytest.mark.parametrize(
    "task_spec", ["encoding,studio,transcription", "[1x(encoding,studio,transcription)]"]
)
def test_main_without_live_keeps_existing_recommendation_path(
    monkeypatch, capsys, gpu_mode, credentials_present, task_spec
):
    for name, value in (
        ("LIVE_MEDIAMTX_API_PASSWORD", "unused-test-password"),
        ("LIVE_UPSTREAM_RTMP_URL", "rtmp://unused-manager:1935"),
    ):
        if credentials_present:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    cfg = SimpleNamespace(
        RUNNER_INSTANCES=1,
        ENCODING_TYPE="GPU" if gpu_mode else "CPU",
        GPU_HWACCEL_DEVICE=0,
        LIVE=LiveSettings(
            mediamtx_api_password="unused-test-password" if credentials_present else "",
            upstream_rtmp_url="rtmp://unused-manager:1935" if credentials_present else "",
        ),
    )
    monkeypatch.setenv("RUNNER_TASK_TYPES", task_spec)
    monkeypatch.setenv("RUNNER_INSTANCES", "1")
    monkeypatch.setattr(
        crr, "_load_config", lambda: (cfg, _parse_grouped_task_types_spec, _parse_task_types_csv)
    )
    monkeypatch.setattr(crr, "_get_resources", lambda: crr.ResourceInfo(16, 32.0, []))
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py", "--live-viewers", "4000"])
    assert crr.main() == (2 if gpu_mode else 0)
    output = capsys.readouterr().out
    assert "Live planning assumptions:" not in output
    assert "Live network impact:" not in output
    assert "Viewer delivery:" not in output
    assert "live" not in output.lower()
    assert "encoding,studio" in output


def test_live_report_prints_disabled_fallback_and_no_feasible_budget(capsys):
    crr._print_live_budget(crr.LiveBudget(transcoder="nvenc", cpu_fallback=False))
    recommended = crr._recommend_with_live(
        crr.ResourceInfo(1, 1.0, []), [{"live"}], False, crr.LiveBudget()
    )
    crr._print_recommendation(recommended=recommended, below_recommendation=False)
    output = capsys.readouterr().out
    assert "CPU fallback reserved: NO" in output
    assert "No feasible configuration" in output
    assert "Recommended RUNNER_INSTANCES:" not in output


@pytest.mark.parametrize(
    "viewers,expected_total",
    [
        (0, "Total outbound bandwidth: not estimated; set --live-viewers."),
        (1, "Outbound payload estimate for 1 viewers: 0.3 - 1.2 Mbps"),
        (4000, "Outbound payload estimate for 4000 viewers: 1376.0 - 4912.0 Mbps"),
    ],
)
def test_network_report_counts_one_video_audio_rendition_per_viewer(
    capsys, viewers, expected_total
):
    crr._print_live_network_impact(LiveSettings(), viewers)
    output = capsys.readouterr().out
    assert "Each concurrent viewer consumes outbound bandwidth on the server serving HLS" in output
    assert "Per viewer (one rendition, video + audio): 0.344 - 1.228 Mbps" in output
    assert expected_total in output
    assert "HLS/HTTP/TLS overhead and traffic peaks" in output
    assert "network link capacity is not measured or validated" in output


@pytest.mark.parametrize("transcoder", ["cpu", "nvenc"])
def test_main_network_report_uses_configured_bitrates_and_host_wide_viewers(
    monkeypatch, capsys, transcoder
):
    cfg = SimpleNamespace(
        RUNNER_INSTANCES=3,
        ENCODING_TYPE="CPU",
        GPU_HWACCEL_DEVICE=0,
        LIVE=LiveSettings(
            transcoder=transcoder,
            variants=[
                dict(
                    name="high", width=1280, height=720, video_bitrate=4000000, audio_bitrate=192000
                ),
                dict(name="low", width=640, height=360, video_bitrate=1000000, audio_bitrate=96000),
            ],
        ),
    )
    gpus = [crr.GPUInfo("NVIDIA L40S", 48.0)] if transcoder == "nvenc" else []
    monkeypatch.setattr(crr, "_load_config", lambda: (cfg, None, None))
    monkeypatch.setattr(crr, "_get_task_sets", lambda *_: [{"live"}] * 3)
    monkeypatch.setattr(crr, "_get_resources", lambda: crr.ResourceInfo(16, 16.0, gpus))
    monkeypatch.setattr("sys.argv", ["check_runner_resources.py", "--live-viewers", "1000"])
    assert crr.main() == 0
    output = capsys.readouterr().out
    assert "Per viewer (one rendition, video + audio): 1.096 - 4.192 Mbps" in output
    assert "Outbound payload estimate for 1000 viewers: 1096.0 - 4192.0 Mbps" in output
