---
name: esup-runner-change-verification
description: Review an implemented Esup-Runner change for completeness and regressions before declaring significant work complete or ready for a PR. Use for final diff review and readiness checks; this skill reviews the change rather than designing or implementing it.
---

# Esup-Runner change verification

## Establish the complete change

Locate the baseline captured before edits as required by
[AGENTS.md](../../../AGENTS.md): starting commit, status, staged/unstaged diffs,
and content snapshots for untracked or ignored files in scope. Compare it with
`git status --short --untracked-files=all`, `git diff`, and `git diff --cached`.
Read new files too: ordinary diffs omit untracked files. For requested files
missing from Git's diff, use the saved content snapshots. For a PR, also include
committed changes against the intended base, verifying that base first.

If the baseline is missing, incomplete, or captured after edits began, state
which attribution is uncertain. Review the relevant diff and available edit
history, but do not claim that every difference from `HEAD` belongs to the agent
or that unrelated user edits were preserved with certainty. Do not reconstruct
a supposed initial state from the current tree or revert unexplained changes.
Continue the checks that remain possible and report the attribution limitation.

Check the result against the requested behavior, including a concrete trigger
and expected outcome. Flag unnecessary refactoring, unrelated files, accidental
formatting changes, and newly added legacy compatibility without a requirement.
This is a review: report findings without expanding the implementation scope.

## Inspect the affected contracts

Apply only checks relevant to the diff, tracing callers and persisted data
instead of relying on passing tests alone. Short paths below are relative to
the named component.

| Changed area | Review questions |
| --- | --- |
| Interfaces and wrappers | Are API models, defaults, return values, CLI `main`/`parse_args`, signatures, and monkeypatch locations still compatible? Do both sides of manager/runner changes agree? |
| Runner state/recovery | In `runner/app/core/state.py`, `app/services/task_recovery.py`, and task routes, do compact statuses retain the fields needed after restart? Are writes atomic and scoped to the owning instance? Does recovery distinguish surviving processes, final outputs, retryable failures, and user cancellation? Is availability withheld until reconciliation finishes? |
| Manager concurrency | In `manager/app/core/runner_store.py`, `app/core/persistence.py`, and dispatch services, are reservations and state updates consistent across workers? Check locks, stale in-memory snapshots, deletion tombstones, and reservation release on failure. |
| Callbacks and results | Trace runner `app/services/manager_service.py` through manager callback/result services. Check payload compatibility, persistence order, duplicate/stale completion, bounded retries, and manager/network failure without losing terminal state. |
| Error paths and logging | Check timeout, cancellation, process failure, malformed persistence, and full/unwritable storage where relevant. Logs should identify task/instance and explain failures without leaking credentials or sensitive payloads. |

Check existing compatibility requirements, including manager/runner
`MAJOR.MINOR` alignment for protocol changes. Do not remove required behavior
merely because it looks old, or add speculative compatibility layers.

## Validate readiness

- Identify missing or weak regression tests, particularly failure paths and
  behavior hidden by mocks. Read the sibling
  [testing skill](../esup-runner-testing/SKILL.md) for command selection and
  coverage rules; this is an explicit file reference, not an automatic skill call.
- Run appropriate checks within the task's scope. Reuse relevant successful
  results for the current diff; after fixes, rerun affected checks. If execution
  is unavailable, recommend exact commands with their component directory and
  state the blocker instead of declaring the change verified.
- Check documentation for changed behavior, configuration, APIs, deployment,
  or recovery procedures. Use `DEVELOPMENT.md`, component `docs/OPERATIONS.md`,
  `docs/CONFIGURATION.md`, and `docs/CHANGELOG.md` as applicable. Consult
  `.github/pull_request_template.md` when preparing a PR; ordinary changes do
  not require a version bump.
- Inspect the final diff again after validation. Use the baseline to check that
  unrelated user changes were preserved and that only intended changes belong
  to this task; retain any attribution limitation if evidence is incomplete.

Return findings in French with affected file/line and concrete consequence,
followed by validation evidence and remaining limitations. If there are no
findings, say so while distinguishing checks run from untested behavior.
