---
name: esup-runner-security
description: Review Esup-Runner security when explicitly requested or when a change materially affects authentication, trust boundaries, outbound URLs, process execution, filesystem access, secrets, dependencies, or deployment exposure. Do not activate for ordinary edits without a material security impact.
---

# Esup-Runner security review

## Establish exposure

Review the requested diff or subsystem and trace input from its caller to the
network, subprocess, filesystem, or protected operation. Identify the realistic
actor (client, admin, registered runner, media provider) and privileges required.
Read [SECURITY.md](../../../SECURITY.md) and the relevant component's
`docs/CONFIGURATION.md`; distinguish deployment choices from code defects.
Short paths below are relative to the named component. Load only relevant areas.

## Review affected boundaries

- **Authentication/authorization:** inspect both `app/core/auth.py` modules,
  manager `app/core/passwords.py`, API routes, and OpenAPI/admin access. Check
  bearer/API tokens, resource access, runner identity, and completion ownership;
  authenticated clients and registered runners still cross trust boundaries.
- **Outbound URLs and callbacks:** follow manager models, registration, dispatch,
  `app/services/task_callback_service.py`, and runner downloads and
  `app/services/manager_service.py`. Check schemes, credentials in URLs,
  allowlists, DNS results, redirects, delivery-time validation, forwarded tokens,
  and SSRF reachability. Manager `notify_url` defaults reject private networks,
  whereas runner registration permits them by default for internal deployments;
  preserve that distinction. Review retries and ambiguous delivery without
  assuming callbacks are harmless or idempotent.
- **Processes and media:** inspect `runner/app/task_handlers/base_handler.py`,
  encoding/studio FFmpeg command builders, transcription runtime helpers, and
  process control. Trace user parameters into argument lists, filters, paths,
  and protocol inputs; consider shell injection and option injection separately.
  Check timeouts, cancellation/process ownership, and cleanup. Preserve applicable
  checks in `runner/app/core/media_denylist.py` before media reaches FFmpeg,
  ffprobe, or Whisper; a denylist does not replace runtime updates.
- **Files:** follow upload/download routes, `runner/app/managers/storage_manager.py`,
  result manifests, and manager persistence. Check identifiers, resolved path
  containment, symlinks, temporary-file permissions, shared-volume boundaries,
  and deletion/cleanup paths. Include malicious filenames and media supplied by
  external sources where accepted.
- **Secrets and deployment:** inspect changed configuration/environment handling,
  Dockerfiles, production service files, and dependency/lockfile changes. Check
  service UID/GID, writable volumes, exposed ports, TLS/proxy assumptions,
  token forwarding, and credentials in logs/errors. Avoid reading or printing
  real `.env` secrets; use configuration code and examples. Validate concrete
  dependency risks against the resolved version and relevant advisory before
  claiming a vulnerability.
- **Resource exhaustion:** check input sizes, streamed downloads, FFmpeg/Whisper
  runtime and memory, GPU allocation, task concurrency, callback retries, and
  storage retention. Tie denial-of-service findings to a reachable operation and
  existing limits, not merely the presence of expensive media processing.

## Report actionable findings

Prioritize by practical impact and likelihood. Trace existing validation and
mitigations before reporting a defect; distinguish demonstrated risks from
unverified assumptions or optional hardening. Do not recommend additional
infrastructure or broad security rewrites without a concrete need.

For each meaningful finding, report in French:

- Affected file/line and code path.
- Concrete risk and severity justified by impact and likelihood.
- Realistic attack/failure scenario, required access, and supporting evidence.
- Minimal recommended fix preserving intended deployments and interfaces.
- Regression tests to add, including rejected input and legitimate behavior.

Use isolated fixtures and mocked network/processes to validate findings; consult
the [testing skill](../esup-runner-testing/SKILL.md) for project commands.
State the reviewed scope and unverified assumptions even when no issue is found.
