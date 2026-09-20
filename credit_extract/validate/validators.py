"""Validators A-F.

Ordered by what they are worth, not by the alphabet:

* **B, the orphan sweep**, is the highest-value mechanism here. Most pipelines
  are recall-limited: they ask "where is field X?" and if X lives somewhere
  unexpected it is silently missing. B inverts the question -- it asks every
  chunk whether it says something the extraction does not capture. Text that
  answers yes while contributing no fields is an orphan.
* **C, negative space**, is what turns a ``null`` into a finding. "The document
  is silent" and "we failed to find it" look identical in the output of every
  pipeline that reports ``null``, and telling them apart is most of the
  long-tail problem.
* **D, override detection**, catches provisions that are correct in isolation
  and wrong in context. Credit agreements are full of ``notwithstanding``.
* **E, external dependency**, forces ``external_reference`` where a magnitude
  lives in a document nobody has.
* **A, span support**, is the basic gate.
* **F, criticality**, is triage: it decides whose review time gets spent.

All arithmetic, date comparison and counting stay in Python. Jev is asked only
for judgements about what text says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..models.core import (
    CRITICALITY_RUBRIC, ConflictRecord, ExtractedValue, OrphanChunk, Span,
    ValidationEvent,
)
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from .calibrate import Thresholds
from .jev import ChoiceQ, Decision, JevSession, Noul, Question, ScoreQ

#: Context either side of a cited span for validator A.
SPAN_CONTEXT_PAD = 500

#: Orphan sweep statements, asked of every chunk in one batched request.
ORPHAN_SIGNALS: dict[str, str] = {
    "payment_obligation":
        "This text creates a payment obligation not captured in the attached "
        "summary.",
    "restriction":
        "This text imposes a restriction on the borrower not captured in the "
        "summary.",
    "override":
        "This text modifies or overrides a term defined elsewhere in the "
        "agreement.",
    "threshold":
        "This text contains a numeric threshold, cap, or basket.",
    "schedule":
        "This text contains a date or schedule.",
}

#: A chunk must clear this on some signal to be considered an orphan at all.
ORPHAN_SIGNAL_FLOOR = 0.55

#: Chunks shorter than this are headings and cross-references, not provisions.
#: Sweeping them produces orphans that are real text but cannot carry a term.
ORPHAN_MIN_CHARS = 200


@dataclass
class ValidationContext:
    """Everything the validators read and write."""

    doc: NormalizedDocument
    fields: dict[str, ExtractedValue]
    chunks: list[Chunk]
    session: JevSession
    thresholds: Thresholds
    #: chunk_id -> field names that chunk contributed. Drives the orphan sweep.
    chunk_contributions: dict[str, set[str]] = dc_field(default_factory=dict)
    specs: dict[str, FieldSpec] = dc_field(default_factory=lambda: FIELD_REGISTRY)
    orphans: list[OrphanChunk] = dc_field(default_factory=list)
    conflicts: list[ConflictRecord] = dc_field(default_factory=list)

    def threshold_for(self, name: str, validator: str = "A_span_support") -> float:
        spec = self.specs.get(name)
        return self.thresholds.for_class(
            spec.field_class if spec else "administrative", validator
        )


def _describe(field: ExtractedValue, spec: FieldSpec) -> str:
    value = field.value
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    elif value is None:
        text = "null"
    else:
        text = str(value)
    if spec.kind == "percent":
        text = f"{text}%"
    elif spec.kind == "money":
        text = f"${text}"
    return text


# ---------------------------------------------------------------------------
# Validator A -- span support
# ---------------------------------------------------------------------------


def validator_a_span_support(ctx: ValidationContext) -> int:
    """Does the cited text actually support the value?

    Fields whose citations land in the same neighbourhood share one state, so
    a clause carrying five extracted figures costs one request rather than
    five. State is the expensive part; questions are nearly free.
    """
    groups: dict[tuple[int, int], list[str]] = {}
    for name, field in ctx.fields.items():
        if field.status != "confirmed" or not field.spans:
            continue
        span = field.spans[0]
        lo, hi = span.expand(SPAN_CONTEXT_PAD, len(ctx.doc.text))
        placed = False
        for (glo, ghi), members in groups.items():
            if lo < ghi and glo < hi:
                merged = (min(glo, lo), max(ghi, hi))
                groups[merged] = groups.pop((glo, ghi)) + [name]
                placed = True
                break
        if not placed:
            groups[(lo, hi)] = [name]

    checked = 0
    for (lo, hi), names in groups.items():
        state = ctx.doc.slice(lo, hi)
        questions: list[Question] = []
        for name in names:
            spec = ctx.specs[name]
            questions.append(Noul(
                name=name,
                statement=(
                    f"The text supports a value of {_describe(ctx.fields[name], spec)} "
                    f"for {spec.description}."
                ),
            ))
        result = ctx.session.ask(state, questions, label="A_span_support")
        for name in names:
            decision = result.get(name)
            if decision is None:
                continue
            field = ctx.fields[name]
            threshold = ctx.threshold_for(name, "A_span_support")
            passed = decision.confidence >= threshold
            field.validation_confidence = decision.confidence
            field.validation_source = "A_span_support"
            field.record(ValidationEvent(
                validator="A_span_support",
                question=questions[names.index(name)].statement,
                jev_type="noul",
                probability=decision.confidence,
                threshold=threshold,
                passed=passed,
                backend=decision.backend,
            ))
            if not passed:
                field.status = "needs_review"
                field.notes = (
                    f"cited text supports the value at {decision.confidence:.2f}, "
                    f"below the {threshold:.2f} threshold for "
                    f"{ctx.specs[name].field_class}"
                )
            checked += 1
    return checked


# ---------------------------------------------------------------------------
# Validator B -- the orphan sweep
# ---------------------------------------------------------------------------


def validator_b_orphan_sweep(ctx: ValidationContext) -> list[OrphanChunk]:
    """Ask every chunk what it says that the extraction does not know about.

    Build this first and measure it: it is the only mechanism here that can
    find a field nobody thought to look for.
    """
    orphans: list[OrphanChunk] = []
    for chunk in ctx.chunks:
        if len(chunk.text.strip()) < ORPHAN_MIN_CHARS:
            continue
        questions: list[Question] = [
            Noul(name=key, statement=statement, concept=key)
            for key, statement in ORPHAN_SIGNALS.items()
        ]
        result = ctx.session.ask(chunk.text, questions, label="B_orphan_sweep")
        signals = {
            key: round(result[key].confidence, 4)
            for key in ORPHAN_SIGNALS
            if result.get(key) is not None
        }
        if not signals:
            continue
        contributed = ctx.chunk_contributions.get(chunk.chunk_id, set())
        top_signal = max(signals, key=signals.get)
        score = signals[top_signal]
        if contributed or score < ORPHAN_SIGNAL_FLOOR:
            continue
        orphans.append(OrphanChunk(
            chunk_id=chunk.chunk_id,
            span=chunk.span,
            text=chunk.text[:2000],
            signals=signals,
            top_signal=top_signal,
            score=score,
        ))
    orphans.sort(key=lambda o: -o.score)
    ctx.orphans = orphans
    return orphans


def rescue_orphans(
    ctx: ValidationContext,
    orphans: list[OrphanChunk],
    reread,
    graph=None,
) -> int:
    """Tier 4: targeted re-read of each orphan chunk.

    ``reread`` is a callable ``(chunk) -> list[Candidate]``. When none is
    available the orphans stay in the report as unreviewed rather than being
    quietly dropped, because an unexplained orphan is itself the finding.
    """
    rescued = 0
    by_id = {c.chunk_id: c for c in ctx.chunks}
    for orphan in orphans:
        chunk = by_id.get(orphan.chunk_id)
        if chunk is None:
            continue
        found = reread(chunk)
        if found:
            orphan.resolution = "rescued"
            orphan.rescued_fields = sorted({c.field for c in found})
            rescued += 1
    return rescued


# ---------------------------------------------------------------------------
# Validator C -- negative-space confirmation
# ---------------------------------------------------------------------------


def validator_c_negative_space(ctx: ValidationContext) -> dict[str, float]:
    """Affirmatively confirm absence, chunk by chunk, combined in Python.

    A claim about the whole agreement cannot fit in a 32k state, and Jev
    questions cannot chain, so the question is asked of every chunk and the
    conjunction is taken here: a field is absent only if no chunk carries it.
    Aggregation is arithmetic, which is exactly what stays out of the model.
    """
    pending = [
        name for name, field in ctx.fields.items()
        if field.value is None
        and field.status in ("needs_review", "conflicted")
        and not field.external_document
    ]
    if not pending:
        return {}

    # Lowest absence probability across chunks = strongest evidence of presence.
    min_absence: dict[str, float] = {name: 1.0 for name in pending}
    witness: dict[str, Span] = {}
    for chunk in ctx.chunks:
        if not chunk.text.strip():
            continue
        questions: list[Question] = [
            Noul(
                name=name,
                statement=ctx.specs[name].absence_statement.replace(
                    "This agreement", "This text"
                ),
                polarity="absence",
            )
            for name in pending
        ]
        result = ctx.session.ask(chunk.text, questions, label="C_negative_space")
        for name in pending:
            decision = result.get(name)
            if decision is None:
                continue
            if decision.confidence < min_absence[name]:
                min_absence[name] = decision.confidence
                witness[name] = chunk.span

    for name, probability in min_absence.items():
        field = ctx.fields[name]
        spec = ctx.specs[name]
        threshold = ctx.threshold_for(name, "C_negative_space")
        field.validation_source = "C_negative_space"
        field.record(ValidationEvent(
            validator="C_negative_space",
            question=spec.absence_statement,
            jev_type="noul",
            probability=probability,
            threshold=threshold,
            passed=probability >= threshold,
            backend=ctx.session.backend.name,
            notes="minimum absence probability across all chunks",
        ))
        if probability >= threshold:
            field.status = "absent_from_document"
            field.validation_confidence = probability
            field.notes = (
                f"affirmatively confirmed absent at {probability:.2f} across "
                f"{len(ctx.chunks)} chunks"
            )
        else:
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                f"absence not confirmed ({probability:.2f} < {threshold:.2f}); "
                "some passage appears to address this, so the extractor "
                "probably missed it -- escalate"
            )
            if name in witness:
                field.spans = [witness[name]]
                field.notes += (
                    f" (strongest signal near offset {witness[name].start})"
                )
    return min_absence


# ---------------------------------------------------------------------------
# Validator D -- override and conflict detection
# ---------------------------------------------------------------------------


class OverridePair(BaseModel):
    """Two provisions that appear to govern the same quantity."""

    subject: str
    first: Span
    second: Span
    probability: float = 0.0
    overrides: bool = False
    note: str = ""


#: Language that signals one provision displacing another.
OVERRIDE_MARKERS = (
    "notwithstanding", "shall be deemed", "in lieu of", "shall not apply",
    "for the avoidance of doubt", "provided that", "except as",
)


def find_override_candidates(
    doc: NormalizedDocument, subject: str, reach: int = 700, pad: int = 400
) -> list[OverridePair]:
    """Find provisions *introduced* by override language that govern ``subject``.

    Anchored on the marker, not on the subject. A window drawn around every
    mention of "Consolidated EBITDA" catches any "provided that" within a few
    hundred characters and reports three findings where there is one, because
    the definition is full of provisos that modify other things. A
    ``notwithstanding`` governs what follows it, so the candidate provision
    starts at the marker and has to mention the subject downstream of it.
    """
    mentions = doc.find_all(subject.replace(" ", r"\s+"))
    if len(mentions) < 2:
        return []
    pattern = "|".join(re.escape(marker) for marker in OVERRIDE_MARKERS)
    pairs: list[OverridePair] = []
    claimed: list[Span] = []
    for match in re.finditer(pattern, doc.text, re.IGNORECASE):
        start = match.start()
        end = min(len(doc.text), start + reach)
        window = doc.text[start:end]
        if subject.lower() not in window.lower():
            continue
        second = doc.span(start, end)
        if any(second.jaccard(existing) > 0.5 for existing in claimed):
            continue
        earlier = [m for m in mentions if m.end <= start]
        if not earlier:
            continue
        base = earlier[-1]
        lo, hi = base.expand(pad, len(doc.text))
        pairs.append(OverridePair(
            subject=subject, first=doc.span(lo, min(hi, start)), second=second
        ))
        claimed.append(second)
    return pairs


def validator_d_overrides(
    ctx: ValidationContext, subjects: Iterable[str]
) -> list[OverridePair]:
    """Ask whether the second provision displaces the first."""
    out: list[OverridePair] = []
    for subject in subjects:
        for pair in find_override_candidates(ctx.doc, subject):
            state = (
                f"FIRST PROVISION\n{pair.first.text}\n\n"
                f"SECOND PROVISION\n{pair.second.text}"
            )
            result = ctx.session.ask(
                state,
                [Noul(
                    name="override",
                    statement=(
                        "The second provision overrides the first with respect "
                        f"to {subject}."
                    ),
                )],
                label="D_override",
            )
            decision = result.get("override")
            if decision is None:
                continue
            pair.probability = decision.confidence
            pair.overrides = decision.confidence >= ctx.thresholds.default
            if pair.overrides:
                pair.note = (
                    f"the second provision governs {subject}; any value "
                    "computed from the first alone is wrong in context"
                )
            out.append(pair)
    return out


# ---------------------------------------------------------------------------
# Validator E -- external dependency
# ---------------------------------------------------------------------------


_EXTERNAL_HINTS = (
    "Sponsor Model", "Disclosure Letter", "as separately agreed",
    "Schedule", "Exhibit", "Annex",
)


def _sentence_window(doc: NormalizedDocument, span: Span, cap: int = 900) -> str:
    """The sentence the span sits in, not a fixed character window.

    Proximity is not dependence. A covenant grid that happens to sit within
    500 characters of "Indebtedness set forth on Schedule 6.01" does not get
    its magnitude from that schedule, and a fixed window cannot tell the
    difference. Sentence bounds can.
    """
    lo = max(0, span.start - cap)
    hi = min(len(doc.text), span.end + cap)
    before = doc.text.rfind(". ", lo, span.start)
    after = doc.text.find(". ", span.end, hi)
    start = before + 2 if before != -1 else lo
    end = after + 1 if after != -1 else hi
    return doc.text[start:end]


def validator_e_external_dependency(ctx: ValidationContext) -> list[str]:
    """Force ``external_reference`` where a magnitude lives outside the document.

    Two tiers. Python first: does the sentence carrying this figure cite
    something outside the agreement at all? Only then is Jev asked the question
    that costs money -- and it is asked with the candidate document named, so
    it is judging dependence rather than rediscovering a citation that a regex
    already found for free.
    """
    forced: list[str] = []
    for name, field in ctx.fields.items():
        spec = ctx.specs.get(name)
        if (
            not field.spans
            or spec is None
            or spec.kind not in ("money", "percent", "ratio")
            or field.status == "external_reference"
        ):
            continue
        state = _sentence_window(ctx.doc, field.spans[0])
        document = next(
            (h for h in _EXTERNAL_HINTS if h.lower() in state.lower()), None
        )
        if document is None:
            continue                         # tier 1 settled it, for free
        statement = (
            f"The magnitude of this limit depends on the {document}, a document "
            "not contained in this agreement."
        )
        result = ctx.session.ask(
            state, [Noul(name="external", statement=statement)],
            label="E_external_dependency",
        )
        decision = result.get("external")
        if decision is None:
            continue
        threshold = ctx.threshold_for(name, "E_external_dependency")
        field.record(ValidationEvent(
            validator="E_external_dependency",
            question=statement,
            jev_type="noul",
            probability=decision.confidence,
            threshold=threshold,
            passed=decision.confidence >= threshold,
            backend=decision.backend,
        ))
        if decision.confidence >= threshold:
            field.value = None
            field.external_document = document
            field.status = "external_reference"
            field.validation_confidence = decision.confidence
            field.validation_source = "E_external_dependency"
            field.notes = (
                f"magnitude is fixed by the {document}, which is not part of "
                "this agreement; the figure stated here is not the real cap"
            )
            forced.append(name)
    return forced


# ---------------------------------------------------------------------------
# Validator F -- criticality scoring for triage
# ---------------------------------------------------------------------------

CRITICALITY_RUBRIC_TEXT: list[str] = [
    "cosmetic: wording or formatting with no effect on any obligation",
    "administrative: mechanics or notice provisions with no cash impact",
    "material: affects cash only in an edge case or on a contingency",
    "economic: affects cash in the ordinary course of the deal",
    "deal defining: pricing, maturity, principal, or the financial covenant",
]


def validator_f_criticality(ctx: ValidationContext) -> dict[str, int]:
    """Rank the review queue by what moves money."""
    scored: dict[str, int] = {}
    for name, field in ctx.fields.items():
        if field.status != "needs_review":
            continue
        spec = ctx.specs.get(name)
        if spec is None:
            continue
        span = field.spans[0] if field.spans else None
        state = (
            ctx.doc.slice(*span.expand(SPAN_CONTEXT_PAD, len(ctx.doc.text)))
            if span else spec.description
        )
        result = ctx.session.ask(
            state,
            [ScoreQ(
                name="criticality",
                question=(
                    f"How economically material is {spec.description} to this "
                    "credit agreement?"
                ),
                rubric=CRITICALITY_RUBRIC_TEXT,
            )],
            label="F_criticality",
        )
        decision = result.get("criticality")
        if decision is None or decision.score is None:
            continue
        # The registry's own criticality is a floor: a deal-defining field does
        # not become administrative because one clause reads quietly.
        field.criticality = max(field.criticality, decision.score)
        field.record(ValidationEvent(
            validator="F_criticality",
            question="economic materiality",
            jev_type="score",
            result=CRITICALITY_RUBRIC[field.criticality - 1],
            probability=None,
            backend=decision.backend,
        ))
        scored[name] = field.criticality
    return scored


# ---------------------------------------------------------------------------
# Conflict resolution via choice
# ---------------------------------------------------------------------------


def resolve_conflicts(ctx: ValidationContext) -> list[ConflictRecord]:
    """Put each unresolved conflict to Jev as a choice over the candidates."""
    resolved: list[ConflictRecord] = []
    for record in ctx.conflicts:
        field = ctx.fields.get(record.field)
        if field is None or field.status != "conflicted":
            continue
        criteria: dict[str, str] = {}
        state_parts: list[str] = []
        for alternative in record.candidates:
            key = str(alternative["value"])
            span = alternative.get("span") or {}
            text = span.get("text", "")
            criteria[key] = f"the text supports {key}"
            if text:
                state_parts.append(f"[{key}] {text}")
        if len(criteria) < 2:
            continue
        spec = ctx.specs.get(record.field)
        result = ctx.session.ask(
            "\n\n".join(state_parts),
            [ChoiceQ(
                name="pick",
                question=(
                    "Which value does the cited text support for "
                    f"{spec.description if spec else record.field}?"
                ),
                criteria=criteria,
            )],
            label="conflict_choice",
        )
        decision = result.get("pick")
        if decision is None:
            continue
        record.jev_distribution = decision.distribution
        threshold = ctx.threshold_for(record.field, "conflict_choice")
        field.record(ValidationEvent(
            validator="conflict_choice",
            question="which candidate value the text supports",
            jev_type="choice",
            result=decision.choice,
            probability=decision.confidence,
            threshold=threshold,
            passed=decision.confidence >= threshold,
            backend=decision.backend,
        ))
        if decision.confidence >= threshold:
            winner = next(
                (a for a in record.candidates if str(a["value"]) == decision.choice),
                None,
            )
            record.resolved = True
            record.resolved_to = decision.choice
            field.status = "needs_review" if winner is None else "confirmed"
            field.validation_confidence = decision.confidence
            field.validation_source = "conflict_choice"
            field.notes = (
                f"conflict resolved to {decision.choice} at "
                f"{decision.confidence:.2f}"
            )
        else:
            field.notes = (
                "passes disagreed and the distribution is flat "
                f"({decision.distribution}); unresolved"
            )
            resolved.append(record)
    return resolved
