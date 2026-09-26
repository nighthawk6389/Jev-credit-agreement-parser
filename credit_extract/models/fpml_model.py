"""FpML loan-product structure and the field registry.

FpML 5.x carries syndicated-loan product definitions built with LSTA/LMA input:
facility types, tranches, accrual options, commitment schedules, LC
sub-facilities and fee types. The intended build path is ``xsdata`` over the
published XSDs (see ``scripts/gen_fpml.py``). Element names resolve against a
checked-in index generated from a pinned public mirror of the published FpML
schemas. Unknown names raise rather than silently becoming plausible-looking
URIs.

    What ``verified=True`` claims here, and what it does not. It claims the
    element name is declared in the pinned schemas -- which the hand-mapped
    names it replaced were not. It does not claim the element means what the
    field it is bound to means, and it does not claim the element is legal in
    the position the mapping implies: FpML declares some names in more than
    one scope and the index merges those declarations. ``declaration()``
    returns what the schemas actually say about a name, and the
    ``FPML_SCHEMAS`` blind spot states the limit in the terms a reader of the
    report needs.

The registry at the bottom is the pipeline's spine: it names every extraction
target, the standard term each maps to, the field class its confidence
threshold is fitted on, and the affirmative question the negative-space
validator asks when the extractor comes back empty.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .fibo_map import TermBinding, binding as fibo_binding, gap as fibo_gap

FPML_VERSION = "5-13-7-rec-1"
FPML_NAMESPACE = "http://www.fpml.org/FpML-5/confirmation"
FPML_SOURCE = "https://www.fpml.org/spec/fpml-5-13-7-rec-1/"
_VENDORED = Path(__file__).parent / "vendored" / "fpml_terms.json"


@lru_cache(maxsize=1)
def _vendored() -> dict:
    if not _VENDORED.exists():  # pragma: no cover - packaging regression
        raise RuntimeError(f"{_VENDORED} is missing; run `python scripts/gen_fpml.py --vendor`")
    return json.loads(_VENDORED.read_text())


def declaration(term: str) -> dict:
    """What the pinned schemas declare about an element name.

    ``schemas`` is the interesting key: a loan concept bound to an element
    that only the asset schema declares is a mapping worth a second look, not
    a resolution failure, so the fact travels with the binding instead of
    being flattened into a yes.
    """
    return _vendored()["elements"][term]


def fpml(term: str) -> TermBinding:
    """Bind to a declared FpML element, refusing invented element names."""
    elements = _vendored()["elements"]
    if term not in elements:
        raise KeyError(
            f"{term!r} is not in the vendored FpML snapshot "
            f"({len(elements)} elements from {len(_vendored()['schema_files'])} schemas). "
            "Add the relevant schema to scripts/gen_fpml.py rather than inventing a URI."
        )
    return TermBinding(
        standard="fpml",
        term=f"fpml:{term}",
        uri=f"{FPML_NAMESPACE}#{term}",
        label=term,
        verified=True,
    )


# ---------------------------------------------------------------------------
# Product structure
# ---------------------------------------------------------------------------

FacilityType = Literal[
    "term_loan", "delayed_draw_term_loan", "revolver", "letter_of_credit"
]
RateOptionType = Literal[
    "fixed", "floating", "legacy_floating", "accruing_pik", "accruing_fee"
]


class AccrualTerms(BaseModel):
    """Facility rate economics represented by FpML rate-option elements."""

    option_type: RateOptionType = "floating"
    base_rate: str | None = None              # fpml:floatingRateIndex
    spread_pct: Decimal | None = None         # fpml:spread
    credit_spread_adjustment_pct: Decimal | None = None  # fpml:spreadAdjustment
    floor_pct: Decimal | None = None          # fpml:floorRate
    cap_pct: Decimal | None = None            # fpml:capRate
    day_count: str | None = None              # fpml:dayCountFraction
    payment_frequency: str | None = None       # fpml:paymentFrequency
    is_compounding_balance: bool | None = None # fpml:isCompoundingBalance
    period_months: int | None = None          # normalized convenience value
    alternate_base_rate_spread_pct: Decimal | None = None


class PikTerms(BaseModel):
    """Payment-in-kind economics, not flattened into ordinary cash margin."""

    rate_pct: Decimal | None = None            # fpml:accruingPikOption
    spread_pct: Decimal | None = None          # fpml:pikSpread
    start_date: date | None = None
    end_date: date | None = None
    capitalization_frequency: str | None = None


class CommitmentTerms(BaseModel):
    """Current/original commitments and draw optionality."""

    current_amount: Decimal | None = None      # fpml:currentCommitment
    original_amount: Decimal | None = None     # fpml:originalCommitment
    unavailable_amount: Decimal | None = None  # fpml:unavailableToUtilizeAmount
    must_draw_by_date: date | None = None      # fpml:mustDrawByDate
    refusal_allowed: bool | None = None        # fpml:refusalAllowed
    scheduled_adjustment: bool | None = None   # fpml:scheduled
    pik_adjustment: bool | None = None         # fpml:pik


class FacilityClassification(BaseModel):
    """Legal/economic classification retained at facility level."""

    feature: str | None = None                 # fpml:feature (bridge/acquisition/...)
    lien: str | None = None                    # fpml:lien
    seniority: str | None = None               # fpml:seniority
    governing_law: str | None = None           # fpml:governingLaw
    multi_currency: bool | None = None          # fpml:multiCurrency
    draw_currencies: list[str] = Field(default_factory=list)


class CreditRating(BaseModel):
    """Rating and issuer classification without conflating the two."""

    agency: str | None = None
    rating: str | None = None                  # fpml:creditRating
    credit_quality: str | None = None          # fpml:creditQuality
    industry_classification: str | None = None # fpml:classification


class PartyReferences(BaseModel):
    borrower: str | None = None
    co_borrowers: list[str] = Field(default_factory=list)
    agent: str | None = None
    lc_issuing_banks: list[str] = Field(default_factory=list)
    guarantors: list[str] = Field(default_factory=list)


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
    commitment_fee_pct: Decimal | None = None     # fpml:accruingFeeOption
    ticking_fee_pct: Decimal | None = None
    lc_participation_fee_pct: Decimal | None = None
    fronting_fee_pct: Decimal | None = None       # fpml:accruingFeeOption


class Facility(BaseModel):
    """One tranche, combining FpML economics with FIBO semantics."""

    facility_id: str                              # fpml:facilityIdentifier
    facility_type: FacilityType                   # FpML substitution element
    commitment_amount: Decimal | None = None      # fpml:totalCommitmentAmount
    currency: str = "USD"                         # fpml:currency
    effective_date: date | None = None            # fpml:startDate
    maturity_date: date | None = None             # fpml:maturityDate
    accrual: AccrualTerms = Field(default_factory=AccrualTerms)
    rate_options: list[AccrualTerms] = Field(default_factory=list)
    pik: PikTerms | None = None
    commitment: CommitmentTerms = Field(default_factory=CommitmentTerms)
    classification: FacilityClassification = Field(default_factory=FacilityClassification)
    parties: PartyReferences = Field(default_factory=PartyReferences)
    ratings: list[CreditRating] = Field(default_factory=list)
    fees: FeeTerms = Field(default_factory=FeeTerms)
    amortization: AmortizationSchedule | None = None
    sublimit_of: str | None = None                # FIBO sub-facility relationship
    actus_mapping: dict[str, Any] | None = None   # ActusMapping, or None + reason


def facility_bindings() -> dict[str, TermBinding]:
    return {
        "facility_id": fpml("facilityIdentifier"),
        "term_loan": fpml("termLoan"),
        "delayed_draw_term_loan": fpml("delayedDraw"),
        "revolver": fpml("revolver"),
        "letter_of_credit": fpml("letterOfCreditFacility"),
        "commitment_amount": fpml("totalCommitmentAmount"),
        "current_commitment": fpml("currentCommitment"),
        "original_commitment": fpml("originalCommitment"),
        "unavailable_commitment": fpml("unavailableToUtilizeAmount"),
        "currency": fpml("currency"),
        "maturity_date": fpml("maturityDate"),
        "effective_date": fpml("startDate"),
        "floating_rate_option": fpml("floatingRateOption"),
        "legacy_floating_rate_option": fpml("legacyFloatingRateOption"),
        "pik_option": fpml("accruingPikOption"),
        "base_rate": fpml("floatingRateIndex"),
        "spread": fpml("spread"),
        "credit_spread_adjustment": fpml("spreadAdjustment"),
        "floor": fpml("floorRate"),
        "cap": fpml("capRate"),
        "day_count": fpml("dayCountFraction"),
        "payment_frequency": fpml("paymentFrequency"),
        "commitment_schedule": fpml("commitmentSchedule"),
        "accruing_fee": fpml("accruingFeeOption"),
        "lien": fpml("lien"),
        "seniority": fpml("seniority"),
        "feature": fpml("feature"),
        "governing_law": fpml("governingLaw"),
        "multi_currency": fpml("multiCurrency"),
        "borrower": fpml("borrowerPartyReference"),
        "guarantor": fpml("guarantorPartyReference"),
        "agent": fpml("agentPartyReference"),
        "must_draw_by_date": fpml("mustDrawByDate"),
        "evergreen_option": fpml("evergreenOption"),
        "credit_rating": fpml("creditRating"),
        "credit_quality": fpml("creditQuality"),
        "classification": fpml("classification"),
    }


def standards_bindings() -> dict[str, list[TermBinding]]:
    """Conceptual fields with complementary FpML and FIBO bindings.

    FpML describes the operational loan record; FIBO describes what the
    entities and relationships mean. A missing term in either standard stays a
    documented gap instead of being force-fit.
    """
    return {
        "term_loan": [fpml("termLoan"), fibo_binding("fibo-fbc-dae-dbt:CreditFacility")],
        "revolver": [fpml("revolver"), fibo_binding("fibo-fbc-dae-dbt:CreditFacility")],
        "commitment": [fpml("currentCommitment"), fibo_binding("fibo-fnd-agr-agr:Commitment")],
        "floating_rate": [fpml("floatingRateOption"), fibo_binding("fibo-fbc-dae-dbt:FloatingInterestRate")],
        "lien": [fpml("lien"), fibo_binding("fibo-loan-ln-ln:LenderLienPosition")],
        "borrower": [fpml("borrowerPartyReference"), fibo_binding("fibo-fbc-dae-dbt:Borrower")],
        "guarantor": [fpml("guarantorPartyReference"), fibo_binding("fibo-fbc-dae-gty:Guarantor")],
        "credit_rating": [fpml("creditRating"), fibo_binding("fibo-fbc-dae-crt:CreditRating")],
        "industry_classification": [
            fpml("classification"),
            fibo_binding("fibo-fnd-arr-cls:IndustrySectorClassificationScheme"),
        ],
        "pik": [
            fpml("accruingPikOption"),
            fibo_gap("FIBO has no dedicated payment-in-kind loan accrual term"),
        ],
        "seniority": [
            fpml("seniority"),
            fibo_gap("FIBO models lien position, but no equivalent facility seniority field"),
        ],
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
        _spec("guarantor.legal_name", "the identity of each Guarantor", "text",
              "parties", 3, "fibo-fbc-dae-gty:Guarantor", "fibo"),
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
              "fpml:effectiveDate", "fpml",
              anchors=["Closing Date"]),
        _spec("initial_term_loan.maturity_date",
              "the maturity date of the Initial Term Loans", "date", "dates", 5,
              "fpml:maturityDate", "fpml",
              anchors=["Initial Term Loan Maturity Date"]),
        _spec("revolver.maturity_date",
              "the maturity date of the Revolving Credit Facility", "date",
              "dates", 5, "fpml:maturityDate", "fpml",
              anchors=["Revolving Credit Maturity Date"]),
        # -- economic terms --------------------------------------------------
        _spec("initial_term_loan.commitment",
              "the aggregate principal amount of the Initial Term Loans",
              "money", "economic_terms", 5, "fpml:totalCommitmentAmount", "fpml",
              anchors=["Initial Term Loan Commitment"],
              sections=["2.01"]),
        _spec("delayed_draw.commitment",
              "the aggregate Delayed Draw Term Loan Commitments", "money",
              "economic_terms", 4, "fpml:totalCommitmentAmount", "fpml",
              anchors=["Delayed Draw Term Loan Commitment"]),
        _spec("revolver.commitment",
              "the aggregate Revolving Credit Commitments", "money",
              "economic_terms", 5, "fpml:totalCommitmentAmount", "fpml",
              anchors=["Revolving Credit Commitment"]),
        _spec("lc_sublimit", "the Letter of Credit Sublimit", "money",
              "economic_terms", 3, "fpml:letterOfCreditFacility", "fpml",
              sections=["2.05"]),
        # The field name says LIBO and the corpus does not. It is kept because
        # renaming a registry key breaks every label that targets it, and the
        # name is cosmetic where the anchors are not: "LIBO Rate" resolved as
        # a defined term in 0 of 100 harvested agreements, so this field's
        # definitional route was dead. "Floor" resolves in 35 of them, with a
        # single clean percentage in 20.
        _spec("libor_floor_pct", "the benchmark rate floor", "percent",
              "economic_terms", 5, "fpml:floorRate", "fpml",
              anchors=["Floor", "SOFR Floor", "LIBO Rate"]),
        _spec("applicable_margin.eurodollar_top_level_pct",
              "the highest Eurodollar Applicable Margin in the pricing grid",
              "percent", "economic_terms", 5, "fpml:spread", "fpml",
              anchors=["Applicable Margin"],
              sections=["2.12"]),
        _spec("commitment_fee_pct", "the unused commitment fee", "percent",
              "economic_terms", 4, "fpml:accruingFeeOption", "fpml",
              sections=["2.09"]),
        _spec("fronting_fee_pct", "the letter of credit fronting fee", "percent",
              "economic_terms", 2, "fpml:accruingFeeOption", "fpml",
              sections=["2.05"]),
        _spec("ticking_fee_pct",
              "the ticking fee on undrawn delayed draw commitments", "percent",
              "economic_terms", 3, "fpml:accruingFeeOption", "fpml",
              sections=["2.09"]),
        # -- FpML/FIBO structural depth ------------------------------------
        _spec("facility.feature", "the facility feature or purpose classification",
              "text", "administrative", 2, "fpml:feature", "fpml"),
        _spec("facility.lien", "the facility lien position", "text",
              "economic_terms", 4, "fpml:lien", "fpml"),
        _spec("facility.seniority", "the facility seniority or ranking", "text",
              "economic_terms", 4, "fpml:seniority", "fpml"),
        _spec("facility.governing_law", "the governing law of the facility", "text",
              "administrative", 2, "fpml:governingLaw", "fpml"),
        _spec("facility.multi_currency", "whether the facility permits multiple currencies",
              "bool", "economic_terms", 3, "fpml:multiCurrency", "fpml"),
        _spec("facility.evergreen_option", "any evergreen extension option",
              "bool", "economic_terms", 3, "fpml:evergreenOption", "fpml"),
        _spec("delayed_draw.must_draw_by_date", "the last date a delayed draw may be made",
              "date", "dates", 4, "fpml:mustDrawByDate", "fpml"),
        _spec("delayed_draw.refusal_allowed", "whether a delayed draw can be refused",
              "bool", "economic_terms", 3, "fpml:refusalAllowed", "fpml"),
        # "Credit Spread Adjustment" resolved in 0 of 100 agreements; the terms
        # that do are SOFR-era. Deliberately NOT anchored on "Benchmark
        # Replacement Adjustment", which resolves in 18 of 50 sampled and
        # carries a percentage in none of them: it is the fallback machinery
        # that computes an adjustment on transition, not a rate anybody pays.
        # Anchoring there would hand this field a number from a mechanism.
        _spec("accrual.credit_spread_adjustment_pct",
              "the credit spread adjustment added to the benchmark", "percent",
              "economic_terms", 5, "fpml:spreadAdjustment", "fpml",
              anchors=["Term SOFR Adjustment", "SOFR Adjustment",
                       "Credit Spread Adjustment"]),
        _spec("accrual.cap_pct", "the cap on the applicable base or all-in rate",
              "percent", "economic_terms", 4, "fpml:capRate", "fpml"),
        _spec("pik.rate_pct", "the payment-in-kind interest rate", "percent",
              "economic_terms", 5, "fpml:accruingPikOption", "fpml",
              anchors=["PIK Interest"]),
        _spec("pik.spread_pct", "the payment-in-kind spread", "percent",
              "economic_terms", 5, "fpml:pikSpread", "fpml",
              anchors=["PIK Interest"]),
        _spec("rating.value", "the borrower or facility credit rating", "text",
              "economic_terms", 3, "fpml:creditRating", "fpml"),
        _spec("rating.agency", "the credit rating agency", "text", "parties", 2,
              "fibo-fnd-arr-rt:RatingAgency", "fibo"),
        _spec("rating.credit_quality", "the investment-grade credit quality classification",
              "text", "economic_terms", 3, "fpml:creditQuality", "fpml"),
        _spec("borrower.industry_classification", "the borrower's industry classification",
              "text", "administrative", 1, "fpml:classification", "fpml"),
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
        # The covenant is one field that changes over time, stored as variants.
        # The opening and final levels below remain for the scalar view.
        _spec("financial_covenant.level",
              "the maximum leverage level under the financial covenant, as it "
              "stands at a given date", "ratio", "covenant_levels", 5,
              None, None, verified_term=False,
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
        # -- archetype-specific (F06) ----------------------------------------
        # These exist so that an archetype that has them can be measured, and
        # so that one that does not resolves them to not_applicable_to_archetype
        # rather than to a spurious null that drags recall down.
        _spec("borrowing_base.advance_rate_accounts",
              "the advance rate against eligible accounts receivable",
              "percent", "economic_terms", 5, None, None, verified_term=False,
              anchors=["Borrowing Base"]),
        _spec("borrowing_base.advance_rate_inventory",
              "the advance rate against eligible inventory", "percent",
              "economic_terms", 5, None, None, verified_term=False,
              anchors=["Borrowing Base"]),
        _spec("borrowing_base.availability_block",
              "the availability block reserved against the borrowing base",
              "money", "economic_terms", 4, None, None, verified_term=False),
        _spec("arr.leverage_covenant_level",
              "the maximum ARR leverage ratio", "ratio", "covenant_levels", 5,
              None, None, verified_term=False,
              anchors=["Annualized Recurring Revenue"]),
        _spec("arr.minimum_liquidity",
              "the minimum liquidity covenant", "money", "covenant_levels", 5,
              None, None, verified_term=False),
        _spec("nav.loan_to_value_cap",
              "the maximum loan-to-value against portfolio net asset value",
              "percent", "covenant_levels", 5, None, None, verified_term=False),
        _spec("pik.toggle_step_up_pct",
              "the margin step-up when interest is paid in kind", "percent",
              "economic_terms", 5, None, None, verified_term=False),
    ]
}

FIELD_CLASSES: tuple[str, ...] = (
    "economic_terms", "dates", "covenant_levels", "baskets", "parties",
    "administrative",
)


def fields_in_class(field_class: str) -> list[FieldSpec]:
    return [s for s in FIELD_REGISTRY.values() if s.field_class == field_class]


def mapped_terms() -> set[str]:
    """Every FpML element name this repository binds to.

    ``fpml()`` raises on a name outside the snapshot, so the two binding
    functions check themselves the moment they are called. ``FIELD_REGISTRY``
    is the one they do not cover: ``standard_term`` is a plain string there,
    so a term that does not exist reaches a report looking like every other
    one. ``scripts/gen_fpml.py --check`` and the test suite both read this.
    """
    bindings = list(facility_bindings().values())
    bindings += [b for pair in standards_bindings().values() for b in pair]
    terms = {b.term.split(":", 1)[1] for b in bindings
             if b.term and b.term.startswith("fpml:")}
    terms |= {spec.standard_term.split(":", 1)[1]
              for spec in FIELD_REGISTRY.values()
              if spec.standard_term and spec.standard_term.startswith("fpml:")}
    return terms


def provenance() -> dict:
    v = _vendored()
    composed = {
        concept: [binding.model_dump() for binding in bindings]
        for concept, bindings in standards_bindings().items()
    }
    return {
        "standard": "FpML",
        "publisher": "ISDA",
        "version": FPML_VERSION,
        "namespace": FPML_NAMESPACE,
        "source": FPML_SOURCE,
        "mirror_commit": v["mirror_commit"],
        "schema_files": v["schema_files"],
        "elements_available": len(v["elements"]),
        "elements_mapped": len(mapped_terms()),
        "composed_with_fibo": composed,
        "verified": True,
        "verified_claim": (
            "every mapped element name is declared in the pinned schemas; "
            "the schemas' own meaning and position for it are not checked"
        ),
        "note": (
            "Element names resolve against a checked-in index generated from "
            "the published schemas at a pinned public mirror commit, not from "
            "ISDA directly. See the FPML_SCHEMAS blind spot."
        ),
    }
