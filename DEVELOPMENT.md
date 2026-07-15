# ESUP-Runner Developer Guide

This guide is the starting point for developers working on ESUP-Runner. It
explains how to set up the monorepo, run the two applications locally, find the
relevant code, and validate a change before opening a pull request.

Production installation and operations are documented separately in the
Manager and Runner documentation.

## Repository at a glance

ESUP-Runner contains two independent Python projects:

| Component | Responsibility | Default port | Project directory |
| --- | --- | ---: | --- |
| Manager | Receives tasks, selects runners, persists task state, exposes the admin UI, and forwards status and results | `8081` | `manager/` |
| Runner | Registers with the Manager, executes media tasks, stores outputs, and reports completion | `8082` | `runner/` |

The normal task flow is:

```text
Client application
       |
       | submit task / read status and results
       v
    Manager  -------- authenticated dispatch -------->  Runner
       ^                                                   |
       |                                                   | task handler,
       +--------------- completion callback --------------+ workspace, outputs
       |
       +---------------- optional notify_url callback ----> Client application
```

Each component has its own `pyproject.toml`, `uv.lock`, `.venv`, configuration,
tests, and release metadata. Both packages expose a top-level Python package
named `app`, so do not combine them in one virtual environment or Python path.
Run Python and Make commands from the component directory you are changing.

Manager and Runner versions must match at the `MAJOR.MINOR` level. Patch
versions may differ.

## Prerequisites

For regular development, install:

- Git and Make;
- Python 3.11 or newer (CI currently tests Python 3.11, 3.12, and 3.13);
- [`uv`](https://docs.astral.sh/uv/), used for environments, dependencies, and
  every Python command.

To run real Runner media jobs and operational checks, also install FFmpeg and
`ffprobe`. GPU and transcription development has additional platform-specific
requirements; see [Runner FFmpeg setup](runner/docs/FFMPEG_SETUP.md) and the
[GPU guides](runner/docs/gpu/README.md).

Use the project cache location in every shell used for development:

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
```

No manual virtual-environment activation is required when commands use
`uv run`. For IDE integration, select `manager/.venv/bin/python` or
`runner/.venv/bin/python` according to the project being edited.

## First-time setup

Clone the complete repository so changes spanning both components and shared
automation remain visible:

```bash
git clone https://github.com/EsupPortail/esup-runner.git
cd esup-runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
```

Install the Manager development environment:

```bash
cd manager
make sync-dev
```

Install the Runner development environment:

```bash
cd ../runner
make sync-dev
```

`make sync-dev` installs the application and its formatting, linting, typing,
testing, and coverage tools. It does not install the optional transcription
stack. When transcription dependencies are required, select exactly one
profile and keep the `dev` extra:

```bash
make sync EXTRAS=dev,transcription-cpu
# or: make sync EXTRAS=dev,transcription-gpu
# or: make sync EXTRAS=dev,transcription-gpu-cuda12
```

The transcription profiles are mutually exclusive. `make sync-all` currently
selects `dev,transcription-gpu`; it does not install every profile.

## Local configuration

The test suites provide their own configuration and usually do not require a
local `.env`. To run either service, copy its example only when `.env` does not
already exist:

```bash
cd manager
cp .env.example .env

cd ../runner
cp .env.example .env
```

Never commit `.env`. It can contain API tokens, bcrypt password hashes, cookie
signing keys, SMTP credentials, and deployment URLs. Use generated local-only
values and placeholders in logs, issues, documentation, and test fixtures.

The example files use production-oriented paths. For an unprivileged local
setup, adjust at least these Manager values:

```properties
ENVIRONMENT=development
MANAGER_HOST=127.0.0.1
MANAGER_BIND_HOST=127.0.0.1
LOG_DIR=/tmp/esup-runner-dev/manager/logs
CACHE_DIR=/tmp/esup-runner-dev/manager/cache
UV_CACHE_DIR=/tmp/esup-runner-uv-cache
AUTHORIZED_TOKENS__runners=replace-with-a-local-random-token
```

Generate Manager tokens and an admin password hash with:

```bash
cd manager
uv run scripts/generate_token.py
uv run scripts/generate_password.py
```

Use the same local runner token in `runner/.env`, together with writable paths:

```properties
RUNNER_HOST=127.0.0.1
MANAGER_URL=http://127.0.0.1:8081
RUNNER_TOKEN=replace-with-the-same-local-random-token
RUNNER_TASK_TYPES=[1x(encoding,studio)]
ENCODING_TYPE=CPU
LOG_DIR=/tmp/esup-runner-dev/runner/logs
STORAGE_DIR=/tmp/esup-runner-dev/storage
CACHE_DIR=/tmp/esup-runner-dev/runner/cache
UV_CACHE_DIR=/tmp/esup-runner-uv-cache
```

With these writable paths, `make init` can create the configured directories
without `sudo`. The privileged initialization and systemd commands in the
installation guides are intended for production hosts, not the normal local
development loop.

For every setting and its security implications, use the component references:

- [Manager configuration](manager/docs/CONFIGURATION.md) and
  [parameters](manager/docs/PARAMETERS.md);
- [Runner configuration](runner/docs/CONFIGURATION.md) and
  [parameters](runner/docs/PARAMETERS.md).

## Running the local stack

Start the Manager with Uvicorn reload in the first terminal:

```bash
cd manager
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run esup-runner-manager-dev
```

Start one Runner instance with reload in a second terminal:

```bash
cd runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run esup-runner-runner-dev
```

Useful local URLs are:

- Manager API documentation: <http://127.0.0.1:8081/docs>
- Manager admin interface: <http://127.0.0.1:8081/admin>
- Runner API documentation: <http://127.0.0.1:8082/docs>
- Runner health: <http://127.0.0.1:8082/runner/health>
- Runner availability: <http://127.0.0.1:8082/runner/ping>

The Manager should be started first. A Runner can start without it, but remains
unregistered and retries registration. If registration fails, first check the
Manager URL, the shared token, and `MAJOR.MINOR` version compatibility.

`make run` uses the regular component launchers: Manager behavior depends on
`ENVIRONMENT`, while the Runner launcher may start multiple processes. The
explicit `*-dev` entry points above are simpler for the edit-run-debug loop.

## Bootstrapping a development workstation with Docker

Docker can provide a working Manager and Runner without installing Python,
`uv`, or FFmpeg on the host. The commands below mount the checkout into the
containers and use the development entry points, so Python and template changes
are reloaded automatically. The host only needs Docker and this repository.

This setup is intended for local API and integration development. Install the
native environments described above when the IDE needs a Python interpreter or
when running formatting, typing, and tests. The repository does not currently
provide a Compose file, so the two containers are started explicitly.

### Create local container configuration

From the repository root, create `manager/.env.docker.local` with this minimal
development configuration:

```properties
MANAGER_PROTOCOL=http
MANAGER_HOST=esup-runner-manager
MANAGER_BIND_HOST=0.0.0.0
MANAGER_PORT=8081
ENVIRONMENT=development
UVICORN_WORKERS=1
LOG_DIR=/var/log/esup-runner
RUNNERS_STORAGE_ENABLED=true
RUNNERS_STORAGE_DIR=/tmp/esup-runner
API_DOCS_VISIBILITY=public
AUTHORIZED_TOKENS__dev=esup-runner-local-dev-token
```

Create `runner/.env.docker.local` with the matching token and Docker hostnames:

```properties
DEBUG=True
RUNNER_PROTOCOL=http
RUNNER_HOST=esup-runner-runner
RUNNER_BASE_PORT=8082
RUNNER_BASE_NAME=dev-runner
RUNNER_TASK_TYPES=[1x(encoding,studio)]
MANAGER_URL=http://esup-runner-manager:8081
RUNNER_TOKEN=esup-runner-local-dev-token
LOG_DIR=/var/log/esup-runner
STORAGE_DIR=/tmp/esup-runner
CACHE_DIR=/home/esup-runner/.cache/esup-runner
ENCODING_TYPE=CPU
```

Both files are ignored by Git. The fixed token is only suitable for this local
setup: the port mappings below listen on `127.0.0.1`. Generate a private token
and update both files before exposing the services to another machine.

`MANAGER_HOST` and `RUNNER_HOST` are addresses advertised to the other
container; Docker DNS resolves them on the shared network. The separate
`MANAGER_BIND_HOST=0.0.0.0` value controls the Manager listening socket. Do not
advertise `0.0.0.0` as either service hostname.

### Prepare Docker once

For a development workstation, use the most recently published Manager and
Runner images:

```bash
export MANAGER_IMAGE=ghcr.io/esupportail/esup-runner-manager:latest
export RUNNER_IMAGE=ghcr.io/esupportail/esup-runner-runner:latest

docker pull "$MANAGER_IMAGE"
docker pull "$RUNNER_IMAGE"
```

The Manager enforces `MAJOR.MINOR` compatibility with its Runners. If the two
`latest` tags are temporarily out of sync during a release, replace `latest` in
both image references with the same rolling `MAJOR.MINOR` tag.

Create one network and persistent volumes. Docker reports existing resources
without replacing their data, so the volume commands are safe to repeat:

```bash
docker network inspect esup-runner-net >/dev/null 2>&1 || \
  docker network create esup-runner-net

docker volume create esup-runner-manager-logs
docker volume create esup-runner-manager-data
docker volume create esup-runner-runner-logs
docker volume create esup-runner-storage
docker volume create esup-runner-cache
```

The shared storage volume lets the Manager read Runner results. The other
volumes preserve task state, logs, and caches when containers are recreated.

### Start the development stack

Keep the image variables from the preceding step and run these commands from
the repository root. Start the Manager first:

```bash
docker run -d --rm \
  --name esup-runner-manager \
  --network esup-runner-net \
  --env-file "$PWD/manager/.env.docker.local" \
  -p 127.0.0.1:8081:8081 \
  -v esup-runner-manager-logs:/var/log/esup-runner \
  -v esup-runner-manager-data:/app/data \
  -v esup-runner-storage:/tmp/esup-runner \
  -v "$PWD/manager/.env.docker.local:/app/.env:ro" \
  -v "$PWD/manager/app:/app/app:ro" \
  -v "$PWD/manager/launcher.py:/app/launcher.py:ro" \
  "$MANAGER_IMAGE" \
  esup-runner-manager-dev
```

Then start one Runner instance:

```bash
docker run -d --rm \
  --name esup-runner-runner \
  --network esup-runner-net \
  --env-file "$PWD/runner/.env.docker.local" \
  -p 127.0.0.1:8082:8082 \
  -v esup-runner-runner-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-cache:/home/esup-runner/.cache/esup-runner \
  -v "$PWD/runner/.env.docker.local:/app/.env:ro" \
  -v "$PWD/runner/app:/app/app:ro" \
  -v "$PWD/runner/launcher.py:/app/launcher.py:ro" \
  "$RUNNER_IMAGE" \
  esup-runner-runner-dev
```

The application mounts are read-only from the containers, but edits made by the
host IDE remain visible and trigger Uvicorn reload. The configuration mounts
are also read-only. To test Manager credential persistence through the admin
interface, use a disposable ignored configuration file and make only that
Manager mount writable.

Confirm that both services are ready:

```bash
docker ps --filter name=esup-runner
docker logs --tail 50 esup-runner-manager
docker logs --tail 50 esup-runner-runner

curl -H "X-API-Token: esup-runner-local-dev-token" \
  http://127.0.0.1:8081/manager/health
curl http://127.0.0.1:8082/runner/ping
```

The local API documentation is available at <http://127.0.0.1:8081/docs> and
<http://127.0.0.1:8082/docs>.

### Daily Docker workflow

Changes below `manager/app/`, `runner/app/`, or either `launcher.py` are picked
up without rebuilding. After changing an environment file, stop and recreate
the corresponding container with the command above.

Stop and remove the development containers with:

```bash
docker stop esup-runner-runner esup-runner-manager
```

The containers disappear because they use `--rm`; the named volumes keep local
state. Remove those volumes separately only when a completely clean workstation
is required.

### Rebuild only when the image changes

Rebuild an image after changing its `pyproject.toml`, `uv.lock`, Dockerfile, or
system dependencies. Local builds also let a branch use dependencies that are
not yet present in the published images:

```bash
cd manager
make docker-build \
  DOCKER_TAG=dev \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)

cd ../runner
make docker-build \
  DOCKER_TAG=dev \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)

export MANAGER_IMAGE=esup-runner-manager:dev
export RUNNER_IMAGE=esup-runner-runner:dev
```

Run each component's `make docker-fix-perms` target if existing volumes do not
match the UID/GID used for the local images. To add transcription, build the
Runner with `DOCKER_RUNNER_EXTRA=transcription-cpu` or
`DOCKER_RUNNER_EXTRA=transcription-gpu`; GPU execution also requires the NVIDIA
Container Toolkit and `--gpus all` on `docker run`.

For pinned production tags, restart policies, multi-instance port ranges, GPU
runtime details, and troubleshooting, see the full
[Manager Docker guide](manager/docs/DOCKER.md) and
[Runner Docker guide](runner/docs/DOCKER.md).

## Where to make changes

### Manager

- `manager/launcher.py`: development and production server entry points.
- `manager/app/main.py`: FastAPI application, middleware, lifespan, routes, and
  background-service startup.
- `manager/app/api/routes/`: HTTP API, runner registration and heartbeats, task
  lifecycle, admin pages, logs, and statistics.
- `manager/app/core/`: configuration, authentication, shared state, runner
  storage, task persistence, priorities, logging, and resource paths.
- `manager/app/models/`: Pydantic request, response, and persistence contracts.
- `manager/app/services/`: runner monitoring, cleanup, timeout handling,
  reconciliation, callbacks, and email notifications.
- `manager/app/web/`: Jinja templates and shared static assets.
- `manager/tests/`: unit, API, persistence, service, and rendering tests.

### Runner

- `runner/launcher.py`: single- and multi-instance process startup.
- `runner/app/main.py`: FastAPI application and runtime lifespan.
- `runner/app/api/routes/`: runner health/status and protected task endpoints.
- `runner/app/core/`: configuration, authentication, compact persistent task
  state, recovery data, logging, and disk diagnostics.
- `runner/app/managers/`: process, storage, and background-service management.
- `runner/app/services/`: Manager communication, task dispatch, result
  manifests, and notifications.
- `runner/app/models/`: API and task contracts.
- `runner/app/task_handlers/`: encoding, studio, and transcription handlers.
  Each task type keeps a stable entry point and places its implementation in a
  sibling `core/` package.
- `runner/tests/`: unit, API, handler, recovery, and coverage-focused tests.

The component documentation homes provide more detailed architecture and API
examples: [Manager](manager/docs/README.md) and [Runner](runner/docs/README.md).

## Development and validation loop

Start with the narrowest relevant tests, then broaden validation within the
affected component:

```bash
uv run pytest -q tests/test_relevant_module.py
uv run pytest -q tests/test_relevant_module.py::test_specific_case

make fmt
make lint
make coverage
```

The common quality targets are identical in both projects:

| Command | Purpose |
| --- | --- |
| `make fmt` | Format with Black and isort; modifies files |
| `make fmt-check` | Check Black and isort without modifying files |
| `make lint` | Run Flake8 and mypy on `app/` |
| `make test` | Run the complete component pytest suite |
| `make coverage` | Run tests with terminal/HTML coverage and enforce 90% |
| `make ci` | Run `fmt`, `lint`, `test`, and `coverage` in sequence |

`make ci` reformats files and runs the test suite once through `make test` and
again through `make coverage`. Review its resulting diff before committing.
The coverage floor is 90%; new or changed code should be covered completely
whenever practical.

CI repeats formatting checks, linting, wheel installation, tests, and coverage
for both projects on Python 3.11, 3.12, and 3.13. If a shared file, packaging,
or GitHub workflow affects both projects, validate both component directories.

For real Runner runtime validation, complement mocked tests with the relevant
diagnostic scripts:

```bash
cd runner
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_gpu.py
uv run scripts/check_runner_resources.py
uv run scripts/check_runner_storage.py
```

## Project-specific testing rules

- Keep tests deterministic: mock DNS, HTTP, SMTP, subprocesses, clocks, and
  external media services instead of using the public network.
- Render Jinja templates directly with Jinja or a rendering helper. Do not use
  `TestClient` solely to test template output.
- Preserve function signatures that tests replace with `monkeypatch`.
- Keep fixtures isolated from production `data/`, storage, logs, and statistics
  files.
- Test failure, timeout, restart, and persistence branches when changing task
  lifecycle code; the happy path alone is not sufficient.
- Prefer focused regression tests close to the behavior being changed.

## Runtime invariants to preserve

Reliability changes deserve extra care. In particular:

- Keep Manager and Runner API contracts and `MAJOR.MINOR` compatibility aligned.
- Preserve atomic runner reservation and multi-instance behavior; never assume
  that a single process owns all runners or tasks.
- Keep persisted task state compact, atomic, and usable after a restart.
- Preserve startup recovery and reconciliation of in-flight tasks.
- Keep completion callbacks and status updates idempotent where retries are
  possible.
- Validate identifiers, URLs, and resolved filesystem paths before using input
  in network or storage operations.
- Never expose tokens, credentials, private URLs, or raw internal errors in
  logs, API responses, fixtures, or documentation.
- Keep the Runner task-handler entry points (`main` and `parse_args`) stable
  when moving implementation code.

Choose the simplest readable implementation that preserves these properties
and passes the relevant tests.

## Preparing a pull request

1. Create a focused branch from `main`.
2. Keep commits scoped and descriptive. Recent history uses prefixes such as
   `feat:`, `fix:`, `test:`, `docs:`, and `chore:`.
3. Add or update regression tests with the implementation.
4. Update component documentation when behavior, configuration, APIs, or
   operations change.
5. Add notable user-facing changes under `Unreleased` in
   `manager/docs/CHANGELOG.md` and/or `runner/docs/CHANGELOG.md`.
6. Run targeted checks, then `make ci` in every affected component when
   possible.
7. Complete the repository pull-request template, including compatibility,
   validation, documentation, and UI screenshots where applicable.

Do not bump component versions in a regular feature or bug-fix pull request.
Version updates, changelog promotion, tags, and publication are handled by the
release workflow described in [RELEASE.md](RELEASE.md).

## Troubleshooting

- Missing pytest, Black, isort, Flake8, or mypy: run `make sync-dev` in the
  affected component.
- `uv` cache permission errors: verify
  `UV_CACHE_DIR=/tmp/esup-runner-uv-cache` in the current shell.
- Imports resolve the wrong `app` package: check the working directory, remove
  the other component from `PYTHONPATH`, and select the matching `.venv`.
- Runner does not register: start the Manager, then check `MANAGER_URL`, the
  matching token, and `MAJOR.MINOR` versions.
- Media or GPU behavior differs from tests: run the Runner diagnostic scripts
  and consult the FFmpeg/GPU documentation.

## Further documentation

- [Manager documentation](manager/docs/README.md)
- [Runner documentation](runner/docs/README.md)
- [Manager operations](manager/docs/OPERATIONS.md)
- [Runner operations](runner/docs/OPERATIONS.md)
- [Manager Docker guide](manager/docs/DOCKER.md)
- [Runner Docker guide](runner/docs/DOCKER.md)
- [Security policy](SECURITY.md)
- [Release automation](RELEASE.md)
