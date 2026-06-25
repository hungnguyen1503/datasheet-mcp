#!/usr/bin/env bash
# Datasheet MCP index build (Linux/macOS).
# Usage: ./build.sh --part ADXL345 [--reset] [--no-prose] [--no-graph]
set -e
cd "$(dirname "$0")"
python build_helper.py "$@"
