# Version Management Guide — Runner Manager

## 📌 Overview

This project follows **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

- **MAJOR**: incompatible API changes
- **MINOR**: backward-compatible feature additions
- **PATCH**: backward-compatible bug fixes

## 📁 Version files

### Primary files

- `app/__version__.py` — single source of truth for the version
- `VERSION` — plain text file with the version number
- `CHANGELOG.md` — detailed history of changes

### Configuration files

- `pyproject.toml` — modern Python configuration (reads from `__version__.py`)

## 🛠️ Using the management script

### Show current version

```bash
uv run scripts/manage_version.py show
```

### Bump the version

#### Patch (1.0.0 → 1.0.1)

For minor bug fixes:

```bash
uv run scripts/manage_version.py bump patch
```

#### Minor (1.0.0 → 1.1.0)

For backward-compatible features:

```bash
uv run scripts/manage_version.py bump minor
```

#### Major (1.0.0 → 2.0.0)

For breaking changes:

```bash
uv run scripts/manage_version.py bump major
```

### Set a specific version

```bash
uv run scripts/manage_version.py set 2.1.3
```

## 📝 Release process

### 1. Update the version

```bash
# Example for a patch release
uv run scripts/manage_version.py bump patch
```

### 2. Update CHANGELOG.md

Add a section for the new version with:

- Release date
- Sections: Added, Changed, Deprecated, Removed, Fixed, Security

Example:

```markdown
## [1.0.1] - 2026-04-09

### Fixed
- Fix XYZ bug
- Improve ABC performance

### Added
- New DEF feature
```

### 3. Commit changes

```bash
git add app/__version__.py VERSION CHANGELOG.md pyproject.toml
git commit -m "chore: bump version to X.Y.Z"
```

### 4. Create a Git tag

```bash
git tag -a manager-vX.Y.Z -m "Esup-Runner Manager release X.Y.Z"
```

### 5. Push changes

```bash
git push origin main
git push origin manager-vX.Y.Z
```

## 🔍 Version checks

### From Python

```python
from app import __version__
print(__version__)  # 1.0.0
```

### From the API

Default local port is `8081` (`MANAGER_PORT` in `.env.example`).

```bash
curl -H "X-API-Token: YOUR_TOKEN" http://localhost:8081/api/version
```

Response:

```json
{
  "version": "1.0.0",
  "version_info": {
    "major": 1,
    "minor": 0,
    "patch": 0
  },
  "description": "Runner Manager - A distributed task runner management system",
  "author": "Loïc Bonavent",
  "email": "xx.xx@univ.fr",
  "license": "Licence GPL 3.0"
}
```

### From the VERSION file

```bash
cat VERSION
```

## 📊 Version endpoints

### GET /api/version

Returns all version information for the API.

**Authentication required**: Yes (API token)

**Response**:

```json
{
  "version": "1.0.0",
  "version_info": {
    "major": 1,
    "minor": 0,
    "patch": 0
  },
  "description": "Runner Manager - A distributed task runner management system",
  "author": "Loïc Bonavent",
  "email": "loic.bon...@univ.fr",
  "license": "Licence GPL 3.0"
}
```

### GET /

The root endpoint also includes the version:

```json
{
  "message": "Runner Manager API",
  "version": "1.0.0",
  "documentation": { … },
  …
}
```

## 🔄 Development workflow

### Feature development

1. Create a branch: `git checkout -b feature/my-feature`
2. Develop and test
3. Update CHANGELOG.md under [Unreleased]
4. Open a PR

### Before a release

1. Merge all PRs planned for the release
2. Review CHANGELOG.md
3. Bump the appropriate version
4. Create the tag and publish

## 📦 Installation and distribution

### Build the package

```bash
uv build
```

## ⚠️ Best practices

1. **Always** update CHANGELOG.md
2. **Never** edit the version in multiple files manually
3. **Use** `manage_version.py` for any version change
4. **Create** a Git tag for each release
5. **Test** the application before creating a release
6. **Document** breaking changes in the changelog

## 🆘 Troubleshooting

### The displayed version is incorrect

```bash
# Check consistency
uv run scripts/manage_version.py show
cat VERSION
uv run python -c "from app import __version__; print(__version__)"
```

### Reset to a specific version

```bash
uv run scripts/manage_version.py set 1.0.0
```

### Git version does not match

```bash
# List tags
git tag -l

# Delete a local tag
git tag -d manager-vX.Y.Z

# Delete a remote tag
git push origin :refs/tags/manager-vX.Y.Z
```

## 📚 References

- [Semantic Versioning](https://semver.org/)
- [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
- [Git Tagging](https://git-scm.com/book/en/v2/Git-Basics-Tagging)
