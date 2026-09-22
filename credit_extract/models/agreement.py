"""The output structure: one agreement, its metadata, and its tranches.

WHY THIS EXISTS, AND WHAT WAS WRONG BEFORE
==========================================

The word *facility* means two different things in this market and the codebase
had picked the wrong one. In ``fpml_model.py`` a :class:`~.fpml_model.Facility`
is **one tranche** -- it carries a ``facility_type`` of ``revolver`` or
``term_loan``, one ``commitment_amount``, one ``maturity_date`` -- and
``build_facilities()`` returns a *list* of them. There was no object for the
agreement. So every deal-level fact had nowhere to live except on each tranche,
and a three-tranche deal reported the borrower three times, the agent three
times, the governing law three times.

That is not merely redundant. It is wrong, and it is wrong in a way that hides
a real defect: because the field registry has one document-level margin, the
revolver, the term loan and the delayed-draw loan were all handed the *same*
spread, the same floor and the same fee. A reader of that export would conclude
the three tranches price identically. In the agreement they do not.

This module says the thing the market says:

    An AGREEMENT has metadata and tranches.
    A TRANCHE has terms, conditions, reference data and schedules.

MISSING IS A STATE, NOT AN ABSENCE
==================================

``models/export.py`` withholds any field that is not settled, for a reason that
is good and still holds *there*: FpML's ``Facility`` has a bare
``Decimal | None`` and no room to say how the number was arrived at, so pouring
an unsettled value into it launders a question into an answer. On Essential
Properties that rule withholds 58 of 78 slots, and a consumer sees a deal with
almost nothing in it.

This model has room. Every leaf is an :class:`Asserted`, which carries the
value *and* its status, its spans, its confidence and the registry field it
came from. So nothing is withheld here: an unsettled margin crosses as a
margin whose status is ``needs_review``, and a consumer that wants only settled
terms filters on ``.settled`` rather than being handed silence. The discipline
is preserved -- you still cannot mistake a guess for a term -- without the
information loss.

The FpML projection keeps the strict rule. :meth:`Tranche.to_fpml` withholds
exactly what ``export.py`` withholds, because the target still has no room.
The two coexist: this is the record, that is the interchange format.

WHERE THE SHAPE IS AHEAD OF THE EXTRACTOR
=========================================

:class:`Tranche` has a slot for each tranche's own spread. The registry has one
``applicable_margin.eurodollar_top_level_pct`` for the whole document. Rather
than pretend, the assembler records the deal-level origin in
:attr:`Asserted.basis`, so an export where three tranches share a margin says
so in each one. A gap that is visible in the output is a gap somebody can close;
a gap papered over by duplication is one nobody can see.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .core import CONFIDENT_STATUSES, ExtractedField, FieldStatus, Span

T = TypeVar("T")

TrancheKind = Literal[
    "revolver", "term_loan", "delayed_draw_term_loan", "letter_of_credit",
    "swingline", "incremental", "unknown",
]

#: Settled enough that a downstream consumer may act on the value without
#: reading the note. Mirrors ``export.EXPORTABLE`` and is deliberately not the
#: same set as ``CONFIDENT_STATUSES``: a field that is inapplicable to the
#: archetype is a confident answer about the deal but not a term to act on.
ACTIONABLE: frozenset[str] = frozenset({"confirmed", "absent_from_document"})


class Asserted(BaseModel, Generic[T]):
    """A value, and everything a reader needs to decide whether to trust it.

    This is the unit the whole structure is built from. It is a flattened
    :class:`~.core.ExtractedField` -- the variants, the validation trace and
    the resolution steps stay in the record, and what travels here is the
    answer plus its provenance.
    """

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    status: FieldStatus = "needs_review"
    #: Calibrated where a validator produced one, extraction confidence
    #: otherwise. None when the field was never populated at all.
    confidence: float | None = None
    spans: tuple[Span, ...] = ()
    #: The registry field this came from, so a value here can be taken back to
    #: its variants, its trace and the passes that produced it.
    source_field: str | None = None
    #: Why this value is on *this* tranche. Empty when it was read for this
    #: tranche specifically. Set when it was inherited -- from the agreement,
    #: or from a sibling tranche -- which is a weaker claim and says so.
    basis: str = ""
    #: Populated when status is external_reference.
    external_document: str | None = None
    note: str = ""

    @property
    def settled(self) -> bool:
        """True when a consumer may act on this without reading the note."""
        return self.status in ACTIONABLE

    @property
    def confident(self) -> bool:
        """True when the pipeline is asserting something, right or wrong.

        Wider than :attr:`settled`: an inapplicable field is a confident claim
        about the deal, and the silent-error budget counts it.
        """
        return self.status in CONFIDENT_STATUSES

    @property
    def inherited(self) -> bool:
        return bool(self.basis)

    @classmethod
    def missing(cls, source_field: str | None = None) -> "Asserted[T]":
        """No such field in the record. Distinct from a field that is empty."""
        return cls(
            status="needs_review", source_field=source_field,
            note="no such field in this record",
        )

    @classmethod
    def from_field(
        cls,
        fields: dict[str, ExtractedField],
        name: str,
        basis: str = "",
    ) -> "Asserted[T]":
        """Flatten the registry field ``name``, keeping its status and spans."""
        field = fields.get(name)
        if field is None:
            return cls.missing(source_field=name)
        variant = field.variants[0] if field.variants else None
        if variant is None:  # pragma: no cover - validator guarantees one
            return cls.missing(source_field=name)
        confidence = (
            variant.validation_confidence
            if variant.validation_confidence is not None
            else variant.extraction_confidence
        )
        return cls(
            value=variant.value,
            status=variant.status,
            confidence=confidence,
            spans=tuple(variant.spans),
            source_field=name,
            basis=basis,
            external_document=variant.external_document,
            note=variant.notes or "",
        )


# ---------------------------------------------------------------------------
# Reference data -- the conventions a tranche is quoted under
# ---------------------------------------------------------------------------

class BenchmarkReference(BaseModel):
    """The index a floating tranche accrues on, and how it is constructed.

    Kept apart from the *rate* on purpose. A benchmark has an administrator, a
    tenor and a fallback waterfall, and none of those is the number paid. F07
    exists because pipelines flatten the two.
    """

    index: Asserted[str] = Field(default_factory=Asserted)
    tenor: Asserted[str] = Field(default_factory=Asserted)
    administrator: str | None = None
    #: The ordered replacement ladder, as the agreement states it.
    fallback_waterfall: tuple[str, ...] = ()
    #: "hardwired" | "amendment" -- whether replacement needs a new amendment.
    fallback_mechanism: str | None = None
    #: Set where the agreement quotes a term SOFR rate against a daily index.
    is_term_rate: bool | None = None


class MarketConventions(BaseModel):
    """How the arithmetic is done. Reference data, not deal terms."""

    day_count: Asserted[str] = Field(default_factory=Asserted)
    business_day_convention: Asserted[str] = Field(default_factory=Asserted)
    payment_frequency: Asserted[str] = Field(default_factory=Asserted)
    interest_period_months: Asserted[int] = Field(default_factory=Asserted)
    rate_reset_lag_days: Asserted[int] = Field(default_factory=Asserted)
    #: Financial centres whose holidays move a payment date.
    business_centres: tuple[str, ...] = ()


class StandardsBindings(BaseModel):
    """What this tranche is called in each vocabulary it maps to.

    ``verified`` travels per standard because the three are not equally
    checked: FpML names resolve against pinned schemas, FIBO against a
    vendored ontology, and the ACTUS mapping is this repository's own
    judgement about which contract type a tranche behaves like.
    """

    fpml_element: str | None = None
    fpml_verified: bool = False
    fibo_term: str | None = None
    fibo_verified: bool = False
    actus_contract_type: str | None = None
    actus_note: str = ""


class TrancheIdentifiers(BaseModel):
    """Market identifiers. Almost never in the filing; the slots exist so a
    consumer that has them from elsewhere has somewhere to put them."""

    lin: str | None = None        # LSTA loan identification number
    cusip: str | None = None
    isin: str | None = None
    bloomberg_id: str | None = None


class TrancheReference(BaseModel):
    """Everything about a tranche that is convention rather than negotiation."""

    benchmark: BenchmarkReference = Field(default_factory=BenchmarkReference)
    conventions: MarketConventions = Field(default_factory=MarketConventions)
    standards: StandardsBindings = Field(default_factory=StandardsBindings)
    identifiers: TrancheIdentifiers = Field(default_factory=TrancheIdentifiers)
    currency: str = "USD"
    permitted_currencies: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

class AccrualEconomics(BaseModel):
    """What this tranche pays, decomposed into the parts that are negotiated
    separately. A single ``rate`` field would have to pick one and lose the
    rest, and the parts move independently: a floor binds when the index is
    low, a CSA is added once at transition, a step-down fires on leverage."""

    spread_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    credit_spread_adjustment_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    floor_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    cap_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    #: The alternate base rate margin, which is a different number from the
    #: benchmark margin and lives in the same grid row.
    alternate_base_rate_spread_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    pik_rate_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    pik_spread_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    #: What the margin becomes if the borrower elects to pay in kind. A toggle
    #: is priced: the step-up is the cost of the option, and folding it into
    #: the cash margin reports a deal as cheaper than it is.
    pik_toggle_step_up_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    #: "fixed" | "floating" | "accruing_pik" | ...
    option_type: str = "floating"


class TrancheFees(BaseModel):
    """Fees charged on this tranche. A term loan has none of the first three."""

    commitment_fee_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    ticking_fee_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    fronting_fee_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    lc_participation_fee_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    #: Set where a fee is fixed by a fee letter nobody filed. The value is real
    #: and is somewhere else, which is not the same as there being no fee.
    fixed_by_external_document: str | None = None


class TrancheTerms(BaseModel):
    """The negotiated economics of one tranche."""

    commitment: Asserted[Decimal] = Field(default_factory=Asserted)
    #: Drawn balance where the agreement states one separately from the
    #: commitment, which an amendment to an existing facility usually does.
    outstanding: Asserted[Decimal] = Field(default_factory=Asserted)
    original_commitment: Asserted[Decimal] = Field(default_factory=Asserted)
    unavailable_amount: Asserted[Decimal] = Field(default_factory=Asserted)
    maturity_date: Asserted[date] = Field(default_factory=Asserted)
    availability_end_date: Asserted[date] = Field(default_factory=Asserted)
    accrual: AccrualEconomics = Field(default_factory=AccrualEconomics)
    fees: TrancheFees = Field(default_factory=TrancheFees)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

class TrancheCondition(BaseModel):
    """Something that has to be true for a term to apply, or for money to move.

    F10 is the family this exists for. A maturity that springs 91 days before
    some other facility's, a covenant that binds only inside a test period, a
    margin step-down that needs an IPO first: each is a term plus a predicate,
    and a model that stores only the term reports a conditional fact as an
    unconditional one.
    """

    #: What kind of thing the condition gates.
    kind: Literal[
        "springing_maturity", "availability", "condition_precedent",
        "step_down", "step_up", "covenant_test_period", "mandatory_prepayment",
        "commitment_reduction", "other",
    ] = "other"
    #: The term it gates, as a dotted path into this tranche where one applies.
    applies_to: str | None = None
    #: The predicate in the agreement's own words.
    trigger: str = ""
    #: The machine-readable form, where ``models/conditions.py`` could parse
    #: one. None means the predicate was recorded but not compiled.
    expression: str | None = None
    #: What the term becomes when the predicate holds.
    consequence: str = ""
    resolved: bool | None = None
    resolution_note: str = ""
    spans: tuple[Span, ...] = ()
    source_field: str | None = None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

class ScheduleEntry(BaseModel):
    """One dated row of any schedule."""

    on: date | None = None
    amount: Decimal | None = None
    pct: Decimal | None = None
    label: str = ""
    row_index: int = 0
    #: Index in the printed table, kept apart from ``row_index`` so a
    #: deduplicated or reordered schedule can still be traced to the page.
    source_row: int | None = None
    span: Span | None = None


class PricingGridRow(BaseModel):
    """One level of a pricing grid: a band, and what is paid inside it."""

    level: int | None = None
    test: str = ""
    lower: Decimal | None = None
    upper: Decimal | None = None
    spread_pct: Decimal | None = None
    alternate_base_rate_spread_pct: Decimal | None = None
    commitment_fee_pct: Decimal | None = None
    span: Span | None = None


class TrancheSchedules(BaseModel):
    """Everything about this tranche that is a series rather than a scalar.

    ``amortization`` is where Trap 1 lives: the rows and the stated bullet are
    two independent facts, and the arithmetic between them is a check rather
    than a derivation. ``derived_bullet`` is deliberately not stored -- storing
    it would make the reconciliation self-satisfying.
    """

    amortization: tuple[ScheduleEntry, ...] = ()
    #: The level payment where the agreement states a rate rather than a table
    #: -- "0.25% of the original principal amount, quarterly". Kept beside the
    #: rows rather than expanded into them: expanding it would manufacture a
    #: schedule the document does not print, and Trap 1 turns on the printed
    #: rows and the printed bullet being independent.
    amortization_quarterly_amount: Asserted[Decimal] = Field(default_factory=Asserted)
    #: Stated in the agreement. The only thing worth checking the rows against.
    stated_bullet_at_maturity: Asserted[Decimal] = Field(default_factory=Asserted)
    original_principal: Asserted[Decimal] = Field(default_factory=Asserted)
    commitment_reductions: tuple[ScheduleEntry, ...] = ()
    pricing_grid: tuple[PricingGridRow, ...] = ()
    interest_payment_dates: tuple[ScheduleEntry, ...] = ()

    @property
    def total_scheduled(self) -> Decimal:
        return sum((e.amount or Decimal(0) for e in self.amortization), Decimal(0))

    @property
    def derived_bullet(self) -> Decimal | None:
        """Balance left at maturity given these rows. Never stored."""
        principal = self.original_principal.value
        if principal is None:
            return None
        return Decimal(principal) - self.total_scheduled

    def bullet_reconciles(self) -> bool | None:
        """Whether the printed bullet and the rows agree. None when untestable."""
        stated = self.stated_bullet_at_maturity.value
        derived = self.derived_bullet
        if stated is None or derived is None:
            return None
        return Decimal(stated) == derived


# ---------------------------------------------------------------------------
# The tranche
# ---------------------------------------------------------------------------

class Tranche(BaseModel):
    """One tranche of one agreement: what it is, what it costs, when it moves."""

    tranche_id: str
    kind: TrancheKind = "unknown"
    #: True when the tranche's existence is settled -- its commitment is
    #: confirmed, or confirmed absent. A tranche that the document mentions but
    #: never sizes is still carried, because dropping it loses the mention, but
    #: a consumer building a position should filter on this.
    established: bool = False
    lien: Asserted[str] = Field(default_factory=Asserted)
    seniority: Asserted[str] = Field(default_factory=Asserted)
    feature: Asserted[str] = Field(default_factory=Asserted)
    #: Set where this tranche draws against another's commitment rather than
    #: adding to it -- an LC sublimit is not new money.
    sublimit_of: str | None = None

    terms: TrancheTerms = Field(default_factory=TrancheTerms)
    conditions: tuple[TrancheCondition, ...] = ()
    reference: TrancheReference = Field(default_factory=TrancheReference)
    schedules: TrancheSchedules = Field(default_factory=TrancheSchedules)

    def leaves(self) -> dict[str, Asserted]:
        """Every :class:`Asserted` in this tranche, by dotted path."""
        return _walk(self, prefix="")

    def coverage(self) -> dict[str, int]:
        """How many of this tranche's slots are settled, unsettled, empty."""
        settled = unsettled = empty = 0
        for asserted in self.leaves().values():
            if asserted.settled:
                settled += 1
            elif asserted.value is not None:
                unsettled += 1
            else:
                empty += 1
        return {"settled": settled, "unsettled": unsettled, "empty": empty}

    def inherited_from_agreement(self) -> list[str]:
        """Slots filled from a deal-level field rather than read per tranche.

        Non-empty is the honest signature of the registry gap this model was
        built to expose: three tranches quoting one margin.
        """
        return sorted(p for p, a in self.leaves().items() if a.inherited)

    def to_fpml(self, agreement: "CreditAgreement") -> dict[str, Any]:
        """Project into the verified FpML element names, strictly.

        The strictness is not duplicated for its own sake. FpML's ``Facility``
        has a bare ``Decimal | None`` and no room to say how a number was
        arrived at, so an unsettled value poured into it reads as a term
        somebody established. Here, where the whole structure carries status,
        nothing needs withholding; there, everything unsettled does.

        Returns ``{element: value}`` for what crosses. What does not is in
        :meth:`withheld_from_fpml`, which names a reason for each, because an
        element missing from an FpML document reads as "this deal has no such
        term" and that is a different claim from "nobody settled it".
        """
        crossing, _ = self._project(agreement)
        return crossing

    def withheld_from_fpml(self, agreement: "CreditAgreement") -> dict[str, str]:
        """Element -> why it did not cross into the FpML projection."""
        _, withheld = self._project(agreement)
        return withheld

    def _project(
        self, agreement: "CreditAgreement"
    ) -> tuple[dict[str, Any], dict[str, str]]:
        # element name -> the Asserted that fills it. Deal-level elements come
        # from the agreement, which is the point of the split: in FpML they sit
        # on the facility, and here they are read once.
        mapping: dict[str, Asserted] = {
            "totalCommitmentAmount": self.terms.commitment,
            "maturityDate": self.terms.maturity_date,
            "startDate": agreement.metadata.dates.closing_date,
            "spread": self.terms.accrual.spread_pct,
            "spreadAdjustment": self.terms.accrual.credit_spread_adjustment_pct,
            "floorRate": self.terms.accrual.floor_pct,
            "capRate": self.terms.accrual.cap_pct,
            "mustDrawByDate": self.terms.availability_end_date,
            "lien": self.lien,
            "seniority": self.seniority,
            "feature": self.feature,
            "governingLaw": agreement.metadata.governing_law,
            "multiCurrency": agreement.metadata.multi_currency,
        }
        crossing: dict[str, Any] = {}
        withheld: dict[str, str] = {}
        for element, asserted in mapping.items():
            if not asserted.settled:
                withheld[element] = (
                    f"{asserted.source_field or element}: "
                    f"not settled ({asserted.status})"
                )
            elif asserted.value is None:
                # absent_from_document: None here means what FpML reads it as,
                # which is that the agreement was checked and has no such term.
                crossing[element] = None
            else:
                crossing[element] = asserted.value
        return crossing, withheld


# ---------------------------------------------------------------------------
# Agreement-level metadata
# ---------------------------------------------------------------------------

class PartyRole(BaseModel):
    """One party, in one role. A party may hold several."""

    role: str
    name: Asserted[str] = Field(default_factory=Asserted)
    #: Where the same role is held by more than one entity -- a multi-currency
    #: deal with an agent per jurisdiction -- the others land here rather than
    #: one of them being picked by convention.
    co_holders: tuple[str, ...] = ()


class Parties(BaseModel):
    """Who is on the deal, in which role.

    The multi-valued roles are tuples of :class:`Asserted` rather than of
    ``str`` so that each named party keeps its own status and citation. A
    guarantor list where one name is confirmed and another is a guess is the
    normal case, and flattening it to ``list[str]`` asserts both equally.
    """

    borrower: Asserted[str] = Field(default_factory=Asserted)
    co_borrowers: tuple[Asserted[str], ...] = ()
    holdings: Asserted[str] = Field(default_factory=Asserted)
    guarantors: tuple[Asserted[str], ...] = ()
    administrative_agent: Asserted[str] = Field(default_factory=Asserted)
    collateral_agent: Asserted[str] = Field(default_factory=Asserted)
    syndication_agent: Asserted[str] = Field(default_factory=Asserted)
    arranger: Asserted[str] = Field(default_factory=Asserted)
    lc_issuing_banks: tuple[Asserted[str], ...] = ()
    #: The general form, for roles the registry does not name individually and
    #: for roles held jointly -- a multi-currency deal with an agent per
    #: jurisdiction has one role and two holders, and neither is the answer.
    other_roles: tuple[PartyRole, ...] = ()


class Rating(BaseModel):
    agency: Asserted[str] = Field(default_factory=Asserted)
    rating: Asserted[str] = Field(default_factory=Asserted)
    credit_quality: Asserted[str] = Field(default_factory=Asserted)


class AgreementDates(BaseModel):
    closing_date: Asserted[date] = Field(default_factory=Asserted)
    effective_date: Asserted[date] = Field(default_factory=Asserted)
    execution_date: Asserted[date] = Field(default_factory=Asserted)


class AmendmentLink(BaseModel):
    """One document in the chain, and what it did to the one before it."""

    document_id: str
    sequence: int | None = None
    dated: date | None = None
    effects: tuple[str, ...] = ()
    #: True where this document restates rather than amends, so its text
    #: supersedes rather than patches.
    restates: bool = False


class AgreementMetadata(BaseModel):
    """Everything true of the deal rather than of one tranche."""

    agreement_id: str
    document_id: str | None = None
    source_path: str | None = None
    #: "credit_agreement" | "amendment" | "amended_and_restated" | ...
    agreement_type: str = "credit_agreement"
    governing_law: Asserted[str] = Field(default_factory=Asserted)
    dates: AgreementDates = Field(default_factory=AgreementDates)
    parties: Parties = Field(default_factory=Parties)
    ratings: tuple[Rating, ...] = ()
    industry_classification: Asserted[str] = Field(default_factory=Asserted)
    multi_currency: Asserted[bool] = Field(default_factory=Asserted)
    base_currency: str = "USD"
    #: An option to extend the maturity annually without a new agreement.
    #: Agreement-level because it moves every tranche's maturity at once.
    evergreen_option: Asserted[bool] = Field(default_factory=Asserted)

    #: What the document was classified as, and how sure. Carried because the
    #: archetype decides which fields are marked inapplicable, so a consumer
    #: reading an inapplicable field needs to know what decided it.
    archetype: str | None = None
    archetype_confidence: float | None = None
    archetype_basis: str = ""

    fiscal_year_end: str | None = None
    chain: tuple[AmendmentLink, ...] = ()


# ---------------------------------------------------------------------------
# Covenants -- the agreement-level terms FpML has no element for
# ---------------------------------------------------------------------------

class FinancialCovenant(BaseModel):
    """A maintenance test. Direction matters: a coverage ratio is a floor and
    a leverage ratio is a ceiling, and a model that stores only the number
    reports a breach as compliance half the time."""

    name: str = ""
    #: "ceiling" | "floor"
    direction: str | None = None
    opening_level: Asserted[str] = Field(default_factory=Asserted)
    level: Asserted[str] = Field(default_factory=Asserted)
    final_level: Asserted[str] = Field(default_factory=Asserted)
    step_downs: tuple[ScheduleEntry, ...] = ()
    #: Set where the covenant binds only in a window -- a springing FCCR.
    test_condition: TrancheCondition | None = None


class CovenantPackage(BaseModel):
    """The negotiated restrictions. Agreement-level, because that is how they
    are drafted: one leverage test binds the borrower, not a tranche.

    These are the terms that have no FpML home at all. Leaving them out of the
    output because the interchange format cannot carry them would mean the
    pipeline's best work never leaves the building.
    """

    financial: tuple[FinancialCovenant, ...] = ()
    excess_cash_flow_sweep_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    mfn_threshold_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    mfn_sunset: Asserted[str] = Field(default_factory=Asserted)
    incremental_free_and_clear: Asserted[Decimal] = Field(default_factory=Asserted)
    incremental_leverage_test: Asserted[str] = Field(default_factory=Asserted)
    ebitda_addback_cap_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    ebitda_addback_cap_clause: Asserted[str] = Field(default_factory=Asserted)
    purchase_money_basket_amount: Asserted[Decimal] = Field(default_factory=Asserted)
    purchase_money_basket_ebitda_pct: Asserted[Decimal] = Field(default_factory=Asserted)
    opening_total_leverage_ratio: Asserted[str] = Field(default_factory=Asserted)
    #: Recurring-revenue deals covenant on ARR and liquidity instead of on
    #: EBITDA, because the borrower has none. Separate slots rather than reused
    #: ones: an ARR leverage level and an EBITDA leverage level are not the
    #: same measurement and a consumer comparing them across deals would be
    #: comparing different denominators.
    arr_leverage_covenant_level: Asserted[str] = Field(default_factory=Asserted)
    arr_minimum_liquidity: Asserted[Decimal] = Field(default_factory=Asserted)


class BorrowingBase(BaseModel):
    """Present on asset-based and warehouse deals and absent everywhere else.

    Kept at agreement level rather than on the revolver because the advance
    rates are set once and the sublimits draw against them.
    """

    advance_rate_accounts: Asserted[Decimal] = Field(default_factory=Asserted)
    advance_rate_inventory: Asserted[Decimal] = Field(default_factory=Asserted)
    availability_block: Asserted[Decimal] = Field(default_factory=Asserted)
    loan_to_value_cap: Asserted[Decimal] = Field(default_factory=Asserted)


# ---------------------------------------------------------------------------
# The agreement
# ---------------------------------------------------------------------------

class AssemblyReport(BaseModel):
    """What the assembler could and could not do, as a fact about the run."""

    #: Registry fields with no slot anywhere in this structure. Should be
    #: empty; a non-empty list means the registry grew and this model did not.
    unmapped_fields: tuple[str, ...] = ()
    #: Slots filled from a deal-level field because the registry has no
    #: per-tranche equivalent. The visible form of the extraction gap.
    inherited_slots: tuple[str, ...] = ()
    #: Recorded model readings whose quote no chunk held, so they silently did
    #: not count. Surfaced here because nothing else surfaces it.
    unplaced_readings: tuple[str, ...] = ()
    tranches_dropped: tuple[str, ...] = ()


class CreditAgreement(BaseModel):
    """One agreement: metadata, tranches, covenants, and what was not filled.

    This is the structure the pipeline is for. ``models/export.py`` projects a
    tranche of it into FpML when an interchange format is wanted; this is the
    record, and it is the one that can carry a status beside every value.
    """

    metadata: AgreementMetadata
    tranches: tuple[Tranche, ...] = ()
    covenants: CovenantPackage = Field(default_factory=CovenantPackage)
    borrowing_base: BorrowingBase | None = None
    assembly: AssemblyReport = Field(default_factory=AssemblyReport)

    # -- accounting ----------------------------------------------------------

    def leaves(self) -> dict[str, Asserted]:
        """Every :class:`Asserted` in the agreement, by dotted path."""
        return _walk(self, prefix="")

    def coverage(self) -> dict[str, int]:
        """Settled / unsettled-with-a-value / empty, over the whole structure.

        The three are different problems. Settled is done. Unsettled with a
        value is a calibration question -- the reading exists and is under the
        threshold. Empty is recall: nothing was ever extracted, and no
        threshold move will produce it.
        """
        leaves = self.leaves()
        settled = sum(1 for a in leaves.values() if a.settled)
        unsettled = sum(
            1 for a in leaves.values() if not a.settled and a.value is not None
        )
        return {
            "slots": len(leaves),
            "settled": settled,
            "unsettled_with_value": unsettled,
            "empty": len(leaves) - settled - unsettled,
        }

    def established_tranches(self) -> tuple[Tranche, ...]:
        """The tranches whose existence the document settles."""
        return tuple(t for t in self.tranches if t.established)

    def confident_claims(self) -> int:
        """How many propositions this record asserts confidently.

        The denominator of the silent-error rate, in the structure rather than
        in the flat field record.
        """
        return sum(1 for a in self.leaves().values() if a.confident)


def _walk(model: BaseModel, prefix: str) -> dict[str, Asserted]:
    """Collect every ``Asserted`` under ``model``, keyed by dotted path."""
    out: dict[str, Asserted] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        path = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
        if isinstance(value, Asserted):
            out[path] = value
        elif isinstance(value, BaseModel):
            out.update(_walk(value, path))
        elif isinstance(value, (tuple, list)):
            for i, item in enumerate(value):
                if isinstance(item, Asserted):
                    out[f"{path}[{i}]"] = item
                elif isinstance(item, BaseModel):
                    out.update(_walk(item, f"{path}[{i}]"))
    return out
