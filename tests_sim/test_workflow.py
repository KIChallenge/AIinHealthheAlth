"""The executable LangGraph workflow, with controlled fake answers and an injected clock."""

import asyncio
import json
import re
import subprocess
import sys

import pytest
from conftest import FIXTURE, ROOT, Harness, check_result, make_request

from healthsim.app import Runtime, export_diagram, run_text
from healthsim.assemble import collect_reference_problems
from healthsim.errors import LlmAuthError, LlmDeploymentError, LlmTransientError
from healthsim.llm import RawResponse
from healthsim import llm_fake as fake
from healthsim.workflow import DebateState


def assess_with(status_for):
    """AssessOutput override: status_for(call_number, priority_id) -> status."""
    def handler(prompt, number):
        data = fake.assess(prompt)
        for item in data["assessments"]:
            item["status"] = status_for(number, item["priority_id"])
        return data
    return handler


def counts(harness):
    result = {}
    for call in harness.adapter.calls:
        result[call["schema"]] = result.get(call["schema"], 0) + 1
    return result


def test_happy_path_stops_after_two_successful_rounds(harness):
    h = harness()
    result = h.run()
    check_result(result)
    assert result["status"] == "completed" and result["stop_reason"] == "satisfaction_threshold"
    assert result["metrics"]["completed_rounds"] == 2 and result["metrics"]["success_streak"] == 2
    assert all(s["threshold_reached"] for s in result["metrics"]["satisfaction"])
    assert result["final_proposal"]["status"] == "frozen" and result["final_proposal"]["version"] == 2
    assert result["errors"] == []


def test_workflow_call_sequence_for_three_actors(harness):
    h = harness()
    h.run()
    c = counts(h)
    assert c["InitialOutput"] == 3 and c["RevisionOutput"] == 3
    # 9 initial + 9 second + two rounds of 3 candidate + 9 assessment + 3 final
    assert c["ReviewOutput"] == 9 + 9 + 2 * (3 + 9) + 3
    assert c["NegotiationOutput"] == 6 and c["CandidateOutput"] == 2 and c["AssessOutput"] == 6
    assert c["FinalOutput"] == 3 and c["ReportOutput"] == 1 and c["EvolutionOutput"] == 1


def test_variable_actor_counts(harness):
    for names in (["Ärztekammer"], ["Ärztekammer", "Sozialversicherung"]):
        h = harness()
        result = h.run(make_request(actors=names), debate_id=f"d-{len(names)}")
        assert result["status"] == "completed" and len(result["actors"]) == len(names)
    actors = FIXTURE["actors"] + [{"actor_id": f"x{i}", "name": f"Institution {i}"} for i in range(3)]
    docs = FIXTURE["documents"] + [
        {"doc_id": f"d-x{i}", "doc_type": "ActorData", "owner": f"x{i}", "title": "T", "content_hash": f"h{i}",
         "text": "Die ambulante Versorgung und der Zugang.", "content_format": "markdown"} for i in range(3)]
    h = harness(actors=actors, documents=docs)
    names = [a["name"] for a in actors]
    assert h.run(make_request(actors=names), debate_id="d-6")["status"] == "completed"
    assert h.runtime.scheduler.peak_active <= 9


def test_all_generated_text_and_prompts_are_german(harness):
    h = harness()
    h.run()
    for call in h.adapter.calls:
        assert "Regelwerk" in call["instructions"] and "Deutsch" in call["instructions"]


def test_every_request_uses_deployment_and_no_shared_conversation(harness):
    h = harness()
    h.run()
    assert {c["model"] for c in h.adapter.calls} == {"test-deployment"}


def test_clarification_before_any_timer_or_model_call(harness):
    h = harness()
    result = h.run(make_request(question="Welche Gesetzesänderung braucht das ASVG?"))
    check_result(result)
    assert result["status"] == "needs_clarification" and result["timing"] is None and h.adapter.calls == []
    assert "geltenden" in result["clarification_questions"][0]["frage"] and result["actors"] == []


def test_missing_reference_date(harness):
    result = harness().run(make_request(reference_date=None))
    assert [q["code"] for q in result["clarification_questions"]] == ["reference_date_missing"]


def test_unknown_duplicate_and_reviewer_actors(harness):
    h = harness()
    result = h.run(make_request(actors=["Gesundheitsministerium", "Patientenanwalt"]), "a")
    assert result["status"] == "invalid_input" and result["errors"][0]["code"] == "unknown_actor" and not h.adapter.calls
    assert harness().run(make_request(actors=["Ärztekammer", "ÖÄK"]), "b")["errors"][0]["code"] == "duplicate_actor"
    assert harness().run(make_request(actors=["TruthSeeker"]), "c")["errors"][0]["code"] == "reviewer_as_actor"


def test_credentials_in_question_are_rejected(harness):
    result = harness().run(make_request(question="Der api_key ist abc, bitte prüfen."))
    assert result["status"] == "invalid_input" and result["errors"][0]["code"] == "credentials_in_request"


# --- privacy --------------------------------------------------------------------

def actor_of(prompt_call):
    match = re.search(r"Institution (\S+) in einer", prompt_call["instructions"])
    return match.group(1) if match else None


def test_initial_privacy_and_source_permissions(harness):
    h = harness()
    h.run()
    for call in h.prompts("InitialOutput"):
        assert "Fixture" in call["input"]
        name = actor_of(call)
        own = {"Gesundheitsministerium": "Fixture Strategie", "Sozialversicherung": "Fixture Jahresbericht",
               "Ärztekammer": "Fixture Resolution"}
        for institution, title in own.items():
            assert (title in call["input"]) is (institution == name)
        assert "TestG" not in call["input"] and "Patientencharta" not in call["input"]


def test_no_other_actor_material_before_publication(harness):
    secret = {"Sozialversicherung": "GEHEIMPOSITION-SV"}

    def initial(prompt, number):
        data = fake.initial(prompt)
        if "für Sozialversicherung" in prompt:
            data["profile"]["standpunkt"] = secret["Sozialversicherung"]
        return data

    h = harness(overrides={"InitialOutput": initial})
    h.run()
    for schema in ("InitialOutput", "RevisionOutput"):
        for call in h.prompts(schema):
            if actor_of(call) != "Sozialversicherung":
                assert "GEHEIMPOSITION-SV" not in call["input"]
    assert any("GEHEIMPOSITION-SV" in c["input"] for c in h.prompts("NegotiationOutput"))


def test_all_actors_receive_the_same_package(harness):
    h = harness()
    h.run()
    def package(prompt):
        return prompt.split("Gemeinsames veröffentlichtes Paket:")[1].split("Runde:")[0]
    first_round = h.prompts("NegotiationOutput")[:3]
    assert len({package(c["input"]) for c in first_round}) == 1


def test_all_actors_assess_the_identical_proposal(harness):
    h = harness()
    h.run()
    def proposal(prompt):
        return re.search(r"Aktueller Vorschlag: (.*)", prompt).group(1)
    first = h.prompts("AssessOutput")[:3]
    assert len({proposal(c["input"]) for c in first}) == 1


def test_secrets_never_reach_prompts_output_or_events(harness):
    h = harness()
    result = h.run()
    blob = json.dumps(result) + json.dumps(h.adapter.calls) + json.dumps(h.events())
    assert "geheimer-testschluessel" not in blob and "neo4j-geheim" not in blob


def test_result_contains_no_raw_source_text(harness):
    result = harness().run()
    assert "ländlichen Raum" not in json.dumps(result, ensure_ascii=False)


def test_uses_stored_snapshots_for_cited_context(harness):
    h = harness()
    result = h.run()
    pid = result["arguments"][0]["claims"][0]["quellen"][0]
    assert h.runtime.events.passage("debatte-1", pid)["text"]


# --- repairs, reviews, challenges ----------------------------------------------

def test_oversized_output_gets_one_repair_and_rejected_version_is_kept(harness):
    def long_first(prompt, number):
        data = fake.negotiation(prompt)
        if number == 1:
            data["items"][0]["text"] = " ".join(["Wort"] * 320)
        return data

    h = harness(overrides={"NegotiationOutput": long_first})
    result = h.run()
    assert result["usage"]["repairs"] == 1
    failures = [e for e in h.events() if e["type"] == "validation_failure"]
    assert len(failures) == 1 and failures[0]["payload"]["rejected"]["items"][0]["text"].startswith("Wort")
    assert any("Deine vorige Antwort war ungültig" in c["input"] for c in h.prompts("NegotiationOutput"))


def test_failed_repair_marks_incomplete_and_keeps_actor(harness):
    def always_long(prompt, number):
        data = fake.initial(prompt)
        if "für Ärztekammer" in prompt:
            data["argument"]["claims"][0]["begruendung"] = " ".join(["Wort"] * 350)
        return data

    h = harness(overrides={"InitialOutput": always_long}, DELIBERATION_TIMEOUT_SECONDS=120, delay=2)
    result = h.run()
    check_result(result)
    assert any(a["actor_id"] == "aerztekammer" for a in result["actors"])
    assert any("prepare:aerztekammer" in w["message"] for w in result["warnings"])
    assert result["metrics"]["completed_rounds"] == 0 and result["final_proposal"] is None
    assert result["stop_reason"] == "deliberation_deadline"


def test_refusal_is_repaired_once_then_incomplete(harness):
    h = harness(overrides={"ChallengesOutput": lambda p, n: None,
                           "FinalOutput": lambda p, n: RawResponse("completed", None, refusal="abgelehnt")})
    result = h.run()
    assert result["status"] == "incomplete"
    assert any("final:" in w["message"] and "verweigert" in w["message"] for w in result["warnings"])
    assert counts(h)["FinalOutput"] == 6


def test_token_exhausted_output_is_explicit(harness):
    h = harness(overrides={"ReportOutput": lambda p, n: RawResponse("incomplete", None, incomplete_reason="max_output_tokens")})
    result = h.run()
    assert result["status"] == "incomplete" and result["report"]["fehlende_abschnitte"]
    assert any("Ausgabelimit" in w["message"] for w in result["warnings"])


def material_review(prompt, number):
    data = fake.review(prompt)
    if "Rolle=truthseeker, Stufe=initial" in prompt:
        data["findings"][0]["assessment"] = "no_support"
        data["findings"][0]["reason"] = "Die Quelle trägt die Behauptung nicht."
    return data


def test_challenge_and_reply_are_bounded(harness):
    def challenge(prompt, number):
        ids = fake.ids_after(prompt, "Befund-IDs")
        return {"challenges": [{"finding_id": i, "reason": "Die Quelle stützt dies sehr wohl."} for i in ids]}

    h = harness(overrides={"ReviewOutput": material_review, "ChallengesOutput": challenge})
    result = h.run()
    assert len(result["challenges"]) == 3
    assert all(c["status"] == "answered" and c["reply_decision"] == "uphold" for c in result["challenges"])
    assert counts(h)["ChallengesOutput"] == 3 and counts(h)["RepliesOutput"] == 1


def test_duplicate_challenge_on_one_finding_is_rejected(harness):
    def duplicate(prompt, number):
        ids = fake.ids_after(prompt, "Befund-IDs")
        return {"challenges": [{"finding_id": ids[0], "reason": "A"}, {"finding_id": ids[0], "reason": "B"}]}

    h = harness(overrides={"ReviewOutput": material_review, "ChallengesOutput": duplicate})
    result = h.run()
    assert any("challenge:" in w["message"] and "nur ein Widerspruch" in w["message"] for w in result["warnings"])


def test_private_feedback_stays_with_the_target_actor(harness):
    def review(prompt, number):
        data = material_review(prompt, number)
        if "Rolle=truthseeker, Stufe=initial" in prompt and '"ARG-sozialversicherung-v1"' in prompt:
            data["findings"][0]["reason"] = "PRIVATER-BEFUND"
        return data

    h = harness(overrides={"ReviewOutput": review})
    h.run()
    for call in h.prompts("RevisionOutput"):
        assert ("PRIVATER-BEFUND" in call["input"]) is (actor_of(call) == "Sozialversicherung")


# --- priorities, satisfaction ---------------------------------------------------

def test_priority_transition_met_to_unmet_is_preserved(harness):
    def status(number, pid):
        return "unmet" if 4 <= number <= 6 else "met"

    h = harness(overrides={"AssessOutput": assess_with(status)})
    result = h.run()
    transitions = [(a["previous_status"], a["status"]) for a in result["priority_history"]]
    assert ("met", "unmet") in transitions and ("unmet", "met") in transitions
    assert result["metrics"]["completed_rounds"] == 4


def test_failed_round_resets_the_streak(harness):
    def status(number, pid):
        return "partly_met" if 4 <= number <= 6 else "met"

    h = harness(overrides={"AssessOutput": assess_with(status)})
    result = h.run()
    assert counts(h)["CandidateOutput"] == 4 and result["stop_reason"] == "satisfaction_threshold"


def test_reviewer_objection_does_not_overwrite_actor_status(harness):
    def review(prompt, number):
        data = fake.review(prompt)
        if "Rolle=truthseeker, Stufe=assessment" in prompt:
            data["findings"][0]["assessment"] = "no_support"
        return data

    result = harness(overrides={"ReviewOutput": review}).run()
    assert all(s["disputed"] for s in result["metrics"]["satisfaction"])
    assert all(s["percent"] == 100.0 for s in result["metrics"]["satisfaction"])
    assert any(a["reviewer_objections"] for a in result["priority_history"])
    assert all(a["status"] == "met" for a in result["priority_history"])


def test_incomplete_review_coverage_prevents_a_successful_round(harness):
    def review(prompt, number):
        data = fake.review(prompt)
        if "Rolle=legaleye, Stufe=assessment" in prompt:
            data["coverage"], data["coverage_note"] = "incomplete", "Nicht alles geprüft."
            data["unreviewed_targets"] = [data["findings"][0]["target_id"]]
        return data

    result = harness(overrides={"ReviewOutput": review}, DELIBERATION_TIMEOUT_SECONDS=100, delay=1).run()
    assert result["metrics"]["completed_rounds"] == 0 and result["stop_reason"] == "deliberation_deadline"
    assert result["metrics"]["success_streak"] == 0


def test_incomplete_candidate_review_is_frozen_but_disclosed(harness):
    def review(prompt, number):
        data = fake.review(prompt)
        if "Rolle=legaleye, Stufe=candidate" in prompt:
            data["coverage"], data["coverage_note"] = "incomplete", "Eine Vorschrift fehlte."
            data["unreviewed_targets"] = [data["findings"][0]["target_id"]]
        return data

    result = harness(overrides={"ReviewOutput": review}, DELIBERATION_TIMEOUT_SECONDS=120, delay=1).run()
    assert result["metrics"]["completed_rounds"] == 0
    assert result["final_proposal"]["status"] == "frozen" and result["final_proposal"]["review_complete"] is False
    assert any(w["code"] == "pruefung_unvollstaendig" for w in result["warnings"])


def test_satisfaction_separate_from_acceptance(harness):
    def reject(prompt, number):
        data = fake.assess(prompt)
        data["decision"], data["decision_reason"] = "reject", "Die Finanzierung ist ungeklärt."
        return data

    result = harness(overrides={"AssessOutput": reject}).run()
    assert all(s["threshold_reached"] for s in result["metrics"]["satisfaction"])
    assert {d["kind"] for d in result["decisions"]} == {"reject", "accept"}
    assert result["report"]["Konsens"]["akzeptiert_von"] == ["Gesundheitsministerium", "Sozialversicherung", "Ärztekammer"]


def test_baseline_correction_creates_version_and_resets_streak(harness):
    calls = {"n": 0}

    def negotiate(prompt, number):
        data = fake.negotiation(prompt)
        if number == 4:          # second round, first actor
            pid = re.search(r"PR-[a-z]+-1", prompt).group(0)
            calls["n"] += 1
            data["corrections"] = [{"priority_id": pid, "acceptance_condition": "Der Vorschlag verkürzt Wartezeiten messbar.",
                                    "justification": "Die Bedingung war nicht prüfbar.", "quellen": []}]
        return data

    h = harness(overrides={"NegotiationOutput": negotiate})
    result = h.run()
    assert calls["n"] == 1
    assert any(e["type"] == "priority_baseline_corrected" for e in h.events())
    assert result["metrics"]["completed_rounds"] == 3
    assert max(p["baseline_version"] for a in result["actors"] for p in a["priorities"]) == 2


def test_no_priorities_gives_null_satisfaction(harness):
    def empty(prompt, number):
        data = fake.initial(prompt)
        data["priorities"] = []
        return data

    result = harness(overrides={"InitialOutput": empty}, DELIBERATION_TIMEOUT_SECONDS=90, delay=1).run()
    check_result(result)
    assert all(s["percent"] is None for s in result["metrics"]["satisfaction"])
    assert result["metrics"]["completed_rounds"] == 0


# --- legal scope ----------------------------------------------------------------

def legal_review(assessment):
    def handler(prompt, number):
        data = fake.review(prompt)
        if "Rolle=legaleye, Stufe=candidate" in prompt:
            data["findings"][0]["assessment"] = assessment
        return data
    return handler


def test_law_amendment_in_candidate_is_rejected_by_controller(harness):
    def candidate(prompt, number):
        data = fake.candidate(prompt)
        if number == 1:
            data["clauses"][0]["text"] = "Das ASVG soll novelliert werden, um die Versorgung zu sichern."
        return data

    h = harness(overrides={"CandidateOutput": candidate})
    result = h.run()
    assert result["usage"]["repairs"] == 1
    assert not any("novelliert" in c["text"] for p in result["proposal_history"] for c in p["clauses"])


def test_legaleye_law_change_finding_triggers_one_amendment(harness):
    def review(prompt, number):
        data = fake.review(prompt)
        if "Rolle=legaleye, Stufe=candidate" in prompt and '"PROP-1-v1"' in prompt:
            data["findings"][0]["assessment"] = "requires_legal_amendment"
        return data

    h = harness(overrides={"ReviewOutput": review})
    result = h.run()
    versions = {p["version"]: p for p in result["proposal_history"]}
    assert versions[1]["legal_admissibility"] == "inadmissible" and versions[1]["status"] == "superseded"
    assert result["final_proposal"]["legal_admissibility"] != "inadmissible"
    assert any("KL-v1-1" in c["input"] for c in h.prompts("CandidateOutput"))


def test_unresolved_legal_status_is_not_presented_as_approved(harness):
    result = harness(overrides={"ReviewOutput": legal_review("unresolved_issue")}).run()
    assert result["final_proposal"]["legal_admissibility"] == "unresolved"
    assert any(w["code"] == "rechtlich_ungeklaert" for w in result["warnings"])
    assert result["report"]["Konsens"]["rechtliche_zulaessigkeit"] == "unresolved"


def test_defensible_interpretation_stays_discussable(harness):
    result = harness(overrides={"ReviewOutput": legal_review("defensible_with_conditions")}).run()
    assert result["final_proposal"]["legal_admissibility"] == "conditional"
    assert result["status"] == "completed"


def test_no_lawful_solution_reported_without_reform(harness):
    h = harness(overrides={"ReviewOutput": legal_review("requires_legal_amendment")})
    result = h.run()
    check_result(result)
    assert result["final_proposal"]["legal_admissibility"] == "inadmissible"
    assert any(w["code"] == "rechtlich_ungeklaert" for w in result["warnings"])


def test_withdrawn_legal_finding_no_longer_blocks(harness):
    def challenge(prompt, number):
        return {"challenges": [{"finding_id": i, "reason": "Die Befugnis besteht bereits."}
                               for i in fake.ids_after(prompt, "Befund-IDs")]}

    def reply(prompt, number):
        return {"replies": [{"challenge_id": c, "decision": "withdraw", "reason": "Einwand trifft zu."}
                            for c in fake.ids_after(prompt, "Widerspruchs-IDs")]}

    h = harness(overrides={"ReviewOutput": legal_review("apparent_legal_conflict"), "ChallengesOutput": challenge,
                           "RepliesOutput": reply})
    result = h.run()
    assert result["proposal_history"][0]["clauses"][0]["legal_status"] == "no_conflict_identified"
    assert result["final_proposal"]["legal_admissibility"] == "admissible_within_reviewed_sources"
    assert counts(h)["CandidateOutput"] == 2      # no amendment in either round


# --- deadlines, failures, resume -------------------------------------------------

def test_deadline_before_any_complete_proposal(harness):
    h = harness(delay=10, DELIBERATION_TIMEOUT_SECONDS=100)
    result = h.run()
    check_result(result)
    assert result["stop_reason"] == "deliberation_deadline" and result["final_proposal"] is None
    assert result["metrics"]["completed_rounds"] == 0
    assert any("keine Einigung" in w["message"] for w in result["warnings"])


def test_deadline_routes_from_a_challenge_stage(harness):
    def challenge(prompt, number):
        h.clock.advance(10000)
        return {"challenges": [{"finding_id": i, "reason": "Einwand."} for i in fake.ids_after(prompt, "Befund-IDs")]}

    h = harness(overrides={"ReviewOutput": material_review, "ChallengesOutput": challenge})
    result = h.run()
    assert result["stop_reason"] == "deliberation_deadline" and counts(h).get("RepliesOutput", 0) == 0
    assert "NegotiationOutput" not in counts(h)


def test_late_response_cannot_change_outcome(harness):
    def negotiate(prompt, number):
        h.clock.advance(10000)
        return fake.negotiation(prompt)

    h = harness(overrides={"NegotiationOutput": negotiate})
    result = h.run()
    assert not result["negotiation"] and "CandidateOutput" not in counts(h)


def stop_at_negotiation(h, call):
    def negotiate(prompt, number):
        if number == call:
            (h.settings.state_dir / "stop-debatte-1").write_text("stop", encoding="utf-8")
        return fake.negotiation(prompt)
    return negotiate


def test_operator_stop_freezes_the_last_complete_proposal_and_finalises(harness):
    h = harness()
    h.adapter.overrides = {"AssessOutput": assess_with(lambda n, p: "partly_met"), "NegotiationOutput": stop_at_negotiation(h, 6)}
    result = h.run()
    check_result(result)
    assert result["status"] == "completed" and result["stop_reason"] == "operator_stop"
    assert result["final_proposal"]["version"] == 1 and result["metrics"]["completed_rounds"] == 1
    assert counts(h)["CandidateOutput"] == 1
    assert all(a["final_standpoint"] for a in result["actors"]) and result["report"]["Konflikte"]


def test_operator_stop_before_any_proposal_invents_no_agreement(harness):
    h = harness()
    h.adapter.overrides = {"NegotiationOutput": stop_at_negotiation(h, 1)}
    result = h.run()
    check_result(result)
    assert result["stop_reason"] == "operator_stop" and result["final_proposal"] is None
    assert any("keine Einigung" in w["message"] for w in result["warnings"])
    assert "CandidateOutput" not in counts(h)


def test_stop_signal_is_removed_after_the_run(harness):
    h = harness()
    h.adapter.overrides = {"NegotiationOutput": stop_at_negotiation(h, 1)}
    h.run()
    assert not (h.settings.state_dir / "stop-debatte-1").exists()


def test_finalisation_timeout_gives_partial_result(harness):
    def final(prompt, number):
        h.clock.advance(1000)
        return fake.final(prompt)

    h = harness(overrides={"FinalOutput": final})
    result = h.run()
    check_result(result)
    assert result["status"] == "incomplete" and result["report"]["fehlende_abschnitte"]
    assert result["final_proposal"] is not None and result["stop_reason"] == "satisfaction_threshold"
    assert "ReportOutput" not in counts(h)


def test_last_model_failure_preserves_results(harness):
    h = harness(overrides={"EvolutionOutput": lambda p, n: LlmTransientError("503")})
    result = h.run()
    assert result["status"] == "incomplete" and result["report"]["Konflikte"]
    assert result["debate_evolution"]["narrative"] is None


def test_authentication_failure_is_reported_without_fake_fallback(harness):
    h = harness(overrides={"InitialOutput": lambda p, n: LlmAuthError("Schlüssel abgelehnt.")})
    result = h.run()
    check_result(result)
    assert result["status"] == "failed" and result["stop_reason"] == "execution_error"
    assert result["errors"][0]["code"] == "llm_auth" and "ReviewOutput" not in counts(h)


def test_missing_deployment_is_actionable(harness):
    result = harness(overrides={"InitialOutput": lambda p, n: LlmDeploymentError("Deployment fehlt.")}).run()
    assert result["status"] == "failed" and result["errors"][0]["code"] == "llm_deployment"


def test_failed_source_access_is_a_failure_not_empty_evidence(harness):
    from healthsim.errors import SourceError
    h = harness()

    async def broken():
        raise SourceError("Neo4j nicht erreichbar.")

    h.sources.list_actors = broken
    result = h.run()
    assert result["status"] == "failed" and "Neo4j" in result["errors"][0]["message"]


def test_durable_resume_keeps_deadlines_and_is_idempotent(tmp_path):
    class Crash(BaseException):
        pass

    def crash(prompt, number):
        if number == 1:
            raise Crash
        return None

    h = Harness(tmp_path, overrides={"NegotiationOutput": crash})

    async def first():
        await h.runtime.run(make_request(), "d")

    with pytest.raises(BaseException):
        asyncio.run(first())
    before = h.events("d")
    deadline = next(e for e in before if e["type"] == "rulebook_released")["at"] + h.settings.deliberation_timeout
    h.clock.advance(50)
    h.runtime.events.close()
    runtime = Runtime(h.settings, h.adapter, h.sources, clock=h.clock, use_lock=False)
    h.runtime = runtime
    h.adapter.overrides = {}
    result = asyncio.run(runtime.resume("d"))
    check_result(result)
    assert result["status"] == "completed"
    after = h.events("d")
    ids = [e["event_id"] for e in after]
    assert len(ids) == len(set(ids)) and {e["event_id"] for e in before} <= set(ids)
    assert counts(h)["InitialOutput"] == 3      # finished work is not repeated
    assert result["timing"]["deliberation_deadline_at"].startswith(__import__("healthsim.events", fromlist=["iso"]).iso(deadline)[:19])
    assert asyncio.run(runtime.resume("d")) == result
    runtime.events.close()


def test_resume_after_expiry_goes_straight_to_finalisation(tmp_path):
    class Crash(BaseException):
        pass

    def crash(prompt, number):
        if number == 1:
            raise Crash
        return None

    h = Harness(tmp_path, overrides={"NegotiationOutput": crash})
    with pytest.raises(BaseException):
        asyncio.run(h.runtime.run(make_request(), "d"))
    h.clock.advance(5000)
    h.runtime.events.close()
    runtime = Runtime(h.settings, h.adapter, h.sources, clock=h.clock, use_lock=False)
    h.runtime = runtime
    h.adapter.overrides = {}
    result = asyncio.run(runtime.resume("d"))
    check_result(result)
    assert result["stop_reason"] == "deliberation_deadline" and "CandidateOutput" not in counts(h)
    runtime.events.close()


# --- output contract ----------------------------------------------------------------

def test_all_references_resolve(harness):
    h = harness()
    result = check_result(h.run())
    assert collect_reference_problems(result) == []


def test_broken_reference_is_reported(harness):
    result = check_result(harness().run())
    result.arguments[0].claims[0].quellen.append("P-erfunden")
    assert collect_reference_problems(result)


def test_status_tables_come_from_stored_records(harness):
    result = harness().run()
    konsens = result["report"]["Konsens"]
    assert konsens["akzeptiert_von"] == ["Gesundheitsministerium", "Sozialversicherung", "Ärztekammer"]
    assert len(konsens["schwelle_erreicht_von"]) == 3
    assert result["actors"][0]["latest_assessments"][0]["status"] == "met"


def test_fixed_report_sections(harness):
    report = harness().run()["report"]
    assert list(report)[:8] == ["Frage", "Agenten", "Verlauf", "Konflikte", "Kompromisse", "Konsens", "Offene_Punkte",
                                "Auswirkungen"]


def test_usage_totals_and_missing_limits(harness):
    usage = harness().run()["usage"]
    assert usage["requests"] > 40 and usage["input_tokens"] == usage["requests"] * 100
    assert usage["missing_measurements"] == ["LLM_RATE_LIMIT_RPM", "LLM_RATE_LIMIT_TPM"]


def test_model_identity_is_disclosed_as_unverified(harness):
    model = harness().run()["model"]
    assert model["capabilities"]["model_identity_verified"] is False and model["deployment"] == "test-deployment"


def test_malformed_input_still_returns_json():
    result = asyncio.run(run_text("{kaputt", llm="fake"))
    check_result(result)
    assert result["status"] == "invalid_input"


def test_missing_azure_credentials_fail_without_fake_fallback(tmp_path):
    env = {"LLM_API_MODE": "responses_v1", "AZURE_OPENAI_API_KEY": "", "AZURE_OPENAI_DEPLOYMENT_NAME": "",
           "LLM_MODEL": "gpt-5.4-mini", "STATE_DIR": str(tmp_path)}
    result = asyncio.run(run_text(make_request().model_dump_json(), env=env))
    assert result["status"] == "failed" and "fehlt" in result["errors"][0]["message"]


def test_cli_stdout_is_json_only(tmp_path):
    env = {**__import__("os").environ, "LLM_API_MODE": "responses_v1", "STATE_DIR": str(tmp_path), "PYTHONIOENCODING": "utf-8",
           "AZURE_OPENAI_DEPLOYMENT_NAME": "", "AZURE_OPENAI_API_KEY": ""}
    run = subprocess.run([sys.executable, "-m", "healthsim", "run", "--llm", "fake", "--sources", "fixture", "--fixture",
                          str(ROOT / "examples" / "fixture_sources.json")], input=make_request().model_dump_json().encode(),
                         capture_output=True, cwd=ROOT, env=env, timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.decode("utf-8"))["status"] == "completed"


def test_cli_bad_input_gives_exit_code_3(tmp_path):
    run = subprocess.run([sys.executable, "-m", "healthsim", "run", "--llm", "fake"], input=b"{kaputt",
                         capture_output=True, cwd=ROOT, timeout=60)
    assert run.returncode == 3 and json.loads(run.stdout.decode("utf-8"))["status"] == "invalid_input"


def test_nan_is_rejected_in_output():
    from healthsim.app import to_stdout
    with pytest.raises(ValueError):
        to_stdout({"x": float("nan")})


def test_diagram_matches_required_workflow(tmp_path):
    text = export_diagram(tmp_path / "w.mmd").read_text(encoding="utf-8")
    order = ["validate_request", "release_question", "prepare_actor", "initial_review", "initial_challenge", "initial_reply",
             "revise_actor", "second_review", "second_challenge", "second_reply", "publish_package", "negotiate_actor",
             "assemble_candidate", "candidate_review", "candidate_challenge", "amend_candidate", "assess_actor",
             "assessment_review", "close_round", "freeze", "final_actor", "final_review", "meta_report", "meta_evolution",
             "build_output"]
    for node in order:
        assert node in text


def test_exported_schemas_match_models(tmp_path):
    from healthsim.app import export_schemas
    directory = export_schemas(tmp_path)
    schema = json.loads((directory / "result.schema.json").read_text(encoding="utf-8"))
    for field in ("schema_version", "debate_id", "status", "stop_reason", "request", "task", "model", "timing", "usage",
                  "metrics", "final_proposal", "actors", "arguments", "reviews", "challenges", "proposal_history",
                  "priority_history", "decisions", "timeline", "sources", "report", "debate_evolution",
                  "clarification_questions", "warnings", "errors"):
        assert field in schema["properties"]


def test_state_schema_has_no_client_or_credential_fields():
    names = set(DebateState.__annotations__)
    assert not {n for n in names if any(word in n for word in ("client", "api_key", "password", "driver", "credential"))}


def test_secrets_are_not_in_checkpoints_or_event_files(harness):
    h = harness()
    h.run()
    h.runtime.events.close()
    state_dir = h.settings.state_dir
    files = list(state_dir.glob("*.sqlite*"))
    assert any(f.name.startswith("checkpoints") for f in files)
    for file in files:
        data = file.read_bytes()
        assert b"geheimer-testschluessel" not in data and b"neo4j-geheim" not in data
    h.runtime.events = type(h.runtime.events)(state_dir / "events.sqlite")


def test_process_lock_enforces_single_process(settings):
    from healthsim.errors import ConfigurationError
    from healthsim.llm import ProcessLock
    first = ProcessLock(settings.state_dir).acquire()
    with pytest.raises(ConfigurationError, match="anderer Simulatorprozess"):
        ProcessLock(settings.state_dir).acquire()
    first.release()
    ProcessLock(settings.state_dir).acquire().release()
