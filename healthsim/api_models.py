"""Public contract: strict request model and typed result model."""

import hashlib
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .llm_models import (Calculation, ClaimKind, ConstraintCheck, Coverage, DecisionKind, EvidenceLabel,
                         ExampleKind, PriorityStatus, ReportCompromise, ReportConflict, ReportImpact,
                         ReportOpenPoint)

SCHEMA_VERSION = "1.0"
RULEBOOK_VERSION = "1.0"
LEGAL_SCOPE = ("Alle Lösungen müssen innerhalb des zum Stichtag geltenden Rechtsrahmens umgesetzt "
               "werden können. Änderungen von Gesetzen oder verbindlichen Verordnungen sind als "
               "Lösung ausgeschlossen.")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assumption(Strict):
    id: str | None = None
    description: str

    @field_validator("description")
    @classmethod
    def not_empty(cls, value):
        if not value.strip():
            raise ValueError("Beschreibung darf nicht leer sein")
        return value


class HardConstraint(Strict):
    id: str | None = None
    description: str
    metric: str | None = None
    operator: Literal["<", "<=", "=", ">=", ">"] | None = None
    value: float | None = None
    unit: str | None = None
    baseline: str | None = None
    period: str | None = None

    @field_validator("description")
    @classmethod
    def not_empty(cls, value):
        if not value.strip():
            raise ValueError("Beschreibung darf nicht leer sein")
        return value

    @model_validator(mode="after")
    def numeric_fields_together(self):
        given = [self.metric, self.operator, self.value, self.unit]
        if any(v is not None for v in given) and any(v is None for v in given):
            raise ValueError("numerische Beschränkungen brauchen metric, operator, value und unit")
        return self


class DebateRequest(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    actors: list[str] = Field(min_length=1)
    question: str
    reference_date: str | None = None
    scenario_assumptions: list[Assumption] = Field(default_factory=list)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def question_not_empty(cls, value):
        if not value.strip():
            raise ValueError("Die Frage darf nicht leer sein")
        return value

    @field_validator("actors")
    @classmethod
    def actors_not_empty(cls, value):
        if any(not name.strip() for name in value):
            raise ValueError("Institutionsnamen dürfen nicht leer sein")
        return value

    @field_validator("reference_date")
    @classmethod
    def iso_date(cls, value):
        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("reference_date muss das Format JJJJ-MM-TT haben")
            date.fromisoformat(value)
        return value


def admin_id(prefix, text):
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


class CapabilityRecord(Strict):
    responses_api: bool | None
    structured_outputs: bool | None
    model_identity_verified: bool
    note: str


class ModelRecord(Strict):
    expected_model: str
    reported_model: str | None
    deployment: str | None
    capabilities: CapabilityRecord
    settings: dict[str, str | int | float | None]


class TaskRecord(Strict):
    question: str
    reference_date: str | None
    assumptions: list[Assumption]
    constraints: list[HardConstraint]
    legal_scope: str
    rulebook_version: str


class TimingRecord(Strict):
    deliberation_started_at: str | None
    deliberation_deadline_at: str | None
    deliberation_ended_at: str | None
    finalization_started_at: str | None
    finalization_deadline_at: str | None
    finalization_ended_at: str | None
    deliberation_elapsed_seconds: float | None
    finalization_elapsed_seconds: float | None
    deliberation_budget_seconds: float
    finalization_budget_seconds: float


class UsageRecord(Strict):
    input_tokens: int
    output_tokens: int                # includes reasoning tokens as reported by the API
    reasoning_tokens: int
    requests: int
    retries: int
    repairs: int
    throttled: int
    queue_seconds: float
    calls_without_usage: int
    missing_measurements: list[str]


class SatisfactionRecord(Strict):
    actor_id: str
    met_count: int
    priority_count: int
    percent: float | None
    threshold_reached: bool | None
    disputed: bool


class MetricsRecord(Strict):
    completed_rounds: int
    success_streak: int
    satisfaction: list[SatisfactionRecord]


class ClauseRecord(Strict):
    clause_id: str
    text: str
    provenance: list[str]
    premises: list[str]
    legal_status: Literal["not_reviewed", "no_conflict_identified", "defensible_with_conditions",
                          "unresolved_issue", "apparent_legal_conflict", "requires_legal_amendment"]
    law_amendment_detected: bool


class ProposalRecord(Strict):
    proposal_id: str
    version: int
    round: int
    created_at: str
    clauses: list[ClauseRecord]
    meta_update: str
    constraint_checks: list[ConstraintCheck]
    legal_admissibility: Literal["admissible_within_reviewed_sources", "conditional", "unresolved",
                                 "inadmissible", "not_reviewed"]
    review_complete: bool
    status: Literal["current", "superseded", "frozen"]


class ClaimRecord(Strict):
    claim_id: str
    kind: ClaimKind
    behauptung: str
    begruendung: str
    beispiel: str
    beispiel_art: ExampleKind
    quellen: list[str]
    berechnung: Calculation | None
    berechnung_geprueft: bool | None


class ArgumentRecord(Strict):
    argument_id: str
    actor_id: str
    version: int
    stage: str
    round: int
    claims: list[ClaimRecord]
    change_explanation: str | None
    word_count: int
    status: Literal["complete", "incomplete"]


class NegotiationRecord(Strict):
    item_id: str
    actor_id: str
    round: int
    kind: Literal["offer", "counteroffer", "concession", "condition", "disagreement"]
    text: str
    priority_ids: list[str]
    quellen: list[str]


class FindingRecord(Strict):
    finding_id: str
    reviewer: str
    target_id: str
    assessment: str
    reason: str
    reference: str
    correction: str | None


class ReviewRecord(Strict):
    review_id: str
    reviewer: str
    stage: str
    round: int
    actor_id: str | None
    findings: list[FindingRecord]
    coverage: Coverage
    unreviewed_targets: list[str]
    coverage_note: str | None
    framework: str | None
    word_count: int
    status: Literal["complete", "incomplete"]


class ChallengeRecord(Strict):
    challenge_id: str
    finding_id: str
    actor_id: str
    stage: str
    round: int
    reason: str
    status: Literal["pending", "answered"]
    reply_decision: Literal["uphold", "amend", "withdraw"] | None
    reply_reason: str | None


class PriorityRecord(Strict):
    priority_id: str
    actor_id: str
    baseline_version: int
    objective: str
    acceptance_condition: str
    quellen: list[str]
    evidence_basis: EvidenceLabel
    condition_is_simulated: bool
    law_scope_conflict: bool


class AssessmentRecord(Strict):
    assessment_id: str
    actor_id: str
    priority_id: str
    baseline_version: int
    round: int
    proposal_id: str
    proposal_version: int
    status: PriorityStatus
    previous_status: PriorityStatus | None
    reason: str
    references: list[str]
    reviewer_objections: list[str]


class DecisionRecord(Strict):
    decision_id: str
    actor_id: str
    target_id: str
    target_version: int
    round: int
    at: str
    kind: DecisionKind
    conditions: list[str]
    reason: str


class ProfileEntryRecord(Strict):
    label: EvidenceLabel
    aussage: str
    quellen: list[str]


class ProfileRecord(Strict):
    entries: list[ProfileEntryRecord]
    standpunkt: str
    standpunkt_label: EvidenceLabel
    word_count: int


class ActorRecord(Strict):
    actor_id: str
    name: str
    requested_name: str
    profile: ProfileRecord | None
    initial_standpoint: str | None
    final_standpoint: str | None
    priorities: list[PriorityRecord]
    latest_assessments: list[AssessmentRecord]
    acceptance_decisions: list[DecisionRecord]
    satisfaction: SatisfactionRecord | None


class TimelineRecord(Strict):
    event_id: str
    at: str
    role: str
    round: int
    phase: str
    type: str
    visibility: str
    refs: list[str]
    causes: list[str]


class SourceRecord(Strict):
    passage_id: str
    doc_id: str
    doc_type: str
    title: str
    publisher: str | None
    publication_date: str | None
    valid_from: str | None
    valid_until: str | None
    source_url: str | None
    content_hash: str
    source_path: str | None
    locator: str
    machine_locator: str
    date_unknown: bool


class ReportFrage(Strict):
    frage: str
    stichtag: str | None
    geltungsbereich: str
    annahmen: list[str]
    einordnung: str | None


class ReportAgent(Strict):
    actor_id: str
    name: str
    standpunkt: str | None
    prioritaeten: list[str]
    entscheidung: str | None
    zufriedenheit: SatisfactionRecord | None


class ReportVerlauf(Strict):
    zusammenfassung: str | None
    runden: int
    vorschlagsversionen: list[str]
    ereignis_refs: list[str]


class ReportKonsens(Strict):
    einschaetzung: str | None
    schwelle_erreicht_von: list[str]
    akzeptiert_von: list[str]
    bedingt_akzeptiert_von: list[str]
    abgelehnt_von: list[str]
    rechtliche_zulaessigkeit: str | None
    auflagen: list[str]


class Report(Strict):
    Frage: ReportFrage
    Agenten: list[ReportAgent]
    Verlauf: ReportVerlauf
    Konflikte: list[ReportConflict]
    Kompromisse: list[ReportCompromise]
    Konsens: ReportKonsens
    Offene_Punkte: list[ReportOpenPoint]
    Auswirkungen: list[ReportImpact]
    fehlende_abschnitte: list[str]


class DebateEvolution(Strict):
    narrative: str | None
    refs: list[str]
    word_count: int


class ClarificationQuestion(Strict):
    code: str
    frage: str


class Message(Strict):
    code: str
    message: str


class DebateResult(Strict):
    schema_version: str
    debate_id: str
    status: Literal["completed", "incomplete", "needs_clarification", "invalid_input", "failed"]
    stop_reason: Literal["satisfaction_threshold", "deliberation_deadline", "execution_error"] | None
    request: DebateRequest | None
    task: TaskRecord | None
    model: ModelRecord | None
    timing: TimingRecord | None
    usage: UsageRecord | None
    metrics: MetricsRecord | None
    final_proposal: ProposalRecord | None
    actors: list[ActorRecord]
    arguments: list[ArgumentRecord]
    negotiation: list[NegotiationRecord]
    reviews: list[ReviewRecord]
    challenges: list[ChallengeRecord]
    proposal_history: list[ProposalRecord]
    priority_history: list[AssessmentRecord]
    decisions: list[DecisionRecord]
    timeline: list[TimelineRecord]
    sources: list[SourceRecord]
    report: Report | None
    debate_evolution: DebateEvolution | None
    clarification_questions: list[ClarificationQuestion]
    warnings: list[Message]
    errors: list[Message]


def empty_result(debate_id, status, request=None, errors=(), warnings=(), clarification=()):
    """Clarification/error response: no fabricated debate content."""
    return DebateResult(
        schema_version=SCHEMA_VERSION, debate_id=debate_id, status=status,
        stop_reason="execution_error" if status == "failed" else None,
        request=request, task=None, model=None, timing=None, usage=None, metrics=None,
        final_proposal=None, actors=[], arguments=[], negotiation=[], reviews=[], challenges=[],
        proposal_history=[], priority_history=[], decisions=[], timeline=[], sources=[], report=None,
        debate_evolution=None, clarification_questions=list(clarification), warnings=list(warnings),
        errors=list(errors))
