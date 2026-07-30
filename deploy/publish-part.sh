#!/usr/bin/env bash
# Build one already-synchronized part into the configured Qdrant namespace.
set -euo pipefail

usage() {
    echo "Usage: $0 --part <PART>" >&2
    exit 2
}

part=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --part) part="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done

[[ "$part" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || usage
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$root/.venv/bin/python" ]] || { echo "missing server virtual environment" >&2; exit 1; }
[[ -f "$root/mcp/build_helper.py" ]] || { echo "missing canonical build helper" >&2; exit 1; }
[[ -d "$root/datasheet/$part/MD" ]] || { echo "missing Markdown input for $part" >&2; exit 1; }

cd "$root/mcp"
exec "$root/.venv/bin/python" build_helper.py --part "$part" --no-enrich
