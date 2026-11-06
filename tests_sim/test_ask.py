"""Private questions to the meta-moderator, and the scenario dropdown."""

import json
from pathlib import Path

import pytest
from conftest import BASE_ENV, ROOT

from healthsim import rules
from healthsim.ask import ANSWER_WORDS, Asker, AskError, RECORD_TOKENS, debate_record
from healthsim.errors import LlmAuthError
from healthsim.intake import clarification_needed, parse_request
from healthsim.llm import ProcessLock, estimate_tokens
from healthsim.llm_fake import FakeAdapter
from healthsim.prompts import dumps
from healthsim.webapp import scenarios

RESULT_FILE = ROOT / "examples" / "result_fake_completed.json"
RESULT = RESULT_FILE.read_text(encoding="utf-8")


def asker(tmp_path, overrides=None):
    adapter = FakeAdapter(overrides=overrides)
    return Asker(tmp_path, llm="fake", adapter=adapter, env={**BASE_ENV, "STATE_DIR": str(tmp_path)}), adapter


def test_answer_comes_from_the_record_and_is_stored(tmp_path):
    a, adapter = asker(tmp_path)
    answer = a.answer("d", "Was blieb offen?", RESULT)
    assert answer["antwort"].startswith("Laut Protokoll") and answer["keine_grundlage"] is False
    prompt = adapter.calls[0]["input"]
    assert "Neue Frage: Was blieb offen?" in prompt and "Debattenprotokoll:" in prompt
    assert "nur aus dem Debattenprotokoll" in adapter.calls[0]["instructions"].replace("Antworte nur", "nur")
    assert [t["role"] for t in a.history("d")] == ["du", "meta"]


def test_follow_up_questions_see_the_conversation(tmp_path):
    a, adapter = asker(tmp_path)
    a.answer("d", "Erste Frage?", RESULT)
    a.answer("d", "Und warum?", RESULT)
    assert "Frage: Erste Frage?" in adapter.calls[1]["input"]
    assert "Antwort: Laut Protokoll" in adapter.calls[1]["input"]


def test_questions_do_not_change_the_stored_result(tmp_path):
    a, _ = asker(tmp_path)
    before = json.loads(RESULT)
    a.answer("d", "Frage?", RESULT)
    assert json.loads(RESULT) == before


def test_unknown_references_are_dropped(tmp_path):
    a, _ = asker(tmp_path, {"MetaAnswer": lambda p, n: {"antwort": "Text.", "refs": ["KL-v1-1", "KL-erfunden"], "keine_grundlage": False}})
    assert a.answer("d", "Frage?", RESULT)["refs"] == ["KL-v1-1"]


def test_oversized_answer_gets_one_repair(tmp_path):
    def long_first(prompt, number):
        text = "Wort " * (ANSWER_WORDS + 5) if number == 1 else "Kurz."
        return {"antwort": text, "refs": [], "keine_grundlage": False}

    a, adapter = asker(tmp_path, {"MetaAnswer": long_first})
    assert a.answer("d", "Frage?", RESULT)["antwort"] == "Kurz."
    assert len(adapter.calls) == 2


def test_answer_may_not_propose_a_law_change(tmp_path):
    a, adapter = asker(tmp_path, {"MetaAnswer": lambda p, n: {"antwort": "Das ASVG soll geändert werden.", "refs": [], "keine_grundlage": False}})
    with pytest.raises(AskError) as info:
        a.answer("d", "Was tun?", RESULT)
    assert info.value.status == 502 and len(adapter.calls) == 2
    assert a.history("d") == []


@pytest.mark.parametrize("question, fragment", [("", "leer"), ("   ", "leer"), ("x" * 1501, "1500"), ("Mein api_key ist abc", "Zugangsdaten")])
def test_bad_questions_are_rejected_in_german(tmp_path, question, fragment):
    a, adapter = asker(tmp_path)
    with pytest.raises(AskError, match=fragment):
        a.answer("d", question, RESULT)
    assert adapter.calls == []


def test_unknown_debate_and_clarification_results(tmp_path):
    a, _ = asker(tmp_path)
    with pytest.raises(AskError) as info:
        a.answer("d", "Frage?", None)
    assert info.value.status == 404
    with pytest.raises(AskError) as info:
        a.answer("d", "Frage?", (ROOT / "examples" / "result_needs_clarification.json").read_text(encoding="utf-8"))
    assert info.value.status == 409


def test_blocked_while_a_debate_holds_the_lock(tmp_path):
    a, adapter = asker(tmp_path)
    lock = ProcessLock(tmp_path).acquire()
    try:
        with pytest.raises(AskError, match="läuft eine Debatte"):
            a.answer("d", "Frage?", RESULT)
    finally:
        lock.release()
    assert adapter.calls == []


def test_lock_is_released_after_a_failure(tmp_path):
    a, _ = asker(tmp_path, {"MetaAnswer": lambda p, n: LlmAuthError("Schlüssel abgelehnt.")})
    with pytest.raises(AskError) as info:
        a.answer("d", "Frage?", RESULT)
    assert info.value.status == 502 and "abgelehnt" in info.value.message
    ProcessLock(tmp_path).acquire().release()


def test_missing_credentials_never_fall_back_to_fake(tmp_path):
    a = Asker(tmp_path, llm="azure", env={"LLM_API_MODE": "responses_v1", "AZURE_OPENAI_API_KEY": "", "STATE_DIR": str(tmp_path)})
    with pytest.raises(AskError) as info:
        a.answer("d", "Frage?", RESULT)
    assert info.value.status == 500 and "fehlt" in info.value.message


def test_record_is_bounded_and_holds_no_raw_sources():
    result = json.loads(RESULT)
    record = dumps(debate_record(result, RECORD_TOKENS))
    assert estimate_tokens(record) <= RECORD_TOKENS and "ländlichen Raum" not in record


@pytest.mark.skipif(not (ROOT / "state" / "live_result_7.json").is_file(), reason="kein Live-Ergebnis")
def test_long_real_debate_fits_and_says_when_shortened():
    result = json.loads((ROOT / "state" / "live_result_7.json").read_text(encoding="utf-8"))
    record = debate_record(result, 16384)
    assert estimate_tokens(dumps(record)) <= 16384 and "gekuerzt" in record


def test_too_small_budget_is_an_explicit_error():
    with pytest.raises(AskError) as info:
        debate_record(json.loads(RESULT), 10)
    assert info.value.status == 413


def test_word_limit_of_ordinary_contributions_is_300():
    assert rules.WORD_LIMITS["ordinary"] == 300 and rules.WORD_LIMITS["assessment"] == 300 and ANSWER_WORDS == 320


def test_all_five_scenarios_load_and_can_start():
    items = scenarios()
    assert len(items) == 5 and len({s["label"] for s in items}) == 5
    for item in items:
        request, errors = parse_request(json.dumps(item["request"]))
        assert not errors, item["id"]
        assert clarification_needed(request) == [], item["id"]
        assert "_" not in item["label"]


def test_scenario_folder_is_part_of_the_project():
    assert Path(ROOT / "Beispielanfragen").is_dir()
