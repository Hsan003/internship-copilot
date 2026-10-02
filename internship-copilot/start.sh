#!/usr/bin/env bash
# Starts Internship Copilot (Linux / macOS). First run creates a virtual environment and installs the dependencies.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "Creating the virtual environment (first run only)..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
  echo "Installing dependencies..."
  pip install -q --upgrade pip >/dev/null 2>&1 || true
  pip install -q -r requirements.txt
  touch .venv/.deps_ok
fi
exec python -m copilot serve --open "$@"
