# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- SMTP STARTTLS support (`SMTP_USE_TLS`) and authentication (`SMTP_USERNAME`, `SMTP_PASSWORD`)

### Changed
-

### Fixed
-

### Deprecated
-

### Removed
-

### Security
- Use `hmac.compare_digest()` for constant-time token comparison (timing attack mitigation)
- Replace `shell=True` with `shlex.split()` in FFmpeg subprocess calls (command injection mitigation)

## [0.9.0] - 2026-02-05

### Added
- Initial release of the ESUP Runner.

[Unreleased]: https://github.com/EsupPortail/esup-runner/compare/0.9.0...HEAD
[0.9.0]: https://github.com/EsupPortail/esup-runner/releases/tag/0.9.0
