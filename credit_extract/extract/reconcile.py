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

from ..models.core import ConflictRecord, ExtractedField, Span, Variant
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from .passes import Candidate

#: Two spans this similar are treated as the same citation.
SPAN_AGREEMENT_THRESHOLD = 0.30

#: Pass ids from deterministic parsing, which need no corroboration.
DETERMINISTIC_PREFIX = "deterministic:"

#: How many independent views must back a value before it may be presented as
#: settled. Every other threshold in this repository is fitted against
#: labelled outcomes; this one was a literal 2, chosen when a run always had
#: three segmentations to sweep, and it governs the same decision.
#:
#: Dropping the definitional sweep made that stop being harmless: with three
#: views ``< 2`` was a majority rule, with two it is unanimity.
#: ``eval/support_fit.py`` is the fitter, and the number lives there so the
#: rule and its evidence cannot drift apart.
from ..eval.support_fit import SUPPORT_FLOOR  # noqa: E402


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
    def from_definition(self) -> bool:
        """True where some candidate was read from the term's own definition.

        This outranks everything else, including the rest of the deterministic
        tier, and the reason is not confidence -- it is that the document said
        where the term is settled. ``closing_date`` is the case that forced it:
        a cover page says "dated as of April 12, 2019", the recitals list seven
        prior amendments with their own dates, and the definitions article says
        '" Closing Date ": June 25, 2018.' All of those are deterministic
        matches at 0.95, so before this the ranking was a coin toss among
        eleven candidates and the answer it returned was 2019-11-26.

        A mention is evidence that a date exists. A definition is evidence that
        it is *this* one.
        """
        return any(
            c.pass_id == f"{DETERMINISTIC_PREFIX}definitions"
            for c in self.candidates
        )

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


def _winner(candidates: list[Candidate]) -> ValueGroup | None:
    """The best-ranked value among ``candidates``, or None if none has one."""
    valued = [c for c in candidates if c.value is not None]
    if not valued:
        return None
    grouped: dict[str, ValueGroup] = {}
    for candidate in valued:
        key = candidate.key()
        if key not in grouped:
            grouped[key] = ValueGroup(key=key, value=candidate.value, candidates=[])
        grouped[key].candidates.append(candidate)
    return max(
        grouped.values(),
        key=lambda g: (g.from_definition, g.deterministic, g.support,
                       g.best.confidence),
    )


def _tranche_variants(
    attributed: dict[str, list[Candidate]], total_passes: int,
) -> list[Variant[Any]]:
    """One variant per tranche the document named, ordered by tranche id.

    Each is a reading, not an inheritance: the document said which tranche the
    number is for, so the span cited is the span that says so.

    The support floor applies here exactly as it does to a deal-wide value.
    Attribution says *whose* the number is; it says nothing about whether one
    pass finding it once is enough to present it as settled, and exempting
    these would be a second route to ``confirmed`` with a lower bar than the
    first.
    """
    variants: list[Variant[Any]] = []
    for tranche_id in sorted(attributed):
        group = _winner(attributed[tranche_id])
        if group is None:
            continue
        thin = group.support < SUPPORT_FLOOR and not group.deterministic
        variants.append(Variant[Any](
            value=group.value,
            spans=group.spans(),
            status="needs_review" if thin else "confirmed",
            applies_to=tranche_id,
            extraction_confidence=_extraction_confidence(group, total_passes),
            pass_support=group.support,
            notes=(
                "single-pass discovery: found by "
                f"{sorted(group.segmentations)[0]} segmentation only, "
                "which is a common false-positive mode"
                if thin
                else next((c.notes for c in group.candidates if c.notes), None)
            ),
        ))
    return variants


def _from_attributed(
    spec: FieldSpec,
    attributed: dict[str, list[Candidate]],
    total_passes: int,
) -> ExtractedField[Any]:
    """Build a field whose only readings were attributed to tranches.

    The deal-level slot is the one every validator, the calibration fit and the
    labelling harness read, so what goes in it decides whether this is an
    improvement or a new way to be confidently wrong. The rule:

    * every tranche priced the same -- there IS a deal-level answer, and it is
      that value. ``"Floor" means (a) with respect to the Initial Term Loans,
      0.00% and (b) with respect to the Revolving Loans, 0.00%`` states one
      number twice, and refusing it would throw away a reading the document
      makes plainly.
    * tranches priced differently -- there is NO deal-level answer, and the
      slot declines. Iridium's floor is 0.75% on the term loan and 0.00% on
      the revolver; either number in this slot is a silent error, and the
      average of them is worse. The tranche variants carry the real values and
      ``Asserted.from_field(for_tranche=...)`` is what finds them.
    """
    variants = _tranche_variants(attributed, total_passes)
    distinct = {v.value for v in variants}
    field = ExtractedField[Any](
        field_class=spec.field_class,
        criticality=spec.criticality,
        standard_term=spec.standard_term,
    )
    if not variants:
        # Attributed candidates that all produced no value. Nothing emits this
        # today -- the decline route is deal-wide on purpose -- but falling
        # through would print "the tranches differ ()" and mean nothing.
        field.variants = [Variant[Any](
            value=None,
            status="needs_review",
            notes=(
                "passes attributed this term to "
                f"{', '.join(sorted(attributed))} and produced no value for any "
                "of them"
            ),
        )]
        return field
    if len(distinct) == 1:
        agreed = variants[0]
        field.variants = [Variant[Any](
            value=agreed.value,
            spans=list(agreed.spans),
            status="confirmed",
            extraction_confidence=agreed.extraction_confidence,
            pass_support=agreed.pass_support,
            notes=(
                f"stated per tranche and the same for each of "
                f"{', '.join(sorted(attributed))}"
            ),
        )] + variants
        return field

    field.variants = [Variant[Any](
        value=None,
        status="needs_review",
        notes=(
            "priced per tranche and the tranches differ ("
            + "; ".join(f"{v.applies_to}={v.value}" for v in variants)
            + "), so this deal-level field has no single answer; the per-"
              "tranche variants carry the values"
        ),
    )] + variants
    field.precedence_basis = (
        "variant 0 declines because the document prices the tranches "
        "differently; the rest are attributed by the document"
    )
    return field


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
        all_found = by_field.get(name, [])
        for candidate in all_found:
            if candidate.span is not None:
                contributions[name].add(f"{candidate.span.start}:{candidate.span.end}")

        # Partition on the tranche the document attributed the value to. Two
        # tranches priced differently are two answers, not a disagreement, so
        # they must not meet in the same value grouping. The deal-wide
        # partition is reconciled exactly as it was before this existed, and a
        # document that states each term once has no other partition -- which
        # is what keeps every downstream reader unchanged on those documents.
        found = [c for c in all_found if c.applies_to is None]
        attributed: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in all_found:
            if candidate.applies_to is not None:
                attributed[candidate.applies_to].append(candidate)

        if not found and attributed:
            fields[name] = _from_attributed(spec, attributed, total_passes)
            continue

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
            key=lambda g: (
                g.from_definition, g.deterministic, g.support,
                g.best.confidence,
            ),
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
        elif winner.support < SUPPORT_FLOOR and not winner.deterministic:
            field.status = "needs_review"
            field.notes = (
                "single-pass discovery: found by "
                f"{sorted(winner.segmentations)[0]} segmentation only, "
                "which is a common false-positive mode"
            )
        if attributed:
            field.variants.extend(_tranche_variants(attributed, total_passes))
            field.precedence_basis = (
                "variant 0 is the deal-wide reading; the rest are attributed "
                "to a tranche by the document and govern for that tranche"
            )
        fields[name] = field

    return Reconciliation(
        fields=fields,
        conflicts=conflicts,
        contributions=dict(contributions),
    )
