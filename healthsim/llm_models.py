"""Structured-output contracts of the model roles.

Azure strict schemas allow at most five nesting levels and require every field,
so optional values are nullable rather than omitted. `strict_schema` converts a
model into that form. Identifiers are assigned by program code, never by the model.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


EvidenceLabel = Literal["documented", "reported", "inferred", "unknown"]
ClaimKind = Literal["fact", "reported", "assumption", "inference", "proposal", "prediction"]
ExampleKind = Literal["source_based", "hypothetical", "unavailable"]
PriorityStatus = Literal["met", "partly_met", "unmet", "unclear"]
DecisionKind = Literal["accept", "reject", "conditional", "withdraw"]
Coverage = Literal["complete", "incomplete"]

TRUTH_ASSESSMENTS = ("supported", "qualified_support", "no_support", "contradiction",
                     "reasoning_problem", "insufficient_information")
LEGAL_ASSESSMENTS = ("no_conflict_identified", "defensible_with_conditions", "unresolved_issue",
                     "apparent_legal_conflict", "requires_legal_amendment")
ETHIC_ASSESSMENTS = ("no_concern_identified", "concern_with_mitigation", "serious_concern",
                     "conflict_of_principles", "insufficient_information")
ALLOWED_ASSESSMENTS = {"truthseeker": TRUTH_ASSESSMENTS, "legaleye": LEGAL_ASSESSMENTS,
                       "ethicmind": ETHIC_ASSESSMENTS}
CLEAN_ASSESSMENTS = {"supported", "no_conflict_identified", "no_concern_identified"}


class CalculationInput(Contract):
    value: float
    unit: str
    period: str | None
    quelle: str          # passage id or assumption id


class Calculation(Contract):
    operation: Literal["sum", "difference", "product", "ratio", "percent_change"]
    inputs: list[CalculationInput]
    result: float
    unit: str


class Claim(Contract):
    kind: ClaimKind
    behauptung: str
    begruendung: str
    beispiel: str
    beispiel_art: ExampleKind
    quellen: list[str]
    berechnung: Calculation | None


class Argument(Contract):
    claims: list[Claim]


class ProfileEntry(Contract):
    label: EvidenceLabel
    aussage: str
    quellen: list[str]


class Profile(Contract):
    entries: list[ProfileEntry]
    standpunkt: str
    standpunkt_label: EvidenceLabel


class Priority(Contract):
    priority_id: str | None        # null for a new priority; existing id when revising
    objective: str
    acceptance_condition: str
    quellen: list[str]
    evidence_basis: EvidenceLabel
    condition_is_simulated: bool
    law_scope_conflict: bool


class InitialOutput(Contract):
    profile: Profile
    priorities: list[Priority]
    argument: Argument


class FindingOutput(Contract):
    target_id: str
    assessment: Literal[
        "supported", "qualified_support", "no_support", "contradiction", "reasoning_problem",
        "insufficient_information", "no_conflict_identified", "defensible_with_conditions",
        "unresolved_issue", "apparent_legal_conflict", "requires_legal_amendment",
        "no_concern_identified", "concern_with_mitigation", "serious_concern", "conflict_of_principles"]
    reason: str
    reference: str
    correction: str | None


class ReviewOutput(Contract):
    findings: list[FindingOutput]
    coverage: Coverage
    unreviewed_targets: list[str]     # target ids that could not be examined
    coverage_note: str | None


class TruthFinding(FindingOutput):
    assessment: Literal[TRUTH_ASSESSMENTS]


class LegalFinding(FindingOutput):
    assessment: Literal[LEGAL_ASSESSMENTS]


class EthicFinding(FindingOutput):
    assessment: Literal[ETHIC_ASSESSMENTS]


class TruthReviewOutput(ReviewOutput):
    findings: list[TruthFinding]


class LegalReviewOutput(ReviewOutput):
    findings: list[LegalFinding]


class EthicReviewOutput(ReviewOutput):
    findings: list[EthicFinding]


REVIEW_MODELS = {"truthseeker": TruthReviewOutput, "legaleye": LegalReviewOutput, "ethicmind": EthicReviewOutput}


class ChallengeItem(Contract):
    finding_id: str
    reason: str


class ChallengesOutput(Contract):
    challenges: list[ChallengeItem]


class ReplyItem(Contract):
    challenge_id: str
    decision: Literal["uphold", "amend", "withdraw"]
    reason: str


class RepliesOutput(Contract):
    replies: list[ReplyItem]


class RevisionOutput(Contract):
    argument: Argument
    change_explanation: str
    profile: Profile | None
    priorities: list[Priority] | None


class NegotiationItem(Contract):
    kind: Literal["offer", "counteroffer", "concession", "condition", "disagreement"]
    text: str
    priority_ids: list[str]
    quellen: list[str]


class PriorityCorrection(Contract):
    priority_id: str
    acceptance_condition: str
    justification: str
    quellen: list[str]


class NegotiationOutput(Contract):
    items: list[NegotiationItem]
    corrections: list[PriorityCorrection]


class ClauseOutput(Contract):
    unchanged_from: str | None     # id of an existing clause to keep word for word; text is then ignored
    text: str
    provenance: list[str]      # ids of negotiation items, arguments or earlier clauses
    premises: list[str]        # passage or assumption ids


class ConstraintCheck(Contract):
    constraint_id: str
    status: Literal["satisfied", "violated", "unclear"]
    reason: str


class CandidateOutput(Contract):
    clauses: list[ClauseOutput]
    meta_update: str
    constraint_checks: list[ConstraintCheck]


class AssessmentItem(Contract):
    priority_id: str
    status: PriorityStatus
    reason: str
    references: list[str]


class AssessOutput(Contract):
    assessments: list[AssessmentItem]
    decision: DecisionKind
    decision_reason: str
    conditions: list[str]


class FinalOutput(Contract):
    summary: str
    assessments: list[AssessmentItem]
    decision: DecisionKind
    decision_reason: str
    conditions: list[str]


class ReportConflict(Contract):
    thema: str
    beschreibung: str
    beteiligte: list[str]
    refs: list[str]


class ReportCompromise(Contract):
    art: Literal["concession", "alternative", "compromise"]
    beschreibung: str
    refs: list[str]


class ReportOpenPoint(Contract):
    art: Literal["legal", "evidence", "ethical", "negotiation", "data"]
    punkt: str
    refs: list[str]


class ReportImpact(Contract):
    basis: Literal["evidence", "assumption", "prediction"]
    beschreibung: str
    refs: list[str]


class ReportOutput(Contract):
    einordnung: str
    verlauf_kurz: str
    konflikte: list[ReportConflict]
    kompromisse: list[ReportCompromise]
    konsens: str
    offene_punkte: list[ReportOpenPoint]
    auswirkungen: list[ReportImpact]


class EvolutionOutput(Contract):
    narrative: str
    refs: list[str]


class MetaAnswer(Contract):
    antwort: str
    refs: list[str]            # ids from the debate record that support the answer
    keine_grundlage: bool      # true if the record does not answer the question


class ConnectionProbe(Contract):
    antwort: str


def strict_schema(model):
    """Pydantic JSON schema -> Responses strict schema (all required, no extras)."""
    schema = model.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            node.pop("description", None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


def schema_depth(schema):
    """Object nesting depth after resolving $ref (Azure allows five levels)."""
    defs = schema.get("$defs", {})

    def depth(node, seen):
        if not isinstance(node, dict):
            return 0
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return 0 if name in seen else depth(defs[name], seen | {name})
        best = 0
        for option in node.get("anyOf", []):
            best = max(best, depth(option, seen))
        if "items" in node:
            best = max(best, depth(node["items"], seen))
        if "properties" in node:
            best = max(best, 1 + max((depth(v, seen) for v in node["properties"].values()), default=0))
        return best

    return depth(schema, frozenset())


def schema_property_count(schema):
    """Total object properties, counting each definition once."""
    total = len(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        total += len(definition.get("properties", {}))
    return total
