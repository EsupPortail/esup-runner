# Makefile - Esup-Runner Runner Service
#
# Purpose:
# - Centralize day-to-day commands for dependency sync, lock maintenance,
#   local run, quality checks, and Docker image build.
#
# Supported extras:
# - dev, transcription-gpu, transcription-cpu
#
# Common workflows:
# - Bootstrap environment:
#   make sync
#   make sync-dev
#   make sync-transcription-gpu
#   make sync-transcription-cpu
#   make sync-all
# - Update lock file:
#   make lock
#   make lock EXTRAS=dev,transcription-gpu
#   make lock-upgrade
# - GPU lock profiles:
#   make lock-upgrade-gpu-12
#   make lock-upgrade-gpu-latest
# - Run and quality:
#   make run
#   make ci
#
# Important variables:
# - EXTRAS: comma-separated extras list (example: EXTRAS=dev,transcription-gpu)
# - UV, PYTHON: command wrappers used by all uv/python targets
# - DOCKER_*: image name/tag and container build/runtime options
#
# Notes:
# - uv sync does not install optional dependencies unless extras are provided.
# - Quality commands do not auto-install dev dependencies.
#
# Troubleshooting:
# - Missing dev tools (pytest/ruff/mypy): run `make sync-dev` or `make sync-all`.
# - GPU transcription not available at runtime: run `make check-gpu`.
# - Need a CUDA 12-compatible lock: run `make lock-upgrade-gpu-12`.

UV      ?= uv
PYTHON  ?= $(UV) run python
UV_LINK_MODE ?= copy
CACHE_DIR ?= /home/esup-runner/.cache/esup-runner
UV_CACHE_DIR ?= $(CACHE_DIR)/uv
XDG_CONFIG_HOME ?= $(HOME)/.config
SYSTEMD_USER_DIR ?= $(XDG_CONFIG_HOME)/systemd/user
SYSTEMD_USER_UNIT ?= $(SYSTEMD_USER_DIR)/esup-runner-runner.service

# Ensure every uv invocation (sync/lock/run/tests/linters) uses the runner cache root.
export UV_CACHE_DIR

EXTRAS  ?=

# Docker build variables (can be overridden via env or make args)
DOCKER            ?= docker
DOCKERFILE        ?= Dockerfile
DOCKER_IMAGE      ?= esup-runner-runner
DOCKER_TAG        ?= latest
ESUP_RUNNER_UID   ?= 1000
ESUP_RUNNER_GID   ?= 1000
DOCKER_BUILD_NETWORK ?=
DOCKER_BUILD_OPTS ?=
DOCKER_RUNNER_EXTRA ?=
DOCKER_LOGS_VOLUME ?= esup-runner-runner-logs
DOCKER_STORAGE_VOLUME ?= esup-runner-storage
DOCKER_CACHE_VOLUME ?= esup-runner-cache
DOCKER_LOGS_PATH ?= /var/log/esup-runner
DOCKER_STORAGE_PATH ?= /tmp/esup-runner
DOCKER_CACHE_PATH ?= /home/esup-runner/.cache/esup-runner

# Builds the --extra ... arguments from EXTRAS=dev,transcription-gpu,transcription-cpu
comma := ,
empty :=
space := $(empty) $(empty)
EXTRA_ARGS := $(strip $(foreach e,$(subst $(comma),$(space),$(EXTRAS)),--extra $(e)))

.DEFAULT_GOAL := help

help:
	@echo "Targets:"
	@echo "  make sync [EXTRAS=dev,transcription-gpu,transcription-cpu]  -> uv sync (+ extras)"
	@echo "  make lock [EXTRAS=dev,transcription-gpu,transcription-cpu]  -> uv lock (+ extras)"
	@echo "  make lock-upgrade [EXTRAS=dev,transcription-gpu,transcription-cpu]  -> uv lock --upgrade (+ extras)"
	@echo "  make run  -> run the app launcher"
	@echo "  make check-gpu  -> verify torch CUDA runtime availability"
	@echo "  make init  -> init directories from .env (sudo)"
	@echo "  make create-service  -> install systemd user service (~/.config/systemd/user)"
	@echo "  make ci  -> run local pre-PR checks (fmt + lint + test + coverage)"
	@echo "  make docker-build [DOCKER_IMAGE=... DOCKER_TAG=... ESUP_RUNNER_UID=... ESUP_RUNNER_GID=... DOCKER_RUNNER_EXTRA=transcription-cpu|transcription-gpu DOCKER_BUILD_NETWORK=host DOCKER_BUILD_OPTS='...'] -> build runner Docker image"
	@echo "  make release-dry-run TAG=runner-vX.Y.Z  -> run release.yml locally with act (no push)"
	@echo "  make docker-fix-perms  -> fix Docker volume permissions for esup-runner"
	@echo "  make clean  -> remove .venv and caches"
	@echo ""
	@echo "Shortcuts:"
	@echo "  make sync-dev | make sync-transcription-gpu | make sync-transcription-cpu | make sync-all"
	@echo "  make lock-dev | make lock-transcription-gpu | make lock-transcription-cpu | make lock-all"
	@echo "  make lock-upgrade-gpu-12  -> uv.lock upgrade with CUDA12-compatible torch"
	@echo "  make lock-upgrade-gpu-latest  -> uv.lock upgrade with latest GPU torch stack"
	@echo ""
	@echo "Quality commands (fmt/lint/test/coverage) do NOT auto-sync dev deps."
	@echo "Run: make sync-dev or make sync-all (once) if needed."
	@echo ""
	@echo "Examples:"
	@echo "  make sync"
	@echo "  make sync EXTRAS=transcription-gpu"
	@echo "  make sync EXTRAS=transcription-cpu"
	@echo "  make lock EXTRAS=dev"
	@echo "  make docker-build DOCKER_IMAGE=ghcr.io/<github-org>/esup-runner-runner DOCKER_TAG=v1.0.0 ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"
	@echo "  make docker-build DOCKER_RUNNER_EXTRA=transcription-cpu"
	@echo "  make docker-fix-perms ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"

# ---------- Dependencies ----------

sync:
	@echo "==> uv sync $(EXTRA_ARGS) [UV_LINK_MODE=$(UV_LINK_MODE)]"
	UV_LINK_MODE=$(UV_LINK_MODE) $(UV) sync $(EXTRA_ARGS)

sync-dev:
	@$(MAKE) sync EXTRAS=dev

sync-all:
	@$(MAKE) sync EXTRAS=dev,transcription-gpu

sync-transcription-gpu:
	@$(MAKE) sync EXTRAS=transcription-gpu

sync-transcription-cpu:
	@$(MAKE) sync EXTRAS=transcription-cpu

lock:
	@echo "==> uv lock $(EXTRA_ARGS)"
	@if $(UV) lock --help | grep -Eq -- '--extra([[:space:]]|$$)'; then \
		$(UV) lock $(EXTRA_ARGS); \
	else \
		if [ -n "$(EXTRAS)" ]; then \
			echo "==> warning: this uv version ignores lock extras; running full lock"; \
		fi; \
		$(UV) lock; \
	fi

lock-upgrade:
	@echo "==> uv lock --upgrade $(EXTRA_ARGS)"
	@if $(UV) lock --help | grep -Eq -- '--extra([[:space:]]|$$)'; then \
		$(UV) lock --upgrade $(EXTRA_ARGS); \
	else \
		if [ -n "$(EXTRAS)" ]; then \
			echo "==> warning: this uv version ignores lock extras; running full lock upgrade"; \
		fi; \
		$(UV) lock --upgrade; \
	fi

# lock-upgrade-gpu-12
# Generate a lockfile compatible with CUDA 12 on Linux x86_64.
# Strategy:
# - Temporarily inject torch==2.10.0 in base dependencies for this command only.
# - Run lock upgrade with UV_NO_SOURCES=1 to avoid source-table side effects.
# - Restore pyproject.toml even if the lock command fails.
lock-upgrade-gpu-12:
	@echo "==> uv lock --upgrade with dev + transcription-gpu (temporary torch==2.10.0 pin for CUDA12)"
	@set -e; \
	backup=$$(mktemp); \
	cp pyproject.toml "$$backup"; \
	trap 'cp "$$backup" pyproject.toml; rm -f "$$backup"' EXIT; \
	perl -0777 -i -pe 's/dependencies = \[\n(.*?)\n\]/dependencies = [\n$$1\n    "torch==2.10.0 ; sys_platform == '\''linux'\'' and platform_machine == '\''x86_64'\''",\n]/s' pyproject.toml; \
	if $(UV) lock --help | grep -Eq -- '--extra([[:space:]]|$$)'; then \
		UV_NO_SOURCES=1 $(UV) lock --upgrade --extra dev --extra transcription-gpu; \
	else \
		echo "==> warning: this uv version ignores lock extras; running full lock upgrade"; \
		UV_NO_SOURCES=1 $(UV) lock --upgrade; \
	fi; \
	cp "$$backup" pyproject.toml; \
	rm -f "$$backup"; \
	trap - EXIT

# lock-upgrade-gpu-latest
# Upgrade the lockfile against the latest available GPU torch stack.
# We also disable sources-table resolution to keep behavior predictable across uv versions.
lock-upgrade-gpu-latest:
	@echo "==> uv lock --upgrade with dev + transcription-gpu (latest GPU torch stack)"
	@if $(UV) lock --help | grep -Eq -- '--extra([[:space:]]|$$)'; then \
		UV_NO_SOURCES=1 $(UV) lock --upgrade --extra dev --extra transcription-gpu -P torch; \
	else \
		echo "==> warning: this uv version ignores lock extras; running full lock upgrade without sources and with torch upgrade"; \
		UV_NO_SOURCES=1 $(UV) lock --upgrade -P torch; \
	fi

lock-dev:
	@$(MAKE) lock EXTRAS=dev

lock-all:
	@$(MAKE) lock EXTRAS=dev,transcription-gpu

lock-transcription-gpu:
	@$(MAKE) lock EXTRAS=transcription-gpu

lock-transcription-cpu:
	@$(MAKE) lock EXTRAS=transcription-cpu

# ---------- Execution ----------

run:
	@echo "==> run the app launcher"
	$(UV) run esup-runner-runner

check-gpu:
	@echo "==> checking torch CUDA availability"
	@$(UV) run python -c "import sys; import torch; print(f'torch={torch.__version__}'); print(f'cuda_build={torch.version.cuda}'); ok=torch.cuda.is_available(); print(f'cuda_available={ok}'); print(f'gpu_count={torch.cuda.device_count() if ok else 0}'); sys.exit(0 if ok else 1)"

# ---------- Utilities ----------

check-fmt-deps:
	@$(UV) run python -c "import black, isort" >/dev/null 2>&1 || \
		(echo "==> Formatting tools are missing."; echo "    Run: make sync-dev or make sync-all"; exit 1)

check-test-deps:
	@$(UV) run python -c "import pytest" >/dev/null 2>&1 || \
		(echo "==> Test dependencies are missing."; echo "    Run: make sync-dev or make sync-all"; exit 1)
check-coverage-deps:
	@$(UV) run python -c "import pytest, pytest_cov" >/dev/null 2>&1 || \
		(echo "==> Coverage dependencies are missing (pytest-cov)."; echo "    Run: make sync-dev or make sync-all"; exit 1)

check-lint-deps:
	@$(UV) run python -c "import flake8, mypy" >/dev/null 2>&1 || \
		(echo "==> Lint tools are missing."; echo "    Run: make sync-dev or make sync-all"; exit 1)

init:
	@echo "==> init directories from .env (sudo)"
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(PYTHON) scripts/init.py; \
	else \
		python3 scripts/init.py; \
	fi

create-service:
	@if [ -n "$$SUDO_USER" ] || [ "$$(id -u)" -eq 0 ]; then \
		echo "==> run 'make create-service' as the service user (without sudo/root)."; \
		exit 1; \
	fi
	@echo "==> install systemd user service ($(SYSTEMD_USER_UNIT))"
	mkdir -p "$(SYSTEMD_USER_DIR)"
	cp -f production/esup-runner-runner.service "$(SYSTEMD_USER_UNIT)"
	systemctl --user daemon-reload
	systemctl --user enable --now esup-runner-runner.service

clean:
	@echo "==> remove venv + caches"
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist build

# ---------- Quality ----------

test: check-test-deps
	@echo "==> pytest"
	$(UV) run pytest

coverage: check-coverage-deps
	@echo "==> pytest (coverage)"
	$(UV) run pytest --cov=app --cov-config=.coveragerc --cov-report=term-missing --cov-report=html --cov-fail-under=90

fmt: check-fmt-deps
	@echo "==> format (black + isort)"
	$(UV) run black .
	$(UV) run isort .

fmt-check: check-fmt-deps
	@echo "==> check format (black + isort)"
	$(UV) run black --check .
	$(UV) run isort --check-only .

lint: check-lint-deps
	@echo "==> lint (flake8 + mypy)"
	$(UV) run flake8 --jobs=1 .
	MYPY_CACHE_DIR=/tmp/mypy_cache_$(USER) $(UV) run mypy app

ci:
	@echo "==> local pre-PR checks (fmt + lint + test + coverage)"
	@$(MAKE) fmt
	@$(MAKE) lint
	@$(MAKE) test
	@$(MAKE) coverage

release-dry-run:
	@if [ -z "$(TAG)" ]; then \
		printf '%s\n' "==> TAG is required." "    Example: make release-dry-run TAG=runner-v1.0.0"; \
		exit 1; \
	fi
	@if ! printf '%s\n' "$(TAG)" | grep -Eq '^runner-v[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		printf '%s\n' "==> TAG must follow runner-vMAJOR.MINOR.PATCH." "    Example: make release-dry-run TAG=runner-v1.0.0"; \
		exit 1; \
	fi
	@if ! command -v act >/dev/null 2>&1; then \
		printf '%s\n' \
			"==> act is not installed." \
			"    On Debian, you can install it with:" \
			"    curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash" \
			"    See: https://nektosact.com/installation/"; \
		exit 1; \
	fi
	@if ! command -v $(DOCKER) >/dev/null 2>&1; then \
		printf '%s\n' "==> Docker is required by act but was not found ($(DOCKER))."; \
		exit 1; \
	fi
	@echo "==> release dry-run via act ($(TAG))"
	@set -e; \
	repo_root=$$(git rev-parse --show-toplevel 2>/dev/null || pwd); \
	event_file=$$(mktemp); \
	trap 'rm -f "$$event_file"' EXIT; \
	printf '{\n  "inputs": {\n    "release_tag": "%s"\n  }\n}\n' "$(TAG)" > "$$event_file"; \
	cd "$$repo_root"; \
	act workflow_dispatch -W .github/workflows/release.yml -e "$$event_file" -j docker

docker-build:
	@echo "==> docker build $(DOCKER_IMAGE):$(DOCKER_TAG)"
	$(DOCKER) build $(if $(strip $(DOCKER_BUILD_NETWORK)),--network $(DOCKER_BUILD_NETWORK),) $(DOCKER_BUILD_OPTS) -f $(DOCKERFILE) \
		--build-arg ESUP_RUNNER_UID=$(ESUP_RUNNER_UID) \
		--build-arg ESUP_RUNNER_GID=$(ESUP_RUNNER_GID) \
		$(if $(strip $(DOCKER_RUNNER_EXTRA)),--build-arg RUNNER_EXTRA=$(DOCKER_RUNNER_EXTRA),) \
		-t $(DOCKER_IMAGE):$(DOCKER_TAG) .

docker-fix-perms:
	@echo "==> fix Docker volume permissions ($(ESUP_RUNNER_UID):$(ESUP_RUNNER_GID))"
	$(DOCKER) run --rm --user root \
		-v $(DOCKER_LOGS_VOLUME):$(DOCKER_LOGS_PATH) \
		-v $(DOCKER_STORAGE_VOLUME):$(DOCKER_STORAGE_PATH) \
		-v $(DOCKER_CACHE_VOLUME):$(DOCKER_CACHE_PATH) \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		sh -c "chown -R $(ESUP_RUNNER_UID):$(ESUP_RUNNER_GID) $(DOCKER_LOGS_PATH) $(DOCKER_STORAGE_PATH) $(DOCKER_CACHE_PATH)"

.PHONY: help sync lock lock-upgrade run init clean \
		sync-dev sync-all sync-transcription-gpu sync-transcription-cpu \
		lock-dev lock-transcription-gpu lock-transcription-cpu lock-all lock-upgrade-gpu-12 lock-upgrade-gpu-latest \
		release-dry-run docker-build docker-fix-perms \
		check-fmt-deps check-test-deps check-coverage-deps check-lint-deps \
		test coverage fmt fmt-check lint ci check-gpu
