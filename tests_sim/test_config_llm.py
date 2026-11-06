"""Configuration, Azure request shape, budgets, scheduler, retries."""

import asyncio
import json
import types

import httpx
import openai
import pytest
from conftest import BASE_ENV, NO_ENV_FILE, make_settings

from healthsim.clock import Deadlines, FakeClock, RealClock
from healthsim.errors import (ConfigurationError, DeadlineExpired, LlmAuthError, LlmDeploymentError, LlmIncomplete,
                              LlmInvalidRequest, LlmOutputError, LlmRefusal, LlmTransientError, OversizedRequest)
from healthsim.llm import Call, CallStats, LlmService, RawResponse, Scheduler, Window, estimate_tokens
from healthsim.llm_azure import AzureResponsesAdapter, request_arguments, retry_after
from healthsim.llm_fake import FakeAdapter
from healthsim import llm_models as lm
from healthsim.settings import load_settings, normalise_endpoint


def env(**changes):
    data = {**BASE_ENV, **changes}
    return {k: v for k, v in data.items() if v is not None}


# --- endpoint and settings --------------------------------------------------

def test_account_name_derives_public_endpoint():
    assert normalise_endpoint("meine-ressource", "") == "https://meine-ressource.openai.azure.com/openai/v1/"


@pytest.mark.parametrize("given", ["https://x.services.ai.azure.com", "https://x.services.ai.azure.com/",
                                   "https://x.services.ai.azure.com/openai/v1", "https://x.services.ai.azure.com/openai/v1/"])
def test_explicit_endpoint_gets_exactly_one_v1_path(given):
    assert normalise_endpoint("ignoriert", given) == "https://x.services.ai.azure.com/openai/v1/"


def test_private_gateway_path_is_kept():
    assert normalise_endpoint("", "https://gw.intern.example/ki") == "https://gw.intern.example/ki/openai/v1/"


@pytest.mark.parametrize("bad", ["http://x.azure.com", "https://x.azure.com/openai/deployments/d",
                                 "https://x.azure.com/api/projects/p", "https://x.azure.com/openai/v1?api-version=1"])
def test_bad_endpoints_are_rejected(bad):
    with pytest.raises(ConfigurationError):
        normalise_endpoint("", bad)


def test_deployment_and_family_are_distinct():
    s = load_settings(env(AZURE_OPENAI_DEPLOYMENT_NAME="mein-deployment"), env_file=NO_ENV_FILE)
    assert s.deployment == "mein-deployment" and s.model == "gpt-5.4-mini"


@pytest.mark.parametrize("change, message", [
    ({"AZURE_OPENAI_API_KEY": ""}, "API_KEY"), ({"AZURE_OPENAI_DEPLOYMENT_NAME": ""}, "DEPLOYMENT"),
    ({"AZURE_OPENAI_DEPLOYMENT_NAME": "your-deployment-name"}, "DEPLOYMENT"),
    ({"LLM_API_MODE": "chat_completions_v1"}, "LLM_API_MODE"), ({"LLM_MODEL": "gpt-4o"}, "LLM_MODEL"),
    ({"LLM_PROVIDER": "openai"}, "LLM_PROVIDER"), ({"LLM_REASONING_EFFORT": "minimal"}, "Reasoning"),
    ({"LLM_MAX_CONCURRENCY": "10"}, "CONCURRENCY"), ({"LLM_MAX_RETRIES": "2"}, "RETRIES"),
    ({"LLM_MAX_OUTPUT_TOKENS": "128001"}, "Modelllimit"), ({"LLM_INPUT_BUDGET_TOKENS": "272001"}, "Modelllimit"),
])
def test_invalid_configuration_is_rejected_in_german(change, message):
    with pytest.raises(ConfigurationError, match=message):
        load_settings(env(**change), env_file=NO_ENV_FILE)


def test_environment_overrides_file_default_and_deviations_are_reported():
    s = load_settings(env(LLM_REQUEST_TIMEOUT_SECONDS="30"), env_file=NO_ENV_FILE)
    assert s.llm_request_timeout == 30
    assert any("30 s statt 1800 s" in d for d in s.deviations())


def test_project_values_are_the_defaults_and_give_no_warning():
    defaults = {k: v for k, v in env().items() if "TIMEOUT" not in k and k != "LLM_REASONING_EFFORT"}
    s = load_settings(defaults, env_file=NO_ENV_FILE)
    assert s.deviations() == []
    assert (s.llm_request_timeout, s.llm_final_report_timeout, s.deliberation_timeout, s.finalization_timeout) == (1800, 1800, 1620, 180)
    assert (s.max_output, s.input_budget, s.reasoning_effort) == (128000, 272000, "medium")


def test_neo4j_user_alias_and_secrets_stay_out_of_views(tmp_path):
    s = load_settings(env(NEO4J_USER="alt"), env_file=NO_ENV_FILE)
    assert s.neo4j_username == "alt"
    text = json.dumps(s.public_view()) + repr(s)
    assert BASE_ENV["AZURE_OPENAI_API_KEY"] not in text and BASE_ENV["NEO4J_PASSWORD"] not in text


def test_empty_limits_mean_unknown(tmp_path):
    s = make_settings(tmp_path)
    assert s.rate_limit_rpm is None and s.rate_limit_tpm is None
    scheduler = Scheduler(s, FakeClock())
    assert scheduler.missing_limits == ["LLM_RATE_LIMIT_RPM", "LLM_RATE_LIMIT_TPM"]


# --- Azure request shape ----------------------------------------------------

def arguments():
    return request_arguments(model="dep", instructions="i", input="p", schema_name="S",
                             schema=lm.strict_schema(lm.ReviewOutput), max_output_tokens=4096, reasoning_effort="low")


def test_request_uses_responses_parameters_only():
    args = arguments()
    assert args["model"] == "dep" and args["max_output_tokens"] == 4096
    assert args["reasoning"] == {"effort": "low"} and args["store"] is False
    assert args["text"]["format"]["type"] == "json_schema" and args["text"]["format"]["strict"] is True
    for forbidden in ("max_tokens", "response_format", "temperature", "top_p", "previous_response_id"):
        assert forbidden not in args


@pytest.mark.parametrize("name", ["InitialOutput", "RevisionOutput", "ReviewOutput", "TruthReviewOutput",
                                  "LegalReviewOutput", "EthicReviewOutput", "ChallengesOutput", "RepliesOutput",
                                  "NegotiationOutput", "CandidateOutput", "AssessOutput", "FinalOutput", "ReportOutput",
                                  "EvolutionOutput"])
def test_schemas_fit_azure_strict_limits(name):
    schema = lm.strict_schema(getattr(lm, name))
    assert lm.schema_depth(schema) <= 5 and lm.schema_property_count(schema) <= 100

    def walk(node):
        if isinstance(node, dict):
            for bad in ("minItems", "maxItems", "minLength", "pattern", "format", "default", "minimum"):
                assert bad not in node or isinstance(node[bad], dict)
            if "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(schema)


def fake_openai_response(status=200, body=None, headers=None):
    request = httpx.Request("POST", "https://x/openai/v1/responses")
    return httpx.Response(status, request=request, json=body or {"error": {"message": "x"}}, headers=headers or {})


@pytest.mark.parametrize("exc, expected", [
    (openai.AuthenticationError("x", response=fake_openai_response(401), body=None), LlmAuthError),
    (openai.NotFoundError("x", response=fake_openai_response(404), body=None), LlmDeploymentError),
    (openai.BadRequestError("Unsupported parameter", response=fake_openai_response(400), body=None), LlmInvalidRequest),
    (openai.RateLimitError("x", response=fake_openai_response(429, headers={"retry-after": "3"}), body=None), LlmTransientError),
    (openai.InternalServerError("x", response=fake_openai_response(500), body=None), LlmTransientError),
])
def test_service_errors_map_without_fallback(settings, exc, expected):
    adapter = AzureResponsesAdapter(settings)

    async def raiser(**kwargs):
        raise exc

    adapter.client = types.SimpleNamespace(responses=types.SimpleNamespace(
        with_raw_response=types.SimpleNamespace(create=raiser)))

    async def go():
        await adapter.create(timeout=5, model="d", instructions="i", input="p", schema_name="S",
                             schema={}, max_output_tokens=10, reasoning_effort="low")
    with pytest.raises(expected) as info:
        asyncio.run(go())
    assert BASE_ENV["AZURE_OPENAI_API_KEY"] not in str(info.value)
    if expected is LlmTransientError and exc.status_code == 429:
        assert info.value.retry_after == 3 and info.value.throttled


def test_sdk_retries_are_disabled_and_tls_is_default(settings):
    adapter = AzureResponsesAdapter(settings)
    assert adapter.client.max_retries == 0
    assert str(adapter.client.base_url) == settings.endpoint


def test_retry_after_headers():
    assert retry_after({"retry-after-ms": "1500"}) == 1.5 and retry_after({"retry-after": "x"}) is None


# --- service ----------------------------------------------------------------

def service(tmp_path, adapter=None, clock=None, **extra):
    settings = make_settings(tmp_path, **extra)
    clock = clock or FakeClock()
    deadlines = Deadlines(clock)
    scheduler = Scheduler(settings, clock)
    return LlmService(settings, adapter or FakeAdapter(clock), scheduler, clock, deadlines), clock


def call(clock, model=lm.ConnectionProbe, prompt="kurz", output=100, deadline=600, final=False):
    return Call("t", "meta", "anweisung", prompt, model, output, clock.time() + deadline, final)


def test_usage_does_not_double_count_reasoning(tmp_path):
    svc, clock = service(tmp_path)
    stats = CallStats()
    asyncio.run(svc.structured(call(clock), stats))
    assert (stats.input_tokens, stats.output_tokens, stats.reasoning_tokens) == (100, 50, 10)


def test_input_budget_includes_schema_and_instructions(tmp_path):
    # Instructions and prompt alone estimate about 21 tokens; only the schema exceeds 40.
    svc, clock = service(tmp_path, LLM_INPUT_BUDGET_TOKENS=40)
    with pytest.raises(OversizedRequest):
        asyncio.run(svc.structured(call(clock), CallStats()))


def test_request_larger_than_second_limit_is_an_error_not_a_wait(tmp_path):
    svc, clock = service(tmp_path, LLM_RATE_LIMIT_TPS=500)
    with pytest.raises(OversizedRequest, match="Sekundenlimit"):
        asyncio.run(svc.structured(call(clock, output=4096), CallStats()))


@pytest.mark.parametrize("raw, expected", [
    (RawResponse("completed", None, refusal="nein"), LlmRefusal),
    (RawResponse("incomplete", None, incomplete_reason="max_output_tokens"), LlmIncomplete),
    (RawResponse("incomplete", None, incomplete_reason="content_filter"), LlmIncomplete),
    (RawResponse("completed", None), LlmOutputError), (RawResponse("completed", "{kein json"), LlmOutputError),
    (RawResponse("completed", '{"falsch": 1}'), LlmOutputError), (RawResponse("failed", "{}"), LlmOutputError),
])
def test_unusable_responses_are_explicit(tmp_path, raw, expected):
    svc, clock = service(tmp_path, FakeAdapter(overrides={"ConnectionProbe": lambda p, n: raw}))
    with pytest.raises(expected):
        asyncio.run(svc.structured(call(clock), CallStats()))


def test_transient_failure_retries_once_honouring_retry_after(tmp_path):
    clock = FakeClock()
    faults = {"ConnectionProbe": lambda p, n: LlmTransientError("429", retry_after=7, throttled=True) if n == 1 else None}
    adapter = FakeAdapter(clock, faults)
    svc, _ = service(tmp_path, adapter, clock)
    stats, start = CallStats(), clock.now
    asyncio.run(svc.structured(call(clock), stats))
    assert stats.requests == 2 and stats.retries == 1 and stats.throttled == 1 and clock.now - start >= 7


def test_second_transient_failure_is_not_retried(tmp_path):
    clock = FakeClock()
    adapter = FakeAdapter(clock, {"ConnectionProbe": lambda p, n: LlmTransientError("503")})
    svc, _ = service(tmp_path, adapter, clock)
    stats = CallStats()
    with pytest.raises(LlmTransientError):
        asyncio.run(svc.structured(call(clock), stats))
    assert stats.requests == 2


def test_auth_error_is_not_retried(tmp_path):
    clock = FakeClock()
    adapter = FakeAdapter(clock, {"ConnectionProbe": lambda p, n: LlmAuthError("abgelehnt")})
    svc, _ = service(tmp_path, adapter, clock)
    stats = CallStats()
    with pytest.raises(LlmAuthError):
        asyncio.run(svc.structured(call(clock), stats))
    assert stats.requests == 1


def test_retry_delay_longer_than_budget_is_not_waited_for(tmp_path):
    clock = FakeClock()
    adapter = FakeAdapter(clock, {"ConnectionProbe": lambda p, n: LlmTransientError("429", retry_after=500, throttled=True)})
    svc, _ = service(tmp_path, adapter, clock)
    stats, start = CallStats(), clock.now
    with pytest.raises(LlmTransientError):
        asyncio.run(svc.structured(call(clock), stats))
    assert stats.requests == 1 and clock.now - start < 60


def test_operation_budget_is_min_of_timeout_and_phase(tmp_path):
    clock = FakeClock()
    adapter = FakeAdapter(clock)
    svc, _ = service(tmp_path, adapter, clock)
    asyncio.run(svc.structured(call(clock, deadline=20), CallStats()))
    asyncio.run(svc.structured(call(clock, deadline=500), CallStats()))
    asyncio.run(svc.structured(call(clock, deadline=500, final=True), CallStats()))
    assert [round(c["timeout"]) for c in adapter.calls] == [20, 45, 60]


def test_no_dispatch_after_the_phase_deadline(tmp_path):
    svc, clock = service(tmp_path)
    with pytest.raises(DeadlineExpired):
        asyncio.run(svc.structured(call(clock, deadline=0), CallStats()))
    assert svc.adapter.calls == []


def test_slow_response_exceeding_budget_is_discarded(tmp_path):
    clock = FakeClock()
    svc, _ = service(tmp_path, FakeAdapter(clock, delay=46), clock)
    with pytest.raises(LlmTransientError, match="Zeitbudget"):
        asyncio.run(svc.structured(call(clock), CallStats()))


def test_real_timeout_cancels_a_hanging_call(tmp_path):
    class Hanging:
        cancelled = False

        async def create(self, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                Hanging.cancelled = True
                raise

    clock = RealClock()
    settings = make_settings(tmp_path, LLM_REQUEST_TIMEOUT_SECONDS=0.05)

    async def go():
        svc = LlmService(settings, Hanging(), Scheduler(settings, clock), clock, Deadlines(clock))
        await svc.structured(call(clock), CallStats())
    with pytest.raises(LlmTransientError):
        asyncio.run(go())
    assert Hanging.cancelled


# --- scheduler ---------------------------------------------------------------

def test_concurrency_never_exceeds_nine(tmp_path):
    clock = RealClock()
    settings = make_settings(tmp_path)
    active = {"now": 0, "peak": 0}

    class Slow:
        async def create(self, **kwargs):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.01)
            active["now"] -= 1
            return RawResponse("completed", '{"antwort": "ja"}', input_tokens=1, output_tokens=1)

    async def go():
        svc = LlmService(settings, Slow(), Scheduler(settings, clock), clock, Deadlines(clock))
        await asyncio.gather(*(svc.structured(call(clock), CallStats()) for _ in range(30)))
    asyncio.run(go())
    assert active["peak"] == 9


def test_sliding_window_never_exceeds_the_second_limit():
    window = Window(1.0, 1000)
    now = 0.0
    log = []
    for i in range(40):
        while (wait := window.wait_time(now, 300)) > 0:     # same loop as Scheduler.admit
            now += wait
        window.add(now, 300)
        log.append(now)
        now += 0.01
    for t in log:
        assert sum(300 for x in log if t - 1.0 + 1e-9 < x <= t) <= 1000     # 1e-9: float rounding only


def test_tps_pacing_delays_admission(tmp_path):
    clock = FakeClock()
    settings = make_settings(tmp_path, LLM_RATE_LIMIT_TPS=10000)
    scheduler = Scheduler(settings, clock)

    async def go():
        first = clock.now
        await scheduler.admit(6000, lambda: 100)
        await scheduler.admit(6000, lambda: 100)
        return clock.now - first
    assert asyncio.run(go()) == pytest.approx(1.0)


def test_reconcile_frees_capacity(tmp_path):
    clock = FakeClock()
    scheduler = Scheduler(make_settings(tmp_path, LLM_RATE_LIMIT_TPS=10000), clock)

    async def go():
        tickets = await scheduler.admit(9000, lambda: 100)
        scheduler.reconcile(tickets, 100)
        start = clock.now
        await scheduler.admit(9000, lambda: 100)
        return clock.now - start
    assert asyncio.run(go()) == 0


def test_rpm_and_tpm_are_additional_limits(tmp_path):
    clock = FakeClock()
    scheduler = Scheduler(make_settings(tmp_path, LLM_RATE_LIMIT_RPM=2, LLM_RATE_LIMIT_TPM=100000), clock)

    async def go():
        start = clock.now
        for _ in range(3):
            await scheduler.admit(10, lambda: 500)
        return clock.now - start
    assert asyncio.run(go()) == pytest.approx(60)


def test_rate_wait_stops_at_phase_deadline(tmp_path):
    clock = FakeClock()
    scheduler = Scheduler(make_settings(tmp_path, LLM_RATE_LIMIT_RPM=1), clock)

    async def go():
        await scheduler.admit(10, lambda: 5)
        await scheduler.admit(10, lambda: 5)
    with pytest.raises(DeadlineExpired):
        asyncio.run(go())


def test_estimate_is_conservative_for_german():
    text = "Die ambulante Gesundheitsversorgung in Österreich wird gemeinsam geplant. " * 20
    assert estimate_tokens(text) >= len(text) / 3
