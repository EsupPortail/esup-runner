---
name: esup-runner-architecture
description: Analyze Esup-Runner architecture and design tradeoffs for major refactoring, new runner types, protocol or state-management changes, scalability, and substantial infrastructure changes. Use for significant design decisions, not routine small code edits.
---

# Esup-Runner architecture

## Locate responsibilities

Read [DEVELOPMENT.md](../../../DEVELOPMENT.md) and the relevant component's
`docs/README.md` before proposing changes. Short paths below are relative to
the named component; load only the sources needed for the decision.

- **Manager:** accepts client tasks, selects/reserves runners, persists task
  state, exposes the admin UI, and forwards results and optional `notify_url`
  callbacks. Start in `manager/app/api/routes/`, `app/services/`,
  `app/core/persistence.py`, and `app/core/runner_store.py`.
- **Runner:** registers and sends heartbeats, executes handlers, manages
  workspaces/output manifests, persists compact recovery state, and reports to
  the manager. Start in `runner/app/services/`, `app/task_handlers/`,
  `app/core/state.py`, `app/managers/`, and `launcher.py`.
- These are independently packaged applications with separate environments and
  the same `app` package name. Manager/runner versions must match at
  `MAJOR.MINOR`; patch versions may differ. Inspect both `app/models/models.py`
  files when changing their protocol.

## Trace state and failure behavior

Map the affected lifecycle: submission, reservation, dispatch, execution,
persisted updates, completion, manager reconciliation, client notification,
and cleanup. Identify which process owns each transition, which fields survive
restart, and how duplicate or stale events are handled.

Preserve the existing design unless a demonstrated requirement needs a change:

- Manager task persistence uses daily JSON files and locks. Shared runner state
  and atomic reservation use `RunnerStore` in production; development memory
  state does not establish multi-worker correctness.
- Runner status persistence is compact, atomic, and scoped per instance.
  Recovery in `runner/app/services/task_recovery.py` must account for surviving
  processes, final outputs, failed work, and user cancellation. Startup keeps
  availability false during reconciliation; consult
  [runner operations](../../../runner/docs/OPERATIONS.md).
- Completion and callback retries must not corrupt terminal state or apply an
  old run's result to a new run. Examine network interruption, process death
  between persistence and notification, concurrent reservation, lock timeout,
  disk failure, and restart during cleanup as relevant.

## Evaluate runtime impact

For encoding, transcription, studio, or proposed live processing, trace the
actual handler and CLI boundaries. Existing handlers live under encoding,
transcription, and studio; do not assume a separate live handler already exists.
Keep `main`, `parse_args`, and test-patched wrappers stable when extracting code.

Use `runner/docs/TYPE_ENCODING.md`, `TYPE_TRANSCRIPTION.md`, `TYPE_STUDIO.md`,
`FFMPEG_SETUP.md`, and `gpu/README.md` under `runner/docs/` as needed. Account for
FFmpeg/ffprobe capabilities, NVIDIA NVENC/NVDEC, CUDA/runtime compatibility,
CPU threads, GPU memory, concurrent sessions, and CPU/GPU allocation across
instances. Assess backpressure, cancellation, and partial outputs for long-lived
or live work. Do not infer hardware support from a mocked test or Python extra.

For deployment changes, inspect both Dockerfiles, launchers, and the relevant
`docs/DOCKER.md` and `docs/CONFIGURATION.md`. Assess persisted volumes, service
user permissions, port allocation, system dependencies, upgrades/rollback, and
mixed process lifetimes. Explain how operators will detect failures through
task/instance logs, health/readiness, and existing statistics.

Choose the simplest design meeting robustness, maintainability, and scalability
requirements. Justify new queues, databases, services, or coordination layers
against a measured need and their operational cost; do not default to a more
complex distributed architecture.

## Present significant decisions

Write the analysis in French, using these nine parts:

1. Problem and constraints, including workload and deployment assumptions.
2. Current architecture, with the relevant code and state ownership.
3. Proposed solution and changed contracts.
4. Alternatives considered, including the smallest viable change.
5. Advantages.
6. Disadvantages and risks, including compatibility and operational complexity.
7. Failure scenarios and recovery behavior.
8. Implementation impact on both components, persistence, runtime, and docs.
9. Testing/validation strategy: consult the
   [testing skill](../esup-runner-testing/SKILL.md) for repository checks and
   specify any multi-process, restart, media, or hardware validation needed.
