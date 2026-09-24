---
name: esup-runner-release
description: Prepare, dry-run, review, and explicitly publish Esup-Runner manager/runner releases; extract release notes, manage release tags, and diagnose release_stack.py or release workflow failures. Use for version/release preparation and release readiness checks, not ordinary development, testing, architecture, or routine code review.
---

# Esup-Runner release

## Sources and authorization

Keep [RELEASE.md](../../../RELEASE.md) as the primary human-readable guide.
Before acting, compare it with [release_stack.py](../../../scripts/release_stack.py),
[release.yml](../../../.github/workflows/release.yml), and each selected component's
`Makefile`, `scripts/manage_version.py`, version files, and changelog. Use the
current parser and execution paths for supported commands, defaults, validation,
and side effects; use the workflow for triggers and published artifacts. Report
relevant discrepancies and derive the command from the implementation. Do not
silently change release tooling or documentation outside the requested scope.

Resolve the requested version and component scope before preparation; manager
and runner can release independently. Ask only if the request leaves them
undetermined. Keep component selection consistent across phases, verify the tag
convention against the helper/workflow, and check `MAJOR.MINOR` compatibility.

Default to **dry-run → prepare → review → publish**. Requests to prepare a
version, check readiness, or "run the release workflow" authorize no publication.
Only explicit intent such as "publish", "push the release", or "create and push
the tags" permits the corresponding external actions. A request for local tags
alone does not authorize pushes. Complete preparation/review before requesting
any missing publication authorization; do not ask again if it was already given.

## 1. Dry-run

Capture the pre-edit baseline required by [AGENTS.md](../../../AGENTS.md).
Run the stack orchestrator from the repository root; it selects component
working directories itself. The examples use `1.9.0` as a placeholder for the
requested version; adapt component options to the agreed scope. Set the cache
in every shell:

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/release_stack.py prepare 1.9.0 --dry-run
```

Inspect the plan and the script's dry-run branches to identify checks performed
or skipped and any remaining side effects. A successful preview is not proof of
release readiness. Distinguish this script preview from a workflow/local Docker
dry-run: inspect the workflow and Make target before invoking either, since a
build may still execute and require external tools.

## 2. Prepare

Start on the intended release branch with a clean working tree.
Report unrelated edits and leave them untouched; do not hide them with
`--allow-dirty` or automatically commit/stash them to make preparation proceed.

```bash
uv run scripts/release_stack.py prepare 1.9.0
```

Inspect the helper's component definitions and preparation path, then follow
their Make recipes for version updates, dependency locking/synchronization,
checks, and changelog promotion. Identify dependency upgrades, optional runtime
extras, and tool installation before execution. Do not repeat orchestrated steps
manually or update tooling unless that is appropriate to the request.

Derive expected changed files from the helper's release paths and version
updater, including lockfile and changelog changes. Check whether invoked quality
targets also format other files; inspect those changes rather than silently
including or discarding them. For test failures or coverage details, consult the
[testing skill](../esup-runner-testing/SKILL.md) as needed.

## 3. Review

Compare Git status, staged/unstaged diffs, and new files with the saved baseline.
Verify version representations managed by the updater, including package
metadata in the lockfile; inspect dependency changes, released changelog
content/date, and successful checks for each selected component. If the baseline
is incomplete, apply the attribution fallback in the
[change-verification skill](../esup-runner-change-verification/SKILL.md).

```bash
uv run scripts/release_stack.py notes manager 1.9.0
uv run scripts/release_stack.py notes runner 1.9.0
```

Run notes only for selected components after their release sections exist.
GitHub notes must be English and match those sections; do not invent highlights
or silently publish internal hostnames copied from local documentation. Use the
change-verification skill for the final diff review.

Stop after this review unless publication was explicitly requested. Report the
prepared files, checks, and planned tags, with no Git publication flags added.

## 4. Publish only with explicit intent

Recheck versions, notes, changelogs, the exact diff, and CI evidence. Verify the
selected target branch and remote; fetch when reachable, compare with that remote
branch, and account for every commit the push would send.
Resolve unexpected divergence before publishing. Check both local and remote
release tags; an unavailable remote check is a blocker, not evidence of absence.

Derive the exact publication command from the current parser and execution
paths. In particular, trace whether it reruns preparation, rejects the prepared
working tree, or attempts to promote an already-promoted changelog. Do not assume
the guide's full command can resume after local preparation, or that a separate
publish subcommand exists. Select supported options for the observed state.
Allow a prepared dirty tree only when it consists exclusively of reviewed release
changes. Skip steps only to preserve successfully completed work with unchanged
validated inputs, never to bypass failures. If inputs changed, complete the
affected preparation/validation before publication.

Inspect the entire staging area: `git commit` includes previously staged changes
as well as the helper's additions. Trace commit/push/tag ordering and resolve the
exact commit selected for each tag; do not assume it is `HEAD`. Verify that
commit's version, changelog, root release helper, and workflow. Execute only the
local/external actions authorized by the request; local-only commit/tag requests
do not authorize pushes.

## GitHub outcome, failures, and reporting

- Read `release.yml` at each tag's commit to determine triggers, validation,
  build platforms, registry/image tags, and GitHub Release naming/latest policy.
  Inspect event conditions before treating manual dispatch as publication or
  dry-run. Verify which checks the workflow actually runs; a successful build
  does not substitute for missing component checks. Do not create duplicate
  GitHub Releases manually when the workflow owns them.
- On failure, identify the exact stage/error and inspect worktree, versions,
  changelog, locks/dependencies, CI, Git state, tags, permissions, or Actions as
  relevant. Preparation and publication are not atomic; inspect completed work
  before retrying. Trace existing-tag guards and supported resume paths against
  actual local/remote tags; do not blindly rerun publication after a partial push.
- Recommend the smallest safe correction. Never use destructive Git commands,
  reset user work, or force-push. Investigate existing tags; do not recreate or
  overwrite them without explicit approval. Do not bypass failed checks with skip
  flags or disable branch/remote-tag checks merely to finish a release.
- Report in French: selected component versions, CI/changelog/version/diff
  outcomes, planned tags, and actual local/external actions. After preparation,
  explicitly state whether any commit, push, or tag occurred. After publication,
  report commit SHA, pushed branch, created/pushed tags, and each tag's Actions
  run, image, and GitHub Release verification where accessible. Distinguish
  expected workflow execution from observed success; name pending or impossible
  checks. Never claim publication success from a successful Git push alone.
