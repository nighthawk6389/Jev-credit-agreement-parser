"""One field, several tranches: the value the document attributed to each.

WHAT THIS REPLACED
==================

The plan was twenty-one new registry entries -- seven economic fields times
three tranches -- so that a revolver and a term loan priced differently could
each report their own number. Two scans over the 100-document harvest rejected
it. Of the four pricing terms, on 100 documents:

    45  the definition carries no percentage at all
    30  one class, one rate -- mostly ``Floor``, genuinely one number
    21  one class, priced by rate type (SOFR vs ABR), which the registry
        already handles by naming the SOFR leg
    11  two or more classes AND two or more rates
     5  one class, several rates

and reading the eleven, most price the classes the *same* ("Revolving Loans and
Swingline Loans 0.25% 1.25% / Tranche A Term Loans and 2026 Incremental Term
Loans 0.25% 1.25%") or are not a rate at all (Latham's ``Applicable
Percentage`` is a pro-rata share formula). Five or six documents in a hundred
price their tranches differently.

And for the worst of those a tranche-prefixed field still could not answer,
because the axis is not the tranche: Essential Properties indexes Applicable
Margin by Credit Rating Level x facility x rate type, and a field named
``revolver.applicable_margin_pct`` has four candidate answers on it. Picking
one is the same arbitrary choice the deal-level field makes today.

So instead: read the attribution the document itself makes, and where it makes
none, decline out loud.

THE SAFETY PROPERTY
===================

A document that states each term once produces byte-identical output. The
deal-wide partition is reconciled exactly as it was, and is still
``variants[0]`` -- which is what every validator, the calibration fit and the
labelling harness read. Attribution only ever *adds* variants after it.
``test_a_document_with_no_attribution_is_untouched`` is that property.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.extract.definitions import (
    MAX_BODY_CHARS, MAX_DECLINED_BODY_CHARS, _per_tranche_values, _tranche_id,
    definition_candidates,
)
from credit_extract.extract.passes import Candidate
from credit_extract.extract.reconcile import reconcile
from credit_extract.models.agreement import Asserted
from credit_extract.models.assemble import (
    ELIGIBLE_KINDS, INHERITED, TRANCHES, build_agreement,
)
from credit_extract.models.core import ExtractedField, Span, Variant
from credit_extract.models.fpml_model import FIELD_REGISTRY

FLOOR = {"libor_floor_pct": FIELD_REGISTRY["libor_floor_pct"]}


def _candidate(value, tranche=None, start=100, pass_id="deterministic:definitions"):
    return Candidate(
        field="libor_floor_pct",
        value=Decimal(value) if value is not None else None,
        span=Span(start=start, end=start + 30, text="x" * 30),
        confidence=0.90,
        pass_id=pass_id,
        segmentation="definitional",
        applies_to=tranche,
    )


# ---------------------------------------------------------------------------
# Reading the attribution out of the definition
# ---------------------------------------------------------------------------


def test_iridium_prices_its_two_tranches_differently():
    """The real body, from O_iridium-communications-inc-irdm. This is the shape
    the whole exercise is for, and there are about six of it in a hundred."""
    body = (
        '" Floor " means a rate of interest equal to (i) with respect to Term '
        "Loans, 0.75% and (ii) with respect to Revolving Loans, 0.00%."
    )
    assert {k: str(v[0]) for k, v in _per_tranche_values(body, "percent").items()} == {
        "initial_term_loan": "0.75", "revolver": "0.00",
    }


def test_latham_prices_them_the_same_and_that_is_still_an_attribution():
    """Two tranches, one number. The reading is per tranche and the deal-level
    answer exists -- refusing it would throw away a value the document states
    plainly, twice."""
    body = (
        '"Floor" means (a) with respect to the Initial Term Loans, 0.00% per '
        "annum and (b) with respect to the Revolving Loans, 0.00% per annum."
    )
    found = _per_tranche_values(body, "percent")
    assert set(found) == {"initial_term_loan", "revolver"}
    assert {str(v[0]) for v in found.values()} == {"0.00"}


def test_a_rate_type_is_not_a_tranche():
    """Hillman: 'with respect to any Initial Term Loans ... (A) 2.00% for SOFR
    Loans and (B) 1.00% for ABR Loans'. One tranche under two conventions. If
    'for ABR Loans' counted as an attribution this would report a revolver."""
    body = (
        '" Applicable Rate " means, for any day, a percentage per annum equal '
        "to, with respect to any Initial Term Loans ( including, for the "
        "avoidance of doubt, the 2026 Incremental Term Loans) ( A) 2.00% for "
        "SOFR Loans and (B) 1.00% for ABR Loans."
    )
    assert _per_tranche_values(body, "percent") == {}


def test_a_pricing_grid_is_not_an_attribution():
    """Somnigroup prices six loan classes at two rate types in a table. A
    tranche id cannot answer it, so this must produce nothing rather than one
    cell of it."""
    body = (
        '" Applicable Margin ": (a) for each Type of Loan, other than '
        "Incremental Term Loans, the rate per annum set forth under the "
        "relevant column heading below: ABR Loans Eurocurrency Loans "
        "Revolving Loans 0.625% 1.625% Initial Term A Loans 0.625% 1.625% "
        "2025 Refinancing Term B Loans 1.25% 2.25%"
    )
    assert _per_tranche_values(body, "percent") == {}


def test_one_tranche_named_twice_at_two_prices_settles_nothing():
    """Sanmina prices the same class before and after an amendment date. The
    attribution is there and the axis underneath it is time, which no tranche
    id resolves."""
    body = (
        "with respect to the Term Loans, 2.00% per annum and, from and after "
        "the Amendment Effective Date, with respect to the Term Loans, 1.75%"
    )
    assert _per_tranche_values(body, "percent") == {}


def test_a_single_attribution_is_not_a_split():
    body = "with respect to the Revolving Loans, 0.50% per annum."
    assert _per_tranche_values(body, "percent") == {}


@pytest.mark.parametrize("named,expected", [
    ("Initial Term Loans", "initial_term_loan"),
    ("Term Loan Facility", "initial_term_loan"),
    ("Revolving Credit Loans", "revolver"),
    ("Revolving Facility", "revolver"),
    ("Delayed Draw Term A Loans", "delayed_draw"),
    ("Letters of Credit", "lc_sublimit"),
    ("Swingline Loans", "lc_sublimit"),
])
def test_a_loan_class_maps_to_a_tranche_the_assembler_builds(named, expected):
    assert _tranche_id(named) == expected
    assert expected in {spec.tranche_id for spec in TRANCHES}


# ---------------------------------------------------------------------------
# The tier emits one candidate per tranche
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, term, body, start=0):
        self.term = term
        self.body = body
        self.span = Span(start=start, end=start + max(1, len(body)), text=body)


class _Graph:
    def __init__(self, nodes):
        self.nodes = nodes

    def resolve(self, term):
        return term if term in self.nodes else None

    def get(self, term):
        return self.nodes.get(term)


class _Doc:
    text = "x" * 4000


def _graph(body, term="Floor"):
    return _Graph({term: _Node(term, body)})


def test_the_tier_emits_one_candidate_per_tranche():
    graph = _graph(
        "a rate of interest equal to (i) with respect to Term Loans, 0.75% "
        "and (ii) with respect to Revolving Loans, 0.00%."
    )
    found = definition_candidates(_Doc(), graph, [FIELD_REGISTRY["libor_floor_pct"]])

    assert {(c.applies_to, str(c.value)) for c in found} == {
        ("initial_term_loan", "0.75"), ("revolver", "0.00"),
    }
    assert all(c.pass_id == "deterministic:definitions" for c in found)
    assert all(c.span == graph.get("Floor").span for c in found), (
        "each cites the definition that attributes it"
    )


def test_a_grid_is_declined_out_loud_with_the_axis_named():
    """It was declined before this, and declined silently -- which reads
    downstream as 'no pass produced a candidate' and invites the negative-space
    validator to confirm a defined term absent."""
    graph = _graph(
        "a percentage per annum determined by reference to the Credit Rating "
        "Level as set forth below: Pricing Level I 0.675% II 0.725% III 0.800%",
        term="Applicable Margin",
    )
    found = definition_candidates(
        _Doc(), graph,
        [FIELD_REGISTRY["applicable_margin.eurodollar_top_level_pct"]],
    )

    assert len(found) == 1
    assert found[0].value is None, "a cell of a grid is not the term"
    assert found[0].qualifiers["unsettled_in_definition"] == (
        "0.675%, 0.725%, 0.800%"
    )
    assert found[0].qualifiers["indexed_by"] == "Credit Rating Level"


def test_a_body_too_long_to_read_is_still_long_enough_to_decline():
    """Essential Properties' Applicable Margin body is 3,796 characters, over
    ``MAX_BODY_CHARS``, so the tier used to skip it entirely -- and the biggest
    grids are the longest bodies. Declining to take a value from a long body is
    safe in a way taking one is not."""
    long_grid = (
        "determined by reference to the Credit Rating Level: 0.675% 0.750% "
        + "filler " * 400
    )
    assert MAX_BODY_CHARS < len(long_grid) < MAX_DECLINED_BODY_CHARS
    found = definition_candidates(
        _Doc(), _graph(long_grid, term="Applicable Margin"),
        [FIELD_REGISTRY["applicable_margin.eurodollar_top_level_pct"]],
    )

    assert len(found) == 1 and found[0].value is None


def test_a_body_past_the_decline_cap_is_skipped_entirely():
    """Past this the span stops being a citation and becomes a chunk wearing
    one, which is the defect F10 was opened for."""
    found = definition_candidates(
        _Doc(), _graph("0.675% 0.750% " + "filler " * 2000, term="Applicable Margin"),
        [FIELD_REGISTRY["applicable_margin.eurodollar_top_level_pct"]],
    )
    assert found == []


def test_a_definition_with_nothing_of_the_right_kind_stays_silent():
    """Declining out loud is for a definition that carries values and settles
    none. A definition that carries none at all -- a Closing Date that is an
    event -- has nothing to report."""
    graph = _graph(
        "the date on which the conditions precedent in Section 4.1 are "
        "satisfied or waived.",
        term="Closing Date",
    )
    assert definition_candidates(
        _Doc(), graph, [FIELD_REGISTRY["closing_date"]]
    ) == []


# ---------------------------------------------------------------------------
# Reconciliation: two tranches are two answers, not a disagreement
# ---------------------------------------------------------------------------


def test_tranches_priced_differently_are_variants_not_a_conflict():
    result = reconcile(
        [_candidate("0.75", "initial_term_loan"), _candidate("0.00", "revolver", 200)],
        specs=FLOOR,
    )
    field = result.fields["libor_floor_pct"]

    assert result.conflicts == [], "the document does not disagree with itself"
    assert [(v.applies_to, v.value) for v in field.variants] == [
        (None, None),
        ("initial_term_loan", Decimal("0.75")),
        ("revolver", Decimal("0.00")),
    ]


def test_the_deal_level_slot_declines_when_the_tranches_differ():
    """This is the whole safety argument. ``variants[0]`` is what every
    validator and the calibration fit read, so putting either tranche's number
    in it would be a silent error -- and the average of them would be worse."""
    field = reconcile(
        [_candidate("0.75", "initial_term_loan"), _candidate("0.00", "revolver", 200)],
        specs=FLOOR,
    ).fields["libor_floor_pct"]

    assert field.variants[0].value is None
    assert field.variants[0].status == "needs_review"
    assert "initial_term_loan=0.75" in field.variants[0].notes
    assert "revolver=0.00" in field.variants[0].notes


def test_tranches_priced_the_same_do_settle_the_deal_level_slot():
    field = reconcile(
        [_candidate("0.00", "initial_term_loan"), _candidate("0.00", "revolver", 200)],
        specs=FLOOR,
    ).fields["libor_floor_pct"]

    assert field.variants[0].value == Decimal("0.00")
    assert field.variants[0].status == "confirmed"
    assert field.variants[0].applies_to is None
    assert "the same for each of" in field.variants[0].notes


def test_a_deal_wide_reading_keeps_variant_zero_and_the_tranches_follow():
    field = reconcile(
        [
            _candidate("0.50", None, 50, pass_id="deterministic:rules"),
            _candidate("0.75", "initial_term_loan", 100),
            _candidate("0.00", "revolver", 200),
        ],
        specs=FLOOR,
    ).fields["libor_floor_pct"]

    assert field.variants[0].value == Decimal("0.50")
    assert field.variants[0].applies_to is None
    assert [v.applies_to for v in field.variants[1:]] == [
        "initial_term_loan", "revolver",
    ]
    assert "variant 0 is the deal-wide reading" in field.precedence_basis


def test_an_attributed_value_faces_the_same_support_floor():
    """Attribution says whose the number is. It says nothing about whether one
    pass finding it once is enough to present it as settled, and exempting
    these would be a second route to ``confirmed`` with a lower bar."""
    thin = Candidate(
        field="libor_floor_pct", value=Decimal("0.75"),
        span=Span(start=0, end=10, text="x" * 10), confidence=0.7,
        pass_id="model:pass1", segmentation="structural",
        applies_to="initial_term_loan",
    )
    other = _candidate("0.00", "revolver", 200)
    field = reconcile([thin, other], specs=FLOOR).fields["libor_floor_pct"]
    by_tranche = {v.applies_to: v for v in field.variants}

    assert by_tranche["initial_term_loan"].status == "needs_review"
    assert "single-pass discovery" in by_tranche["initial_term_loan"].notes
    assert by_tranche["revolver"].status == "confirmed", (
        "the deterministic definitions tier needs no corroboration"
    )


def test_attributed_candidates_with_no_value_do_not_print_an_empty_list():
    """Nothing emits this today -- the decline route is deal-wide on purpose --
    and falling through would read "the tranches differ ()" and mean nothing."""
    field = reconcile(
        [_candidate(None, "revolver"), _candidate(None, "initial_term_loan")],
        specs=FLOOR,
    ).fields["libor_floor_pct"]

    assert len(field.variants) == 1
    assert field.variants[0].value is None
    assert "produced no value for any of them" in field.variants[0].notes


def test_a_document_with_no_attribution_is_untouched():
    """The property that makes this safe to ship: 94 documents in 100 state each
    term once, and for them nothing about the record changes."""
    plain = [_candidate("0.50", None, 50), _candidate("0.50", None, 50)]
    field = reconcile(plain, specs=FLOOR).fields["libor_floor_pct"]

    assert len(field.variants) == 1
    assert field.variants[0].value == Decimal("0.50")
    assert field.variants[0].status == "confirmed"
    assert field.precedence_basis is None


# ---------------------------------------------------------------------------
# The assembler asks for its own tranche's reading
# ---------------------------------------------------------------------------


def _field_with(*variants: Variant) -> dict[str, ExtractedField]:
    return {"libor_floor_pct": ExtractedField[object](variants=list(variants))}


def _variant(value, tranche=None):
    return Variant[object](
        value=Decimal(value), status="confirmed", applies_to=tranche,
        spans=[Span(start=0, end=10, text="x" * 10)], extraction_confidence=0.9,
    )


def test_an_attributed_reading_is_used_and_stops_being_inherited():
    fields = _field_with(
        _variant("0.50"), _variant("0.75", "initial_term_loan"),
    )
    asserted = Asserted.from_field(
        fields, "libor_floor_pct", basis="deal-level: one floor field",
        for_tranche="initial_term_loan",
    )

    assert asserted.value == Decimal("0.75")
    assert asserted.basis == "", "the document said which tranche, so it is a reading"
    assert not asserted.inherited


def test_a_tranche_with_no_attributed_reading_still_inherits():
    fields = _field_with(_variant("0.50"), _variant("0.75", "initial_term_loan"))
    asserted = Asserted.from_field(
        fields, "libor_floor_pct", basis="deal-level: one floor field",
        for_tranche="revolver",
    )

    assert asserted.value == Decimal("0.50")
    assert asserted.inherited


def test_asking_for_no_tranche_reads_variant_zero_as_it_always_did():
    fields = _field_with(_variant("0.50"), _variant("0.75", "initial_term_loan"))
    assert Asserted.from_field(fields, "libor_floor_pct").value == Decimal("0.50")


# ---------------------------------------------------------------------------
# A fee only one kind of tranche can carry
# ---------------------------------------------------------------------------


def test_a_term_loan_is_not_offered_a_commitment_fee():
    """Not a weak reading -- an invented term. A term loan drawn in full at
    closing has no undrawn commitment to charge a fee on, and quoting it
    0.375% is a claim the document never made about it."""
    agreement = build_agreement({}, agreement_id="t")
    term = next(t for t in agreement.tranches if t.tranche_id == "initial_term_loan")

    assert term.terms.fees.commitment_fee_pct.value is None
    assert term.terms.fees.commitment_fee_pct.status == "needs_review"


def test_a_ticking_fee_belongs_to_the_delayed_draw_alone():
    """It accrues between signing and funding, which is what a delayed draw is.
    A revolver's undrawn balance pays the commitment fee instead."""
    agreement = build_agreement({}, agreement_id="t")
    offered = {
        t.tranche_id for t in agreement.tranches
        if "terms.fees.ticking_fee_pct" not in "".join(
            s for s in agreement.assembly.ineligible_slots
            if s.startswith(f"{t.tranche_id}.")
        )
    }
    assert offered == {"delayed_draw"}


def test_a_revolver_is_not_offered_an_amortization_schedule():
    agreement = build_agreement({}, agreement_id="t")
    ruled_out = set(agreement.assembly.ineligible_slots)
    assert any(
        s.startswith("revolver.schedules.amortization_quarterly_amount")
        for s in ruled_out
    )


def test_every_ruled_out_slot_says_why():
    agreement = build_agreement({}, agreement_id="t")
    assert agreement.assembly.ineligible_slots
    for entry in agreement.assembly.ineligible_slots:
        slot, _, reason = entry.partition(": ")
        assert len(reason) > 30, f"{slot} is ruled out without a reason"


def test_eligibility_names_slots_that_exist():
    """A typo here silently offers a fee to every tranche, which is the
    condition this table was added to fix."""
    assert set(ELIGIBLE_KINDS) <= set(INHERITED), (
        "an eligibility rule for a slot nothing fills does nothing"
    )
    kinds = {spec.kind for spec in TRANCHES}
    for slot, (allowed, _) in ELIGIBLE_KINDS.items():
        assert allowed <= kinds, f"{slot} names a tranche kind nothing builds"
        assert allowed, f"{slot} is offered to nothing at all"
