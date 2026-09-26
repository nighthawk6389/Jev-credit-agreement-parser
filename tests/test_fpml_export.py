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
    assert export.provenance["floorRate"] == "libor_floor_pct"


def test_an_unsettled_value_does_not_cross_and_says_why():
    """The flat record carries a status beside every value. A Facility does
    not: its floor_pct is a Decimal or None and a consumer has nothing to
    consult, so an unsettled number in it is a question laundered into an
    answer."""
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(Decimal("1.0"), "needs_review")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "not settled" in export.withheld["floorRate"]
    assert "floorRate" not in export.provenance


def test_a_conflicted_value_does_not_cross():
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(Decimal("1.0"), "conflicted")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "passes disagreed" in export.withheld["floorRate"]


def test_confirmed_absent_crosses_as_absent():
    """None here is a term: the agreement was checked and has no floor."""
    export = build_facilities(
        _base(**{"libor_floor_pct": _field(None, "absent_from_document")})
    )[0]
    assert export.facility.accrual.floor_pct is None
    assert "floorRate" not in export.withheld, (
        "confirmed absent is an answer, not a withholding"
    )


def test_an_external_reference_is_withheld_rather_than_exported_as_absent():
    """The one that would be a silent error. The value is real and is in a
    document nobody has; FpML has no element for that, so None would report a
    facility with no floor where the truth is a floor in a fee letter."""
    field = _field(None, "external_reference", external_document="Fee Letter")
    export = build_facilities(_base(**{"libor_floor_pct": field}))[0]
    assert export.facility.accrual.floor_pct is None
    assert "in a document" in export.withheld["floorRate"], (
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


# ---------------------------------------------------------------------------
# One declaration of what crosses
# ---------------------------------------------------------------------------


def test_the_two_export_paths_agree_because_they_share_a_table():
    """``build_facilities`` used to address 26 field paths of its own while
    ``Tranche.to_fpml`` projected 13 element names. Both were populated by
    every run and nothing reconciled them, so a consumer got a different FpML
    view of the same document depending on which it read."""
    from credit_extract.models.agreement import FPML_BINDINGS
    from credit_extract.models.assemble import build_agreement
    from credit_extract.models.export import facilities_from

    agreement = build_agreement(_base(), agreement_id="d")
    tranche = next(t for t in agreement.tranches if t.tranche_id == "revolver")
    export = facilities_from(agreement)[0]

    declared = {element for element, _s, _a in FPML_BINDINGS}
    projected = set(tranche.to_fpml(agreement)) | set(
        tranche.withheld_from_fpml(agreement)
    )
    accounted = set(export.provenance) | set(export.withheld)

    assert projected == declared, "the projection is exactly the table"
    assert accounted <= declared, "the typed export names nothing extra"


def test_every_projected_element_is_declared_in_the_pinned_schemas():
    from credit_extract.models.agreement import FPML_BINDINGS
    from credit_extract.models.fpml_model import _vendored

    declared = set(_vendored()["elements"])
    named = {element for element, _s, _a in FPML_BINDINGS}
    assert named <= declared, f"invented: {sorted(named - declared)}"


def test_what_fpml_cannot_carry_is_named_separately_from_what_is_unsettled():
    """A reader told a confirmed agent name was "withheld" would conclude the
    agent was unknown. FpML 5.x simply declares no agent element."""
    from credit_extract.models.export import build_facilities

    fields = _base(**{
        "administrative_agent.legal_name": _field("Wells Fargo", "confirmed"),
        "ticking_fee_pct": _field(Decimal("0.25"), "confirmed"),
    })
    export = build_facilities(fields)[0]

    assert "metadata.parties.administrative_agent" in export.not_expressible
    assert "no agent element" in export.not_expressible[
        "metadata.parties.administrative_agent"
    ]
    assert not any("agent" in k for k in export.withheld), (
        "not a run failure -- the format has nowhere to put it"
    )


def test_an_unsettled_value_is_not_also_reported_as_inexpressible():
    """Reporting it twice would overstate what the format costs."""
    from credit_extract.models.export import build_facilities

    fields = _base(**{
        "administrative_agent.legal_name": _field("Wells Fargo", "needs_review"),
    })
    export = build_facilities(fields)[0]
    assert "metadata.parties.administrative_agent" not in export.not_expressible


def test_every_path_in_both_tables_actually_resolves():
    """The bug this caught: NOT_IN_FPML named ``parties.agent`` and the model's
    field is ``parties.administrative_agent``, so the entry silently matched
    nothing and a settled agent name was reported as neither carried nor
    inexpressible. A table of dotted paths needs a test that they exist."""
    from credit_extract.models.agreement import FPML_BINDINGS, NOT_IN_FPML
    from credit_extract.models.assemble import build_agreement

    agreement = build_agreement(_base(), agreement_id="d")
    tranche = agreement.tranches[0]

    for element, source, _attr in FPML_BINDINGS:
        if source.startswith("derived:"):
            continue
        kind, _, path = source.partition(":")
        target = tranche if kind == "tranche" else agreement
        for part in path.split("."):
            target = getattr(target, part, None)
            assert target is not None, f"{element}: {source} breaks at {part!r}"

    for source, _why in NOT_IN_FPML:
        kind, _, path = source.partition(":")
        target = tranche if kind == "tranche" else agreement
        for part in path.split("."):
            nxt = getattr(target, part, None)
            assert nxt is not None or hasattr(target, part), (
                f"{source} breaks at {part!r}"
            )
            target = nxt
