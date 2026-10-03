"""The economic terms of fund and BDC facilities, and their near misses.

Out of sample the rules tier read none of the 61 labelled commitments,
maturities, top margins and commitment fees on twenty BDC facilities. The
readers in ``extract/economics.py`` were written against the in-sample fund
agreements' own drafting after those agreements were labelled, and every form
here is quoted or paraphrased from one of them, named in the test.

Each reader is pinned twice: on the form it reads, and on the near miss that
sits beside the right answer in the same documents -- an accordion ceiling, the
end of a revolving period, a base-rate margin, a default increment, an upfront
fee, a figure fused with the one it replaced. The near misses are the point:
the readers' values are taken at the definitions tier's confidence, so a wrong
one is a confident wrong answer.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest

from credit_extract.extract import economics as E
from credit_extract.extract.passes import OFFLINE_RULES
from credit_extract.graph.definitions import build_definition_graph
from credit_extract.ingest.normalize import NormalizedDocument
from credit_extract.pipeline import run_pipeline
from credit_extract.validate.jev import OfflineJev
from credit_extract.validate.validators import _sentence_window


def _doc(*definitions: str, cover: str = "") -> NormalizedDocument:
    text = (
        cover + "LOAN AND SECURITY AGREEMENT\n\nARTICLE I DEFINITIONS\n\n"
        + "\n\n".join(definitions)
        + "\n\nARTICLE II THE ADVANCES\n\nSection 2.1 Advances. Each Lender "
        "shall make Advances to the Borrower.\n"
    )
    return NormalizedDocument(document_id="t", source_path="t", source_format="txt",
                              text=text)


def _read(reader, *definitions: str, cover: str = ""):
    doc = _doc(*definitions, cover=cover)
    return reader(doc, build_definition_graph(doc))


def _value(reader, *definitions: str, cover: str = ""):
    found = _read(reader, *definitions, cover=cover)
    assert len(found) <= 1
    return found[0].value if found else None


# ---------------------------------------------------------------------------
# revolver.commitment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("definition, expected", [
    # PennantPark, 5C: the other limb only ever lowers it.
    ('" Facility Amount ": As of any date, an amount equal to the lesser of (a) '
     "$200,000,000 and (b) the aggregate principal amount of the Commitments "
     "provided by the Lenders as of such date.", Decimal("200000000")),
    # StepStone: the second limb is the accordion, stated as growth.
    ('" Maximum Facility Amount " means, at any date, (a) $250,000,000 plus (b) '
     "the aggregate amount of New Commitments that have become effective after "
     "the Closing Date.", Decimal("250000000")),
    # SLR.
    ('" Facility Amount " means (a) prior to the end of the Revolving Period, '
     "$35,000,000, unless this amount is permanently reduced pursuant to "
     "Section 2.5 or increased pursuant to Section 2.8.", Decimal("35000000")),
    # BlackRock Monticello: the signed size, the increase option on top of it.
    ('" Maximum Facility Amount " means the aggregate Commitments as then in '
     "effect, which amount shall not exceed $ 410,000,000 (as such amount may be "
     "increased from time to time in accordance with Section 2.13 ).",
     Decimal("410000000")),
])
def test_a_size_definition_in_a_shape_that_fixes_it(definition, expected):
    assert _value(E.commitment_candidates, definition) == expected


@pytest.mark.parametrize("definition", [
    # Ares CP Funding: "may be up to" is a ceiling, and the commitments that
    # make it up are in an annex the filing does not include.
    '" Maximum Facility Amount " means the aggregate Commitments of the Lenders '
    "then in effect, which amount may be up to $ 2,250,000,000 , as such amount "
    "may vary from time to time pursuant to Section 2.18(b).",
    # The accordion form: the only figure is what the size may become.
    '" Maximum Facility Amount " means the Aggregate Commitments as then in '
    "effect, which amount may be increased pursuant to Section 2.18 to "
    "$350,000,000.",
])
def test_an_accordion_ceiling_is_not_a_size(definition):
    assert _value(E.commitment_candidates, definition) is None


@pytest.mark.parametrize("definition", [
    # Eagle Point's conformed copy: the struck figure fused to the new one.
    '" Facility Amount " means the aggregate Commitments. As of the First '
    "Amendment Effective Date, the Facility Amount is $60,000,00075,000,000.",
    '" Facility Amount " means the lesser of (a) $60,000,00075,000,000 and (b) '
    "the aggregate principal amount of the Commitments.",
])
def test_a_figure_fused_with_the_one_it_replaced_is_not_read(definition):
    assert _value(E.commitment_candidates, definition) is None


def test_a_size_handed_to_another_definition_is_followed():
    """The GBDC facility: the Facility Amount is the Maximum Facility Amount,
    which is the figure."""
    found = _read(
        E.commitment_candidates,
        '" Facility Amount " means (a) on or prior to the Facility Termination '
        "Date, an amount equal to the Maximum Facility Amount (as such amount may "
        "be reduced from time to time pursuant to Section 2.07) and (b) following "
        "the Facility Termination Date, the outstanding principal balance of all "
        "Advances.",
        '" Maximum Facility Amount " means $750,000,000 (as such amount may be '
        "reduced pursuant to Section 2.07).",
    )
    assert [c.value for c in found] == [Decimal("750000000")]
    assert "Maximum Facility Amount" in found[0].notes


_TRANCHES = (
    '" Dollar Commitment " means, with respect to each Dollar Lender, its '
    "commitment to make Revolving Loans in Dollars. The aggregate amount of the "
    "Lenders' Dollar Commitments as of the Fourth Amendment Effective Date is "
    "$25,000,000.",
    '" Initial Term Commitment " means, as to each Term Lender, its obligation to '
    "make a Term Loan. The aggregate amount of the Lenders' Initial Term "
    "Commitments as of the Fourth Amendment Effective Date was $ 50,000,000 .",
    '" Multicurrency Commitment " means, with respect to each Multicurrency '
    "Lender, its commitment to make Revolving Loans. The aggregate amount of the "
    "Lenders' Multicurrency Commitments as of the Fourth Amendment Effective "
    "Date is $ 900,000,000 .",
)


def test_the_largest_revolving_tranche_and_never_a_sum():
    """Blue Owl Technology: a term tranche is not a revolver, and $925,000,000
    appears nowhere in the agreement."""
    assert _value(E.commitment_candidates, *_TRANCHES) == Decimal("900000000")


def test_a_total_the_agreement_prints_is_the_aggregate():
    """Fidelity's restated Schedule I prints the tranches' total as one figure,
    which the guide prefers to the largest tranche."""
    schedule = ("SCHEDULE I Commitments Lender | Multicurrency | Dollar | Total "
                "ING Capital LLC | $850,000,000 | $25,000,000 | $875,000,000 "
                "Total | $ 900,000,000 | $25,000,000 | $925,000,000")
    assert _value(E.commitment_candidates, *_TRANCHES, schedule) == Decimal("925000000")


def test_an_amendments_total_dated_before_it_is_stale():
    """Lafayette Square's Amendment No. 1 increases the Maximum Commitment
    and carries the Multicurrency total "as of the Effective Date" through
    unchanged. Blue Owl Technology dates its totals as of the amendment."""
    def doc(as_of):
        return NormalizedDocument(
            document_id="t", source_path="t", source_format="txt",
            text=("AMENDMENT NO. 1 TO SENIOR SECURED REVOLVING CREDIT AGREEMENT\n\n"
                  '" Multicurrency Commitment " means each Lender\'s commitment. The '
                  "aggregate amount of the Lenders' Multicurrency Commitments as of "
                  f"the {as_of} is $75,000,000.00.\n"))
    stale, current = doc("Effective Date"), doc("First Amendment Effective Date")
    assert E.commitment_candidates(stale, build_definition_graph(stale)) == []
    found = E.commitment_candidates(current, build_definition_graph(current))
    assert [c.value for c in found] == [Decimal("75000000.00")]


def test_a_term_loan_agreements_commitments_are_not_a_revolver():
    """Constellation Brands' TERM LOAN CREDIT AGREEMENT states its total in
    the same words a revolver does."""
    statement = ('" Commitment " means, with respect to each Lender, its '
                 "commitment to make a Loan. The aggregate amount of the Lenders' "
                 "Commitments as of the Effective Date is $300,000,000.")
    assert _value(E.commitment_candidates, statement) == Decimal("300000000")
    assert _value(E.commitment_candidates, statement,
                  cover="TERM LOAN CREDIT AGREEMENT dated as of September 18, "
                        "2026\n\n") is None


def test_an_untranched_total_that_includes_term_commitments_is_not_read():
    assert _value(
        E.commitment_candidates,
        '" Commitments " means, collectively, the Term Commitments and the '
        "Revolving Commitments.",
        '" Revolving Commitments " means each Lender\'s commitment to make '
        "Revolving Loans. The aggregate amount of the Lenders' Commitments as of "
        "the Effective Date is $975,000,000.",
    ) is None


# ---------------------------------------------------------------------------
# revolver.maturity_date
# ---------------------------------------------------------------------------


def test_the_earliest_of_a_date_and_an_event_is_the_date():
    """Blue Owl Technology and Fidelity."""
    found = _read(
        E.maturity_candidates,
        '" Maturity Date " means the earliest to occur of (a) September 10, 2031 '
        "and (b) the date on which all Commitments have been terminated and the "
        "aggregate amount of Loans outstanding has been repaid in full.",
    )
    assert [c.value for c in found] == [dt.date(2031, 9, 10)]
    assert "derived" not in found[0].qualifiers


def test_a_reference_chain_cites_the_definition_that_states_the_date():
    """BlackRock Monticello: limb (ii) names the term it defines, which is
    harmless, and the cited span is where the date is written."""
    found = _read(
        E.maturity_candidates,
        '" Maturity Date " means the earliest to occur of (i) the Stated Maturity '
        "Date, (ii) the date of the declaration, or automatic occurrence, of the "
        "Maturity Date pursuant to Section 7.2 and (iii) the Collection Date.",
        '" Collection Date " means the date on which the aggregate outstanding '
        "principal amount of the Advances has been repaid in full.",
        '" Stated Maturity Date " means July 30, 2030.',
    )
    assert [c.value for c in found] == [dt.date(2030, 7, 30)]
    assert "July 30, 2030" in found[0].span.text
    assert "derived" not in found[0].qualifiers


def test_years_after_a_scheduled_end_is_computed_and_marked():
    """5C and PennantPark: the maturity is arithmetic over two definitions, and
    no sentence states it, so the candidate says how it was reached."""
    found = _read(
        E.maturity_candidates,
        '" Revolving Period End Date ": The earlier to occur of (a) the Scheduled '
        "Revolving Period End Date and (b) the date of the declaration of the "
        "Revolving Period End Date pursuant to Section 9.2(a).",
        '" Scheduled Revolving Period End Date ": November 6, 2028.',
        '" Termination Date ": The earliest of (a) the date that is two (2) years '
        "after the Revolving Period End Date, (b) the date of the declaration of "
        "the Termination Date pursuant to Section 9.2(a) or (c) the date of the "
        "termination of the Commitments.",
    )
    assert [c.value for c in found] == [dt.date(2030, 11, 6)]
    assert "Scheduled Revolving Period End Date" in found[0].qualifiers["derived"]


def test_the_date_of_this_agreement_is_the_cover_date():
    """StepStone: the Closing Date is "the date of this Agreement"."""
    found = _read(
        E.maturity_candidates,
        '" Closing Date " means the date of this Agreement.',
        '" Maturity Date " means the earlier of (a) the Scheduled Maturity Date '
        "and (b) the date on which all Loans shall become due and payable in "
        "full hereunder, whether by acceleration or otherwise.",
        '" Scheduled Maturity Date " means the five-year anniversary of the '
        "Closing Date.",
        cover="CREDIT AGREEMENT dated as of September 14, 2026 among the parties "
              "hereto.\n\n",
    )
    assert [c.value for c in found] == [dt.date(2031, 9, 14)]


def test_the_first_business_day_on_or_after_rolls_a_weekend():
    """The GBDC facility's form, with dates moved so that the 36-month
    anniversary falls on a Saturday."""
    found = _read(
        E.maturity_candidates,
        '" Closing Date " means August 17, 2024.',
        '" Facility Termination Date " means the last day of the Reinvestment '
        "Period.",
        '" Final Maturity Date " means the earlier to occur of (i) the first '
        "Business Day on or after the date that is the 36 month anniversary of "
        "the Facility Termination Date and (ii) the date on which the Final "
        "Maturity Date is declared pursuant to Section 6.01.",
        '" Reinvestment Period " means the period from and including the Closing '
        "Date to and including the earlier of (a) the date that is the 3rd "
        "anniversary of the Closing Date and (b) the date of the termination of "
        "the Commitments.",
    )
    assert dt.date(2030, 8, 17).weekday() == 5
    assert [c.value for c in found] == [dt.date(2030, 8, 19)]


@pytest.mark.parametrize("definitions", [
    # SLR: the Closing Date is an event, so nothing downstream of it is dated.
    ('" Closing Date " has the meaning set forth in Section 6.1.',
     '" Facility Termination Date " means the earliest to occur of (i) the date '
     "that is two (2) years after the last day of the Revolving Period and (ii) "
     "the effective date on which the facility hereunder is terminated.",
     '" Revolving Period " means the period of time starting on the Closing Date '
     "and ending on the earliest to occur of (a) an Equityholder RP Extension "
     "Failure Event; (b) the date that is three (3) years after the Closing "
     "Date."),
    # Eagle Point: the only dated limb is a date fused with the one it replaced.
    ('" Commitment Termination Date " means NovemberJune 1226, 20262028, or such '
     "later date to which the Commitment Termination Date may be extended.",
     '" Maturity Date " means the earlier of (a) the date that is three (3) years '
     "after the Commitment Termination Date and (b) the date declared by the "
     "Administrative Agent."),
])
def test_a_chain_that_ends_at_an_event_or_a_fused_date_resolves_to_nothing(definitions):
    assert _read(E.maturity_candidates, *definitions) == []


def test_an_unresolved_maturity_is_not_replaced_by_the_termination_date():
    """Eagle Point's Termination Date is the end of the revolving period; it
    resolves, and reporting it as the maturity would be the near miss."""
    found = _read(
        E.maturity_candidates,
        '" Maturity Date " means the date that is three (3) years after the '
        "Initial Funding Date.",
        '" Termination Date " means the earliest to occur of (a) June 26, 2028 and '
        "(b) the date declared by the Administrative Agent.",
    )
    assert found == []


@pytest.mark.parametrize("evidence, expected", [
    # Lifetime Brands' term loan and Health Catalyst: no revolver to mature.
    ("Amounts repaid or prepaid in respect of Term Loans may not be reborrowed.",
     None),
    # A revolver says so, even beside a term tranche that may not be reborrowed.
    ("Amounts repaid or prepaid in respect of Term Loans may not be reborrowed. "
     "Any Revolving Loan so repaid may, subject to the terms and conditions "
     "hereof, be reborrowed.", dt.date(2031, 9, 10)),
])
def test_a_term_facility_has_no_revolver_maturity(evidence, expected):
    doc = _doc('" Maturity Date " means September 10, 2031.')
    doc = NormalizedDocument(document_id="t", source_path="t", source_format="txt",
                             text=doc.text + "\n" + evidence + "\n")
    found = E.maturity_candidates(doc, build_definition_graph(doc))
    assert (found[0].value if found else None) == expected


def test_the_commitment_termination_date_is_never_the_maturity():
    assert _read(
        E.maturity_candidates,
        '" Commitment Termination Date " means September 10, 2030.',
    ) == []


# ---------------------------------------------------------------------------
# applicable_margin.eurodollar_top_level_pct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("definition, expected", [
    # Blue Owl Technology: the base-rate margin is set aside.
    ('" Applicable Margin " means: (a) with respect to any ABR Loan, 0.875% per '
     "annum; (b) with respect to any Term Benchmark Loan, 1.875% per annum and "
     "(c) with respect to any RFR Loan, 1.875% per annum.", Decimal("1.875")),
    # Fidelity: a coverage grid, and its top level.
    ('" Applicable Margin " means: if the Gross Borrowing Base is (a) less than '
     "the product of 1.60 and the Combined Debt Amount, (i) with respect to any "
     "ABR Loan, 0.875% per annum; and (ii) with respect to any Index Rate Loan, "
     "Term Benchmark Loan or RFR Loan, 1.875% per annum; or (b) equal to or "
     "greater than the product of 1.60 and the Combined Debt Amount, (i) with "
     "respect to any ABR Loan, 0.750% per annum; and (ii) with respect to any "
     "Index Rate Loan, Term Benchmark Loan or RFR loan, 1.750% per annum.",
     Decimal("1.875")),
    # 5C: the default election sits in a parenthesised proviso inside the
    # default limb, and splitting there once made 3.83% the top margin.
    ('" Applicable Spread ": A rate per annum equal to (a) with respect to any '
     "Advance bearing interest at the Benchmark, (i) so long as no Event of "
     "Default has occurred and is continuing, 1.83 % or (ii) if an Event of "
     "Default has occurred and is continuing, at the election (provided that in "
     "the case of any Event of Default described in Section 9.1(h) such election "
     "shall be automatic) of the Administrative Agent, 3.83 % and (b) with "
     "respect to any Advance bearing interest at the Base Rate, (i) so long as no "
     "Event of Default has occurred and is continuing, 0.83 % or (ii) if an "
     "Event of Default has occurred and is continuing, 2.83 %.", Decimal("1.83")),
    # SLR: the default proviso cites its own clauses, which are not new limbs.
    ('" Applicable Margin " means (i) during the Revolving Period, 2.60% per '
     "annum and (ii) after the end of the Revolving Period, 2.80% per annum ; "
     "provided that, after the occurrence and during the continuation of an "
     "Event of Default, the Applicable Margin determined pursuant to the "
     "foregoing clause (i) or clause (ii) , as applicable, shall be increased by "
     "2.00% per annum .", Decimal("2.80")),
    # Ares CP Funding: the rate the eighteenth amendment replaced.
    ('" Applicable Spread " means, (i) prior to the Eighteenth Amendment '
     "Effective Date, 2.00% per annum and (ii) thereafter, 1.80 % per annum ; "
     "provided that, at any time during the existence of an Event of Default or "
     "after the occurrence of the Facility Maturity Date, the Applicable Spread "
     "shall be increased by 2.00% per annum .", Decimal("1.80")),
    # BlackRock Monticello's June facility: "twenty percent (20%) of the
    # rentable units" is a threshold, not a rate.
    ('" Applicable Spread " means, with respect to each Advance, (i) from the '
     "applicable Advance Date through and including the day on which not less "
     "than twenty percent (20%) of the rentable units at the related Mortgaged "
     "Property are subject to active lease agreements, 3.00%, and (ii) from the "
     "day following the Stabilization Date to the Maturity Date, 2.50%.",
     Decimal("3.00")),
    # StepStone.
    ('" Spread " means 1.90% per annum.', Decimal("1.90")),
])
def test_the_top_of_a_margin_schedule(definition, expected):
    assert _value(E.margin_candidates, definition) == expected


def test_an_increment_the_reader_would_have_to_add_is_declined():
    """KKR: 3.00%, and 0.50% more while an LTV test is failed. Both labellers
    wrote 3.50%; the reader does no arithmetic on a margin and says nothing."""
    assert _value(
        E.margin_candidates,
        '" Margin " means three percent (3.00%) per annum in the case of Term '
        "Rate Loans or Daily SOFR Loans and two percent (2.00%) per annum in the "
        "case of Base Rate Loans; provided that, (i) at any time that the LTV is "
        "greater than or equal to the Cash Sweep LTV, the relevant Margin for all "
        "Loans shall be increased by one-half percent (0.50%) per annum, and (ii) "
        "at any time an Event of Default is continuing the relevant Margin for "
        "all Loans shall be increased by two percent (2.00%) per annum.",
    ) is None


def test_a_margin_schedule_that_prices_a_term_loan_or_points_at_a_table_declines():
    """Latham: the revolver's grid is a table the filing does not include, and
    the only rate in the prose is the term loans' 4.00% -- the answer its label
    names as the one to avoid. Either feature alone means the definition does
    not settle the revolver's top margin."""
    latham = (
        '" Applicable Rate " means, for any day, with respect to (a) any Initial '
        "Loan, (x) 4.00% per annum in the case of Term SOFR Rate Loans and (y) "
        "3.00% per annum in the case of ABR Loans; and (b) any Initial Revolving "
        "Loan, the rate per annum applicable to the relevant Class of Loans in "
        "the table set forth below, based upon the First Lien Net Leverage Ratio."
    )
    assert _read(E.margin_candidates, latham) == []
    assert _read(
        E.margin_candidates,
        '" Applicable Margin " means (a) with respect to Term Loans, 3.25% per '
        "annum and (b) with respect to Revolving Loans, 2.75% per annum.",
    ) == []


def test_a_term_only_agreements_margin_is_its_term_loans():
    """Health Catalyst has no revolver; its term loans' margin is the answer."""
    assert _value(
        E.margin_candidates,
        '" Applicable Margin " means, for the Initial Loans and the Delayed Draw '
        "Loans, (i) with respect to SOFR Loans, 6.50% per annum , and (ii) with "
        "respect to ABR Loans, 5.50% per annum .",
    ) == Decimal("6.50")


def test_a_portfolio_loans_spread_is_not_the_facilitys_margin():
    """Eagle Point defines "Spread" as a collateral loan's coupon."""
    assert _read(
        E.margin_candidates,
        '" Spread " means, with respect to Floating Rate Loans, the cash interest '
        "spread (after giving effect to any floor) of such Floating Rate Loan over "
        "the Term SOFR Rate.",
    ) == []


def test_a_margin_set_in_the_fee_letter_is_an_external_reference():
    """PIMCO: "Applicable Margin" has the meaning specified in the Fee Letter."""
    found = _read(E.margin_candidates,
                  '" Applicable Margin " has the meaning specified in the Fee Letter.')
    assert len(found) == 1
    assert found[0].value is None and found[0].external_document == "Fee Letter"


# ---------------------------------------------------------------------------
# commitment_fee_pct
# ---------------------------------------------------------------------------


def test_an_upfront_commitment_fee_is_not_the_unused_fee():
    """BlackRock Monticello's "Commitment Fee" is charged on the whole
    facility, at closing and with each Advance, at the unused fee's rate."""
    assert _read(
        E.fee_candidates,
        '" Commitment Fee " means an aggregate amount equal to the product of the '
        "Maximum Facility Amount and 0.25%, which fee shall be paid (i) in part on "
        "the Closing Date and (ii) on the date of each Advance.",
    ) == []


def test_the_top_tier_of_a_non_usage_fee_and_not_its_thresholds():
    """5C: two periods, two usage tiers, and 35.00% is a threshold."""
    assert _value(
        E.fee_candidates,
        '" Non-Usage Fee ": A fee payable monthly in arrears equal to: (a) for each '
        "day during the first three (3) months following the Effective Date, the "
        "sum of the products of (A) one divided by 360, (B) one-half of one percent "
        "(0.50%) and (C) the Unused Facility Amount; and (b) thereafter, (i) for "
        "each day that the Advances Outstanding are less than the product of "
        "thirty-five percent (35.00%) multiplied by the Facility Amount, the sum of "
        "the products of (A) one divided by 360, (B) three-quarters of one percent "
        "(0.75%) and (C) the Unused Facility Amount; plus (ii) for each day that "
        "the Advances Outstanding are greater than or equal to the product of "
        "thirty-five percent (35.00%) multiplied by the Facility Amount, the sum "
        "of the products of (A) one divided by 360, (B) one-half of one percent "
        "(0.50%) and (C) the Unused Facility Amount.",
    ) == Decimal("0.75")


def test_a_usage_grid_and_its_comparison_operators():
    """KKR's Unused Rate."""
    assert _value(
        E.fee_candidates,
        '" Unused Rate " means the following percentages per annum based upon the '
        "Daily Usage as set forth below: Daily Usage Unused Rate ≤50% 0.65% >50% "
        "0.45%",
    ) == Decimal("0.65")


def test_a_defined_term_set_with_a_unicode_hyphen_is_found():
    """PennantPark defines '" Non‐Usage Fee "' with U+2010."""
    assert _value(
        E.fee_candidates,
        '" Non‐Usage Fee ": With respect to each Lender, a fee equal to the sum '
        "of the products of (A) one divided by 360, (B) three-quarters of one "
        "percent (0.75%) and (C) such Lender's Unused Commitment Amount.",
    ) == Decimal("0.75")


def test_a_fee_rate_set_in_the_fee_letter_is_an_external_reference():
    """SLR's Undrawn Fee Rate; Horizon's Unused Fee names its letter with a
    year, which the first version of the pattern could not read."""
    for definition, letter in [
        ('" Undrawn Fee Rate " has the meaning set forth in the Fee Letter.',
         "Fee Letter"),
        ('" Unused Fee " has the meaning specified in the 2026 Amendment Date Fee '
         "Letter.", "2026 Amendment Date Fee Letter"),
    ]:
        found = _read(E.fee_candidates, definition)
        assert [(c.value, c.external_document) for c in found] == [(None, letter)]


def test_a_fee_the_agreement_says_is_defined_in_a_fee_letter():
    """Eagle Point defines nothing with "means" here; it says '"Unused Fee" is
    defined in the Lender Fee Letter.', and the same of its margin."""
    doc = _doc(
        '"Applicable Margin" is defined in the Lender Fee Letter. '
        '"Unused Fee" is defined in the Lender Fee Letter.',
    )
    graph = build_definition_graph(doc)
    for reader in (E.fee_candidates, E.margin_candidates):
        found = reader(doc, graph)
        assert [(c.value, c.external_document) for c in found] == [
            (None, "Lender Fee Letter")]


def test_unused_fees_payable_in_the_amounts_a_fee_letter_sets():
    """StepStone defines no fee term; Section 2.6 pays "non-utilization fees
    (the " Commitment Fees ") in the amounts set forth in the Commitment Fee
    Letter"."""
    doc = _doc('" Spread " means 1.90% per annum.')
    doc = NormalizedDocument(
        document_id="t", source_path="t", source_format="txt",
        text=doc.text + "\n(b) Commitment Fees . The Borrower Parties agree to "
        "pay to the Administrative Agent for the pro rata benefit of the Lenders "
        'non-utilization fees (the " Commitment Fees ") in the amounts set forth '
        "in the Commitment Fee Letter.\n",
    )
    found = E.fee_candidates(doc, build_definition_graph(doc))
    assert [(c.value, c.external_document) for c in found] == [
        (None, "Commitment Fee Letter")]


_PROSE_FEE = [
    r for r in OFFLINE_RULES
    if r.field == "commitment_fee_pct" and "accrue" in r.pattern
]


def test_the_bdc_revolvers_prose_commitment_fee():
    """Blue Owl Technology and Fidelity state the fee in Section 2.12."""
    (rule,) = _PROSE_FEE
    text = ("The Borrower agrees to pay to the Administrative Agent for the account "
            "of each Revolving Lender a commitment fee, which shall accrue at a rate "
            "per annum equal to 0.350% on the average daily unused amount of the "
            "Dollar Commitment of such Lender.")
    assert re.search(rule.pattern, text, rule.flags).group(1) == "0.350%"


def test_a_stepped_prose_fee_is_not_read_as_its_first_step():
    (rule,) = _PROSE_FEE
    text = ("a commitment fee, which shall accrue at a rate per annum equal to (a) "
            "0.50% if the Utilization is less than 50% and (b) 0.25% otherwise, on "
            "the average daily unused amount")
    assert re.search(rule.pattern, text, rule.flags) is None


# ---------------------------------------------------------------------------
# libor_floor_pct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("definitions, expected", [
    # KKR: no unit at all.
    (('" Floor " means zero (0).',), Decimal("0")),
    # BlackRock Monticello's June facility: the floor is inside Term SOFR.
    (('" Term SOFR " means for any calculation with respect to a SOFR Advance, '
      "the greater of (x) three percent (3.00%) per annum, or (y) the Term SOFR "
      "Reference Rate for a tenor of one month.",), Decimal("3.00")),
    # SLR.
    (('" Term SOFR " means the greater of (i) 0.25% and (ii) the Term SOFR '
      "Reference Rate for a tenor comparable to the applicable Interest Period.",),
     Decimal("0.25")),
    # StepStone.
    (('" Adjusted Term SOFR " means Term SOFR plus the Term SOFR Adjustment; '
      "provided that Adjusted Term SOFR shall at no time be less than 0.0% per "
      "annum.",), Decimal("0.0")),
])
def test_a_benchmark_floor_where_the_floor_definition_does_not_state_one(definitions, expected):
    assert _value(E.floor_candidates, *definitions) == expected


def test_a_floor_definition_with_a_percentage_is_the_definitions_tiers():
    assert _read(E.floor_candidates, '" Floor " means a rate of interest equal to 0.50%.') == []


def test_a_base_rate_floor_is_not_the_benchmark_floor():
    assert _read(
        E.floor_candidates,
        '" Alternate Base Rate " means the greatest of (a) the Prime Rate, (b) the '
        "Federal Funds Effective Rate plus 0.50% and (c) 1.00%; provided that the "
        "Alternate Base Rate shall at no time be less than 1.00% per annum.",
    ) == []


# ---------------------------------------------------------------------------
# Validator E reads a definition as its own sentence
# ---------------------------------------------------------------------------


def test_the_external_dependency_window_is_the_definition():
    """Ares CP Funding's spread ends "per annum ." -- a space before the stop --
    so the next ". " fell inside the following definition, and validator E asked
    whether Exhibit A sets the margin."""
    doc = _doc(
        '" Applicable Spread " means, (i) prior to the Eighteenth Amendment '
        "Effective Date, 2.00% per annum and (ii) thereafter, 1.80 % per annum .",
        '" Approval Notice " means the written notice, in substantially the form '
        "attached hereto as Exhibit A , evidencing the approval by the Agent.",
    )
    graph = build_definition_graph(doc)
    window = _sentence_window(doc, graph.get("Applicable Spread").span)
    assert "1.80 %" in window and "Exhibit" not in window


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


FUND_FACILITY = (
    "LOAN AND SECURITY AGREEMENT dated as of September 1, 2026 among PPIF "
    "FUNDING LLC, as the Borrower, the Lenders party hereto, and CANADIAN BANK OF "
    "COMMERCE, as Administrative Agent.\n\n"
    "ARTICLE I DEFINITIONS\n\n"
    'Section 1.1 Certain Defined Terms. " Applicable Spread ": A rate per annum '
    "equal to: (a) prior to the twelve (12) month anniversary of the Closing "
    "Date, 1.875%; (b) thereafter but prior to the Revolving Period End Date, "
    "2.00%; and (c) after the Revolving Period End Date, 2.125%; provided that if "
    "an Event of Default has occurred and is continuing, the rate otherwise in "
    "effect shall be increased by 2.00%.\n\n"
    '" Closing Date ": October 1, 2025.\n\n'
    '" Facility Amount ": As of any date, an amount equal to the lesser of (a) '
    "$200,000,000 and (b) the aggregate principal amount of the Commitments; "
    "provided that the Facility Amount may be increased pursuant to Section 2.18 "
    "to an amount not to exceed $300,000,000.\n\n"
    '" Floor ": A rate of interest equal to 0.0%.\n\n'
    '" Non-Usage Fee ": A fee equal to the sum of the products of (A) one divided '
    "by 360, (B) three-quarters of one percent (0.75%) and (C) the Unused "
    "Commitment Amount.\n\n"
    '" Revolving Period End Date ": The earliest to occur of (a) the Scheduled '
    "Revolving Period End Date and (b) the date of the declaration of the "
    "Revolving Period End Date pursuant to Section 9.2(a).\n\n"
    '" Scheduled Revolving Period End Date ": October 2, 2028.\n\n'
    '" Termination Date ": The earlier of (a) the date that is two (2) years '
    "after the Revolving Period End Date or (b) the date of the declaration of "
    "the Termination Date pursuant to Section 9.2(a).\n\n"
    "ARTICLE II THE ADVANCES\n\n"
    "Section 2.1 Advances. Each Lender shall make Advances to the Borrower in an "
    "aggregate principal amount not to exceed the Facility Amount.\n\n"
    "Section 9.9 Governing Law. This Agreement shall be governed by the laws of "
    "the State of New York.\n"
)


def test_a_fund_facility_end_to_end(tmp_path: Path):
    """The readers reach the output through the pipeline, outrank nothing they
    should not, and the accordion never becomes the size."""
    source = tmp_path / "fund.txt"
    source.write_text(FUND_FACILITY)
    fields = run_pipeline(source, jev_backend=OfflineJev()).fields
    assert fields["revolver.commitment"].value == Decimal("200000000")
    assert fields["revolver.maturity_date"].value == dt.date(2030, 10, 2)
    assert fields["applicable_margin.eurodollar_top_level_pct"].value == Decimal("2.125")
    assert fields["commitment_fee_pct"].value == Decimal("0.75")
    assert fields["libor_floor_pct"].value == Decimal("0.0")
