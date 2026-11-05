"""Small local server for the debate player (standard library only).

It binds to 127.0.0.1, serves the fixed frontend files and runs one debate at a time in a
child process, so the CLI's single-process lock and JSON contract apply unchanged. POST only
accepts application/json and a local Host header, so another website in the browser cannot
start a debate or read results.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api_models import Message
from .app import invalid_result, new_debate_id
from .intake import parse_request
from .settings import ROOT, read_env_file

FRONTEND = ROOT / "frontend"
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/chats.js": ("chats.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css")}
MAX_BODY = 1_000_000


def state_dir():
    import os
    value = os.environ.get("STATE_DIR") or read_env_file(ROOT / ".env").get("STATE_DIR") or "./state"
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


class Store:
    """Read-only view of stored debates."""

    def __init__(self, directory):
        self.path = Path(directory) / "events.sqlite"

    def _rows(self, query, *params):
        if not self.path.is_file():
            return []
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return db.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()

    def results(self):
        out = []
        for debate_id, created, result in self._rows(
                "SELECT debate_id, created_at, result FROM debates WHERE result IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 50"):
            status = json.loads(result).get("status")
            out.append({"debate_id": debate_id, "created_at": created, "status": status})
        return out

    def result(self, debate_id):
        rows = self._rows("SELECT result FROM debates WHERE debate_id=?", debate_id)
        return rows[0][0] if rows and rows[0][0] else None

    def newest_events(self, since):
        rows = self._rows("SELECT COUNT(*) FROM events WHERE at >= ?", since)
        return rows[0][0] if rows else 0


class Runner:
    """One debate at a time, as a CLI child process."""

    def __init__(self, directory, cli_args):
        self.directory, self.cli_args = directory, list(cli_args)
        self.lock = threading.Lock()
        self.process = None
        self.started = None
        self.output = None
        self.finished = None

    def start(self, raw):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False
            self.output = Path(tempfile.mkdtemp(prefix="healthsim-")) / "result.json"
            self.started, self.finished = time.time(), None
            self.process = subprocess.Popen(
                [sys.executable, "-m", "healthsim", "run", "--input", "-", "--output", str(self.output), *self.cli_args],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
            self.process.stdin.write(raw.encode("utf-8"))
            self.process.stdin.close()
            return True

    def status(self, store):
        with self.lock:
            if self.process is None:
                return {"running": False, "result": None}
            if self.process.poll() is None:
                return {"running": True, "elapsed_seconds": time.time() - self.started,
                        "events": store.newest_events(self.started)}
            if self.finished is None and self.output.is_file():
                self.finished = json.loads(self.output.read_text(encoding="utf-8"))
            result, self.finished, self.process = self.finished, None, None
            return {"running": False, "result": result}


def make_handler(store, runner, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        server_version = "healthsim"

        def log_message(self, *args):
            pass

        def _send(self, code, body, content_type="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, data):
            self._send(code, json.dumps(data, ensure_ascii=False))

        def _local(self):
            if self.headers.get("Host", "") in allowed_hosts:
                return True
            self._json(403, {"message": "Nur lokale Zugriffe sind erlaubt."})
            return False

        def do_GET(self):
            if not self._local():
                return
            path = self.path.split("?", 1)[0]
            if path in STATIC:
                name, kind = STATIC[path]
                self._send(200, (FRONTEND / name).read_bytes(), kind)
            elif path == "/api/results":
                self._json(200, store.results())
            elif path.startswith("/api/results/"):
                result = store.result(path.rsplit("/", 1)[1])
                if result:
                    self._send(200, result)
                else:
                    self._json(404, {"message": "Ergebnis nicht gefunden."})
            elif path == "/api/run":
                self._json(200, runner.status(store))
            else:
                self._json(404, {"message": "Nicht gefunden."})

        def do_POST(self):
            if not self._local():
                return
            if self.path != "/api/run":
                self._json(404, {"message": "Nicht gefunden."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                self._json(413, {"message": "Die Anfrage ist zu groß."})
                return
            raw = self.rfile.read(length).decode("utf-8", "replace")      # read first, then answer
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"message": "Erwartet wird application/json."})
                return
            _, errors = parse_request(raw)
            if errors:     # no child process and no cost for a request that cannot run
                self._json(200, {"result": invalid_result(new_debate_id(), [Message(**e.model_dump()) for e in errors])})
                return
            if runner.start(raw):
                self._json(202, {"started": True})
            else:
                self._json(409, {"message": "Es läuft bereits eine Debatte."})

    return Handler


def serve(port=8765, cli_args=(), directory=None, ready=None):
    directory = Path(directory or state_dir())
    store, runner = Store(directory), Runner(directory, cli_args)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, runner, allowed))
    if ready:
        ready(server)
    print(f"Debatten-Player: http://127.0.0.1:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
