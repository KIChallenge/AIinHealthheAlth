"""Request intake: parsing, secrets check, actor resolution, clarification rules.

All of this runs before the deliberation timer starts.
"""

import json
import re

from pydantic import ValidationError

from .api_models import (LEGAL_SCOPE, RULEBOOK_VERSION, ClarificationQuestion, DebateRequest, Message,
                         admin_id)
from .rules import mentions_law_change

SECRET_PATTERNS = re.compile(
    r"(api[_-]?key|passwor[dt]|client[_-]?secret|bearer\s+[A-Za-z0-9._-]{12,}|\bsk-[A-Za-z0-9]{16,})",
    re.IGNORECASE)
PERCENT = re.compile(r"\d[\d.,]*\s*(%|prozent)", re.IGNORECASE)
REVIEWERS = {"truthseeker", "legaleye", "ethicmind", "meta-moderator", "metamoderator"}

FIELD_LABELS = {"extra_forbidden": "unbekanntes Feld", "missing": "Pflichtfeld fehlt"}


def parse_request(raw):
    """Parse raw JSON text. Returns (request, errors)."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        return None, [Message(code="malformed_json", message=f"Die Anfrage ist kein gültiges JSON: {exc}")]
    return validate_request(data)


def validate_request(data):
    if not isinstance(data, dict):
        return None, [Message(code="invalid_request", message="Die Anfrage muss ein JSON-Objekt sein.")]
    try:
        return DebateRequest.model_validate(data), []
    except ValidationError as exc:
        errors = []
        for item in exc.errors():
            field = ".".join(str(p) for p in item["loc"]) or "Anfrage"
            reason = FIELD_LABELS.get(item["type"], item["msg"].removeprefix("Value error, "))
            errors.append(Message(code="invalid_request", message=f"{field}: {reason}"))
        return None, errors


def contains_secret(request, secrets=()):
    texts = [request.question] + [a.description for a in request.scenario_assumptions] \
        + [c.description for c in request.hard_constraints]
    for text in texts:
        if SECRET_PATTERNS.search(text) or any(s and s in text for s in secrets):
            return True
    return False


def resolve_actors(names, known, aliases):
    """Exact name match plus alias table. known: [{actor_id, name}]. Returns (resolved, errors)."""
    by_name = {}
    for actor in known:
        by_name.setdefault(actor["name"], []).append(actor)
    resolved, errors, seen = [], [], {}
    for requested in names:
        target = aliases.get(requested, requested)
        if requested.strip().casefold() in REVIEWERS:
            errors.append(Message(code="reviewer_as_actor",
                                  message=f"{requested} ist eine Prüfrolle und kein Akteur."))
            continue
        matches = by_name.get(target, [])
        if not matches:
            errors.append(Message(code="unknown_actor", message=f"Unbekannter Akteur: {requested}."))
        elif len(matches) > 1:
            errors.append(Message(code="ambiguous_actor", message=f"Mehrdeutiger Akteur: {requested}."))
        elif matches[0]["actor_id"] in seen:
            errors.append(Message(code="duplicate_actor",
                                  message=f"Akteur doppelt angegeben: {requested} und "
                                          f"{seen[matches[0]['actor_id']]}."))
        else:
            seen[matches[0]["actor_id"]] = requested
            resolved.append({**matches[0], "requested_name": requested})
    return resolved, errors


def clarification_needed(request):
    """German questions that must be answered before a debate can start."""
    questions = []
    if mentions_law_change(request.question):
        questions.append(ClarificationQuestion(
            code="law_change_out_of_scope",
            frage="Die Anfrage zielt auf eine Rechtsänderung. Der Simulator arbeitet ausschließlich "
                  f"innerhalb des geltenden Rechts: {LEGAL_SCOPE} Bitte formulieren Sie die Frage so, "
                  "dass sie innerhalb des bestehenden Rechtsrahmens beantwortet werden kann."))
    if request.reference_date is None:
        questions.append(ClarificationQuestion(
            code="reference_date_missing",
            frage="Welcher Stichtag (JJJJ-MM-TT) gilt für das Szenario? Er bestimmt das anwendbare "
                  "Recht und die zulässigen Quellen und wird nicht stillschweigend ergänzt."))
    for constraint in request.hard_constraints:
        numeric_percent = constraint.unit is not None and constraint.unit.strip() in ("%", "Prozent")
        if (numeric_percent or PERCENT.search(constraint.description)) \
                and not (constraint.baseline and constraint.period):
            questions.append(ClarificationQuestion(
                code="ambiguous_percentage",
                frage=f"Die Beschränkung „{constraint.description}“ ist nicht eindeutig: Auf welche "
                      "Bezugsgröße (baseline) und welchen Zeitraum (period) bezieht sich der Prozentwert?"))
    return questions


def normalise_task(request):
    assumptions = [a.model_copy(update={"id": a.id or admin_id("A", a.description)})
                   for a in request.scenario_assumptions]
    constraints = [c.model_copy(update={"id": c.id or admin_id("C", c.description)})
                   for c in request.hard_constraints]
    return {
        "question": request.question, "reference_date": request.reference_date,
        "assumptions": [a.model_dump() for a in assumptions],
        "constraints": [c.model_dump() for c in constraints],
        "legal_scope": LEGAL_SCOPE, "rulebook_version": RULEBOOK_VERSION,
    }
