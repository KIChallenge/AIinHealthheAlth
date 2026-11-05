"""Deterministic rules, intake and source permissions."""

import asyncio
import json

import pytest
from conftest import FIXTURE, make_request

from healthsim import rules
from healthsim.api_models import DebateRequest
from healthsim.errors import PermissionDeniedError, SourceError
from healthsim.events import EventStore
from healthsim.intake import (clarification_needed, contains_secret, normalise_task, parse_request, resolve_actors)
from healthsim.llm_models import (Argument, Calculation, CalculationInput, Claim, FindingOutput, Priority, ReviewOutput)
from healthsim.sources import (ACTOR, ETHICMIND, LEGALEYE, META, TRUTHSEEKER, FixtureSources, Role, SourceService,
                               extract_terms, parse_sozdok, passages_from, split_markdown)

KNOWN = [{"actor_id": "gm", "name": "Gesundheitsministerium"}, {"actor_id": "ak", "name": "Ärztekammer"}]


def claim(**changes):
    data = {"kind": "reported", "behauptung": "Die Wartezeit ist lang.", "begruendung": "Laut Quelle.",
            "beispiel": "Beispiel nicht verfügbar.", "beispiel_art": "unavailable", "quellen": ["P-1"], "berechnung": None}
    data.update(changes)
    return Claim(**data)


# --- request ------------------------------------------------------------------

def test_minimal_and_complete_requests():
    minimal, errors = parse_request(json.dumps({"actors": ["Ärztekammer"], "question": "Eine Frage?"}))
    assert not errors and minimal.schema_version == "1.0" and minimal.scenario_assumptions == []
    full, errors = parse_request(json.dumps({
        "schema_version": "1.0", "actors": ["Ärztekammer"], "question": "Frage", "reference_date": "2026-11-05",
        "scenario_assumptions": [{"description": "Budget bleibt stabil"}],
        "hard_constraints": [{"description": "Kostendeckel", "metric": "Kosten", "operator": "<=", "value": 5,
                              "unit": "Mio EUR", "baseline": "2025", "period": "2027"}]}))
    assert not errors and full.hard_constraints[0].value == 5


@pytest.mark.parametrize("raw, fragment", [
    ("{kein json", "kein gültiges JSON"), ("[1]", "JSON-Objekt"), ('{"actors": ["A"]}', "question"),
    ('{"actors": [], "question": "F"}', "actors"), ('{"actors": ["A"], "question": "  "}', "leer"),
    ('{"actors": ["A"], "question": "F", "language": "de"}', "unbekanntes Feld"),
    ('{"actors": ["A"], "question": "F", "allow_law_changes": true}', "unbekanntes Feld"),
    ('{"actors": ["A"], "question": "F", "api_key": "x"}', "unbekanntes Feld"),
    ('{"actors": ["A"], "question": "F", "reference_date": "5.11.2026"}', "JJJJ-MM-TT"),
])
def test_bad_requests_give_german_errors(raw, fragment):
    request, errors = parse_request(raw)
    assert request is None and any(fragment in e.message for e in errors)


def test_input_schema_is_strict():
    schema = DebateRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"actors", "question"}
    assert "language" not in schema["properties"] and "allow_law_changes" not in schema["properties"]


def test_credentials_in_text_are_detected():
    assert contains_secret(make_request(question="Mein api_key lautet abc"))
    assert contains_secret(make_request(question="Bitte nutze zzz-123"), ["zzz-123"])
    assert not contains_secret(make_request())


def test_actor_resolution():
    assert resolve_actors(["Ärztekammer"], KNOWN, {})[0][0]["actor_id"] == "ak"
    assert resolve_actors(["ÖÄK"], KNOWN, {"ÖÄK": "Ärztekammer"})[0][0]["requested_name"] == "ÖÄK"
    assert resolve_actors(["Ministerium"], KNOWN, {})[1][0].code == "unknown_actor"
    assert resolve_actors(["ärztekammer"], KNOWN, {})[1][0].code == "unknown_actor"
    assert resolve_actors(["Ärztekammer", "ÖÄK"], KNOWN, {"ÖÄK": "Ärztekammer"})[1][0].code == "duplicate_actor"
    assert resolve_actors(["LegalEye"], KNOWN, {})[1][0].code == "reviewer_as_actor"
    twice = KNOWN + [{"actor_id": "x", "name": "Ärztekammer"}]
    assert resolve_actors(["Ärztekammer"], twice, {})[1][0].code == "ambiguous_actor"


@pytest.mark.parametrize("question", [
    "Wie muss das ASVG geändert werden, um die Versorgung zu sichern?", "Entwerfen Sie eine Gesetzesnovelle zur Versorgung.",
    "Welche Gesetzesänderung ist nötig?", "Schlagen Sie ein neues Gesetz für Primärversorgung vor.",
    "Die Verordnung soll novelliert werden."])
def test_law_change_requests_need_clarification(question):
    codes = [q.code for q in clarification_needed(make_request(question=question))]
    assert codes == ["law_change_out_of_scope"]


@pytest.mark.parametrize("question", [
    "Verbessern Sie die Versorgung ohne Gesetzesänderung.", "Wie geht das innerhalb des geltenden Rechtsrahmens?",
    "Es ist keine Gesetzesnovelle vorgesehen, wie gehen wir vor?",
    "Umsetzung nur ohne Gesetzes- oder Verordnungsänderung.",
    "Nur umsetzbar ohne Gesetzes- oder Verordnungsänderung; Annahme: bestehende Schnittstellen reichen.",
    "Rechtsänderungen und neue Fonds sind ausgeschlossen.", "Eine Gesetzesänderung ist nicht erforderlich.",
    "Alle Klauseln bleiben im geltenden Recht; Rechtsänderungen werden ausgeschlossen."])
def test_negated_law_mentions_are_fine(question):
    assert clarification_needed(make_request(question=question)) == []


def test_missing_reference_date_is_clarified_not_substituted():
    questions = clarification_needed(make_request(reference_date=None))
    assert [q.code for q in questions] == ["reference_date_missing"] and "Stichtag" in questions[0].frage


def test_ambiguous_percentage_is_clarified():
    request = make_request(hard_constraints=[{"description": "Kosten dürfen um höchstens 5 % steigen."}])
    assert [q.code for q in clarification_needed(request)] == ["ambiguous_percentage"]
    clear = make_request(hard_constraints=[{"description": "Kosten höchstens 5 %", "metric": "Kosten", "operator": "<=",
                                            "value": 5, "unit": "%", "baseline": "2025", "period": "2027"}])
    assert clarification_needed(clear) == []


def test_generated_ids_are_deterministic():
    request = make_request(scenario_assumptions=[{"description": "Budget stabil"}])
    a = normalise_task(request)["assumptions"][0]["id"]
    assert a == normalise_task(request)["assumptions"][0]["id"] and a.startswith("A-")


# --- word rules ----------------------------------------------------------------

def test_word_count_uses_whitespace_units():
    assert rules.count_words("eins  zwei\ndrei") == 3


def test_word_limit_counts_model_text_and_citations_but_not_program_labels():
    long_claim = claim(behauptung=" ".join(["Wort"] * 95), quellen=["P-1"])
    rendered = rules.render_argument(Argument(claims=[long_claim]))
    assert "Behauptung:" not in rendered and "P-1" in rendered
    assert rules.word_problem(rendered, "ordinary", "A")          # 95 + 2 + 3 + 1 citation = 101
    shorter = Argument(claims=[claim(behauptung=" ".join(["Wort"] * 94))])
    assert not rules.word_problem(rules.render_argument(shorter), "ordinary", "A")


def test_review_schemas_only_offer_the_roles_own_assessments():
    from pydantic import ValidationError

    from healthsim import llm_models as lm
    good = {"target_id": "T", "assessment": "no_conflict_identified", "reason": "r", "reference": "x", "correction": None}
    wrapped = {"findings": [good], "coverage": "complete", "unreviewed_targets": [], "coverage_note": None}
    assert lm.LegalReviewOutput.model_validate(wrapped)
    with pytest.raises(ValidationError):
        lm.TruthReviewOutput.model_validate(wrapped)
    schema = json.dumps(lm.strict_schema(lm.TruthReviewOutput))
    assert "no_conflict_identified" not in schema and "qualified_support" in schema


@pytest.mark.parametrize("name, value", [("ordinary", 100), ("profile", 300), ("priorities", 600), ("assessment", 100),
                                         ("final_summary", 600), ("conclusion", 2000), ("evolution", 800)])
def test_budgets_match_the_specification(name, value):
    assert rules.WORD_LIMITS[name] == value


# --- claims --------------------------------------------------------------------

def test_valid_argument_passes():
    assert rules.validate_claims(Argument(claims=[claim()]), {"P-1"}, []) == []


@pytest.mark.parametrize("changes, fragment", [
    ({"quellen": ["P-9"]}, "nicht aufgelöst"), ({"quellen": []}, "brauchen eine Quelle"),
    ({"beispiel": "keins", "beispiel_art": "unavailable"}, "nicht verfügbar"),
    ({"beispiel": "Ein Fall.", "beispiel_art": "hypothetical"}, "gekennzeichnet"),
    ({"kind": "proposal", "behauptung": "Das ASVG soll geändert werden.", "quellen": []}, "ausgeschlossen"),
])
def test_claim_problems(changes, fragment):
    assert any(fragment in p for p in rules.validate_claims(Argument(claims=[claim(**changes)]), {"P-1"}, []))


def test_proposal_needs_no_source_but_assumption_ids_resolve():
    proposal = claim(kind="proposal", behauptung="Regionale Abstimmung einführen.", quellen=["A-1"])
    assert rules.validate_claims(Argument(claims=[proposal]), set(), ["A-1"]) == []


def calc(operation, values, result):
    return Calculation(operation=operation, unit="EUR", result=result,
                       inputs=[CalculationInput(value=v, unit="EUR", period="2025", quelle="P-1") for v in values])


@pytest.mark.parametrize("operation, values, result, ok", [
    ("sum", [1, 2, 3], 6, True), ("difference", [5, 2], 3, True), ("product", [2, 3], 6, True), ("ratio", [1, 4], 0.25, True),
    ("percent_change", [110, 100], 10, True), ("sum", [1, 2], 4, False), ("ratio", [1, 0], 0, False),
])
def test_constrained_arithmetic(operation, values, result, ok):
    assert (rules.check_calculation(calc(operation, values, result), {"P-1"}, [], "x") == []) is ok


def test_calculation_inputs_need_sources():
    assert rules.check_calculation(calc("sum", [1], 1), set(), [], "x")


def priority(**changes):
    data = {"priority_id": None, "objective": "Zugang sichern", "acceptance_condition": "Wartezeit sinkt", "quellen": ["P-1"],
            "evidence_basis": "reported", "condition_is_simulated": False, "law_scope_conflict": False}
    data.update(changes)
    return Priority(**data)


def test_priority_rules():
    assert rules.validate_priorities([priority()], {"P-1"}) == []
    assert rules.validate_priorities([priority(), priority()], {"P-1"})
    dropped = rules.validate_priorities([priority(priority_id="PR-1")], {"P-1"}, ["PR-1", "PR-2"])
    assert any("nicht entfallen" in p for p in dropped)
    assert rules.validate_priorities([priority(priority_id="PR-9")], {"P-1"}, ["PR-9"]) == []


def finding(assessment="supported", target="T"):
    return FindingOutput(target_id=target, assessment=assessment, reason="Grund", reference="R", correction=None)


def test_review_rules():
    def report(findings, coverage="complete", unreviewed=(), note=None):
        return ReviewOutput(findings=findings, coverage=coverage, unreviewed_targets=list(unreviewed), coverage_note=note)

    assert rules.validate_review(report([finding()]), "truthseeker", {"T"}, set()) == []
    assert rules.validate_review(report([finding("no_conflict_identified")]), "truthseeker", {"T"}, set())
    assert rules.validate_review(report([finding(target="X")]), "truthseeker", {"T"}, set())
    assert rules.validate_review(report([]), "truthseeker", {"T"}, set())
    assert rules.validate_review(report([finding()], "incomplete", ["T"]), "truthseeker", {"T"}, set())
    assert rules.validate_review(report([finding()], "incomplete", ["T"], "Nicht geprüft."), "truthseeker", {"T"}, set()) == []


def test_missing_evidence_is_not_a_coverage_gap():
    review = ReviewOutput(findings=[finding()], coverage="incomplete", unreviewed_targets=[],
                          coverage_note="Es fehlen Daten zu Wartezeiten.")
    assert any("Befund" in p for p in rules.validate_review(review, "truthseeker", {"T"}, set()))


def test_unreviewed_targets_must_exist_and_match_coverage():
    unknown = ReviewOutput(findings=[finding()], coverage="incomplete", unreviewed_targets=["Y"], coverage_note="x")
    assert rules.validate_review(unknown, "truthseeker", {"T"}, set())
    contradictory = ReviewOutput(findings=[finding()], coverage="complete", unreviewed_targets=["T"], coverage_note=None)
    assert rules.validate_review(contradictory, "truthseeker", {"T"}, set())


# --- satisfaction --------------------------------------------------------------

@pytest.mark.parametrize("statuses, reached, percent", [
    (["met"] * 4 + ["unmet"], True, 80.0), (["met"] * 3 + ["unmet"], False, 75.0), (["met"] * 5, True, 100.0),
    (["met"] * 4 + ["partly_met"], True, 80.0), (["met", "partly_met"], False, 50.0), (["met", "unclear", "unclear"], False, 33.3),
    (["met"] * 79 + ["unmet"] * 21, False, 79.0),
])
def test_exact_threshold(statuses, reached, percent):
    result = rules.satisfaction("a", statuses)
    assert result["threshold_reached"] is reached and result["percent"] == percent


def test_threshold_is_integer_exact_at_four_fifths():
    assert rules.satisfaction("a", ["met"] * 4 + ["unmet"])["met_count"] * 5 == 4 * 5


def test_zero_denominator_is_null():
    result = rules.satisfaction("a", [])
    assert result["percent"] is None and result["threshold_reached"] is None


def sat(reached):
    return [{"threshold_reached": reached}]


def test_streak():
    assert rules.next_streak(0, True, sat(True)) == 1 and rules.next_streak(1, True, sat(True)) == 2
    assert rules.next_streak(1, False, sat(True)) == 0 and rules.next_streak(1, True, sat(False)) == 0
    assert rules.next_streak(1, True, sat(None)) == 0 and rules.next_streak(1, True, []) == 0


def test_legal_admissibility():
    assert rules.worst_legal(["no_conflict_identified", "unresolved_issue"]) == "unresolved_issue"
    assert rules.worst_legal([]) == "not_reviewed"
    assert rules.admissibility(["no_conflict_identified"]) == "admissible_within_reviewed_sources"
    assert rules.admissibility(["no_conflict_identified", "defensible_with_conditions"]) == "conditional"
    assert rules.admissibility(["unresolved_issue"]) == "unresolved"
    assert rules.admissibility(["requires_legal_amendment"]) == "inadmissible"
    assert rules.admissibility(["no_conflict_identified", "not_reviewed"]) == "not_reviewed"


# --- sources -------------------------------------------------------------------

def adapter():
    return FixtureSources(FIXTURE["actors"], FIXTURE["documents"])


def search(role, doc_type=None, ref="2026-11-05"):
    rows = asyncio.run(adapter().search(role, ["versorgung", "ambulanten"], ref, 10, doc_type))
    return {r["doc_id"] for r in rows}


def test_actor_sees_only_own_profile_and_connected_articles():
    role = Role(ACTOR, "sozialversicherung")
    assert search(role, "ActorData") == {"d-sv"} and search(role, "Article") == {"d-a1"}


def test_reviewer_permissions():
    assert search(Role(LEGALEYE)) == {"d-l1", "d-l2"} and search(Role(ETHICMIND)) == {"d-e1"}
    assert search(Role(TRUTHSEEKER), "Article") == {"d-a1", "d-a2"}
    assert search(Role(TRUTHSEEKER), "ActorData") == {"d-gm", "d-sv", "d-ak"}
    assert search(Role(TRUTHSEEKER), "LegalText") == set()


def test_meta_moderator_cannot_search():
    with pytest.raises(PermissionDeniedError):
        search(Role(META))


def test_later_sources_are_hidden_for_a_historical_date():
    assert search(Role(ACTOR, "sozialversicherung"), "Article", ref="2020-01-01") == set()
    assert search(Role(LEGALEYE), ref="2019-01-01") == set()


def test_sozdok_keeps_structure_and_references():
    doc = next(d for d in FIXTURE["documents"] if d["doc_id"] == "d-l1")
    blocks, refs = parse_sozdok(doc["original_content"])
    assert blocks[0]["text"] == "Geltungsbereich"
    assert blocks[1]["machine"] == "sozdok/rvo/nutzdaten/abschnitt[1]/absatz[1]"
    assert blocks[1]["heading"] == "Geltungsbereich" and "§ 1." in blocks[1]["text"]
    assert {"law": "TestG", "section_type": "PARA", "section_number": "2", "label": "§ 2"} in refs


def test_external_entities_are_rejected():
    evil = '<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]><sozdok>&x;</sozdok>'
    with pytest.raises(SourceError, match="DOCTYPE"):
        parse_sozdok(evil)


def test_xml_passage_has_locators_hash_and_context():
    doc = next(d for d in FIXTURE["documents"] if d["doc_id"] == "d-l1")
    passage = passages_from(doc, ["versorgung"], 1, 160)[0]
    assert passage["content_hash"] == "hash-l1" and passage["passage_id"].startswith("P-")
    assert passage["machine_locator"].startswith("sozdok/") and "TestG § 1" in passage["locator"]


def test_markdown_chunks_never_exceed_much_over_limit():
    text = "\n\n".join(" ".join(["wort"] * 70) for _ in range(5))
    assert all(len(c[2].split()) <= 160 for c in split_markdown(text, 160))


def test_terms_are_deterministic_and_skip_stopwords():
    terms = extract_terms("Die Versorgung und die Versorgung sowie Finanzierung innerhalb Österreichs")
    assert terms[0] == "versorgung" and "innerhalb" not in terms
    assert terms == extract_terms("Die Versorgung und die Versorgung sowie Finanzierung innerhalb Österreichs")


def service(tmp_path, settings):
    return SourceService(adapter(), EventStore(tmp_path / "e.sqlite"), settings)


def test_references_resolved_and_missing_reported(tmp_path, settings):
    svc = service(tmp_path, settings)
    passages, warnings = asyncio.run(svc.retrieve("d", Role(LEGALEYE), "ambulante Versorgung Finanzierung", "2026-11-05", 1))
    assert "d-l2" in {p["doc_id"] for p in passages}
    assert any("§ 9 FehltG" in w and "nicht aufgelöst" in w for w in warnings)
    svc.events.close()


def test_citation_lookup_only_for_debate_citations(tmp_path, settings):
    svc = service(tmp_path, settings)
    passages, _ = asyncio.run(svc.retrieve("d", Role(LEGALEYE), "ambulante Versorgung", "2026-11-05", 1))
    pid = passages[0]["passage_id"]
    found, same = asyncio.run(svc.citation_context("d", pid, {pid}))
    assert found["text"] == passages[0]["text"] and same is True
    with pytest.raises(PermissionDeniedError):
        asyncio.run(svc.citation_context("d", pid, set()))
    svc.events.close()


def test_changed_source_is_detected_via_stored_version(tmp_path, settings):
    svc = service(tmp_path, settings)
    passages, _ = asyncio.run(svc.retrieve("d", Role(LEGALEYE), "ambulante Versorgung", "2026-11-05", 1))
    pid = passages[0]["passage_id"]
    next(d for d in svc.adapter.documents if d["doc_id"] == passages[0]["doc_id"])["content_hash"] = "neu"
    found, same = asyncio.run(svc.citation_context("d", pid, {pid}))
    assert same is False and found["content_hash"] != "neu"
    next(d for d in svc.adapter.documents if d["doc_id"] == passages[0]["doc_id"])["content_hash"] = found["content_hash"]
    svc.events.close()


def test_events_are_idempotent_and_append_only(tmp_path):
    store = EventStore(tmp_path / "e.sqlite")
    assert store.emit("d", "x", 1.0, "r", 0, "p", "public", "t") is True
    assert store.emit("d", "x", 2.0, "r", 0, "p", "public", "anders") is False
    events = store.events("d")
    assert len(events) == 1 and events[0]["type"] == "t" and events[0]["event_id"] == "x"
    store.close()
