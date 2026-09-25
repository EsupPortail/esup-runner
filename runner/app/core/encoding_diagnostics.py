"""Keep incomplete-output diagnostics consistent across execution and recovery."""

INCOMPLETE_OUTPUT_PREFIX = "Encoding incomplete:"


def incomplete_output_error(text: str) -> str:
    """Extract the first explicit incomplete-output diagnostic, without parsing FFmpeg logs."""
    for line in text.splitlines():
        message = line.removeprefix("WARNING: ")
        if message.startswith(INCOMPLETE_OUTPUT_PREFIX):
            return message
    return ""


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
