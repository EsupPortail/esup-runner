# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Rate limiting via `slowapi` (120 req/min global, 10 req/min on `/admin`)
- SSRF validation on `source_url` and `notify_url` (rejects private/loopback IPs and non-HTTP schemes)

### Changed
-

### Fixed
-

### Deprecated
-

### Removed
-

### Security
- Fixed token leak in HTTP 401 error responses
- Use `hmac.compare_digest()` for constant-time token comparison (timing attack mitigation)
- Reject `default-manager-token` in production environment

## [1.0.0] - 2026-02-05

### Added
- Initial release of the ESUP Runner Manager.

[Unreleased]: https://github.com/ESUP-Portail/esup-runner-manager/compare/1.0.0...HEAD
[1.0.0]: https://github.com/ESUP-Portail/esup-runner-manager/releases/tag/1.0.0
