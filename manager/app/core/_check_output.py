#!/usr/bin/env python3
"""Shared output formatting helpers for manager check scripts."""

from __future__ import annotations

import os
from typing import Literal

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
