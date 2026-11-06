"""Watching a running debate: partial results, their display, and the server endpoints."""

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import check_result
from healthsim import app as simulator
from healthsim.events import EventStore
from healthsim.webapp import Runner, Store, make_handler
from test_workflow import assess_with

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    """Keeps a copy of every partial result a debate writes."""
    folder = tmp_path / "snapshots"
    folder.mkdir()
    replace = os.replace

    def keep(source, target):
        shutil.copy(source, folder / f"{len(list(folder.iterdir())):04d}.json")
        replace(source, target)

    monkeypatch.setattr(simulator, "LIVE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(simulator.os, "replace", keep)
    return folder


def load(folder):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.iterdir())]


def test_partial_results_grow_message_by_message(harness, snapshots):
    h = harness()
    final = h.run()
    check_result(final)
    partial = load(snapshots)
    assert len(partial) > 10
    assert all(p["live"] is True for p in partial)
    assert "live" not in final
    counts = [len(p["reviews"]) for p in partial]
    assert counts == sorted(counts)                         # nothing disappears again
    # finer than one graph step: reviews of a fan-out arrive one at a time, not in blocks of nine
    assert 1 in {b - a for a, b in zip(counts, counts[1:])}
    assert partial[0]["reviews"] == [] and partial[-1]["reviews"]
    assert partial[0]["final_proposal"] is None


def test_partial_results_are_valid_results(harness, snapshots):
    harness().run()
    for partial in load(snapshots):
        check_result({k: v for k, v in partial.items() if k != "live"})


def test_live_file_is_gone_after_the_debate(harness, snapshots):
    h = harness()
    h.run()
    assert not list(Path(h.settings.state_dir).glob("live-*"))


def test_a_failing_live_view_never_touches_the_debate(harness, monkeypatch):
    h = harness()
    baseline = h.run()
    other = harness()
    monkeypatch.setattr(simulator, "LIVE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(simulator, "build_result", lambda *a: (_ for _ in ()).throw(ValueError("kaputt")))
    assert other.run()["status"] == baseline["status"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node nicht installiert")
def test_every_partial_result_can_be_shown(harness, snapshots):
    harness(overrides={"AssessOutput": assess_with(lambda number, pid: "partly_met" if number <= 3 else "met")}).run()
    files = [str(p) for p in sorted(snapshots.iterdir())]
    run = subprocess.run(["node", str(ROOT / "tests_sim" / "live_check.js"), *files], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-1500:]


DEBATE, CASE_DEBATE = "a" * 32, "c" * 32


def stored(directory, debate_id, created):
    events = EventStore(Path(directory) / "events.sqlite")
    events.save_debate(debate_id, "{}", created)
    events.close()


def write_live(directory, debate_id, payload):
    Path(directory, f"live-{debate_id}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def server(tmp_path):
    case = tmp_path / "cases" / "01_facharzt"
    case.mkdir(parents=True)
    stored(tmp_path, DEBATE, 1000.0)
    stored(case, CASE_DEBATE, 2000.0)
    store = Store(tmp_path)
    runner = Runner(tmp_path, [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, runner, set()))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(store, runner, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, tmp_path, case
    httpd.shutdown()
    httpd.server_close()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body)


def test_running_debates_are_found_in_parallel_state_directories(server):
    port, _, case = server
    write_live(case, CASE_DEBATE, {"debate_id": CASE_DEBATE, "live": True})
    status, running = get(port, "/api/running")
    assert status == 200
    assert [(r["debate_id"], r["case"]) for r in running] == [(CASE_DEBATE, "01_facharzt")]


def test_a_left_over_file_of_a_crashed_run_is_not_shown_as_running(server):
    port, _, case = server
    write_live(case, CASE_DEBATE, {"live": True})
    old = time.time() - 3 * 3600
    os.utime(case / f"live-{CASE_DEBATE}.json", (old, old))
    assert get(port, "/api/running")[1] == []


def test_live_sends_the_partial_result_only_when_it_changed(server):
    port, root, _ = server
    write_live(root, DEBATE, {"debate_id": DEBATE, "live": True})
    status, first = get(port, f"/api/live/{DEBATE}")
    assert status == 200 and first["running"] is True and first["own"] is False
    assert first["result"]["debate_id"] == DEBATE
    again = get(port, f"/api/live/{DEBATE}?seen={first['stamp']}")[1]
    assert again["running"] is True and again["result"] is None
    assert "progress" in again


def test_live_hands_over_to_the_final_result(server):
    port, root, _ = server
    write_live(root, DEBATE, {"debate_id": DEBATE, "live": True})
    events = EventStore(root / "events.sqlite")
    events.save_result(DEBATE, json.dumps({"debate_id": DEBATE, "status": "completed"}))
    events.close()
    status, answer = get(port, f"/api/live/{DEBATE}")
    assert status == 200 and answer["running"] is False and answer["result"]["status"] == "completed"
    assert get(port, "/api/running")[1] == []


@pytest.mark.parametrize("path", ["/api/live/zzz", f"/api/live/{'b' * 32}", f"/api/live/{DEBATE}/../x"])
def test_unknown_or_malformed_live_paths_are_refused(server, path):
    assert get(server[0], path)[0] == 404


def test_results_from_parallel_runs_are_listed_and_named(server):
    port, root, case = server
    events = EventStore(case / "events.sqlite")
    events.save_result(CASE_DEBATE, json.dumps({"debate_id": CASE_DEBATE, "status": "completed"}))
    events.close()
    listed = get(port, "/api/results")[1]
    assert [(r["debate_id"], r["case"]) for r in listed] == [(CASE_DEBATE, "01_facharzt")]
    assert get(port, f"/api/results/{CASE_DEBATE}")[1]["status"] == "completed"


def test_round_countdown_comes_from_the_real_round_lengths(server):
    _, root, _ = server
    events = EventStore(root / "events.sqlite")
    for number, (kind, at) in enumerate([("package_published", 100.0), ("round_closed", 400.0), ("round_closed", 800.0)]):
        events.emit(DEBATE, f"e{number}", at, "system", number, "negotiation", "public", kind, payload={})
    events.close()
    progress = Store(root).progress_of(root, DEBATE)
    assert progress["round_started_at"] == 800.0
    assert progress["average_round_seconds"] == 350.0


def test_no_countdown_before_the_first_round_closed(server):
    _, root, _ = server
    progress = Store(root).progress_of(root, DEBATE)
    assert progress["round_started_at"] is None and progress["average_round_seconds"] is None


class EndedProcess:
    """A child that is gone."""

    pid = 1

    def poll(self):
        return 1

    def wait(self, timeout=None):
        return 1


def test_a_run_that_ends_without_a_result_is_reported_and_cleaned_up(tmp_path):
    stored(tmp_path, DEBATE, 1000.0)
    write_live(tmp_path, DEBATE, {"live": True})
    Path(tmp_path, f"stop-{DEBATE}").write_text("stop", encoding="utf-8")
    runner = Runner(tmp_path, [])
    runner.process, runner.started = EndedProcess(), 1000.0
    runner.output = tmp_path / "fehlt.json"
    status = runner.status(Store(tmp_path))
    assert status == {"running": False, "result": None, "cancelled": False, "failed": True}
    assert not list(tmp_path.glob("live-*")) and not list(tmp_path.glob("stop-*"))


def test_cancelling_removes_the_working_files(tmp_path, monkeypatch):
    stored(tmp_path, DEBATE, 1000.0)
    write_live(tmp_path, DEBATE, {"live": True})
    Path(tmp_path, f"stop-{DEBATE}").write_text("stop", encoding="utf-8")

    class Running(EndedProcess):
        def poll(self):
            return None

    monkeypatch.setattr("healthsim.webapp.kill_tree", lambda process: None)
    runner = Runner(tmp_path, [])
    runner.process, runner.started = Running(), 1000.0
    assert runner.cancel() is True
    assert not list(tmp_path.glob("live-*")) and not list(tmp_path.glob("stop-*"))


def test_stop_is_shown_while_the_evaluation_runs(server):
    port, root, _ = server
    write_live(root, DEBATE, {"debate_id": DEBATE, "live": True})
    assert get(port, f"/api/live/{DEBATE}")[1]["stopping"] is False
