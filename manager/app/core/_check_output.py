#!/usr/bin/env python3
"""Shared output formatting helpers for manager check scripts."""

from __future__ import annotations

import os
from typing import Any, Literal

Severity = Literal["info", "warning", "error"]

_COLORS = {
    "info": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
}
_PREFIXES = {
    "info": "✓ INFO",
    "warning": "⚠ WARNING",
    "error": "✗ ERROR",
}
_RESET = "\033[0m"

_MANAGER_ENV_FIELDS = (
    "MANAGER_PROTOCOL",
    "MANAGER_HOST",
    "MANAGER_PUBLIC_URL",
    "MANAGER_BIND_HOST",
    "MANAGER_PORT",
    "MANAGER_EMAIL",
)


def _supports_color() -> bool:
    return not bool(os.getenv("NO_COLOR"))


def colorize(text: str, *, level: Severity) -> str:
    color = _COLORS.get(level, "")
    if not color or not _supports_color():
        return text
    return f"{color}{text}{_RESET}"


def format_prefix(*, level: Severity) -> str:
    return colorize(_PREFIXES[level], level=level)


def format_status(message: str, *, level: Severity) -> str:
    return colorize(f"{_PREFIXES[level]}: {message}", level=level)


def check_level(*, ok: bool, required: bool) -> Severity:
    if ok:
        return "info"
    return "error" if required else "warning"


def format_check(name: str, *, ok: bool, required: bool) -> str:
    return format_status(name, level=check_level(ok=ok, required=required))


def manager_configuration_rows(config: Any) -> tuple[tuple[str, str], ...]:
    """Return non-sensitive Manager settings and their effective values."""
    rows = [
        (name, str(getattr(config, name, "") or "").strip() or "(empty)")
        for name in _MANAGER_ENV_FIELDS
    ]

    manager_url = str(getattr(config, "MANAGER_URL", "") or "").strip().rstrip("/")
    public_url = str(getattr(config, "MANAGER_PUBLIC_URL", "") or "").strip().rstrip("/")
    rows.append(("MANAGER_URL", manager_url or "(empty)"))
    rows.append(
        (
            "Manager public admin URL",
            f"{public_url}/admin" if public_url else "(empty)",
        )
    )
    return tuple(rows)


def format_configuration_rows(
    rows: tuple[tuple[str, str], ...], *, indent: str = "  "
) -> tuple[str, ...]:
    """Align configuration labels consistently across check scripts."""
    width = max((len(label) for label, _value in rows), default=0)
    return tuple(f"{indent}{label:<{width}} : {value}" for label, value in rows)
