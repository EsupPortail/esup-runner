"""Filesystem paths for resources bundled with the Manager package."""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
WEB_TEMPLATES_DIR = APP_DIR / "web" / "templates"
