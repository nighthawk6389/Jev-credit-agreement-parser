"""Core extraction record types.

The invariant that makes this pipeline auditable lives here: a field that
carries a value must carry at least one span into the normalized text. No
span, no value -- the record fails construction rather than silently
shipping an unprovenanced number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .conditions import parse_condition

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

FieldStatus = Literal[
    "confirmed",                  # extracted and Jev-validated
    "conflicted",                 # passes disagreed, unresolved
    "absent_from_document",       # affirmatively checked, genuinely not there
    "external_reference",         # value lives in a document we don't have
    "needs_review",               # below calibrated threshold
    "not_applicable_to_archetype",  # this deal kind has no such term by design
]

#: Statuses that are acceptable terminal states for a field with no value.
#: ``null`` alone is never one of them (see the project's non-negotiables).
RESOLVED_NULL_STATES: frozenset[str] = frozenset(
    {"absent_from_document", "external_reference", "needs_review", "conflicted",
     "not_applicable_to_archetype"}
)

#: Why a magnitude is not in this document. These are different facts and
#: conflating them corrupts a corpus: the first is a deal characteristic, the
#: second is an artifact of how the document was filed.
ExternalKind = Literal[
    "by_design",            # unobtainable in principle; a real deal term
    "omitted_from_filing",  # obtainable from the borrower; a source artifact
]

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
    #: Which document in the set this offset space belongs to. Offsets are only
    #: meaningful relative to one document, so a span that travels between
    #: documents without this is a citation into the wrong text.
    document_id: str | None = None

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


class Condition(BaseModel):
    """A restriction on when a variant governs.

    ``expr`` is written in the restricted grammar in
    :mod:`credit_extract.models.conditions` and is *parsed*, never evaluated as
    Python. An expression that does not parse fails construction here rather
    than at resolution time, which is when nobody is watching.
    """

    model_config = ConfigDict(validate_assignment=True)

    kind: Literal["temporal", "state", "event"]
    expr: str
    source_spans: list[Span] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expression_parses(self) -> "Condition":
        parse_condition(self.expr)       # raises ConditionSyntaxError
        return self

    def evaluate(self, state: dict[str, Any]) -> bool | None:
        """Three-valued: ``None`` means the state does not say."""
        return parse_condition(self.expr).evaluate(state)

    def variables(self) -> set[str]:
        return parse_condition(self.expr).variables()

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.expr


class Variant(BaseModel, Generic[T]):
    """One value of a field, and the window and state in which it governs.

    A credit agreement term is rarely a scalar. "4.50x from March 2021",
    "converts to Senior Secured Net Leverage if junior debt exceeds $25M" and
    "applies only after an IPO" are all one field with several variants, and a
    model that stores a single number cannot represent any of them.
    """

    model_config = ConfigDict(validate_assignment=True)

    value: T | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    #: ALL conditions must hold. An empty list means unconditional.
    conditions: list[Condition] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)
    status: FieldStatus = "needs_review"

    # -- provenance and confidence ------------------------------------------
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Which validator produced ``validation_confidence``. Different validators
    #: answer different questions and their probabilities are not on a common
    #: scale, so a threshold fitted on one cannot be applied to another.
    validation_source: str | None = None
    notes: str | None = None
    trace: list[ValidationEvent] = Field(default_factory=list)
    #: Populated when status == "external_reference".
    external_document: str | None = None
    external_kind: ExternalKind | None = None
    #: Number of independent extraction passes that produced this value.
    pass_support: int = 0
    #: Competing candidates when status == "conflicted".
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    #: Structured modifiers that change what the value means -- e.g. which
    #: EBITDA base a percentage basket is measured against. A basket quoted as
    #: a percentage of EBITDA means materially different things pre- and
    #: post-add-back, so the distinction is carried, not flattened.
    qualifiers: dict[str, str] = Field(default_factory=dict)
    #: Normalized quantity, when the value is numeric. Carries the unit.
    quantity: Any = None

    @model_validator(mode="after")
    def _provenance_is_mandatory(self) -> "Variant[T]":
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
        if self.effective_from and self.effective_to:
            if self.effective_to < self.effective_from:
                raise ValueError(
                    f"effective_to {self.effective_to} precedes effective_from "
                    f"{self.effective_from}"
                )
        return self

    @property
    def confidence(self) -> float:
        """The score that governs triage: validation if it ran, else extraction."""
        return (
            self.validation_confidence
            if self.validation_confidence is not None
            else self.extraction_confidence
        )

    @property
    def unconditional(self) -> bool:
        return not self.conditions

    @property
    def undated(self) -> bool:
        return self.effective_from is None and self.effective_to is None

    def applies_at(self, at: date | None) -> bool:
        if at is None or self.undated:
            return True
        if self.effective_from and at < self.effective_from:
            return False
        if self.effective_to and at > self.effective_to:
            return False
        return True

    def conditions_hold(self, state: dict[str, Any]) -> bool | None:
        """``None`` when the state does not determine it."""
        verdicts = [condition.evaluate(state) for condition in self.conditions]
        if any(v is False for v in verdicts):
            return False
        return None if any(v is None for v in verdicts) else True

    def governs(self, at: date | None, state: dict[str, Any]) -> bool | None:
        if not self.applies_at(at):
            return False
        return self.conditions_hold(state)

    def record(self, event: ValidationEvent) -> None:
        self.trace = [*self.trace, event]

    @property
    def is_resolved(self) -> bool:
        if self.value is not None:
            return self.status in ("confirmed", "needs_review", "conflicted",
                                   "external_reference")
        return self.status in RESOLVED_NULL_STATES


class ResolutionStep(BaseModel):
    """Why one variant was or was not selected, for the audit trail."""

    index: int
    verdict: Literal["governs", "out_of_window", "condition_false",
                     "undeterminable"]
    detail: str = ""


class ExtractedField(BaseModel, Generic[T]):
    """A field: one or more variants, ordered by precedence, highest first.

    Most fields have exactly one unconditional variant, and the delegating
    properties below make that case read exactly as it did before variants
    existed. The storage model is nonetheless the list: a field that turns out
    to step down over time, or to convert on a trigger, gains a variant rather
    than needing a different type.
    """

    model_config = ConfigDict(validate_assignment=True)

    standard_term: str | None = None
    variants: list[Variant[T]] = Field(default_factory=list)
    #: Why the variants are in this order -- cite the clause that establishes
    #: precedence. An ordering nobody can justify is a guess.
    precedence_basis: str | None = None

    field_class: FieldClass = "economic_terms"
    criticality: int = Field(default=3, ge=1, le=5)
    #: Set when status is not_applicable_to_archetype: which archetype, and why.
    archetype_note: str | None = None

    @model_validator(mode="after")
    def _at_least_one_variant(self) -> "ExtractedField[T]":
        if not self.variants:
            self.variants = [Variant[Any]()]
        return self

    # -- construction --------------------------------------------------------

    @classmethod
    def single(cls, **kwargs: Any) -> "ExtractedField[T]":
        """Build the common single-variant field from variant keyword args."""
        field_keys = {"standard_term", "field_class", "criticality",
                      "precedence_basis", "archetype_note"}
        field_args = {k: v for k, v in kwargs.items() if k in field_keys}
        variant_args = {k: v for k, v in kwargs.items() if k not in field_keys}
        return cls(variants=[Variant[Any](**variant_args)], **field_args)

    # -- resolution ----------------------------------------------------------

    def resolve(
        self, at: date | None = None, state: dict[str, Any] | None = None
    ) -> Variant[T] | None:
        """The variant governing at ``at`` given ``state``, or None.

        Returns None when nothing governs *or* when the state is too
        incomplete to tell. ``resolve_trace`` says which of the two it was,
        because "no such term applies" and "you did not tell me whether the
        IPO happened" are different answers.
        """
        state = state or {}
        for variant in self.variants:
            if variant.governs(at, state) is True:
                return variant
        return None

    def resolve_trace(
        self, at: date | None = None, state: dict[str, Any] | None = None
    ) -> list[ResolutionStep]:
        state = state or {}
        steps: list[ResolutionStep] = []
        for index, variant in enumerate(self.variants):
            if not variant.applies_at(at):
                steps.append(ResolutionStep(
                    index=index, verdict="out_of_window",
                    detail=f"window {variant.effective_from}..{variant.effective_to}",
                ))
                continue
            held = variant.conditions_hold(state)
            if held is True:
                steps.append(ResolutionStep(index=index, verdict="governs"))
            elif held is False:
                steps.append(ResolutionStep(
                    index=index, verdict="condition_false",
                    detail="; ".join(c.expr for c in variant.conditions),
                ))
            else:
                missing = sorted(
                    v for c in variant.conditions for v in c.variables()
                    if v not in state
                )
                steps.append(ResolutionStep(
                    index=index, verdict="undeterminable",
                    detail=f"state does not supply {missing}",
                ))
        return steps

    def required_state(self) -> set[str]:
        """Every state variable any variant's conditions depend on."""
        return {
            name
            for variant in self.variants
            for condition in variant.conditions
            for name in condition.variables()
        }

    # -- primary-variant delegation -----------------------------------------
    #
    # Sugar for the single-variant case. The list remains the storage.

    @property
    def primary(self) -> Variant[T]:
        return self.variants[0]

    @property
    def is_conditional(self) -> bool:
        return len(self.variants) > 1 or bool(self.primary.conditions)

    @property
    def value(self) -> T | None:
        return self.primary.value

    @value.setter
    def value(self, new: T | None) -> None:
        self.primary.value = new

    @property
    def spans(self) -> list[Span]:
        return self.primary.spans

    @spans.setter
    def spans(self, new: list[Span]) -> None:
        self.primary.spans = new

    @property
    def status(self) -> FieldStatus:
        return self.primary.status

    @status.setter
    def status(self, new: FieldStatus) -> None:
        self.primary.status = new

    @property
    def extraction_confidence(self) -> float:
        return self.primary.extraction_confidence

    @property
    def validation_confidence(self) -> float | None:
        return self.primary.validation_confidence

    @validation_confidence.setter
    def validation_confidence(self, new: float | None) -> None:
        self.primary.validation_confidence = new

    @property
    def validation_source(self) -> str | None:
        return self.primary.validation_source

    @validation_source.setter
    def validation_source(self, new: str | None) -> None:
        self.primary.validation_source = new

    @property
    def notes(self) -> str | None:
        return self.primary.notes

    @notes.setter
    def notes(self, new: str | None) -> None:
        self.primary.notes = new

    @property
    def external_document(self) -> str | None:
        return self.primary.external_document

    @external_document.setter
    def external_document(self, new: str | None) -> None:
        self.primary.external_document = new

    @property
    def external_kind(self) -> ExternalKind | None:
        return self.primary.external_kind

    @external_kind.setter
    def external_kind(self, new: ExternalKind | None) -> None:
        self.primary.external_kind = new

    @property
    def qualifiers(self) -> dict[str, str]:
        return self.primary.qualifiers

    @property
    def quantity(self) -> Any:
        return self.primary.quantity

    @quantity.setter
    def quantity(self, new: Any) -> None:
        self.primary.quantity = new

    @property
    def trace(self) -> list[ValidationEvent]:
        return self.primary.trace

    @property
    def pass_support(self) -> int:
        return self.primary.pass_support

    @property
    def alternatives(self) -> list[dict[str, Any]]:
        return self.primary.alternatives

    @alternatives.setter
    def alternatives(self, new: list[dict[str, Any]]) -> None:
        self.primary.alternatives = new

    @property
    def is_resolved(self) -> bool:
        return all(variant.is_resolved for variant in self.variants)

    @property
    def criticality_label(self) -> str:
        return CRITICALITY_RUBRIC[self.criticality - 1]

    def record(self, event: ValidationEvent) -> None:
        self.primary.record(event)


# Concrete aliases used across the pipeline. Pydantic v2 resolves these into
# distinct concrete models, so JSON schema stays precise per field type.
MoneyField = ExtractedField[Decimal]
RateField = ExtractedField[Decimal]
DateField = ExtractedField[str]        # ISO-8601; parsed in Python, never by a model
TextField = ExtractedField[str]
IntField = ExtractedField[int]
BoolField = ExtractedField[bool]


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
    #: The amendment chain: what was in the set, what each amendment did, and
    #: what could not be applied. Absent for a single-document run.
    chain: dict[str, Any] = Field(default_factory=dict)
    archetype: dict[str, Any] = Field(default_factory=dict)
    #: Per-family coverage and the blind-spot register, rendered.
    coverage: str = ""
    blind_spots: str = ""
    cost: CostLedger = Field(default_factory=CostLedger)
    thresholds_version: str | None = None
    notes: list[str] = Field(default_factory=list)
