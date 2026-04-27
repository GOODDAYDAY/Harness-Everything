#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Running unit tests ==="
python -m pytest tests/unit/ -v

echo "TEST PASS"
