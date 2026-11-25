# idx-engine Makefile

.PHONY: all install format format-check analyze hooks test test-ci fix upgrade clean web-install web-build

all: format-check analyze test

install:
	uv sync --locked

hooks:
	uvx pre-commit install

format:
	uv run ruff format src tests

format-check:
	uv run ruff format --check src tests

analyze:
	uv run ruff check src tests
	uv run mypy src tests

test:
	uv run pytest

test-ci: format-check analyze
	uv run pytest -v

fix:
	uv run ruff check --fix src tests
	uv run ruff format src tests

upgrade:
	uv lock --upgrade
	uv sync

web-install:
	cd web && npm ci

web-build:
	cd web && npm run build

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache web/dist
