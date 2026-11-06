"""Builds the final JSON from stored records through validated schemas.

Graph state is never dumped. Status tables come from stored assessments and decisions,
not from model prose. Every cross-reference is checked before the result is returned.
"""

from .api_models import (LEGAL_SCOPE, RULEBOOK_VERSION, SCHEMA_VERSION, CapabilityRecord, DebateRequest,
                         DebateResult, Message, ModelRecord, empty_result)
from .events import iso
from .llm_models import CLEAN_ASSESSMENTS


def _proposal_view(proposal):
    return {**{k: v for k, v in proposal.items()}, "created_at": iso(proposal["created_at"])}


def _clean_review(review):
    return {k: v for k, v in review.items() if k not in ("version", "targets")}


def _usage(state, deps):
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "requests": 0, "retries": 0,
              "repairs": 0, "throttled": 0, "queue_seconds": 0.0, "calls_without_usage": 0}
    for record in (state.get("usage") or {}).values():
        for key in totals:
            totals[key] += record.get(key, 0)
    missing = list(deps.llm.scheduler.missing_limits)
    if totals["calls_without_usage"]:
        missing.append("Tokenverbrauch einzelner Aufrufe")
    return {**totals, "queue_seconds": round(totals["queue_seconds"], 3), "missing_measurements": missing}


def _reported_model(state):
    for record in (state.get("usage") or {}).values():
        if record.get("reported_model"):
            return record["reported_model"]
    return None


def _model(state, settings):
    public = settings.public_view()
    reported = _reported_model(state)
    capabilities = CapabilityRecord(
        responses_api=True if reported else None, structured_outputs=True if reported else None,
        model_identity_verified=False,
        note="Die Antwort nennt nur das Deployment; die Modellfamilie gpt-5.4-mini ist ohne "
             "Verwaltungszugriff nicht bestätigt.")
    return ModelRecord(expected_model=settings.model, reported_model=reported, deployment=settings.deployment or None,
                       capabilities=capabilities,
                       settings={k: v for k, v in public.items() if isinstance(v, (str, int, float)) or v is None})


def _timing(state, settings):
    def span(a, b):
        return round(b - a, 3) if a and b else None
    started, ended = state.get("deliberation_started_at"), state.get("deliberation_ended_at")
    final_start, final_end = state.get("finalization_started_at"), state.get("finalization_ended_at")
    return {
        "deliberation_started_at": iso(started), "deliberation_deadline_at": iso(state.get("deliberation_deadline_at")),
        "deliberation_ended_at": iso(ended), "finalization_started_at": iso(final_start),
        "finalization_deadline_at": iso(state.get("finalization_deadline_at")), "finalization_ended_at": iso(final_end),
        "deliberation_elapsed_seconds": span(started, ended), "finalization_elapsed_seconds": span(final_start, final_end),
        "deliberation_budget_seconds": settings.deliberation_timeout,
        "finalization_budget_seconds": settings.finalization_timeout,
    }


def _satisfaction(state):
    rounds = state.get("rounds") or {}
    if not rounds:
        return []
    return rounds[max(rounds, key=int)]["satisfaction"]


def _latest_assessments(state, actor_id):
    latest = {}
    for a in sorted((state.get("assessments") or {}).values(), key=lambda x: (x["round"], x["proposal_version"],
                                                                             x["assessment_id"].startswith("AS-final"))):
        if a["actor_id"] == actor_id:
            latest[a["priority_id"]] = a
    return [latest[k] for k in sorted(latest)]


def _actors(state):
    out = []
    satisfaction = {s["actor_id"]: s for s in _satisfaction(state)}
    for actor in state.get("actors") or []:
        aid = actor["actor_id"]
        profile = (state.get("profiles") or {}).get(aid)
        latest = {}
        for p in (state.get("priorities") or {}).values():
            if p["actor_id"] == aid and (p["priority_id"] not in latest
                                         or p["baseline_version"] > latest[p["priority_id"]]["baseline_version"]):
                latest[p["priority_id"]] = p
        decisions = sorted((_decision(d) for d in (state.get("decisions") or {}).values() if d["actor_id"] == aid),
                           key=lambda d: d["decision_id"])
        final = (state.get("final_summaries") or {}).get(aid)
        out.append({"actor_id": aid, "name": actor["name"], "requested_name": actor["requested_name"],
                    "profile": profile, "initial_standpoint": profile["standpunkt"] if profile else None,
                    "final_standpoint": final["summary"] if final else None,
                    "priorities": [latest[k] for k in sorted(latest)],
                    "latest_assessments": _latest_assessments(state, aid), "acceptance_decisions": decisions,
                    "satisfaction": satisfaction.get(aid)})
    return out


def _decision(decision):
    return {**decision, "at": iso(decision["at"])}


def _sources(state):
    fields = ("passage_id", "doc_id", "doc_type", "title", "publisher", "publication_date", "valid_from",
              "valid_until", "source_url", "content_hash", "source_path", "locator", "machine_locator", "date_unknown")
    return [{k: p.get(k) for k in fields} for _, p in sorted((state.get("passages") or {}).items())]


def _report(state, actors):
    task = state["task"]
    main = (state.get("report") or {}).get("main")
    missing = []
    if not main:
        missing.append("Schlussbericht des Meta-Moderators")
    decisions = list((state.get("decisions") or {}).values())
    frozen = state.get("frozen_key")
    proposal = (state.get("proposals") or {}).get(frozen) if frozen else None
    last = {}
    for d in sorted(decisions, key=lambda x: (x["round"], x["decision_id"].startswith("D-final"))):
        if proposal and d["target_version"] == proposal["version"]:
            last[d["actor_id"]] = d
    names = {a["actor_id"]: a["name"] for a in actors}
    by_kind = lambda kind: sorted(names[a] for a, d in last.items() if d["kind"] == kind)
    agents = [{"actor_id": a["actor_id"], "name": a["name"],
               "standpunkt": a["final_standpoint"] or a["initial_standpoint"],
               "prioritaeten": [f"{p['priority_id']}: {p['objective']}" for p in a["priorities"]],
               "entscheidung": last[a["actor_id"]]["kind"] if a["actor_id"] in last else None,
               "zufriedenheit": a["satisfaction"]} for a in actors]
    sections = main or {}
    return {
        "Frage": {"frage": task["question"], "stichtag": task["reference_date"], "geltungsbereich": task["legal_scope"],
                  "annahmen": [a["description"] for a in task["assumptions"]], "einordnung": sections.get("einordnung")},
        "Agenten": agents,
        "Verlauf": {"zusammenfassung": sections.get("verlauf_kurz"), "runden": state.get("completed_rounds") or 0,
                    "vorschlagsversionen": sorted(state.get("proposals") or {}),
                    "ereignis_refs": sorted(state.get("decisions") or {})},
        "Konflikte": sections.get("konflikte", []), "Kompromisse": sections.get("kompromisse", []),
        "Konsens": {"einschaetzung": sections.get("konsens"),
                    "schwelle_erreicht_von": sorted(names[s["actor_id"]] for s in _satisfaction(state)
                                                    if s["threshold_reached"]),
                    "akzeptiert_von": by_kind("accept"), "bedingt_akzeptiert_von": by_kind("conditional"),
                    "abgelehnt_von": by_kind("reject"),
                    "rechtliche_zulaessigkeit": proposal["legal_admissibility"] if proposal else None,
                    "auflagen": sorted({c for d in last.values() for c in d["conditions"]})},
        "Offene_Punkte": sections.get("offene_punkte", []), "Auswirkungen": sections.get("auswirkungen", []),
        "fehlende_abschnitte": missing,
    }


def collect_reference_problems(result):
    """All references must resolve to returned records or explicit source records."""
    sources = {s.passage_id for s in result.sources}
    assumptions = {a.id for a in result.task.assumptions} if result.task else set()
    ids = set(sources) | assumptions
    for a in result.arguments:
        ids.add(a.argument_id)
        ids |= {c.claim_id for c in a.claims}
    ids |= {n.item_id for n in result.negotiation}
    proposals = {f"{p.proposal_id}-v{p.version}" for p in result.proposal_history}
    ids |= proposals
    for p in result.proposal_history:
        ids |= {c.clause_id for c in p.clauses}
    for actor in result.actors:
        ids |= {p.priority_id for p in actor.priorities}
        ids.add(f"PROFIL-{actor.actor_id}")
    assessments = {a.assessment_id for a in result.priority_history}
    decisions = {d.decision_id for d in result.decisions}
    ids |= assessments | decisions
    findings = {f.finding_id for r in result.reviews for f in r.findings}
    ids |= findings | {c.challenge_id for c in result.challenges}
    problems = []

    def need(ref, where):
        if ref not in ids:
            problems.append(f"{where}: Verweis {ref} ist nicht aufgelöst.")

    for a in result.arguments:
        for c in a.claims:
            for ref in c.quellen:
                need(ref, c.claim_id)
    for p in result.proposal_history:
        for c in p.clauses:
            for ref in c.provenance + c.premises:
                need(ref, c.clause_id)
    for r in result.reviews:
        for f in r.findings:
            if f.target_id != "KEIN-VORSCHLAG":
                need(f.target_id, f.finding_id)
    for c in result.challenges:
        need(c.finding_id, c.challenge_id)
    for a in result.priority_history:
        need(a.priority_id, a.assessment_id)
        for ref in a.references + a.reviewer_objections:
            need(ref, a.assessment_id)
    for d in result.decisions:
        need(d.target_id, d.decision_id)
    if result.report:
        groups = (result.report.Konflikte, result.report.Kompromisse, result.report.Offene_Punkte,
                  result.report.Auswirkungen)
        for group in groups:
            for item in group:
                for ref in item.refs:
                    need(ref, "Bericht")
    if result.debate_evolution:
        for ref in result.debate_evolution.refs:
            need(ref, "Debattenverlauf")
    return problems


def build_result(state, deps):
    settings = deps.settings
    debate_id = state["debate_id"]
    request = DebateRequest.model_validate(state["request"])
    outcome = state.get("outcome")
    fatal = state.get("fatal") or {}
    if outcome in ("needs_clarification", "invalid_input") or (outcome == "failed"):
        errors = [Message(**e) for e in state.get("input_errors") or []]
        errors += [Message(code=f["code"], message=f["message"]) for f in fatal.values()]
        status = outcome
        result = empty_result(debate_id, status, request=request, errors=errors,
                              clarification=state.get("clarifications") or [])
        return result.model_dump(mode="json")

    actors = _actors(state)
    warnings = [Message(code="hinweis", message=text) for _, text in sorted((state.get("warnings") or {}).items())]
    errors = [Message(code=f["code"], message=f["message"]) for f in fatal.values()]
    for task_id, record in sorted((state.get("incomplete") or {}).items()):
        warnings.append(Message(code="unvollstaendig", message=f"{task_id}: {record['reason']}"))
    pending = [c for c in (state.get("challenges") or {}).values() if c["status"] == "pending"]
    if pending:
        warnings.append(Message(code="offene_widersprueche", message=f"{len(pending)} Widersprüche blieben unbeantwortet."))
    report_data = state.get("report") or {}
    evolution = report_data.get("evolution")
    proposals = {k: _proposal_view(p) for k, p in (state.get("proposals") or {}).items()}
    frozen = proposals.get(state.get("frozen_key"))
    if frozen:
        if not frozen["review_complete"]:
            warnings.append(Message(code="pruefung_unvollstaendig", message="Der eingefrorene Vorschlag wurde nicht "
                                    "vollständig geprüft; unvollständige Prüfung ist keine Zustimmung."))
        unresolved = frozen["legal_admissibility"] in ("unresolved", "inadmissible", "not_reviewed")
        if unresolved:
            warnings.append(Message(code="rechtlich_ungeklaert", message="Der eingefrorene Vorschlag ist rechtlich "
                                    f"nicht bestätigt ({frozen['legal_admissibility']}) und keine freigegebene Lösung."))
    final_reviews = [r for r in (state.get("reviews") or {}).values() if r["stage"] == "final"]
    finished_actors = len(state.get("final_summaries") or {}) == len(state.get("actors") or [])
    all_final = (finished_actors and bool(final_reviews) and report_data.get("main") and evolution
                 and all(r["status"] == "complete" for r in final_reviews))
    stop = state.get("stop_reason")
    if fatal and not state.get("finalization_started_at"):
        status, stop = "failed", "execution_error"
    else:
        status = "completed" if all_final and not fatal else "incomplete"
    data = {
        "schema_version": SCHEMA_VERSION, "debate_id": debate_id, "status": status, "stop_reason": stop,
        "request": request.model_dump(mode="json"),
        "task": {**state["task"], "legal_scope": LEGAL_SCOPE, "rulebook_version": RULEBOOK_VERSION},
        "model": _model(state, settings).model_dump(mode="json"), "timing": _timing(state, settings),
        "usage": _usage(state, deps),
        "metrics": {"completed_rounds": state.get("completed_rounds") or 0, "success_streak": state.get("success_streak") or 0,
                    "satisfaction": _satisfaction(state)},
        "final_proposal": frozen, "actors": actors,
        "arguments": [a for _, a in sorted((state.get("arguments") or {}).items())],
        "negotiation": [n for _, n in sorted((state.get("negotiation") or {}).items())],
        "reviews": [_clean_review(r) for _, r in sorted((state.get("reviews") or {}).items())],
        "challenges": [c for _, c in sorted((state.get("challenges") or {}).items())],
        "proposal_history": [p for _, p in sorted(proposals.items())],
        "priority_history": sorted((state.get("assessments") or {}).values(),
                                   key=lambda a: (a["round"], a["proposal_version"], a["assessment_id"])),
        "decisions": sorted((_decision(d) for d in (state.get("decisions") or {}).values()),
                            key=lambda d: d["decision_id"]),
        "timeline": [{"event_id": e["event_id"], "at": iso(e["at"]), "role": e["role"], "round": e["round"],
                      "phase": e["phase"], "type": e["type"], "visibility": e["visibility"], "refs": e["refs"],
                      "causes": e["causes"]} for e in deps.events.events(debate_id)],
        "sources": _sources(state), "report": _report(state, actors),
        "debate_evolution": ({"narrative": evolution["narrative"], "refs": evolution["refs"],
                              "word_count": len(evolution["narrative"].split())} if evolution
                             else {"narrative": None, "refs": [], "word_count": 0}),
        "clarification_questions": [], "warnings": [w.model_dump() for w in warnings],
        "errors": [e.model_dump() for e in errors],
    }
    result = DebateResult.model_validate(data)
    problems = collect_reference_problems(result)
    if problems:
        result.errors += [Message(code="unresolved_reference", message=p) for p in problems[:50]]
        if result.status == "completed":
            result.status = "incomplete"
    return result.model_dump(mode="json")


__all__ = ["build_result", "collect_reference_problems", "CLEAN_ASSESSMENTS"]
