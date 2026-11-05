"""Model access: deployment-wide scheduler and the single application retry layer.

Admission policy: a sliding one-second window of token reservations (input estimate
plus the full output allowance, which includes reasoning). A request is admitted only
if the sum of reservations in the last second plus its own stays within
LLM_RATE_LIMIT_TPS. Because this is a sliding log and not a token bucket, no burst can
exceed the declared per-second limit. After a response, the reservation is reconciled
down to the reported usage. RPM/TPM, when configured, are additional 60-second windows.
These reservations are application bookkeeping, not Azure's accounting.

The application assumes one process per state directory, enforced by `ProcessLock`.
Limits are therefore not coordinated across processes.
"""

import asyncio
import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .errors import (ConfigurationError, DeadlineExpired, LlmIncomplete, LlmOutputError, LlmRefusal,
                     LlmTransientError, OversizedRequest)
from .llm_models import strict_schema
from .settings import MODEL_CONTEXT_TOKENS, MODEL_MAX_INPUT_TOKENS, MODEL_MAX_OUTPUT_TOKENS

CHARS_PER_TOKEN = 3.0      # conservative for German text (no tokenizer dependency)
ESTIMATE_MARGIN = 1.10
BACKOFF_SECONDS = 2.0


def estimate_tokens(*texts):
    return int(sum(len(t) for t in texts) / CHARS_PER_TOKEN * ESTIMATE_MARGIN) + 16


@dataclass
class RawResponse:
    status: str
    output_text: str | None
    refusal: str | None = None
    incomplete_reason: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    headers: dict = field(default_factory=dict)


@dataclass
class Call:
    task_id: str
    role: str
    instructions: str
    prompt: str
    output_model: type
    max_output_tokens: int
    deadline_at: float
    final: bool = False


@dataclass
class CallStats:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    requests: int = 0
    retries: int = 0
    throttled: int = 0
    queue_seconds: float = 0.0
    calls_without_usage: int = 0
    reported_model: str | None = None

    def as_dict(self):
        return dict(self.__dict__)


class ProcessLock:
    """Exclusive lock on the state directory for the single-process assumption."""

    def __init__(self, state_dir):
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(state_dir) / "process.lock"
        self._file = None

    def acquire(self):
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ConfigurationError("Ein anderer Simulatorprozess nutzt dieses STATE_DIR bereits. Die "
                                     "Limits gelten nur innerhalb eines Prozesses.") from None
        self._file = handle
        return self

    def release(self):
        if self._file:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            self._file.close()
            self._file = None


class Window:
    """Sliding log of reservations over a fixed number of seconds."""

    def __init__(self, seconds, limit):
        self.seconds, self.limit = seconds, limit
        self.entries = deque()

    def prune(self, now):
        while self.entries and self.entries[0][0] <= now - self.seconds:
            self.entries.popleft()

    def used(self, now):
        self.prune(now)
        return sum(e[1] for e in self.entries)

    def wait_time(self, now, amount):
        """Seconds until `amount` fits, 0 if it fits now."""
        self.prune(now)
        used = sum(e[1] for e in self.entries)
        if used + amount <= self.limit:
            return 0.0
        freed = 0
        for entry in self.entries:
            freed += entry[1]
            if used - freed + amount <= self.limit:
                return entry[0] + self.seconds - now
        return self.seconds

    def add(self, now, amount):
        entry = [now, amount]
        self.entries.append(entry)
        return entry


class Scheduler:
    """Concurrency and rate admission shared by all roles and debates in this process."""

    def __init__(self, settings, clock):
        self.clock = clock
        self.semaphore = asyncio.Semaphore(settings.max_concurrency)
        self.max_concurrency = settings.max_concurrency
        self.active = 0
        self.peak_active = 0
        self.tps = Window(1.0, settings.rate_limit_tps)
        self.tpm = Window(60.0, settings.rate_limit_tpm) if settings.rate_limit_tpm else None
        self.rpm = Window(60.0, settings.rate_limit_rpm) if settings.rate_limit_rpm else None
        self._admission = asyncio.Lock()
        self.missing_limits = [name for name, value in (
            ("LLM_RATE_LIMIT_RPM", settings.rate_limit_rpm),
            ("LLM_RATE_LIMIT_TPM", settings.rate_limit_tpm)) if value is None]

    def check_fits(self, tokens):
        if tokens > self.tps.limit:
            raise OversizedRequest(f"Die Reservierung von {tokens} Token übersteigt das Sekundenlimit "
                                   f"von {self.tps.limit}; der Kontext muss verkleinert werden.")
        if self.tpm and tokens > self.tpm.limit:
            raise OversizedRequest(f"Die Reservierung von {tokens} Token übersteigt das Minutenlimit "
                                   f"von {self.tpm.limit}.")

    async def admit(self, tokens, remaining):
        """Wait for rate capacity. `remaining()` gives seconds left in the phase."""
        self.check_fits(tokens)
        async with self._admission:     # first come, first served
            while True:
                now = self.clock.monotonic()
                wait = max(self.tps.wait_time(now, tokens),
                           self.tpm.wait_time(now, tokens) if self.tpm else 0.0,
                           self.rpm.wait_time(now, 1) if self.rpm else 0.0)
                if wait <= 0:
                    tickets = [self.tps.add(now, tokens)]
                    if self.tpm:
                        tickets.append(self.tpm.add(now, tokens))
                    if self.rpm:
                        self.rpm.add(now, 1)
                    return tickets
                left = remaining()
                if left <= 0 or wait >= left:
                    raise DeadlineExpired("Die Phasenfrist endet vor der nächsten Kapazitätsfreigabe.")
                await self.clock.sleep(wait)

    @staticmethod
    def reconcile(tickets, actual):
        """Lower reservations to reported usage; never raise them retroactively."""
        for ticket in tickets:
            ticket[1] = min(ticket[1], actual)

    async def __aenter__(self):
        await self.semaphore.acquire()
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        return self

    async def __aexit__(self, *exc):
        self.active -= 1
        self.semaphore.release()


class LlmService:
    """Budget checks, admission, dispatch, one transient retry, parsing and local validation."""

    def __init__(self, settings, adapter, scheduler, clock, deadlines):
        self.settings, self.adapter, self.scheduler = settings, adapter, scheduler
        self.clock, self.deadlines = clock, deadlines
        self.reported_model = None

    def check_budget(self, input_tokens, max_output):
        if input_tokens > self.settings.input_budget:
            raise OversizedRequest(f"Eingabe von geschätzt {input_tokens} Token übersteigt das Budget "
                                   f"von {self.settings.input_budget}.")
        if input_tokens > MODEL_MAX_INPUT_TOKENS or max_output > MODEL_MAX_OUTPUT_TOKENS \
                or input_tokens + max_output > MODEL_CONTEXT_TOKENS:
            raise OversizedRequest("Die Anfrage übersteigt die Kontextgrenzen des Modells.")

    async def structured(self, call: Call, stats: CallStats):
        """Return the validated output model. Raises SimError subclasses."""
        schema = strict_schema(call.output_model)
        estimate = estimate_tokens(call.instructions, call.prompt, json.dumps(schema))
        self.check_budget(estimate, call.max_output_tokens)
        reservation = estimate + call.max_output_tokens
        self.scheduler.check_fits(reservation)

        def remaining():
            return self.deadlines.remaining(call.deadline_at)

        queued_at = self.clock.monotonic()
        async with self.scheduler:
            tickets = await self.scheduler.admit(reservation, remaining)
            stats.queue_seconds += self.clock.monotonic() - queued_at
            configured = (self.settings.llm_final_report_timeout if call.final
                          else self.settings.llm_request_timeout)
            budget = min(configured, remaining())
            if budget <= 0:
                raise DeadlineExpired("Für diesen Modellaufruf bleibt keine Zeit.")
            try:
                raw = await self.clock.wait_for(
                    self._attempts(call, schema, stats, budget, reservation), budget)
            except TimeoutError:
                raise LlmTransientError(f"Zeitbudget von {budget:.0f} s für den Modellaufruf "
                                        "überschritten.") from None
            self._record_usage(raw, stats, tickets)
        return self._parse(raw, call)

    async def _attempts(self, call, schema, stats, budget, reservation):
        started = self.clock.monotonic()
        attempt = 0
        while True:
            left = budget - (self.clock.monotonic() - started)
            try:
                stats.requests += 1
                return await self.adapter.create(
                    model=self.settings.deployment, instructions=call.instructions, input=call.prompt,
                    schema_name=call.output_model.__name__, schema=schema,
                    max_output_tokens=call.max_output_tokens,
                    reasoning_effort=self.settings.reasoning_for(call.role), timeout=left)
            except LlmTransientError as exc:
                stats.throttled += 1 if exc.throttled else 0
                if attempt >= self.settings.llm_max_retries:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else BACKOFF_SECONDS
                left = budget - (self.clock.monotonic() - started)
                if delay >= left:
                    raise
                attempt += 1
                stats.retries += 1
                await self.clock.sleep(delay)
                await self.scheduler.admit(reservation, lambda: budget - (self.clock.monotonic() - started))

    def _record_usage(self, raw, stats, tickets):
        if raw.model:
            stats.reported_model = raw.model
            self.reported_model = raw.model
        if raw.input_tokens is None or raw.output_tokens is None:
            stats.calls_without_usage += 1
            return
        stats.input_tokens += raw.input_tokens
        stats.output_tokens += raw.output_tokens           # already includes reasoning tokens
        stats.reasoning_tokens += raw.reasoning_tokens or 0
        self.scheduler.reconcile(tickets, raw.input_tokens + raw.output_tokens)

    @staticmethod
    def _parse(raw, call):
        if raw.refusal:
            raise LlmRefusal(f"Das Modell hat die Antwort verweigert: {raw.refusal[:200]}")
        if raw.status == "incomplete":
            reason = raw.incomplete_reason or "unbekannt"
            hint = " (Ausgabelimit erreicht)" if reason == "max_output_tokens" else ""
            raise LlmIncomplete(f"Die Modellantwort ist unvollständig: {reason}{hint}.")
        if raw.status != "completed":
            raise LlmOutputError(f"Unerwarteter Antwortstatus: {raw.status}.")
        if not raw.output_text:
            raise LlmOutputError("Die Modellantwort enthält keine strukturierte Ausgabe.")
        try:
            return call.output_model.model_validate_json(raw.output_text)
        except ValidationError as exc:
            raise LlmOutputError(f"Die Modellantwort entspricht nicht dem Schema "
                                 f"({exc.error_count()} Fehler).") from None


def build_lock(settings):
    os.makedirs(settings.state_dir, exist_ok=True)
    return ProcessLock(settings.state_dir)


async def gather_bounded(coroutines):
    return await asyncio.gather(*coroutines, return_exceptions=True)
