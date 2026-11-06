"""Explicitly selected offline adapter. It is never used as a fallback for Azure.

It reads the IDs a prompt offers, so its answers go through the same validation as
real ones. Tests can replace answers per schema, inject faults and record prompts.
"""

import json
import re

from .llm import RawResponse


def ids_after(prompt, label):
    match = re.search(rf"^{re.escape(label)}: (.*)$", prompt, re.M)
    if not match or match.group(1).strip() in ("", "keine"):
        return []
    return [x.strip() for x in match.group(1).split(",") if x.strip()]


def first_sources(prompt, count=1):
    return ids_after(prompt, "Quellen-IDs")[:count]


def claim(prompt, text):
    sources = first_sources(prompt)
    return {"kind": "proposal" if not sources else "reported", "behauptung": text,
            "begruendung": "Die Quelle stützt diese Einschätzung.",
            "beispiel": "Beispiel nicht verfügbar.", "beispiel_art": "unavailable",
            "quellen": sources, "berechnung": None}


def initial(prompt):
    sources = first_sources(prompt)
    return {
        "profile": {"entries": [{"label": "reported", "aussage": "Die Institution berichtet über ihre "
                                 "Aufgaben in der ambulanten Versorgung.", "quellen": sources}],
                    "standpunkt": "Die ambulante Versorgung soll innerhalb des geltenden Rechts gestärkt werden.",
                    "standpunkt_label": "inferred"},
        "priorities": [
            {"priority_id": None, "objective": "Besseren Zugang zur ambulanten Versorgung sichern.",
             "acceptance_condition": "Der Vorschlag verkürzt Wartezeiten mit bestehenden Instrumenten.",
             "quellen": sources, "evidence_basis": "reported", "condition_is_simulated": True,
             "law_scope_conflict": False},
            {"priority_id": None, "objective": "Personelle Belastung im Versorgungsalltag begrenzen.",
             "acceptance_condition": "Der Vorschlag enthält eine Maßnahme zur Entlastung des Personals.",
             "quellen": sources, "evidence_basis": "reported", "condition_is_simulated": True,
             "law_scope_conflict": False}],
        "argument": {"claims": [claim(prompt, "Die ambulante Versorgung braucht bessere Abstimmung.")]},
    }


def review(prompt):
    targets = ids_after(prompt, "Ziel-IDs") or ["unbekannt"]
    assessment = "supported"
    if "Rolle=legaleye" in prompt:
        assessment = "no_conflict_identified"
    elif "Rolle=ethicmind" in prompt:
        assessment = "no_concern_identified"
    return {"findings": [{"target_id": targets[0], "assessment": assessment,
                          "reason": "Innerhalb der geprüften Quellen ist kein Mangel erkennbar.",
                          "reference": targets[0], "correction": None}],
            "coverage": "complete", "unreviewed_targets": [], "coverage_note": None}


def negotiation(prompt):
    return {"items": [{"kind": "offer", "text": "Wir bieten eine abgestimmte Versorgungsplanung an.",
                       "priority_ids": [], "quellen": []}], "corrections": []}


def candidate(prompt):
    premises = ids_after(prompt, "Erlaubte Prämissen-IDs")[:1]
    items = re.findall(r"NEG-[\w-]+", prompt)[:1]
    checks = [{"constraint_id": c, "status": "unclear", "reason": "Keine Daten zur Prüfung."}
              for c in ids_after(prompt, "Beschränkungs-IDs")]
    return {"clauses": [{"unchanged_from": None, "text": "Die Partner stimmen die Versorgungsplanung regional mit bestehenden "
                                 "Instrumenten ab und entlasten das Personal organisatorisch.",
                         "provenance": items, "premises": premises}],
            "meta_update": "Ein erster gemeinsamer Vorschlag liegt vor.", "constraint_checks": checks}


def assess(prompt):
    refs = ids_after(prompt, "Erlaubte Verweise")[:1]
    return {"assessments": [{"priority_id": p, "status": "met", "reason": "Die Klausel erfüllt dies.",
                             "references": refs} for p in ids_after(prompt, "Prioritäts-IDs")],
            "decision": "accept", "decision_reason": "Der Vorschlag ist tragfähig.", "conditions": []}


def final(prompt):
    data = assess(prompt)
    data["summary"] = "Die Institution stellt ihren Standpunkt zum eingefrorenen Vorschlag abschließend dar."
    return data


def report(prompt):
    refs = ids_after(prompt, "Erlaubte Verweise")[:1]
    return {"einordnung": "Die Frage wurde innerhalb des geltenden Rechts behandelt.",
            "verlauf_kurz": "Die Akteure näherten sich über mehrere Runden an.",
            "konflikte": [{"thema": "Finanzierung", "beschreibung": "Die Mittelverteilung blieb strittig.",
                           "beteiligte": [], "refs": refs}],
            "kompromisse": [{"art": "compromise", "beschreibung": "Regionale Abstimmung mit bestehenden Mitteln.",
                             "refs": refs}],
            "konsens": "Die Akteure stimmen dem Vorschlag im simulierten Rahmen zu.",
            "offene_punkte": [{"art": "data", "punkt": "Belastbare Kostendaten fehlen.", "refs": refs}],
            "auswirkungen": [{"basis": "assumption", "beschreibung": "Bessere Abstimmung könnte Wartezeiten "
                              "senken.", "refs": refs}]}


def evolution(prompt):
    refs = ids_after(prompt, "Erlaubte Verweise")[:2]
    return {"narrative": "Die Akteure überarbeiteten ihre Positionen nach den Prüfungen "
                         + " ".join(refs) + " und nahmen den Vorschlag an.", "refs": refs}


DEFAULTS = {
    "InitialOutput": initial, "ReviewOutput": review, "ChallengesOutput": lambda p: {"challenges": []},
    "RepliesOutput": lambda p: {"replies": [{"challenge_id": c, "decision": "uphold",
                                             "reason": "Der Befund bleibt bestehen."}
                                            for c in ids_after(p, "Widerspruchs-IDs")]},
    "RevisionOutput": lambda p: {"argument": {"claims": [claim(p, "Die überarbeitete Position stützt "
                                                                  "regionale Abstimmung.")]},
                                 "change_explanation": "Die Rückmeldungen wurden berücksichtigt.",
                                 "profile": None, "priorities": None},
    "NegotiationOutput": negotiation, "CandidateOutput": candidate, "AssessOutput": assess,
    "FinalOutput": final, "ReportOutput": report, "EvolutionOutput": evolution,
    "ConnectionProbe": lambda p: {"antwort": "Verbindung simuliert."},
    "MetaAnswer": lambda p: {"antwort": "Laut Protokoll blieb die Finanzierung strittig.", "refs": re.findall(r"KL-[\w-]+", p)[:1],
                             "keine_grundlage": False},
}


class FakeAdapter:
    """overrides: {schema_name: fn(prompt, call_number) -> dict | RawResponse | Exception}."""

    def __init__(self, clock=None, overrides=None, delay=0.0):
        self.clock, self.overrides, self.delay = clock, overrides or {}, delay
        self.calls = []
        self.counts = {}

    async def close(self):
        pass

    async def create(self, *, model, instructions, input, schema_name, schema, max_output_tokens,
                     reasoning_effort, timeout):
        schema_name = "ReviewOutput" if schema_name.endswith("ReviewOutput") else schema_name
        self.counts[schema_name] = self.counts.get(schema_name, 0) + 1
        number = self.counts[schema_name]
        self.calls.append({"schema": schema_name, "instructions": instructions, "input": input,
                           "model": model, "max_output_tokens": max_output_tokens,
                           "reasoning_effort": reasoning_effort, "timeout": timeout})
        if self.delay and self.clock:
            await self.clock.sleep(self.delay)
        handler = self.overrides.get(schema_name)
        result = handler(input, number) if handler else None
        if result is None:
            result = DEFAULTS[schema_name](input)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, RawResponse):
            return result
        return RawResponse(status="completed", output_text=json.dumps(result, ensure_ascii=False),
                           model="fake-adapter", input_tokens=100, output_tokens=50, reasoning_tokens=10)
