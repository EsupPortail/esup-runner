# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-04-10

### Security

- Removed token content from unauthorized authentication logs in `manager/app/core/auth.py` to avoid clear-text exposure of sensitive API/Bearer token values.
- Kept existing authentication behavior (constant-time token comparison and HTTP 401 responses) while hardening log hygiene.

## [1.0.0] - 2026-04-09

### Added

- Initial release of the ESUP Runner Manager.
