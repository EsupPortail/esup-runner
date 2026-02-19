# Makefile — Esup-Runner Manager (uv + extras)
#
# Extras: dev
#
# Usage:
#   make sync
#   make sync-dev
#   make sync EXTRAS=dev
#
#   make lock
#   make lock-dev
#   make lock EXTRAS=dev
#
#   make run
#   make test
#   make coverage
#   make fmt
#   make lint
#   make docs
#   make init
#   make clean
#
# Notes:
# - uv sync does not install optional dependencies without --extra.

UV      ?= uv
PYTHON  ?= $(UV) run python

EXTRAS  ?=

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
	@echo "  make run                                        -> run the manager"
	@echo "  make status                                     -> show manager status (best-effort)"
	@echo "  make stop                                       -> stop the manager (best-effort)"
	@echo "  make restart                                    -> restart the manager (systemd or local)"
	@echo "  make test                                       -> run tests"
	@echo "  make coverage                                   -> run tests with coverage"
	@echo "  make fmt                                        -> format code (black + isort)"
	@echo "  make lint                                       -> lint (flake8 + mypy)"
	@echo "  make docs                                       -> generate docs diagrams"
	@echo "  make init                                       -> init directories from .env (sudo)"
	@echo "  make create-service                             -> install systemd service (sudo)"
	@echo "  make clean                                      -> remove .venv and caches"
	@echo ""
	@echo "Shortcuts:"
	@echo "  make sync-dev | lock-dev | sync-docs | lock-docs"
	@echo ""
	@echo "Quality commands (fmt/test/lint) do NOT auto-sync dev deps."
	@echo "Run: make sync-dev (once) if needed."
	@echo ""
	@echo "Examples:"
	@echo "  make sync"
	@echo "  make sync EXTRAS=dev"
	@echo "  make lock EXTRAS=dev"
	@echo "  make run"

# ---------- Dependencies ----------

sync:
	@echo "==> uv sync $(EXTRA_ARGS)"
	$(UV) sync $(EXTRA_ARGS)

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
	@echo "==> manager status"
	@bash scripts/status_manager.sh

stop:
	@echo "==> stop the manager"
	@bash scripts/stop_manager.sh

restart:
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
	@echo "==> install systemd service (sudo)"
	cp -f production/esup-runner-manager.service /etc/systemd/system/esup-runner-manager.service
	systemctl daemon-reload
	systemctl enable esup-runner-manager
	systemctl restart esup-runner-manager

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

docs:
	@echo "==> docs (diagrams)"
	@$(MAKE) sync EXTRAS=docs
	$(PYTHON) scripts/generate_architecture_diagram.py
	$(PYTHON) scripts/generate_tree_diagram.py

.PHONY: help sync lock lock-upgrade run status stop restart init clean create-service docs \
	sync-dev lock-dev sync-docs lock-docs \
	check-fmt-deps check-test-deps check-coverage-deps check-lint-deps \
	test coverage fmt fmt-check lint
