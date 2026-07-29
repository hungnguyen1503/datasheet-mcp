#!/usr/bin/env bash
# ============================================================
#  Datasheet MCP — Full Pipeline Orchestrator (Linux/macOS)
# ============================================================
#
#  Usage:
#    ./build_all.sh                           interactive mode
#    ./build_all.sh --part ADXL345            single part
#    ./build_all.sh --part ADXL345 --index-only   re-index only
#    ./build_all.sh --part ADXL345 --yes      non-interactive
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve Python — prefer .venv
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
else
    PYTHON="python3"
fi

echo
echo " =============================================="
echo "  Datasheet MCP — Build Pipeline"
echo " =============================================="
echo
echo " Python: $PYTHON"
echo

exec "$PYTHON" "$SCRIPT_DIR/tools/build_all.py" "$@"
