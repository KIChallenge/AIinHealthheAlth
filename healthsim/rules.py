"""Deterministic rules: word budgets, citation and content checks, arithmetic, satisfaction."""

import math
import re

from .llm_models import (ALLOWED_ASSESSMENTS, Argument, Claim, EvolutionOutput, FinalOutput, Profile,
                         ReportOutput, ReviewOutput)

WORD_LIMITS = {"ordinary": 300, "profile": 300, "priorities": 600, "assessment": 300,
               "final_summary": 600, "conclusion": 2000, "evolution": 800}


def count_words(text):
    """Whitespace-separated units of the canonical rendering: everything the model wrote,
    including citations. Program labels, enum values, JSON punctuation and ids are excluded."""
    return len(text.split())


def render_claim(claim: Claim):
    parts = [claim.behauptung, claim.begruendung, claim.beispiel, " ".join(claim.quellen)]
    if claim.berechnung:
        calc = claim.berechnung
        inputs = " ".join(f"{i.value:g} {i.unit} {i.quelle}" for i in calc.inputs)
        parts.append(f"{calc.operation} {inputs} = {calc.result:g} {calc.unit}")
    return " ".join(parts)


def render_argument(argument: Argument):
    return " ".join(render_claim(c) for c in argument.claims)


def render_profile(profile: Profile):
    entries = " ".join(f"{e.aussage} {' '.join(e.quellen)}" for e in profile.entries)
    return f"{entries} {profile.standpunkt}"


def render_priorities(priorities):
    return " ".join(f"{p.objective} {p.acceptance_condition} {' '.join(p.quellen)}" for p in priorities)


def render_review(review: ReviewOutput):
    parts = [f"{f.reason} {f.reference} {f.correction or ''}" for f in review.findings]
    return " ".join(parts + [review.coverage_note or ""])


def render_assessments(items):
    return " ".join(f"{a.reason} {' '.join(a.references)}" for a in items)


def render_report(report: ReportOutput):
    parts = [report.einordnung, report.verlauf_kurz, report.konsens]
    for c in report.konflikte:
        parts += [c.thema, c.beschreibung, " ".join(c.refs)]
    for c in report.kompromisse:
        parts += [c.beschreibung, " ".join(c.refs)]
    for p in report.offene_punkte:
        parts += [p.punkt, " ".join(p.refs)]
    for i in report.auswirkungen:
        parts += [i.beschreibung, " ".join(i.refs)]
    return " ".join(parts)


def word_problem(text, limit_name, label):
    limit = WORD_LIMITS[limit_name]
    words = count_words(text)
    return [f"{label}: {words} Wörter überschreiten das Limit von {limit}."] if words > limit else []


def word_problem_limit(text, limit, label):
    words = count_words(text)
    return [f"{label}: {words} Wörter überschreiten das Limit von {limit}."] if words > limit else []


# --------------------------------------------------------------------------
# Scope and content checks

_LAW_TERMS = (r"Gesetzes(?:änderung|novelle|reform|vorschlag|entwurf|initiative|vorlage)\w*|Gesetzentwurf"
              r"|Novellierung\w*|Rechtsänderung\w*|Verfassungsänderung\w*|Verordnungsänderung\w*"
              r"|neues?\s+Gesetz\w*|(?:ASVG|ÄrzteG|Ärztegesetz\w*|B-ZG|G-ZG|KAKuG)[- ]?Novelle")
_LAW_VERB = (r"(?:Gesetz\w*|ASVG|ÄrzteG|Ärztegesetz\w*|Verordnung\w*|Bundesverfassung\w*|Rechtslage|"
             r"Rechtsrahmen\w*)\s+(?:\w+\s+){0,4}?(?:ändern|ändert|geändert|abändern|novellier\w*|"
             r"reformier\w*|anpassen|angepasst|aufheben|aufgehoben)")
_ANY_LAW = re.compile(rf"(?:{_LAW_TERMS})|(?:{_LAW_VERB})", re.IGNORECASE)
# A sentence that rules out a law change is not a proposal to make one. Plain "nicht" is
# deliberately not enough: "... ändern, weil die Regelung nicht reicht" must stay flagged.
_DENIAL = re.compile(r"\b(?:ohne|kein\w*|Verzicht\w*|vermeid\w*|ausgeschlossen|statt|nicht\s+(?:[\w-]+\s+){0,3}?"
                     r"(?:verlangt|vorausgesetzt|erforderlich|nötig|notwendig|vorgesehen|geplant|Teil))\b",
                     re.IGNORECASE)
_SENTENCE = re.compile(r"[.;!?](?:\s+|$)")


def mentions_law_change(text):
    """True if a sentence asks for or proposes changing law.

    A safeguard only: legality remains LegalEye's reasoned judgement.
    """
    return any(_ANY_LAW.search(s) and not _DENIAL.search(s) for s in _SENTENCE.split(text))


_NUMBER = re.compile(r"\d")


def validate_claims(argument: Argument, allowed_sources, assumption_ids, label="Argument"):
    """Citation resolution, source requirements and arithmetic. German problem list."""
    problems = word_problem(render_argument(argument), "ordinary", label)
    if not argument.claims:
        problems.append(f"{label}: mindestens eine Behauptung ist erforderlich.")
    for i, claim in enumerate(argument.claims, 1):
        problems += check_citations(claim.quellen, allowed_sources, assumption_ids, f"{label}, Behauptung {i}")
        if claim.kind in ("fact", "reported") and not claim.quellen:
            problems.append(f"{label}, Behauptung {i}: Fakten und Berichte brauchen eine Quelle.")
        if claim.kind in ("fact", "reported", "prediction") and _NUMBER.search(claim.behauptung) \
                and not claim.quellen:
            problems.append(f"{label}, Behauptung {i}: Zahlenangaben brauchen eine Quelle.")
        if claim.beispiel_art == "unavailable" and "nicht verfügbar" not in claim.beispiel.lower():
            problems.append(f"{label}, Behauptung {i}: Ein fehlendes Beispiel muss als 'nicht verfügbar' "
                            "benannt werden.")
        if claim.beispiel_art == "hypothetical" and "hypothetisch" not in claim.beispiel.lower():
            problems.append(f"{label}, Behauptung {i}: Hypothetische Beispiele müssen so gekennzeichnet sein.")
        if claim.kind == "proposal" and mentions_law_change(claim.behauptung):
            problems.append(f"{label}, Behauptung {i}: Rechtsänderungen sind als Lösung ausgeschlossen.")
        if claim.berechnung:
            problems += check_calculation(claim.berechnung, allowed_sources, assumption_ids,
                                          f"{label}, Behauptung {i}")
    return problems


def check_citations(ids, allowed_sources, assumption_ids, label):
    allowed = set(allowed_sources) | set(assumption_ids)
    return [f"{label}: Quelle {i} ist nicht aufgelöst oder nicht zugelassen." for i in ids if i not in allowed]


def compute(calc):
    """Constrained arithmetic; no model-generated code runs."""
    values = [i.value for i in calc.inputs]
    if not values:
        raise ValueError("keine Eingaben")
    if calc.operation == "sum":
        return math.fsum(values)
    if calc.operation == "product":
        return math.prod(values)
    if len(values) != 2:
        raise ValueError("zwei Eingaben erforderlich")
    a, b = values
    if calc.operation == "difference":
        return a - b
    if b == 0 and calc.operation in ("ratio", "percent_change"):
        raise ValueError("Division durch null")
    return a / b if calc.operation == "ratio" else (a - b) / b * 100


def check_calculation(calc, allowed_sources, assumption_ids, label):
    problems = check_citations([i.quelle for i in calc.inputs], allowed_sources, assumption_ids, label)
    try:
        expected = compute(calc)
    except ValueError as exc:
        return problems + [f"{label}: Berechnung ungültig ({exc})."]
    if not math.isclose(expected, calc.result, rel_tol=0.005, abs_tol=0.005):
        problems.append(f"{label}: Berechnungsergebnis {calc.result:g} stimmt nicht "
                        f"(korrekt: {expected:g}).")
    return problems


def validate_profile(profile: Profile, allowed):
    problems = word_problem(render_profile(profile), "profile", "Profil")
    for i, entry in enumerate(profile.entries, 1):
        problems += check_citations(entry.quellen, allowed, (), f"Profil, Eintrag {i}")
        if entry.label in ("documented", "reported") and not entry.quellen:
            problems.append(f"Profil, Eintrag {i}: '{entry.label}' braucht eine Quelle.")
    return problems


def validate_priorities(priorities, allowed, existing_ids=()):
    problems = word_problem(render_priorities(priorities), "priorities", "Prioritätenliste")
    objectives = [re.sub(r"\W+", " ", p.objective.casefold()).strip() for p in priorities]
    if len(set(objectives)) != len(objectives):
        problems.append("Prioritätenliste: Ziele dürfen nicht doppelt vorkommen.")
    for i, p in enumerate(priorities, 1):
        problems += check_citations(p.quellen, allowed, (), f"Priorität {i}")
        if not p.objective.strip() or not p.acceptance_condition.strip():
            problems.append(f"Priorität {i}: Ziel und Annahmebedingung sind erforderlich.")
    kept = {p.priority_id for p in priorities if p.priority_id}
    missing = set(existing_ids) - kept
    if missing:
        problems.append("Prioritäten dürfen nicht entfallen: " + ", ".join(sorted(missing)))
    unknown = kept - set(existing_ids)
    if unknown:
        problems.append("Unbekannte Prioritäts-IDs: " + ", ".join(sorted(unknown)))
    return problems


def validate_review(review: ReviewOutput, reviewer, target_ids, allowed_sources):
    problems = word_problem(render_review(review), "ordinary", "Prüfbericht")
    allowed = ALLOWED_ASSESSMENTS[reviewer]
    for i, finding in enumerate(review.findings, 1):
        if finding.assessment not in allowed:
            problems.append(f"Befund {i}: Bewertung {finding.assessment} gehört nicht zu dieser Rolle.")
        if finding.target_id not in target_ids:
            problems.append(f"Befund {i}: Ziel {finding.target_id} gehört nicht zu dieser Prüfung; "
                            f"erlaubt sind: {', '.join(sorted(target_ids))}.")
        if not finding.reason.strip():
            problems.append(f"Befund {i}: Begründung fehlt.")
    # Coverage means "what was examined": it is incomplete only if named targets stayed unexamined.
    # Missing evidence is a finding about the target, not a coverage gap.
    unknown = set(review.unreviewed_targets) - set(target_ids)
    if unknown:
        problems.append("unreviewed_targets enthält unbekannte Ziele: " + ", ".join(sorted(unknown)))
    if review.coverage == "incomplete":
        if not review.unreviewed_targets:
            problems.append("coverage incomplete verlangt unreviewed_targets. Fehlende Belege sind ein Befund "
                            "(insufficient_information oder unresolved_issue), keine Prüflücke.")
        if not (review.coverage_note or "").strip():
            problems.append("Unvollständige Prüfung braucht einen Hinweis zum Prüfumfang.")
    elif review.unreviewed_targets:
        problems.append("Bei coverage complete muss unreviewed_targets leer sein.")
    if not review.findings and review.coverage == "complete":
        problems.append("Eine vollständige Prüfung braucht mindestens einen Befund.")
    return problems


def validate_assessments(items, priority_ids, allowed_refs, limit="assessment"):
    problems = word_problem(render_assessments(items), limit, "Prioritätsbewertung")
    seen = [a.priority_id for a in items]
    missing, unknown = set(priority_ids) - set(seen), set(seen) - set(priority_ids)
    if missing:
        problems.append("Bewertung fehlt für: " + ", ".join(sorted(missing)))
    if unknown:
        problems.append("Unbekannte Prioritäten bewertet: " + ", ".join(sorted(unknown)))
    if len(seen) != len(set(seen)):
        problems.append("Jede Priorität darf nur einmal bewertet werden.")
    for a in items:
        problems += check_citations(a.references, allowed_refs, (), f"Bewertung {a.priority_id}")
    return problems


def validate_final(final: FinalOutput, priority_ids, allowed_refs):
    problems = word_problem(final.summary, "final_summary", "Abschlussstellungnahme")
    if priority_ids:        # without a frozen proposal there is nothing to assess
        problems += validate_assessments(final.assessments, priority_ids, allowed_refs, "final_summary")
    if mentions_law_change(final.summary):
        problems.append("Abschlussstellungnahme: Rechtsänderungen sind als Lösung ausgeschlossen.")
    return problems


def validate_report(report: ReportOutput, known_refs):
    problems = word_problem(render_report(report), "conclusion", "Schlussbericht")
    refs = [r for group in (report.konflikte, report.kompromisse, report.offene_punkte,
                            report.auswirkungen) for item in group for r in item.refs]
    problems += [f"Schlussbericht: Verweis {r} ist nicht aufgelöst." for r in refs if r not in known_refs]
    prose = " ".join(i.beschreibung for i in report.kompromisse)
    if mentions_law_change(prose):
        problems.append("Schlussbericht: Eine Rechtsänderung darf nicht als Lösung vorgeschlagen werden.")
    return problems


def validate_evolution(evolution: EvolutionOutput, known_refs):
    problems = word_problem(evolution.narrative, "evolution", "Debattenverlauf")
    problems += [f"Debattenverlauf: Verweis {r} ist nicht aufgelöst."
                 for r in evolution.refs if r not in known_refs]
    if not evolution.refs:
        problems.append("Debattenverlauf: Quellen- und Ereignisverweise sind erforderlich.")
    return problems


# --------------------------------------------------------------------------
# Satisfaction and stopping

def satisfaction(actor_id, statuses, disputed=False):
    """Exact integer rule: 5 * met >= 4 * total. Invalid denominators give null."""
    total = len(statuses)
    met = sum(1 for s in statuses if s == "met")
    if total == 0:
        return {"actor_id": actor_id, "met_count": 0, "priority_count": 0, "percent": None,
                "threshold_reached": None, "disputed": disputed}
    return {"actor_id": actor_id, "met_count": met, "priority_count": total,
            "percent": round(100 * met / total, 1), "threshold_reached": 5 * met >= 4 * total,
            "disputed": disputed}


def next_streak(streak, round_complete, per_actor):
    """Counter after a round. Any failed or incomplete round resets it."""
    if round_complete and per_actor and all(s["threshold_reached"] is True for s in per_actor):
        return streak + 1
    return 0


LEGAL_ORDER = ("no_conflict_identified", "defensible_with_conditions", "unresolved_issue",
               "apparent_legal_conflict", "requires_legal_amendment")


def worst_legal(assessments):
    """Most restrictive LegalEye category among findings, or not_reviewed."""
    ranked = [a for a in assessments if a in LEGAL_ORDER]
    return max(ranked, key=LEGAL_ORDER.index) if ranked else "not_reviewed"


def admissibility(clause_statuses):
    if not clause_statuses or "not_reviewed" in clause_statuses:
        return "not_reviewed"
    if {"requires_legal_amendment", "apparent_legal_conflict"} & set(clause_statuses):
        return "inadmissible"
    if "unresolved_issue" in clause_statuses:
        return "unresolved"
    if "defensible_with_conditions" in clause_statuses:
        return "conditional"
    return "admissible_within_reviewed_sources"
