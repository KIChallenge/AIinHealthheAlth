"""The executable debate workflow (LangGraph StateGraph).

Program nodes enforce rules, model nodes run in Send fan-out, and each fan-out joins at
an explicit barrier. Clients and credentials live in closures, never in graph state.
Records are stored under stable ids, so re-running a branch cannot duplicate anything.
Stopping is decided by `close_round` and the deadline checks, not by the recursion limit.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import llm_models as lm
from . import prompts, rules
from .assemble import build_result
from .errors import (DeadlineExpired, LlmFatalError, LlmOutputError, LlmTransientError, OversizedRequest,
                     PermissionDeniedError, SourceError)
from .intake import clarification_needed, contains_secret, normalise_task, resolve_actors
from .llm import Call, CallStats, estimate_tokens
from .sources import ACTOR, ETHICMIND, LEGALEYE, TRUTHSEEKER, Role, searchable_text

REVIEWERS = (TRUTHSEEKER, LEGALEYE, ETHICMIND)
PROPOSAL_ID = "PROP-1"
CLAUSE_BATCH = 3
PASSAGE_ID = re.compile(r"P-[0-9a-f]{12}")


def merge(left, right):
    return {**(left or {}), **(right or {})}


class DebateState(TypedDict, total=False):
    debate_id: str
    request: dict
    item: dict                      # payload of a fan-out branch
    outcome: str | None
    clarifications: list
    input_errors: list
    task: dict
    actors: list
    search_mode: str
    deliberation_started_at: float
    deliberation_deadline_at: float
    deliberation_ended_at: float
    finalization_started_at: float
    finalization_deadline_at: float
    finalization_ended_at: float
    round: int
    baseline_version: int
    proposal_version: int
    amendments_in_round: int
    success_streak: int
    completed_rounds: int
    last_complete_key: str | None
    frozen_key: str | None
    stop_reason: str | None
    stage: str
    blockage: str | None
    result: dict
    passages: Annotated[dict, merge]
    grants: Annotated[dict, merge]
    profiles: Annotated[dict, merge]
    arguments: Annotated[dict, merge]
    priorities: Annotated[dict, merge]
    negotiation: Annotated[dict, merge]
    corrections: Annotated[dict, merge]
    reviews: Annotated[dict, merge]
    challenges: Annotated[dict, merge]
    proposals: Annotated[dict, merge]
    assessments: Annotated[dict, merge]
    decisions: Annotated[dict, merge]
    final_summaries: Annotated[dict, merge]
    rounds: Annotated[dict, merge]
    report: Annotated[dict, merge]
    incomplete: Annotated[dict, merge]
    usage: Annotated[dict, merge]
    warnings: Annotated[dict, merge]
    fatal: Annotated[dict, merge]


@dataclass
class Deps:
    settings: object
    llm: object
    sources: object
    events: object
    clock: object
    deadlines: object


def combine(*updates):
    out = {}
    for update in updates:
        for key, value in update.items():
            out[key] = {**out[key], **value} if isinstance(value, dict) and key in out else value
    return out


def warning_update(text):
    return {hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]: text}


def current_priorities(state, actor_id):
    latest = {}
    for record in (state.get("priorities") or {}).values():
        if record["actor_id"] == actor_id:
            pid = record["priority_id"]
            if pid not in latest or record["baseline_version"] > latest[pid]["baseline_version"]:
                latest[pid] = record
    return [latest[k] for k in sorted(latest)]


def latest_argument(state, actor_id):
    found = [a for a in (state.get("arguments") or {}).values() if a["actor_id"] == actor_id]
    return max(found, key=lambda a: a["version"]) if found else None


def current_key(state):
    version = state.get("proposal_version") or 0
    return f"{PROPOSAL_ID}-v{version}" if version else None


def priority_gaps(state):
    """Status of every fixed priority against the latest assessment, open ones first.

    Built by program code from stored records. Empty before anything was assessed.
    """
    latest = {}
    for a in sorted((state.get("assessments") or {}).values(), key=lambda x: (x["round"], x["proposal_version"])):
        latest[a["priority_id"]] = a
    if not latest:
        return []
    rows = []
    for actor in state["actors"]:
        for p in current_priorities(state, actor["actor_id"]):
            a = latest.get(p["priority_id"])
            rows.append({"priority_id": p["priority_id"], "akteur": actor["name"],
                         "annahmebedingung": p["acceptance_condition"],
                         "status": a["status"] if a else "nicht bewertet",
                         "begruendung": a["reason"] if a else None})
    return sorted(rows, key=lambda r: (r["status"] == "met", r["priority_id"]))


def public_proposal(state, key=None):
    proposal = (state.get("proposals") or {}).get(key or current_key(state))
    if not proposal:
        return None
    return {"proposal_id": proposal["proposal_id"], "version": proposal["version"],
            "legal_admissibility": proposal["legal_admissibility"],
            "clauses": [{k: c[k] for k in ("clause_id", "text", "legal_status")} for c in proposal["clauses"]]}


def material(finding):
    return finding["assessment"] not in lm.CLEAN_ASSESSMENTS


def stage_reviews(state, stage, round_no):
    return [r for r in (state.get("reviews") or {}).values() if r["stage"] == stage and r["round"] == round_no]


def findings_of(reviews):
    return [f for r in reviews for f in r["findings"]]


def withdrawn_findings(state):
    return {c["finding_id"] for c in (state.get("challenges") or {}).values()
            if c.get("reply_decision") == "withdraw"}


def actor_view(state, deps, actor):
    aid = actor["actor_id"]
    round_no = state.get("round") or 0
    assessments = [a for a in (state.get("assessments") or {}).values()
                   if a["actor_id"] == aid and a["round"] == round_no - 1]
    open_issues = [{"finding_id": f["finding_id"], "assessment": f["assessment"], "reason": f["reason"]}
                   for r in stage_reviews(state, "candidate", max(round_no - 1, 1))
                   for f in r["findings"] if material(f)]
    remaining = deps.deadlines.remaining(state.get("deliberation_deadline_at")) or 0
    profile = (state.get("profiles") or {}).get(aid)
    return {"round": round_no, "remaining_seconds": max(int(remaining), 0),
            "success_streak": state.get("success_streak") or 0,
            "profile": profile, "priorities": current_priorities(state, aid),
            "proposal": public_proposal(state), "assessments": assessments, "open_issues": open_issues}


def published_package(state, limit_tokens):
    """The same package for every actor; reduced to current versions if it is too large."""
    entries, reduced = [], False
    for actor in state["actors"]:
        aid = actor["actor_id"]
        versions = sorted((a for a in state.get("arguments", {}).values() if a["actor_id"] == aid),
                          key=lambda a: a["version"])
        entries.append({"akteur": actor["name"], "profil": state.get("profiles", {}).get(aid),
                        "prioritaeten": current_priorities(state, aid), "argumente": versions})
    reviews = [{"stage": r["stage"], "reviewer": r["reviewer"], "actor_id": r["actor_id"],
                "findings": [{k: f[k] for k in ("finding_id", "target_id", "assessment", "reason")}
                             for f in r["findings"]]}
               for stage in ("initial", "second") for r in stage_reviews(state, stage, 0)]
    package = {"akteure": entries, "pruefungen": reviews}
    if estimate_tokens(prompts.dumps(package)) > limit_tokens:
        reduced = True
        for entry in entries:
            entry["argumente"] = entry["argumente"][-1:]
        package = {"akteure": entries, "pruefungen": [
            {**r, "findings": [f for f in r["findings"] if material(f)]} for r in reviews
            if r["stage"] == "second"],
            "hinweis": "Frühere Fassungen sind gekürzt und im Ereignisprotokoll abrufbar."}
    return package, reduced


def known_references(state):
    refs = set()
    for arg in (state.get("arguments") or {}).values():
        refs.add(arg["argument_id"])
        refs |= {c["claim_id"] for c in arg["claims"]}
    refs |= set(state.get("negotiation") or {}) | set(state.get("decisions") or {})
    refs |= set(state.get("assessments") or {}) | set(state.get("proposals") or {})
    for proposal in (state.get("proposals") or {}).values():
        refs |= {c["clause_id"] for c in proposal["clauses"]}
    for review in (state.get("reviews") or {}).values():
        refs |= {f["finding_id"] for f in review["findings"]}
    refs |= set(state.get("passages") or {}) | {a["id"] for a in state["task"]["assumptions"]}
    return refs


def debate_record(state, limit_tokens):
    """Protocol for the meta-moderator: full when it fits, else current state plus references."""
    frozen = state.get("frozen_key")
    record = {
        "stopp": state.get("stop_reason"), "runden": state.get("rounds"),
        "vorschlag": public_proposal(state, frozen) if frozen else None,
        "versionen": [{"key": k, "meta": p["meta_update"], "zulaessigkeit": p["legal_admissibility"]}
                      for k, p in sorted((state.get("proposals") or {}).items())],
        "abschluss": state.get("final_summaries"),
        "entscheidungen": sorted(state.get("decisions", {}).values(), key=lambda d: d["decision_id"]),
        "bewertungen": [{k: a[k] for k in ("assessment_id", "actor_id", "priority_id", "round", "status",
                                           "previous_status", "reason")}
                        for a in state.get("assessments", {}).values()],
        "prioritaeten": [{k: p[k] for k in ("priority_id", "actor_id", "objective", "baseline_version")}
                         for p in state.get("priorities", {}).values()],
        "befunde": [{"finding_id": f["finding_id"], "assessment": f["assessment"], "reason": f["reason"]}
                    for r in state.get("reviews", {}).values() for f in r["findings"] if material(f)],
        "verhandlung": sorted(state.get("negotiation", {}).values(), key=lambda n: n["item_id"]),
        "widersprueche": list(state.get("challenges", {}).values()),
    }
    if estimate_tokens(prompts.dumps(record)) > limit_tokens:
        record["verhandlung"] = record["verhandlung"][-12:]
        record["bewertungen"] = record["bewertungen"][-12:]
        record["befunde"] = record["befunde"][-20:]
        record["gekuerzt"] = "Ältere Einträge sind gekürzt und per ID im Ereignisprotokoll abrufbar."
    return record


def build_workflow(deps: Deps):
    settings, llm, sources, events, clock, deadlines = (
        deps.settings, deps.llm, deps.sources, deps.events, deps.clock, deps.deadlines)
    limits = settings.limits
    retrieval = settings.retrieval

    # ---- helpers -----------------------------------------------------------

    def in_finalization(state):
        return state.get("finalization_started_at") is not None

    def phase_deadline(state):
        return state["finalization_deadline_at"] if in_finalization(state) else state["deliberation_deadline_at"]

    def stop_requested(state):
        """The operator asked to end the deliberation now and go to finalisation."""
        return Path(settings.state_dir, f"stop-{state['debate_id']}").exists()

    def emit(state, event_id, role, phase, visibility, type_, refs=(), causes=(), payload=None):
        return events.emit(state["debate_id"], event_id, clock.time(), role, state.get("round") or 0, phase,
                           visibility, type_, refs, causes, payload)

    async def ask(state, task_id, role, phase, instr, prompt, model, validate, max_output, final=False):
        """One model operation with at most one recorded repair. Returns (parsed, updates)."""
        stats, repairs, parsed, failure = CallStats(), 0, None, None
        updates = {"incomplete": {}, "fatal": {}}
        deadline, text = phase_deadline(state), prompt
        for attempt in (0, 1):
            if deadlines.expired(deadline):
                failure = "Phasenfrist abgelaufen."
                break
            if not in_finalization(state) and stop_requested(state):
                failure = "Beratung wurde gestoppt."
                break
            rejected = None
            try:
                candidate = await llm.structured(
                    Call(task_id=task_id, role=role, instructions=instr, prompt=text, output_model=model,
                         max_output_tokens=max_output, deadline_at=deadline, final=final), stats)
                problems = validate(candidate)
                if not problems:
                    parsed, failure = candidate, None
                    break
                failure, rejected = " ".join(problems), candidate.model_dump()
            except LlmFatalError as exc:
                updates["fatal"][task_id] = {"code": exc.code, "message": exc.message}
                failure = exc.message
                break
            except (DeadlineExpired, OversizedRequest, LlmTransientError) as exc:
                failure = exc.message
                break
            except LlmOutputError as exc:
                failure = exc.message
            emit(state, f"{task_id}:fehler{attempt}", role, phase, "internal", "validation_failure",
                 payload={"reason": failure, "rejected": rejected})
            if attempt == 0:
                repairs = 1
                text = (f"{prompt}\n\nDeine vorige Antwort war ungültig: {failure}\n"
                        f"Korrigiere nur diese Mängel. Vorige Antwort: {prompts.dumps(rejected) if rejected else '-'}")
        if parsed is not None and deadlines.expired(deadline):
            emit(state, f"{task_id}:spaet", role, phase, "internal", "late_response")
            parsed, failure = None, "Antwort traf nach Fristablauf ein und wurde verworfen."
        if parsed is None and task_id not in updates["fatal"]:
            updates["incomplete"][task_id] = {"role": role, "phase": phase, "reason": failure or "unbekannt"}
        updates["usage"] = {task_id: {**stats.as_dict(), "repairs": repairs}}
        return parsed, updates

    async def fetch(state, role, query, documents, doc_type=None):
        """Permission-filtered retrieval within the remaining phase time."""
        remaining = deadlines.remaining(phase_deadline(state))
        if remaining <= 0:
            return [], {}
        try:
            passages, notes = await clock.wait_for(
                sources.retrieve(state["debate_id"], role, query, state["task"]["reference_date"], documents,
                                 doc_type), remaining)
        except TimeoutError:
            return [], {"warnings": warning_update(f"Quellensuche für {role.key} wegen Fristablauf abgebrochen.")}
        except (SourceError, PermissionDeniedError) as exc:
            return [], {"warnings": warning_update(f"Quellensuche für {role.key} fehlgeschlagen: {exc.message}")}
        hidden = ("text", "context_before", "context_after", "cross_references")
        return passages, {
            "passages": {p["passage_id"]: {k: v for k, v in p.items() if k not in hidden} for p in passages},
            "grants": {f"{role.key}|{p['passage_id']}": True for p in passages},
            "warnings": {k: v for n in notes for k, v in warning_update(n).items()}}

    def granted(state, role):
        prefix = f"{role.key}|"
        return {k[len(prefix):] for k in (state.get("grants") or {}) if k.startswith(prefix)}

    def assumption_ids(state):
        return [a["id"] for a in state["task"]["assumptions"]]

    def priority_records(state, actor_id, priorities, existing):
        """Assign program ids. Pre-publication corrections stay in baseline version 1."""
        out, used = {}, len(existing)
        for p in priorities:
            pid = p.priority_id
            if pid is None:
                used += 1
                pid = f"PR-{actor_id}-{used}"
            out[f"{pid}@1"] = {"priority_id": pid, "actor_id": actor_id, "baseline_version": 1,
                               "objective": p.objective, "acceptance_condition": p.acceptance_condition,
                               "quellen": p.quellen, "evidence_basis": p.evidence_basis,
                               "condition_is_simulated": p.condition_is_simulated,
                               "law_scope_conflict": p.law_scope_conflict}
        return out

    def argument_record(actor_id, version, stage, round_no, argument, explanation):
        arg_id = f"ARG-{actor_id}-v{version}"
        claims = []
        for i, c in enumerate(argument.claims, 1):
            data = c.model_dump()
            checked = None
            if c.berechnung:
                checked = not rules.check_calculation(c.berechnung, data["quellen"] + [
                    x.quelle for x in c.berechnung.inputs], (), "")
            claims.append({"claim_id": f"{arg_id}-B{i}", **data, "berechnung_geprueft": checked})
        return {"argument_id": arg_id, "actor_id": actor_id, "version": version, "stage": stage,
                "round": round_no, "claims": claims, "change_explanation": explanation,
                "word_count": rules.count_words(rules.render_argument(argument)), "status": "complete"}

    def profile_record(profile):
        return {**profile.model_dump(), "word_count": rules.count_words(rules.render_profile(profile))}

    # ---- steps 1-3 ---------------------------------------------------------

    async def validate_request(state):
        from .api_models import DebateRequest, Message
        request = DebateRequest.model_validate(state["request"])
        if contains_secret(request, [settings.api_key, settings.neo4j_password]):
            return {"outcome": "invalid_input", "input_errors": [Message(
                code="credentials_in_request",
                message="Die Anfrage darf keine Zugangsdaten enthalten.").model_dump()]}
        try:
            schema = await sources.adapter.verify_schema()
            known = await sources.adapter.list_actors()
        except SourceError as exc:
            return {"outcome": "failed", "fatal": {"sources": {"code": exc.code, "message": exc.message}}}
        if not schema["ok"]:
            return {"outcome": "failed", "fatal": {"schema": {
                "code": "schema_mismatch",
                "message": "Das Graphschema entspricht nicht der Erwartung: " + "; ".join(schema["problems"])}}}
        actors, errors = resolve_actors(request.actors, known, settings.actor_aliases)
        if errors:
            return {"outcome": "invalid_input", "input_errors": [e.model_dump() for e in errors]}
        questions = clarification_needed(request)
        if questions:
            return {"outcome": "needs_clarification", "clarifications": [q.model_dump() for q in questions]}
        return {"outcome": None, "task": normalise_task(request), "actors": actors,
                "search_mode": schema["search_mode"]}

    async def release_question(state):
        started = state.get("deliberation_started_at") or clock.time()
        emit({**state, "round": 0}, "regelwerk", "meta", "deliberation", "public", "rulebook_released",
             payload={"rulebook_version": state["task"]["rulebook_version"]})
        warnings = {}
        for text in settings.deviations():
            warnings.update(warning_update(text))
        for name in llm.scheduler.missing_limits:
            warnings.update(warning_update(f"{name} ist nicht gesetzt: dieses Limit ist unbekannt, nicht unbegrenzt."))
        warnings.update(warning_update(
            f"Suchmodus {state['search_mode']}: lexikalische Suche findet nur wörtliche Treffer. Fehlende "
            "Treffer beweisen nicht, dass keine Belege existieren."))
        return {"deliberation_started_at": started,
                "deliberation_deadline_at": state.get("deliberation_deadline_at")
                or started + settings.deliberation_timeout,
                "round": 0, "baseline_version": 1, "proposal_version": 0, "amendments_in_round": 0,
                "success_streak": 0, "completed_rounds": 0, "last_complete_key": None, "frozen_key": None,
                "stop_reason": None, "blockage": None, "warnings": warnings}

    # ---- steps 4-5 ---------------------------------------------------------

    async def prepare_actor(state):
        actor = state["item"]["actor"]
        aid = actor["actor_id"]
        role = Role(ACTOR, aid)
        half = max(retrieval.get("actor_documents", 8) // 2, 1)
        question = state["task"]["question"]
        profile_docs, up1 = await fetch(state, role, f"{question} {actor['name']}", half, "ActorData")
        articles, up2 = await fetch(state, role, question, half, "Article")
        passages = profile_docs + articles
        allowed = {p["passage_id"] for p in passages}
        assumptions = assumption_ids(state)

        def validate(out):
            return (rules.validate_profile(out.profile, allowed)
                    + rules.validate_priorities(out.priorities, allowed)
                    + rules.validate_claims(out.argument, allowed, assumptions))

        out, up3 = await ask(state, f"prepare:{aid}", "actor", "preparation", prompts.instructions("actor", actor["name"]),
                             prompts.initial_prompt(state["task"], actor["name"], passages), lm.InitialOutput,
                             validate, settings.initial_max_output)
        if out is None:
            emit(state, f"prepare:{aid}", aid, "preparation", "private", "contribution_incomplete")
            return combine(up1, up2, up3)
        arg = argument_record(aid, 1, "initial", 0, out.argument, None)
        emit(state, arg["argument_id"], aid, "preparation", "private", "argument_submitted", [arg["argument_id"]],
             payload=arg)
        return combine(up1, up2, up3, {
            "profiles": {aid: profile_record(out.profile)}, "arguments": {arg["argument_id"]: arg},
            "priorities": priority_records(state, aid, out.priorities, [])})

    async def prepare_barrier(state):
        emit(state, "barriere:vorbereitung", "controller", "preparation", "internal", "barrier_reached")
        return {"stage": "initial"}

    # ---- steps 6-9, 13, 15: review, challenge, reply -----------------------

    def review_items(stage, state):
        round_no = state.get("round") or 0
        version = state.get("proposal_version") or 0
        if stage in ("initial", "second", "assessment"):
            return [{"stage": stage, "reviewer": r, "actor_id": a["actor_id"], "batch": 0, "round": round_no,
                     "version": version} for a in state["actors"] for r in REVIEWERS]
        key = current_key(state) if stage == "candidate" else state.get("frozen_key")
        proposal = (state.get("proposals") or {}).get(key)
        count = len(proposal["clauses"]) if proposal else 0
        batches = max((count + CLAUSE_BATCH - 1) // CLAUSE_BATCH, 1)
        return [{"stage": stage, "reviewer": r, "actor_id": None, "batch": b, "round": round_no,
                 "version": proposal["version"] if proposal else 0, "key": key}
                for b in range(batches) for r in REVIEWERS]

    def review_id(item):
        who = item["actor_id"] or "alle"
        return f"R-{item['stage']}-{item['round']}-v{item['version']}-{item['reviewer']}-{who}-{item['batch']}"

    def subject(state, item):
        """(content text, target ids) of a review; a program-built view of published data."""
        stage, aid = item["stage"], item["actor_id"]
        if stage in ("initial", "second"):
            profile = state.get("profiles", {}).get(aid)
            priorities = current_priorities(state, aid)
            arg = latest_argument(state, aid)
            targets = [p["priority_id"] for p in priorities]
            if profile:
                targets.append(f"PROFIL-{aid}")
            if arg:
                targets += [arg["argument_id"]] + [c["claim_id"] for c in arg["claims"]]
            return prompts.contribution_text(profile, priorities, arg), targets
        if stage == "assessment":
            assessments = [a for a in state.get("assessments", {}).values()
                           if a["actor_id"] == aid and a["round"] == item["round"]
                           and a["proposal_version"] == item["version"]]
            decisions = [d for d in state.get("decisions", {}).values()
                         if d["actor_id"] == aid and d["round"] == item["round"]]
            name = next(a["name"] for a in state["actors"] if a["actor_id"] == aid)
            targets = [a["assessment_id"] for a in assessments] + [d["decision_id"] for d in decisions]
            return prompts.assessment_review_content(name, assessments, decisions), targets
        proposal = state.get("proposals", {}).get(item.get("key"))
        if not proposal:
            return prompts.dumps(state.get("final_summaries")), ["KEIN-VORSCHLAG"]
        clauses = proposal["clauses"][item["batch"] * CLAUSE_BATCH:(item["batch"] + 1) * CLAUSE_BATCH]
        return prompts.dumps({"vorschlag": item["key"], "klauseln": clauses}), [c["clause_id"] for c in clauses]

    async def reviewer_evidence(state, reviewer, content):
        role = Role(reviewer)
        documents = retrieval.get("reviewer_documents", 6)
        query = f"{state['task']['question']} {searchable_text(content)}"
        if reviewer == TRUTHSEEKER:
            shown, updates = [], {"warnings": {}}
            for pid in sorted(set(PASSAGE_ID.findall(content)))[:12]:
                try:
                    passage, same = await sources.citation_context(state["debate_id"], pid, state.get("passages", {}))
                except (SourceError, PermissionDeniedError) as exc:
                    updates["warnings"].update(warning_update(f"Beleg {pid}: {exc.message}"))
                    continue
                if not same:
                    updates["warnings"].update(warning_update(
                        f"Beleg {pid}: Die Quelle hat sich seit dem Abruf geändert; geprüft wird die gesicherte Fassung."))
                shown.append({**passage, "text": f"{passage['context_before']} [[{passage['text']}]] "
                                                 f"{passage['context_after']}".strip()})
            more, extra = await fetch(state, role, query, max(documents // 3, 1))
            grants = {f"{role.key}|{p['passage_id']}": True for p in shown}
            return shown + more, combine(updates, extra, {"grants": grants})
        return await fetch(state, role, query, documents)

    async def review_node(state):
        item = state["item"]
        reviewer, rid = item["reviewer"], review_id(item)
        content, targets = subject(state, item)
        passages, up1 = await reviewer_evidence(state, reviewer, content)
        allowed = {p["passage_id"] for p in passages} | set(state.get("passages") or {})
        framework = None
        if reviewer == ETHICMIND:
            titles = sorted({p["title"] for p in passages})
            framework = "; ".join(titles[:4]) if titles else "Kein Ethiktext gefunden; Rahmen nicht belegbar."
        earlier = None
        if item["stage"] == "assessment":
            earlier = [{k: f[k] for k in ("finding_id", "assessment", "reason")}
                       for r in stage_reviews(state, "candidate", item["round"])
                       if r["reviewer"] == reviewer for f in r["findings"]][:8]
        prompt = prompts.review_prompt(state["task"], f"Rolle={reviewer}, Stufe={item['stage']}", content, targets,
                                       passages, earlier, framework)

        def validate(out):
            problems = rules.validate_review(out, reviewer, set(targets), allowed)
            if reviewer == LEGALEYE and item["stage"] in ("candidate", "final"):
                covered = {f.target_id for f in out.findings} | set(out.unreviewed_targets)
                problems += [f"Klausel {t} braucht einen Befund oder einen Eintrag in unreviewed_targets."
                             for t in targets if t not in covered]
            return problems

        phase = "finalization" if item["stage"] == "final" else "review"
        out, up2 = await ask(state, rid, reviewer, phase, prompts.instructions(reviewer), prompt,
                             lm.REVIEW_MODELS[reviewer], validate, settings.max_output)
        if out is None:
            record = {"review_id": rid, "reviewer": reviewer, "stage": item["stage"], "round": item["round"],
                      "actor_id": item["actor_id"], "findings": [], "coverage": "incomplete",
                      "unreviewed_targets": targets,
                      "coverage_note": "Die Prüfung konnte nicht abgeschlossen werden.", "framework": framework,
                      "word_count": 0, "status": "incomplete", "version": item["version"], "targets": targets}
            return combine(up1, up2, {"reviews": {rid: record}})
        findings = [{"finding_id": f"F-{rid[2:]}-{i}", "reviewer": reviewer, **f.model_dump()}
                    for i, f in enumerate(out.findings, 1)]
        record = {"review_id": rid, "reviewer": reviewer, "stage": item["stage"], "round": item["round"],
                  "actor_id": item["actor_id"], "findings": findings, "coverage": out.coverage,
                  "unreviewed_targets": out.unreviewed_targets, "coverage_note": out.coverage_note,
                  "framework": framework,
                  "word_count": rules.count_words(rules.render_review(out)), "status": "complete",
                  "version": item["version"], "targets": targets}
        emit(state, rid, reviewer, phase, "private" if item["actor_id"] else "public", "review_submitted",
             [f["finding_id"] for f in findings], payload=record)
        return combine(up1, up2, {"reviews": {rid: record}})

    def apply_legal_status(state):
        """Clause legal status comes from LegalEye findings; a withdrawn finding does not count."""
        key = current_key(state)
        proposal = state.get("proposals", {}).get(key)
        if not proposal:
            return {}
        withdrawn = withdrawn_findings(state)
        reviews = [r for r in stage_reviews(state, "candidate", state["round"])
                   if r.get("version") == proposal["version"]]
        all_legal = [f for r in reviews if r["reviewer"] == LEGALEYE for f in r["findings"]]
        legal = [f for f in all_legal if f["finding_id"] not in withdrawn]
        clauses = []
        for clause in proposal["clauses"]:
            status = rules.worst_legal([f["assessment"] for f in legal if f["target_id"] == clause["clause_id"]])
            if status == "not_reviewed" and any(f["target_id"] == clause["clause_id"] for f in all_legal):
                status = "no_conflict_identified"     # reviewed; the only finding was withdrawn
            if clause["law_amendment_detected"]:
                status = "requires_legal_amendment"
            clauses.append({**clause, "legal_status": status})
        statuses = [c["legal_status"] for c in clauses]
        complete = bool(reviews) and all(r["status"] == "complete" and r["coverage"] == "complete" for r in reviews) \
            and len(reviews) == len(review_items("candidate", state))
        return {"proposals": {key: {**proposal, "clauses": clauses, "legal_admissibility": rules.admissibility(statuses),
                                    "review_complete": complete}}}

    def make_review_barrier(stage):
        async def barrier(state):
            emit(state, f"barriere:{stage}:{state.get('round') or 0}:{state.get('proposal_version') or 0}",
                 "controller", "review", "internal", "barrier_reached")
            return combine({"stage": stage}, apply_legal_status(state) if stage == "candidate" else {})
        return barrier

    def challengeable(state, stage, actor_id):
        round_no = state.get("round") or 0
        used = {c["finding_id"] for c in (state.get("challenges") or {}).values()}
        version = state.get("proposal_version") or 0
        out = []
        for review in stage_reviews(state, stage, round_no):
            if stage == "candidate" and review.get("version") != version:
                continue
            if stage != "candidate" and review["actor_id"] != actor_id:
                continue
            out += [f for f in review["findings"] if material(f) and f["finding_id"] not in used]
        return out

    async def challenge_node(state):
        item = state["item"]
        aid, stage = item["actor_id"], item["stage"]
        actor = next(a for a in state["actors"] if a["actor_id"] == aid)
        findings = challengeable(state, stage, aid)
        allowed = {f["finding_id"] for f in findings}
        task_id = f"challenge:{stage}:{state['round']}:v{state.get('proposal_version') or 0}:{aid}"

        def validate(out):
            problems = []
            ids = [c.finding_id for c in out.challenges]
            if len(ids) != len(set(ids)):
                problems.append("Je Befund ist nur ein Widerspruch zulässig.")
            for c in out.challenges:
                if c.finding_id not in allowed:
                    problems.append(f"Befund {c.finding_id} ist nicht anfechtbar.")
                problems += rules.word_problem(c.reason, "ordinary", "Widerspruch")
            return problems

        out, updates = await ask(state, task_id, "actor", "challenge", prompts.instructions("actor", actor["name"]),
                                 prompts.challenge_prompt(state["task"], findings), lm.ChallengesOutput, validate,
                                 settings.max_output)
        records = {}
        for c in (out.challenges if out else []):
            cid = f"CH-{c.finding_id}"
            records[cid] = {"challenge_id": cid, "finding_id": c.finding_id, "actor_id": aid, "stage": stage,
                            "round": state["round"], "reason": c.reason, "status": "pending",
                            "reply_decision": None, "reply_reason": None}
            emit(state, cid, aid, "challenge", "private", "challenge_submitted", [cid], [c.finding_id], records[cid])
        return combine(updates, {"challenges": records})

    def pending(state, stage):
        return [c for c in (state.get("challenges") or {}).values()
                if c["stage"] == stage and c["round"] == (state.get("round") or 0) and c["status"] == "pending"]

    def finding_by_id(state, finding_id):
        for review in (state.get("reviews") or {}).values():
            for finding in review["findings"]:
                if finding["finding_id"] == finding_id:
                    return finding
        return None

    async def reply_node(state):
        item = state["item"]
        reviewer, stage = item["reviewer"], item["stage"]
        mine = [c for c in pending(state, stage) if finding_by_id(state, c["finding_id"])["reviewer"] == reviewer]
        pairs = [(c, finding_by_id(state, c["finding_id"])) for c in mine]
        allowed = {c["challenge_id"] for c in mine}
        task_id = f"reply:{stage}:{state['round']}:v{state.get('proposal_version') or 0}:{reviewer}"

        def validate(out):
            problems = []
            answered = {r.challenge_id for r in out.replies}
            if answered != allowed:
                problems.append("Jeder Widerspruch braucht genau eine Antwort.")
            for r in out.replies:
                problems += rules.word_problem(r.reason, "ordinary", "Antwort")
            return problems

        out, updates = await ask(state, task_id, reviewer, "challenge", prompts.instructions(reviewer),
                                 prompts.reply_prompt(state["task"], pairs), lm.RepliesOutput, validate,
                                 settings.max_output)
        records = {}
        for reply in (out.replies if out else []):
            old = state["challenges"][reply.challenge_id]
            records[reply.challenge_id] = {**old, "status": "answered", "reply_decision": reply.decision,
                                           "reply_reason": reply.reason}
            emit(state, f"antwort:{reply.challenge_id}", reviewer, "challenge", "private", "challenge_answered",
                 [reply.challenge_id], payload=records[reply.challenge_id])
        return combine(updates, {"challenges": records})

    def make_stage_barrier(stage, name):
        async def barrier(state):
            emit(state, f"barriere:{name}:{stage}:{state.get('round') or 0}:{state.get('proposal_version') or 0}",
                 "controller", "challenge", "internal", "barrier_reached")
            return combine({"stage": stage}, apply_legal_status(state) if stage == "candidate" else {})
        return barrier

    # ---- steps 8, 10 -------------------------------------------------------

    async def revise_actor(state):
        actor = state["item"]["actor"]
        aid = actor["actor_id"]
        role = Role(ACTOR, aid)
        arg = latest_argument(state, aid)
        priorities = current_priorities(state, aid)
        if arg is None:
            return {"incomplete": {f"revise:{aid}": {"role": "actor", "phase": "revision",
                                                     "reason": "Kein Erstbeitrag vorhanden."}}}
        allowed = granted(state, role)
        passages = [p for p in (events.passage(state["debate_id"], i) for i in sorted(allowed)) if p]
        findings = [f for r in stage_reviews(state, "initial", 0) if r["actor_id"] == aid for f in r["findings"]]
        challenges = [c for c in state.get("challenges", {}).values() if c["actor_id"] == aid and c["stage"] == "initial"]
        assumptions = assumption_ids(state)

        def validate(out):
            problems = rules.validate_claims(out.argument, allowed, assumptions)
            problems += rules.word_problem(out.change_explanation, "ordinary", "Änderungsbegründung")
            if out.profile:
                problems += rules.validate_profile(out.profile, allowed)
            if out.priorities is not None:
                problems += rules.validate_priorities(out.priorities, allowed, [p["priority_id"] for p in priorities])
            return problems

        out, updates = await ask(state, f"revise:{aid}", "actor", "revision", prompts.instructions("actor", actor["name"]),
                                 prompts.revision_prompt(state["task"], state["profiles"].get(aid), priorities, arg,
                                                         findings, challenges, passages),
                                 lm.RevisionOutput, validate, settings.initial_max_output)
        if out is None:
            return updates
        record = argument_record(aid, 2, "second", 0, out.argument, out.change_explanation)
        emit(state, record["argument_id"], aid, "revision", "private", "argument_revised", [record["argument_id"]],
             [arg["argument_id"]], record)
        extra = {"arguments": {record["argument_id"]: record}}
        if out.profile:
            extra["profiles"] = {aid: profile_record(out.profile)}
        if out.priorities is not None:
            extra["priorities"] = priority_records(state, aid, out.priorities, priorities)
        return combine(updates, extra)

    async def revision_barrier(state):
        emit(state, "barriere:revision", "controller", "revision", "internal", "barrier_reached")
        return {"stage": "second"}

    async def publish_package(state):
        """Same package for all actors; the priority baseline is frozen here."""
        package, reduced = published_package(state, settings.input_budget // 2)
        refs = [p["priority_id"] for a in state["actors"] for p in current_priorities(state, a["actor_id"])]
        emit({**state, "round": 1}, "publikation", "controller", "publication", "public", "package_published", refs,
             payload={"baseline_version": 1})
        warnings = {}
        if reduced:
            warnings.update(warning_update("Das veröffentlichte Paket wurde auf aktuelle Fassungen gekürzt."))
        for actor in state["actors"]:
            if not current_priorities(state, actor["actor_id"]):
                warnings.update(warning_update(f"Für {actor['name']} liegt keine Prioritätenliste vor; "
                                               "Zufriedenheit bleibt null."))
        return {"round": 1, "baseline_version": 1, "warnings": warnings, "stage": "published"}

    # ---- steps 11-16 -------------------------------------------------------

    async def negotiate_actor(state):
        actor = state["item"]["actor"]
        aid = actor["actor_id"]
        round_no = state["round"]
        role = Role(ACTOR, aid)
        allowed = granted(state, role)
        passages = [p for p in (events.passage(state["debate_id"], i) for i in sorted(allowed)[:8]) if p]
        package, _ = published_package(state, settings.input_budget // 2)
        own = {p["priority_id"] for p in current_priorities(state, aid)}
        all_priorities = {p["priority_id"] for a in state["actors"] for p in current_priorities(state, a["actor_id"])}

        def validate(out):
            text = " ".join(f"{i.kind} {i.text} {' '.join(i.quellen)}" for i in out.items)
            problems = rules.word_problem(text, "ordinary", "Verhandlungsbeitrag")
            if not out.items:
                problems.append("Der Verhandlungsbeitrag braucht mindestens einen Eintrag.")
            for i in out.items:
                problems += rules.check_citations(i.quellen, allowed, assumption_ids(state), "Verhandlung")
                problems += [f"Unbekannte Priorität {p}." for p in i.priority_ids if p not in all_priorities]
                if i.kind != "disagreement" and rules.mentions_law_change(i.text):
                    problems.append("Rechtsänderungen sind als Lösung ausgeschlossen.")
            for c in out.corrections:
                if c.priority_id not in own:
                    problems.append(f"Korrektur nur für eigene Prioritäten: {c.priority_id}.")
                if not c.justification.strip():
                    problems.append("Eine Korrektur braucht eine Begründung.")
                problems += rules.check_citations(c.quellen, allowed, (), "Korrektur")
            return problems

        task_id = f"negotiate:{round_no}:{aid}"
        out, updates = await ask(state, task_id, "actor", "negotiation", prompts.instructions("actor", actor["name"]),
                                 prompts.negotiation_prompt(state["task"], actor_view(state, deps, actor), package,
                                                            passages, state.get("blockage"), priority_gaps(state)),
                                 lm.NegotiationOutput, validate, settings.max_output)
        if out is None:
            return updates
        records = {}
        for i, entry in enumerate(out.items, 1):
            item_id = f"NEG-{round_no}-{aid}-{i}"
            records[item_id] = {"item_id": item_id, "actor_id": aid, "round": round_no, **entry.model_dump()}
            emit(state, item_id, aid, "negotiation", "public", "negotiation_item", [item_id], payload=records[item_id])
        corrections = {f"KORR-{round_no}-{aid}-{i}": {**c.model_dump(), "actor_id": aid, "round": round_no}
                       for i, c in enumerate(out.corrections, 1)}
        return combine(updates, {"negotiation": records, "corrections": corrections})

    async def negotiation_barrier(state):
        """Apply justified definition corrections: new baseline version, success streak reset."""
        round_no = state["round"]
        baseline = state["baseline_version"]
        streak = state.get("success_streak") or 0
        applied, used, warnings = {}, {}, {}
        for key, correction in sorted((state.get("corrections") or {}).items()):
            if correction["round"] != round_no:
                used[correction["actor_id"]] = used.get(correction["actor_id"], 0) + 1
        for key, correction in sorted((state.get("corrections") or {}).items()):
            if correction["round"] != round_no:
                continue
            aid = correction["actor_id"]
            if used.get(aid, 0) >= limits.get("max_priority_corrections_per_actor", 1):
                warnings.update(warning_update(f"Prioritätskorrektur {key} abgelehnt: Limit erreicht."))
                continue
            used[aid] = used.get(aid, 0) + 1
            old = next(p for p in current_priorities(state, aid) if p["priority_id"] == correction["priority_id"])
            baseline += 1
            record = {**old, "baseline_version": baseline, "acceptance_condition": correction["acceptance_condition"],
                      "quellen": old["quellen"] + correction["quellen"]}
            applied[f"{old['priority_id']}@{baseline}"] = record
            emit(state, key, aid, "negotiation", "public", "priority_baseline_corrected", [old["priority_id"]],
                 payload={"justification": correction["justification"], "baseline_version": baseline})
            streak = 0
        return {"priorities": applied, "baseline_version": baseline, "success_streak": streak,
                "warnings": warnings, "amendments_in_round": 0, "stage": "negotiated"}

    async def build_candidate(state, amend):
        round_no = state["round"]
        version = (state.get("proposal_version") or 0) + 1
        key = f"{PROPOSAL_ID}-v{version}"
        items = [n for n in state.get("negotiation", {}).values() if n["round"] >= round_no - 1]
        previous = public_proposal(state)
        previous_full = state.get("proposals", {}).get(current_key(state))
        withdrawn = withdrawn_findings(state)
        open_findings = [{k: f[k] for k in ("finding_id", "target_id", "assessment", "reason", "correction")}
                         for r in stage_reviews(state, "candidate", round_no) for f in r["findings"]
                         if material(f) and f["finding_id"] not in withdrawn]
        needs_change = [c for c in (previous_full["clauses"] if previous_full else [])
                        if c["legal_status"] in ("requires_legal_amendment", "apparent_legal_conflict")] if amend else []
        constraint_ids = [c["id"] for c in state["task"]["constraints"]]
        allowed_premises = set(state.get("passages") or {}) | set(assumption_ids(state))
        provenance = set(state.get("negotiation") or {}) | {a for a in state.get("arguments", {})}
        for proposal in state.get("proposals", {}).values():
            provenance |= {c["clause_id"] for c in proposal["clauses"]}

        existing = {c["clause_id"]: c for c in (previous_full["clauses"] if previous_full else [])}

        def resolved(clause):
            """A kept clause is copied word for word from the current version by the program."""
            source = existing.get(clause.unchanged_from)
            if source is None:
                return clause.text, clause.provenance, clause.premises
            return source["text"], [source["clause_id"]], source["premises"]

        def validate(out):
            problems = rules.word_problem(out.meta_update, "ordinary", "Meta-Update")
            if not out.clauses:
                problems.append("Der Vorschlag braucht mindestens eine Klausel.")
            if len(out.clauses) > limits.get("max_clauses", 8):
                problems.append(f"Höchstens {limits.get('max_clauses', 8)} Klauseln sind zulässig.")
            for i, c in enumerate(out.clauses, 1):
                if c.unchanged_from and c.unchanged_from not in existing:
                    problems.append(f"Klausel {i}: {c.unchanged_from} gehört nicht zur aktuellen Fassung; "
                                    f"erlaubt sind: {', '.join(existing) or 'keine'}.")
                    continue
                text, origin, premises = resolved(c)
                if not text.strip():
                    problems.append(f"Klausel {i}: Text fehlt.")
                problems += rules.word_problem(text, "ordinary", f"Klausel {i}")
                problems += rules.check_citations(premises, allowed_premises, (), f"Klausel {i}")
                problems += [f"Klausel {i}: Herkunft {p} ist nicht aufgelöst." for p in origin if p not in provenance]
                if not origin:
                    problems.append(f"Klausel {i}: Herkunft fehlt.")
                if rules.mentions_law_change(text):
                    problems.append(f"Klausel {i}: Rechtsänderungen sind als Lösung ausgeschlossen.")
            kept = [c.unchanged_from for c in out.clauses if c.unchanged_from]
            if len(kept) != len(set(kept)):
                problems.append("Eine Klausel darf nur einmal übernommen werden.")
            checked = [c.constraint_id for c in out.constraint_checks if c.constraint_id in constraint_ids]
            if sorted(checked) != sorted(constraint_ids):
                problems.append("constraint_checks muss jede Beschränkung genau einmal bewerten.")
            return problems

        prompt = prompts.candidate_prompt(
            state["task"], round_no, items, previous, open_findings, [c["clause_id"] for c in needs_change],
            state.get("blockage"), constraint_ids, limits.get("max_clauses", 8), sorted(allowed_premises),
            priority_gaps(state))
        out, updates = await ask(state, f"candidate:{round_no}:v{version}", "meta", "candidate", prompts.instructions("meta"),
                                 prompt, lm.CandidateOutput, validate, settings.initial_max_output)
        if out is None:
            return combine(updates, {"amendments_in_round": (state.get("amendments_in_round") or 0) + (1 if amend else 0)})
        clauses = []
        for i, clause in enumerate(out.clauses, 1):
            text, origin, premises = resolved(clause)
            clauses.append({"clause_id": f"KL-v{version}-{i}", "text": text, "provenance": origin, "premises": premises,
                            "legal_status": "not_reviewed", "law_amendment_detected": rules.mentions_law_change(text)})
        record = {"proposal_id": PROPOSAL_ID, "version": version, "round": round_no, "created_at": clock.time(),
                  "clauses": clauses, "meta_update": out.meta_update,
                  "constraint_checks": [c.model_dump() for c in out.constraint_checks
                                         if c.constraint_id in constraint_ids],     # invented ids are ignored
                  "legal_admissibility": "not_reviewed", "review_complete": False, "status": "current"}
        emit(state, key, "meta", "candidate", "public", "proposal_created", [key] + [c["clause_id"] for c in clauses],
             [current_key(state)] if current_key(state) else [], record)
        return combine(updates, {"proposals": {key: record}, "proposal_version": version,
                                 "amendments_in_round": (state.get("amendments_in_round") or 0) + (1 if amend else 0)})

    async def assemble_candidate(state):
        return await build_candidate(state, False)

    async def amend_candidate(state):
        return await build_candidate(state, True)

    async def assess_actor(state):
        actor = state["item"]["actor"]
        aid, round_no, version = actor["actor_id"], state["round"], state["proposal_version"]
        key = current_key(state)
        proposal = state["proposals"][key]
        priorities = current_priorities(state, aid)
        ids = [p["priority_id"] for p in priorities]
        clause_ids = [c["clause_id"] for c in proposal["clauses"]]
        view = actor_view(state, deps, actor)

        def validate(out):
            problems = rules.validate_assessments(out.assessments, ids, clause_ids)
            problems += rules.word_problem(out.decision_reason + " " + " ".join(out.conditions), "ordinary", "Entscheidung")
            if out.decision == "conditional" and not out.conditions:
                problems.append("Eine bedingte Annahme braucht Bedingungen.")
            return problems

        out, updates = await ask(state, f"assess:{round_no}:v{version}:{aid}", "actor", "assessment",
                                 prompts.instructions("actor", actor["name"]),
                                 prompts.assessment_prompt(state["task"], view, clause_ids), lm.AssessOutput, validate,
                                 settings.max_output)
        if out is None:
            return updates
        return combine(updates, assessment_records(state, aid, out, round_no, key, proposal, "assessment"))

    def assessment_records(state, aid, out, round_no, key, proposal, phase):
        previous = {}
        for a in sorted(state.get("assessments", {}).values(), key=lambda x: (x["round"], x["proposal_version"])):
            if a["actor_id"] == aid:
                previous[a["priority_id"]] = a["status"]
        version = proposal["version"]
        priorities = {p["priority_id"]: p for p in current_priorities(state, aid)}
        records, tag = {}, "final" if phase == "finalization" else str(round_no)
        for item in out.assessments:
            aid_key = f"AS-{tag}-{item.priority_id}-v{version}"
            records[aid_key] = {"assessment_id": aid_key, "actor_id": aid, "priority_id": item.priority_id,
                                "baseline_version": priorities[item.priority_id]["baseline_version"],
                                "round": round_no, "proposal_id": PROPOSAL_ID, "proposal_version": version,
                                "status": item.status, "previous_status": previous.get(item.priority_id),
                                "reason": item.reason, "references": item.references, "reviewer_objections": []}
            emit(state, aid_key, aid, phase, "public", "priority_assessed", [aid_key], [key], records[aid_key])
        decision_id = f"D-{tag}-{aid}-v{version}"
        decision = {"decision_id": decision_id, "actor_id": aid, "target_id": key, "target_version": version,
                    "round": round_no, "at": clock.time(), "kind": out.decision, "conditions": out.conditions,
                    "reason": out.decision_reason}
        emit(state, decision_id, aid, phase, "public", "decision_recorded", [decision_id], [key], decision)
        return {"assessments": records, "decisions": {decision_id: decision}}

    async def assess_barrier(state):
        emit(state, f"barriere:bewertung:{state['round']}:{state.get('proposal_version') or 0}", "controller",
             "assessment", "internal", "barrier_reached")
        return {"stage": "assessed"}

    async def close_round(state):
        """Deterministic: validate the round, compute satisfaction, update the streak."""
        round_no = state["round"]
        key = current_key(state)
        proposal = state.get("proposals", {}).get(key)
        version = state.get("proposal_version") or 0
        current_candidate = proposal is not None and proposal["round"] == round_no
        complete = current_candidate
        assessments = {k: a for k, a in state.get("assessments", {}).items()
                       if a["round"] == round_no and a["proposal_version"] == version}
        reviews = stage_reviews(state, "assessment", round_no)
        expected = {review_id(i) for i in review_items("assessment", state)}
        if {r["review_id"] for r in reviews} >= expected:
            done = [r for r in reviews if r["review_id"] in expected]
        else:
            done, complete = [], False
        if any(r["status"] != "complete" or r["coverage"] != "complete" for r in done):
            complete = False
        if not (proposal and proposal["review_complete"]):
            complete = False
        withdrawn = withdrawn_findings(state)
        objections = {}
        for review in done:
            for f in review["findings"]:
                if material(f) and f["finding_id"] not in withdrawn:
                    objections.setdefault(f["target_id"], []).append(f["finding_id"])
        updated, per_actor = {}, []
        assessed = current_candidate        # every actor assessed every priority of this version
        for actor in state["actors"]:
            aid = actor["actor_id"]
            ids = [p["priority_id"] for p in current_priorities(state, aid)]
            mine = {a["priority_id"]: a for a in assessments.values() if a["actor_id"] == aid}
            if not ids or set(mine) != set(ids):
                complete = assessed = False
            disputed = False
            for a in mine.values():
                found = objections.get(a["assessment_id"], [])
                disputed = disputed or bool(found)
                updated[a["assessment_id"]] = {**a, "reviewer_objections": found}
            per_actor.append(rules.satisfaction(aid, [mine[i]["status"] if i in mine else "unclear" for i in ids],
                                                disputed))
        streak = rules.next_streak(state.get("success_streak") or 0, complete, per_actor)
        completed = (state.get("completed_rounds") or 0) + (1 if complete else 0)
        stop = "satisfaction_threshold" if streak >= 2 else None
        emit(state, f"runde:{round_no}", "controller", "assessment", "public", "round_closed", [key] if key else [],
             payload={"complete": complete, "assessed": assessed, "streak": streak, "satisfaction": per_actor,
                      "unfinished_reviews": [r["review_id"] for r in done
                                             if r["status"] != "complete" or r["coverage"] != "complete"]})
        fingerprint = hashlib.sha256(prompts.dumps(
            [c["text"] for c in proposal["clauses"]] if proposal else []).encode("utf-8")).hexdigest()[:12]
        rounds = state.get("rounds") or {}
        unchanged = bool(rounds) and rounds.get(str(round_no - 1), {}).get("fingerprint") == fingerprint
        blockage = ("Positionen und Vorschlag blieben über zwei Runden unverändert. Bitte nennt neue Belege, "
                    "Zugeständnisse oder begründet die fortbestehende Differenz.") if unchanged else None
        return {"assessments": updated, "success_streak": streak, "completed_rounds": completed,
                "last_complete_key": key if assessed else state.get("last_complete_key"),
                "stop_reason": stop, "blockage": blockage,
                "round": round_no if stop else round_no + 1,
                "rounds": {str(round_no): {"complete": complete, "assessed": assessed, "proposal": key,
                                           "satisfaction": per_actor,
                                           "streak": streak, "fingerprint": fingerprint}},
                "stage": "round_closed"}

    # ---- steps 17-21 -------------------------------------------------------

    async def freeze(state):
        now = clock.time()
        started = state.get("finalization_started_at") or now
        frozen = state.get("last_complete_key")
        warnings, proposals = {}, {}
        for key, proposal in (state.get("proposals") or {}).items():
            status = "frozen" if key == frozen else "superseded"
            proposals[key] = {**proposal, "status": status}
        newest = current_key(state)
        if newest and newest != frozen:
            warnings.update(warning_update(f"Die neuere Fassung {newest} blieb unvollständig und wird nur im "
                                           "Verlauf geführt."))
        if frozen is None:
            warnings.update(warning_update("Es liegt kein vollständiger Vorschlag vor; es wird keine Einigung ausgewiesen."))
        stop = state.get("stop_reason") or ("operator_stop" if stop_requested(state) else "deliberation_deadline")
        emit(state, "einfrieren", "controller", "freeze", "public", "candidate_frozen", [frozen] if frozen else [],
             payload={"stop_reason": stop})
        return {"stop_reason": stop, "frozen_key": frozen, "proposals": proposals, "warnings": warnings,
                "deliberation_ended_at": state.get("deliberation_ended_at") or now,
                "finalization_started_at": started,
                "finalization_deadline_at": state.get("finalization_deadline_at") or started + settings.finalization_timeout,
                "stage": "frozen"}

    async def final_actor(state):
        actor = state["item"]["actor"]
        aid = actor["actor_id"]
        key = state.get("frozen_key")
        proposal = state.get("proposals", {}).get(key)
        ids = [p["priority_id"] for p in current_priorities(state, aid)] if proposal else []
        clause_ids = [c["clause_id"] for c in proposal["clauses"]] if proposal else []
        view = {**actor_view(state, deps, actor), "proposal": public_proposal(state, key)}

        def validate(out):
            return rules.validate_final(out, ids, clause_ids)

        out, updates = await ask(state, f"final:{aid}", "actor", "finalization", prompts.instructions("actor", actor["name"]),
                                 prompts.final_prompt(state["task"], view, clause_ids, proposal is not None),
                                 lm.FinalOutput, validate, settings.final_max_output)
        if out is None:
            return updates
        extra = {"final_summaries": {aid: {"summary": out.summary, "word_count": rules.count_words(out.summary)}}}
        if proposal:
            extra = combine(extra, assessment_records(state, aid, out, state["round"], key, proposal, "finalization"))
        emit(state, f"abschluss:{aid}", aid, "finalization", "public", "final_summary", payload={"summary": out.summary})
        return combine(updates, extra)

    async def final_actor_barrier(state):
        return {"stage": "final_actors"}

    async def final_review_barrier(state):
        return {"stage": "final_reviews"}

    async def meta_report(state):
        known = known_references(state)
        record = debate_record(state, settings.input_budget // 2)

        def validate(out):
            return rules.validate_report(out, known)

        out, updates = await ask(state, "bericht", "meta", "finalization", prompts.instructions("meta"),
                                 prompts.report_prompt(state["task"], record, sorted(known)), lm.ReportOutput, validate,
                                 settings.final_max_output, final=True)
        return combine(updates, {"report": {"main": out.model_dump()} if out else {}})

    async def meta_evolution(state):
        known = known_references(state)
        record = debate_record(state, settings.input_budget // 2)

        def validate(out):
            return rules.validate_evolution(out, known)

        out, updates = await ask(state, "verlauf", "meta", "finalization", prompts.instructions("meta"),
                                 prompts.evolution_prompt(state["task"], record, sorted(known)), lm.EvolutionOutput,
                                 validate, settings.final_max_output, final=True)
        return combine(updates, {"report": {"evolution": out.model_dump()} if out else {}})

    async def report_barrier(state):
        return {"stage": "reported"}

    async def build_output(state):
        ended = state.get("finalization_ended_at") or clock.time()
        final_state = {**state, "finalization_ended_at": ended if in_finalization(state) else None}
        result = build_result(final_state, deps)
        return {"result": result, "finalization_ended_at": final_state["finalization_ended_at"]}

    # ---- routing -----------------------------------------------------------

    def guarded(state, target):
        if state.get("fatal"):
            return "build_output"
        if deadlines.expired(state["deliberation_deadline_at"]) or stop_requested(state):
            return "freeze"
        return target(state)

    def final_guarded(state, target):
        if deadlines.expired(state["finalization_deadline_at"]):
            return "build_output"
        return target(state)

    def sends(node, state, items):
        return [Send(node, {**state, "item": item}) for item in items]

    def to_actors(node, state):
        return sends(node, state, [{"actor": a} for a in state["actors"]])

    def to_reviews(stage):
        return lambda state: sends(f"{stage}_review", state, review_items(stage, state))

    def next_after(stage, state):
        if stage == "initial":
            return to_actors("revise_actor", state)
        if stage == "second":
            return "publish_package"
        if stage == "candidate":
            proposal = state.get("proposals", {}).get(current_key(state))
            blocked = proposal and proposal["legal_admissibility"] in ("inadmissible",)
            if blocked and (state.get("amendments_in_round") or 0) < 1:
                return "amend_candidate"
            if not proposal or proposal["round"] != state["round"]:
                return "close_round"
            return to_actors("assess_actor", state)
        return "close_round"

    def route_after_review(stage):
        def route(state):
            def target(s):
                who = [a["actor_id"] for a in s["actors"] if challengeable(s, stage, a["actor_id"])]
                if who:
                    return sends(f"{stage}_challenge", s, [{"actor_id": a, "stage": stage} for a in who])
                return next_after(stage, s)
            return guarded(state, target)
        return route

    def route_after_challenge(stage):
        def route(state):
            def target(s):
                open_items = pending(s, stage)
                reviewers = sorted({finding_by_id(s, c["finding_id"])["reviewer"] for c in open_items})
                if reviewers:
                    return sends(f"{stage}_reply", s, [{"reviewer": r, "stage": stage} for r in reviewers])
                return next_after(stage, s)
            return guarded(state, target)
        return route

    def route_after_reply(stage):
        return lambda state: guarded(state, lambda s: next_after(stage, s))

    def route_start(state):
        return "build_output" if state.get("outcome") or state.get("fatal") else "release_question"

    def route_round(state):
        if state.get("fatal"):
            return "build_output"
        if state.get("stop_reason") or deadlines.expired(state["deliberation_deadline_at"]) or stop_requested(state):
            return "freeze"
        return to_actors("negotiate_actor", state)

    # ---- graph -------------------------------------------------------------

    graph = StateGraph(DebateState)
    for name, fn in (("validate_request", validate_request), ("release_question", release_question),
                     ("prepare_actor", prepare_actor), ("prepare_barrier", prepare_barrier),
                     ("revise_actor", revise_actor), ("revision_barrier", revision_barrier),
                     ("publish_package", publish_package), ("negotiate_actor", negotiate_actor),
                     ("negotiation_barrier", negotiation_barrier), ("assemble_candidate", assemble_candidate),
                     ("amend_candidate", amend_candidate), ("assess_actor", assess_actor),
                     ("assess_barrier", assess_barrier), ("close_round", close_round), ("freeze", freeze),
                     ("final_actor", final_actor), ("final_actor_barrier", final_actor_barrier),
                     ("final_review", review_node), ("final_review_barrier", final_review_barrier),
                     ("meta_report", meta_report), ("meta_evolution", meta_evolution),
                     ("report_barrier", report_barrier), ("build_output", build_output)):
        graph.add_node(name, fn)

    exits = ["freeze", "build_output"]
    next_targets = {
        "initial": ["revise_actor"], "second": ["publish_package"],
        "candidate": ["amend_candidate", "assess_actor", "close_round"], "assessment": ["close_round"]}
    for stage, targets in next_targets.items():
        graph.add_node(f"{stage}_review", review_node)
        graph.add_node(f"{stage}_review_barrier", make_review_barrier(stage))
        graph.add_node(f"{stage}_challenge", challenge_node)
        graph.add_node(f"{stage}_challenge_barrier", make_stage_barrier(stage, "widerspruch"))
        graph.add_node(f"{stage}_reply", reply_node)
        graph.add_node(f"{stage}_reply_barrier", make_stage_barrier(stage, "antwort"))
        graph.add_edge(f"{stage}_review", f"{stage}_review_barrier")
        graph.add_edge(f"{stage}_challenge", f"{stage}_challenge_barrier")
        graph.add_edge(f"{stage}_reply", f"{stage}_reply_barrier")
        graph.add_conditional_edges(f"{stage}_review_barrier", route_after_review(stage),
                                    [f"{stage}_challenge", *targets, *exits])
        graph.add_conditional_edges(f"{stage}_challenge_barrier", route_after_challenge(stage),
                                    [f"{stage}_reply", *targets, *exits])
        graph.add_conditional_edges(f"{stage}_reply_barrier", route_after_reply(stage), [*targets, *exits])

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges("validate_request", route_start, ["release_question", "build_output"])
    graph.add_conditional_edges("release_question", lambda s: to_actors("prepare_actor", s), ["prepare_actor"])
    graph.add_edge("prepare_actor", "prepare_barrier")
    graph.add_conditional_edges("prepare_barrier", lambda s: guarded(s, to_reviews("initial")),
                                ["initial_review", *exits])
    graph.add_edge("revise_actor", "revision_barrier")
    graph.add_conditional_edges("revision_barrier", lambda s: guarded(s, to_reviews("second")),
                                ["second_review", *exits])
    graph.add_conditional_edges("publish_package", lambda s: guarded(s, lambda x: to_actors("negotiate_actor", x)),
                                ["negotiate_actor", *exits])
    graph.add_edge("negotiate_actor", "negotiation_barrier")
    graph.add_conditional_edges("negotiation_barrier", lambda s: guarded(s, lambda x: "assemble_candidate"),
                                ["assemble_candidate", *exits])

    def after_candidate(state):
        def target(s):
            proposal = s.get("proposals", {}).get(current_key(s))
            if not proposal or proposal["round"] != s["round"]:
                return "close_round"
            return to_reviews("candidate")(s)
        return guarded(state, target)

    graph.add_conditional_edges("assemble_candidate", after_candidate, ["candidate_review", "close_round", *exits])
    graph.add_conditional_edges("amend_candidate", after_candidate, ["candidate_review", "close_round", *exits])
    graph.add_edge("assess_actor", "assess_barrier")
    graph.add_conditional_edges("assess_barrier", lambda s: guarded(s, to_reviews("assessment")),
                                ["assessment_review", *exits])
    graph.add_conditional_edges("close_round", route_round, ["negotiate_actor", *exits])
    graph.add_conditional_edges("freeze", lambda s: final_guarded(s, lambda x: to_actors("final_actor", x)),
                                ["final_actor", "build_output"])
    graph.add_edge("final_actor", "final_actor_barrier")
    graph.add_conditional_edges("final_actor_barrier", lambda s: final_guarded(s, to_final_reviews),
                                ["final_review", "build_output"])
    graph.add_edge("final_review", "final_review_barrier")
    graph.add_conditional_edges("final_review_barrier",
                                lambda s: final_guarded(s, lambda x: [Send("meta_report", x), Send("meta_evolution", x)]),
                                ["meta_report", "meta_evolution", "build_output"])
    graph.add_edge("meta_report", "report_barrier")
    graph.add_edge("meta_evolution", "report_barrier")
    graph.add_edge("report_barrier", "build_output")
    graph.add_edge("build_output", END)

    def to_final_reviews(state):
        return [Send("final_review", {**state, "item": item}) for item in review_items("final", state)]

    return graph


def mermaid_diagram(graph):
    """Mermaid text generated from the executable graph."""
    return graph.compile().get_graph().draw_mermaid()
