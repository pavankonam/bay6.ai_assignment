PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python
COV_MIN ?= 85
PYTEST_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help install seed run run-offline eval-fake test test-llm test-deterministic lint typecheck format check \
        demo graph eval clean reset

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

$(PY):
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip

install: $(PY) ## Create .venv and install pinned dependencies (incl. dev tools)
	$(PY) -m pip install --quiet -r requirements.lock
	$(PY) -m pip install --quiet --no-deps -e .

seed: ## Load seed policies/claims into data/app.db (idempotent)
	$(PY) -m app.seed

run: ## Start the API (single worker, A8); settings come from the environment / .env
	$(BIN)/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

run-offline: ## Start the API with the fake LLM and APP_TODAY=2026-10-22 (no API key needed)
	LLM_PROVIDER=fake APP_TODAY=2026-10-22 $(BIN)/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

test-llm: ## Full test suite, TOOL_SELECTION_MODE=llm, fake LLM, coverage gate
	TOOL_SELECTION_MODE=llm $(PY) -m pytest --cov=app --cov-report=term --cov-fail-under=$(COV_MIN) $(PYTEST_ARGS)

test-deterministic: ## Full test suite, TOOL_SELECTION_MODE=deterministic, fake LLM, coverage gate
	TOOL_SELECTION_MODE=deterministic $(PY) -m pytest --cov=app --cov-report=term --cov-fail-under=$(COV_MIN) $(PYTEST_ARGS)

test: test-llm test-deterministic ## Run the suite in both tool-selection modes

lint: ## ruff (lint + format check) and mypy --strict on app/
	$(BIN)/ruff check app tests scripts
	$(BIN)/ruff format --check app tests scripts
	$(BIN)/mypy

typecheck: ## mypy --strict on app/
	$(BIN)/mypy

format: ## Auto-format and fix lint where safe
	$(BIN)/ruff format app tests scripts
	$(BIN)/ruff check --fix app tests scripts

check: lint test ## All gates: lint, mypy, tests in both modes with coverage

graph: ## Write the Mermaid graph to docs/graph.md
	$(BIN)/support graph draw

demo: ## Scripted demo of all three requests + human approval (fake LLM, starts its own API)
	$(PY) scripts/demo.py

eval: ## Real-LLM evaluation of the 60 conversations (needs ANTHROPIC_API_KEY); reports/eval.md
	$(PY) -m tests.eval.run_eval

eval-fake: ## Dry-run the evaluation harness with the fake model (no key); reports/eval-fake.md
	$(PY) -m tests.eval.run_eval --provider fake --out reports/eval-fake.md

reset: ## Delete local databases (data/*.db) and re-seed
	rm -f data/*.db data/*.db-wal data/*.db-shm
	$(MAKE) seed

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build *.egg-info
