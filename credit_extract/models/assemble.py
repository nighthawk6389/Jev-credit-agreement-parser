"""Build a :class:`~.agreement.CreditAgreement` from a flat field record.

The pipeline produces ``dict[str, ExtractedField]`` -- fifty-six registry
fields in one namespace, with tranche membership encoded in a dotted prefix
and nothing at all encoding the difference between a fact about the deal and a
fact about one tranche. This module reads that record and produces the shape
the market actually has.

THE MAPPING IS DATA, NOT CODE
=============================

Every registry field is named in one of the tables below, and
:func:`unmapped_fields` reports any that are not. That check is the reason the
tables are data: a field added to the registry with no slot here would
otherwise be extracted, validated, costed and then silently dropped on the way
out, which is the failure this whole repository is organised against.

THREE KINDS OF SLOT, AND THE MIDDLE ONE IS THE INTERESTING ONE
==============================================================

``AGREEMENT_SLOTS``
    Facts about the deal. One borrower, one governing law, one agent.

``TRANCHES``
    Facts read for one tranche specifically. There are five such fields in the
    registry today: each tranche's commitment and maturity, and the delayed
    draw's must-draw-by date.

``INHERITED``
    Facts that *belong* to a tranche and that the registry only knows
    deal-wide. The margin is the clearest: a revolver and a delayed-draw term
    loan in the same agreement price differently, and
    ``applicable_margin.eurodollar_top_level_pct`` is one field. Handing it to
    all three tranches is the best available answer and is also a claim the
    document does not make, so every slot filled this way carries
    :attr:`~.agreement.Asserted.basis` saying where it came from, and
    :attr:`~.agreement.AssemblyReport.inherited_slots` counts them.

    That is the whole point of the exercise. The old export duplicated these
    values silently and a reader could not tell a per-tranche reading from a
    deal-level one. Now the output says which it is, every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .agreement import (
    AccrualEconomics, AgreementDates, AgreementMetadata, AmendmentLink,
    AssemblyReport, Asserted, BorrowingBase, CovenantPackage, CreditAgreement,
    FinancialCovenant, Parties, Rating, TrancheCondition, TrancheKind,
    TrancheFees, TrancheReference, TrancheSchedules, TrancheTerms, Tranche,
)
from .core import ExtractedField
from .fpml_model import FIELD_REGISTRY

#: Registry field -> where it lands in :class:`AgreementMetadata`, as a dotted
#: path. Deal-level: read once, true of the whole agreement.
AGREEMENT_SLOTS: dict[str, str] = {
    "borrower.legal_name": "parties.borrower",
    "holdings.legal_name": "parties.holdings",
    "administrative_agent.legal_name": "parties.administrative_agent",
    "collateral_agent.legal_name": "parties.collateral_agent",
    "syndication_agent.legal_name": "parties.syndication_agent",
    "arranger.legal_name": "parties.arranger",
    "facility.governing_law": "governing_law",
    "facility.multi_currency": "multi_currency",
    "facility.evergreen_option": "evergreen_option",
    "borrower.industry_classification": "industry_classification",
    "closing_date": "dates.closing_date",
}

#: Registry field -> a list-valued role on :class:`Parties`. Separate from
#: ``AGREEMENT_SLOTS`` because the registry holds one name where the deal has
#: several: a credit agreement with twelve guarantors has one
#: ``guarantor.legal_name``. The slot is a tuple so the others have somewhere
#: to go when the extractor learns to find them.
PARTY_LIST_SLOTS: dict[str, str] = {
    "guarantor.legal_name": "guarantors",
}

#: Registry field -> slot in :class:`CovenantPackage`. These have no FpML
#: element anywhere, which is why the old export dropped all of them.
COVENANT_SLOTS: dict[str, str] = {
    "excess_cash_flow.sweep_pct": "excess_cash_flow_sweep_pct",
    "mfn_threshold_pct": "mfn_threshold_pct",
    "mfn_sunset": "mfn_sunset",
    "incremental.free_and_clear_amount": "incremental_free_and_clear",
    "incremental.leverage_based_test": "incremental_leverage_test",
    "consolidated_ebitda.addback_cap_pct": "ebitda_addback_cap_pct",
    "consolidated_ebitda.addback_cap_clause_a_xvi": "ebitda_addback_cap_clause",
    "indebtedness.purchase_money_basket_amount": "purchase_money_basket_amount",
    "indebtedness.purchase_money_basket_ebitda_pct":
        "purchase_money_basket_ebitda_pct",
    "opening_total_leverage_ratio": "opening_total_leverage_ratio",
    "arr.leverage_covenant_level": "arr_leverage_covenant_level",
    "arr.minimum_liquidity": "arr_minimum_liquidity",
}

#: Registry field -> slot in :class:`BorrowingBase`.
BORROWING_BASE_SLOTS: dict[str, str] = {
    "borrowing_base.advance_rate_accounts": "advance_rate_accounts",
    "borrowing_base.advance_rate_inventory": "advance_rate_inventory",
    "borrowing_base.availability_block": "availability_block",
    "nav.loan_to_value_cap": "loan_to_value_cap",
}

#: The maintenance test. Three registry fields describe one covenant over
#: time -- where it opens, where it sits, where it ends -- so they assemble
#: into a single :class:`FinancialCovenant` rather than three.
COVENANT_LEVEL_SLOTS: dict[str, str] = {
    "financial_covenant.opening_level": "opening_level",
    "financial_covenant.level": "level",
    "financial_covenant.final_level": "final_level",
}

#: Ratings. Three fields, one :class:`Rating`.
RATING_SLOTS: dict[str, str] = {
    "rating.agency": "agency",
    "rating.value": "rating",
    "rating.credit_quality": "credit_quality",
}

#: Tranche slot -> the deal-level registry field that fills it, and why that
#: is weaker than a reading. The note travels into every value.
INHERITED: dict[str, tuple[str, str]] = {
    "terms.accrual.spread_pct": (
        "applicable_margin.eurodollar_top_level_pct",
        "deal-level margin: the registry holds one margin for the agreement, "
        "so tranches that price differently are quoted the same here",
    ),
    "terms.accrual.credit_spread_adjustment_pct": (
        "accrual.credit_spread_adjustment_pct",
        "deal-level: one CSA field for the agreement",
    ),
    "terms.accrual.floor_pct": (
        "libor_floor_pct",
        "deal-level: one floor field for the agreement",
    ),
    "terms.accrual.cap_pct": (
        "accrual.cap_pct",
        "deal-level: one cap field for the agreement",
    ),
    "terms.accrual.pik_rate_pct": (
        "pik.rate_pct", "deal-level: one PIK rate field for the agreement",
    ),
    "terms.accrual.pik_spread_pct": (
        "pik.spread_pct", "deal-level: one PIK spread field for the agreement",
    ),
    "terms.accrual.pik_toggle_step_up_pct": (
        "pik.toggle_step_up_pct",
        "deal-level: one PIK toggle field for the agreement",
    ),
    "terms.fees.commitment_fee_pct": (
        "commitment_fee_pct",
        "deal-level: one commitment fee field for the agreement",
    ),
    "terms.fees.ticking_fee_pct": (
        "ticking_fee_pct", "deal-level: one ticking fee field for the agreement",
    ),
    "terms.fees.fronting_fee_pct": (
        "fronting_fee_pct",
        "deal-level: one fronting fee field for the agreement",
    ),
    "lien": ("facility.lien", "deal-level: one lien field for the agreement"),
    "seniority": (
        "facility.seniority", "deal-level: one seniority field for the agreement",
    ),
    "feature": (
        "facility.feature", "deal-level: one feature field for the agreement",
    ),
    "schedules.amortization_quarterly_amount": (
        "amortization.quarterly_amount",
        "deal-level: one amortization field, which in a multi-tranche deal "
        "amortizes only the term loans",
    ),
}

#: Inherited slot -> the tranche kinds that can carry it at all, and why the
#: others cannot. Offering a slot to a tranche the fee cannot attach to is not
#: a weak reading, it is an invented term: a term loan drawn in full at closing
#: has no undrawn commitment to charge a commitment fee on, and saying it pays
#: 0.375% is a claim the document never made about it.
#:
#: This is the one place where per-tranche economics is genuinely a matter of
#: which tranche, rather than of which price. A ticking fee is charged on a
#: commitment that has been signed and not yet drawn, which is the definition
#: of a delayed draw; a fronting fee is what an issuing bank charges for
#: standing behind a letter of credit. Neither is "the same term priced
#: differently per tranche" -- they are terms only one tranche can have.
ELIGIBLE_KINDS: dict[str, tuple[frozenset[TrancheKind], str]] = {
    "terms.fees.commitment_fee_pct": (
        frozenset({"revolver", "letter_of_credit", "delayed_draw_term_loan"}),
        "a commitment fee accrues on an undrawn commitment, and a term loan "
        "drawn in full at closing has none",
    ),
    "terms.fees.ticking_fee_pct": (
        frozenset({"delayed_draw_term_loan"}),
        "a ticking fee accrues between signing and funding, which is what a "
        "delayed draw is; a revolver's undrawn balance pays the commitment "
        "fee instead",
    ),
    "terms.fees.fronting_fee_pct": (
        frozenset({"letter_of_credit"}),
        "a fronting fee is charged by the issuing bank for standing behind a "
        "letter of credit, so it attaches to the LC line, not to the "
        "revolving commitment it draws against",
    ),
    "schedules.amortization_quarterly_amount": (
        frozenset({"term_loan", "delayed_draw_term_loan"}),
        "amortization repays principal before maturity; a revolver repays on "
        "maturity and reborrows until then",
    ),
}


@dataclass(frozen=True)
class TrancheSpec:
    """One tranche the registry can describe, and the fields that size it."""

    tranche_id: str
    kind: TrancheKind
    commitment_field: str | None = None
    maturity_field: str | None = None
    availability_field: str | None = None
    refusal_field: str | None = None
    sublimit_of: str | None = None


#: Every tranche the field registry can currently describe. The LC sublimit is
#: a tranche that draws against the revolver rather than adding to it, which
#: is why it carries ``sublimit_of``: summing commitments across this list
#: without reading that would double-count the deal.
TRANCHES: tuple[TrancheSpec, ...] = (
    TrancheSpec(
        "revolver", "revolver",
        commitment_field="revolver.commitment",
        maturity_field="revolver.maturity_date",
    ),
    TrancheSpec(
        "initial_term_loan", "term_loan",
        commitment_field="initial_term_loan.commitment",
        maturity_field="initial_term_loan.maturity_date",
    ),
    TrancheSpec(
        "delayed_draw", "delayed_draw_term_loan",
        commitment_field="delayed_draw.commitment",
        # The registry has no separate maturity for the delayed draw. It
        # matures with the initial term loan in the deals this corpus holds,
        # and the value is marked inherited so a reader can see that is an
        # assumption rather than a reading.
        maturity_field="initial_term_loan.maturity_date",
        availability_field="delayed_draw.must_draw_by_date",
        refusal_field="delayed_draw.refusal_allowed",
    ),
    TrancheSpec(
        "lc_sublimit", "letter_of_credit",
        commitment_field="lc_sublimit",
        maturity_field="revolver.maturity_date",
        sublimit_of="revolver",
    ),
)

#: Fields consumed by the tranche table, derived so it cannot drift from it.
_TRANCHE_FIELDS: frozenset[str] = frozenset(
    f for spec in TRANCHES
    for f in (spec.commitment_field, spec.maturity_field,
              spec.availability_field, spec.refusal_field)
    if f
)


def mapped_fields() -> frozenset[str]:
    """Every registry field this module knows a slot for."""
    return frozenset(
        set(AGREEMENT_SLOTS)
        | set(PARTY_LIST_SLOTS)
        | set(COVENANT_SLOTS)
        | set(BORROWING_BASE_SLOTS)
        | set(COVENANT_LEVEL_SLOTS)
        | set(RATING_SLOTS)
        | {field for field, _ in INHERITED.values()}
        | _TRANCHE_FIELDS
    )


def unmapped_fields() -> tuple[str, ...]:
    """Registry fields with nowhere to go. Should always be empty.

    A field here is extracted, validated, costed and then dropped on the way
    out. The test suite asserts this is empty, so adding a registry field
    without a slot fails loudly rather than quietly.
    """
    return tuple(sorted(set(FIELD_REGISTRY) - mapped_fields()))


def _set_path(model: Any, path: str, value: Any) -> None:
    """Assign to a dotted attribute path, building nothing along the way."""
    head, _, tail = path.rpartition(".")
    target = model
    for part in head.split(".") if head else []:
        target = getattr(target, part)
    setattr(target, tail, value)


def _tranche(
    spec: TrancheSpec,
    fields: dict[str, ExtractedField],
    inherited: list[str],
    ruled_out: list[str],
) -> Tranche:
    """One tranche, with its own readings where they exist and the deal's
    where they do not."""
    tranche = Tranche(
        tranche_id=spec.tranche_id, kind=spec.kind, sublimit_of=spec.sublimit_of,
        terms=TrancheTerms(accrual=AccrualEconomics(), fees=TrancheFees()),
        reference=TrancheReference(), schedules=TrancheSchedules(),
    )

    # -- read for this tranche --------------------------------------------
    if spec.commitment_field:
        tranche.terms.commitment = Asserted.from_field(
            fields, spec.commitment_field
        )
        tranche.established = tranche.terms.commitment.settled
    if spec.maturity_field:
        # The delayed draw borrows the term loan's maturity, so the same field
        # is a reading for one tranche and an assumption for another.
        borrowed = spec.tranche_id not in spec.maturity_field
        tranche.terms.maturity_date = Asserted.from_field(
            fields, spec.maturity_field,
            basis=(
                f"{spec.maturity_field}: this tranche has no maturity field of "
                "its own in the registry"
                if borrowed else ""
            ),
        )
        if borrowed:
            inherited.append(f"{spec.tranche_id}.terms.maturity_date")
    if spec.availability_field:
        tranche.terms.availability_end_date = Asserted.from_field(
            fields, spec.availability_field
        )

    # -- handed down from the deal ----------------------------------------
    for path, (field_name, why) in INHERITED.items():
        kinds, ineligible = ELIGIBLE_KINDS.get(path, (None, ""))
        if kinds is not None and spec.kind not in kinds:
            ruled_out.append(f"{spec.tranche_id}.{path}: {ineligible}")
            continue
        asserted = Asserted.from_field(
            fields, field_name, basis=why, for_tranche=spec.tranche_id,
        )
        _set_path(tranche, path, asserted)
        if asserted.value is not None and asserted.inherited:
            # A value the document attributed to this tranche is a reading, so
            # it is not counted as an inherited slot even though the registry
            # field it came from is deal-level. ``inherited`` measures the
            # extraction gap, and a gap that has been closed for this tranche
            # should stop being counted.
            inherited.append(f"{spec.tranche_id}.{path}")

    # -- conditions --------------------------------------------------------
    conditions: list[TrancheCondition] = []
    if spec.refusal_field:
        refusal = fields.get(spec.refusal_field)
        variant = refusal.variants[0] if refusal and refusal.variants else None
        if variant is not None and variant.value is not None:
            conditions.append(TrancheCondition(
                kind="availability",
                applies_to="terms.commitment",
                trigger="lenders may refuse a delayed-draw request",
                consequence=(
                    "the commitment is not certain funds"
                    if variant.value else "funding is mandatory once requested"
                ),
                resolved=bool(variant.value),
                spans=tuple(variant.spans),
                source_field=spec.refusal_field,
            ))
    conditions.extend(_conditions_from_variants(tranche, fields, spec))
    tranche.conditions = tuple(conditions)
    return tranche


def _conditions_from_variants(
    tranche: Tranche, fields: dict[str, ExtractedField], spec: TrancheSpec,
) -> Iterable[TrancheCondition]:
    """Surface the conditions already attached to this tranche's variants.

    A field that steps down over time, or converts on a trigger, stores that
    as ``Variant.conditions``. The old export resolved to one variant and
    dropped the rest, so a conditional term left the pipeline looking
    unconditional -- which is the whole of family F10.
    """
    watched = {
        spec.commitment_field: "terms.commitment",
        spec.maturity_field: "terms.maturity_date",
    }
    for field_name, applies_to in watched.items():
        if not field_name:
            continue
        field = fields.get(field_name)
        if field is None:
            continue
        for variant in field.variants:
            for condition in variant.conditions:
                yield TrancheCondition(
                    kind=(
                        "springing_maturity"
                        if applies_to.endswith("maturity_date")
                        else "commitment_reduction"
                    ),
                    applies_to=applies_to,
                    trigger=condition.expr,
                    expression=condition.expr,
                    consequence=(
                        f"{field_name} = {variant.value}"
                        if variant.value is not None else ""
                    ),
                    spans=tuple(condition.source_spans or variant.spans),
                    source_field=field_name,
                )


def _covenants(fields: dict[str, ExtractedField]) -> CovenantPackage:
    package = CovenantPackage()
    for field_name, slot in COVENANT_SLOTS.items():
        setattr(package, slot, Asserted.from_field(fields, field_name))

    covenant = FinancialCovenant(name="financial covenant")
    for field_name, slot in COVENANT_LEVEL_SLOTS.items():
        setattr(covenant, slot, Asserted.from_field(fields, field_name))
    if any(getattr(covenant, s).value is not None
           for s in COVENANT_LEVEL_SLOTS.values()):
        # Direction is not in the registry and guessing it from the level is
        # exactly the error a coverage ratio invites, so it stays unset.
        package.financial = (covenant,)
    return package


def _borrowing_base(fields: dict[str, ExtractedField]) -> BorrowingBase | None:
    base = BorrowingBase()
    for field_name, slot in BORROWING_BASE_SLOTS.items():
        setattr(base, slot, Asserted.from_field(fields, field_name))
    populated = any(
        getattr(base, slot).value is not None
        for slot in BORROWING_BASE_SLOTS.values()
    )
    return base if populated else None


def _ratings(fields: dict[str, ExtractedField]) -> tuple[Rating, ...]:
    rating = Rating()
    for field_name, slot in RATING_SLOTS.items():
        setattr(rating, slot, Asserted.from_field(fields, field_name))
    if any(getattr(rating, s).value is not None for s in RATING_SLOTS.values()):
        return (rating,)
    return ()


def build_agreement(
    fields: dict[str, ExtractedField],
    *,
    agreement_id: str,
    document_id: str | None = None,
    source_path: str | None = None,
    archetype: dict[str, Any] | None = None,
    chain: Iterable[AmendmentLink] = (),
    unplaced_readings: Iterable[str] = (),
) -> CreditAgreement:
    """Assemble the whole agreement from one document's field record.

    Unlike ``export.build_facilities``, this drops nothing. A tranche whose
    commitment never settled is still carried, with ``established=False``, and
    an unsettled margin is still carried, with its status. The discipline that
    made the old export withhold lives in :attr:`~.agreement.Asserted.status`
    now, where it costs no information.
    """
    inherited: list[str] = []
    ruled_out: list[str] = []
    tranches = tuple(
        _tranche(spec, fields, inherited, ruled_out) for spec in TRANCHES
    )

    metadata = AgreementMetadata(
        agreement_id=agreement_id,
        document_id=document_id,
        source_path=source_path,
        dates=AgreementDates(),
        parties=Parties(),
        ratings=_ratings(fields),
    )
    for field_name, path in AGREEMENT_SLOTS.items():
        _set_path(metadata, path, Asserted.from_field(fields, field_name))

    for field_name, slot in PARTY_LIST_SLOTS.items():
        named = Asserted.from_field(fields, field_name)
        if named.value is not None:
            setattr(metadata.parties, slot, (named,))

    if archetype:
        metadata.archetype = archetype.get("archetype")
        metadata.archetype_confidence = archetype.get("confidence")
        metadata.archetype_basis = str(archetype.get("basis") or "")
    metadata.chain = tuple(chain)

    return CreditAgreement(
        metadata=metadata,
        tranches=tranches,
        covenants=_covenants(fields),
        borrowing_base=_borrowing_base(fields),
        assembly=AssemblyReport(
            unmapped_fields=unmapped_fields(),
            inherited_slots=tuple(sorted(inherited)),
            unplaced_readings=tuple(sorted(unplaced_readings)),
            ineligible_slots=tuple(sorted(ruled_out)),
        ),
    )
