#!/usr/bin/env sh
# Starts Rental finder (Linux and macOS) and opens it in your browser. Press Ctrl+C to stop it.
# Uses the Python in ~/.venvs/rental-finder (see README.md), or the one in RF_PYTHON if you set it.
cd "$(dirname "$0")" || exit 1

PY="${RF_PYTHON:-$HOME/.venvs/rental-finder/bin/python}"
if [ ! -x "$PY" ]; then
  echo "No Python found at $PY."
  echo "Set it up once with:"
  echo "  python3 -m venv ~/.venvs/rental-finder"
  echo "  ~/.venvs/rental-finder/bin/pip install -r requirements.txt"
  exit 1
fi

# server.py opens the browser once it is ready (or just opens it if the app already runs)
exec "$PY" server.py
