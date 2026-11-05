import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from healthsim.api_models import DebateRequest, DebateResult  # noqa: E402
from healthsim.app import Runtime  # noqa: E402
from healthsim.clock import FakeClock  # noqa: E402
from healthsim.llm_fake import FakeAdapter  # noqa: E402
from healthsim.settings import load_settings  # noqa: E402
from healthsim.sources import FixtureSources  # noqa: E402

FIXTURE = json.loads((ROOT / "examples" / "fixture_sources.json").read_text(encoding="utf-8"))
QUESTION = ("Entwickeln Sie einen gemeinsamen Vorschlag zur Verbesserung der ambulanten Versorgung in "
            "Österreich innerhalb des geltenden Rechtsrahmens. Berücksichtigen Sie Zugang, Personal und "
            "Finanzierung.")
BASE_ENV = {"LLM_API_MODE": "responses_v1", "AZURE_OPENAI_API_KEY": "geheimer-testschluessel",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "test-deployment", "AZURE_OPENAI_ACCOUNT_NAME": "testkonto",
            "LLM_REASONING_EFFORT": "low", "LLM_REQUEST_TIMEOUT_SECONDS": "45",
            "LLM_FINAL_REPORT_TIMEOUT_SECONDS": "60", "NEO4J_PASSWORD": "neo4j-geheim", "LLM_MODEL": "gpt-5.4-mini"}
NO_ENV_FILE = ROOT / "tests_sim" / "kein.env"


def make_settings(tmp_path, **extra):
    env = {**BASE_ENV, "STATE_DIR": str(tmp_path / "state"), **{k: str(v) for k, v in extra.items()}}
    return load_settings(env, env_file=NO_ENV_FILE)


def make_request(**changes):
    data = {"actors": ["Gesundheitsministerium", "Sozialversicherung", "Ärztekammer"], "question": QUESTION,
            "reference_date": "2026-11-05"}
    data.update(changes)
    return DebateRequest.model_validate(data)


class Harness:
    def __init__(self, tmp_path, overrides=None, delay=0.0, documents=None, actors=None, **settings):
        self.settings = make_settings(tmp_path, **settings)
        self.clock = FakeClock()
        self.adapter = FakeAdapter(self.clock, overrides, delay)
        self.sources = FixtureSources(actors or FIXTURE["actors"], documents or FIXTURE["documents"])
        self.runtime = Runtime(self.settings, self.adapter, self.sources, clock=self.clock, use_lock=False)

    def run(self, request=None, debate_id="debatte-1"):
        async def go():
            return await self.runtime.run(request or make_request(), debate_id)
        return asyncio.run(go())

    def prompts(self, schema=None):
        return [c for c in self.adapter.calls if schema is None or c["schema"] == schema]

    def events(self, debate_id="debatte-1"):
        return self.runtime.events.events(debate_id)

    def close(self):
        self.runtime.events.close()


@pytest.fixture
def harness(tmp_path):
    made = []

    def build(**kwargs):
        made.append(Harness(tmp_path, **kwargs))
        return made[-1]

    yield build
    for h in made:
        h.close()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def check_result(result):
    """Every result must satisfy the public schema."""
    return DebateResult.model_validate(result)
