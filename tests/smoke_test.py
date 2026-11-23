"""Smoke test: the app starts on this system and every screen's API answers, without any downloaded data.

Runs on a copy of the code in a temporary folder (never touches your data/ or key file).
    python tests/smoke_test.py
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GET = ["/", "/app.js", "/style.css", "/api/status", "/api/stations", "/api/areas", "/api/supermarkets", "/api/parks",
       "/api/listings", "/api/municipalities", "/api/weights"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="rf-smoke-"))
    app = tmp / "app"
    shutil.copytree(REPO, app, ignore=shutil.ignore_patterns("data", ".git", "__pycache__", "ep-online-key.txt", "tests"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=app, env={**os.environ, "RF_PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    failures = []
    try:
        for _ in range(60):
            try:
                call(base, "/api/status")
                break
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit("server exited at start:\n" + proc.stdout.read())
                time.sleep(1)
        else:
            raise SystemExit("server did not answer within 60 s")

        for path in GET:
            try:
                code, _ = call(base, path)
                print(f"ok   GET {path}")
            except Exception as e:
                failures.append(f"GET {path}: {e}")
                print(f"FAIL GET {path}: {e}")

        # saving settings and the API key works, and the key file is private on Linux/macOS
        call(base, "/api/weights", "PUT", {})
        call(base, "/api/travel", "PUT", {"arrivals": {}, "speeds": {"walk": 5.5}})
        call(base, "/api/energy-key", "POST", {"key": "test-key-not-real"})
        key = app / "ep-online-key.txt"
        if key.read_text(encoding="utf-8") != "test-key-not-real":
            failures.append("key file has the wrong content")
        if os.name != "nt":
            mode = stat.S_IMODE(key.stat().st_mode)
            if mode & 0o077:
                failures.append(f"key file is readable by others (mode {oct(mode)})")
        print("ok   settings and key file saved")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        raise SystemExit("Failures:\n  " + "\n  ".join(failures))
    print(f"All good on {sys.platform}, Python {sys.version.split()[0]}")


if __name__ == "__main__":
    main()
