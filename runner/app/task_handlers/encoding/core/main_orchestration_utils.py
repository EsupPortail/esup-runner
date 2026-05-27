"""Top-level orchestration helpers for the encoding runtime flow.

Runs the main CLI workflow with dependency-injected callbacks.
Keeps startup/error-handling sequencing deterministic and easy to test.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class MainFlowContext:
    """Dependencies required to run the main encoding flow."""

    apply_cli_config_fn: Callable[[Any], str]
    process_encoding_fn: Callable[[Any], str]
    add_info_video_fn: Callable[[str, Any], None]
    encode_log_fn: Callable[[str], None]
    encoding_validation_error_type: type[Exception]


def run_main_flow(args: Any, *, context: MainFlowContext) -> int:
    """Run the end-to-end encoding workflow for parsed CLI args."""
    msg = "--> Main\n"
    msg += "- Starting encoding cycle: %s\n" % time.ctime()
    msg += context.apply_cli_config_fn(args)

    try:
        msg += context.process_encoding_fn(args)
    except context.encoding_validation_error_type as exc:
        error_message = str(exc)
        msg += f"Error: {error_message}\n"
        context.add_info_video_fn("error", error_message)
        context.encode_log_fn(msg)
        print(error_message, file=sys.stderr)
        raise SystemExit(1)

    context.encode_log_fn(msg)
    return 0
