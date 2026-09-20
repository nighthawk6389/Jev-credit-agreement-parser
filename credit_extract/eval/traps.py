"""The four verified traps, as machine-checkable assertions.

A pipeline that does not catch all four is not finished, so they ship as
integration tests rather than as prose in a README. Each check states what the
trap is, which mechanism is supposed to catch it, and what the correct output
looks like -- because the point of each one is that the wrong answer parses
cleanly and looks entirely reasonable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..pipeline import ExtractionResult


class TrapResult(BaseModel):
    trap: str
    title: str
    caught: bool
    mechanism: str
    detail: str
    #: Whether this document contains the trap at all. ``False`` means it was
    #: ruled out on the document's own text; ``None`` means the question could
    #: not be settled. Only ``True`` makes ``caught`` meaningful.
    #:
    #: Each trap is a defect in a particular agreement. Run against a different
    #: one, a trap check that reports FAIL is announcing a failure that did not
    #: happen -- four of them on every real filing, which is enough noise to
    #: make a reader stop believing the ones that are real.
    present: bool | None = True
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def meaningful(self) -> bool:
        # A trap that was caught is in the document by definition. Presence is
        # a cheap textual test and can be wrong; a positive catch cannot, so
        # it wins. Without this, a presence test that misfires turns a real
        # PASS into a silent n/a -- and would do the same to a real FAIL.
        return self.present is True or self.caught

    def __str__(self) -> str:  # pragma: no cover - display helper
        if self.caught:
            return f"[PASS] {self.trap}: {self.title} -- {self.detail}"
        if self.present is False:
            return f"[n/a ] {self.trap}: {self.title} -- {self.detail}"
        if self.present is None:
            return f"[ ?  ] {self.trap}: {self.title} -- {self.detail}"
        return f"[FAIL] {self.trap}: {self.title} -- {self.detail}"

def _text(result: ExtractionResult) -> str:
    """The operative text, when the result carried it."""
    return getattr(result.document, "text", "") or ""


def _mentions(result: ExtractionResult, *needles: str) -> bool:
    """Whether the document uses any of these terms at all.

    Presence is judged on the document's own words rather than on what
    extraction managed to find, so a trap is never ruled out because the
    pipeline failed to read the clause it lives in -- which would turn every
    miss into a silent n/a.
    """
    text = _text(result).lower()
    if not text:
        return True          # nothing to rule it out with; assume it applies
    return any(needle.lower() in text for needle in needles)


def check_trap_1(result: ExtractionResult) -> TrapResult:
    """Duplicated amortization rows.

    The Section 2.10 table lists 31 payment dates for a period spanning 27
    quarters; the four 2023 quarters appear twice. Every row parses cleanly, so
    no extraction model flags it. Read literally the table sums to $11,663,750
    against a correct $10,158,750.
    """
    invariants = {v.invariant for v in result.report.invariant_violations}
    required = {
        "amortization_dates_strictly_increasing",
        "amortization_row_count_matches_quarters",
    }
    totals = [
        v for v in result.report.invariant_violations
        if v.invariant == "amortization_total_consistent"
    ]
    caught = required.issubset(invariants)
    schedule = result.amortization
    if schedule is None:
        # No schedule was parsed. That is a finding in its own right when the
        # document has one, but it is not evidence either way about this trap.
        present: bool | None = (
            None if _mentions(result, "amortization", "installment")
            else False
        )
    else:
        present = len(schedule.rows) != len(schedule.deduplicated().rows)
    return TrapResult(
        trap="trap_1",
        title="duplicated amortization rows",
        caught=caught,
        present=present,
        mechanism="deterministic invariants (strictly increasing dates, row "
                  "count against quarters spanned) plus the generated ACTUS "
                  "schedule diffed against the printed table",
        detail=(
            "caught by " + ", ".join(sorted(invariants & required))
            if caught else
            "no duplicated rows in this document\'s schedule"
            if present is False and result.amortization is not None else
            "this document has no amortization schedule to duplicate rows in"
            if present is False else
            "no amortization schedule was parsed, so this trap can be neither "
            "confirmed nor ruled out here"
            if present is None else
            "NOT caught: no invariant flagged the duplicated rows"
        ),
        evidence={
            "invariants_fired": sorted(invariants),
            "quantified_discrepancy": totals[0].message if totals else None,
            "printed_rows": (
                len(result.amortization.rows) if result.amortization else None
            ),
            "distinct_rows": (
                len(result.amortization.deduplicated().rows)
                if result.amortization else None
            ),
        },
    )


def check_trap_2(result: ExtractionResult) -> TrapResult:
    """Hardcoded opening EBITDA.

    Four pre-closing quarters are fixed by table and override the Consolidated
    EBITDA definition entirely. Any pipeline that computes EBITDA from the
    definition gets the first four test periods wrong.
    """
    overrides = [
        o for o in result.report.override_findings
        if "EBITDA" in o.get("subject", "")
    ]
    hardcoded = [
        o for o in overrides
        if "deemed to be" in o["governing_span"]["text"]
        or "Notwithstanding" in o["governing_span"]["text"]
    ]
    caught = bool(hardcoded)
    present = _mentions(result, "Consolidated EBITDA")
    return TrapResult(
        trap="trap_2",
        title="hardcoded opening EBITDA overrides the definition",
        caught=caught,
        present=present,
        mechanism="validator D (override detection), anchored on the "
                  "notwithstanding clause that introduces the table",
        detail=(
            f"{len(hardcoded)} override provision(s) found governing "
            "Consolidated EBITDA"
            if caught else
            "this agreement does not use Consolidated EBITDA, so there is no "
            "definition for a table to displace"
            if not present else
            "NOT caught: no provision was found displacing the Consolidated "
            "EBITDA definition, so the hardcoded quarters would be ignored"
        ),
        evidence={
            "override_findings": [
                {
                    "probability": o["probability"],
                    "text": o["governing_span"]["text"][:300],
                }
                for o in hardcoded
            ],
        },
    )


def check_trap_3(result: ExtractionResult) -> TrapResult:
    """Uncapped external add-back.

    Clause (a)(xvi) permits add-backs set out in the Sponsor Model, capped at
    the amounts in that model -- a spreadsheet delivered before closing that is
    not a Loan Document and was never filed. The stated 25% cap governs clauses
    (a)(xiii)-(a)(xv) and therefore is not the real cap. Correct output is
    ``external_reference``, not ``0.25``.
    """
    field = result.fields.get("consolidated_ebitda.addback_cap_clause_a_xvi")
    caught = bool(
        field
        and field.status == "external_reference"
        and field.value is None
        and field.external_document
        and "sponsor model" in field.external_document.lower()
    )
    ruled_out = (
        field is not None and field.status == "not_applicable_to_archetype"
    ) or not _mentions(result, "add back", "add-back", "addback")
    return TrapResult(
        trap="trap_3",
        title="add-back cap lives in the Sponsor Model, not in the agreement",
        caught=caught,
        present=not ruled_out,
        mechanism="definition graph (Consolidated EBITDA reaches an external "
                  "document) plus validator E (external dependency)",
        detail=(
            f"reported as external_reference to {field.external_document}"
            if caught else
            "this deal has no EBITDA add-back regime for a cap to sit outside of"
            if ruled_out else
            f"NOT caught: status is "
            f"{field.status if field else 'missing'} with value "
            f"{field.value if field else None}; a number here would be wrong"
        ),
        evidence={
            "status": field.status if field else None,
            "value": str(field.value) if field and field.value is not None else None,
            "external_document": field.external_document if field else None,
            "external_references": result.report.external_references,
        },
    )


def check_trap_4(result: ExtractionResult) -> TrapResult:
    """Absent MFN sunset.

    MFN protection at 50bps with no expiry, which is materially
    lender-favourable and unusual. ``mfn_sunset: null`` conveys nothing;
    ``absent_from_document`` with high confidence conveys a deal term.
    """
    field = result.fields.get("mfn_sunset")
    caught = bool(
        field
        and field.status == "absent_from_document"
        and field.value is None
        and field.validation_source == "C_negative_space"
    )
    mfn = result.fields.get("mfn_threshold_pct")
    # MFN protection is almost never drafted under that name. In the canonical
    # fixture it is a yield provision on an Incremental Term Facility and the
    # letters MFN appear nowhere, so the presence test asks the question the
    # provision actually turns on: is there an incremental facility at all?
    present = _mentions(
        result, "incremental", "most favored nation", "most favoured nation",
        "all-in yield",
    )
    return TrapResult(
        trap="trap_4",
        title="MFN protection with no sunset",
        caught=caught,
        present=present,
        mechanism="validator C (negative-space confirmation), asked of every "
                  "chunk and combined in Python",
        detail=(
            f"affirmatively confirmed absent at "
            f"{field.validation_confidence:.2f}"
            if caught and field and field.validation_confidence is not None else
            "this agreement has no incremental facility, so there is no MFN "
            "protection to carry a sunset"
            if not present else
            f"NOT caught: status is {field.status if field else 'missing'}; "
            "a bare null here conveys nothing about the deal"
        ),
        evidence={
            "status": field.status if field else None,
            "confidence": field.validation_confidence if field else None,
            "mfn_threshold_pct": (
                str(mfn.value) if mfn and mfn.value is not None else None
            ),
            "note": field.notes if field else None,
        },
    )


TRAP_CHECKS = (check_trap_1, check_trap_2, check_trap_3, check_trap_4)


def check_all(result: ExtractionResult) -> list[TrapResult]:
    return [check(result) for check in TRAP_CHECKS]


def summarize(results: list[TrapResult]) -> str:
    applicable = [r for r in results if r.meaningful]
    caught = sum(1 for r in applicable if r.caught)
    absent = sum(1 for r in results if r.present is False)
    unknown = sum(1 for r in results if r.present is None)
    headline = f"traps: {caught}/{len(applicable)} caught"
    qualifiers = []
    if absent:
        qualifiers.append(f"{absent} not present in this document")
    if unknown:
        qualifiers.append(f"{unknown} undetermined")
    if qualifiers:
        headline += " (" + ", ".join(qualifiers) + ")"
    lines = [headline]
    lines += [f"  {r}" for r in results]
    return "\n".join(lines)
