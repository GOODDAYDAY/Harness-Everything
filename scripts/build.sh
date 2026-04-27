#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Syntax check: harness core ==="
python -m py_compile main.py
find harness -name '*.py' -exec python -m py_compile {} +

echo "=== Syntax check: pilot ==="
python -m py_compile pilot.py

echo "BUILD PASS"
