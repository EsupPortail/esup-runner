# Docker Installation Guide

This page describes a full Docker-based installation of the **ESUP Runner Manager**.
It only covers the `manager/` project (not the `runner/`).

## Assumptions

- OS: Debian/Ubuntu-like system
- Installation directory: `/opt/esup-runner`
- Dedicated host user: `esup-runner`

## 1) Create the dedicated host user

Run as `root`:

```bash
adduser esup-runner
adduser esup-runner sudo
```

Then switch to this user:

```bash
su - esup-runner
```

## 2) Install host prerequisites

Run as `root`:

```bash
apt update
apt install -y ca-certificates curl git make docker.io
systemctl enable --now docker
```

Quick verification:

```bash
docker --version
```

## 3) Allow `esup-runner` to run Docker commands

Run as `root`:

```bash
usermod -aG docker esup-runner
```

Apply group changes by opening a new session:

```bash
su - esup-runner
```

Optional verification:

```bash
docker run --rm hello-world
```

## 4) Fetch sources (recommended)

Run as `esup-runner`:

```bash
sudo mkdir -p /opt/esup-runner
sudo chown esup-runner:esup-runner /opt/esup-runner

cd /opt/esup-runner
git clone --filter=blob:none --sparse https://github.com/EsupPortail/esup-runner.git .
git sparse-checkout set manager
# To install **both** the runner and the manager
# git sparse-checkout set manager runner
cd manager
```

Why this step is still useful with **Option B** (GHCR image pull):

- get `.env.example` to create your `.env`
- use helper targets like `make docker-fix-perms`
- keep a standard host path for the bind mount (`/opt/esup-runner/manager/.env`)

If you do not want a local checkout, you can skip this step and:

- create/store `.env` in another local path
- replace `-v /opt/esup-runner/manager/.env:/app/.env:ro` in `docker run` with your own path
- run equivalent permission-fix commands manually (instead of `make docker-fix-perms`)

## 5) Prepare manager configuration

```bash
cp .env.example .env
nano .env
```

If you skipped step 4, create an env file in a path of your choice (example: `/opt/esup-runner/manager.env`) and use that same path later in both `--env-file` and the `/app/.env` bind mount.

At minimum, review:

- `MANAGER_HOST`, `MANAGER_PORT`
- `AUTHORIZED_TOKENS__*`
- `ADMIN_USERS__*`
- `LOG_DIR`
- `RUNNERS_STORAGE_ENABLED` and `RUNNERS_STORAGE_DIR`

Compatibility note: legacy names `LOG_DIRECTORY` and `RUNNERS_STORAGE_PATH` are still accepted.

For Docker deployment with a shared Docker network, set:

- `MANAGER_HOST=esup-runner-manager`

Important:

- `MANAGER_HOST` is used to build `MANAGER_URL`.
- `MANAGER_BIND_HOST` controls the server bind interface (leave empty for default auto behavior).
- `MANAGER_URL` is injected into runner tasks as `completion_callback` (`/task/completion`).
- If `MANAGER_HOST=0.0.0.0`, callbacks from runner may fail in Docker (`http://0.0.0.0:...` points to the runner container itself, not the manager).

## 6) Choose how to get the manager image

You can choose between:

- **Option A:** build locally (current/default workflow)
- **Option B:** pull the image published automatically on GHCR

### Option A) Build locally

From `/opt/esup-runner/manager`:

```bash
make docker-build ESUP_RUNNER_UID=$(id -u) ESUP_RUNNER_GID=$(id -g)
export MANAGER_IMAGE_REF=esup-runner-manager:latest
```

Defaults:

- image name: `esup-runner-manager`
- tag: `latest`
- container user/group: `esup-runner`

Custom image/tag example:

```bash
make docker-build \
  DOCKER_IMAGE=ghcr.io/<github-org>/esup-runner-manager \
  DOCKER_TAG=v1.0.0 \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)

export MANAGER_IMAGE_REF=ghcr.io/<github-org>/esup-runner-manager:v1.0.0
```

If your Docker environment has DNS issues during build, try host networking:

```bash
make docker-build DOCKER_BUILD_NETWORK=host
```

You can also pass additional `docker build` options:

```bash
make docker-build DOCKER_BUILD_OPTS="--progress=plain --no-cache"
```

Example with all common options:

```bash
make docker-build \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g) \
  DOCKER_BUILD_NETWORK=host \
  DOCKER_BUILD_OPTS="--progress=plain"
```

Equivalent raw Docker command:

```bash
docker build -f Dockerfile \
  --build-arg ESUP_RUNNER_UID=$(id -u) \
  --build-arg ESUP_RUNNER_GID=$(id -g) \
  -t esup-runner-manager:latest .
```

### Option B) Pull the published GHCR image

```bash
docker pull ghcr.io/esupportail/esup-runner-manager:latest
export MANAGER_IMAGE_REF=ghcr.io/esupportail/esup-runner-manager:latest
```

You can also pin to a specific release tag:

```bash
# Example
docker pull ghcr.io/esupportail/esup-runner-manager:v1.0.0
export MANAGER_IMAGE_REF=ghcr.io/esupportail/esup-runner-manager:v1.0.0
```

Published tags include `latest`, `vX.Y.Z`, `X.Y`, and `X`.

Important:

- Published images use the default container user/group `UID:GID=1000:1000`.
- For this option, keep `docker-fix-perms` aligned with `1000:1000` unless you rebuild your own image with different UID/GID.
- For production, prefer a pinned version tag (`vX.Y.Z`) instead of `latest`.

## 7) Run the manager container

Create volumes:

```bash
docker volume create esup-runner-manager-logs
docker volume create esup-runner-storage
docker volume create esup-runner-manager-data
```

Create a dedicated Docker network shared with runner containers:

```bash
docker network inspect esup-runner-net >/dev/null 2>&1 || docker network create esup-runner-net
```

Ensure volume ownership matches `esup-runner` (important if volumes already contain root-owned files):

```bash
# Option A (local build with host UID/GID)
make docker-fix-perms ESUP_RUNNER_UID=$(id -u) ESUP_RUNNER_GID=$(id -g)

# Option B (pulled GHCR image defaults to UID/GID 1000)
# Update DOCKER_TAG if you pinned another tag in step 6.
# make docker-fix-perms \
#   DOCKER_IMAGE=ghcr.io/esupportail/esup-runner-manager \
#   DOCKER_TAG=latest \
#   ESUP_RUNNER_UID=1000 \
#   ESUP_RUNNER_GID=1000
```

Raw Docker equivalent (useful if you skipped source checkout):

```bash
MANAGER_IMAGE_REF="${MANAGER_IMAGE_REF:-ghcr.io/esupportail/esup-runner-manager:latest}"

docker run --rm --user root \
  -v esup-runner-manager-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-manager-data:/app/data \
  "$MANAGER_IMAGE_REF" \
  sh -c "chown -R 1000:1000 /var/log/esup-runner /tmp/esup-runner /app/data"
```

Run in background:

```bash
MANAGER_IMAGE_REF="${MANAGER_IMAGE_REF:-esup-runner-manager:latest}"
MANAGER_ENV_FILE="${MANAGER_ENV_FILE:-/opt/esup-runner/manager/.env}"

docker run -d \
  --name esup-runner-manager \
  --network esup-runner-net \
  --restart unless-stopped \
  --env-file "$MANAGER_ENV_FILE" \
  -e MANAGER_HOST=esup-runner-manager \
  -p 8081:8081 \
  -v esup-runner-manager-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-manager-data:/app/data \
  -v "$MANAGER_ENV_FILE":/app/.env:ro \
  "$MANAGER_IMAGE_REF"
```

This container name (`esup-runner-manager`) is resolvable by other containers on
`esup-runner-net` and should be used by runners with:
`MANAGER_URL=http://esup-runner-manager:8081`.

## 8) Verify runtime

Check container state:

```bash
docker ps --filter name=esup-runner-manager
```

Check logs:

```bash
docker logs -f esup-runner-manager
```

Health check example:

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:8081/manager/health"
```

Replace `<AUTHORIZED_TOKEN>` with one of your `AUTHORIZED_TOKENS__*` values.

Manual end-to-end task test (from manager sources):

```bash
cd /opt/esup-runner/manager
RUNNER_API_TOKEN="<AUTHORIZED_TOKEN>" \
RUNNER_MANAGER_URL="http://127.0.0.1:8081" \
uv run scripts/check_pipeline_tasks.py
```

Before running this script:

- Set `RUNNER_API_TOKEN` to one of your manager `AUTHORIZED_TOKENS__*` values.
- Set `RUNNER_MANAGER_URL` to your published manager endpoint.
- If the script runs from another server, avoid `127.0.0.1`; use the real manager host/IP.
- Ensure at least one runner is registered and supports `TASK_TYPE` (default script value: `encoding`).

Inspect mounted data paths (container must be running):

```bash
docker exec -it esup-runner-manager sh
ls -lah /app
ls -lah /app/data
ls -lah /var/log/esup-runner
ls -lah /tmp/esup-runner
```

## 9) Common operations

Stop:

```bash
docker stop esup-runner-manager
```

Start:

```bash
docker start esup-runner-manager
```

Restart:

```bash
docker restart esup-runner-manager
```

Remove container:

```bash
docker rm -f esup-runner-manager
```

## Notes

- The manager listens on port `8081` by default. If you change `MANAGER_PORT` in `.env`, update `-p`.
- Task persistence uses `/app/data` inside the container.
- For production, put a reverse proxy with HTTPS in front of the manager.
- Keep tokens and admin password hashes only in `.env` and do not commit this file.

## Troubleshooting: DNS error during `docker build`

If you see errors like `dns error` or `failed to lookup address information` while downloading Python packages:

1) Validate DNS from a container:

```bash
docker run --rm busybox nslookup files.pythonhosted.org
```

2) Retry with host networking:

```bash
make docker-build DOCKER_BUILD_NETWORK=host
```

3) If DNS still fails, configure Docker daemon DNS (`/etc/docker/daemon.json`) then restart Docker:

```json
{
  "dns": ["1.1.1.1", "8.8.8.8"]
}
```

```bash
sudo systemctl restart docker
```

## Troubleshooting: `PermissionError` on `/app/data/*.tmp`

If logs show:

`PermissionError: [Errno 13] Permission denied: '/app/data/...tmp'`

it means files/volume are not writable by container user `esup-runner`.

Fix:

```bash
cd /opt/esup-runner/manager
make docker-fix-perms ESUP_RUNNER_UID=$(id -u) ESUP_RUNNER_GID=$(id -g)
docker restart esup-runner-manager
```
