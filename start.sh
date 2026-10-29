#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || [ ! -d frontend/dist ]; then
  echo "Please follow the first-launch setup in README.md."
  exit 1
fi
echo "Statify: http://127.0.0.1:8765 — press Ctrl+C to stop."
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765 --no-access-log
