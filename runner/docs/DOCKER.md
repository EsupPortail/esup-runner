# Docker Installation Guide

This page describes a full Docker-based installation of the **ESUP Runner Runner**.
It only covers the `runner/` project (not the `manager/`).

## Assumptions

- OS: Debian/Ubuntu-like system
- Installation directory: `/opt/esup-runner`
- Dedicated host user: `esup-runner`
- A manager is already installed and reachable from the runner container

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
git sparse-checkout set runner update-stack.sh
# To install **both** the runner and the manager
# git sparse-checkout set manager runner update-stack.sh
cd runner
```

Why this step is still useful with **Option B** (GHCR image pull):

- get `.env.example` to create your `.env`
- use helper targets like `make docker-fix-perms`
- keep a standard host path for the bind mount (`/opt/esup-runner/runner/.env`)

If you do not want a local checkout, you can skip this step and:

- create/store `.env` in another local path
- replace `-v /opt/esup-runner/runner/.env:/app/.env:ro` in `docker run` with your own path
- run equivalent permission-fix commands manually (instead of `make docker-fix-perms`)

## 5) Prepare runner configuration

```bash
cp .env.example .env
nano .env
```

If you skipped step 4, create an env file in a path of your choice (example: `/opt/esup-runner/runner.env`) and use that same path later in both `--env-file` and the `/app/.env` bind mount.

At minimum, review:

- `MANAGER_URL`
- `RUNNER_TOKEN`
- `RUNNER_HOST`, `RUNNER_BASE_PORT`, `RUNNER_TASK_TYPES`
- `STORAGE_DIR`
- `LOG_DIR`
- `ENCODING_TYPE`

Compatibility note: legacy variable `LOG_DIRECTORY` is still accepted.

For Docker deployment with a shared Docker network, set:

- `RUNNER_HOST=esup-runner-runner`
- `MANAGER_URL=http://esup-runner-manager:8081`

The runner and manager containers must be on the same Docker network
(recommended: `esup-runner-net`) for this hostname-based URL to work.

Important:

- `RUNNER_HOST` is advertised to the manager and used for manager -> runner calls.
- Do not use `RUNNER_HOST=0.0.0.0` in Docker, otherwise the manager will try to call `http://0.0.0.0:<port>` and task dispatch will fail.
- Keep `--name esup-runner-runner` aligned with `RUNNER_HOST=esup-runner-runner`.

### Shared volume alignment (`esup-runner-storage`)

When using manager shared-storage mode:

- Manager: `RUNNERS_STORAGE_ENABLED=true`
- Manager: `RUNNERS_STORAGE_DIR` must be exactly equal to runner `STORAGE_DIR`
- Both containers must mount the same Docker volume name (recommended: `esup-runner-storage`) at that path

Compatibility note: manager legacy variable `RUNNERS_STORAGE_PATH` is still accepted.

Example with defaults:

- Runner `.env`: `STORAGE_DIR=/tmp/esup-runner`
- Manager `.env`: `RUNNERS_STORAGE_DIR=/tmp/esup-runner`
- Docker mount on both containers: `-v esup-runner-storage:/tmp/esup-runner`

If you change `STORAGE_DIR`, update both the manager setting and Docker mount target accordingly.

## 6) Choose how to get the runner image

You can choose between:

- **Option A:** build locally (current/default workflow)
- **Option B:** pull the image published automatically on GHCR

### Option A) Build locally

From `/opt/esup-runner/runner`:

```bash
make docker-build ESUP_RUNNER_UID=$(id -u) ESUP_RUNNER_GID=$(id -g)
export RUNNER_IMAGE_REF=esup-runner-runner:latest
```

Defaults:

- image name: `esup-runner-runner`
- tag: `latest`
- container user/group: `esup-runner`
- install profile: base dependencies (encoding/studio)

Build with transcription extra:

```bash
# CPU transcription extra
make docker-build \
  DOCKER_RUNNER_EXTRA=transcription-cpu \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)

# GPU transcription extra
make docker-build \
  DOCKER_RUNNER_EXTRA=transcription-gpu \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)
```

Disk space note (transcription only):

- This high disk usage is specific to transcription builds (`DOCKER_RUNNER_EXTRA=transcription-cpu` or `transcription-gpu`), because `openai-whisper` pulls `torch` (and GPU-related CUDA wheels in GPU mode).
- The default build (encoding/studio only, without transcription extra) is much smaller and is usually not affected by this issue.
- Free space is required on the host partition that stores Docker build data (`DockerRootDir`, usually `/var/lib/docker` on `/var` or `/`).
- Check the exact location and available space with:

```bash
docker info --format 'DockerRootDir={{.DockerRootDir}}'
df -h "$(docker info --format '{{.DockerRootDir}}')"
```

Custom image/tag example:

```bash
make docker-build \
  DOCKER_IMAGE=ghcr.io/<github-org>/esup-runner-runner \
  DOCKER_TAG=v1.0.0 \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g)

export RUNNER_IMAGE_REF=ghcr.io/<github-org>/esup-runner-runner:v1.0.0
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
  DOCKER_RUNNER_EXTRA=transcription-cpu \
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
  -t esup-runner-runner:latest .
```

### Option B) Pull the published GHCR image

```bash
docker pull ghcr.io/esupportail/esup-runner-runner:latest
export RUNNER_IMAGE_REF=ghcr.io/esupportail/esup-runner-runner:latest
```

You can also pin to a specific release tag:

```bash
# Example
docker pull ghcr.io/esupportail/esup-runner-runner:v1.0.0
export RUNNER_IMAGE_REF=ghcr.io/esupportail/esup-runner-runner:v1.0.0
```

Published tags include `latest`, `vX.Y.Z`, `X.Y`, and `X`.

Important:

- The published runner image is built with the default profile (encoding/studio).
- If you need `transcription-cpu` or `transcription-gpu`, use **Option A** with `DOCKER_RUNNER_EXTRA=...`.
- Published images use the default container user/group `UID:GID=1000:1000`.
- For this option, keep `docker-fix-perms` aligned with `1000:1000` unless you rebuild your own image with different UID/GID.
- For production, prefer a pinned version tag (`vX.Y.Z`) instead of `latest`.

## 7) Run the runner container

Create volumes:

```bash
docker volume create esup-runner-runner-logs
docker volume create esup-runner-storage
docker volume create esup-runner-cache
```

Create (or reuse) the Docker network shared with the manager:

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
#   DOCKER_IMAGE=ghcr.io/esupportail/esup-runner-runner \
#   DOCKER_TAG=latest \
#   ESUP_RUNNER_UID=1000 \
#   ESUP_RUNNER_GID=1000
```

Raw Docker equivalent (useful if you skipped source checkout):

```bash
RUNNER_IMAGE_REF="${RUNNER_IMAGE_REF:-ghcr.io/esupportail/esup-runner-runner:latest}"

docker run --rm --user root \
  -v esup-runner-runner-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-cache:/home/esup-runner/.cache/esup-runner \
  "$RUNNER_IMAGE_REF" \
  sh -c "chown -R 1000:1000 /var/log/esup-runner /tmp/esup-runner /home/esup-runner/.cache/esup-runner"
```

If you changed `STORAGE_DIR` or `CACHE_DIR`, adjust mount paths in `docker-fix-perms`:

```bash
make docker-fix-perms \
  ESUP_RUNNER_UID=$(id -u) \
  ESUP_RUNNER_GID=$(id -g) \
  DOCKER_STORAGE_PATH=/path/from/STORAGE_DIR \
  DOCKER_CACHE_PATH=/path/from/CACHE_DIR
```

If you use **Option B** (GHCR image), also add:
`DOCKER_IMAGE=ghcr.io/esupportail/esup-runner-runner DOCKER_TAG=<your-tag>`.

Run in background:

```bash
RUNNER_IMAGE_REF="${RUNNER_IMAGE_REF:-esup-runner-runner:latest}"
RUNNER_ENV_FILE="${RUNNER_ENV_FILE:-/opt/esup-runner/runner/.env}"

docker run -d \
  --name esup-runner-runner \
  --network esup-runner-net \
  --restart unless-stopped \
  --env-file "$RUNNER_ENV_FILE" \
  -e RUNNER_HOST=esup-runner-runner \
  -p 8082:8082 \
  -v esup-runner-runner-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-cache:/home/esup-runner/.cache/esup-runner \
  -v "$RUNNER_ENV_FILE":/app/.env:ro \
  "$RUNNER_IMAGE_REF"
```

Notes:
- Mounting `CACHE_DIR` keeps Whisper, translation, and uv caches across container recreations.

- If `RUNNER_BASE_PORT` in `.env` is not `8082`, update `-p`.
- If `RUNNER_INSTANCES>1`, publish the full port range (`RUNNER_BASE_PORT ... RUNNER_BASE_PORT + RUNNER_INSTANCES - 1`).
- Example: if `RUNNER_BASE_PORT=8082` and `RUNNER_INSTANCES=3`, publish `8082`, `8083`, and `8084` (for example `-p 8082-8084:8082-8084`).
- If `STORAGE_DIR` is not `/tmp/esup-runner`, update the volume target in `-v esup-runner-storage:...`.
- `MANAGER_URL` should target the manager container name on the same network, for example `http://esup-runner-manager:8081`.
- `RUNNER_HOST` should be a manager-reachable hostname on the same network (for example `esup-runner-runner`), not `0.0.0.0`.

GPU runtime example (NVIDIA):

```bash
RUNNER_IMAGE_REF="${RUNNER_IMAGE_REF:-esup-runner-runner:latest}"
RUNNER_ENV_FILE="${RUNNER_ENV_FILE:-/opt/esup-runner/runner/.env}"

docker run -d \
  --name esup-runner-runner \
  --network esup-runner-net \
  --restart unless-stopped \
  --gpus all \
  --env-file "$RUNNER_ENV_FILE" \
  -e RUNNER_HOST=esup-runner-runner \
  -p 8082:8082 \
  -v esup-runner-runner-logs:/var/log/esup-runner \
  -v esup-runner-storage:/tmp/esup-runner \
  -v esup-runner-cache:/home/esup-runner/.cache/esup-runner \
  -v "$RUNNER_ENV_FILE":/app/.env:ro \
  "$RUNNER_IMAGE_REF"
```

## 8) Verify runtime

Check container state:

```bash
docker ps --filter name=esup-runner-runner
```

Check logs:

```bash
docker logs -f esup-runner-runner
```

Health check example:

```bash
curl "http://127.0.0.1:8082/runner/ping"
```

Inspect mounted data paths (container must be running):

```bash
docker exec -it esup-runner-runner sh
ls -lah /var/log/esup-runner
ls -lah /tmp/esup-runner
ls -lah /home/esup-runner/.cache/esup-runner
```

## 9) Common operations

Stop:

```bash
docker stop esup-runner-runner
```

Start:

```bash
docker start esup-runner-runner
```

Restart:

```bash
docker restart esup-runner-runner
```

Remove container:

```bash
docker rm -f esup-runner-runner
```

## Notes

- The image includes CPU FFmpeg support and the `time` binary required by encoding scripts.
- Keep runner tokens and manager URLs only in `.env` and do not commit this file.
- For production, place manager and runner behind network controls and limit exposed ports.

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
