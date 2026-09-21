"""The export, and the three ways a field can be empty in it.

Forty FpML element names in this repository are verified against pinned
schemas, and until this module existed nothing constructed a ``Facility``, so
none of them was populated by any run. The tests that matter here are not
about whether values arrive; they are about which values are refused, because
a ``Facility`` has nowhere to record that a number was only a guess.
"""

from __future__ import annotations

from decimal import Decimal

from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.export import (
    EXPORTABLE, build_facilities, export_summary,
)


def _field(value, status, criticality=4, external_document=None):
    spans = [Span(start=0, end=5, text="hello")] if value is not None else []
    return ExtractedField.single(
        value=value, spans=spans, status=status,
        field_class="economic_terms", criticality=criticality,
        external_document=external_document,
    )


def _base(**overrides):
    fields = {
        "revolver.commitment": _field(Decimal("100000000"), "confirmed"),
        "libor_floor_pct": _field(Decimal("1.0"), "confirmed"),
        "facility.governing_law": _field("New York", "confirmed"),
    }
    fields.update(overrides)
    return fields


def test_a_settled_value_crosses():
    export = build_facilities(_base())[0]
    assert export.facility.commitment_amount == Decimal("100000000")
    assert export.facility.accrual.floor_pct == Decimal("1.0")
    assert export.provenance["accrual.floor_pct"] == "libor_floor_pct"


def test_an_unsettled_value_does_not_cross_and_says_why():
    """The flat record carries a status beside every value. A Facility does
    not: its floor_pct is a Decimal or None and a consumer has nothing to
    consult, so an unsettled number in it is a question laundered into an
    answer."""
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(Decimal("1.0"), "needs_review")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "not settled" in export.withheld["accrual.floor_pct"]
    assert "accrual.floor_pct" not in export.provenance


def test_a_conflicted_value_does_not_cross():
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(Decimal("1.0"), "conflicted")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "passes disagreed" in export.withheld["accrual.floor_pct"]


def test_confirmed_absent_crosses_as_absent():
    """None here is a term: the agreement was checked and has no floor."""
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(None, "absent_from_document")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "accrual.floor_pct" not in export.withheld, (
        "confirmed absent is an answer, not a withholding"
    )


def test_an_external_reference_is_withheld_rather_than_exported_as_absent():
    """The one that would be a silent error. The value is real and is in a
    document nobody has; FpML has no element for that, so None would report a
    facility with no floor where the truth is a floor in a fee letter."""
    field = _field(None, "external_reference", external_document="Fee Letter")
    export = build_facilities(_base(**{"libor_floor_pct": field}))[0]
    assert export.facility.accrual.floor_pct is None
    assert "in a document" in export.withheld["accrual.floor_pct"], (
        "absent and elsewhere are different facts and the model cannot tell "
        "them apart, so the export has to"
    )


def test_a_tranche_with_no_settled_size_is_not_exported():
    """A facility that exists because one unresolved candidate named a number
    is a deal term invented by a threshold."""
    assert build_facilities(
        _base(**{"revolver.commitment": _field(Decimal("1"), "needs_review")})
    ) == []
    assert build_facilities(
        _base(**{"revolver.commitment": _field(None, "absent_from_document")})
    ) == [], "confirmed absent means there is no such tranche, not an empty one"


def test_every_exportable_status_is_one_a_reader_could_act_on():
    assert EXPORTABLE == {"confirmed", "absent_from_document"}


def test_the_summary_counts_what_the_run_earned():
    summary = export_summary(build_facilities(_base()))
    assert summary["facilities"] == 1
    assert summary["elements_populated"] >= 3
    assert summary["elements_withheld"] > 0
    assert summary["withheld_by_reason"]
