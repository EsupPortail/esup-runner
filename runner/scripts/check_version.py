#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test script to verify version management is working correctly.

Usage:
    uv run scripts/check_version.py
"""

import re
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def test_version_import():
    """Test that version can be imported correctly."""
    from app import __author__, __email__, __version__, __version_info__

    print("✓ Version import successful")
    print(f"  Version: {__version__}")
    print(f"  Version info: {__version_info__}")
    print(f"  Author: {__author__}")
    print(f"  Email: {__email__}")


def test_version_format():
    """Test that version follows semantic versioning format."""
    from app import __version__, __version_info__

    # Check version format
    parts = __version__.split(".")
    assert len(parts) == 3, "Version should have 3 parts (MAJOR.MINOR.PATCH)"

    # Check all parts are integers
    for part in parts:
        assert part.isdigit(), f"Version part '{part}' should be a number"

    # Check version_info matches version string
    expected_info = tuple(int(x) for x in parts)
    assert __version_info__ == expected_info, "Version info doesn't match version string"

    print("✓ Version format is valid")
    print("  Format: MAJOR.MINOR.PATCH")
    print(f"  Values: {__version_info__[0]}.{__version_info__[1]}.{__version_info__[2]}")


def test_version_file():
    """Test that VERSION file exists and matches __version__.py"""
    from app import __version__

    version_file = project_root / "VERSION"
    assert version_file.exists(), "VERSION file should exist"

    file_version = version_file.read_text().strip()
    assert (
        file_version == __version__
    ), f"VERSION file ({file_version}) doesn't match __version__.py ({__version__})"

    print("✓ VERSION file is consistent")
    print(f"  File version: {file_version}")
    print(f"  Module version: {__version__}")


def test_openapi_config():
    """Test that OpenAPI config uses version correctly."""
    from app import __version__

    try:
        from app.api.openapi import OpenAPIConfig
    except ModuleNotFoundError as e:
        # Allows running this script in minimal environments where FastAPI
        # dependencies are not installed.
        if getattr(e, "name", None) == "fastapi":
            print("! Skipped: fastapi not installed")
            return
        raise

    assert OpenAPIConfig.VERSION == __version__, "OpenAPIConfig.VERSION should match __version__"

    config = OpenAPIConfig.get_fastapi_config()
    assert config["version"] == __version__, "FastAPI config version should match __version__"

    print("✓ OpenAPI configuration is correct")
    print(f"  OpenAPI version: {OpenAPIConfig.VERSION}")


def test_pyproject_version():
    """Test that pyproject.toml [project].version matches __version__.py."""
    from app import __version__

    pyproject_file = project_root / "pyproject.toml"
    assert pyproject_file.exists(), "pyproject.toml should exist"

    lines = pyproject_file.read_text().splitlines()
    in_project_section = False
    pyproject_version = None

    for line in lines:
        stripped = line.strip()
        if stripped == "[project]":
            in_project_section = True
            continue
        if in_project_section and stripped.startswith("[") and stripped.endswith("]"):
            in_project_section = False
        if in_project_section and stripped.startswith("version"):
            match = re.search(r"^version\s*=\s*['\"]([^'\"]+)['\"]\s*$", stripped)
            assert match, "Unable to parse [project].version in pyproject.toml"
            pyproject_version = match.group(1)
            break

    assert pyproject_version is not None, "Unable to find [project].version in pyproject.toml"
    assert (
        pyproject_version == __version__
    ), f"pyproject.toml version ({pyproject_version}) doesn't match __version__.py ({__version__})"

    print("✓ pyproject.toml version is consistent")
    print(f"  pyproject version: {pyproject_version}")


def test_all():
    """Run all tests."""
    print("=" * 60)
    print("Testing Runner Version Management")
    print("=" * 60)
    print()

    tests = [
        ("Version Import", test_version_import),
        ("Version Format", test_version_format),
        ("VERSION File", test_version_file),
        ("OpenAPI Config", test_openapi_config),
        ("pyproject.toml", test_pyproject_version),
    ]

    passed = 0
    failed = 0

    for test_name, test_func in tests:
        print(f"\nRunning: {test_name}")
        print("-" * 60)
        try:
            test_func()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    assert failed == 0


if __name__ == "__main__":
    try:
        test_all()
        sys.exit(0)
    except AssertionError:
        sys.exit(1)
