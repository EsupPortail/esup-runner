# Makefile - Esup-Runner Manager Service
#
# Purpose:
# - Provide a single command entrypoint for dependency sync, lock management,
#   service lifecycle commands, tests/quality, docs generation, and Docker build.
#
# Supported extras:
# - dev
#
# Common workflows:
# - Bootstrap environment:
#   make sync
#   make sync-dev
#   make sync EXTRAS=dev
# - Update lock file:
#   make lock
#   make lock-dev
#   make lock-upgrade EXTRAS=dev
# - Run and operations:
#   make run
#   make status
#   make stop
#   make restart
# - Quality and docs:
#   make test
#   make coverage
#   make fmt
#   make lint
#   make ci
#   make docs
#
# Important variables:
# - EXTRAS: comma-separated extras list (example: EXTRAS=dev)
# - UV, PYTHON: command wrappers used by all uv/python targets
# - DOCKER_*: image name/tag and container build/runtime options
#
# Notes:
# - uv sync does not install optional dependencies unless extras are provided.
# - Quality commands do not auto-install dev dependencies.
#
# Troubleshooting:
# - Missing dev tools (pytest/black/isort/mypy): run `make sync-dev`.
# - Optional deps not installed: use `make sync EXTRAS=dev` explicitly.
# - After lock update, refresh env with `make sync`.

UV      ?= uv
PYTHON  ?= $(UV) run python
UV_LINK_MODE ?= copy
CACHE_DIR ?= /home/esup-runner/.cache/esup-runner
UV_CACHE_DIR ?= $(CACHE_DIR)/uv
XDG_CONFIG_HOME ?= $(HOME)/.config
SYSTEMD_USER_DIR ?= $(XDG_CONFIG_HOME)/systemd/user
SYSTEMD_USER_UNIT ?= $(SYSTEMD_USER_DIR)/esup-runner-manager.service

# Ensure every uv invocation (sync/lock/run/tests/linters) uses the manager cache root.
export UV_CACHE_DIR

EXTRAS  ?=

# ---------------------------------------------------------------------------
# Readability helpers
# ---------------------------------------------------------------------------
# Regex used to detect whether the local uv version supports `--extra` on lock.
UV_LOCK_EXTRA_REGEX := --extra([[:space:]]|$$)

# Docker build variables (can be overridden via env or make args)
DOCKER            ?= docker
DOCKERFILE        ?= Dockerfile
DOCKER_IMAGE      ?= esup-runner-manager
DOCKER_TAG        ?= latest
ESUP_RUNNER_UID   ?= 1000
ESUP_RUNNER_GID   ?= 1000
DOCKER_BUILD_NETWORK ?=
DOCKER_BUILD_OPTS ?=
DOCKER_LOGS_VOLUME ?= esup-runner-manager-logs
DOCKER_STORAGE_VOLUME ?= esup-runner-storage
DOCKER_DATA_VOLUME ?= esup-runner-manager-data

# Builds the --extra ... arguments from EXTRAS=dev,docs
comma := ,
empty :=
space := $(empty) $(empty)
EXTRA_ARGS := $(strip $(foreach e,$(subst $(comma),$(space),$(EXTRAS)),--extra $(e)))


.DEFAULT_GOAL := help

help:
	@echo "Targets:"
	@echo "  make sync [EXTRAS=dev]                          -> uv sync (+ extras)"
	@echo "  make lock [EXTRAS=dev]                          -> uv lock (+ extras)"
	@echo "  make lock-upgrade [EXTRAS=dev]                  -> uv lock --upgrade (+ extras)"
	@echo "  make run  -> run the manager"
	@echo "  make status  -> show manager status (best-effort)"
	@echo "  make stop  -> stop the manager (best-effort)"
	@echo "  make restart  -> restart the manager (systemd or local)"
	@echo "  make test  -> run tests"
	@echo "  make coverage  -> run tests with coverage"
	@echo "  make fmt  -> format code (black + isort)"
	@echo "  make lint  -> lint (flake8 + mypy)"
	@echo "  make ci  -> run local pre-PR checks (fmt + lint + test + coverage)"
	@echo "  make docs  -> generate docs diagrams"
	@echo "  make docker-build [DOCKER_IMAGE=... DOCKER_TAG=... ESUP_RUNNER_UID=... ESUP_RUNNER_GID=... DOCKER_BUILD_NETWORK=host DOCKER_BUILD_OPTS='...']  -> build manager Docker image"
	@echo "  make release-dry-run TAG=manager-vX.Y.Z  -> run release.yml locally with act (no push)"
	@echo "  make docker-fix-perms  -> fix Docker volume permissions for esup-runner"
	@echo "  make init  -> init directories from .env (sudo)"
	@echo "  make create-service  -> install systemd user service (~/.config/systemd/user)"
	@echo "  make clean  -> remove .venv and caches"
	@echo ""
	@echo "Shortcuts:"
	@echo "  make sync-dev | make lock-dev | make sync-docs | make lock-docs"
	@echo ""
	@echo "Quality commands (fmt/lint/test/coverage) do NOT auto-sync dev deps."
	@echo "Run: make sync-dev (once) if needed."
	@echo ""
	@echo "Examples:"
	@echo "  make sync"
	@echo "  make sync EXTRAS=dev"
	@echo "  make lock EXTRAS=dev"
	@echo "  make run"
	@echo "  make docker-build DOCKER_IMAGE=ghcr.io/<github-org>/esup-runner-manager DOCKER_TAG=v1.0.0 ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"
	@echo "  make docker-build DOCKER_BUILD_NETWORK=host"
	@echo "  make docker-fix-perms ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"

# ---------- Dependencies ----------

sync:
	@echo "==> uv sync $(EXTRA_ARGS) [UV_LINK_MODE=$(UV_LINK_MODE)]"
	UV_LINK_MODE=$(UV_LINK_MODE) $(UV) sync $(EXTRA_ARGS)

lock:
	# lock
	# Create or refresh uv.lock for the selected extras.
	# Falls back to a full lock when the installed uv version does not support lock extras.
	@echo "==> uv lock $(EXTRA_ARGS)"
	@if $(UV) lock --help | grep -Eq -- '$(UV_LOCK_EXTRA_REGEX)'; then \
		$(UV) lock $(EXTRA_ARGS); \
	else \
		if [ -n "$(EXTRAS)" ]; then \
			echo "==> warning: this uv version ignores lock extras; running full lock"; \
		fi; \
		$(UV) lock; \
	fi

lock-upgrade:
	# lock-upgrade
	# Upgrade dependencies in uv.lock, optionally scoped by extras.
	# Falls back to a full lock upgrade when lock extras are unsupported.
	@echo "==> uv lock --upgrade $(EXTRA_ARGS)"
	@if $(UV) lock --help | grep -Eq -- '$(UV_LOCK_EXTRA_REGEX)'; then \
		$(UV) lock --upgrade $(EXTRA_ARGS); \
	else \
		if [ -n "$(EXTRAS)" ]; then \
			echo "==> warning: this uv version ignores lock extras; running full lock upgrade"; \
		fi; \
		$(UV) lock --upgrade; \
	fi

sync-dev:
	@$(MAKE) sync EXTRAS=dev

lock-dev:
	@$(MAKE) lock EXTRAS=dev

sync-docs:
	@$(MAKE) sync EXTRAS=docs

lock-docs:
	@$(MAKE) lock EXTRAS=docs

# ---------- Execution ----------

run:
	# run
	# Start the manager process and normalize Ctrl+C (130) to a clean exit.
	@echo "==> run the manager"
	@set +e; \
	$(UV) run esup-runner-manager; \
	code=$$?; \
	if [ $$code -eq 130 ]; then \
		echo "==> manager stopped (Ctrl+C)"; \
		exit 0; \
	fi; \
	exit $$code

status:
	# status
	# Best-effort status report via helper script.
	@echo "==> manager status"
	@bash scripts/status_manager.sh

stop:
	# stop
	# Best-effort stop via helper script.
	@echo "==> stop the manager"
	@bash scripts/stop_manager.sh

restart:
	# restart
	# Restart via helper script (systemd-aware when available).
	@echo "==> restart the manager"
	@bash scripts/restart_manager.sh

# ---------- Utilities ----------

check-fmt-deps:
	@$(UV) run python -c "import black, isort" >/dev/null 2>&1 || \
		(echo "==> Formatting tools are missing."; echo "    Run: make sync-dev"; exit 1)

check-test-deps:
	@$(UV) run python -c "import pytest" >/dev/null 2>&1 || \
		(echo "==> Test dependencies are missing."; echo "    Run: make sync-dev"; exit 1)

check-coverage-deps:
	@$(UV) run python -c "import pytest, pytest_cov" >/dev/null 2>&1 || \
		(echo "==> Coverage dependencies are missing (pytest-cov)."; echo "    Run: make sync-dev"; exit 1)

check-lint-deps:
	@$(UV) run python -c "import flake8, mypy" >/dev/null 2>&1 || \
		(echo "==> Lint tools are missing."; echo "    Run: make sync-dev"; exit 1)

init:
	@echo "==> init directories from .env"
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(PYTHON) scripts/init.py; \
	else \
		python3 scripts/init.py; \
	fi

clean:
	@echo "==> remove venv + caches"
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist build

create-service:
	@if [ -n "$$SUDO_USER" ] || [ "$$(id -u)" -eq 0 ]; then \
		echo "==> run 'make create-service' as the service user (without sudo/root)."; \
		exit 1; \
	fi
	@echo "==> install systemd user service ($(SYSTEMD_USER_UNIT))"
	mkdir -p "$(SYSTEMD_USER_DIR)"
	cp -f production/esup-runner-manager.service "$(SYSTEMD_USER_UNIT)"
	@if systemctl --user show-environment >/dev/null 2>&1; then \
		systemctl --user daemon-reload; \
		systemctl --user enable --now esup-runner-manager.service; \
		echo "==> service installed and started"; \
	else \
		echo "==> service file installed, but systemd --user bus is unavailable in this session."; \
		echo "    This can happen in shells started with su - or sudo -u <user> -i."; \
		echo "    Start a user session with systemd (or enable linger), then run:"; \
		echo "    systemctl --user daemon-reload"; \
		echo "    systemctl --user enable --now esup-runner-manager.service"; \
	fi

# ---------- Quality ----------

test: check-test-deps
	@echo "==> pytest"
	$(UV) run pytest

coverage: check-coverage-deps
	@echo "==> pytest (coverage)"
	$(UV) run pytest --cov=app --cov-report=term-missing --cov-report=html --cov-fail-under=90

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
	$(UV) run flake8 .
	$(UV) run mypy app

ci:
	@echo "==> local pre-PR checks (fmt + lint + test + coverage)"
	@$(MAKE) fmt
	@$(MAKE) lint
	@$(MAKE) test
	@$(MAKE) coverage

docs:
	# docs
	# Generate architecture and tree diagrams using project scripts.
	@echo "==> docs (diagrams)"
	@$(MAKE) sync EXTRAS=docs
	$(PYTHON) scripts/generate_architecture_diagram.py
	$(PYTHON) scripts/generate_tree_diagram.py

release-dry-run:
	@if [ -z "$(TAG)" ]; then \
		printf '%s\n' "==> TAG is required." "    Example: make release-dry-run TAG=manager-v1.0.0"; \
		exit 1; \
	fi
	@if ! printf '%s\n' "$(TAG)" | grep -Eq '^manager-v[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		printf '%s\n' "==> TAG must follow manager-vMAJOR.MINOR.PATCH." "    Example: make release-dry-run TAG=manager-v1.0.0"; \
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
		-t $(DOCKER_IMAGE):$(DOCKER_TAG) .

docker-fix-perms:
	@echo "==> fix Docker volume permissions ($(ESUP_RUNNER_UID):$(ESUP_RUNNER_GID))"
	$(DOCKER) run --rm --user root \
		-v $(DOCKER_LOGS_VOLUME):/var/log/esup-runner \
		-v $(DOCKER_STORAGE_VOLUME):/tmp/esup-runner \
		-v $(DOCKER_DATA_VOLUME):/app/data \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		sh -c "chown -R $(ESUP_RUNNER_UID):$(ESUP_RUNNER_GID) /var/log/esup-runner /tmp/esup-runner /app/data"

.PHONY: help sync lock lock-upgrade run status stop restart init clean create-service docs \
	sync-dev lock-dev sync-docs lock-docs \
	release-dry-run docker-build docker-fix-perms \
	check-fmt-deps check-test-deps check-coverage-deps check-lint-deps \
	test coverage fmt fmt-check lint ci
