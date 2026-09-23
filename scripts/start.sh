#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --frozen --no-dev --python 3.12 python scripts/start.py "$@"
