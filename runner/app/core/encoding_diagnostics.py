"""Keep incomplete-output diagnostics consistent across execution and recovery."""

import json
from pathlib import Path

INCOMPLETE_OUTPUT_PREFIX = "Encoding incomplete:"


def normalize_encoding_warnings(value: object) -> list[str]:
    """Keep only distinct, explicit incomplete-encoding diagnostics."""
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            message
            for message in value
            if isinstance(message, str) and message.startswith(INCOMPLETE_OUTPUT_PREFIX)
        )
    )


def incomplete_output_warnings(text: str) -> list[str]:
    """Extract all incomplete-output diagnostics without interpreting FFmpeg logs."""
    return normalize_encoding_warnings(
        [line.removeprefix("WARNING: ") for line in text.splitlines()]
    )


def read_encoding_warnings(output_dir: Path) -> list[str]:
    """Read incomplete-output warnings recorded by the encoding script."""
    try:
        metadata = json.loads((output_dir / "info_video.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return (
        normalize_encoding_warnings(metadata.get("warnings")) if isinstance(metadata, dict) else []
    )


def incomplete_output_error(text: str) -> str:
    """Extract the first explicit incomplete-output diagnostic, without parsing FFmpeg logs."""
    return next(iter(incomplete_output_warnings(text)), "")


def prepend_encoding_warning(error_message: str | None, script_output: str | None) -> str | None:
    """Put the failure summary before displayed logs while keeping raw logs untouched."""
    if not (error_message or "").startswith(INCOMPLETE_OUTPUT_PREFIX):
        return script_output
    summary = f"WARNING: {error_message}"
    if not script_output:
        return summary
    if script_output.startswith(summary + "\n") or script_output == summary:
        return script_output
    return f"{summary}\n\n{script_output}"
