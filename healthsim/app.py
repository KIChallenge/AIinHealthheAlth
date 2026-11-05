"""Application entry point: run, resume, connection check, schema and diagram export."""

import hashlib
import json
import logging
import sys
import uuid
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .api_models import DebateRequest, DebateResult, Message, empty_result
from .clock import Deadlines, RealClock
from .errors import ConfigurationError, SimError, SourceError
from .events import EventStore
from .intake import parse_request
from .llm import LlmService, Scheduler, build_lock
from .llm_models import ConnectionProbe
from .settings import ROOT, load_settings
from .sources import FixtureSources, Neo4jSources, SourceService
from .workflow import Deps, build_workflow, mermaid_diagram

log = logging.getLogger("healthsim")
EXIT_CODES = {"completed": 0, "incomplete": 1, "needs_clarification": 2, "invalid_input": 3, "failed": 4}
RECURSION_LIMIT = 5000    # technical guard only; stopping is decided by the workflow


def new_debate_id():
    return uuid.uuid4().hex


class Runtime:
    """Clients and credentials live here, outside serialised graph state."""

    def __init__(self, settings, adapter, source_adapter, clock=None, use_lock=True):
        self.settings, self.adapter, self.source_adapter = settings, adapter, source_adapter
        self.clock = clock or RealClock()
        self.deadlines = Deadlines(self.clock)
        self.events = EventStore(Path(settings.state_dir) / "events.sqlite")
        self.scheduler = Scheduler(settings, self.clock)
        self.llm = LlmService(settings, adapter, self.scheduler, self.clock, self.deadlines)
        self.sources = SourceService(source_adapter, self.events, settings)
        self.sources.adapter = source_adapter
        self.lock = build_lock(settings).acquire() if use_lock else None
        self.graph = build_workflow(Deps(settings, self.llm, self.sources, self.events, self.clock, self.deadlines))

    async def close(self):
        await self.adapter.close()
        await self.source_adapter.close()
        self.events.close()
        if self.lock:
            self.lock.release()

    def _checkpoint_path(self):
        return str(Path(self.settings.state_dir) / "checkpoints.sqlite")

    async def _run_graph(self, debate_id, graph_input):
        config = {"configurable": {"thread_id": debate_id}, "recursion_limit": RECURSION_LIMIT}
        async with AsyncSqliteSaver.from_conn_string(self._checkpoint_path()) as saver:
            compiled = self.graph.compile(checkpointer=saver)
            state = await compiled.ainvoke(graph_input, config)
        return state["result"]

    async def run(self, request, debate_id=None):
        debate_id = debate_id or new_debate_id()
        request_json = request.model_dump_json()
        self.events.save_debate(debate_id, request_json, self.clock.time())
        result = await self._run_graph(debate_id, {"debate_id": debate_id, "request": json.loads(request_json)})
        self.events.save_result(debate_id, json.dumps(result, ensure_ascii=False))
        return result

    async def resume(self, debate_id):
        stored = self.events.debate(debate_id)
        if stored is None:
            raise ConfigurationError(f"Unbekannte Debatte: {debate_id}.")
        if stored["result"]:
            return json.loads(stored["result"])
        result = await self._run_graph(debate_id, None)
        self.events.save_result(debate_id, json.dumps(result, ensure_ascii=False))
        return result


def failure_result(debate_id, code, message, request=None):
    return empty_result(debate_id, "failed", request=request,
                        errors=[Message(code=code, message=message)]).model_dump(mode="json")


def invalid_result(debate_id, errors):
    return empty_result(debate_id, "invalid_input", errors=errors).model_dump(mode="json")


def build_runtime(settings, llm="azure", sources="neo4j", fixture=None):
    """The fake adapter runs only when explicitly selected; it never replaces Azure on failure."""
    if llm == "azure":
        from .llm_azure import AzureResponsesAdapter
        adapter = AzureResponsesAdapter(settings)
    elif llm == "fake":
        from .llm_fake import FakeAdapter
        adapter = FakeAdapter()
    else:
        raise ConfigurationError(f"Unbekannter LLM-Adapter: {llm}.")
    if sources == "neo4j":
        source_adapter = Neo4jSources(settings)
    elif sources == "fixture":
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        source_adapter = FixtureSources(data["actors"], data["documents"])
    else:
        raise ConfigurationError(f"Unbekannte Quellenanbindung: {sources}.")
    return Runtime(settings, adapter, source_adapter)


async def run_text(raw, llm="azure", sources="neo4j", fixture=None, env=None):
    """JSON text in, result dict out. Never raises for bad input or service failures."""
    debate_id = new_debate_id()
    request, errors = parse_request(raw)
    if errors:
        return invalid_result(debate_id, errors)
    runtime = None
    try:
        settings = load_settings(env, require_azure=llm == "azure")
        runtime = build_runtime(settings, llm, sources, fixture)
        return await runtime.run(request, debate_id)
    except SimError as exc:
        return failure_result(debate_id, exc.code, exc.message, request)
    except Exception as exc:  # the JSON contract also holds for unexpected failures
        log.exception("unerwarteter Fehler")
        return failure_result(debate_id, "unexpected_error",
                              f"Unerwarteter interner Fehler: {type(exc).__name__}.", request)
    finally:
        if runtime:
            await runtime.close()


async def resume_debate(debate_id, llm="azure", sources="neo4j", fixture=None, env=None):
    runtime = None
    try:
        settings = load_settings(env, require_azure=llm == "azure")
        runtime = build_runtime(settings, llm, sources, fixture)
        return await runtime.resume(debate_id)
    except SimError as exc:
        return failure_result(debate_id, exc.code, exc.message)
    except Exception as exc:
        log.exception("unerwarteter Fehler")
        return failure_result(debate_id, "unexpected_error", f"Unerwarteter interner Fehler: {type(exc).__name__}.")
    finally:
        if runtime:
            await runtime.close()


async def check_connections(env=None):
    """Neo4j, Azure endpoint, deployment, and one small structured-output request."""
    report = {"neo4j": {"ok": False}, "azure": {"ok": False}, "model_identity_verified": False,
              "settings": None, "warnings": []}
    settings = load_settings(env)
    report["settings"] = settings.public_view()
    report["warnings"] = settings.deviations()
    neo = Neo4jSources(settings)
    try:
        schema = await neo.verify_schema()
        actors = await neo.list_actors()
        report["neo4j"] = {"ok": schema["ok"], "problems": schema["problems"], "search_mode": schema["search_mode"],
                           "actors": [a["name"] for a in actors]}
    except SourceError as exc:
        report["neo4j"] = {"ok": False, "error": exc.message}
    finally:
        await neo.close()
    from .llm import Call, CallStats
    from .llm_azure import AzureResponsesAdapter
    clock = RealClock()
    deadlines = Deadlines(clock)
    adapter = AzureResponsesAdapter(settings)
    service = LlmService(settings, adapter, Scheduler(settings, clock), clock, deadlines)
    stats = CallStats()
    try:
        out = await service.structured(Call(
            task_id="verbindungstest", role="meta", instructions="Antworte kurz auf Deutsch.",
            prompt="Bestätige die Verbindung in einem Satz.", output_model=ConnectionProbe, max_output_tokens=800,
            deadline_at=clock.time() + settings.llm_request_timeout), stats)
        report["azure"] = {"ok": True, "antwort": out.antwort, "reported_model": stats.reported_model,
                           "usage": {"input_tokens": stats.input_tokens, "output_tokens": stats.output_tokens,
                                     "reasoning_tokens": stats.reasoning_tokens}, "structured_output": True,
                           "api": "responses_v1", "store": False}
        report["model_identity_note"] = ("Azure meldet nur das Deployment; dass es gpt-5.4-mini ist, lässt sich "
                                         "ohne Verwaltungszugriff nicht bestätigen.")
    except SimError as exc:
        report["azure"] = {"ok": False, "error": exc.message, "code": exc.code}
    finally:
        await adapter.close()
    return report


def export_schemas(directory=None):
    directory = Path(directory or ROOT / "schemas")
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in (("request.schema.json", DebateRequest), ("result.schema.json", DebateResult)):
        (directory / name).write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    return directory


def export_diagram(path=None, settings=None):
    """Writes the Mermaid diagram of the executable graph."""
    settings = settings or load_settings({}, env_file=ROOT / "missing.env", require_azure=False)
    from .clock import RealClock as Clock
    clock = Clock()
    deps = Deps(settings, None, None, None, clock, Deadlines(clock))
    text = mermaid_diagram(build_workflow(deps))
    target = Path(path or ROOT / "docs" / "workflow.mmd")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def to_stdout(data):
    sys.stdout.buffer.write((json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
