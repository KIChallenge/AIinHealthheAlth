"""Regressions from the first live debate, using real rejected model outputs.

The scripted fake always follows the rules, so it could not predict live behaviour.
`live_rejections.json` holds real outputs, and `Noisy` misbehaves the way the model did.
"""

import json
from pathlib import Path

import pytest
from conftest import check_result

from healthsim import llm_fake as fake
from healthsim import rules
from healthsim.llm_models import FinalOutput, ReviewOutput
from healthsim.sources import extract_terms, searchable_text

LIVE = json.loads((Path(__file__).parent / "live_rejections.json").read_text(encoding="utf-8"))


def as_review(output):
    return ReviewOutput.model_validate({"unreviewed_targets": [], **output})


# --- law-change rule ---------------------------------------------------------------

@pytest.mark.parametrize("text", LIVE["law_texts"])
def test_real_texts_ruling_out_law_changes_are_not_flagged(text):
    assert not rules.mentions_law_change(text)


def test_all_real_cases_are_in_the_fixture():
    assert len(LIVE["law_texts"]) == 27


@pytest.mark.parametrize("text", [
    "Das ASVG soll geändert werden, weil die Regelung nicht reicht.",
    "Wir schlagen eine Gesetzesnovelle zur Primärversorgung vor.",
    "Ein neues Gesetz regelt die Patientenlenkung.",
    "Die Regionalpiloten starten sofort; das Ärztegesetz wird angepasst.",
    "Keine neuen Fonds. Eine Gesetzesänderung sichert die Zweckwidmung.",
])
def test_real_proposals_to_change_law_stay_flagged(text):
    assert rules.mentions_law_change(text)


# --- search terms --------------------------------------------------------------------

def test_search_uses_content_not_json_field_names():
    content = json.dumps({"argument": {"argument_id": "ARG-x-v1", "claims": [
        {"claim_id": "c1", "kind": "reported", "behauptung": "Wartezeiten in Regionen steigen deutlich.",
         "begruendung": "Primärversorgung fehlt vielerorts im Land.", "quellen": ["P-abc"], "beispiel_art": "unavailable"}]}})
    assert "quellen" in extract_terms(content, 30) and "behauptung" in extract_terms(content, 30)
    terms = extract_terms(searchable_text(content), 30)
    assert "wartezeiten" in terms and "primärversorgung" in terms
    assert not {"quellen", "behauptung", "begruendung", "claims", "argument", "reported", "unavailable"} & set(terms)


def test_real_review_subject_is_searchable():
    terms = extract_terms(searchable_text(json.dumps(LIVE["over_limit_reviews"][0]["output"], ensure_ascii=False)), 12)
    assert terms and not {"findings", "target_id", "assessment", "coverage"} & set(terms)


# --- real outputs through the validators -----------------------------------------------

@pytest.mark.parametrize("sample", LIVE["wrong_target_reviews"], ids=lambda s: s["role"])
def test_real_wrong_targets_are_rejected_with_the_allowed_ids(sample):
    problems = rules.validate_review(as_review(sample["output"]), sample["role"], {"AS-1"}, set())
    assert any("erlaubt sind: AS-1" in p for p in problems)


def test_real_review_words_exclude_program_labels():
    review = as_review(LIVE["over_limit_reviews"][0]["output"])
    rendered = rules.render_review(review)
    assert "Bewertung:" not in rendered and rules.count_words(rendered) > 0


def test_real_final_output_without_proposal_is_accepted():
    final = FinalOutput.model_validate(LIVE["final_outputs"][0]["output"])
    assert final.assessments                       # the model did rate priorities
    problems = rules.validate_final(final, [], [])
    assert not [p for p in problems if "Unbekannte Prioritäten" in p]


# --- workflow with real and noisy behaviour ----------------------------------------------

def test_real_invented_constraints_are_ignored(harness):
    def candidate(prompt, number):
        data = fake.candidate(prompt)
        data["constraint_checks"] = LIVE["invented_constraint_checks"][0]
        return data

    result = harness(overrides={"CandidateOutput": candidate}).run()
    assert result["status"] == "completed" and result["usage"]["repairs"] == 0
    assert all(p["constraint_checks"] == [] for p in result["proposal_history"])


def test_missing_constraint_check_is_still_rejected(harness):
    from conftest import make_request
    result = harness().run(make_request(hard_constraints=[{"id": "C-1", "description": "Kein Zusatzpersonal."}]))
    assert result["usage"]["repairs"] == 0
    assert result["final_proposal"]["constraint_checks"][0]["constraint_id"] == "C-1"

    h = harness(overrides={"CandidateOutput": lambda p, n: {**fake.candidate(p), "constraint_checks": []}},
                DELIBERATION_TIMEOUT_SECONDS=60, delay=1)
    result = h.run(make_request(hard_constraints=[{"id": "C-1", "description": "Kein Zusatzpersonal."}]), "d2")
    assert result["proposal_history"] == []


def noisy_overrides():
    """First attempts break the rules the way the live model did; repairs then succeed."""
    filler = " ".join(["Begründung"] * 120)

    def review(prompt, number):
        data = fake.review(prompt)
        corrected = "Deine vorige Antwort war ungültig" in prompt
        if corrected:
            return data
        if number % 3 == 0:
            data["findings"][0]["reason"] = filler
        elif number % 3 == 1:
            data["findings"][0]["target_id"] = "Ärztekammer"
        else:
            data["coverage"], data["coverage_note"] = "incomplete", "Es fehlen Daten."
        return data

    def initial(prompt, number):
        data = fake.initial(prompt)
        if "Deine vorige Antwort war ungültig" not in prompt:
            data["argument"]["claims"][0]["begruendung"] = filler
        return data

    return {"ReviewOutput": review, "InitialOutput": initial,
            "CandidateOutput": lambda p, n: {**fake.candidate(p), "constraint_checks": LIVE["invented_constraint_checks"][0]}}


def test_debate_survives_realistic_misbehaviour(harness):
    h = harness(overrides=noisy_overrides())
    result = h.run()
    check_result(result)
    assert result["status"] == "completed" and result["stop_reason"] == "satisfaction_threshold"
    assert result["usage"]["repairs"] >= 30
    failures = [e for e in h.events() if e["type"] == "validation_failure"]
    assert any("Fehlende Belege sind ein Befund" in e["payload"]["reason"] for e in failures)
    assert result["final_proposal"]["review_complete"] is True


def test_no_proposal_keeps_real_closing_statements(harness):
    def final(prompt, number):
        return LIVE["final_outputs"][0]["output"]

    h = harness(overrides={"NegotiationOutput": lambda p, n: {"items": [], "corrections": []}, "FinalOutput": final},
                DELIBERATION_TIMEOUT_SECONDS=120, delay=1)
    result = h.run()
    check_result(result)
    assert result["final_proposal"] is None and result["stop_reason"] == "deliberation_deadline"
    assert all(a["final_standpoint"] for a in result["actors"])
    assert not any(w["message"].startswith("final:") for w in result["warnings"])
    assert not [d for d in result["decisions"] if d["decision_id"].startswith("D-final")]


def test_final_assessments_use_the_final_allowance(harness):
    def final(prompt, number):
        data = fake.final(prompt)
        for item in data["assessments"]:
            item["reason"] = " ".join(["Bewertung"] * 80)
        return data

    result = harness(overrides={"FinalOutput": final}).run()
    assert result["status"] == "completed"


def test_legaleye_must_address_every_clause(harness):
    def candidate(prompt, number):
        data = fake.candidate(prompt)
        data["clauses"].append({**data["clauses"][0], "text": "Die Partner veröffentlichen gemeinsame Kennzahlen."})
        return data

    # The fake reviewer never covers the second clause, so no round can succeed; the delay lets
    # virtual time reach the deadline.
    h = harness(overrides={"CandidateOutput": candidate}, DELIBERATION_TIMEOUT_SECONDS=150, delay=1)
    result = h.run()
    failures = [e for e in h.events() if e["type"] == "validation_failure"]
    assert any("braucht einen Befund" in e["payload"]["reason"] for e in failures)
    assert result["metrics"]["completed_rounds"] == 0
