# Release Automation

This repository contains two releasable components:

- `manager`
- `runner`

Release preparation is automated by `scripts/release_stack.py`. The script
orchestrates the existing component commands and keeps the risky git actions
behind explicit flags.

## Prepare A Release

Preview the full release preparation without changing files:

```bash
uv run scripts/release_stack.py prepare 1.1.1 --dry-run
```

Prepare both components locally:

```bash
uv run scripts/release_stack.py prepare 1.1.1
```

This runs, for each selected component:

- `uv run scripts/manage_version.py set <version>`
- manager: `make lock-upgrade EXTRAS=dev`
- runner: `make lock-all`
- manager: `make sync-dev`
- runner: `make sync-all`
- `make ci` against the refreshed and synchronized dependencies
- changelog promotion from `Unreleased` to `[<version>] - <date>`

By default, the script does not commit, push, create tags, or push tags.

## Publish A Release

After reviewing the local changes, the complete release command is:

```bash
uv run scripts/release_stack.py prepare 1.1.1 --commit --push --create-tags --push-tags
```

The generated tags follow the workflow naming convention:

- `manager-v1.1.1`
- `runner-v1.1.1`

Each tag is created on the latest commit that touched its component directory.
Pushing these tags triggers `.github/workflows/release.yml`.

## Useful Options

Release only one component:

```bash
uv run scripts/release_stack.py prepare 1.1.1 --components manager
uv run scripts/release_stack.py prepare 1.1.1 --components runner
```

Update `uv` before release preparation:

```bash
uv run scripts/release_stack.py prepare 1.1.1 --update-uv
```

Print release notes extracted from a component changelog:

```bash
uv run scripts/release_stack.py notes manager 1.1.1
uv run scripts/release_stack.py notes runner 1.1.1
```

Skip expensive local checks only when deliberately needed:

```bash
uv run scripts/release_stack.py prepare 1.1.1 --skip-ci --skip-lock --skip-sync
```

If the default uv cache directory is not writable in a restricted environment,
set a writable cache path before the command:

```bash
UV_CACHE_DIR=/tmp/esup-runner-uv-cache uv run scripts/release_stack.py prepare 1.1.1 --dry-run
```

## GitHub Releases

The release workflow publishes Docker images and then creates the matching
GitHub Release from the component changelog.

- Manager releases use the title `Manager vX.Y.Z` and are not marked as latest.
- Runner releases use the title `Runner vX.Y.Z` and are marked as latest.

Manual workflow dispatch remains a dry run for Docker publication and does not
create a GitHub Release.
