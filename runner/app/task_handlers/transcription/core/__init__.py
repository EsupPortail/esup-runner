"""Core implementation package for transcription script.

Re-exports the stable entrypoints used by the outer runner script.
Keeps imports centralized so callers do not depend on internal module layout.
"""

from .main_runtime_utils import main
from .runtime_args_utils import parse_args

__all__ = ["main", "parse_args"]
