"""Helpers for publishing task result manifests."""

from pathlib import Path

_INTERNAL_RESULT_DIR_NAMES = {"_gap_repairs"}


def _is_internal_result_path(relative_path: Path) -> bool:
    """Return True when a path lives under an internal task output directory."""
    parent_parts = relative_path.parts[:-1]
    return any(part.lower() in _INTERNAL_RESULT_DIR_NAMES for part in parent_parts)


def collect_manifest_output_files(
    output_dir: Path,
    *,
    ignored_names: set[str] | None = None,
) -> list[str]:
    """Collect publishable task output files relative to the output directory."""
    ignored_lower = {name.lower() for name in ignored_names or set()}
    output_files: list[str] = []

    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() in ignored_lower:
            continue
        relative_path = path.relative_to(output_dir)
        if _is_internal_result_path(relative_path):
            continue
        output_files.append(str(relative_path))

    return output_files
