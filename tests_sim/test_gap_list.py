"""Gap list and word-for-word clause preservation."""

import re

from conftest import check_result
from test_workflow import assess_with, counts

from healthsim import llm_fake as fake


def partly_first_round(number, pid):
    return "partly_met" if number <= 3 else "met"


def test_gap_list_reaches_moderator_with_condition_status_and_reason(harness):
    h = harness(overrides={"AssessOutput": assess_with(partly_first_round)})
    h.run()
    first, second = h.prompts("CandidateOutput")[:2]
    assert "noch keine Bewertung" in first["input"]
    assert "Stand der Prioritäten (offene zuerst):" in second["input"]
    assert "partly_met. Bedingung: Der Vorschlag verkürzt Wartezeiten" in second["input"]
    assert "Begründung: Die Klausel erfüllt dies." in second["input"]


def test_all_actors_get_the_same_gap_list(harness):
    h = harness(overrides={"AssessOutput": assess_with(partly_first_round)})
    h.run()

    def gaps(prompt):
        return re.search(r"Stand der Prioritäten \(offene zuerst\):\n(.*?)\n\n", prompt, re.S).group(1)

    second_round = h.prompts("NegotiationOutput")[3:6]
    assert len({gaps(c["input"]) for c in second_round}) == 1
    assert "aerztekammer" in gaps(second_round[0]["input"])


def test_open_priorities_come_first(harness):
    def status(number, pid):
        return "unmet" if number == 3 and pid.endswith("-2") else "met"

    h = harness(overrides={"AssessOutput": assess_with(status)})
    h.run()
    second = h.prompts("CandidateOutput")[1]["input"]
    lines = re.findall(r"^- (\S+) \([^)]*\): (\w+)\.", second, re.M)
    assert lines[0][1] == "unmet" and {s for _, s in lines[1:]} == {"met"}


def test_gap_list_is_not_shown_during_assessment(harness):
    h = harness(overrides={"AssessOutput": assess_with(partly_first_round)})
    h.run()
    assert not any("Stand der Prioritäten" in c["input"] for c in h.prompts("AssessOutput"))


def test_no_gap_list_before_publication(harness):
    h = harness()
    h.run()
    for schema in ("InitialOutput", "RevisionOutput"):
        assert not any("Stand der Prioritäten" in c["input"] for c in h.prompts(schema))


def keep_second_time(prompt, number):
    data = fake.candidate(prompt)
    if number == 1:
        data["clauses"].append({"unchanged_from": None, "text": "Die Partner veröffentlichen gemeinsame Kennzahlen.",
                                "provenance": data["clauses"][0]["provenance"], "premises": []})
        return data
    data["clauses"] = [{"unchanged_from": "KL-v1-2", "text": "", "provenance": [], "premises": []},
                       {"unchanged_from": None, "text": "Die Partner stimmen sich neu ab.",
                        "provenance": ["KL-v1-1"], "premises": []}]
    return data


def legal_for_all(prompt, number):
    data = fake.review(prompt)
    if "Rolle=legaleye" in prompt:
        data["findings"] = [{**data["findings"][0], "target_id": t, "reference": t}
                            for t in fake.ids_after(prompt, "Ziel-IDs")]
    return data


def test_unchanged_clause_is_copied_word_for_word(harness):
    h = harness(overrides={"CandidateOutput": keep_second_time, "ReviewOutput": legal_for_all})
    result = h.run()
    check_result(result)
    assert result["usage"]["repairs"] == 0
    versions = {p["version"]: p for p in result["proposal_history"]}
    kept = versions[2]["clauses"][0]
    assert kept["text"] == "Die Partner veröffentlichen gemeinsame Kennzahlen."
    assert kept["clause_id"] == "KL-v2-1" and kept["provenance"] == ["KL-v1-2"]


def test_kept_clause_ignores_any_text_the_model_supplies(harness):
    def candidate(prompt, number):
        data = keep_second_time(prompt, number)
        if number > 1:
            data["clauses"][0]["text"] = "Das ASVG soll novelliert werden."
        return data

    result = harness(overrides={"CandidateOutput": candidate, "ReviewOutput": legal_for_all}).run()
    assert result["usage"]["repairs"] == 0
    assert "novelliert" not in str(result["proposal_history"])


def test_kept_clauses_are_reviewed_again(harness):
    h = harness(overrides={"CandidateOutput": keep_second_time, "ReviewOutput": legal_for_all})
    h.run()
    assert any("KL-v2-1" in c["input"] and "Stufe=candidate" in c["input"] for c in h.prompts("ReviewOutput"))


def test_unknown_or_duplicate_kept_clause_is_rejected(harness):
    def candidate(prompt, number):
        data = fake.candidate(prompt)
        if number == 1:
            return data
        data["clauses"] = [{"unchanged_from": "KL-v9-9", "text": "", "provenance": [], "premises": []}]
        return data

    h = harness(overrides={"CandidateOutput": candidate}, DELIBERATION_TIMEOUT_SECONDS=150, delay=1)
    h.run()
    failures = [e["payload"]["reason"] for e in h.events() if e["type"] == "validation_failure"]
    assert any("gehört nicht zur aktuellen Fassung" in f and "KL-v1-1" in f for f in failures)
    assert counts(h)["CandidateOutput"] >= 3


def test_empty_new_clause_is_rejected(harness):
    def candidate(prompt, number):
        data = fake.candidate(prompt)
        if number == 1:
            data["clauses"][0]["text"] = " "
        return data

    h = harness(overrides={"CandidateOutput": candidate})
    h.run()
    assert any("Text fehlt" in e["payload"]["reason"] for e in h.events() if e["type"] == "validation_failure")
