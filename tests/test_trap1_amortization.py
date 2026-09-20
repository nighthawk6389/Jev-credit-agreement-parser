"""Trap 1 -- duplicated amortization rows.

The repayment table prints 31 payment dates for a period spanning 27 quarters:
the four 2023 quarters appear twice. Every row parses cleanly, so no extraction
model flags it. Read literally the table sums to $11,663,750 against a correct
$10,158,750 -- a $1,505,000 overstatement of scheduled amortization.

This is the cheapest possible proof that the architecture works, and per the
build order nothing downstream of the invariant checker is worth having until
it passes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.extract.passes import parse_amortization_schedule
from credit_extract.models.actus_map import (
    ActusContract, diff_schedule, generate_schedule, map_facility,
)
from credit_extract.validate.invariants import InvariantContext, check_all

PRINCIPAL = Decimal(150_500_000)


@pytest.fixture(scope="module")
def schedule(doc):
    parsed = parse_amortization_schedule(doc, PRINCIPAL)
    assert parsed is not None, "no amortization table found"
    return parsed


def test_table_is_read_literally_not_repaired(schedule):
    """The parser must not silently de-duplicate; that would hide the finding."""
    assert len(schedule.rows) == 31
    assert schedule.total_scheduled == Decimal(11_663_750)
    assert len(schedule.deduplicated().rows) == 27


def test_strictly_increasing_dates_catches_the_duplicates(schedule):
    violations = check_all(InvariantContext(amortization=schedule))
    increasing = [
        v for v in violations
        if v.invariant == "amortization_dates_strictly_increasing"
    ]
    assert increasing, "duplicated rows must break the increasing-dates invariant"
    assert any("2023" in v.message for v in increasing)


def test_row_count_disagrees_with_quarters_spanned(schedule):
    violations = check_all(InvariantContext(amortization=schedule))
    counts = [
        v for v in violations
        if v.invariant == "amortization_row_count_matches_quarters"
    ]
    assert counts, "31 rows over a 27-quarter span must be flagged"
    assert counts[0].observed == 31
    assert counts[0].expected == 27


def test_total_overstatement_is_quantified(schedule):
    """The report must name the discrepancy, not merely that one exists."""
    violations = check_all(InvariantContext(amortization=schedule))
    totals = [v for v in violations if v.invariant == "amortization_total_consistent"]
    assert totals, "the $1,505,000 overstatement must be quantified"
    assert totals[0].observed == "11663750"
    assert totals[0].expected == "10158750"
    assert "1,505,000" in totals[0].message


def test_actus_contract_executes_and_falsifies_the_table(schedule):
    """Generate the schedule from the extracted terms and diff it, cell by cell."""
    mapping = map_facility("term_loan")
    assert mapping.contract_type == "LAX", "1%/yr plus bullet is LAX, not LAM"

    corrected = schedule.deduplicated()
    contract = ActusContract(
        contractType=mapping.contract_type,
        statusDate=date(2017, 8, 1),
        initialExchangeDate=date(2017, 8, 1),
        maturityDate=date(2024, 8, 1),
        notionalPrincipal=PRINCIPAL,
        nominalInterestRate=Decimal("0.06"),
        arrayCycleAnchorDateOfPrincipalRedemption=[
            r.payment_date for r in corrected.rows
        ],
        arrayNextPrincipalRedemptionPayment=[r.amount for r in corrected.rows],
    )
    events = generate_schedule(contract)
    redemptions = [e for e in events if e.event_type == "PR"]
    assert len(redemptions) == 27
    assert sum(e.principal for e in redemptions) == Decimal(10_158_750)
    assert events[-1].event_type == "MD"
    assert events[-1].principal == Decimal(140_341_250)
    assert (
        sum(e.principal for e in events if e.event_type in ("PR", "MD"))
        == PRINCIPAL
    ), "amortization plus bullet must reconstruct the original principal"

    diffs = diff_schedule(events, schedule.as_pairs())
    assert diffs, "generated schedule must disagree with the printed table"
    assert any(d.field == "date" for d in diffs), "dates must diverge at the duplicate"


def test_revolver_is_not_force_fitted_to_actus():
    mapping = map_facility("revolver")
    assert mapping.contract_type is None
    assert mapping.executable is False
    assert "CLM" in mapping.rationale


def test_clean_schedule_raises_no_amortization_violations(schedule):
    """Guard against a checker that flags everything."""
    violations = check_all(InvariantContext(amortization=schedule.deduplicated()))
    amort = [v for v in violations if v.invariant.startswith("amortization")]
    assert amort == [], f"corrected schedule should be clean, got {amort}"
