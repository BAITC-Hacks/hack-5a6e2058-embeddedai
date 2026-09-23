#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen mypy src
uv run --frozen pytest -q
npm --prefix web ci --no-audit --no-fund
npm --prefix web run check
npm --prefix web run build
uv run --frozen python scripts/smoke.py
