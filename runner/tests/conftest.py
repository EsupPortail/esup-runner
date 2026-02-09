"""Pytest configuration for adding the project root to sys.path."""

import os
import sys

# Ensure the repository root (containing the `app` package) is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
