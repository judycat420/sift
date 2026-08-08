.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-fast check clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the environment from uv.lock (including dev deps)
	uv sync --all-extras --dev

lint:  ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply ruff formatting and safe fixes
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## mypy --strict
	uv run mypy

test:  ## pytest with coverage
	uv run pytest

# The inner loop: everything except the whole-deck and whole-corpus scans, and
# no coverage. Not a substitute for `make check` — it is what makes running the
# gate before every commit cheap enough to actually do. The 90% floor is only
# meaningful over the full suite, so measuring it here would either fail
# spuriously or invite lowering it; `--no-cov` refuses the question instead.
test-fast:  ## pytest without the slow scans or coverage — the inner loop
	uv run pytest -m "not slow" --no-cov

check: install lint typecheck test  ## Everything CI runs

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
