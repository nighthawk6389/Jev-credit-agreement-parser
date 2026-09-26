"""Tier 2: targeted single-proposition assertions.

Exhaustively labelling a hundred agreements is thousands of fields and it does
not get done. A Tier 2 label is one proposition, bound to one family member,
cheap enough to write in a line:

    - id: mfn_has_no_sunset
      family: F04_absence
      member: no_mfn_sunset
      kind: field_status
      target: mfn_sunset
      expect: absent_from_document

Each assertion tests exactly one mechanism, so a failure names the mechanism
rather than a document. Every assertion also records whether the pipeline
presented its answer *confidently*, which is what makes the silent error rate
computable from Tier 2 labels rather than only from Tier 1.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ..models.core import CONFIDENT_STATUSES as _CONFIDENT_STATUSES
from .families import FamilyRegister, load_families

AssertionKind = Literal[
    "field_value",          # the field carries this value
    "tranche_value",        # the value the document attributed to one tranche
    "field_status",         # the field resolved to this status
    "field_external_kind",  # external_by_design vs omitted_from_filing
    "field_unit",           # the stored quantity carries this unit
    "resolve",              # field.resolve(at, state) yields this value
    "invariant_fired",      # a named invariant did / did not fire
    "trap_caught",          # a named trap check passed
    "orphan_found",         # some orphan chunk contains this text
    "archetype",            # the detected deal archetype
    "report_count",         # a countable on the document report
    "chain_finding",        # a named chain finding is / is not reported
    "text_present",         # a string does / does not occur in operative text
    "closure_contains",     # resolving a term reaches these other terms
]

#: Left on every line of a scaffolded label file by credit_extract.eval.label,
#: and deleted by the human who checked that line. Its presence means the file
#: still holds the extractor's own answers.
SCAFFOLD_MARKER = "VERIFY"

#: Statuses the pipeline presents as settled. Anything else is a review flag,
#: and a wrong answer behind a review flag is not a silent error. Defined in
#: ``models.core`` and re-exported here, because the output structure asserts
#: under the same set and a second copy could drift.
CONFIDENT_STATUSES = _CONFIDENT_STATUSES

#: Tolerance for percentage and ratio comparisons.
NUMERIC_TOLERANCE = Decimal("0.005")


class Assertion(BaseModel):
    """One labelled proposition about one document."""

    id: str
    family: str
    member: str | None = None
    kind: AssertionKind
    target: str | None = None
    expect: Any = None
    #: For ``resolve``: the point in time and the state to resolve against.
    at: date | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    #: For ``tranche_value``: which tranche's reading to assert on.
    #:
    #: ``field_value`` reads ``field.value``, which is ``variants[0]`` -- the
    #: deal-level answer. Where a document prices its tranches separately the
    #: real content is in the attributed variants, and without this there is no
    #: way for the corpus to assert on them at all: the behaviour would be
    #: tested by unit tests over quoted bodies and unmeasured on any document.
    for_tranche: str | None = None
    #: How ``expect`` is compared. ``eq`` for every proposition about a deal:
    #: a margin is the margin, and "close enough" is not a reading.
    #:
    #: ``at_most`` exists for counts of the pipeline's own working -- how many
    #: chunks the sweep left uncaptured, how long the review queue is. Those
    #: labels all say the same thing in their notes: "a tripwire on the sweep,
    #: not a target". The harness had no way to express that, so every
    #: improvement in coverage broke them and the number got bumped, which
    #: teaches a reader that the label follows the code. A ceiling says what
    #: was meant: fewer uncaptured chunks is always fine, more is the thing to
    #: investigate.
    compare: Literal["eq", "at_most"] = "eq"
    note: str = ""

    @model_validator(mode="after")
    def _needs_a_target(self) -> "Assertion":
        if self.kind != "orphan_found" and self.kind != "archetype":
            if not self.target:
                raise ValueError(f"{self.id}: kind {self.kind} requires a target")
        if self.kind == "resolve" and self.at is None:
            raise ValueError(f"{self.id}: resolve assertions require `at`")
        if self.kind == "tranche_value" and not self.for_tranche:
            raise ValueError(
                f"{self.id}: tranche_value assertions require `for_tranche` -- "
                "the whole point is which tranche the value was attributed to"
            )
        if self.for_tranche and self.kind != "tranche_value":
            raise ValueError(
                f"{self.id}: for_tranche is only read by tranche_value, so on "
                f"{self.kind} it would be silently ignored"
            )
        if self.compare == "at_most" and self.kind != "report_count":
            # A ceiling on a deal term would let a wrong answer pass for being
            # small enough, which is the opposite of what this corpus measures.
            raise ValueError(
                f"{self.id}: compare: at_most is only for report_count, not "
                f"{self.kind} -- a proposition about the deal is equal or wrong"
            )
        return self


class AssertionFile(BaseModel):
    """Assertions about one document, or about one chain of them.

    ``chain`` is the F05 case and is why this is not simply a per-file thing.
    The operative terms of an amended agreement live scattered across a base
    and however many amendments, so the unit that can be labelled is the set,
    not any file in it -- and a label written against a single amendment
    cannot express the thing most worth asserting, which is what the terms
    say *after* the chain is folded in.

    ``chain`` is ordered as filed, not as it takes effect. Working out the
    operative order is the pipeline's job and is exactly what the assertions
    are here to check, so a label that pre-sorted the documents would be
    marking its own homework.
    """

    document: str
    #: Documents forming a set, oldest-looking first. Mutually exclusive with
    #: ``document``; ``document`` then names the chain for reporting.
    chain: list[str] = Field(default_factory=list)
    #: The harvest's name for this document, when it differs from ``document``.
    #: EDGAR filenames carry a stratum letter, the filer, the accession number
    #: and the exhibit id, which is unreadable in a report and is also what the
    #: frozen split is keyed on. Naming both keeps the report legible without
    #: making the document unfindable or the split unresolvable.
    corpus_name: str | None = None
    path: Path | None = None
    source: Literal["real", "synthetic"] = "synthetic"
    tier: int = 2
    archetype: str | None = None
    assertions: list[Assertion] = Field(default_factory=list)

    @property
    def is_chain(self) -> bool:
        return len(self.chain) > 1

    @property
    def is_synthetic(self) -> bool:
        return self.source == "synthetic"


class AssertionOutcome(BaseModel):
    assertion_id: str
    document: str
    family: str
    member: str | None
    kind: str
    passed: bool
    expected: Any = None
    observed: Any = None
    #: Did the pipeline present this answer as settled rather than flagged?
    confident: bool = True
    source: Literal["real", "synthetic"] = "synthetic"
    detail: str = ""
    #: The label file's name for the document, which is what the frozen split
    #: is keyed on. ``document`` above is the pipeline's internal id -- a hash
    #: that says nothing about which side of the split this was measured on.
    label_document: str = ""
    #: How many independent views backed the winning value, where this
    #: assertion is about a field. ``reconcile`` demotes a field below a
    #: support floor, and that floor was never fitted against anything --
    #: carrying the number here is what lets it be. None for assertions that
    #: are not about a field's value.
    support: int | None = None
    #: The field's criticality, so the fit can be read per class rather than
    #: pooled: a wrong notice address and a wrong maturity are not the same
    #: error and should not share a floor.
    criticality: int | None = None

    @property
    def silent_error(self) -> bool:
        return self.confident and not self.passed


def load_assertion_file(
    path: Path, register: FamilyRegister | None = None
) -> AssertionFile:
    """Load and validate one assertion file against the family register."""
    register = register or load_families()
    text = path.read_text()
    if SCAFFOLD_MARKER in text:
        # A scaffold from credit_extract.eval.label holds what the extractor
        # said, not what the document says. Loading one would score the
        # pipeline against its own output and report perfect agreement.
        remaining = sum(
            1 for line in text.splitlines()
            if line.lstrip().startswith(("note:", "- ")) and SCAFFOLD_MARKER in line
        )
        raise ValueError(
            f"{path}: {remaining} assertion(s) still marked {SCAFFOLD_MARKER}. "
            "A scaffold is the extractor's answers, not ground truth -- read "
            "each quote, correct the value, and delete the marker."
        )
    raw = yaml.safe_load(text)
    parsed = AssertionFile(path=path, **raw)
    seen: set[str] = set()
    for assertion in parsed.assertions:
        # Binding to an unknown family or member is a hard error: the register
        # is the single source of truth, and a typo that silently creates a
        # new family makes coverage unreadable.
        register.validate_member(assertion.family, assertion.member)
        if assertion.id in seen:
            raise ValueError(f"{path}: duplicate assertion id {assertion.id!r}")
        seen.add(assertion.id)
    return parsed


def load_assertions(
    directory: Path, register: FamilyRegister | None = None
) -> list[AssertionFile]:
    register = register or load_families()
    return [
        load_assertion_file(path, register)
        for path in sorted(directory.glob("*.yaml"))
    ]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


#: Decoration a label may carry around a number because the document does.
#: A percent sign, a currency symbol, thousands separators, and the
#: non-breaking space some filings put before the sign.
_DECORATION = re.compile(r"[%$,\s ]")


def _as_decimal(value: Any) -> Decimal | None:
    """Parse a labelled or extracted magnitude, ignoring its decoration.

    The decoration stripping is not cosmetic tolerance, it is the difference
    between a comparison that happens and one that silently does not. A label
    written ``expect: 0.75%`` -- which is how the document writes it, and what
    the labelling guide asks for -- arrived here as the string ``'0.75%'``,
    raised ``InvalidOperation``, returned None, and sent ``values_equal`` down
    the text branch to compare ``'0.75%'`` against ``'0.75'``.

    That was invisible for as long as the field found nothing: no value, no
    comparison. The moment the floor rules started working, 25 correct
    extractions arrived as silent errors -- confirmed, right, and scored wrong,
    which is the one failure mode this harness exists to measure and so the
    worst place to have a bug.

    Only decoration is removed. Nothing here rescales, so a percent stored as
    a fraction still compares unequal to one stored as a number, and a ratio
    like ``3.50:1.00`` still fails to parse and falls to the text branch where
    it belongs.
    """
    if value is None or isinstance(value, bool):
        return None
    text = _DECORATION.sub("", str(value))
    if not text or text in ("-", "+", "."):
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def values_equal(expected: Any, observed: Any) -> bool:
    """Compare a labelled value to an extracted one, tolerant of formatting."""
    if expected is None or observed is None:
        return expected is None and observed is None
    if isinstance(expected, bool) or isinstance(observed, bool):
        return bool(expected) == bool(observed)
    a, b = _as_decimal(expected), _as_decimal(observed)
    if a is not None and b is not None:
        return abs(a - b) <= NUMERIC_TOLERANCE
    a_text, b_text = str(expected), str(observed)
    if len(a_text) >= 10 and a_text[4] == "-" and b_text[:10]:
        return a_text[:10] == b_text[:10]      # ISO dates
    return " ".join(a_text.split()).casefold() == " ".join(b_text.split()).casefold()


def _field_confident(field: Any) -> bool:
    status = getattr(field, "status", None)
    return status in CONFIDENT_STATUSES


def evaluate_assertion(
    assertion: Assertion, result: Any, source: str = "synthetic"
) -> AssertionOutcome:
    """Run one assertion against an :class:`ExtractionResult`."""
    kind = assertion.kind
    observed: Any = None
    confident = True
    detail = ""
    support: int | None = None
    criticality: int | None = None

    if kind in ("field_value", "tranche_value", "field_status",
                "field_external_kind", "field_unit"):
        field = result.fields.get(assertion.target)
        if field is None:
            return AssertionOutcome(
                assertion_id=assertion.id, document=result.document_id,
                family=assertion.family, member=assertion.member, kind=kind,
                passed=False, expected=assertion.expect, observed=None,
                confident=False, source=source,
                detail=f"{assertion.target} is not an extraction target",
            )
        confident = _field_confident(field)
        support = getattr(field, "pass_support", None)
        criticality = getattr(field, "criticality", None)
        if kind == "tranche_value":
            # The variant the document attributed to this tranche. No such
            # variant is a miss, not a pass with None: the assertion says the
            # document names a value for this tranche, and not finding the
            # attribution is exactly the failure being measured. It is also not
            # a *confident* miss, because a field carrying no reading for a
            # tranche asserts nothing about it.
            attributed = next(
                (v for v in field.variants
                 if getattr(v, "applies_to", None) == assertion.for_tranche),
                None,
            )
            observed = attributed.value if attributed is not None else None
            confident = (
                attributed is not None
                and attributed.status in CONFIDENT_STATUSES
            )
            support = getattr(attributed, "pass_support", None)
            detail = (
                f"attributed to {assertion.for_tranche}"
                if attributed is not None
                else f"no variant is attributed to {assertion.for_tranche}; "
                     f"the field carries {[getattr(v, 'applies_to', None) for v in field.variants]}"
            )
        elif kind == "field_value":
            observed = field.value
        elif kind == "field_status":
            observed = field.status
        elif kind == "field_external_kind":
            observed = getattr(field, "external_kind", None)
        else:
            unit = getattr(getattr(field, "quantity", None), "unit", None)
            observed = getattr(unit, "value", unit)
        passed = values_equal(assertion.expect, observed)

    elif kind == "resolve":
        field = result.fields.get(assertion.target)
        if field is None or not hasattr(field, "resolve"):
            return AssertionOutcome(
                assertion_id=assertion.id, document=result.document_id,
                family=assertion.family, member=assertion.member, kind=kind,
                passed=False, expected=assertion.expect, observed=None,
                confident=False, source=source,
                detail="field does not support point-in-time resolution",
            )
        variant = field.resolve(assertion.at, assertion.state)
        observed = variant.value if variant is not None else None
        confident = variant is not None and variant.status in CONFIDENT_STATUSES
        passed = values_equal(assertion.expect, observed)
        detail = (
            f"resolved at {assertion.at} with state {assertion.state}"
            if variant is not None else "no variant governs that point"
        )

    elif kind == "invariant_fired":
        fired = {v.invariant for v in result.report.invariant_violations}
        observed = assertion.target in fired
        passed = bool(observed) == bool(assertion.expect)
        # A deterministic check is always a settled claim.
        confident = True

    elif kind == "trap_caught":
        from . import traps as trap_checks

        checks = {c.trap: c for c in trap_checks.check_all(result)}
        check = checks.get(assertion.target)
        observed = bool(check.caught) if check else None
        passed = observed is not None and observed == bool(assertion.expect)
        detail = check.detail if check else f"unknown trap {assertion.target!r}"

    elif kind == "orphan_found":
        needle = str(assertion.expect)
        hits = [o for o in result.report.orphan_chunks if needle in o.text]
        observed = [o.chunk_id for o in hits]
        passed = bool(hits)
        detail = f"{len(result.report.orphan_chunks)} orphan chunk(s) swept"

    elif kind == "archetype":
        detected = getattr(result, "archetype", None)
        observed = getattr(detected, "archetype", detected)
        confident = bool(getattr(detected, "confident", True))
        passed = values_equal(assertion.expect, observed)

    elif kind == "report_count":
        observed = _report_count(result.report, assertion.target)
        if assertion.compare == "at_most":
            ceiling, actual = _as_decimal(assertion.expect), _as_decimal(observed)
            passed = (
                ceiling is not None and actual is not None and actual <= ceiling
            )
            detail = f"ceiling {assertion.expect}, observed {observed}"
        else:
            passed = values_equal(assertion.expect, observed)
        # A count of the pipeline's own working -- how many chunks the sweep
        # caught, how long the review queue is -- is a tripwire on the harness,
        # not a proposition about the deal. No reader is ever shown one as a
        # term, so a drifted count is a change to investigate and never a
        # silent error. Counting it as one would let harness noise consume a
        # family's error budget and hide a real mistake behind it.
        confident = False

    elif kind == "chain_finding":
        reported = {
            finding.get("kind") for finding in result.report.chain.get("findings", [])
        }
        observed = assertion.target in reported
        passed = bool(observed) == bool(assertion.expect)

    elif kind == "closure_contains":
        # The thing F09 is actually about: a term's meaning is the chain of
        # definitions below it, and a covenant extracted without that chain is
        # a symbol rather than a number.
        graph = getattr(result, "definition_graph", None)
        closure = graph.closure(assertion.target) if graph is not None else []
        wanted = [assertion.expect] if isinstance(assertion.expect, str) else list(
            assertion.expect or []
        )
        folded = {c.casefold() for c in closure}
        missing = [w for w in wanted if w.casefold() not in folded]
        observed = closure[:12]
        passed = not missing and bool(closure)
        confident = bool(closure)
        detail = (
            f"{len(closure)} term(s) in the closure"
            + (f"; missing {missing}" if missing else "")
        )

    elif kind == "text_present":
        # Asserted against the *operative* text, which is what every span
        # quotes. A figure a blackline deleted must not be addressable here.
        observed = assertion.target in getattr(result.document, "text", "")
        passed = bool(observed) == bool(assertion.expect)

    else:  # pragma: no cover - AssertionKind is exhaustive
        raise ValueError(f"unhandled assertion kind {kind!r}")

    return AssertionOutcome(
        assertion_id=assertion.id, document=result.document_id,
        family=assertion.family, member=assertion.member, kind=kind,
        passed=passed, expected=assertion.expect, observed=observed,
        confident=confident, source=source, detail=detail,
        support=support, criticality=criticality,
    )


_REPORT_COUNTS = {
    "orphan_chunks": lambda r: len(r.orphan_chunks),
    "invariant_violations": lambda r: len(r.invariant_violations),
    "external_references": lambda r: len(r.external_references),
    "unresolved_conflicts": lambda r: len(r.unresolved_conflicts),
    "override_findings": lambda r: len(r.override_findings),
    "review_queue": lambda r: len(r.review_queue),
    "chain_findings": lambda r: len(r.chain.get("findings", [])),
    "definition_terms": lambda r: r.definition_graph_stats.get("terms"),
    "definition_cycles": lambda r: len(r.definition_graph_stats.get("cycles") or []),
    "definition_max_depth": lambda r: r.definition_graph_stats.get("max_depth"),
    "definition_deep_terms": lambda r: len(
        r.definition_graph_stats.get("terms_at_depth_3_or_more") or []
    ),
    "effects_applied": lambda r: len(r.chain.get("effects_applied", [])),
    "effects_unapplied": lambda r: len(r.chain.get("effects_unapplied", [])),
}


def _report_count(report: Any, name: str) -> int | None:
    getter = _REPORT_COUNTS.get(name)
    if getter is None:
        return None
    return getter(report)


def evaluate_file(file: AssertionFile, result: Any) -> list[AssertionOutcome]:
    outcomes = [
        evaluate_assertion(assertion, result, source=file.source)
        for assertion in file.assertions
    ]
    for outcome in outcomes:
        # The split is keyed on the harvest name, so that is what travels with
        # the outcome; the readable name is what the label file is called.
        outcome.label_document = file.corpus_name or file.document
    return outcomes
