"""Reconcile candidates from independent passes into one record per field.

The rules:

* passes that agree on both value and span produce a candidate ``confirmed``,
  which still has to survive Jev span-support validation before it is trusted;
* passes that disagree produce a ``conflicted`` field carrying every competing
  candidate, so the choice can be put to Jev with the competing spans as state;
* a value only one pass found is treated with suspicion, because single-pass
  discovery is a common false-positive mode -- unless the single pass is the
  deterministic table parser, which is a parse rather than a guess;
* a candidate naming a document outside the agreement resolves to
  ``external_reference`` and never to a number.

Nothing here decides a field is absent. Absence is a claim about the whole
document, so it belongs to the negative-space validator, not to a function that
has only seen the candidates that happened to be produced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..models.core import ConflictRecord, ExtractedField, Span
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from .passes import Candidate

#: Two spans this similar are treated as the same citation.
SPAN_AGREEMENT_THRESHOLD = 0.30

#: Pass ids from deterministic parsing, which need no corroboration.
DETERMINISTIC_PREFIX = "deterministic:"


@dataclass
class ValueGroup:
    """All candidates that agree on a value."""

    key: str
    value: Any
    candidates: list[Candidate]

    @property
    def segmentations(self) -> set[str]:
        """Independent views of the document that reached this value.

        A candidate that merely echoed a value it was shown does not count:
        the model tier is handed what the cheaper tiers found so that it can
        overturn a wrong rule, and the price of that is that agreement with
        the prior is no longer evidence. One wrong regex, shown to three
        passes, would otherwise arrive as three-way corroboration.

        An echo still contributes its span, its confidence and its value --
        it is excluded from *support*, not from the record.
        """
        return {c.segmentation for c in self.candidates if not c.echoes}

    @property
    def echoed_segmentations(self) -> set[str]:
        """Views that agreed with a value they were shown. Weak evidence."""
        return {c.segmentation for c in self.candidates if c.echoes}

    @property
    def overturned(self) -> bool:
        """True where some pass was shown a different value and rejected it.

        The reason for showing the prior in the first place, and worth
        knowing about a value: it survived a reader who had seen the
        alternative.
        """
        return any(c.overturns for c in self.candidates)

    @property
    def pass_ids(self) -> set[str]:
        return {c.pass_id for c in self.candidates}

    @property
    def deterministic(self) -> bool:
        return any(c.pass_id.startswith(DETERMINISTIC_PREFIX) for c in self.candidates)

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.confidence)

    @property
    def support(self) -> int:
        """Independent segmentations backing this value."""
        return len(self.segmentations)

    def spans(self, limit: int = 3) -> list[Span]:
        ordered = sorted(
            (c for c in self.candidates if c.span is not None),
            key=lambda c: -c.confidence,
        )
        kept: list[Span] = []
        for candidate in ordered:
            span = candidate.span
            if any(span.jaccard(k) > SPAN_AGREEMENT_THRESHOLD for k in kept):
                continue
            kept.append(span)
            if len(kept) >= limit:
                break
        return kept

    def span_agreement(self) -> float:
        """Mean pairwise span overlap; 1.0 when a single span is cited."""
        spans = [c.span for c in self.candidates if c.span is not None]
        if len(spans) < 2:
            return 1.0
        scores = [
            a.jaccard(b)
            for index, a in enumerate(spans)
            for b in spans[index + 1:]
        ]
        return sum(scores) / len(scores) if scores else 0.0


@dataclass
class Reconciliation:
    fields: dict[str, ExtractedField]
    conflicts: list[ConflictRecord]
    #: field -> chunk ids that contributed a candidate, for the orphan sweep.
    contributions: dict[str, set[str]]

    def statuses(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for field in self.fields.values():
            counts[field.status] += 1
        return dict(counts)


def _extraction_confidence(group: ValueGroup, total_passes: int) -> float:
    """Confidence in the extraction, before any validation.

    Agreement across independent segmentations is the signal; a high
    self-reported number from a single pass is not.
    """
    base = group.best.confidence
    if group.deterministic:
        return min(0.99, max(base, 0.95))
    # Corroboration is relative to the views that were actually run, not to a
    # literal 3. This argument was accepted and then ignored in favour of a
    # hardcoded denominator, which was harmless only while every run had three
    # segmentations to sweep. The ladder sweeps two, so the old form capped
    # every corroborated value at 2/3 and reported "we ran fewer views" as
    # "this value was less corroborated" -- which are different facts.
    views = max(1, total_passes)
    corroboration = min(group.support, views) / views
    return round(min(0.99, 0.45 * base + 0.55 * corroboration), 4)


def reconcile(
    candidates: list[Candidate],
    specs: dict[str, FieldSpec] | None = None,
    total_passes: int = 3,
) -> Reconciliation:
    """Fold every pass's candidates into one record per field."""
    specs = specs or FIELD_REGISTRY
    by_field: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_field[candidate.field].append(candidate)

    fields: dict[str, ExtractedField] = {}
    conflicts: list[ConflictRecord] = []
    contributions: dict[str, set[str]] = defaultdict(set)

    for name, spec in specs.items():
        found = by_field.get(name, [])
        for candidate in found:
            if candidate.span is not None:
                contributions[name].add(f"{candidate.span.start}:{candidate.span.end}")

        if not found:
            # Not "absent" -- just not found by these passes. Validator C is the
            # only thing entitled to call a field absent from the document.
            fields[name] = ExtractedField[Any].single(
                value=None,
                status="needs_review",
                field_class=spec.field_class,
                criticality=spec.criticality,
                standard_term=spec.standard_term,
                notes="no pass produced a candidate; pending negative-space check",
            )
            continue

        external = [c for c in found if c.external_document]
        if external and all(c.value is None for c in external):
            best = max(external, key=lambda c: c.confidence)
            fields[name] = ExtractedField[Any].single(
                value=None,
                spans=[s for s in (best.span,) if s],
                status="external_reference",
                external_document=best.external_document,
                field_class=spec.field_class,
                criticality=spec.criticality,
                standard_term=spec.standard_term,
                extraction_confidence=best.confidence,
                pass_support=len({c.pass_id for c in external}),
                notes=best.notes
                or f"magnitude is fixed by {best.external_document}, "
                   "which is not part of this agreement",
            )
            continue

        valued = [c for c in found if c.value is not None]
        if not valued:
            # A pass that produced no value still produced a reading, and the
            # reading is the whole content of this field. Flattening it to
            # "produced no value" threw away the span it cited, why it could
            # not be typed, and the qualifiers that say so -- and the negative
            # space check then wrote over what was left with "the extractor
            # probably missed it", which is the opposite of what happened.
            best = max(found, key=lambda c: c.confidence)
            qualifiers = {}
            for candidate in found:
                qualifiers.update(candidate.qualifiers)
            fields[name] = ExtractedField[Any].single(
                value=None,
                spans=[s for s in (best.span,) if s],
                status="needs_review",
                field_class=spec.field_class,
                criticality=spec.criticality,
                standard_term=spec.standard_term,
                extraction_confidence=best.confidence,
                pass_support=len({c.pass_id for c in found}),
                qualifiers=qualifiers,
                notes=(
                    best.notes
                    or "passes reported the field present but produced no value"
                ),
            )
            continue

        grouped: dict[str, ValueGroup] = {}
        for candidate in valued:
            key = candidate.key()
            if key not in grouped:
                grouped[key] = ValueGroup(key=key, value=candidate.value,
                                          candidates=[])
            grouped[key].candidates.append(candidate)
        ranked = sorted(
            grouped.values(),
            key=lambda g: (g.deterministic, g.support, g.best.confidence),
            reverse=True,
        )
        winner = ranked[0]
        qualifiers: dict[str, str] = {}
        for candidate in winner.candidates:
            qualifiers.update(candidate.qualifiers)
        notes = next((c.notes for c in winner.candidates if c.notes), None)

        quantity = next(
            (c.quantity for c in winner.candidates if c.quantity is not None), None
        )
        field = ExtractedField[Any].single(
            value=winner.value,
            spans=winner.spans(),
            quantity=quantity,
            status="confirmed",
            field_class=spec.field_class,
            criticality=spec.criticality,
            standard_term=spec.standard_term,
            extraction_confidence=_extraction_confidence(winner, total_passes),
            pass_support=winner.support,
            qualifiers=qualifiers,
            notes=notes,
        )

        if len(ranked) > 1:
            field.status = "conflicted"
            field.alternatives = [
                {
                    "value": str(group.value),
                    "support": group.support,
                    "segmentations": sorted(group.segmentations),
                    "confidence": group.best.confidence,
                    "span": group.best.span.model_dump() if group.best.span else None,
                }
                for group in ranked
            ]
            conflicts.append(ConflictRecord(
                field=name,
                candidates=field.alternatives,
            ))
        elif winner.support < 2 and not winner.deterministic:
            field.status = "needs_review"
            field.notes = (
                "single-pass discovery: found by "
                f"{sorted(winner.segmentations)[0]} segmentation only, "
                "which is a common false-positive mode"
            )
        fields[name] = field

    return Reconciliation(
        fields=fields,
        conflicts=conflicts,
        contributions=dict(contributions),
    )
