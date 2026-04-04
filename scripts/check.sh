#!/usr/bin/env bash
# scripts/check.sh — run all checks
set -euo pipefail

echo "=== pyright ==="
pyright

echo ""
echo "=== pytest ==="
pytest tests/ -x -q

echo ""
echo "=== ruff check ==="
ruff check eisenberg/ custom_components/ tests/

echo ""
echo "=== ruff format check ==="
ruff format --check eisenberg/ custom_components/ tests/

echo ""
echo "All checks passed."
