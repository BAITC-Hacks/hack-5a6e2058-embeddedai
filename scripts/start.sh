#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
npm --prefix web ci --no-audit --no-fund
npm --prefix web run build
exec uv run --frozen money-graph serve "$@"
