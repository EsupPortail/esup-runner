---
name: esup-runner-testing
description: Run and improve Esup-Runner tests and Python validation. Use for pytest, coverage, formatting, linting, type checking, CI failures, test creation, or validation of Python changes in runner or manager.
---

# Esup-Runner testing

## Environment and sources

Run from `runner/` or `manager/`, never with both `app` packages on one Python
path. Use `uv` for every Python tool and set this in each shell:

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
```

Read [DEVELOPMENT.md](../../../DEVELOPMENT.md) for the shared workflow, then
resolve the relevant details from the current executable configuration:

| Question | Source to inspect |
| --- | --- |
| Available commands, prerequisite installation, formatting side effects, coverage gate | Affected component's `Makefile` |
| Python requirements, extras/conflicts, resolved dependencies, tool and pytest settings | Component's `pyproject.toml`, `uv.lock`, `.flake8`, and coverage configuration (including `runner/.coveragerc`) |
| CI matrix, dependency installation, packaging and smoke checks | [ci.yml](../../../.github/workflows/ci.yml) and [code_formatting.yml](../../../.github/workflows/code_formatting.yml) |
| Runtime Python and system dependencies, when relevant | Component's `Dockerfile` |
| Client fixtures, mocks, startup/lifespan behavior | Component's `tests/conftest.py` and affected tests |

If tools are missing, use the component's development dependency target after
reading its recipe. Select only extras needed for the task, respecting declared
conflicts; do not assume an `all` target is required for tests. To reproduce CI,
use its current installation command and lockfile policy. Report relevant
documentation/configuration discrepancies instead of relying on copied settings.

## Select and run checks

1. Map changed functions to tests and callers with `rg` in the component's
   `tests/` and `app/`. Search patched symbol names as well as module names:
   implementation extracted into services or handler `core/` packages can still
   be exercised through route or CLI wrappers. Start with the smallest relevant
   regression tests, then run the containing modules.
   Use `uv run pytest -q` with the discovered test paths or node IDs.
2. Run the configured formatters, linters, and type checker. For formatting
   writes, pass explicit changed paths to avoid unrelated edits; use check-only
   options for a review. Reuse the Makefile's tool options and rerun affected
   tests after fixes.
3. Run the applicable component checks, using `make ci` after inspecting its
   recipe. If it formats broadly or repeats checks already run, select the
   equivalent check targets as appropriate (for example `make fmt-check`,
   `make lint`, and `make coverage`). Inspect the resulting diff against the
   task baseline. Shared protocol, packaging, or CI changes can require both
   components; packaging changes also need the workflow's build/smoke checks.

## Regression and coverage expectations

- Preserve the official coverage gate defined by the current Makefile/CI
  configuration. Aim for **100% on modified code**, ideally globally, whenever
  reasonably possible. Explain remaining gaps; do not lower the gate or hide
  changed code with exclusions.
- For lifecycle changes, test restart from persisted state, instance ownership,
  readiness during recovery, terminal results, cancellation, timeouts, failed
  persistence, and callback failures/retries as applicable. Include stale or
  duplicate updates and concurrent reservation when those paths change.
- Preserve signatures and patch locations used by tests. Inspect ASGI client,
  thread, and lifespan behavior in the fixtures; explicitly exercise startup
  for recovery tests rather than assuming client creation invokes it.
- Mock DNS, HTTP, SMTP, subprocesses, clocks, and media services where appropriate;
  isolate filesystem effects with temporary paths, away from real task data.
- For Jinja output tests, render directly with `Environment`/`FileSystemLoader`
  or an existing rendering helper; do not use `TestClient` for template rendering.
- Mocked media tests do not validate FFmpeg or NVIDIA hardware. For runtime
  changes, consult `runner/docs/FFMPEG_SETUP.md` and `runner/docs/gpu/README.md`
  and distinguish unit coverage from actual CPU/GPU validation.

Report commands, component directory, results, and coverage gaps in French.
If a command cannot run, give the exact cause (missing dependency, unavailable
runtime/hardware, network restriction, or permissions) and identify checks left
unverified. Do not present skipped checks as passing.
