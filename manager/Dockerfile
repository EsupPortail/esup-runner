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
COPY app ./app

# Install production dependencies from the lockfile.
RUN uv sync --frozen --no-dev


# Runtime stage: minimal image with non-root execution.
FROM python:3.12-slim AS runtime

WORKDIR /app

# Align container UID/GID with host user when needed (mounted volumes).
ARG ESUP_RUNNER_UID=1000
ARG ESUP_RUNNER_GID=1000

# Runtime defaults (can be overridden via --env-file and/or -e flags).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    MANAGER_HOST=0.0.0.0 \
    MANAGER_PORT=8081 \
    LOG_DIRECTORY=/var/log/esup-runner \
    RUNNERS_STORAGE_PATH=/tmp/esup-runner

# Create dedicated non-root user and writable runtime directories.
RUN groupadd --gid "${ESUP_RUNNER_GID}" esup-runner && \
    useradd --uid "${ESUP_RUNNER_UID}" --gid "${ESUP_RUNNER_GID}" --home-dir /home/esup-runner --create-home --shell /usr/sbin/nologin esup-runner && \
    mkdir -p /var/log/esup-runner /tmp/esup-runner && \
    chown -R esup-runner:esup-runner /var/log/esup-runner /tmp/esup-runner

# Copy the built app and virtual environment with correct ownership.
COPY --from=build --chown=esup-runner:esup-runner /app /app

# Manager task persistence and shared state directory (mount a volume in production).
RUN mkdir -p /app/data && \
    chown -R esup-runner:esup-runner /app

USER esup-runner

EXPOSE 8081

# Launch the manager entrypoint.
CMD ["esup-runner-manager"]
