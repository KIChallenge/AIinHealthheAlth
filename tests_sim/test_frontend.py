"""Debate player: slide building, local server and its safety rules."""

import http.client
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from healthsim.errors import SourceError
from healthsim.events import EventStore
from healthsim.webapp import make_handler, Runner, Store, FRONTEND
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
RESULTS = [EXAMPLES / n for n in ("result_fake_completed.json", "result_needs_clarification.json", "result_invalid_input.json")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node nicht installiert")
def test_chats_build_from_every_result_shape():
    files = [str(p) for p in RESULTS]
    files += [str(p) for p in sorted((ROOT / "state").glob("live_result*.json"))]
    run = subprocess.run(["node", str(ROOT / "tests_sim" / "chats_check.js"), *files], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr[-1500:]


def test_generated_text_is_never_inserted_as_markup():
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", source)


def test_nested_view_content_is_flattened_completely():
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "children.flat(Infinity)" in source      # live bug: "[object HTMLElement]" on the assessment slide


def test_design_rules_hold_in_css():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    weights = set(re.findall(r"font-weight:\s*(\d+)", css)) | set(re.findall(r"font:\s*(\d+)", css))
    assert weights == {"400", "700"}
    assert "box-shadow" not in css and "text-shadow" not in css and "blur(" not in css
    assert "#000000" not in css and "#000;" not in css
    assert (FRONTEND / "logo.svg").read_text(encoding="utf-8").startswith("<svg")


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("text, surface", [
    ("surface", "green-dark"),      # primary buttons
    ("green-dark", "surface"),      # secondary buttons
    ("surface", "olive"),           # rail icons, dividers
    ("ink", "green"),               # active rail icon
    ("ink", "surface"), ("ink", "green-tint"), ("ink", "canvas"),
    ("olive", "surface"), ("olive", "green-tint"), ("olive", "canvas"),     # notes, tags, times
    ("teal", "canvas"), ("teal", "green-tint"),                             # links, buttons on own cards
    ("surface", "teal"), ("surface", "navy"),                               # avatars
])
def test_svs_colours_stay_readable(text, surface):
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6});", css))
    assert contrast(tokens[text], tokens[surface]) >= 4.5


def test_white_text_never_sits_on_the_light_green():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    for rule in re.findall(r"[^{}]+\{[^{}]*background: var\(--green\)[^{}]*\}", css):
        assert "color: var(--surface)" not in rule


def test_the_question_is_never_cut_off():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    rules = " ".join(re.findall(r"#question[^{]*\{[^}]*\}", css))
    assert "line-clamp" not in rules and "ellipsis" not in rules and "hover" not in rules
    assert "overflow-y: auto" in rules          # never hidden: what exceeds the cap can be scrolled


def test_rail_offers_exactly_the_two_chats():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'class="rail-item"', html) == ['class="rail-item"'] * 2
    assert 'id="chat-gruppe"' in html and 'id="chat-meta"' in html
    assert "aria-label=\"Gruppe\"" in html and "aria-label=\"Meta-Moderator\"" in html


@pytest.fixture
def server(tmp_path):
    events = EventStore(tmp_path / "events.sqlite")
    events.save_debate("d1", "{}", 1000.0)
    events.save_result("d1", json.dumps({"debate_id": "d1", "status": "completed"}))
    events.save_debate("d2", "{}", 2000.0)     # still running: no result
    events.close()
    port_holder = {}
    runner = Runner(tmp_path, [])
    handler_args = (Store(tmp_path), runner)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(*handler_args, set()))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(*handler_args, {f"127.0.0.1:{port}", f"localhost:{port}"})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port_holder["port"] = port
    yield port, runner
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data, response


def test_serves_frontend_and_blocks_other_paths(server):
    port, _ = server
    status, data, response = call(port, "GET", "/")
    assert status == 200 and b"debAIte" in data
    assert "script-src" not in response.getheader("Content-Security-Policy")      # default-src 'self'
    assert call(port, "GET", "/chats.js")[0] == 200
    assert call(port, "GET", "/../.env")[0] == 404
    assert call(port, "GET", "/.env")[0] == 404


def test_lists_and_opens_finished_results(server):
    port, _ = server
    status, data, _ = call(port, "GET", "/api/results")
    assert status == 200 and [r["debate_id"] for r in json.loads(data)] == ["d1"]
    assert json.loads(call(port, "GET", "/api/results/d1")[1])["status"] == "completed"
    assert call(port, "GET", "/api/results/d2")[0] == 404
    assert call(port, "GET", "/api/results/x'--")[0] == 404


def test_form_gets_the_configured_institutions(server):
    port, _ = server
    names = json.loads(call(port, "GET", "/api/actors")[1])
    assert names == ["Gesundheitsministerium", "Sozialversicherung", "Ärztekammer"]


def test_interface_says_debaite_instead_of_debatte():
    for name in ("index.html", "app.js", "chats.js"):
        source = (FRONTEND / name).read_text(encoding="utf-8")
        shown = re.findall(r'"[^"\n]*Debatte[^"\n]*"|>[^<\n]*Debatte[^<\n]*<', source)
        assert shown == [], (name, shown[:4])


def test_foreign_host_is_rejected(server):
    port, _ = server
    assert call(port, "GET", "/api/results", headers={"Host": "evil.example"})[0] == 403


def test_post_needs_json_content_type(server):
    port, runner = server
    status, _, _ = call(port, "POST", "/api/run", body="{}", headers={"Content-Type": "text/plain"})
    assert status == 415 and runner.process is None


def test_oversized_request_is_rejected(server):
    port, runner = server
    try:
        status = call(port, "POST", "/api/run", body="x" * 1_000_001, headers={"Content-Type": "application/json"})[0]
    except ConnectionError:
        status = 413          # the server may close before the whole body is sent
    assert status == 413 and runner.process is None


def test_invalid_request_returns_german_json_without_starting(server):
    port, runner = server
    status, data, _ = call(port, "POST", "/api/run", body="{kaputt", headers={"Content-Type": "application/json"})
    assert status == 200 and runner.process is None
    result = json.loads(data)["result"]
    assert result["status"] == "invalid_input" and "gültiges JSON" in result["errors"][0]["message"]


def test_only_one_debate_runs_at_a_time(server, monkeypatch):
    port, runner = server

    class Busy:
        def poll(self):
            return None

    runner.process = Busy()
    body = json.dumps({"actors": ["Ärztekammer"], "question": "Frage", "reference_date": "2026-11-05"})
    status, data, _ = call(port, "POST", "/api/run", body=body, headers={"Content-Type": "application/json"})
    assert status == 409 and "bereits" in json.loads(data)["message"]
    runner.process = None


def test_progress_shows_rounds_and_latest_proposal(tmp_path):
    events = EventStore(tmp_path / "events.sqlite")
    events.save_debate("d", "{}", 1000.0)
    events.emit("d", "a", 1001, "meta", 1, "candidate", "public", "proposal_created",
                payload={"version": 2, "round": 1, "meta_update": "Fortschritt", "clauses": [{"text": "Klausel eins"}]})
    events.emit("d", "b", 1002, "controller", 1, "assessment", "public", "round_closed",
                payload={"complete": True, "streak": 0, "satisfaction": [{"actor_id": "x", "met_count": 1, "priority_count": 4}]})
    events.emit("d", "c", 1003, "ak", 2, "negotiation", "public", "validation_failure", payload={"reason": "r", "rejected": "geheimer Rohtext"})
    events.close()
    progress = Store(tmp_path).progress(1000.0)
    assert progress["phase"] == "negotiation" and progress["round"] == 2 and progress["rejected"] == 1
    assert progress["proposal"]["clauses"] == ["Klausel eins"] and progress["rounds"][0]["complete"] is True
    assert "geheimer Rohtext" not in json.dumps(progress)
    assert Store(tmp_path).progress(5000.0) is None


def test_cancel_stops_the_whole_process_tree(server, tmp_path):
    port, runner = server
    marker = tmp_path / "pid.txt"
    code = f"import os,time; open(r'{marker}','w').write(str(os.getpid())); time.sleep(120)"
    runner.process = subprocess.Popen([sys.executable, "-c", code])
    runner.started = time.time()
    for _ in range(100):
        if marker.is_file() and marker.read_text():
            break
        time.sleep(0.1)
    worker = int(marker.read_text())
    assert json.loads(call(port, "GET", "/api/run")[1])["running"] is True
    status, data, _ = call(port, "POST", "/api/cancel", body="{}", headers={"Content-Type": "application/json"})
    assert status == 200 and json.loads(data)["cancelled"] is True
    time.sleep(0.5)
    alive = subprocess.run(["tasklist", "/FI", f"PID eq {worker}"], capture_output=True).stdout if sys.platform == "win32" else b""
    assert str(worker).encode() not in alive
    state = json.loads(call(port, "GET", "/api/run")[1])
    assert state["running"] is False and state["cancelled"] is True
    assert json.loads(call(port, "GET", "/api/run")[1])["cancelled"] is False


def test_stop_signals_the_running_debate(server, tmp_path):
    port, runner = server

    class Busy:
        def poll(self):
            return None

    runner.process, runner.started = Busy(), 1000.0
    runner.started = 2000.0
    status, _, _ = call(port, "POST", "/api/stop", body="{}", headers={"Content-Type": "application/json"})
    assert status == 200 and (tmp_path / "stop-d2").read_text(encoding="utf-8") == "stop"
    assert json.loads(call(port, "GET", "/api/run")[1])["stopping"] is True
    runner.process = None


def test_stop_too_early_and_without_a_debate(server):
    port, runner = server
    headers = {"Content-Type": "application/json"}
    status, data, _ = call(port, "POST", "/api/stop", body="{}", headers=headers)
    assert status == 409 and "keine debAIte" in json.loads(data)["message"]

    class Busy:
        def poll(self):
            return None

    runner.process, runner.started = Busy(), 9e9
    status, data, _ = call(port, "POST", "/api/stop", body="{}", headers=headers)
    assert status == 409 and "startet noch" in json.loads(data)["message"]
    runner.process = None


def test_cancel_without_a_running_debate(server):
    port, _ = server
    status, data, _ = call(port, "POST", "/api/cancel", body="{}", headers={"Content-Type": "application/json"})
    assert status == 409 and "keine debAIte" in json.loads(data)["message"]


def test_cancel_needs_json_and_a_local_host(server):
    port, _ = server
    assert call(port, "POST", "/api/cancel", body="{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert call(port, "POST", "/api/cancel", body="{}", headers={"Content-Type": "application/json", "Host": "evil.example"})[0] == 403


def test_full_offline_run_through_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_MODE", "responses_v1")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    runner = Runner(tmp_path, ["--llm", "fake", "--sources", "fixture", "--fixture", str(EXAMPLES / "fixture_sources.json")])
    store = Store(tmp_path)
    assert runner.start((EXAMPLES / "request.json").read_text(encoding="utf-8"))
    deadline = time.time() + 60
    status = runner.status(store)
    while status["running"] and time.time() < deadline:
        time.sleep(0.2)
        status = runner.status(store)
    assert status["result"]["status"] == "completed"
    assert [r["status"] for r in store.results()] == ["completed"]
    assert runner.status(store) == {"running": False, "result": None, "cancelled": False}


DEBATE = "a" * 32
PASSAGE = {"passage_id": "P-0123456789ab", "doc_id": "doc-1", "content_hash": "hash-1", "title": "Titel",
           "doc_type": "Article", "text": "Die zitierte Stelle.", "context_before": "Davor.", "context_after": "Danach."}


@pytest.fixture
def source_server(tmp_path):
    events = EventStore(tmp_path / "events.sqlite")
    events.save_debate(DEBATE, "{}", 1000.0)
    events.save_debate("b" * 32, "{}", 1000.0)
    events.save_passage(DEBATE, PASSAGE)
    events.close()
    calls, document = [], {"value": {"text": "Lang. Die zitierte Stelle. Ende.", "content_hash": "hash-1"}}

    def documents(doc_id):
        calls.append(doc_id)
        if isinstance(document["value"], Exception):
            raise document["value"]
        return document["value"]

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path), Runner(tmp_path, []), set(), documents=documents))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(Store(tmp_path), Runner(tmp_path, []), {f"127.0.0.1:{port}"}, documents=documents)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, calls, document
    httpd.shutdown()
    httpd.server_close()


def test_source_returns_the_stored_passage_without_the_database(source_server):
    port, calls, _ = source_server
    status, body = call(port, "GET", f"/api/source/{DEBATE}/{PASSAGE['passage_id']}")[:2]
    assert status == 200
    answer = json.loads(body)
    assert answer["text"] == "Die zitierte Stelle." and answer["context_before"] == "Davor."
    assert calls == []


@pytest.mark.parametrize("path", [
    f"/api/source/{'b' * 32}/P-0123456789ab",           # the passage belongs to another debate
    f"/api/source/{'b' * 32}/P-0123456789ab/document",
    f"/api/source/{DEBATE}/P-ffffffffffff",             # never read in this debate
    f"/api/source/{DEBATE}/P-ffffffffffff/document",
])
def test_only_passages_of_that_debate_can_be_opened(source_server, path):
    port, calls, _ = source_server
    assert call(port, "GET", path)[0] == 404
    assert calls == []


@pytest.mark.parametrize("path", [f"/api/source/{DEBATE}/doc-1", f"/api/source/{DEBATE}/P-0123456789ab/x",
                                  f"/api/source/../{DEBATE}/P-0123456789ab", "/api/source/x/P-0123456789ab"])
def test_malformed_source_paths_are_refused(source_server, path):
    assert call(source_server[0], "GET", path)[0] == 404


def test_whole_document_says_whether_it_changed(source_server):
    port, calls, document = source_server
    path = f"/api/source/{DEBATE}/{PASSAGE['passage_id']}/document"
    status, body = call(port, "GET", path)[:2]
    assert status == 200 and json.loads(body)["unchanged"] is True
    assert calls == ["doc-1"]
    document["value"] = {"text": "Neu.", "content_hash": "hash-2"}
    assert json.loads(call(port, "GET", path)[1])["unchanged"] is False


def test_database_failure_keeps_the_passage_readable(source_server):
    port, _, document = source_server
    document["value"] = SourceError("Neo4j-Lesezugriff fehlgeschlagen: ServiceUnavailable")
    status, body = call(port, "GET", f"/api/source/{DEBATE}/{PASSAGE['passage_id']}/document")[:2]
    assert status == 503 and "ServiceUnavailable" not in json.loads(body)["message"]
    assert call(port, "GET", f"/api/source/{DEBATE}/{PASSAGE['passage_id']}")[0] == 200


def test_document_gone_from_the_graph(source_server):
    port, _, document = source_server
    document["value"] = None
    assert call(port, "GET", f"/api/source/{DEBATE}/{PASSAGE['passage_id']}/document")[0] == 404
