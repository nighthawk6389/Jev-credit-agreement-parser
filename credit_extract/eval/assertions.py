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

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .families import FamilyRegister, load_families

AssertionKind = Literal[
    "field_value",          # the field carries this value
    "field_status",         # the field resolved to this status
    "field_external_kind",  # external_by_design vs omitted_from_filing
    "field_unit",           # the stored quantity carries this unit
    "resolve",              # field.resolve(at, state) yields this value
    "invariant_fired",      # a named invariant did / did not fire
    "trap_caught",          # a named trap check passed
    "orphan_found",         # some orphan chunk contains this text
    "archetype",            # the detected deal archetype
    "report_count",         # a countable on the document report
]

#: Statuses the pipeline presents as settled. Anything else is a review flag,
#: and a wrong answer behind a review flag is not a silent error.
CONFIDENT_STATUSES = frozenset(
    {"confirmed", "absent_from_document", "external_reference",
     "not_applicable_to_archetype"}
)

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
    note: str = ""

    @model_validator(mode="after")
    def _needs_a_target(self) -> "Assertion":
        if self.kind != "orphan_found" and self.kind != "archetype":
            if not self.target:
                raise ValueError(f"{self.id}: kind {self.kind} requires a target")
        if self.kind == "resolve" and self.at is None:
            raise ValueError(f"{self.id}: resolve assertions require `at`")
        return self


class AssertionFile(BaseModel):
    document: str
    path: Path | None = None
    source: Literal["real", "synthetic"] = "synthetic"
    tier: int = 2
    archetype: str | None = None
    assertions: list[Assertion] = Field(default_factory=list)

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

    @property
    def silent_error(self) -> bool:
        return self.confident and not self.passed


def load_assertion_file(
    path: Path, register: FamilyRegister | None = None
) -> AssertionFile:
    """Load and validate one assertion file against the family register."""
    register = register or load_families()
    raw = yaml.safe_load(path.read_text())
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


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
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

    if kind in ("field_value", "field_status", "field_external_kind", "field_unit"):
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
        if kind == "field_value":
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
        passed = values_equal(assertion.expect, observed)

    else:  # pragma: no cover - AssertionKind is exhaustive
        raise ValueError(f"unhandled assertion kind {kind!r}")

    return AssertionOutcome(
        assertion_id=assertion.id, document=result.document_id,
        family=assertion.family, member=assertion.member, kind=kind,
        passed=passed, expected=assertion.expect, observed=observed,
        confident=confident, source=source, detail=detail,
    )


_REPORT_COUNTS = {
    "orphan_chunks": lambda r: len(r.orphan_chunks),
    "invariant_violations": lambda r: len(r.invariant_violations),
    "external_references": lambda r: len(r.external_references),
    "unresolved_conflicts": lambda r: len(r.unresolved_conflicts),
    "override_findings": lambda r: len(r.override_findings),
    "review_queue": lambda r: len(r.review_queue),
}


def _report_count(report: Any, name: str) -> int | None:
    getter = _REPORT_COUNTS.get(name)
    if getter is None:
        return None
    return getter(report)


def evaluate_file(file: AssertionFile, result: Any) -> list[AssertionOutcome]:
    return [
        evaluate_assertion(assertion, result, source=file.source)
        for assertion in file.assertions
    ]
