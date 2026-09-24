# ESUP-Runner project instructions

- This monorepo contains two independent Python applications: `runner/` and
  `manager/`. Work and run Python/Make commands from the affected component;
  each has its own environment and a top-level `app` package.
- Prefer simplicity and readability over unnecessary abstractions. Choose the
  simplest solution that preserves production robustness and passes tests and lint.
- Protect runner restart/recovery, compact persistent task statuses, manager
  callbacks, and multi-instance behavior.
- Preserve existing tests and the public/internal interfaces they rely on,
  including function and method signatures and `monkeypatch` entry points.
- Do not add unrequested legacy compatibility.
- Never use destructive Git commands or revert unrelated user changes.
- Before the first edit of a task, save a baseline outside the repository (for
  example in a task-specific directory under `/tmp`): `git rev-parse HEAD`,
  `git status --short --untracked-files=all`, `git diff --binary`, and
  `git diff --cached --binary`. Also copy the initial contents of untracked or
  ignored files in scope before editing them; add snapshots if scope expands,
  without replacing earlier ones. Keep the baseline location for final review.
  If edits already started without a baseline, record that limitation; a later
  snapshot or `HEAD` cannot establish which changes predated the task.
- Use `uv` for Python and Python tooling, always with
  `UV_CACHE_DIR=/tmp/esup-runner-uv-cache`.
- Write explanations, findings, and final responses in French unless explicitly
  requested otherwise. Lead with the result, then list changed files, checks run,
  and limitations. Suggest next steps only when useful.

Skills describe workflows and invariants. Resolve changeable details (versions,
dependencies, CI commands, release options and effects) from the current source
files they reference, rather than treating skill prose as a second specification.
Report relevant discrepancies; follow executable configuration for actual
behavior without silently expanding the task to fix its documentation.

Use the appropriate repository skills for significant changes; read their
`SKILL.md` when applicable:

- [Testing](.agents/skills/esup-runner-testing/SKILL.md): Python validation, tests,
  coverage, and CI failures.
- [Change verification](.agents/skills/esup-runner-change-verification/SKILL.md):
  final review before declaring a significant change complete or PR-ready.
- [Architecture](.agents/skills/esup-runner-architecture/SKILL.md): substantial
  design, protocol, state, or infrastructure decisions.
- [Security](.agents/skills/esup-runner-security/SKILL.md): requested security
  reviews or material changes to security-sensitive behavior.
- [Release](.agents/skills/esup-runner-release/SKILL.md): release preparation,
  dry-runs, readiness checks, notes, tags, and explicitly requested publication.
