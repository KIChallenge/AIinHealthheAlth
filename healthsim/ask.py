"""Private questions to the meta-moderator about a finished debate.

The answer comes only from the stored debate record. There is no source search and no new
negotiation, and the record never changes: questions and answers are appended to the event
store as private events. A call takes the same process lock as a debate, so the concurrency
and rate limits still hold. The fake adapter is used only when explicitly selected.
"""

import asyncio
import json
import re
import uuid
from pathlib import Path

from . import prompts, rules
from .clock import Deadlines, RealClock
from .errors import ConfigurationError, SimError
from .events import EventStore
from .intake import SECRET_PATTERNS
from .llm import Call, CallStats, LlmService, ProcessLock, Scheduler, estimate_tokens
from .llm_models import MetaAnswer
from .settings import load_settings

MAX_QUESTION_CHARS = 1500
ANSWER_WORDS = 320          # 300 words asked for, small margin before a repair
HISTORY_TURNS = 6
RECORD_TOKENS = 40_000      # per question; the debate budget would make every question expensive
IDS = re.compile(r"\b(?:KL|D|AS|F|NEG|PROP|PR|ARG)-[\w.-]*\w")


class AskError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _short(records, keys):
    return [{k: r.get(k) for k in keys} for r in records]


def debate_record(result, limit_tokens):
    """Compact protocol of a result. No raw source text; shortened only as far as needed."""
    proposals = sorted(result["proposal_history"], key=lambda p: p["version"])
    full = [{"key": f"{p['proposal_id']}-v{p['version']}", "runde": p["round"], "zulaessigkeit": p["legal_admissibility"],
             "klauseln": _short(p["clauses"], ("clause_id", "text", "legal_status"))} for p in proposals[-3:]]
    actors = [{"name": a["name"], "standpunkt": a["initial_standpoint"], "abschluss": a["final_standpoint"],
               "prioritaeten": _short(a["priorities"], ("priority_id", "objective", "acceptance_condition"))}
              for a in result["actors"]]
    rounds = {}
    for a in result["priority_history"]:
        rounds.setdefault(a["round"], []).append({k: a[k] for k in ("assessment_id", "priority_id", "status", "reason")})
    findings = [f for r in result["reviews"] for f in r["findings"]
                if f["assessment"] not in ("supported", "no_conflict_identified", "no_concern_identified")]
    report = result.get("report") or {}
    record = {
        "status": result["status"], "stoppgrund": result["stop_reason"], "kennzahlen": result["metrics"],
        "eingefroren": result.get("final_proposal") and f"PROP-1-v{result['final_proposal']['version']}",
        "akteure": actors, "fassungen": full,
        "verlauf_der_fassungen": [{"version": p["version"], "runde": p["round"], "fortschritt": p["meta_update"]} for p in proposals],
        "entscheidungen": _short(result["decisions"], ("decision_id", "actor_id", "round", "kind", "reason", "conditions")),
        "bewertungen": rounds,
        "befunde": _short(findings, ("finding_id", "reviewer", "target_id", "assessment", "reason")),
        "verhandlung": _short(result["negotiation"], ("item_id", "actor_id", "round", "kind", "text")),
        "konflikte": report.get("Konflikte"), "offene_punkte": report.get("Offene_Punkte"),
        "auswirkungen": report.get("Auswirkungen"), "konsens": (report.get("Konsens") or {}).get("einschaetzung"),
        "entwicklung": (result.get("debate_evolution") or {}).get("narrative"),
    }

    def fits():
        return estimate_tokens(prompts.dumps(record)) <= limit_tokens

    keep = 256
    while not fits() and keep >= 1:
        record["verhandlung"] = record["verhandlung"][-keep:]
        record["befunde"] = record["befunde"][-keep:]
        record["bewertungen"] = {k: rounds[k] for k in sorted(rounds)[-max(keep // 8, 1):]}
        record["entscheidungen"] = record["entscheidungen"][-keep:]
        record["verlauf_der_fassungen"] = record["verlauf_der_fassungen"][-keep:]
        record["gekuerzt"] = "Ältere Einträge fehlen hier; sage das, wenn eine Frage sie betrifft."
        keep //= 2
    if not fits():
        raise AskError(413, "Das Protokoll ist für eine Nachfrage zu groß.")
    return record


class Asker:
    def __init__(self, directory, llm="azure", adapter=None, env=None, clock=None):
        self.directory = Path(directory)
        self.llm, self.adapter, self.env = llm, adapter, env
        self.clock = clock or RealClock()

    def history(self, debate_id):
        events = EventStore(self.directory / "events.sqlite")
        try:
            rows = [e for e in events.events(debate_id) if e["type"] in ("meta_question", "meta_answer")]
        finally:
            events.close()
        return [{"role": "du" if e["type"] == "meta_question" else "meta", "text": e["payload"]["text"],
                 "refs": e["payload"].get("refs", []), "at": e["at"]} for e in rows]

    def _adapter(self, settings):
        if self.adapter is not None:
            return self.adapter
        if self.llm == "fake":
            from .llm_fake import FakeAdapter
            return FakeAdapter(self.clock)
        from .llm_azure import AzureResponsesAdapter
        return AzureResponsesAdapter(settings)

    async def _call(self, settings, instructions, prompt, known):
        adapter = self._adapter(settings)
        deadlines = Deadlines(self.clock)
        service = LlmService(settings, adapter, Scheduler(settings, self.clock), self.clock, deadlines)
        text = prompt
        problems = []
        try:
            for _ in range(2):      # one bounded repair
                out = await service.structured(
                    Call("nachfrage", "meta", instructions, text, MetaAnswer, settings.max_output,
                         self.clock.time() + settings.llm_request_timeout), CallStats())
                problems = rules.word_problem_limit(out.antwort, ANSWER_WORDS, "Antwort")
                if rules.mentions_law_change(out.antwort):
                    problems.append("Rechtsänderungen sind als Lösung ausgeschlossen.")
                if not problems:
                    return out, [r for r in out.refs if r in known]
                text = f"{prompt}\n\nDeine vorige Antwort war ungültig: {' '.join(problems)} Korrigiere nur das."
        finally:
            if self.adapter is None:
                await adapter.close()
        raise AskError(502, "Der Meta-Moderator hat keine gültige Antwort geliefert: " + " ".join(problems))

    def answer(self, debate_id, question, result_json):
        question = (question or "").strip()
        if not question:
            raise AskError(400, "Die Frage ist leer.")
        if len(question) > MAX_QUESTION_CHARS:
            raise AskError(400, f"Die Frage darf höchstens {MAX_QUESTION_CHARS} Zeichen lang sein.")
        if SECRET_PATTERNS.search(question):
            raise AskError(400, "Die Frage darf keine Zugangsdaten enthalten.")
        if result_json is None:
            raise AskError(404, "Nachfragen gehen nur bei gespeicherten debAItes.")
        result = json.loads(result_json)
        if not result.get("task"):
            raise AskError(409, "Zu dieser Anfrage gibt es keine debAIte.")
        try:
            settings = load_settings(self.env, require_azure=self.llm == "azure" and self.adapter is None)
        except ConfigurationError as exc:
            raise AskError(500, exc.message) from None
        try:
            lock = ProcessLock(self.directory).acquire()
        except ConfigurationError:
            raise AskError(409, "Gerade läuft eine debAIte. Nachfragen gehen, sobald sie fertig ist.") from None
        try:
            record = debate_record(result, min(settings.input_budget // 2, RECORD_TOKENS))
            turns = self.history(debate_id)[-HISTORY_TURNS * 2:]
            text = prompts.dumps(record)
            prompt = prompts.meta_answer_prompt(result["task"], text, turns, question)
            instructions = prompts.instructions("meta") + "\n\n" + prompts.META_ANSWER_RULES
            try:
                out, refs = asyncio.run(self._call(settings, instructions, prompt, set(IDS.findall(text))))
            except SimError as exc:
                raise AskError(502, exc.message) from None
            at = self.clock.time()
            turn = uuid.uuid4().hex
            events = EventStore(self.directory / "events.sqlite")
            try:
                events.emit(debate_id, f"frage:{turn}", at, "du", 0, "nachfrage", "private", "meta_question", payload={"text": question})
                events.emit(debate_id, f"antwort:{turn}", at, "meta", 0, "nachfrage", "private", "meta_answer", refs,
                            [f"frage:{turn}"], {"text": out.antwort, "refs": refs})
            finally:
                events.close()
            return {"antwort": out.antwort, "refs": refs, "keine_grundlage": out.keine_grundlage}
        finally:
            lock.release()
