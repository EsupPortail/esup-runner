# Makefile — Esup-Runner (uv + extras)
#
# Extras: dev, transcription-gpu, transcription-cpu
#
# Usage:
#   make sync
#   make sync-dev
#   make sync-transcription-gpu
#   make sync-transcription-cpu
# 	make sync-all
#
#   make lock
#   make lock-transcription-gpu
#   make lock-transcription-cpu
#   make lock EXTRAS=dev,transcription-gpu
#
#   make run
#   make ci
#
# Notes:
# - uv sync does not install optional dependencies without --extra.

UV      ?= uv
PYTHON  ?= $(UV) run python

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
DOCKER_WHISPER_VOLUME ?= esup-runner-whisper-models
DOCKER_LOGS_PATH ?= /var/log/esup-runner
DOCKER_STORAGE_PATH ?= /tmp/esup-runner
DOCKER_WHISPER_MODELS_PATH ?= /home/esup-runner/.cache/esup-runner/whisper-models

# Builds the --extra ... arguments from EXTRAS=dev,transcription-gpu,transcription-cpu
comma := ,
empty :=
space := $(empty) $(empty)
EXTRA_ARGS := $(strip $(foreach e,$(subst $(comma),$(space),$(EXTRAS)),--extra $(e)))

.DEFAULT_GOAL := help

help:
	@echo "Targets:"
	@echo "  make sync [EXTRAS=dev,transcription-gpu,transcription-cpu]	-> uv sync (+ extras)"
	@echo "  make lock [EXTRAS=dev,transcription-gpu,transcription-cpu]	-> uv lock (+ extras)"
	@echo "  make lock-upgrade [EXTRAS=dev,transcription-gpu,transcription-cpu] -> uv lock --upgrade (+ extras)"
	@echo "  make run                                       -> run the app launcher"
	@echo "  make init                                      -> init directories from .env (sudo)"
	@echo "  make create-service                            -> install systemd service (sudo)"
	@echo "  make ci                                        -> run local pre-PR checks (fmt + lint + test + coverage)"
	@echo "  make docker-build [DOCKER_IMAGE=... DOCKER_TAG=... ESUP_RUNNER_UID=... ESUP_RUNNER_GID=... DOCKER_RUNNER_EXTRA=transcription-cpu|transcription-gpu DOCKER_BUILD_NETWORK=host DOCKER_BUILD_OPTS='...'] -> build runner Docker image"
	@echo "  make docker-fix-perms                          -> fix Docker volume permissions for esup-runner"
	@echo "  make clean                                     -> remove .venv and caches"
	@echo ""
	@echo "Shortcuts:"
	@echo "  make sync-dev | sync-transcription-gpu | sync-transcription-cpu | make sync-all"
	@echo "  make lock-dev | lock-transcription-gpu | lock-transcription-cpu | make lock-all"
	@echo ""
	@echo "Quality commands (fmt/lint/test/coverage) do NOT auto-sync dev deps."
	@echo "Run: make sync-dev or make sync-all (once) if needed."
	@echo ""
	@echo "Examples:"
	@echo "  make sync"
	@echo "  make sync EXTRAS=transcription-gpu"
	@echo "  make sync EXTRAS=transcription-cpu"
	@echo "  make lock EXTRAS=dev"
	@echo "  make docker-build DOCKER_IMAGE=ghcr.io/<github-org>/esup-runner-runner DOCKER_TAG=v0.9.0 ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"
	@echo "  make docker-build DOCKER_RUNNER_EXTRA=transcription-cpu"
	@echo "  make docker-fix-perms ESUP_RUNNER_UID=$$(id -u) ESUP_RUNNER_GID=$$(id -g)"

# ---------- Dependencies ----------

sync:
	@echo "==> uv sync $(EXTRA_ARGS)"
	$(UV) sync $(EXTRA_ARGS)

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
	@echo "==> install systemd service (sudo)"
	cp -f production/esup-runner-runner.service /etc/systemd/system/esup-runner-runner.service
	systemctl daemon-reload
	systemctl enable esup-runner-runner
	systemctl restart esup-runner-runner

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
		-v $(DOCKER_WHISPER_VOLUME):$(DOCKER_WHISPER_MODELS_PATH) \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		sh -c "chown -R $(ESUP_RUNNER_UID):$(ESUP_RUNNER_GID) $(DOCKER_LOGS_PATH) $(DOCKER_STORAGE_PATH) $(DOCKER_WHISPER_MODELS_PATH)"

.PHONY: help sync lock lock-upgrade run init clean \
		sync-dev sync-all sync-transcription-gpu sync-transcription-cpu \
		lock-dev lock-transcription-gpu lock-transcription-cpu lock-all \
		docker-build docker-fix-perms \
		check-fmt-deps check-test-deps check-coverage-deps check-lint-deps \
		test coverage fmt fmt-check lint ci
