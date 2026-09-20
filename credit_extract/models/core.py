"""Core extraction record types.

The invariant that makes this pipeline auditable lives here: a field that
carries a value must carry at least one span into the normalized text. No
span, no value -- the record fails construction rather than silently
shipping an unprovenanced number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

FieldStatus = Literal[
    "confirmed",              # extracted and Jev-validated
    "conflicted",             # passes disagreed, unresolved
    "absent_from_document",   # affirmatively checked, genuinely not there
    "external_reference",     # value lives in a document we don't have
    "needs_review",           # below calibrated threshold
]

#: Statuses that are acceptable terminal states for a field with no value.
#: ``null`` alone is never one of them (see the project's non-negotiables).
RESOLVED_NULL_STATES: frozenset[str] = frozenset(
    {"absent_from_document", "external_reference", "needs_review", "conflicted"}
)

#: Coarse buckets used to fit separate confidence thresholds per field class.
FieldClass = Literal[
    "economic_terms",
    "dates",
    "covenant_levels",
    "baskets",
    "parties",
    "administrative",
]

CRITICALITY_RUBRIC: tuple[str, ...] = (
    "cosmetic",        # 1 - notice addresses, defined-term casing
    "administrative",  # 2 - mechanics with no cash impact
    "material",        # 3 - moves cash in an edge case
    "economic",        # 4 - moves cash in the base case
    "deal_defining",   # 5 - pricing, maturity, principal, leverage covenant
)


class Span(BaseModel):
    """A half-open ``[start, end)`` slice of the normalized character space."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str
    section_id: str | None = None
    page: int | None = None
    #: Which segmentation surfaced this span, when known.
    segmentation: str | None = None

    @model_validator(mode="after")
    def _check_bounds(self) -> "Span":
        if self.end <= self.start:
            raise ValueError(f"span end {self.end} must exceed start {self.start}")
        return self

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end

    def jaccard(self, other: "Span") -> float:
        """Character-level overlap, used to decide whether two passes agree."""
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        inter = max(0, hi - lo)
        union = (self.end - self.start) + (other.end - other.start) - inter
        return inter / union if union else 0.0

    def expand(self, pad: int, doc_len: int) -> tuple[int, int]:
        return max(0, self.start - pad), min(doc_len, self.end + pad)


class ValidationEvent(BaseModel):
    """One question asked of one validator, recorded for the audit trail."""

    validator: str                     # "A_span_support", "C_negative_space", ...
    question: str
    jev_type: Literal["noul", "choice", "score", "python", "llm"] = "noul"
    result: Any = None
    probability: float | None = None
    threshold: float | None = None
    passed: bool | None = None
    cost_usd: float = 0.0
    backend: str = "offline"
    notes: str | None = None


class ExtractedValue(BaseModel, Generic[T]):
    """A single extracted field, with provenance and both confidence scores."""

    model_config = ConfigDict(validate_assignment=True)

    value: T | None = None
    spans: list[Span] = Field(default_factory=list)
    standard_term: str | None = None
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: FieldStatus = "needs_review"
    notes: str | None = None
    #: Which validator produced ``validation_confidence``. Different validators
    #: answer different questions and their probabilities are not on a common
    #: scale, so a threshold fitted on one cannot be applied to another.
    validation_source: str | None = None

    # --- triage / audit metadata -------------------------------------------
    field_class: FieldClass = "economic_terms"
    criticality: int = Field(default=3, ge=1, le=5)
    trace: list[ValidationEvent] = Field(default_factory=list)
    #: Populated when status == "external_reference".
    external_document: str | None = None
    #: Number of independent extraction passes that produced this value.
    pass_support: int = 0
    #: Competing candidates when status == "conflicted".
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    #: Structured modifiers that change what the value means -- e.g. which
    #: EBITDA base a percentage basket is measured against. A basket quoted as
    #: a percentage of EBITDA means materially different things pre- and
    #: post-add-back, so the distinction is carried, not flattened.
    qualifiers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _provenance_is_mandatory(self) -> "ExtractedValue[T]":
        if self.value is not None and not self.spans:
            raise ValueError(
                "a field with a value must cite at least one span; "
                f"got value={self.value!r} with no spans"
            )
        if self.status == "absent_from_document" and self.value is not None:
            raise ValueError("absent_from_document cannot carry a value")
        if self.status == "external_reference" and not self.external_document:
            raise ValueError(
                "external_reference must name the document the value lives in"
            )
        return self

    @property
    def is_resolved(self) -> bool:
        """True when this field is a legitimate terminal state."""
        if self.value is not None:
            return self.status in ("confirmed", "needs_review", "conflicted",
                                   "external_reference")
        return self.status in RESOLVED_NULL_STATES

    @property
    def criticality_label(self) -> str:
        return CRITICALITY_RUBRIC[self.criticality - 1]

    def record(self, event: ValidationEvent) -> None:
        self.trace = [*self.trace, event]


# Concrete aliases used across the pipeline. Pydantic v2 resolves these into
# distinct concrete models, so JSON schema stays precise per field type.
MoneyField = ExtractedValue[Decimal]
RateField = ExtractedValue[Decimal]
DateField = ExtractedValue[str]        # ISO-8601; parsed in Python, never by a model
TextField = ExtractedValue[str]
IntField = ExtractedValue[int]
BoolField = ExtractedValue[bool]


class InvariantViolation(BaseModel):
    """A deterministic check that failed. Never produced by a model."""

    invariant: str
    severity: Literal["error", "warning"] = "error"
    message: str
    fields: list[str] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)
    observed: Any = None
    expected: Any = None

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{self.severity}] {self.invariant}: {self.message}"


class OrphanChunk(BaseModel):
    """Text that scored high on the orphan sweep but produced no fields."""

    chunk_id: str
    span: Span
    text: str
    signals: dict[str, float] = Field(default_factory=dict)
    top_signal: str | None = None
    score: float = 0.0
    resolution: Literal["unreviewed", "rescued", "benign"] = "unreviewed"
    rescued_fields: list[str] = Field(default_factory=list)


class ConflictRecord(BaseModel):
    field: str
    candidates: list[dict[str, Any]]
    jev_distribution: dict[str, float] = Field(default_factory=dict)
    resolved_to: Any = None
    resolved: bool = False


class CostLedger(BaseModel):
    """Every tier of the escalation ladder reports its spend here."""

    deterministic_calls: int = 0
    jev_requests: int = 0
    jev_questions: int = 0
    jev_input_tokens: int = 0
    jev_cost_usd: float = 0.0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return round(self.jev_cost_usd + self.llm_cost_usd, 6)

    def merge(self, other: "CostLedger") -> None:
        for name in self.__class__.model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class DocumentReport(BaseModel):
    """Document-level output: what we know, and what we know we don't."""

    document_id: str
    source_path: str
    normalized_chars: int = 0
    coverage_pct: float = 0.0
    fields_total: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    invariant_violations: list[InvariantViolation] = Field(default_factory=list)
    orphan_chunks: list[OrphanChunk] = Field(default_factory=list)
    unresolved_conflicts: list[ConflictRecord] = Field(default_factory=list)
    external_references: list[dict[str, Any]] = Field(default_factory=list)
    #: Provisions found to displace an earlier one governing the same quantity.
    #: A term that is correct in isolation and wrong in context is invisible
    #: without this, so it is reported whether or not it changed a field.
    override_findings: list[dict[str, Any]] = Field(default_factory=list)
    review_queue: list[dict[str, Any]] = Field(default_factory=list)
    definition_graph_stats: dict[str, Any] = Field(default_factory=dict)
    cost: CostLedger = Field(default_factory=CostLedger)
    thresholds_version: str | None = None
    notes: list[str] = Field(default_factory=list)
