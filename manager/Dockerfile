# syntax=docker/dockerfile:1.7

# Build stage: resolve and install Python dependencies into /app/.venv.
FROM python:3.12-slim AS build

# Copy uv binaries from the official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Precompile bytecode and copy files instead of symlinks for portability.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy only project metadata and application code required for install.
COPY pyproject.toml uv.lock LICENSE launcher.py ./
COPY docs/README.md ./docs/README.md
COPY app ./app

# Install production dependencies from the lockfile.
RUN uv sync --frozen --no-dev


# Runtime stage: minimal image with non-root execution.
FROM python:3.12-slim AS runtime

WORKDIR /app

# Align container UID/GID with host user when needed (mounted volumes).
ARG ESUP_RUNNER_UID=1000
ARG ESUP_RUNNER_GID=1000
ARG SERVICE_USER=esup-runner

ENV SERVICE_USER=${SERVICE_USER} \
    HOME=/home/${SERVICE_USER}

# Runtime defaults (can be overridden via --env-file and/or -e flags).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    MANAGER_PROTOCOL=http \
    MANAGER_HOST=127.0.0.1 \
    MANAGER_BIND_HOST=0.0.0.0 \
    MANAGER_PORT=8081 \
    LOG_DIR=/var/log/esup-runner \
    RUNNERS_STORAGE_DIR=/tmp/esup-runner \
    CACHE_DIR=${HOME}/.cache/esup-runner \
    UV_CACHE_DIR=${CACHE_DIR}/uv

# Create dedicated non-root user and writable runtime directories.
RUN groupadd --gid "${ESUP_RUNNER_GID}" "${SERVICE_USER}" && \
    useradd --uid "${ESUP_RUNNER_UID}" --gid "${ESUP_RUNNER_GID}" --home-dir "${HOME}" --create-home --shell /usr/sbin/nologin "${SERVICE_USER}" && \
    mkdir -p /var/log/esup-runner /tmp/esup-runner "${UV_CACHE_DIR}" && \
    chown -R "${SERVICE_USER}:${SERVICE_USER}" /var/log/esup-runner /tmp/esup-runner "${HOME}"

# Copy the built app and virtual environment with correct ownership.
COPY --from=build --chown=${SERVICE_USER}:${SERVICE_USER} /app /app

# Manager task persistence and shared state directory (mount a volume in production).
RUN mkdir -p /app/data && \
    chown -R "${SERVICE_USER}:${SERVICE_USER}" /app

USER ${SERVICE_USER}

EXPOSE 8081

# Launch the manager entrypoint.
CMD ["esup-runner-manager"]
