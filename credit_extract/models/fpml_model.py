"""FpML loan-product structure and the field registry.

FpML 5.x carries syndicated-loan product definitions built with LSTA/LMA input:
facility types, tranches, accrual options, commitment schedules, LC
sub-facilities and fee types. The intended build path is ``xsdata`` over the
published XSDs (see ``scripts/gen_fpml.py``).

    Provenance caveat: fpml.org is not reachable from this build environment,
    so the element names below are hand-mapped from the FpML 5.x loan product
    schemas and carry ``verified=False``. Every other standard binding in this
    project resolves against a vendored source of truth and carries
    ``verified=True``. Run ``python scripts/gen_fpml.py --verify`` from a
    network that can reach fpml.org to promote these.

The registry at the bottom is the pipeline's spine: it names every extraction
target, the standard term each maps to, the field class its confidence
threshold is fitted on, and the affirmative question the negative-space
validator asks when the extractor comes back empty.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from .fibo_map import TermBinding, binding as fibo_binding
from .fibo_map import gap as fibo_gap

FPML_VERSION = "5.13"
FPML_NAMESPACE = "http://www.fpml.org/FpML-5/confirmation"
FPML_SOURCE = "https://www.fpml.org/spec/ (loan product schemas)"


def fpml(term: str, verified: bool = False) -> TermBinding:
    """Bind to an FpML element name. Unverified until the XSD is reachable."""
    return TermBinding(
        standard="fpml",
        term=f"fpml:{term}",
        uri=f"{FPML_NAMESPACE}#{term}",
        label=term,
        verified=verified,
        gap_reason=None if verified else "hand-mapped; fpml.org unreachable at build time",
    )


# ---------------------------------------------------------------------------
# Product structure
# ---------------------------------------------------------------------------

FacilityType = Literal[
    "term_loan", "delayed_draw_term_loan", "revolver", "letter_of_credit"
]


class AccrualTerms(BaseModel):
    """fpml:accrualOptions -- how interest is computed on a tranche."""

    base_rate: str | None = None              # fpml:floatingRateIndex
    spread_pct: Decimal | None = None         # fpml:spread
    floor_pct: Decimal | None = None          # fpml:rateFloor
    day_count: str | None = None              # fpml:dayCountFraction
    period_months: int | None = None          # fpml:calculationPeriodFrequency
    alternate_base_rate_spread_pct: Decimal | None = None


class ScheduleRow(BaseModel):
    """One row of a commitment-reduction or amortization schedule."""

    payment_date: date
    amount: Decimal
    row_index: int
    source_row: int | None = None             # index in the printed table
    span_start: int | None = None
    span_end: int | None = None


class AmortizationSchedule(BaseModel):
    """fpml:facilityCommitmentSchedule projected onto principal repayment.

    The documented bullet and the derived bullet are kept apart on purpose.
    ``stated_bullet_at_maturity`` is a fact printed in the agreement and is the
    only thing worth checking the arithmetic against; ``derived_bullet`` is
    computed from the rows and therefore moves whenever the rows do. Storing a
    derived value as if it were documented makes the reconciliation identity
    self-satisfying, which would have quietly hidden Trap 1.
    """

    rows: list[ScheduleRow] = Field(default_factory=list)
    stated_bullet_at_maturity: Decimal | None = None
    original_principal: Decimal | None = None
    table_id: str | None = None

    @property
    def total_scheduled(self) -> Decimal:
        return sum((r.amount for r in self.rows), Decimal(0))

    @property
    def derived_bullet(self) -> Decimal | None:
        """Balance left at maturity given these rows. Never stored."""
        if self.original_principal is None:
            return None
        return self.original_principal - self.total_scheduled

    def as_pairs(self) -> list[tuple[date, Decimal]]:
        return [(r.payment_date, r.amount) for r in self.rows]

    def deduplicated(self) -> "AmortizationSchedule":
        """Drop repeated payment dates, keeping first occurrence order."""
        seen: set[date] = set()
        kept: list[ScheduleRow] = []
        for row in self.rows:
            if row.payment_date in seen:
                continue
            seen.add(row.payment_date)
            kept.append(row)
        return AmortizationSchedule(
            rows=[r.model_copy(update={"row_index": i}) for i, r in enumerate(kept)],
            stated_bullet_at_maturity=self.stated_bullet_at_maturity,
            original_principal=self.original_principal,
            table_id=self.table_id,
        )


class FeeTerms(BaseModel):
    commitment_fee_pct: Decimal | None = None     # fpml:commitmentFee
    ticking_fee_pct: Decimal | None = None
    lc_participation_fee_pct: Decimal | None = None
    fronting_fee_pct: Decimal | None = None       # fpml:lcIssuanceFee


class Facility(BaseModel):
    """fpml:facility -- one tranche of the credit."""

    facility_id: str                              # fpml:facilityIdentifier
    facility_type: FacilityType                   # fpml:facilityType
    commitment_amount: Decimal | None = None      # fpml:totalCommitmentAmount
    currency: str = "USD"                         # fpml:currency
    effective_date: date | None = None            # fpml:effectiveDate
    maturity_date: date | None = None             # fpml:maturityDate
    accrual: AccrualTerms = Field(default_factory=AccrualTerms)
    fees: FeeTerms = Field(default_factory=FeeTerms)
    amortization: AmortizationSchedule | None = None
    sublimit_of: str | None = None                # fpml:lcSubFacility parent
    actus_mapping: dict[str, Any] | None = None   # ActusMapping, or None + reason


def facility_bindings() -> dict[str, TermBinding]:
    return {
        "facility_id": fpml("facilityIdentifier"),
        "facility_type": fpml("facilityType"),
        "commitment_amount": fpml("totalCommitmentAmount"),
        "currency": fpml("currency"),
        "maturity_date": fpml("maturityDate"),
        "effective_date": fpml("effectiveDate"),
        "accrual": fpml("accrualOptions"),
        "base_rate": fpml("floatingRateIndex"),
        "spread": fpml("spread"),
        "day_count": fpml("dayCountFraction"),
        "commitment_schedule": fpml("facilityCommitmentSchedule"),
        "commitment_fee": fpml("commitmentFee"),
        "lc_issuance_fee": fpml("lcIssuanceFee"),
        "lc_sub_facility": fpml("lcSubFacility"),
    }


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------

ValueKind = Literal["money", "percent", "ratio", "date", "text", "int", "bool"]


class FieldSpec(BaseModel):
    """One extraction target, and everything the pipeline needs to know about it."""

    name: str
    description: str
    kind: ValueKind
    field_class: str
    criticality: int = Field(ge=1, le=5)
    #: Standard term this field reports under, as a CURIE-ish string.
    standard_term: str | None = None
    standard: str | None = None
    verified_term: bool = True
    #: Defined terms whose closure must be in context when extracting this.
    definition_anchors: list[str] = Field(default_factory=list)
    #: Sections the field usually lives in. A hint only -- never a filter, or
    #: the orphan sweep would have nothing left to find.
    section_hints: list[str] = Field(default_factory=list)
    #: Affirmative statement for validator C when the extractor returns null.
    negative_question: str = ""

    @property
    def absence_statement(self) -> str:
        return self.negative_question or (
            f"This agreement contains no provision addressing {self.description}."
        )


def _spec(
    name: str,
    description: str,
    kind: ValueKind,
    field_class: str,
    criticality: int,
    standard_term: str | None = None,
    standard: str | None = None,
    verified_term: bool = True,
    anchors: list[str] | None = None,
    sections: list[str] | None = None,
    negative: str = "",
) -> FieldSpec:
    return FieldSpec(
        name=name,
        description=description,
        kind=kind,
        field_class=field_class,
        criticality=criticality,
        standard_term=standard_term,
        standard=standard,
        verified_term=verified_term,
        definition_anchors=anchors or [],
        section_hints=sections or [],
        negative_question=negative,
    )


FIELD_REGISTRY: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in [
        # -- parties ---------------------------------------------------------
        _spec("borrower.legal_name", "the identity of the Borrower", "text",
              "parties", 4, "fibo-fbc-dae-dbt:Borrower", "fibo"),
        _spec("holdings.legal_name", "the identity of Holdings", "text",
              "parties", 3, "fibo-fnd-agr-ctr:ContractParty", "fibo"),
        _spec("administrative_agent.legal_name", "the Administrative Agent",
              "text", "parties", 4, None, "fibo", verified_term=False),
        _spec("collateral_agent.legal_name", "the Collateral Agent", "text",
              "parties", 2, None, "fibo", verified_term=False),
        _spec("arranger.legal_name", "the Lead Arranger", "text", "parties", 2,
              None, "fibo", verified_term=False),
        _spec("syndication_agent.legal_name", "the Syndication Agent", "text",
              "parties", 2, None, "fibo", verified_term=False),
        # -- dates -----------------------------------------------------------
        _spec("closing_date", "the Closing Date", "date", "dates", 5,
              "fpml:effectiveDate", "fpml", verified_term=False,
              anchors=["Closing Date"]),
        _spec("initial_term_loan.maturity_date",
              "the maturity date of the Initial Term Loans", "date", "dates", 5,
              "fpml:maturityDate", "fpml", verified_term=False,
              anchors=["Initial Term Loan Maturity Date"]),
        _spec("revolver.maturity_date",
              "the maturity date of the Revolving Credit Facility", "date",
              "dates", 5, "fpml:maturityDate", "fpml", verified_term=False,
              anchors=["Revolving Credit Maturity Date"]),
        # -- economic terms --------------------------------------------------
        _spec("initial_term_loan.commitment",
              "the aggregate principal amount of the Initial Term Loans",
              "money", "economic_terms", 5, "fpml:totalCommitmentAmount", "fpml",
              verified_term=False, anchors=["Initial Term Loan Commitment"],
              sections=["2.01"]),
        _spec("delayed_draw.commitment",
              "the aggregate Delayed Draw Term Loan Commitments", "money",
              "economic_terms", 4, "fpml:totalCommitmentAmount", "fpml",
              verified_term=False, anchors=["Delayed Draw Term Loan Commitment"]),
        _spec("revolver.commitment",
              "the aggregate Revolving Credit Commitments", "money",
              "economic_terms", 5, "fpml:totalCommitmentAmount", "fpml",
              verified_term=False, anchors=["Revolving Credit Commitment"]),
        _spec("lc_sublimit", "the Letter of Credit Sublimit", "money",
              "economic_terms", 3, "fpml:lcSubFacility", "fpml",
              verified_term=False, sections=["2.05"]),
        _spec("libor_floor_pct", "the LIBO Rate floor", "percent",
              "economic_terms", 5, "fpml:rateFloor", "fpml", verified_term=False,
              anchors=["LIBO Rate"]),
        _spec("applicable_margin.eurodollar_top_level_pct",
              "the highest Eurodollar Applicable Margin in the pricing grid",
              "percent", "economic_terms", 5, "fpml:spread", "fpml",
              verified_term=False, anchors=["Applicable Margin"],
              sections=["2.12"]),
        _spec("commitment_fee_pct", "the unused commitment fee", "percent",
              "economic_terms", 4, "fpml:commitmentFee", "fpml",
              verified_term=False, sections=["2.09"]),
        _spec("fronting_fee_pct", "the letter of credit fronting fee", "percent",
              "economic_terms", 2, "fpml:lcIssuanceFee", "fpml",
              verified_term=False, sections=["2.05"]),
        _spec("ticking_fee_pct",
              "the ticking fee on undrawn delayed draw commitments", "percent",
              "economic_terms", 3, "fpml:commitmentFee", "fpml",
              verified_term=False, sections=["2.09"]),
        _spec("amortization.quarterly_amount",
              "the quarterly principal amortization payment", "money",
              "economic_terms", 5, "actus:arrayNextPrincipalRedemptionPayment",
              "actus", sections=["2.10"]),
        _spec("excess_cash_flow.sweep_pct",
              "the Excess Cash Flow mandatory prepayment percentage", "percent",
              "economic_terms", 4, None, None, verified_term=False,
              sections=["2.11"]),
        # -- covenant levels -------------------------------------------------
        _spec("financial_covenant.opening_level",
              "the opening maximum leverage level under the financial covenant",
              "ratio", "covenant_levels", 5, None, None, verified_term=False,
              anchors=["Total Leverage Ratio"], sections=["6.12"]),
        _spec("financial_covenant.final_level",
              "the final stepped-down maximum leverage level", "ratio",
              "covenant_levels", 5, None, None, verified_term=False,
              anchors=["Total Leverage Ratio"], sections=["6.12"]),
        _spec("opening_total_leverage_ratio",
              "the Total Leverage Ratio as of the Closing Date", "ratio",
              "covenant_levels", 5, None, None, verified_term=False,
              anchors=["Total Leverage Ratio"], sections=["4.14"]),
        # -- baskets ---------------------------------------------------------
        _spec("indebtedness.purchase_money_basket_amount",
              "the fixed dollar component of the purchase money debt basket",
              "money", "baskets", 3, None, None, verified_term=False,
              sections=["6.01"]),
        _spec("indebtedness.purchase_money_basket_ebitda_pct",
              "the EBITDA-grower component of the purchase money debt basket",
              "percent", "baskets", 3, None, None, verified_term=False,
              anchors=["Consolidated EBITDA"], sections=["6.01"]),
        _spec("incremental.free_and_clear_amount",
              "the fixed dollar incremental facility capacity", "money",
              "baskets", 4, None, None, verified_term=False, sections=["2.14"]),
        _spec("incremental.leverage_based_test",
              "the pro forma leverage test governing unlimited incremental "
              "capacity", "ratio", "baskets", 4, None, None, verified_term=False,
              anchors=["Senior Secured First Lien Net Leverage Ratio"],
              sections=["2.14"]),
        # -- MFN and EBITDA construction -------------------------------------
        _spec("mfn_threshold_pct",
              "the MFN yield differential that triggers repricing of the "
              "Initial Term Loans", "percent", "economic_terms", 5, None, None,
              verified_term=False, anchors=["All-In Yield"], sections=["2.14"]),
        # Kind is text, not date: a sunset is written as a period running from
        # closing ("twelve months after the Closing Date"), not as a calendar
        # date. Typing it as a date would force the extractor to compute one,
        # and no model in this pipeline is allowed to compute anything.
        _spec("mfn_sunset",
              "any expiry or sunset of the MFN protection", "text",
              "economic_terms", 5, None, None, verified_term=False,
              sections=["2.14"],
              negative="This agreement contains no provision under which the "
                       "MFN (most favoured nation) pricing protection expires, "
                       "sunsets or ceases to apply after any period of time."),
        _spec("consolidated_ebitda.addback_cap_pct",
              "the percentage cap on cost-savings add-backs to Consolidated "
              "EBITDA", "percent", "covenant_levels", 5, None, None,
              verified_term=False, anchors=["Consolidated EBITDA"]),
        _spec("consolidated_ebitda.addback_cap_clause_a_xvi",
              "the cap applicable to the run-rate synergies add-back in clause "
              "(a)(xvi) of Consolidated EBITDA", "percent", "covenant_levels", 5,
              None, None, verified_term=False, anchors=["Consolidated EBITDA"]),
    ]
}

FIELD_CLASSES: tuple[str, ...] = (
    "economic_terms", "dates", "covenant_levels", "baskets", "parties",
    "administrative",
)


def fields_in_class(field_class: str) -> list[FieldSpec]:
    return [s for s in FIELD_REGISTRY.values() if s.field_class == field_class]


def provenance() -> dict:
    return {
        "standard": "FpML",
        "publisher": "ISDA",
        "version": FPML_VERSION,
        "namespace": FPML_NAMESPACE,
        "source": FPML_SOURCE,
        "verified": False,
        "note": (
            "fpml.org was unreachable from the build environment; loan product "
            "element names are hand-mapped from FpML 5.x and marked "
            "verified=False. Run scripts/gen_fpml.py --verify to promote."
        ),
    }
