"""Configuration. Precedence: process environment > .env > config/simulator.json > defaults."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "config" / "simulator.json"
ENV_FILE = ROOT / ".env"

EXPECTED_MODEL = "gpt-5.4-mini"
REQUIRED_API_MODE = "responses_v1"
REASONING_VALUES = ("none", "low", "medium", "high", "xhigh")   # OpenAI model reference
MODEL_CONTEXT_TOKENS = 400_000
MODEL_MAX_INPUT_TOKENS = 272_000
MODEL_MAX_OUTPUT_TOKENS = 128_000
MAX_CONCURRENCY = 9
# Project values. 45 s and 60 s are an operator decision and replace the 60 s and 90 s of the spec.
EXPECTED_TIMEOUTS = {"llm_request_timeout": 45.0, "llm_final_report_timeout": 60.0,
                     "deliberation_timeout": 600.0, "finalization_timeout": 180.0}
ROLES = ("actor", "truthseeker", "legaleye", "ethicmind", "meta")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_database: str
    neo4j_username: str
    neo4j_password: str = field(repr=False)
    account_name: str
    endpoint: str
    deployment: str
    api_key: str = field(repr=False)
    provider: str
    model: str
    api_mode: str
    reasoning_effort: str
    max_concurrency: int
    rate_limit_tps: int
    rate_limit_rpm: int | None
    rate_limit_tpm: int | None
    llm_request_timeout: float
    llm_final_report_timeout: float
    llm_max_retries: int
    input_budget: int
    max_output: int
    initial_max_output: int
    final_max_output: int
    deliberation_timeout: float
    finalization_timeout: float
    state_dir: Path
    role_reasoning: dict
    actor_aliases: dict
    retrieval: dict
    limits: dict
    graph_mapping: dict

    def reasoning_for(self, role):
        return self.role_reasoning.get(role, self.reasoning_effort)

    def public_view(self):
        """Allowlisted non-secret configuration; the only form that may be logged."""
        return {
            "provider": self.provider, "expected_model": self.model, "api_mode": self.api_mode,
            "deployment": self.deployment, "endpoint": self.endpoint,
            "reasoning_effort": self.reasoning_effort, "role_reasoning": dict(self.role_reasoning),
            "max_concurrency": self.max_concurrency, "rate_limit_tps": self.rate_limit_tps,
            "rate_limit_rpm": self.rate_limit_rpm, "rate_limit_tpm": self.rate_limit_tpm,
            "request_timeout_seconds": self.llm_request_timeout,
            "final_report_timeout_seconds": self.llm_final_report_timeout,
            "max_retries": self.llm_max_retries, "input_budget_tokens": self.input_budget,
            "max_output_tokens": self.max_output, "initial_max_output_tokens": self.initial_max_output,
            "final_max_output_tokens": self.final_max_output,
        }

    def deviations(self):
        """German warnings for settings that differ from the project values."""
        out = []
        for name, wanted in EXPECTED_TIMEOUTS.items():
            value = getattr(self, name)
            if value != wanted:
                out.append(f"Konfiguration weicht von den Projektwerten ab: {name} = {value:g} s "
                           f"statt {wanted:g} s.")
        return out


def read_env_file(path):
    values = {}
    path = Path(path)
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def normalise_endpoint(account_name, endpoint):
    """Return a validated Azure v1 base URL ending in exactly one /openai/v1/."""
    if endpoint:
        parts = urlsplit(endpoint.strip())
        local = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ConfigurationError("AZURE_OPENAI_ENDPOINT muss eine https-Adresse sein.")
        if not parts.netloc or parts.query or parts.fragment:
            raise ConfigurationError("AZURE_OPENAI_ENDPOINT ist ungültig: erwartet wird eine "
                                     "Ursprungsadresse oder eine Basisadresse mit /openai/v1/.")
        path = parts.path.rstrip("/")
        if path.endswith("/openai/v1"):
            path = path[:-len("/openai/v1")]
        if "openai" in path.split("/") or "deployments" in path or "projects" in path:
            raise ConfigurationError("AZURE_OPENAI_ENDPOINT darf keinen Deployment- oder "
                                     "Projektpfad enthalten.")
        return f"{parts.scheme}://{parts.netloc}{path}/openai/v1/"
    if not account_name:
        raise ConfigurationError("Bitte AZURE_OPENAI_ENDPOINT oder AZURE_OPENAI_ACCOUNT_NAME setzen.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", account_name):
        raise ConfigurationError("AZURE_OPENAI_ACCOUNT_NAME ist kein gültiger Ressourcenname.")
    return f"https://{account_name}.openai.azure.com/openai/v1/"


def _int(value, name, minimum=0):
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ConfigurationError(f"{name} muss eine ganze Zahl sein.") from None
    if number < minimum:
        raise ConfigurationError(f"{name} muss mindestens {minimum} sein.")
    return number


def _number(value, name):
    try:
        number = float(str(value).strip())
    except ValueError:
        raise ConfigurationError(f"{name} muss eine Zahl sein.") from None
    if number <= 0:
        raise ConfigurationError(f"{name} muss größer als 0 sein.")
    return number


def load_settings(env=None, env_file=ENV_FILE, settings_file=SETTINGS_FILE, require_azure=True):
    """Load and validate settings. Raises ConfigurationError with a German message."""
    try:
        base = json.loads(Path(settings_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Einstellungsdatei nicht lesbar: {exc}") from None
    merged = {**base, **read_env_file(env_file), **(os.environ if env is None else env)}

    def get(name, default=""):
        value = merged.get(name, default)
        return value if value is not None else default

    provider, model = get("LLM_PROVIDER"), get("LLM_MODEL")
    api_mode, effort = get("LLM_API_MODE"), get("LLM_REASONING_EFFORT")
    if provider != "azure_openai":
        raise ConfigurationError(f"LLM_PROVIDER muss azure_openai sein, nicht {provider!r}.")
    if model != EXPECTED_MODEL:
        raise ConfigurationError(f"LLM_MODEL muss {EXPECTED_MODEL} sein, nicht {model!r}.")
    if api_mode != REQUIRED_API_MODE:
        raise ConfigurationError(
            f"LLM_API_MODE ist {api_mode!r}, unterstützt wird nur {REQUIRED_API_MODE}. Bitte in .env "
            f"LLM_API_MODE={REQUIRED_API_MODE} setzen; ein anderer API-Modus wird nicht stillschweigend "
            "verwendet.")
    role_reasoning = dict(base.get("role_reasoning") or {})
    for role, value in {**role_reasoning, "_default": effort}.items():
        if value not in REASONING_VALUES:
            raise ConfigurationError(f"Reasoning-Stufe {value!r} wird nicht unterstützt "
                                     f"({', '.join(REASONING_VALUES)}).")
        if role != "_default" and role not in ROLES:
            raise ConfigurationError(f"Unbekannte Rolle in role_reasoning: {role!r}.")
    mapping = base.get("graph_mapping") or {}
    for key, value in mapping.items():
        if not IDENTIFIER.match(str(value)):
            raise ConfigurationError(f"graph_mapping.{key} ist kein zulässiger Bezeichner.")
    concurrency = _int(get("LLM_MAX_CONCURRENCY"), "LLM_MAX_CONCURRENCY", 1)
    if concurrency > MAX_CONCURRENCY:
        raise ConfigurationError(f"LLM_MAX_CONCURRENCY darf höchstens {MAX_CONCURRENCY} sein.")
    retries = _int(get("LLM_MAX_RETRIES"), "LLM_MAX_RETRIES")
    if retries > 1:
        raise ConfigurationError("LLM_MAX_RETRIES darf höchstens 1 sein.")
    limits = {name: _int(get(name), name, 1) for name in (
        "LLM_RATE_LIMIT_TPS", "LLM_INPUT_BUDGET_TOKENS", "LLM_MAX_OUTPUT_TOKENS",
        "LLM_INITIAL_MAX_OUTPUT_TOKENS", "LLM_FINAL_MAX_OUTPUT_TOKENS")}
    for name in ("LLM_MAX_OUTPUT_TOKENS", "LLM_INITIAL_MAX_OUTPUT_TOKENS", "LLM_FINAL_MAX_OUTPUT_TOKENS"):
        if limits[name] > MODEL_MAX_OUTPUT_TOKENS:
            raise ConfigurationError(f"{name} übersteigt das Modelllimit von {MODEL_MAX_OUTPUT_TOKENS}.")
    if limits["LLM_INPUT_BUDGET_TOKENS"] > MODEL_MAX_INPUT_TOKENS:
        raise ConfigurationError(
            f"LLM_INPUT_BUDGET_TOKENS übersteigt das Modelllimit von {MODEL_MAX_INPUT_TOKENS}.")

    deployment = get("AZURE_OPENAI_DEPLOYMENT_NAME")
    api_key = get("AZURE_OPENAI_API_KEY")
    account = get("AZURE_OPENAI_ACCOUNT_NAME")
    endpoint = get("AZURE_OPENAI_ENDPOINT")
    if require_azure:
        if not api_key:
            raise ConfigurationError("AZURE_OPENAI_API_KEY fehlt.")
        if not deployment or deployment == "your-deployment-name":
            raise ConfigurationError("AZURE_OPENAI_DEPLOYMENT_NAME fehlt: bitte den tatsächlichen "
                                     "Namen des Azure-Deployments eintragen.")
        base_url = normalise_endpoint(account, endpoint)
    else:
        base_url = normalise_endpoint(account, endpoint) if (account or endpoint) else ""
    state_dir = Path(get("STATE_DIR", "./state"))
    if not state_dir.is_absolute():
        state_dir = ROOT / state_dir
    rpm, tpm = str(get("LLM_RATE_LIMIT_RPM")).strip(), str(get("LLM_RATE_LIMIT_TPM")).strip()
    return Settings(
        neo4j_uri=get("NEO4J_URI", "neo4j://localhost:7687"),
        neo4j_database=get("NEO4J_DATABASE", "neo4j"),
        neo4j_username=get("NEO4J_USERNAME") or get("NEO4J_USER") or "neo4j",
        neo4j_password=get("NEO4J_PASSWORD"),
        account_name=account, endpoint=base_url, deployment=deployment, api_key=api_key,
        provider=provider, model=model, api_mode=api_mode, reasoning_effort=effort,
        max_concurrency=concurrency, rate_limit_tps=limits["LLM_RATE_LIMIT_TPS"],
        rate_limit_rpm=_int(rpm, "LLM_RATE_LIMIT_RPM", 1) if rpm else None,
        rate_limit_tpm=_int(tpm, "LLM_RATE_LIMIT_TPM", 1) if tpm else None,
        llm_request_timeout=_number(get("LLM_REQUEST_TIMEOUT_SECONDS"), "LLM_REQUEST_TIMEOUT_SECONDS"),
        llm_final_report_timeout=_number(get("LLM_FINAL_REPORT_TIMEOUT_SECONDS"),
                                         "LLM_FINAL_REPORT_TIMEOUT_SECONDS"),
        llm_max_retries=retries, input_budget=limits["LLM_INPUT_BUDGET_TOKENS"],
        max_output=limits["LLM_MAX_OUTPUT_TOKENS"], initial_max_output=limits["LLM_INITIAL_MAX_OUTPUT_TOKENS"],
        final_max_output=limits["LLM_FINAL_MAX_OUTPUT_TOKENS"],
        deliberation_timeout=_number(get("DELIBERATION_TIMEOUT_SECONDS"), "DELIBERATION_TIMEOUT_SECONDS"),
        finalization_timeout=_number(get("FINALIZATION_TIMEOUT_SECONDS"), "FINALIZATION_TIMEOUT_SECONDS"),
        state_dir=state_dir, role_reasoning=role_reasoning,
        actor_aliases=dict(base.get("actor_aliases") or {}),
        retrieval=dict(base.get("retrieval") or {}), limits=dict(base.get("limits") or {}),
        graph_mapping=mapping,
    )
