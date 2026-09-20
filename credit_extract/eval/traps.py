"""The four verified traps, as machine-checkable assertions.

A pipeline that does not catch all four is not finished, so they ship as
integration tests rather than as prose in a README. Each check states what the
trap is, which mechanism is supposed to catch it, and what the correct output
looks like -- because the point of each one is that the wrong answer parses
cleanly and looks entirely reasonable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from ..pipeline import ExtractionResult


class TrapResult(BaseModel):
    trap: str
    title: str
    caught: bool
    mechanism: str
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - display helper
        mark = "PASS" if self.caught else "FAIL"
        return f"[{mark}] {self.trap}: {self.title} -- {self.detail}"


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
    return TrapResult(
        trap="trap_1",
        title="duplicated amortization rows",
        caught=caught,
        mechanism="deterministic invariants (strictly increasing dates, row "
                  "count against quarters spanned) plus the generated ACTUS "
                  "schedule diffed against the printed table",
        detail=(
            "caught by " + ", ".join(sorted(invariants & required))
            if caught else
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
    return TrapResult(
        trap="trap_2",
        title="hardcoded opening EBITDA overrides the definition",
        caught=caught,
        mechanism="validator D (override detection), anchored on the "
                  "notwithstanding clause that introduces the table",
        detail=(
            f"{len(hardcoded)} override provision(s) found governing "
            "Consolidated EBITDA"
            if caught else
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
    return TrapResult(
        trap="trap_3",
        title="add-back cap lives in the Sponsor Model, not in the agreement",
        caught=caught,
        mechanism="definition graph (Consolidated EBITDA reaches an external "
                  "document) plus validator E (external dependency)",
        detail=(
            f"reported as external_reference to {field.external_document}"
            if caught else
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
    return TrapResult(
        trap="trap_4",
        title="MFN protection with no sunset",
        caught=caught,
        mechanism="validator C (negative-space confirmation), asked of every "
                  "chunk and combined in Python",
        detail=(
            f"affirmatively confirmed absent at "
            f"{field.validation_confidence:.2f}"
            if caught and field and field.validation_confidence is not None else
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
    caught = sum(1 for r in results if r.caught)
    lines = [f"traps: {caught}/{len(results)} caught"]
    lines += [f"  {r}" for r in results]
    return "\n".join(lines)
