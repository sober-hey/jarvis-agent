#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --frozen
# Formal Python assets only; archived reports and delivery copies are not product code.
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen pyright
uv run --frozen pytest -q
uv run --frozen python scripts/check_environment.py
