"""Versioned objects exchanged between the runtime and business flows."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessLine(IntEnum):
    DOCUMENTS = 1
    FINANCIAL_REPORT = 2
    INDUSTRY_IMPACT = 3
    VALUATION = 4
    REPORT_REVIEW = 5
    INVESTMENT_MEMO = 6
    REPORT_QUALITY = 7


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkflowNodeStatus(str, Enum):
    """Durable execution state for one idempotent workflow node."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class FindingVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"


class WorkflowNodeSpec(StrictModel):
    node_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    depends_on: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=10)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    idempotency_key: str = Field(min_length=1, max_length=200)


class WorkflowNodeRecord(StrictModel):
    workflow_id: str
    node_id: str
    status: WorkflowNodeStatus
    spec: WorkflowNodeSpec
    attempt_count: int = Field(default=0, ge=0)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReviewFinding(StrictModel):
    id: str
    workflow_id: str
    node_id: str
    reviewer: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    verdict: FindingVerdict
    severity: Literal["info", "warning", "error"]
    reason: str = Field(min_length=1)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    suggested_action: Literal["continue", "retry", "escalate", "block"]
    resolved_at: datetime | None = None


class FactNature(str, Enum):
    DISCLOSED = "disclosed"
    CALCULATED = "calculated"
    OPINION = "opinion"
    ASSUMPTION = "assumption"


class DataClass(str, Enum):
    PUBLIC = "public"
    RESTRICTED = "restricted"


class TaskSpec(StrictModel):
    line: BusinessLine
    sources: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    as_of: date
    question: str = ""
    max_model_calls: int = Field(default=2, ge=0, le=20)
    source_data_class: DataClass = DataClass.RESTRICTED
    corpus_task_id: str | None = None
    expected_output_fields: list[str] = Field(default_factory=list)
    network_policy: str = "offline"

    @model_validator(mode="after")
    def needs_source(self) -> TaskSpec:
        if not self.sources:
            raise ValueError("at least one source path or document ID is required")
        return self


class FinancialObservation(StrictModel):
    field: str = Field(min_length=1)
    period_end: date
    period_kind: str = Field(pattern="^(FY|H1|Q1|Q2|Q3|Q4)$")
    value: Decimal
    unit: str = Field(default="CNY", min_length=1)
    scope: str = Field(default="consolidated", pattern="^(consolidated|parent)$")


class FinancialInput(StrictModel):
    schema_id: str = Field(alias="schema", pattern="^finagent.financial.v1$")
    entity_id: str = Field(min_length=1)
    published_at: date
    observations: list[FinancialObservation] = Field(min_length=1)


class VerifiedFinancialObservation(FinancialObservation):
    """A financial field approved for analytical use with source-level evidence."""

    locator: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    block_type: str | None = None
    verification_status: Literal[
        "human_verified",
        "visual_spotchecked_against_original",
        "source_system_verified",
    ]
    confidence: Literal["high", "medium", "low"] = "high"
    confidence_reason: str = Field(min_length=1)


class VerifiedFinancialInput(StrictModel):
    """Curated facts emitted by a parser-review or human-review workflow."""

    schema_id: str = Field(alias="schema", pattern="^finagent.verified-financial.v1$")
    entity_id: str = Field(min_length=1)
    published_at: date
    observations: list[VerifiedFinancialObservation] = Field(min_length=1)


class SourceDocument(StrictModel):
    id: str
    name: str
    sha256: str
    media_type: str
    published_at: date | None = None
    ingested_at: datetime
    data_class: DataClass = DataClass.PUBLIC
    object_path: str
    source_url: str | None = None
    source_tier: str | None = None
    retrieved_at: datetime | None = None
    version: str = "1"
    is_synthetic: bool = False
    corpus_document_id: str | None = None


class EvidenceSpan(StrictModel):
    document_id: str
    locator: str
    excerpt: str
    external_id: str | None = None
    page_number: int | None = None
    block_type: str | None = None
    verification_status: str | None = None


class ExtractionCandidate(StrictModel):
    """Parser output awaiting mapping and evidence review before analytical use."""

    id: str
    document_id: str
    label: str = Field(min_length=1)
    raw_value: str = Field(min_length=1)
    field_hint: str | None = None
    unit_hint: str | None = None
    period_hint: str | None = None
    period_kind_hint: str | None = Field(default=None, pattern="^(FY|H1|Q1|Q2|Q3|Q4)$")
    scope_hint: str | None = None
    evidence: EvidenceSpan
    parser: str
    status: Literal["candidate"] = "candidate"
    confidence: Literal["high", "medium", "low"] = "low"
    confidence_reason: str = Field(min_length=1)


class XbrlTaxonomyEntry(StrictModel):
    namespace_uri: str = Field(min_length=1)
    local_name: str = Field(min_length=1)
    field: str = Field(min_length=1)
    unit: str | None = Field(default=None, min_length=1)
    scope: Literal["consolidated", "parent"] | None = None
    period_type: Literal["duration", "instant"] | None = None
    period_kind_hint: Literal["FY", "H1", "Q1", "Q2", "Q3", "Q4"] | None = None


class XbrlTaxonomyMap(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^finagent.xbrl-taxonomy-map.v1$")
    mapping_id: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    entries: list[XbrlTaxonomyEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def concept_keys_are_unique(self) -> "XbrlTaxonomyMap":
        keys = [(entry.namespace_uri, entry.local_name) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("namespace_uri and local_name pairs must be unique")
        return self


class CandidateApproval(StrictModel):
    candidate_id: str
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    normalized_value: Decimal
    unit: str = Field(min_length=1)
    period_end: date
    period_kind: str = Field(pattern="^(FY|H1|Q1|Q2|Q3|Q4)$")
    scope: str = Field(pattern="^(consolidated|parent)$")
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    review_reason: str = Field(min_length=1)


class CandidateReviewBundle(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^finagent.candidate-review.v1$")
    review_id: str = Field(min_length=1)
    approvals: list[CandidateApproval] = Field(min_length=1)

    @model_validator(mode="after")
    def candidate_ids_are_unique(self) -> "CandidateReviewBundle":
        candidate_ids = [approval.candidate_id for approval in self.approvals]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_id values must be unique within a review bundle")
        return self


class FactResolution(StrictModel):
    """An auditable reviewer choice for a conflicting financial fact key."""

    schema_id: str = Field(alias="schema", pattern=r"^finagent.fact-resolution.v1$")
    resolution_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    period_end: date
    period_kind: str = Field(pattern="^(FY|H1|Q1|Q2|Q3|Q4)$")
    scope: str = Field(pattern="^(consolidated|parent)$")
    unit: str = Field(min_length=1)
    preferred_fact_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    resolved_at: datetime
    reason: str = Field(min_length=1)


class OcrJob(StrictModel):
    id: str
    document_id: str
    page_number: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1)
    status: Literal["queued", "processing", "completed", "failed"] = "queued"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    error: str | None = None


class OcrTextSpan(StrictModel):
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=100)
    bounding_box: tuple[int, int, int, int] | None = None


class OcrTranscript(StrictModel):
    """Immutable OCR output that remains below the verified-fact layer."""

    schema_id: str = Field(alias="schema", pattern=r"^finagent.ocr-transcript.v1$")
    id: str
    job_id: str
    document_id: str
    page_number: int | None = Field(default=None, ge=1)
    engine: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    language: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    text: str
    mean_confidence: float | None = Field(default=None, ge=0, le=100)
    spans: list[OcrTextSpan] = Field(default_factory=list)


class Fact(StrictModel):
    id: str
    document_id: str
    entity_id: str
    field: str
    value: Decimal
    unit: str
    period_end: date
    period_kind: str
    scope: str
    published_at: date
    nature: FactNature = FactNature.DISCLOSED
    evidence: EvidenceSpan
    raw_value: str | None = None
    confidence: str = "unassessable"
    confidence_reason: str = "not assessed"
    verification_status: str = "unverified"
    source_information_type: str | None = None


class Metric(StrictModel):
    name: str
    value: Decimal | None
    unit: str
    formula: str
    input_fact_ids: list[str]
    status: str
    reason: str | None = None
    input_document_ids: list[str] = Field(default_factory=list)
    confidence: str = "unassessable"
    confidence_reason: str = "not assessed"


class Claim(StrictModel):
    text: str = Field(min_length=1)
    document_id: str
    locator: str = Field(min_length=1)
    nature: FactNature


class ResearchAnswer(StrictModel):
    summary: str
    claims: list[Claim]
    limitations: list[str] = Field(default_factory=list)


class FlowResult(StrictModel):
    status: RunStatus
    payload: dict[str, Any]
    limitations: list[str] = Field(default_factory=list)


class RunRecord(StrictModel):
    id: str
    task: TaskSpec
    status: RunStatus
    source_ids: list[str]
    created_at: datetime
    finished_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
