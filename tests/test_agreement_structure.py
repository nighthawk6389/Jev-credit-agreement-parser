"""The output structure: an agreement with metadata, and tranches with terms.

Every test here fails for something that was true of the export before this
structure existed. The old one had no object for the agreement, so a
three-tranche deal repeated the borrower three times; it dropped every field
that had not settled, so a real document left with almost nothing; and it had
no element for a covenant, so the covenant work never left the building.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.extract.passes import LayeredBackend
from credit_extract.models.agreement import (
    ACTIONABLE, AgreementMetadata, Asserted, CreditAgreement, ScheduleEntry,
    Tranche, TrancheSchedules,
)
from credit_extract.models.assemble import (
    INHERITED, TRANCHES, build_agreement, mapped_fields, unmapped_fields,
)
from credit_extract.models.core import CONFIDENT_STATUSES, ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY, _vendored


def _field(name: str, value, status: str, start: int = 0) -> ExtractedField:
    """One settled-or-not field, with the span the model demands of a value."""
    spans = (
        [Span(start=start, end=start + 10, text="x" * 10)]
        if value is not None else []
    )
    return ExtractedField.single(
        value=value, status=status, spans=spans, extraction_confidence=0.9
    )


# ---------------------------------------------------------------------------
# The mapping is complete, and stays complete
# ---------------------------------------------------------------------------


def test_every_registry_field_has_somewhere_to_go():
    """A field with no slot is extracted, validated, costed and then dropped.

    This is the check that makes the mapping tables data rather than
    archaeology: adding a registry field without a slot fails here instead of
    silently vanishing on the way out.
    """
    assert unmapped_fields() == (), (
        "registry fields with no slot in CreditAgreement: "
        f"{unmapped_fields()}. Add them to a table in models/assemble.py."
    )
    assert mapped_fields() >= set(FIELD_REGISTRY)


def test_the_confident_set_has_one_definition():
    """Two copies that drifted would move the silent-error denominator."""
    from credit_extract.eval.assertions import CONFIDENT_STATUSES as evaluated

    assert evaluated is CONFIDENT_STATUSES


def test_actionable_is_narrower_than_confident():
    """An inapplicable field is a confident claim and not a term to act on.

    Collapsing the two would export "this deal kind has no such term" as a
    term, which is the failure ``not_applicable_to_archetype`` exists to
    prevent.
    """
    assert ACTIONABLE < CONFIDENT_STATUSES
    assert "not_applicable_to_archetype" in CONFIDENT_STATUSES
    assert "not_applicable_to_archetype" not in ACTIONABLE


# ---------------------------------------------------------------------------
# A tranche is a tranche, not a copy of the deal
# ---------------------------------------------------------------------------


def test_a_deal_level_margin_is_marked_inherited_on_every_tranche():
    """The defect this structure was built to expose, made visible.

    The registry holds one margin for the agreement. Three tranches that price
    differently are still quoted the same number, because that is the best
    available answer -- but each one now says the number is deal-level, so a
    reader can tell it from a per-tranche reading.
    """
    fields = {
        "applicable_margin.eurodollar_top_level_pct":
            _field("margin", Decimal("1.75"), "confirmed"),
        "revolver.commitment": _field("rc", Decimal("500"), "confirmed", 20),
        "initial_term_loan.commitment": _field("tc", Decimal("300"), "confirmed", 40),
    }
    agreement = build_agreement(fields, agreement_id="d")

    priced = [
        t for t in agreement.tranches
        if t.terms.accrual.spread_pct.value is not None
    ]
    assert len(priced) > 1, "the fixture should reach more than one tranche"
    for tranche in priced:
        spread = tranche.terms.accrual.spread_pct
        assert spread.value == Decimal("1.75")
        assert spread.inherited, f"{tranche.tranche_id} should flag the origin"
        assert "deal-level" in spread.basis

    assert agreement.assembly.inherited_slots, (
        "the assembly report should count them, so the gap is visible in the "
        "output and not only in the per-tranche basis"
    )


def test_a_commitment_read_for_one_tranche_is_not_inherited():
    """The counterweight: five registry fields really are per-tranche, and
    marking those inherited too would make the signal meaningless."""
    fields = {
        "revolver.commitment": _field("rc", Decimal("500"), "confirmed"),
    }
    agreement = build_agreement(fields, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    assert revolver.terms.commitment.value == Decimal("500")
    assert not revolver.terms.commitment.inherited
    assert revolver.terms.commitment.source_field == "revolver.commitment"


def test_the_delayed_draw_says_its_maturity_is_borrowed():
    """It matures with the term loan in every deal in this corpus, and the
    registry has no field of its own for it. That is an assumption, and an
    assumption presented as a reading is the thing this repository is against.
    """
    fields = {
        "delayed_draw.commitment": _field("dc", Decimal("450"), "confirmed"),
        "initial_term_loan.maturity_date": _field(
            "m", date(2030, 1, 1), "confirmed", 30
        ),
    }
    agreement = build_agreement(fields, agreement_id="d")
    ddtl = next(t for t in agreement.tranches if t.tranche_id == "delayed_draw")
    term = next(t for t in agreement.tranches if t.tranche_id == "initial_term_loan")

    assert ddtl.terms.maturity_date.value == date(2030, 1, 1)
    assert ddtl.terms.maturity_date.inherited
    # The same field, read for the tranche it belongs to, is not inherited.
    assert not term.terms.maturity_date.inherited


def test_a_term_loan_is_not_offered_a_commitment_fee():
    """An undrawn-balance fee on a fully drawn term loan is not a weak
    reading, it is an invented term. Inheritance is not a licence to put a
    value where the instrument cannot have one."""
    fields = {"commitment_fee_pct": _field("cf", Decimal("0.25"), "confirmed")}
    agreement = build_agreement(fields, agreement_id="d")

    term = next(t for t in agreement.tranches if t.tranche_id == "initial_term_loan")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    assert term.terms.fees.commitment_fee_pct.value is None
    assert revolver.terms.fees.commitment_fee_pct.value == Decimal("0.25")


def test_a_sublimit_is_marked_so_commitments_are_not_double_counted():
    """An LC sublimit draws against the revolver rather than adding to it.
    Summing commitments across the tranche list without reading this reports
    the deal as larger than it is."""
    agreement = build_agreement({}, agreement_id="d")
    lc = next(t for t in agreement.tranches if t.tranche_id == "lc_sublimit")

    assert lc.sublimit_of == "revolver"
    assert [t.tranche_id for t in agreement.tranches if t.sublimit_of] == [
        "lc_sublimit"
    ]


# ---------------------------------------------------------------------------
# Missing is a state, not an absence
# ---------------------------------------------------------------------------


def test_an_unsettled_value_is_carried_with_its_status():
    """The old export withheld it and the consumer saw silence. Here it
    crosses, and a consumer that wants only settled terms filters."""
    fields = {
        "revolver.commitment": _field("rc", Decimal("500"), "needs_review"),
    }
    agreement = build_agreement(fields, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    assert revolver.terms.commitment.value == Decimal("500")
    assert revolver.terms.commitment.status == "needs_review"
    assert not revolver.terms.commitment.settled
    assert not revolver.established, (
        "carrying the value is not the same as establishing the tranche"
    )


def test_an_unestablished_tranche_is_still_carried():
    """Dropping it loses the document's mention of it. The old export dropped
    every tranche whose commitment had not settled, which on one real fund
    facility meant exporting nothing at all."""
    agreement = build_agreement({}, agreement_id="d")

    assert len(agreement.tranches) == len(TRANCHES)
    assert agreement.established_tranches() == ()


def test_coverage_separates_recall_from_calibration():
    """Three different problems, and the reason strings in the old export
    called two of them the same thing. Empty is recall -- nothing was ever
    extracted. Unsettled-with-a-value is calibration -- the reading exists and
    sits under the threshold."""
    fields = {
        "revolver.commitment": _field("rc", Decimal("500"), "confirmed"),
        "facility.governing_law": _field("gl", "New York", "needs_review", 60),
    }
    agreement = build_agreement(fields, agreement_id="d")
    coverage = agreement.coverage()

    assert coverage["settled"] >= 1
    assert coverage["unsettled_with_value"] >= 1
    assert coverage["empty"] > 0
    assert (
        coverage["settled"] + coverage["unsettled_with_value"] + coverage["empty"]
        == coverage["slots"]
    )


# ---------------------------------------------------------------------------
# The FpML projection stays strict, because its target has no room
# ---------------------------------------------------------------------------


def test_the_fpml_projection_withholds_what_the_structure_carries():
    """Both behaviours are right, for different targets. FpML's Facility has a
    bare ``Decimal | None`` and no room to say how a number was arrived at, so
    an unsettled value poured into it reads as an established term."""
    fields = {
        "revolver.commitment": _field("rc", Decimal("500"), "needs_review"),
        "facility.governing_law": _field("gl", "New York", "confirmed", 60),
    }
    agreement = build_agreement(fields, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    crossing = revolver.to_fpml(agreement)
    withheld = revolver.withheld_from_fpml(agreement)

    assert "totalCommitmentAmount" not in crossing
    assert "needs_review" in withheld["totalCommitmentAmount"]
    assert crossing["governingLaw"] == "New York"
    # But the structure still has it, which is the whole point of the split.
    assert revolver.terms.commitment.value == Decimal("500")


def test_a_confirmed_absence_crosses_as_none():
    """``absent_from_document`` means the agreement was checked and has no
    such term, which is exactly what ``None`` reads as in FpML. That one is
    not a withholding."""
    fields = {
        "revolver.commitment": _field("rc", Decimal("500"), "confirmed"),
        "libor_floor_pct": _field("f", None, "absent_from_document"),
    }
    agreement = build_agreement(fields, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    crossing = revolver.to_fpml(agreement)
    assert "floorRate" in crossing
    assert crossing["floorRate"] is None


def test_the_projection_only_names_declared_fpml_elements():
    """A name that is not in the pinned schemas is an invented URI, which is
    the failure ``fpml()`` was written to refuse."""
    agreement = build_agreement({}, agreement_id="d")
    declared = set(_vendored()["elements"])
    for tranche in agreement.tranches:
        named = set(tranche.to_fpml(agreement)) | set(
            tranche.withheld_from_fpml(agreement)
        )
        assert named <= declared, f"undeclared: {sorted(named - declared)}"


# ---------------------------------------------------------------------------
# Covenants have a home now
# ---------------------------------------------------------------------------


def test_a_covenant_survives_into_the_output():
    """Twenty-seven registry fields have no FpML element anywhere, so the old
    export dropped every one. Leaving them out because the interchange format
    cannot carry them means the pipeline's best work never leaves."""
    fields = {
        "financial_covenant.level": _field("lvl", "3.50 to 1.00", "confirmed"),
        "excess_cash_flow.sweep_pct": _field("ecf", Decimal("50"), "confirmed", 20),
        "mfn_threshold_pct": _field("mfn", Decimal("0.50"), "confirmed", 40),
    }
    agreement = build_agreement(fields, agreement_id="d")

    assert agreement.covenants.financial, "the maintenance test should be built"
    assert agreement.covenants.financial[0].level.value == "3.50 to 1.00"
    assert agreement.covenants.excess_cash_flow_sweep_pct.value == Decimal("50")
    assert agreement.covenants.mfn_threshold_pct.value == Decimal("0.50")


def test_covenant_direction_is_not_guessed():
    """A coverage ratio is a floor and a leverage ratio is a ceiling, and the
    registry does not say which. Inferring it from the number would report a
    breach as compliance half the time."""
    fields = {"financial_covenant.level": _field("lvl", "1.00 to 1.00", "confirmed")}
    agreement = build_agreement(fields, agreement_id="d")

    assert agreement.covenants.financial[0].direction is None


def test_a_borrowing_base_appears_only_where_there_is_one():
    """Most deals have none, and an empty borrowing base in the output reads
    as an asset-based deal with every advance rate at zero."""
    assert build_agreement({}, agreement_id="d").borrowing_base is None

    fields = {
        "borrowing_base.advance_rate_accounts":
            _field("ar", Decimal("85"), "confirmed"),
    }
    with_base = build_agreement(fields, agreement_id="d")
    assert with_base.borrowing_base is not None
    assert with_base.borrowing_base.advance_rate_accounts.value == Decimal("85")


# ---------------------------------------------------------------------------
# Schedules: Trap 1 survives the move
# ---------------------------------------------------------------------------


def test_the_derived_bullet_is_never_stored():
    """Storing it would make the reconciliation self-satisfying, which is how
    Trap 1 hides. The rows and the printed bullet must stay independent."""
    schedules = TrancheSchedules(
        amortization=(
            ScheduleEntry(on=date(2027, 3, 31), amount=Decimal("10"), row_index=0),
            ScheduleEntry(on=date(2027, 6, 30), amount=Decimal("10"), row_index=1),
        ),
        original_principal=Asserted(value=Decimal("100"), status="confirmed"),
        stated_bullet_at_maturity=Asserted(value=Decimal("80"), status="confirmed"),
    )

    assert "derived_bullet" not in TrancheSchedules.model_fields
    assert schedules.derived_bullet == Decimal("80")
    assert schedules.bullet_reconciles() is True


def test_a_bullet_that_does_not_reconcile_says_so():
    schedules = TrancheSchedules(
        amortization=(
            ScheduleEntry(on=date(2027, 3, 31), amount=Decimal("10"), row_index=0),
        ),
        original_principal=Asserted(value=Decimal("100"), status="confirmed"),
        stated_bullet_at_maturity=Asserted(value=Decimal("80"), status="confirmed"),
    )
    assert schedules.bullet_reconciles() is False


def test_an_untestable_bullet_is_none_not_false():
    """No printed bullet is not a failed reconciliation."""
    schedules = TrancheSchedules(
        original_principal=Asserted(value=Decimal("100"), status="confirmed"),
    )
    assert schedules.bullet_reconciles() is None


# ---------------------------------------------------------------------------
# Provenance survives the flattening
# ---------------------------------------------------------------------------


def test_a_value_keeps_the_span_it_was_read_from():
    """A value that arrives without its citation cannot be checked, and an
    export nobody can check against the document is an opinion."""
    fields = {"revolver.commitment": _field("rc", Decimal("500"), "confirmed", 77)}
    agreement = build_agreement(fields, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    assert revolver.terms.commitment.spans
    assert revolver.terms.commitment.spans[0].start == 77
    assert revolver.terms.commitment.source_field == "revolver.commitment"


def test_a_field_absent_from_the_record_is_distinguishable_from_an_empty_one():
    """"Nobody asked" and "asked and found nothing" are different facts, and
    a model that returns None for both loses the difference."""
    agreement = build_agreement({}, agreement_id="d")
    revolver = next(t for t in agreement.tranches if t.tranche_id == "revolver")

    assert revolver.terms.commitment.note == "no such field in this record"


def test_unplaced_readings_reach_the_assembly_report():
    """``RecordedBackend`` has always been able to report a quote no chunk
    held, and nothing ever called it, so readings that silently did not count
    looked like readings the model never made."""
    agreement = build_agreement(
        {}, agreement_id="d", unplaced_readings=["commitment_fee_pct"]
    )
    assert agreement.assembly.unplaced_readings == ("commitment_fee_pct",)


def test_the_layered_backend_delegates_the_drift_report():
    """The pipeline looks for ``unplaced`` on the backend it was handed, and
    every recorded run hands it a wrapper."""

    class _Model:
        name = "model"

        def unplaced(self) -> list[str]:
            return ["closing_date"]

    assert LayeredBackend(model=_Model()).unplaced() == ["closing_date"]
    # No model tier, nothing to report -- and not an AttributeError.
    assert LayeredBackend().unplaced() == []
