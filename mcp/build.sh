#!/usr/bin/env bash
# Datasheet MCP index build — pushes registers, prose, pins, and graph edges
# to Qdrant. Reads QDRANT_URL / QDRANT_API_KEY from mcp/.env.
#
# Usage:
#   bash build.sh --part ADXL345               index/refresh one part
#   bash build.sh --part ADXL345 --reset       DROP all ds_* collections then rebuild
#   Prose and graph evidence are mandatory in the canonical index.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Resolve Python — prefer .venv
if [ -f "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif [ -f "$REPO_ROOT/.venv/bin/python3" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python3"
else
    PYTHON="python3"
fi

echo
echo "[1/1] Building and pushing index to Qdrant..."
"$PYTHON" "$SCRIPT_DIR/build_helper.py" "$@"

echo
echo "Done. Start the server with:  python server.py"
