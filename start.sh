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

URL=http://127.0.0.1:8765

# open the browser as soon as the server answers
(
  i=0
  while [ $i -lt 60 ]; do
    if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('$URL/api/status', timeout=1)" 2>/dev/null; then
      if command -v open >/dev/null 2>&1 && [ "$(uname)" = "Darwin" ]; then open "$URL"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
      else echo "Open $URL in your browser."
      fi
      exit 0
    fi
    i=$((i + 1)); sleep 1
  done
) &

exec "$PY" server.py
