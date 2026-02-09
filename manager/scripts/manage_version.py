#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Version management utility for Runner Manager.

This script helps manage version numbers across the project.
It can bump version numbers (major, minor, patch) and update
all relevant files.

Usage:
    uv run scripts/manage_version.py show
    uv run scripts/manage_version.py bump patch
    uv run scripts/manage_version.py bump minor
    uv run scripts/manage_version.py bump major
    uv run scripts/manage_version.py set 2.0.0
"""

import argparse
import re
import sys
from pathlib import Path


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def get_current_version() -> str:
    """Read the current version from __version__.py"""
    version_file = get_project_root() / "app" / "__version__.py"
    if not version_file.exists():
        raise FileNotFoundError(f"Version file not found: {version_file}")

    content = version_file.read_text()
    version_match = re.search(r"^__version__\s*=\s*['\"]([^'\"]*)['\"]", content, re.M)
    if version_match:
        return version_match.group(1)
    raise ValueError("Unable to find version string in __version__.py")


def parse_version(version: str) -> tuple:
    """Parse version string into (major, minor, patch) tuple."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version}. Expected MAJOR.MINOR.PATCH")
    return tuple(int(x) for x in parts)


def bump_version(current_version: str, bump_type: str) -> str:
    """
    Bump version number.

    Args:
        current_version: Current version string (e.g., "1.2.3")
        bump_type: Type of bump ("major", "minor", or "patch")

    Returns:
        New version string
    """
    major, minor, patch = parse_version(current_version)

    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    else:
        raise ValueError(f"Invalid bump type: {bump_type}. Must be 'major', 'minor', or 'patch'")

    return f"{major}.{minor}.{patch}"


def update_version_file(new_version: str) -> None:
    """Update the __version__.py file with the new version."""
    version_file = get_project_root() / "app" / "__version__.py"
    content = version_file.read_text()

    # Update __version__
    content = re.sub(
        r"^__version__\s*=\s*['\"]([^'\"]*)['\"]",
        f'__version__ = "{new_version}"',
        content,
        flags=re.M,
    )

    version_file.write_text(content)
    print(f"✓ Updated {version_file}")


def update_version_txt(new_version: str) -> None:
    """Update the VERSION file with the new version."""
    version_txt = get_project_root() / "VERSION"
    version_txt.write_text(f"{new_version}\n")
    print(f"✓ Updated {version_txt}")


def show_version() -> None:
    """Display the current version."""
    current = get_current_version()
    major, minor, patch = parse_version(current)

    print(f"Current version: {current}")
    print(f"  Major: {major}")
    print(f"  Minor: {minor}")
    print(f"  Patch: {patch}")


def set_version(new_version: str) -> None:
    """
    Set a specific version number.

    Args:
        new_version: The new version string
    """
    # Validate version format
    parse_version(new_version)

    current = get_current_version()
    print(f"Current version: {current}")
    print(f"New version: {new_version}")

    update_version_file(new_version)
    update_version_txt(new_version)

    print(f"\n✓ Version updated successfully to {new_version}")
    print("\nDon't forget to:")
    print("  1. Update CHANGELOG.md with release notes")
    print("  2. Commit the changes: git add app/__version__.py VERSION")
    print(f"  3. Create a git tag: git tag -a v{new_version} -m 'Release {new_version}'")
    print("  4. Push changes and tags: git push && git push --tags")


def bump_version_command(bump_type: str) -> None:
    """
    Bump the version number.

    Args:
        bump_type: Type of bump ("major", "minor", or "patch")
    """
    current = get_current_version()
    new_version = bump_version(current, bump_type)

    print(f"Bumping {bump_type} version:")
    print(f"  {current} → {new_version}")

    update_version_file(new_version)
    update_version_txt(new_version)

    print(f"\n✓ Version bumped successfully to {new_version}")
    print("\nDon't forget to:")
    print("  1. Update CHANGELOG.md with release notes")
    print("  2. Commit the changes: git add app/__version__.py VERSION")
    print(f"  3. Create a git tag: git tag -a v{new_version} -m 'Release {new_version}'")
    print("  4. Push changes and tags: git push && git push --tags")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Manage version numbers for Runner Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s show              Show current version
  %(prog)s bump patch        Bump patch version (1.0.0 → 1.0.1)
  %(prog)s bump minor        Bump minor version (1.0.0 → 1.1.0)
  %(prog)s bump major        Bump major version (1.0.0 → 2.0.0)
  %(prog)s set 2.0.0         Set specific version
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Show command
    subparsers.add_parser("show", help="Show current version")

    # Bump command
    bump_parser = subparsers.add_parser("bump", help="Bump version number")
    bump_parser.add_argument(
        "type", choices=["major", "minor", "patch"], help="Type of version bump"
    )

    # Set command
    set_parser = subparsers.add_parser("set", help="Set specific version")
    set_parser.add_argument("version", help="Version number (e.g., 1.2.3)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "show":
            show_version()
        elif args.command == "bump":
            bump_version_command(args.type)
        elif args.command == "set":
            set_version(args.version)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
