# Makefile — Esup-Runner (uv + extras)
#
# Extras: dev, transcription
#
# Usage:
#   make sync
#   make sync-dev
#   make sync-transcription
# 	make sync-all
#
#   make lock
#   make lock-transcription
#   make lock EXTRAS=dev,transcription
#
#   make run
#
# Notes:
# - uv sync does not install optional dependencies without --extra.

UV      ?= uv
PYTHON  ?= $(UV) run python

EXTRAS  ?=

# Builds the --extra ... arguments from EXTRAS=dev,transcription
comma := ,
empty :=
space := $(empty) $(empty)
EXTRA_ARGS := $(strip $(foreach e,$(subst $(comma),$(space),$(EXTRAS)),--extra $(e)))

.DEFAULT_GOAL := help

help:
	@echo "Targets:"
	@echo "  make sync [EXTRAS=dev,transcription]   		-> uv sync (+ extras)"
	@echo "  make lock [EXTRAS=dev,transcription]   		-> uv lock (+ extras)"
	@echo "  make run                                       -> run the app launcher"
	@echo "  make init                                      -> init directories from .env (sudo)"
	@echo "  make create-service                            -> install systemd service (sudo)"
	@echo "  make clean                                     -> remove .venv and caches"
	@echo ""
	@echo "Shortcuts:"
	@echo "  make sync-dev | sync-transcription | make sync-all"
	@echo "  make lock-dev | lock-transcription | make lock-all"
	@echo ""
	@echo "Quality commands (fmt/test/lint) do NOT auto-sync dev deps."
	@echo "Run: make sync-dev or make sync-all (once) if needed."
	@echo ""
	@echo "Examples:"
	@echo "  make sync"
	@echo "  make sync EXTRAS=transcription"
	@echo "  make lock EXTRAS=dev"

# ---------- Dependencies ----------

sync:
	@echo "==> uv sync $(EXTRA_ARGS)"
	$(UV) sync $(EXTRA_ARGS)

sync-dev:
	@$(MAKE) sync EXTRAS=dev

sync-all:
	@$(MAKE) sync EXTRAS=dev,transcription

sync-transcription:
	@$(MAKE) sync EXTRAS=transcription

lock:
	@echo "==> uv lock $(EXTRA_ARGS)"
	$(UV) lock $(EXTRA_ARGS)

lock-dev:
	@$(MAKE) lock EXTRAS=dev

lock-all:
	@$(MAKE) lock EXTRAS=dev,transcription

lock-transcription:
	@$(MAKE) lock EXTRAS=transcription

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
	$(UV) run flake8 .
	MYPY_CACHE_DIR=/tmp/mypy_cache_$(USER) $(UV) run mypy app

.PHONY: help sync lock run init clean \
		sync-dev sync-all sync-transcription \
		lock-dev lock-transcription lock-all \
		check-fmt-deps check-test-deps check-coverage-deps check-lint-deps \
		test coverage fmt fmt-check lint
