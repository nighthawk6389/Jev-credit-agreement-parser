"""Build the primary integration fixture.

The spec names the Paya / GTCR-Ultra credit agreement on SEC EDGAR as the
primary test document. This build environment has no egress to sec.gov, and in
any case a checked-in fixture must be reproducible, so this script emits a
*synthetic surrogate*: fictional parties, real trap structure, and the exact
arithmetic the spec calls out.

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
"""

from __future__ import annotations

import json
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


def amortization_dates() -> list[date]:
    """27 quarter-end dates: 31 December 2017 through 30 June 2024."""
    dates: list[date] = []
    year, month = 2017, 12
    while len(dates) < UNIQUE_QUARTERS:
        last_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
        dates.append(date(year, month, last_day))
        month += 3
        if month > 12:
            month -= 12
            year += 1
    return dates


def amortization_rows() -> list[tuple[date, int]]:
    """The table as the document prints it -- 2023 duplicated (Trap 1)."""
    rows = [(d, QUARTERLY_AMORT) for d in amortization_dates()]
    duplicates = [r for r in rows if r[0].year == DUPLICATED_YEAR]
    last_2023 = max(i for i, r in enumerate(rows) if r[0].year == DUPLICATED_YEAR)
    return rows[: last_2023 + 1] + duplicates + rows[last_2023 + 1 :]


def _fmt(d: date) -> str:
    return d.strftime("%B %-d, %Y") if hasattr(d, "strftime") else str(d)


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

DEFINITIONS: list[tuple[str, str]] = [
    ("Administrative Agent",
     'means Northgate Capital LP, in its capacity as administrative agent for '
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
    ("Closing Date", 'means August 1, 2017.'),
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
     'exceed $20,000,000.'),
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
     '$25,000,000.'),
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
     'Initial Term Loan Commitments on the Closing Date is $150,500,000.'),
    ("Initial Term Loan Maturity Date", 'means August 1, 2024.'),
    ("LIBO Rate",
     'means, for any Interest Period, the rate per annum appearing on the '
     'applicable Reuters screen page as the London interbank offered rate for '
     'deposits in Dollars for a period equal to such Interest Period; provided '
     'that the LIBO Rate shall not at any time be less than 1.00% per annum.'),
    ("Restricted Subsidiary",
     'means any Subsidiary of Holdings other than an Unrestricted Subsidiary.'),
    ("Revolving Credit Commitment",
     'means, with respect to each Lender, the commitment of such Lender to make '
     'Revolving Credit Loans and to acquire participations in Letters of '
     'Credit. The aggregate Revolving Credit Commitments on the Closing Date '
     'are $20,000,000.'),
    ("Revolving Credit Maturity Date", 'means August 1, 2022.'),
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


def consolidated_ebitda_definition() -> str:
    clauses = " ".join(
        f"({num}) {body};" for num, body in EBITDA_CLAUSES[:-1]
    )
    last = EBITDA_CLAUSES[-1]
    override_rows = [
        [quarter, _money(amount)]
        for quarter, amount in OPENING_EBITDA_QUARTERS.items()
    ]
    return (
        '<p><b>"Consolidated EBITDA"</b> means, for any period, Consolidated Net '
        'Income for such period, <i>plus</i> (a) without duplication and to the '
        'extent deducted in determining Consolidated Net Income for such period, '
        'the sum of: ' + clauses +
        f' and ({last[0]}) {last[1]}; <i>minus</i> (b) without duplication and '
        'to the extent included in Consolidated Net Income, any non-cash gains '
        'for such period; <i>provided</i> that the aggregate amount added back '
        'pursuant to clauses (a)(xiii) through (a)(xv) above shall not exceed '
        '25% of Consolidated EBITDA for such period (calculated prior to giving '
        'effect to any add-back pursuant to such clauses).</p>'
        '<p>Notwithstanding anything to the contrary in this definition, '
        'Consolidated EBITDA for each of the fiscal quarters set forth below '
        'shall be deemed to be the amount set forth opposite such fiscal '
        'quarter, and the foregoing provisions of this definition shall not '
        'apply to any such fiscal quarter:</p>'
        + _table(["Fiscal Quarter Ended", "Consolidated EBITDA"], override_rows)
    )


def amortization_section() -> str:
    rows = [[_fmt(d), _money(amount)] for d, amount in amortization_rows()]
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


PRICING_GRID = _table(
    ["Level", "Senior Secured First Lien Net Leverage Ratio",
     "Eurodollar Rate", "Base Rate"],
    [
        ["I", "Greater than 5.50:1.00", "5.00%", "4.00%"],
        ["II", "Less than or equal to 5.50:1.00 but greater than 4.50:1.00",
         "4.75%", "3.75%"],
        ["III", "Less than or equal to 4.50:1.00", "4.50%", "3.50%"],
    ],
)

COVENANT_GRID = _table(
    ["Test Period Ending", "Maximum Total Leverage Ratio"],
    [
        ["December 31, 2017 through December 31, 2018", "6.50:1.00"],
        ["March 31, 2019 through December 31, 2019", "6.25:1.00"],
        ["March 31, 2020 through December 31, 2020", "6.00:1.00"],
        ["March 31, 2021 through December 31, 2021", "5.75:1.00"],
        ["March 31, 2022 and thereafter", "5.50:1.00"],
    ],
)

COMMITMENT_TABLE = _table(
    ["Facility", "Aggregate Commitment", "Maturity"],
    [
        ["Initial Term Loan Facility", _money(TERM_PRINCIPAL), "August 1, 2024"],
        ["Delayed Draw Term Loan Facility", _money(25_000_000), "August 1, 2024"],
        ["Revolving Credit Facility", _money(20_000_000), "August 1, 2022"],
    ],
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


def build_html() -> str:
    definitions = "".join(
        f'<p><b>"{term}"</b> {body}</p>' for term, body in DEFINITIONS
    )
    toc = "".join(
        f"<p>{num} {title}</p>" for num, title in TOC_ENTRIES
    )
    return f"""<!DOCTYPE html>
<!-- SYNTHETIC TEST FIXTURE. Fictional parties. Not a real credit agreement and
     not a filing by any real issuer. Generated by build_fixture.py to carry the
     four documented traps with the arithmetic given in the project spec. -->
<html><head><meta charset="utf-8"><title>Credit Agreement (synthetic fixture)</title></head>
<body>
<p>EXHIBIT 10.1</p>
<p>CREDIT AGREEMENT</p>
<p>dated as of August 1, 2017</p>
<p>among</p>
<p>MERIDIAN PAYMENTS HOLDINGS, LLC, as Holdings,</p>
<p>MERIDIAN PAYMENTS INTERMEDIATE, LLC, as the Borrower,</p>
<p>THE LENDERS PARTY HERETO FROM TIME TO TIME,</p>
<p>and</p>
<p>NORTHGATE CAPITAL LP, as Administrative Agent and Collateral Agent</p>
<p>NORTHGATE CAPITAL LP, as Lead Arranger and Sole Bookrunner</p>
<p>CEDAR POINT CREDIT PARTNERS LLC, as Syndication Agent</p>
<hr>
<p>TABLE OF CONTENTS</p>
{toc}
<hr>
<p>ARTICLE I</p>
<p>DEFINITIONS</p>
<p>SECTION 1.01 Defined Terms. As used in this Agreement, the following terms
have the meanings specified below:</p>
{definitions}
{consolidated_ebitda_definition()}
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
{COMMITMENT_TABLE}
<p>SECTION 2.05 Letters of Credit. The aggregate face amount of all Letters of
Credit outstanding at any time shall not exceed $5,000,000 (the "Letter of
Credit Sublimit"). The Borrower shall pay to the Issuing Bank a fronting fee
equal to 0.125% per annum of the daily maximum amount available to be drawn
under each Letter of Credit.</p>
<p>SECTION 2.09 Fees. The Borrower agrees to pay to the Administrative Agent
for the account of each Revolving Credit Lender a commitment fee equal to
0.50% per annum on the average daily unused amount of the Revolving Credit
Commitment of such Lender, payable quarterly in arrears. The Borrower agrees to
pay a ticking fee on the undrawn Delayed Draw Term Loan Commitments equal to
1.00% per annum, accruing from and after the date that is 60 days after the
Closing Date.</p>
{amortization_section()}
<p>SECTION 2.11 Prepayment of Loans. (a) The Borrower may, upon notice to the
Administrative Agent, voluntarily prepay the Loans in whole or in part without
premium or penalty, subject to Section 2.16. (b) The Borrower shall prepay the
Initial Term Loans with 50% of Excess Cash Flow for each fiscal year, with
step-downs to 25% and 0% based on the Senior Secured First Lien Net Leverage
Ratio.</p>
<p>SECTION 2.12 Interest. (a) Each Loan shall bear interest at the LIBO Rate
plus the Applicable Margin or, at the Borrower's election, the Base Rate plus
the Applicable Margin. (b) The Applicable Margin shall be determined from the
following grid:</p>
{PRICING_GRID}
<p>SECTION 2.14 Incremental Facilities. (a) The Borrower may request one or
more Incremental Term Facilities in an aggregate principal amount not to exceed
the greater of $30,000,000 and 100% of Consolidated EBITDA for the most
recently ended Test Period, plus unlimited additional amounts so long as the
Senior Secured First Lien Net Leverage Ratio would not exceed 4.75:1.00 on a
Pro Forma Basis. (b) If the All-In Yield applicable to any Incremental Term
Facility that is secured on a pari passu basis with the Initial Term Loans
exceeds the All-In Yield applicable to the Initial Term Loans by more than
0.50% per annum, then the Applicable Margin applicable to the Initial Term
Loans shall be increased so that the All-In Yield applicable to the Initial
Term Loans equals the All-In Yield applicable to such Incremental Term Facility
minus 0.50% per annum. (c) The Borrower shall deliver to the Administrative
Agent a certificate demonstrating compliance with clause (a) above.</p>
<p>SECTION 2.16 Call Protection. In the event that, on or prior to the date
that is twelve months after the Closing Date, the Borrower consummates any
Repricing Transaction, the Borrower shall pay to the Administrative Agent, for
the ratable account of each applicable Lender, a fee in an amount equal to 1.00%
of the aggregate principal amount of the Initial Term Loans subject to such
Repricing Transaction.</p>
<hr>
<p>ARTICLE IV</p>
<p>REPRESENTATIONS AND WARRANTIES</p>
<p>SECTION 4.14 Solvency; Closing Date Leverage. As of the Closing Date, after
giving effect to the Transactions and calculated on a Pro Forma Basis, the
Total Leverage Ratio is 3.72:1.00.</p>
<hr>
<p>ARTICLE VI</p>
<p>NEGATIVE COVENANTS</p>
<p>SECTION 6.01 Indebtedness. Holdings will not, and will not permit any
Restricted Subsidiary to, create, incur, assume or permit to exist any
Indebtedness, except: (a) Indebtedness under the Loan Documents; (b) purchase
money Indebtedness and Capital Lease Obligations in an aggregate principal
amount not to exceed the greater of $15,000,000 and 35% of Consolidated EBITDA
for the most recently ended Test Period, calculated on a Pro Forma Basis after
giving effect to the add-backs described in clause (a) of the definition
thereof; (c) Indebtedness of non-Loan Party Restricted Subsidiaries in an
aggregate principal amount not to exceed the greater of $10,000,000 and 20% of
Consolidated EBITDA; and (d) Indebtedness set forth on Schedule 6.01.</p>
<p>SECTION 6.12 Financial Covenant. Holdings will not permit the Total Leverage
Ratio as of the last day of any Test Period to exceed the ratio set forth below
opposite such Test Period:</p>
{COVENANT_GRID}
<hr>
<p>ARTICLE IX</p>
<p>MISCELLANEOUS</p>
<p>SECTION 9.01 Notices. All notices hereunder shall be given to the Borrower
at 200 Harbor Street, Suite 1400, Wilmington, Delaware 19801, Attention: Chief
Financial Officer.</p>
<p>SECTION 9.22 Governing Law. THIS AGREEMENT SHALL BE GOVERNED BY, AND
CONSTRUED IN ACCORDANCE WITH, THE LAW OF THE STATE OF NEW YORK.</p>
</body></html>
"""


def build_labels() -> dict:
    rows = amortization_rows()
    literal_total = sum(a for _, a in rows)
    correct_total = QUARTERLY_AMORT * UNIQUE_QUARTERS
    return {
        "document_id": "fixture_meridian_2017",
        "synthetic": True,
        "note": "Synthetic surrogate for the Paya/GTCR-Ultra agreement.",
        "fields": {
            "borrower.legal_name": "MERIDIAN PAYMENTS INTERMEDIATE, LLC",
            "holdings.legal_name": "MERIDIAN PAYMENTS HOLDINGS, LLC",
            "administrative_agent.legal_name": "NORTHGATE CAPITAL LP",
            "syndication_agent.legal_name": "CEDAR POINT CREDIT PARTNERS LLC",
            "closing_date": "2017-08-01",
            "initial_term_loan.commitment": TERM_PRINCIPAL,
            "initial_term_loan.maturity_date": "2024-08-01",
            "delayed_draw.commitment": 25_000_000,
            "revolver.commitment": 20_000_000,
            "revolver.maturity_date": "2022-08-01",
            "lc_sublimit": 5_000_000,
            "commitment_fee_pct": 0.50,
            "fronting_fee_pct": 0.125,
            "libor_floor_pct": 1.00,
            "applicable_margin.eurodollar_top_level_pct": 5.00,
            "amortization.quarterly_amount": QUARTERLY_AMORT,
            "amortization.unique_payment_count": UNIQUE_QUARTERS,
            "amortization.correct_total": correct_total,
            "amortization.literal_table_total": literal_total,
            "financial_covenant.opening_level": 6.50,
            "financial_covenant.final_level": 5.50,
            "opening_total_leverage_ratio": 3.72,
            "mfn_threshold_pct": 0.50,
            "mfn_sunset": None,
            "call_protection.soft_call_pct": 1.00,
            "call_protection.months": 12,
        },
        "expected_status": {
            "mfn_sunset": "absent_from_document",
            "consolidated_ebitda.addback_cap_clause_a_xvi": "external_reference",
        },
        "traps": {
            "trap_1_duplicated_amortization_rows": {
                "printed_rows": len(rows),
                "true_quarters": UNIQUE_QUARTERS,
                "literal_total": literal_total,
                "correct_total": correct_total,
                "duplicated_year": DUPLICATED_YEAR,
            },
            "trap_2_hardcoded_opening_ebitda": {
                "quarters": OPENING_EBITDA_QUARTERS,
                "ltm_total": sum(OPENING_EBITDA_QUARTERS.values()),
            },
            "trap_3_uncapped_external_addback": {
                "clause": "(a)(xvi)",
                "external_document": "Sponsor Model",
                "visible_cap_pct": 25.0,
                "visible_cap_applies_to": "(a)(xiii)-(a)(xv)",
            },
            "trap_4_absent_mfn_sunset": {
                "mfn_threshold_pct": 0.50,
                "sunset": None,
            },
        },
    }


def main() -> None:
    html_path = HERE / "fixture_meridian_2017.html"
    labels_path = HERE / "fixture_meridian_2017.labels.json"
    html_path.write_text(build_html(), encoding="utf-8")
    labels_path.write_text(json.dumps(build_labels(), indent=2), encoding="utf-8")
    labels = build_labels()["traps"]["trap_1_duplicated_amortization_rows"]
    print(f"wrote {html_path} ({html_path.stat().st_size:,} bytes)")
    print(f"wrote {labels_path}")
    print(
        f"  trap 1: {labels['printed_rows']} printed rows vs "
        f"{labels['true_quarters']} quarters; "
        f"${labels['literal_total']:,} literal vs ${labels['correct_total']:,} correct"
    )


if __name__ == "__main__":
    main()
