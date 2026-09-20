"""Build the integration fixture and the gold corpus.

The spec names the Paya / GTCR-Ultra credit agreement on SEC EDGAR as the
primary test document. This build environment has no egress to sec.gov, so this
module emits a *synthetic surrogate*: fictional parties, real trap structure,
and the exact arithmetic the spec calls out.

    Trap 1  31 amortization rows for a 27-quarter period; 2023 appears twice.
            Literal sum $11,663,750 against a correct $10,158,750.
    Trap 2  four pre-closing quarters of Consolidated EBITDA fixed by table,
            overriding the definition entirely.
    Trap 3  add-back clause (a)(xvi) capped only by the Sponsor Model, a
            spreadsheet that is expressly not a Loan Document. The visible 25%
            cap governs clauses (a)(xiii)-(a)(xv) and not (a)(xvi).
    Trap 4  MFN protection at 50bps with no sunset provision anywhere.

``scripts/fetch_corpus.py`` pulls the genuine EDGAR exhibit when egress allows;
the trap tests run against it in preference to this surrogate.

:class:`Variant` parameterizes the document so the calibration corpus can be
generated deterministically. The default ``Variant()`` reproduces the canonical
fixture byte for byte; ``tests/test_fixture_stability.py`` asserts it.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent

TERM_PRINCIPAL = 150_500_000
QUARTERLY_AMORT = 376_250          # 0.25% of original principal per quarter
UNIQUE_QUARTERS = 27
DUPLICATED_YEAR = 2023
OPENING_EBITDA_QUARTERS = {
    "September 30, 2016": 11_206_749,
    "December 31, 2016": 10_115_035,
    "March 31, 2017": 9_449_315,
    "June 30, 2017": 9_699_216,
}

DEFAULT_COVENANT_LEVELS: tuple[tuple[str, str], ...] = (
    ("December 31, 2017 through December 31, 2018", "6.50:1.00"),
    ("March 31, 2019 through December 31, 2019", "6.25:1.00"),
    ("March 31, 2020 through December 31, 2020", "6.00:1.00"),
    ("March 31, 2021 through December 31, 2021", "5.75:1.00"),
    ("March 31, 2022 and thereafter", "5.50:1.00"),
)

DEFAULT_PRICING: tuple[tuple[str, str, str, str], ...] = (
    ("I", "Greater than 5.50:1.00", "5.00%", "4.00%"),
    ("II", "Less than or equal to 5.50:1.00 but greater than 4.50:1.00",
     "4.75%", "3.75%"),
    ("III", "Less than or equal to 4.50:1.00", "4.50%", "3.50%"),
)


@dataclass(frozen=True)
class Variant:
    """Parameters of one synthetic agreement."""

    variant_id: str = "meridian_2017"
    holdings: str = "MERIDIAN PAYMENTS HOLDINGS, LLC"
    borrower: str = "MERIDIAN PAYMENTS INTERMEDIATE, LLC"
    admin_agent: str = "NORTHGATE CAPITAL LP"
    syndication_agent: str = "CEDAR POINT CREDIT PARTNERS LLC"

    closing: date = date(2017, 8, 1)
    first_payment: date = date(2017, 12, 31)
    term_maturity: date = date(2024, 8, 1)
    revolver_maturity: date = date(2022, 8, 1)

    term_principal: int = TERM_PRINCIPAL
    amort_bps_per_quarter: int = 25
    unique_quarters: int = UNIQUE_QUARTERS
    duplicated_year: int | None = DUPLICATED_YEAR

    revolver_commitment: int = 20_000_000
    dd_commitment: int = 25_000_000
    lc_sublimit: int = 5_000_000
    netting_cap: int = 20_000_000

    libor_floor: str = "1.00"
    commitment_fee: str = "0.50"
    fronting_fee: str = "0.125"
    ticking_fee: str = "1.00"
    ecf_sweep: str = "50"
    call_protection_pct: str = "1.00"

    opening_ebitda: tuple[tuple[str, int], ...] = tuple(
        OPENING_EBITDA_QUARTERS.items()
    )
    addback_cap: str = "25"
    sponsor_model_clause: bool = True

    mfn_pct: str = "0.50"
    mfn_sunset_months: int | None = None

    covenant_levels: tuple[tuple[str, str], ...] = DEFAULT_COVENANT_LEVELS
    pricing: tuple[tuple[str, str, str, str], ...] = DEFAULT_PRICING

    pm_basket_amount: int = 15_000_000
    pm_basket_pct: str = "35"
    incremental_amount: int = 30_000_000
    incremental_test: str = "4.75"

    #: Which deal kind to emit. Changes which sections exist at all, which is
    #: the point: an ABL has no amortization table and an ARR loan has no
    #: EBITDA anywhere, so a pipeline that assumes either is wrong by default.
    archetype: str = "cash_flow_term_loan"
    abl_advance_accounts: str = "85"
    abl_advance_inventory: str = "65"
    abl_availability_block: int = 7_500_000
    arr_leverage: str = "6.00"
    arr_min_liquidity: int = 10_000_000
    nav_ltv_cap: str = "25"
    pik_step_up: str = "0.75"
    #: Moves the incremental capacity into a schedule and files the document
    #: with that schedule omitted -- an artifact of the source, not a deal term.
    omitted_schedules: bool = False
    #: Injects a superseded figure that competes with the real one.
    decoy: bool = False
    #: Drops the maturity column and restates definitions the rules anchor on.
    alt_phrasing: bool = False

    @property
    def has_amortization(self) -> bool:
        return self.archetype not in ("abl_revolver", "nav_or_subscription")

    @property
    def has_ebitda(self) -> bool:
        return self.archetype not in ("recurring_revenue", "nav_or_subscription")

    @property
    def quarterly_amort(self) -> int:
        return self.term_principal * self.amort_bps_per_quarter // 10_000

    @property
    def ltm_ebitda(self) -> int:
        return sum(amount for _, amount in self.opening_ebitda)

    @property
    def opening_leverage(self) -> str:
        return f"{self.term_principal / self.ltm_ebitda:.2f}"

    def amortization_dates(self) -> list[date]:
        dates: list[date] = []
        year, month = self.first_payment.year, self.first_payment.month
        while len(dates) < self.unique_quarters:
            dates.append(date(year, month, {3: 31, 6: 30, 9: 30, 12: 31}[month]))
            month += 3
            if month > 12:
                month -= 12
                year += 1
        return dates

    def amortization_rows(self) -> list[tuple[date, int]]:
        """The table as the document prints it -- 2023 duplicated (Trap 1)."""
        rows = [(d, self.quarterly_amort) for d in self.amortization_dates()]
        if self.duplicated_year is None:
            return rows
        duplicates = [r for r in rows if r[0].year == self.duplicated_year]
        if not duplicates:
            return rows
        last = max(
            i for i, r in enumerate(rows) if r[0].year == self.duplicated_year
        )
        return rows[: last + 1] + duplicates + rows[last + 1:]


# Backwards-compatible module-level helpers for the canonical variant.
def amortization_dates() -> list[date]:
    return Variant().amortization_dates()


def amortization_rows() -> list[tuple[date, int]]:
    return Variant().amortization_rows()


def _fmt(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


#: Entity suffixes that stay upper-case when a party name is title-cased.
_ENTITY_SUFFIXES = frozenset({"LP", "LLC", "INC", "LTD", "PLC", "LLP", "N.A.", "CO"})


def _title(name: str) -> str:
    """Title-case a party name the way an agreement does.

    Agreements shout party names in the preamble and title-case them in the
    definitions, which means the extractor has to cope with both forms of the
    same entity -- so the fixture reproduces the difference rather than
    normalizing it away.
    """
    return " ".join(
        word if word.upper().strip(",.") in _ENTITY_SUFFIXES else word.title()
        for word in name.split()
    )


def _money(n: int) -> str:
    return "$" + format(n, ",d")


def _row(cells: list[str], header: bool = False) -> str:
    tag = "th" if header else "td"
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


def _table(headers: list[str], rows: list[list[str]], caption: str = "") -> str:
    cap = f"<caption>{caption}</caption>" if caption else ""
    body = "".join(_row(r) for r in rows)
    return (
        '<table border="1" cellpadding="4">'
        + cap + _row(headers, header=True) + body + "</table>"
    )


# ---------------------------------------------------------------------------
# Document body
# ---------------------------------------------------------------------------


#: Defined terms that only make sense when the deal is covenanted on earnings.
EBITDA_DEPENDENT_TERMS = frozenset({
    "Available Amount",
    "Consolidated Senior Secured First Lien Net Indebtedness",
    "Senior Secured First Lien Net Leverage Ratio",
    "Total Leverage Ratio",
})


def definitions(v: Variant) -> list[tuple[str, str]]:
    terms = _all_definitions(v)
    if v.has_ebitda:
        return terms
    # An ARR or NAV facility has no EBITDA anywhere. Leaving the leverage
    # definitions in would make the fixture a hybrid that exists nowhere, and
    # a detector that cannot classify a hybrid is behaving correctly.
    return [(t, b) for t, b in terms if t not in EBITDA_DEPENDENT_TERMS]


def _all_definitions(v: Variant) -> list[tuple[str, str]]:
    return [
        ("Administrative Agent",
         f'means {_title(v.admin_agent)}, in its capacity as administrative agent for '
         'the Lenders hereunder, and its permitted successors in such capacity.'),
        ("All-In Yield",
         'means, as to any Indebtedness, the yield thereof, whether in the form of '
         'interest rate, margin, original issue discount, upfront fees or a LIBO '
         'Rate floor, in each case as determined by the Administrative Agent in '
         'consultation with the Borrower; provided that original issue discount and '
         'upfront fees shall be equated to interest based on an assumed four-year '
         'average life to maturity.'),
        ("Applicable Margin",
         'means, for any day, with respect to any Initial Term Loan or Revolving '
         'Credit Loan, the applicable rate per annum set forth in Section 2.12(b) '
         'under the caption "Eurodollar Rate" or "Base Rate", as the case may be, '
         'based upon the Senior Secured First Lien Net Leverage Ratio as of the end '
         'of the most recently ended Test Period.'),
        ("Available Amount",
         'means, at any time, an amount equal to the greater of (a) $12,000,000 and '
         '(b) 25% of Consolidated EBITDA for the most recently ended Test Period, '
         'calculated on a Pro Forma Basis after giving effect to the add-backs '
         'described in clause (a) of the definition of Consolidated EBITDA, plus '
         'the Cumulative Retained Excess Cash Flow Amount.'),
        ("Closing Date", f'means {_fmt(v.closing)}.'),
        ("Consolidated Net Income",
         'means, for any period, the net income (loss) of Holdings and its '
         'Restricted Subsidiaries for such period, determined on a consolidated '
         'basis in accordance with GAAP, excluding the income (or loss) of any '
         'Unrestricted Subsidiary except to the extent of cash distributions '
         'actually received.'),
        ("Consolidated Total Debt",
         'means, as of any date, the aggregate principal amount of Indebtedness of '
         'Holdings and its Restricted Subsidiaries outstanding on such date, '
         'consisting of Indebtedness for borrowed money, Capital Lease Obligations '
         'and debt obligations evidenced by promissory notes or similar '
         'instruments, determined on a consolidated basis; provided that '
         'obligations in respect of undrawn Letters of Credit shall be excluded.'),
        ("Consolidated Senior Secured First Lien Net Indebtedness",
         'means, as of any date, Consolidated Total Debt as of such date that is '
         'secured by a Lien on the Collateral that is senior in priority to, or '
         'pari passu with, the Liens securing the Initial Term Loans, minus the '
         'aggregate amount of Unrestricted Cash as of such date in an amount not to '
         f'exceed {_money(v.netting_cap)}.'),
        ("Senior Secured First Lien Net Leverage Ratio",
         'means, as of any date, the ratio of (a) Consolidated Senior Secured First '
         'Lien Net Indebtedness as of such date to (b) Consolidated EBITDA for the '
         'Test Period most recently ended on or prior to such date.'),
        ("Total Leverage Ratio",
         'means, as of any date, the ratio of (a) Consolidated Total Debt as of '
         'such date to (b) Consolidated EBITDA for the Test Period most recently '
         'ended on or prior to such date.'),
        ("Delayed Draw Term Loan Commitment",
         'means, with respect to each Lender, the commitment of such Lender to make '
         'Delayed Draw Term Loans hereunder in an aggregate principal amount not to '
         'exceed the amount set forth opposite such Lender’s name on Schedule '
         '1.01(a) under the caption "Delayed Draw Term Loan Commitment". The '
         'aggregate Delayed Draw Term Loan Commitments on the Closing Date are '
         f'{_money(v.dd_commitment)}.'),
        ("Disclosure Letter",
         'means that certain disclosure letter dated as of the Closing Date '
         'delivered by the Borrower to the Administrative Agent, which is not a '
         'Loan Document.'),
        ("Indebtedness",
         'means, as to any Person, without duplication, (a) all obligations for '
         'borrowed money, (b) all obligations evidenced by bonds, debentures, notes '
         'or similar instruments, (c) all Capital Lease Obligations and (d) all '
         'Guarantees of the foregoing.'),
        ("Initial Term Loan",
         'means a term loan made by a Lender to the Borrower on the Closing Date '
         'pursuant to Section 2.01(a).'),
        ("Initial Term Loan Commitment",
         'means, with respect to each Lender, the commitment of such Lender to make '
         'an Initial Term Loan on the Closing Date. The aggregate amount of the '
         'Initial Term Loan Commitments on the Closing Date is '
         f'{_money(v.term_principal)}.'),
        ("Initial Term Loan Maturity Date", f'means {_fmt(v.term_maturity)}.'),
        ("LIBO Rate",
         'means, for any Interest Period, the rate per annum appearing on the '
         'applicable Reuters screen page as the London interbank offered rate for '
         'deposits in Dollars for a period equal to such Interest Period; provided '
         f'that the LIBO Rate shall not at any time be less than {v.libor_floor}% '
         'per annum.'),
        ("Restricted Subsidiary",
         'means any Subsidiary of Holdings other than an Unrestricted Subsidiary.'),
        ("Revolving Credit Commitment",
         'means, with respect to each Lender, the commitment of such Lender to make '
         'Revolving Credit Loans and to acquire participations in Letters of '
         'Credit. The aggregate Revolving Credit Commitments on the Closing Date '
         f'are {_money(v.revolver_commitment)}.'),
        ("Revolving Credit Maturity Date", f'means {_fmt(v.revolver_maturity)}.'),
        ("Sponsor Model",
         'means that certain financial model prepared by the Sponsor and delivered '
         'to the Administrative Agent on May 31, 2017, as in effect on the Closing '
         'Date. For the avoidance of doubt, the Sponsor Model is not a Loan '
         'Document and is not attached hereto.'),
        ("Test Period",
         'means, as of any date, the period of four consecutive fiscal quarters of '
         'Holdings most recently ended on or prior to such date for which financial '
         'statements have been delivered.'),
        ("Unrestricted Cash",
         'means unrestricted cash and Cash Equivalents of Holdings and its '
         'Restricted Subsidiaries that would be stated on a consolidated balance '
         'sheet prepared in accordance with GAAP.'),
        ("Unrestricted Subsidiary",
         'means any Subsidiary of Holdings designated as such by the Borrower in '
         'accordance with Section 5.14, and any Subsidiary of such a Subsidiary, '
         'in each case so long as such Subsidiary is not a Restricted Subsidiary.'),
    ]


EBITDA_CLAUSES = [
    ("i", "provision for taxes based on income, profits or capital"),
    ("ii", "Consolidated Interest Expense"),
    ("iii", "depreciation and amortization expense"),
    ("iv", "any non-cash charges, losses or expenses"),
    ("v", "transaction costs, fees and expenses incurred in connection with "
          "the Transactions"),
    ("vi", "any expenses in connection with any Permitted Acquisition, whether "
           "or not consummated"),
    ("vii", "any losses attributable to the early extinguishment of "
            "Indebtedness"),
    ("viii", "any non-cash compensation expense recorded from grants of stock "
             "options or other equity awards"),
    ("ix", "any net loss from discontinued operations"),
    ("x", "management, monitoring, consulting and advisory fees paid to the "
          "Sponsor"),
    ("xi", "any net loss resulting from Hedging Obligations"),
    ("xii", "restructuring charges, accruals or reserves"),
    ("xiii", "the amount of net cost savings projected by the Borrower in good "
             "faith to be realized as a result of actions taken or expected to "
             "be taken"),
    ("xiv", "business optimization expenses, including severance and "
            "retention"),
    ("xv", "the amount of pro forma cost savings related to any Permitted "
           "Acquisition"),
    ("xvi", "the amount of “run-rate” cost savings, operating expense "
            "reductions and synergies set forth in the Sponsor Model, in an "
            "amount not to exceed, with respect to each such item, the amount "
            "set forth in the Sponsor Model with respect thereto"),
]


def consolidated_ebitda_definition(v: Variant = Variant()) -> str:
    clauses_source = EBITDA_CLAUSES if v.sponsor_model_clause else EBITDA_CLAUSES[:-1]
    clauses = " ".join(f"({num}) {body};" for num, body in clauses_source[:-1])
    last = clauses_source[-1]
    override_rows = [[quarter, _money(amount)] for quarter, amount in v.opening_ebitda]
    override_block = (
        '<p>Notwithstanding anything to the contrary in this definition, '
        'Consolidated EBITDA for each of the fiscal quarters set forth below '
        'shall be deemed to be the amount set forth opposite such fiscal '
        'quarter, and the foregoing provisions of this definition shall not '
        'apply to any such fiscal quarter:</p>'
        + _table(["Fiscal Quarter Ended", "Consolidated EBITDA"], override_rows)
    ) if override_rows else ""
    return (
        '<p><b>"Consolidated EBITDA"</b> means, for any period, Consolidated Net '
        'Income for such period, <i>plus</i> (a) without duplication and to the '
        'extent deducted in determining Consolidated Net Income for such period, '
        'the sum of: ' + clauses +
        f' and ({last[0]}) {last[1]}; <i>minus</i> (b) without duplication and '
        'to the extent included in Consolidated Net Income, any non-cash gains '
        'for such period; <i>provided</i> that the aggregate amount added back '
        'pursuant to clauses (a)(xiii) through (a)(xv) above shall not exceed '
        f'{v.addback_cap}% of Consolidated EBITDA for such period (calculated '
        'prior to giving effect to any add-back pursuant to such clauses).</p>'
        + override_block
    )


def amortization_section(v: Variant = Variant()) -> str:
    rows = [[_fmt(d), _money(amount)] for d, amount in v.amortization_rows()]
    return (
        "<p>SECTION 2.10 Repayment of Loans.</p>"
        "<p>(a) The Borrower shall repay the outstanding principal amount of "
        "the Initial Term Loans in consecutive quarterly installments on the "
        "dates set forth in the table below, in the amounts set forth opposite "
        "such dates:</p>"
        + _table(["Payment Date", "Principal Amortization Payment"], rows)
        + "<p>(b) To the extent not previously repaid, the outstanding "
          "principal amount of the Initial Term Loans, together with all "
          "accrued and unpaid interest thereon, shall be due and payable in "
          "full on the Initial Term Loan Maturity Date.</p>"
    )


TOC_ENTRIES = [
    ("ARTICLE I", "DEFINITIONS"),
    ("1.01", "Defined Terms"),
    ("1.02", "Terms Generally"),
    ("1.03", "Accounting Terms; Pro Forma Basis"),
    ("ARTICLE II", "THE CREDITS"),
    ("2.01", "Commitments"),
    ("2.05", "Letters of Credit"),
    ("2.09", "Fees"),
    ("2.10", "Repayment of Loans"),
    ("2.11", "Prepayment of Loans"),
    ("2.12", "Interest"),
    ("2.14", "Incremental Facilities"),
    ("2.16", "Call Protection"),
    ("ARTICLE VI", "NEGATIVE COVENANTS"),
    ("6.01", "Indebtedness"),
    ("6.12", "Financial Covenant"),
    ("ARTICLE IX", "MISCELLANEOUS"),
    ("9.01", "Notices"),
]



ARCHETYPE_SECTIONS: dict[str, str] = {
    "abl_revolver": """
<p>SECTION 2.02 Borrowing Base. The Borrowing Base means, at any time, the sum
of (a) {accounts}% of the face amount of Eligible Accounts, plus (b)
{inventory}% of the value of Eligible Inventory valued at the lower of cost or
market, minus (c) the Availability Block and any Reserves established by the
Administrative Agent in its Permitted Discretion. Availability Block means
{block}. The Administrative Agent shall exercise dominion over the Borrower's
deposit accounts during any Cash Dominion Period.</p>
""",
    "recurring_revenue": """
<p>SECTION 6.13 ARR Financial Covenants. Annualized Recurring Revenue means, as
of any date, the aggregate annualized contractually recurring subscription
revenue of the Borrower and its Restricted Subsidiaries. This is a Recurring
Revenue Loan and no covenant herein is measured against earnings. Holdings will
not permit the ARR Leverage Ratio as of the last day of any Test Period to
exceed {arr_leverage}:1.00. Holdings will not permit Minimum Liquidity to be
less than {liquidity} at any time.</p>
""",
    "nav_or_subscription": """
<p>SECTION 6.14 Portfolio Tests. The Borrower is a fund whose obligations are
secured by uncalled capital commitments of its limited partners and by its
portfolio investments. Net Asset Value means the aggregate fair value of the
Portfolio Investments. The Borrower shall not permit the Loan-to-Value Ratio to
exceed {ltv}% at any time.</p>
""",
    "holdco_pik": """
<p>SECTION 2.13 PIK Toggle. Interest on the Loans may at the Borrower's
election be paid in kind by capitalizing such interest, in which case the
Applicable Margin shall be increased by {pik}% per annum for the applicable
Interest Period. The Borrower is a holding company and the Obligations are
structurally subordinated to the obligations of its operating subsidiaries.</p>
""",
    "second_lien": """
<p>SECTION 1.04 Lien Priority. This is a Second Lien Credit Agreement. The
Liens securing the Obligations are junior lien and second priority to the Liens
securing the First Lien Obligations, and are subject in all respects to the
Intercreditor Agreement.</p>
""",
}


def archetype_section(v: Variant) -> str:
    """Sections that exist only for this deal kind."""
    template = ARCHETYPE_SECTIONS.get(v.archetype)
    if not template:
        return ""
    return template.format(
        accounts=v.abl_advance_accounts,
        inventory=v.abl_advance_inventory,
        block=_money(v.abl_availability_block),
        arr_leverage=v.arr_leverage,
        liquidity=_money(v.arr_min_liquidity),
        ltv=v.nav_ltv_cap,
        pik=v.pik_step_up,
    )


def build_html(v: Variant = Variant()) -> str:
    defs = "".join(f'<p><b>"{term}"</b> {body}</p>' for term, body in definitions(v))
    toc = "".join(f"<p>{num} {title}</p>" for num, title in TOC_ENTRIES)
    commitment_rows = [
        ["Initial Term Loan Facility", _money(v.term_principal),
         _fmt(v.term_maturity)],
        ["Delayed Draw Term Loan Facility", _money(v.dd_commitment),
         _fmt(v.term_maturity)],
        ["Revolving Credit Facility", _money(v.revolver_commitment),
         _fmt(v.revolver_maturity)],
    ]
    if not v.has_amortization:
        commitment_rows = [commitment_rows[-1]]     # revolver only
    headers = ["Facility", "Aggregate Commitment", "Maturity"]
    if v.alt_phrasing:
        # No maturity column: the dates must then come from the definitions
        # alone, which is where a recall failure shows up.
        headers = headers[:2]
        commitment_rows = [row[:2] for row in commitment_rows]
    commitment_table = _table(headers, commitment_rows)
    pricing_grid = _table(
        ["Level", "Senior Secured First Lien Net Leverage Ratio",
         "Eurodollar Rate", "Base Rate"],
        [list(row) for row in v.pricing],
    )
    covenant_grid = _table(
        ["Test Period Ending", "Maximum Total Leverage Ratio"],
        [list(row) for row in v.covenant_levels],
    )
    financial_covenant_block = (
        "<p>SECTION 6.12 Financial Covenant. Holdings will not permit the Total "
        "Leverage Ratio as of the last day of any Test Period to exceed the "
        "ratio set forth below opposite such Test Period:</p>\n" + covenant_grid
        if v.has_ebitda else ""
    )
    leverage_rep_block = (
        "<p>SECTION 4.14 Solvency; Closing Date Leverage. As of the Closing "
        "Date, after giving effect to the Transactions and calculated on a Pro "
        f"Forma Basis, the Total Leverage Ratio is {v.opening_leverage}:1.00.</p>"
        if v.has_ebitda else
        "<p>SECTION 4.14 Solvency. As of the Closing Date, after giving effect "
        "to the Transactions, the Borrower is Solvent.</p>"
    )
    pm_basket = (
        f"the greater of {_money(v.pm_basket_amount)} and {v.pm_basket_pct}% of "
        "Consolidated EBITDA for the most recently ended Test Period, "
        "calculated on a Pro Forma Basis after giving effect to the add-backs "
        "described in clause (a) of the definition thereof"
        if v.has_ebitda else _money(v.pm_basket_amount)
    )
    other_basket = (
        "the greater of $10,000,000 and 20% of Consolidated EBITDA"
        if v.has_ebitda else "$10,000,000"
    )
    incremental_test_block = (
        " plus unlimited additional amounts so long as the Senior Secured "
        f"First Lien Net Leverage Ratio would not exceed {v.incremental_test}"
        ":1.00 on a Pro Forma Basis" if v.has_ebitda else ""
    )
    decoy_block = (
        "<p>Prior to giving effect to the First Amendment, the aggregate "
        f"Revolving Credit Commitments on the Closing Date are "
        f"{_money(v.revolver_commitment // 2)}, which amount was superseded in "
        "its entirety on the First Amendment Effective Date.</p>"
        if v.decoy else ""
    )
    amortization_block = amortization_section(v) if v.has_amortization else (
        "<p>SECTION 2.10 Repayment of Loans. The Loans shall be repaid in full "
        "on the Maturity Date. There is no scheduled amortization.</p>"
    )
    ebitda_block = consolidated_ebitda_definition(v) if v.has_ebitda else ""
    omission_notice = (
        "<p>The Schedules and Exhibits to this Agreement have been omitted "
        "pursuant to Item 601(a)(5) of Regulation S-K. The Registrant hereby "
        "agrees to furnish supplementally a copy of any omitted schedule to "
        "the Securities and Exchange Commission upon request.</p>"
        if v.omitted_schedules else ""
    )
    incremental_capacity = (
        "the amount set forth on Schedule 2.14"
        if v.omitted_schedules
        else (
            f"the greater of {_money(v.incremental_amount)} and 100% of "
            "Consolidated EBITDA" if v.has_ebitda
            else _money(v.incremental_amount)
        )
    )
    mfn_sunset = (
        f" This clause (b) shall not apply to any Incremental Term Facility "
        f"incurred after the date that is {v.mfn_sunset_months} months after "
        "the Closing Date."
        if v.mfn_sunset_months is not None else ""
    )
    return f"""<!DOCTYPE html>
<!-- SYNTHETIC TEST FIXTURE. Fictional parties. Not a real credit agreement and
     not a filing by any real issuer. Generated by build_fixture.py to carry the
     four documented traps with the arithmetic given in the project spec. -->
<html><head><meta charset="utf-8"><title>Credit Agreement (synthetic fixture)</title></head>
<body>
<p>EXHIBIT 10.1</p>
<p>CREDIT AGREEMENT</p>
<p>dated as of {_fmt(v.closing)}</p>
<p>among</p>
<p>{v.holdings}, as Holdings,</p>
<p>{v.borrower}, as the Borrower,</p>
<p>THE LENDERS PARTY HERETO FROM TIME TO TIME,</p>
<p>and</p>
<p>{v.admin_agent}, as Administrative Agent and Collateral Agent</p>
<p>{v.admin_agent}, as Lead Arranger and Sole Bookrunner</p>
<p>{v.syndication_agent}, as Syndication Agent</p>
<hr>
<p>TABLE OF CONTENTS</p>
{toc}
<hr>
<p>ARTICLE I</p>
<p>DEFINITIONS</p>
<p>SECTION 1.01 Defined Terms. As used in this Agreement, the following terms
have the meanings specified below:</p>
{defs}
{ebitda_block}
<p>SECTION 1.02 Terms Generally. The definitions of terms herein shall apply
equally to the singular and plural forms of the terms defined.</p>
<p>SECTION 1.03 Accounting Terms; Pro Forma Basis. All financial statements to
be delivered hereunder shall be prepared in accordance with GAAP. Calculations
on a "Pro Forma Basis" shall give effect to each Subject Transaction as if it
had occurred on the first day of the applicable Test Period.</p>
<hr>
<p>ARTICLE II</p>
<p>THE CREDITS</p>
<p>SECTION 2.01 Commitments. Subject to the terms and conditions set forth
herein, each Lender severally agrees to make its Initial Term Loan to the
Borrower on the Closing Date in a principal amount equal to its Initial Term
Loan Commitment. The Commitments on the Closing Date are as follows:</p>
{commitment_table}
{decoy_block}
<p>SECTION 2.05 Letters of Credit. The aggregate face amount of all Letters of
Credit outstanding at any time shall not exceed {_money(v.lc_sublimit)} (the
"Letter of Credit Sublimit"). The Borrower shall pay to the Issuing Bank a
fronting fee equal to {v.fronting_fee}% per annum of the daily maximum amount
available to be drawn under each Letter of Credit.</p>
<p>SECTION 2.09 Fees. The Borrower agrees to pay to the Administrative Agent
for the account of each Revolving Credit Lender a commitment fee equal to
{v.commitment_fee}% per annum on the average daily unused amount of the
Revolving Credit Commitment of such Lender, payable quarterly in arrears. The
Borrower agrees to pay a ticking fee on the undrawn Delayed Draw Term Loan
Commitments equal to {v.ticking_fee}% per annum, accruing from and after the
date that is 60 days after the Closing Date.</p>
{amortization_block}
<p>SECTION 2.11 Prepayment of Loans. (a) The Borrower may, upon notice to the
Administrative Agent, voluntarily prepay the Loans in whole or in part without
premium or penalty, subject to Section 2.16. (b) The Borrower shall prepay the
Initial Term Loans with {v.ecf_sweep}% of Excess Cash Flow for each fiscal
year, with step-downs to 25% and 0% based on the Senior Secured First Lien Net
Leverage Ratio.</p>
<p>SECTION 2.12 Interest. (a) Each Loan shall bear interest at the LIBO Rate
plus the Applicable Margin or, at the Borrower's election, the Base Rate plus
the Applicable Margin. (b) The Applicable Margin shall be determined from the
following grid:</p>
{pricing_grid}
{archetype_section(v)}
<p>SECTION 2.14 Incremental Facilities. (a) The Borrower may request one or
more Incremental Term Facilities in an aggregate principal amount not to exceed
{incremental_capacity}{incremental_test_block}. (b) If the All-In Yield
applicable to any Incremental Term Facility that is secured on a pari passu
basis with the Initial Term Loans exceeds the All-In Yield applicable to the
Initial Term Loans by more than {v.mfn_pct}% per annum, then the Applicable
Margin applicable to the Initial Term Loans shall be increased so that the
All-In Yield applicable to the Initial Term Loans equals the All-In Yield
applicable to such Incremental Term Facility minus {v.mfn_pct}% per annum.{mfn_sunset}
(c) The Borrower shall deliver to the Administrative Agent a certificate
demonstrating compliance with clause (a) above.</p>
<p>SECTION 2.16 Call Protection. In the event that, on or prior to the date
that is twelve months after the Closing Date, the Borrower consummates any
Repricing Transaction, the Borrower shall pay to the Administrative Agent, for
the ratable account of each applicable Lender, a fee in an amount equal to
{v.call_protection_pct}% of the aggregate principal amount of the Initial Term
Loans subject to such Repricing Transaction.</p>
<hr>
<p>ARTICLE IV</p>
<p>REPRESENTATIONS AND WARRANTIES</p>
{leverage_rep_block}
<hr>
<p>ARTICLE VI</p>
<p>NEGATIVE COVENANTS</p>
<p>SECTION 6.01 Indebtedness. Holdings will not, and will not permit any
Restricted Subsidiary to, create, incur, assume or permit to exist any
Indebtedness, except: (a) Indebtedness under the Loan Documents; (b) purchase
money Indebtedness and Capital Lease Obligations in an aggregate principal
amount not to exceed {pm_basket}; (c) Indebtedness of non-Loan Party Restricted
Subsidiaries in an aggregate principal amount not to exceed {other_basket}; and
(d) Indebtedness set forth on Schedule 6.01.</p>
{financial_covenant_block}
<hr>
<p>ARTICLE IX</p>
<p>MISCELLANEOUS</p>
<p>SECTION 9.01 Notices. All notices hereunder shall be given to the Borrower
at 200 Harbor Street, Suite 1400, Wilmington, Delaware 19801, Attention: Chief
Financial Officer.</p>
{omission_notice}
<p>SECTION 9.22 Governing Law. THIS AGREEMENT SHALL BE GOVERNED BY, AND
CONSTRUED IN ACCORDANCE WITH, THE LAW OF THE STATE OF NEW YORK.</p>
</body></html>
"""



AMENDMENT_TEMPLATE = """<!DOCTYPE html>
<!-- SYNTHETIC TEST FIXTURE. Fictional parties. -->
<html><head><meta charset="utf-8"><title>Amendment (synthetic fixture)</title></head>
<body>
<p>AMENDMENT NO. {number} TO CREDIT AGREEMENT</p>
<p>This AMENDMENT NO. {number} TO CREDIT AGREEMENT (this "Amendment"), dated as
of {effective}, is entered into among {borrower}, as the Borrower, {holdings},
as Holdings, and {agent}, as Administrative Agent, and amends that certain
Credit Agreement dated as of {closing} (as amended, the "Credit Agreement").
Capitalized terms used herein and not otherwise defined have the meanings given
in the Credit Agreement.</p>
<p>SECTION 1.01 Amendments to the Credit Agreement. Effective as of the
Amendment No. {number} Effective Date, the Credit Agreement is hereby amended
as follows:</p>
{changes}
<p>SECTION 2.01 Conditions to Effectiveness. This Amendment shall become
effective on the date on which the Administrative Agent has received
counterparts hereof executed by the Borrower and the Required Lenders (the
"Amendment No. {number} Effective Date"), which date is {effective}.</p>
<p>SECTION 3.01 Effect of Amendment. Except as expressly amended hereby, the
Credit Agreement remains in full force and effect.</p>
</body></html>
"""


def restate_change(section: str, body: str) -> str:
    """An amendment that replaces a section wholesale."""
    return (
        f"<p>(a) Section {section} of the Credit Agreement is hereby amended "
        f"and restated in its entirety to read as follows:</p>"
        f'<p>"{body}"</p>' 
    )


def replace_change(section: str, old: str, new: str, label: str = "b") -> str:
    """The common case: a short amendment that swaps one figure for another.

    Nothing is restated, so a pipeline that only understands restatement reads
    the old figure straight out of the base agreement and reports it
    confidently.
    """
    return (
        f"<p>({label}) Section {section} of the Credit Agreement is hereby "
        f'amended by deleting the text "{old}" and inserting in lieu thereof '
        f'the text "{new}".</p>'
    )


def build_amendment(
    v: Variant,
    number: int,
    effective: date,
    changes: list[str],
) -> str:
    return AMENDMENT_TEMPLATE.format(
        number=number,
        effective=_fmt(effective),
        closing=_fmt(v.closing),
        borrower=v.borrower,
        holdings=v.holdings,
        agent=v.admin_agent,
        changes="\n".join(changes),
    )


def build_labels(v: Variant = Variant()) -> dict:
    """Ground truth. True by construction, since the document is generated."""
    rows = v.amortization_rows()
    literal_total = sum(a for _, a in rows)
    correct_total = v.quarterly_amort * v.unique_quarters
    fields: dict = {
        "borrower.legal_name": v.borrower,
        "holdings.legal_name": v.holdings,
        "administrative_agent.legal_name": v.admin_agent,
        "collateral_agent.legal_name": v.admin_agent,
        "arranger.legal_name": v.admin_agent,
        "syndication_agent.legal_name": v.syndication_agent,
        "closing_date": v.closing.isoformat(),
        "initial_term_loan.commitment": v.term_principal,
        "initial_term_loan.maturity_date": v.term_maturity.isoformat(),
        "delayed_draw.commitment": v.dd_commitment,
        "revolver.commitment": v.revolver_commitment,
        "revolver.maturity_date": v.revolver_maturity.isoformat(),
        "lc_sublimit": v.lc_sublimit,
        "commitment_fee_pct": float(v.commitment_fee),
        "fronting_fee_pct": float(v.fronting_fee),
        "ticking_fee_pct": float(v.ticking_fee),
        "libor_floor_pct": float(v.libor_floor),
        "excess_cash_flow.sweep_pct": float(v.ecf_sweep),
        "applicable_margin.eurodollar_top_level_pct": max(
            float(row[2].rstrip("%")) for row in v.pricing
        ),
        "amortization.quarterly_amount": v.quarterly_amort,
        "financial_covenant.opening_level": float(
            v.covenant_levels[0][1].split(":")[0]
        ),
        "financial_covenant.final_level": float(
            v.covenant_levels[-1][1].split(":")[0]
        ),
        "opening_total_leverage_ratio": float(v.opening_leverage),
        "mfn_threshold_pct": float(v.mfn_pct),
        "mfn_sunset": (
            f"{v.mfn_sunset_months} months after the Closing Date"
            if v.mfn_sunset_months is not None else None
        ),
        "consolidated_ebitda.addback_cap_pct": float(v.addback_cap),
        "indebtedness.purchase_money_basket_amount": v.pm_basket_amount,
        "indebtedness.purchase_money_basket_ebitda_pct": float(v.pm_basket_pct),
        "incremental.free_and_clear_amount": (
            None if v.omitted_schedules else v.incremental_amount
        ),
        "incremental.leverage_based_test": float(v.incremental_test),
    }
    expected_status: dict[str, str] = {}
    if v.omitted_schedules:
        expected_status["incremental.free_and_clear_amount"] = "external_reference"
    expected_status.update({
        "mfn_sunset": (
            "absent_from_document" if v.mfn_sunset_months is None else "confirmed"
        ),
    })
    if v.sponsor_model_clause:
        expected_status["consolidated_ebitda.addback_cap_clause_a_xvi"] = (
            "external_reference"
        )
    return {
        "document_id": f"fixture_{v.variant_id}",
        "synthetic": True,
        "note": "Synthetic surrogate for the Paya/GTCR-Ultra agreement.",
        "variant": {
            "decoy": v.decoy,
            "alt_phrasing": v.alt_phrasing,
            "duplicated_year": v.duplicated_year,
            "mfn_sunset_months": v.mfn_sunset_months,
        },
        "fields": {
            **fields,
            "amortization.unique_payment_count": v.unique_quarters,
            "amortization.correct_total": correct_total,
            "amortization.literal_table_total": literal_total,
            "call_protection.soft_call_pct": float(v.call_protection_pct),
            "call_protection.months": 12,
        },
        "expected_status": expected_status,
        "traps": {
            "trap_1_duplicated_amortization_rows": {
                "printed_rows": len(rows),
                "true_quarters": v.unique_quarters,
                "literal_total": literal_total,
                "correct_total": correct_total,
                "duplicated_year": v.duplicated_year,
            },
            "trap_2_hardcoded_opening_ebitda": {
                "quarters": dict(v.opening_ebitda),
                "ltm_total": v.ltm_ebitda,
            },
            "trap_3_uncapped_external_addback": {
                "clause": "(a)(xvi)",
                "external_document": "Sponsor Model",
                "visible_cap_pct": float(v.addback_cap),
                "visible_cap_applies_to": "(a)(xiii)-(a)(xv)",
            },
            "trap_4_absent_mfn_sunset": {
                "mfn_threshold_pct": float(v.mfn_pct),
                "sunset": v.mfn_sunset_months,
            },
        },
    }


# ---------------------------------------------------------------------------
# Gold corpus
# ---------------------------------------------------------------------------

_NAMES = [
    ("ALDERWOOD", "BRIGHTFIELD"), ("CASTLETON", "DUNMORE"),
    ("ELSTREE", "FAIRHAVEN"), ("GRANTLEY", "HOLLOWAY"),
    ("IRONGATE", "JASPER RIDGE"), ("KESWICK", "LANGMERE"),
    ("MARLOWE", "NORTHWOOD"), ("OAKHURST", "PENDRAGON"),
    ("QUARRYMAN", "ROSEDALE"), ("STONEBRIDGE", "THORNBURY"),
    ("UPLANDS", "VALEMONT"), ("WESTMARCH", "YARROW"),
]


def gold_corpus(n: int = 24, seed: int = 20260920) -> list[Variant]:
    """A deterministic set of variants for threshold fitting.

    These are synthetic. The spec calls for >= 20 hand-labelled *real*
    agreements, which this environment cannot source -- sec.gov is blocked. The
    harness reads a labels JSON alongside any document, so a real gold set
    drops straight in; what is fitted here demonstrates the machinery and will
    need refitting against real documents before the numbers mean anything
    about production.
    """
    rng = random.Random(seed)
    variants: list[Variant] = [Variant()]
    for index in range(1, n):
        borrower, agent = _NAMES[index % len(_NAMES)]
        principal = rng.randrange(60, 480) * 500_000
        closing_year = rng.choice([2016, 2017, 2018, 2019])
        closing = date(closing_year, rng.choice([2, 5, 8, 11]), 1)
        tenor = rng.choice([6, 7])
        first_payment = date(
            closing.year + (1 if closing.month >= 8 else 0),
            {2: 6, 5: 9, 8: 12, 11: 3}[closing.month],
            {6: 30, 9: 30, 12: 31, 3: 31}[{2: 6, 5: 9, 8: 12, 11: 3}[closing.month]],
        )
        term_maturity = date(closing.year + tenor, closing.month, 1)
        # Derived, not drawn: the last scheduled payment has to land in a
        # quarter strictly before maturity, or the document contradicts itself
        # before the pipeline ever reads it.
        quarters = max(
            4,
            (term_maturity.year * 4 + (term_maturity.month - 1) // 3)
            - (first_payment.year * 4 + (first_payment.month - 1) // 3),
        )
        base = rng.randrange(7_000_000, 15_000_000)
        opening = tuple(
            (label, base + rng.randrange(-900_000, 900_000))
            for label in ("Q-4", "Q-3", "Q-2", "Q-1")
        )
        opening = tuple(
            (
                _fmt(date(closing.year - (1 if i < 2 else 0), m, d)),
                amount,
            )
            for i, ((_, amount), (m, d)) in enumerate(
                zip(opening, [(9, 30), (12, 31), (3, 31), (6, 30)])
            )
        )
        top = rng.choice([4.50, 4.75, 5.00, 5.25, 5.50])
        pricing = tuple(
            (level, band, f"{top - step:.2f}%", f"{top - step - 1:.2f}%")
            for (level, band, step) in (
                ("I", "Greater than 5.50:1.00", 0.0),
                ("II", "Less than or equal to 5.50:1.00 but greater than "
                       "4.50:1.00", 0.25),
                ("III", "Less than or equal to 4.50:1.00", 0.50),
            )
        )
        opening_cov = rng.choice([6.00, 6.25, 6.50, 6.75])
        covenant = tuple(
            (label, f"{max(opening_cov - 0.25 * i, 4.50):.2f}:1.00")
            for i, (label, _) in enumerate(DEFAULT_COVENANT_LEVELS)
        )
        variants.append(Variant(
            variant_id=(
                f"{index:02d}_{borrower.lower().replace(' ', '_')}_{closing_year}"
            ),
            holdings=f"{borrower} HOLDINGS, LLC",
            borrower=f"{borrower} INTERMEDIATE, LLC",
            admin_agent=f"{agent} CAPITAL LP",
            syndication_agent=f"{agent} CREDIT PARTNERS LLC",
            closing=closing,
            first_payment=first_payment,
            term_maturity=term_maturity,
            revolver_maturity=date(closing.year + tenor - 2, closing.month, 1),
            term_principal=principal,
            unique_quarters=quarters,
            duplicated_year=(
                first_payment.year + 2 if index % 3 == 0 else None
            ),
            revolver_commitment=rng.randrange(10, 60) * 1_000_000,
            dd_commitment=rng.randrange(10, 60) * 1_000_000,
            lc_sublimit=rng.randrange(2, 12) * 1_000_000,
            libor_floor=f"{rng.choice([0.00, 0.75, 1.00]):.2f}",
            commitment_fee=f"{rng.choice([0.25, 0.375, 0.50]):.3f}".rstrip("0"),
            fronting_fee=f"{rng.choice([0.125, 0.250]):.3f}",
            ticking_fee=f"{rng.choice([0.50, 1.00, 2.00]):.2f}",
            ecf_sweep=f"{rng.choice([50, 75])}",
            opening_ebitda=opening,
            addback_cap=f"{rng.choice([20, 25, 30, 35])}",
            sponsor_model_clause=index % 4 != 0,
            mfn_pct=f"{rng.choice([0.50, 0.75, 1.00]):.2f}",
            mfn_sunset_months=rng.choice([None, None, 6, 12, 18]),
            covenant_levels=covenant,
            pricing=pricing,
            pm_basket_amount=rng.randrange(5, 40) * 1_000_000,
            pm_basket_pct=f"{rng.choice([25, 30, 35, 40])}",
            incremental_amount=rng.randrange(10, 90) * 1_000_000,
            incremental_test=f"{rng.choice([4.25, 4.50, 4.75, 5.00]):.2f}",
            omitted_schedules=index % 6 == 0,
            decoy=index % 5 == 0,
            alt_phrasing=index % 7 == 0,
        ))
    return variants


def write_corpus(out_dir: Path, n: int = 24) -> list[tuple[Path, Path]]:
    """Write the gold corpus to disk; returns (html, labels) pairs.

    Clears the directory first. Variant ids move whenever the generator's
    random draw sequence changes, so leftovers from an earlier generation
    would sit alongside the current corpus and silently inflate every
    evaluation -- which is precisely how a calibration run comes to be fitted
    on a corpus nobody meant to use.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*out_dir.glob("*.html"), *out_dir.glob("*.labels.json")]:
        stale.unlink()
    written: list[tuple[Path, Path]] = []
    for variant in gold_corpus(n):
        html_path = out_dir / f"{variant.variant_id}.html"
        labels_path = out_dir / f"{variant.variant_id}.labels.json"
        html_path.write_text(build_html(variant), encoding="utf-8")
        labels_path.write_text(
            json.dumps(build_labels(variant), indent=2), encoding="utf-8"
        )
        written.append((html_path, labels_path))
    return written


def main() -> None:
    html_path = HERE / "fixture_meridian_2017.html"
    labels_path = HERE / "fixture_meridian_2017.labels.json"
    html_path.write_text(build_html(), encoding="utf-8")
    labels_path.write_text(json.dumps(build_labels(), indent=2), encoding="utf-8")
    trap = build_labels()["traps"]["trap_1_duplicated_amortization_rows"]
    print(f"wrote {html_path} ({html_path.stat().st_size:,} bytes)")
    print(f"wrote {labels_path}")
    print(
        f"  trap 1: {trap['printed_rows']} printed rows vs "
        f"{trap['true_quarters']} quarters; "
        f"${trap['literal_total']:,} literal vs ${trap['correct_total']:,} correct"
    )


if __name__ == "__main__":
    main()
