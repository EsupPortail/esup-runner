"""Validates manager pipeline smoke-check task payload helpers."""

import scripts.check_pipeline_tasks as pipeline


def test_build_task_request_disables_notify_callback_by_default(monkeypatch):
    """Validate Build task request disables notify callback by default."""
    monkeypatch.delenv("RUNNER_NOTIFY_URL", raising=False)

    request = pipeline._build_task_request(
        "encoding",
        "https://example.com/source.mp4",
        {"rendition": "{}"},
    )

    assert request["notify_url"] == ""


def test_build_task_request_uses_explicit_notify_url(monkeypatch):
    """Validate Build task request uses explicit notify url."""
    monkeypatch.setenv("RUNNER_NOTIFY_URL", " https://callback.example.org/hook ")

    request = pipeline._build_task_request(
        "encoding",
        "https://example.com/source.mp4",
        {"rendition": "{}"},
    )

    assert request["notify_url"] == "https://callback.example.org/hook"


def test_resolve_source_urls_keeps_montpellier_source_first(monkeypatch):
    """Validate Resolve source urls keeps Montpellier media first."""
    monkeypatch.delenv("RUNNER_SOURCE_URL", raising=False)
    monkeypatch.delenv("SOURCE_FILE", raising=False)

    encoding_sources = pipeline._resolve_source_urls(with_transcription_translation=False)
    transcription_sources = pipeline._resolve_source_urls(with_transcription_translation=True)

    assert encoding_sources[0] == pipeline.UMONTPELLIER_TEST_SOURCE_URL
    assert transcription_sources[0] == pipeline.UMONTPELLIER_TEST_SOURCE_URL
    assert pipeline.WIKITONGUES_FRENCH_SOURCE_URL in transcription_sources


def test_resolve_source_urls_keeps_explicit_override_first(monkeypatch):
    """Validate Resolve source urls keeps explicit override first."""
    monkeypatch.setenv("RUNNER_SOURCE_URL", " https://media.example.org/input.mp4 ")

    assert pipeline._resolve_source_urls(with_transcription_translation=True) == [
        "https://media.example.org/input.mp4"
    ]


def test_format_script_output_excerpt_truncates_from_tail(monkeypatch):
    """Validate Format script output excerpt truncates from tail."""
    monkeypatch.setattr(pipeline, "SCRIPT_OUTPUT_EXCERPT_CHARS", 8)

    excerpt = pipeline._format_script_output_excerpt("0123456789abcdef")

    assert "last 8 characters" in excerpt
    assert excerpt.endswith("89abcdef")
