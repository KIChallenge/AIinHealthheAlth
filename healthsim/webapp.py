"""Small local server for the debate player (standard library only).

It binds to 127.0.0.1, serves the fixed frontend files and runs one debate at a time in a
child process, so the CLI's single-process lock and JSON contract apply unchanged. POST only
accepts application/json and a local Host header, so another website in the browser cannot
start a debate or read results.
"""

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

from .api_models import Message
from .app import invalid_result, new_debate_id
from .ask import Asker, AskError
from .errors import ConfigurationError, SourceError
from .intake import parse_request
from .settings import ROOT, load_settings, read_env_file
from .sources import Neo4jSources

FRONTEND = ROOT / "frontend"
SCENARIOS = ROOT / "Beispielanfragen"
SCENARIO_LABELS = {"01_facharzt_wartezeiten": "Facharzt-Wartezeiten um 20 % senken",
                   "02_gesundheitsausgaben_2035": "Gesundheitsausgaben bis 2035 senken",
                   "03_honorarverhandlungen": "Honorierung der Vertragsärzte",
                   "04_primaerversorgung": "Wohnortnahe Primärversorgung",
                   "05_telemedizin_triage": "Pilot für Telemedizin und Triage"}
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/logo.svg": ("logo.svg", "image/svg+xml"),
          "/app.js": ("app.js", "text/javascript"), "/chats.js": ("chats.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css")}
MAX_BODY = 1_000_000
LIVE_STALE_SECONDS = 2400         # longer than the slowest model call: older files are from a crashed run
SOURCE_PATH = re.compile(r"^/api/source/([0-9a-f]{32})/(P-[0-9a-f]{12})(/document)?$")
LIVE_PATH = re.compile(r"^/api/live/([0-9a-f]{32})$")


def state_dir():
    import os
    value = os.environ.get("STATE_DIR") or read_env_file(ROOT / ".env").get("STATE_DIR") or "./state"
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def actor_names():
    """Institutions for the form, from the same configuration the graph was built with."""
    try:
        return [a["name"] for a in json.loads((ROOT / "config" / "actors.json").read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def scenarios():
    """Example requests for the dropdown, from fixed files in Beispielanfragen/."""
    out = []
    for path in sorted(SCENARIOS.glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": path.stem, "label": SCENARIO_LABELS.get(path.stem, path.stem.replace("_", " ")), "request": request})
    return out


def read_document(doc_id):
    """One document from the graph, read-only. The connection lasts only for this request."""
    settings = load_settings(require_azure=False)

    async def fetch():
        adapter = Neo4jSources(settings)
        try:
            return await adapter.document(doc_id)
        finally:
            await adapter.close()

    return asyncio.run(fetch())


class Store:
    """Read-only view of stored debates: state/ and the parallel runs under state/cases/."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "events.sqlite"

    def directories(self):
        cases = self.directory / "cases"
        return [self.directory, *(sorted(p for p in cases.iterdir() if p.is_dir()) if cases.is_dir() else [])]

    def _rows_in(self, directory, query, *params):
        path = Path(directory) / "events.sqlite"
        if not path.is_file():
            return []
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return db.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()

    def _rows(self, query, *params):
        return self._rows_in(self.directory, query, *params)

    def home(self, debate_id):
        """The state directory that holds this debate, or None."""
        for directory in self.directories():
            if self._rows_in(directory, "SELECT 1 FROM debates WHERE debate_id=?", debate_id):
                return directory
        return None

    def case_name(self, directory):
        return None if Path(directory) == self.directory else Path(directory).name

    def results(self):
        out = []
        for directory in self.directories():
            for debate_id, created, result in self._rows_in(directory,
                    "SELECT debate_id, created_at, result FROM debates WHERE result IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 50"):
                out.append({"debate_id": debate_id, "created_at": created, "status": json.loads(result).get("status"),
                            "case": self.case_name(directory)})
        return sorted(out, key=lambda item: item["created_at"], reverse=True)[:50]

    def result(self, debate_id):
        directory = self.home(debate_id)
        rows = self._rows_in(directory, "SELECT result FROM debates WHERE debate_id=?", debate_id) if directory else []
        return rows[0][0] if rows and rows[0][0] else None

    def passage(self, debate_id, passage_id):
        """The exact passage this debate read, or None: nothing outside the debate can be opened."""
        directory = self.home(debate_id)
        rows = self._rows_in(directory, "SELECT payload FROM passages WHERE debate_id=? AND passage_id=?",
                             debate_id, passage_id) if directory else []
        return json.loads(rows[0][0]) if rows else None

    def newest_events(self, since):
        rows = self._rows("SELECT COUNT(*) FROM events WHERE at >= ?", since)
        return rows[0][0] if rows else 0

    def live_path(self, debate_id):
        directory = self.home(debate_id)
        path = Path(directory) / f"live-{debate_id}.json" if directory else None
        return path if path and path.is_file() and self.result(debate_id) is None else None

    def running(self):
        """Debates that are writing a live view, wherever they were started."""
        out = []
        for directory in self.directories():
            for path in directory.glob("live-*.json"):
                debate_id = path.stem[len("live-"):]
                if time.time() - path.stat().st_mtime < LIVE_STALE_SECONDS and self.live_path(debate_id):
                    progress = self.progress_of(directory, debate_id)
                    out.append({"debate_id": debate_id, "case": self.case_name(directory),
                                "round": progress["round"], "phase": progress["phase"]})
        return sorted(out, key=lambda item: item["case"] or "")

    def progress(self, since):
        """Interim state of the debate started at `since`, from its events. Read-only."""
        rows = self._rows("SELECT debate_id FROM debates WHERE created_at >= ? ORDER BY created_at DESC LIMIT 1", since - 2)
        return self.progress_of(self.directory, rows[0][0]) if rows else None

    def progress_of(self, directory, debate_id):
        rows = lambda query, *params: self._rows_in(directory, query, *params)
        last = rows("SELECT phase, round FROM events WHERE debate_id=? ORDER BY seq DESC LIMIT 1", debate_id)
        counts = dict(rows("SELECT type, COUNT(*) FROM events WHERE debate_id=? GROUP BY type", debate_id))
        rounds = [{"round": number, **{k: payload[k] for k in ("complete", "streak", "satisfaction") if k in payload}}
                  for number, payload in ((n, json.loads(p)) for n, p in rows(
                      "SELECT round, payload FROM events WHERE debate_id=? AND type='round_closed' ORDER BY seq", debate_id))]
        proposals = rows("SELECT payload FROM events WHERE debate_id=? AND type='proposal_created' ORDER BY seq DESC LIMIT 1", debate_id)
        proposal = None
        if proposals:
            data = json.loads(proposals[0][0])
            proposal = {"version": data["version"], "round": data["round"], "meta_update": data["meta_update"],
                        "clauses": [c["text"] for c in data["clauses"]]}
        marks = [at for (at,) in rows("SELECT at FROM events WHERE debate_id=? AND type IN ('package_published', 'round_closed') ORDER BY seq", debate_id)]
        spans = [b - a for a, b in zip(marks, marks[1:])]
        return {"debate_id": debate_id, "phase": last[0][0] if last else None, "round": last[0][1] if last else 0,
                "events": sum(counts.values()), "rejected": counts.get("validation_failure", 0),
                "decisions": counts.get("decision_recorded", 0), "rounds": rounds[-6:], "proposal": proposal,
                "round_started_at": marks[-1] if marks else None,
                "average_round_seconds": sum(spans) / len(spans) if spans else None}


def kill_tree(process):
    """On Windows the venv launcher starts the real interpreter as a child, so stop the whole tree."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
    else:
        process.terminate()
    process.wait(timeout=10)


class Runner:
    """One debate at a time, as a CLI child process."""

    def __init__(self, directory, cli_args):
        self.directory, self.cli_args = directory, list(cli_args)
        self.lock = threading.Lock()
        self.process = None
        self.started = None
        self.output = None
        self.finished = None
        self.cancelled = False
        self.stopping = False

    def start(self, raw):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False
            self.output = Path(tempfile.mkdtemp(prefix="healthsim-")) / "result.json"
            self.started, self.finished, self.stopping = time.time(), None, False
            Path(self.directory).mkdir(parents=True, exist_ok=True)
            with open(Path(self.directory) / "run.log", "ab") as log:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "healthsim", "run", "--input", "-", "--output", str(self.output), *self.cli_args],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, cwd=ROOT)
            self.process.stdin.write(raw.encode("utf-8"))
            self.process.stdin.close()
            return True

    def status(self, store):
        with self.lock:
            if self.process is None:
                cancelled, self.cancelled = self.cancelled, False
                return {"running": False, "result": None, "cancelled": cancelled}
            if self.process.poll() is None:
                return {"running": True, "elapsed_seconds": time.time() - self.started,
                        "events": store.newest_events(self.started), "progress": store.progress(self.started),
                        "stopping": self.stopping}
            if self.finished is None and self.output.is_file():
                self.finished = json.loads(self.output.read_text(encoding="utf-8"))
            result, self.finished, self.process = self.finished, None, None
            if result is None:
                self._forget(store)
            return {"running": False, "result": result, "cancelled": False, "failed": result is None}

    def request_stop(self, store):
        """Ends the deliberation; the child freezes the last candidate and finalises as usual."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return "not_running"
            progress = store.progress(self.started)
            if progress is None:
                return "starting"
            Path(self.directory, f"stop-{progress['debate_id']}").write_text("stop", encoding="utf-8")
            self.stopping = True
            return "stopping"

    def _forget(self, store):
        """A process that ended without a result leaves its working files behind."""
        progress = store.progress(self.started)
        for name in (f"live-{progress['debate_id']}.json", f"stop-{progress['debate_id']}") if progress else ():
            Path(self.directory, name).unlink(missing_ok=True)

    def owns(self, store, debate_id):
        """True if this server started the debate and can stop it."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return False
            progress = store.progress(self.started)
            return bool(progress) and progress["debate_id"] == debate_id

    def cancel(self):
        """Stops the running debate, including child processes. Returns False if none is running."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return False
            kill_tree(self.process)
            self._forget(Store(self.directory))
            self.process, self.finished = None, None
            self.cancelled = True
            return True


def make_handler(store, runner, allowed_hosts, asker=None, documents=read_document):
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
            elif path == "/api/scenarios":
                self._json(200, scenarios())
            elif path == "/api/actors":
                self._json(200, actor_names())
            elif path.startswith("/api/ask/") and asker:
                debate_id = path.rsplit("/", 1)[1]
                self._json(200, asker.at(store.home(debate_id)).history(debate_id))
            elif SOURCE_PATH.match(path):
                self._source(SOURCE_PATH.match(path))
            elif path == "/api/running":
                self._json(200, store.running())
            elif LIVE_PATH.match(path):
                self._live(LIVE_PATH.match(path).group(1))
            else:
                self._json(404, {"message": "Nicht gefunden."})

        def _live(self, debate_id):
            """The newest partial result. The file is only sent when it changed since `seen`."""
            final = store.result(debate_id)
            if final:
                self._send(200, f'{{"running": false, "result": {final}}}')
                return
            path = store.live_path(debate_id)
            if path is None:
                self._json(404, {"message": "Zu dieser Debatte gibt es noch keinen Zwischenstand."})
                return
            stamp = str(path.stat().st_mtime_ns)
            seen = dict(parse_qsl(self.path.partition("?")[2])).get("seen")
            own = runner.owns(store, debate_id)
            status = {"running": True, "stamp": stamp, "own": own, "stopping": own and runner.stopping,
                      "progress": store.progress_of(store.home(debate_id), debate_id)}
            head = json.dumps(status, ensure_ascii=False)[:-1]
            try:
                body = "null" if seen == stamp else path.read_text(encoding="utf-8")
            except OSError:         # being replaced this very moment
                body = "null"
            self._send(200, f'{head}, "result": {body}}}')

        def _source(self, match):
            debate_id, passage_id, whole = match.groups()
            passage = store.passage(debate_id, passage_id)
            if passage is None:
                self._json(404, {"message": "Diese Quelle gehört nicht zu dieser Debatte."})
            elif not whole:
                self._json(200, passage)
            else:
                try:
                    document = documents(passage["doc_id"])
                except (ConfigurationError, SourceError):
                    self._json(503, {"message": "Die Datenbank ist gerade nicht erreichbar. Die zitierte Stelle bleibt lesbar."})
                    return
                if document is None:
                    self._json(404, {"message": "Das Dokument ist nicht mehr in der Datenbank."})
                else:
                    self._json(200, {"text": document["text"], "unchanged": document["content_hash"] == passage["content_hash"]})

        def _ask(self, raw):
            if asker is None:
                self._json(404, {"message": "Nachfragen sind hier nicht verfügbar."})
                return
            try:
                body = json.loads(raw)
                debate_id = str(body["debate_id"])
                self._json(200, asker.at(store.home(debate_id)).answer(debate_id, body.get("question"), store.result(debate_id)))
            except (ValueError, KeyError, TypeError):
                self._json(400, {"message": "Die Nachfrage ist ungültig."})
            except AskError as exc:
                self._json(exc.status, {"message": exc.message})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                self._json(413, {"message": "Die Anfrage ist zu groß."})
                return
            raw = self.rfile.read(length).decode("utf-8", "replace")      # read first, then answer
            if not self._local():
                return
            if self.path not in ("/api/run", "/api/ask", "/api/cancel", "/api/stop"):
                self._json(404, {"message": "Nicht gefunden."})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"message": "Erwartet wird application/json."})
                return
            if self.path == "/api/ask":
                self._ask(raw)
                return
            if self.path == "/api/stop":
                outcome = runner.request_stop(store)
                if outcome == "stopping":
                    self._json(200, {"stopping": True})
                else:
                    message = "Die Debatte startet noch. Bitte gleich noch einmal." if outcome == "starting" else "Es läuft keine Debatte."
                    self._json(409, {"message": message})
                return
            if self.path == "/api/cancel":
                if runner.cancel():
                    self._json(200, {"cancelled": True})
                else:
                    self._json(409, {"message": "Es läuft keine Debatte."})
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
    llm = cli_args[cli_args.index("--llm") + 1] if "--llm" in cli_args else "azure"
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, runner, allowed, Asker(directory, llm)))
    if ready:
        ready(server)
    print(f"debAIte: http://127.0.0.1:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
