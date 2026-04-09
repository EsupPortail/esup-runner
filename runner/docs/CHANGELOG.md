# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Upgraded `transformers` to a non-vulnerable range for transcription extras: `>=5.0.0rc3,<6.0.0` (for both `transcription-cpu` and `transcription-gpu`).
- Resolved Dependabot alert related to arbitrary code execution risk in `Trainer._load_rng_state()` when loading malicious checkpoint RNG files (e.g. `rng_state.pth`) in affected versions.
- Regenerated `runner/uv.lock` and updated resolved dependencies, including `transformers` to `5.5.1` and compatible transitive packages.
- Hardened task result filesystem access in `runner/app/api/routes/task.py` by validating `task_id` and result relative paths before file resolution.
- Added strict path boundary checks for manifest/result retrieval and deletion flows to prevent traversal/symlink escape patterns from user-controlled inputs.
- Refactored task result path resolution to traverse filesystem entries from trusted base directories (instead of composing paths from user input), improving robustness against `Uncontrolled data used in path expression` CodeQL alerts.
- Extended storage route security coverage with targeted regression tests in `runner/tests/test_storage_routes_coverage.py`.

## [1.0.0] - 2026-04-09

### Added

- Initial release of the ESUP Runner.
