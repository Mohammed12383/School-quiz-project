#!/usr/bin/env bash
# ضفاف العربية — تشغيل الموقع على ماك أو لينكس
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.12 is required: https://www.python.org/downloads/"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi

# install requirements only when they changed (works offline afterwards)
if ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing requirements..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

PORT="${PORT:-8000}"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
