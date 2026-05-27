"""Default runtime wiring for the top-level encoding flow.

Builds the concrete dependency graph consumed by `run_main_flow`.
Binds orchestration hooks to runtime implementations from sibling modules.
Keeps entrypoint startup minimal by moving wiring concerns into one place.
"""

from __future__ import annotations

from typing import cast

from . import main_orchestration_utils, runtime_flow_utils
from .runtime_args_utils import parse_args


def build_main_flow_context() -> main_orchestration_utils.MainFlowContext:
    """Build default dependencies for the end-to-end encoding flow."""
    return main_orchestration_utils.MainFlowContext(
        apply_cli_config_fn=runtime_flow_utils._apply_cli_config,
        process_encoding_fn=runtime_flow_utils._process_encoding,
        add_info_video_fn=runtime_flow_utils.add_info_video,
        encode_log_fn=runtime_flow_utils.encode_log,
        encoding_validation_error_type=runtime_flow_utils.EncodingValidationError,
    )


def main() -> int:
    """Run the encoding script end to end and return an exit code."""
    return cast(
        int,
        main_orchestration_utils.run_main_flow(
            parse_args(),
            context=build_main_flow_context(),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
