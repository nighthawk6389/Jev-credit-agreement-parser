"""Assemble the extracted fields into the FpML-shaped facility model.

Forty FpML element names in this repository are verified against pinned
schemas. ``Facility``, ``AccrualTerms``, ``CommitmentTerms``, ``PikTerms`` and
``CreditRating`` are all defined in ``fpml_model.py``, with the element each
attribute stands for named in a comment beside it. And until this module
existed nothing anywhere constructed one: the pipeline emitted a flat
dictionary of fields and stopped, so **zero** of those forty elements were
populated by any run the project had ever made. The standards mapping was a
promise about a shape nothing was ever poured into.

Pouring it in is not the hard part. The hard part is which fields are allowed
through, and that is the same question this whole repository is about.

    A FACILITY IS AN ASSERTION. A flat field record carries a status beside
    every value, so a reader who takes ``libor_floor_pct`` at face value has
    been told, in the same object, that it is only ``needs_review``. A
    ``Facility`` carries no such thing: its ``accrual.floor_pct`` is a
    ``Decimal | None`` and a consumer has nothing to consult. Exporting an
    unsettled value into it launders a question into an answer, and exporting
    the *absence* of an unsettled value is worse -- ``None`` in this model
    reads as "this facility has no floor", which is a term, confidently
    stated, that nobody established.

So only settled fields cross, and the three ways of being settled are not the
same and do not collapse to ``None``:

``confirmed``
    The value crosses.

``absent_from_document``
    ``None`` crosses, and means what the model says it means: the agreement
    was checked and has no such term.

``external_reference``
    Nothing crosses. The value exists and is in a document nobody has. FpML
    has no element for "this is real and lives elsewhere", so writing ``None``
    would report a deal with no floor where the truth is a floor in a fee
    letter. It is withheld, and the reason travels with the export.

Everything withheld is named in :class:`FacilityExport.withheld`, so the count
that matters -- how much of the forty a run actually earned -- is a fact about
the run rather than a claim about the mapping.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .core import ExtractedField
from .fpml_model import (
    AccrualTerms, CommitmentTerms, CreditRating, Facility,
    FacilityClassification, FacilityType, FeeTerms, PartyReferences, PikTerms,
)

#: A field in one of these states has been decided, and may cross into a model
#: that has no room to say how it was decided. ``needs_review`` and
#: ``conflicted`` are questions, and a question in this shape reads as an
#: answer.
EXPORTABLE = frozenset({"confirmed", "absent_from_document"})

#: Settled, and still not exportable: the value is real and is somewhere else.
WITHHELD_WITH_REASON = {
    "external_reference": "the value is in a document this agreement points at",
    "not_applicable_to_archetype": "this deal kind has no such term",
    "needs_review": "not settled: below the calibrated threshold",
    "conflicted": "not settled: the passes disagreed and nothing resolved it",
}


class FacilityExport(BaseModel):
    """One facility, plus an account of everything that did not go into it."""

    facility: Facility
    #: element path -> why it is empty. Every entry is a place a consumer
    #: might otherwise read ``None`` as a term.
    withheld: dict[str, str] = Field(default_factory=dict)
    #: element path -> the field it came from, so a value in the export can be
    #: taken back to its span in the record.
    provenance: dict[str, str] = Field(default_factory=dict)

    @property
    def populated(self) -> int:
        return len(self.provenance)


def _settled(
    fields: dict[str, ExtractedField], name: str
) -> tuple[Any, str | None]:
    """``(value, reason_it_was_withheld)``. Exactly one is ever meaningful."""
    field = fields.get(name)
    if field is None:
        return None, "no such field in this record"
    if field.status in EXPORTABLE:
        return field.value, None
    return None, WITHHELD_WITH_REASON.get(field.status, f"status {field.status}")


class _Builder:
    """Collects values, and the reason for every one that does not arrive."""

    def __init__(self, fields: dict[str, ExtractedField], prefix: str) -> None:
        self.fields = fields
        self.prefix = prefix
        self.withheld: dict[str, str] = {}
        self.provenance: dict[str, str] = {}

    def take(self, element: str, name: str) -> Any:
        value, reason = _settled(self.fields, name)
        if reason is not None:
            self.withheld[element] = f"{name}: {reason}"
            return None
        if value is not None:
            self.provenance[element] = name
        return value


def _facility(
    fields: dict[str, ExtractedField],
    facility_id: str,
    facility_type: FacilityType,
    commitment_field: str,
    maturity_field: str,
) -> FacilityExport:
    b = _Builder(fields, facility_id)
    facility = Facility(
        facility_id=facility_id,
        facility_type=facility_type,
        commitment_amount=b.take("commitment_amount", commitment_field),
        effective_date=b.take("effective_date", "closing_date"),
        maturity_date=b.take("maturity_date", maturity_field),
        accrual=AccrualTerms(
            spread_pct=b.take(
                "accrual.spread_pct",
                "applicable_margin.eurodollar_top_level_pct",
            ),
            credit_spread_adjustment_pct=b.take(
                "accrual.credit_spread_adjustment_pct",
                "accrual.credit_spread_adjustment_pct",
            ),
            floor_pct=b.take("accrual.floor_pct", "libor_floor_pct"),
            cap_pct=b.take("accrual.cap_pct", "accrual.cap_pct"),
        ),
        commitment=CommitmentTerms(
            must_draw_by_date=b.take(
                "commitment.must_draw_by_date", "delayed_draw.must_draw_by_date"
            ),
            refusal_allowed=b.take(
                "commitment.refusal_allowed", "delayed_draw.refusal_allowed"
            ),
        ),
        classification=FacilityClassification(
            feature=b.take("classification.feature", "facility.feature"),
            lien=b.take("classification.lien", "facility.lien"),
            seniority=b.take("classification.seniority", "facility.seniority"),
            governing_law=b.take(
                "classification.governing_law", "facility.governing_law"
            ),
            multi_currency=b.take(
                "classification.multi_currency", "facility.multi_currency"
            ),
        ),
        parties=PartyReferences(
            borrower=b.take("parties.borrower", "borrower.legal_name"),
            agent=b.take("parties.agent", "administrative_agent.legal_name"),
            guarantors=[
                g for g in [b.take("parties.guarantors", "guarantor.legal_name")]
                if g
            ],
        ),
        fees=FeeTerms(
            commitment_fee_pct=b.take("fees.commitment_fee_pct", "commitment_fee_pct"),
            ticking_fee_pct=b.take("fees.ticking_fee_pct", "ticking_fee_pct"),
            fronting_fee_pct=b.take("fees.fronting_fee_pct", "fronting_fee_pct"),
        ),
    )
    rating = CreditRating(
        agency=b.take("ratings.agency", "rating.agency"),
        rating=b.take("ratings.rating", "rating.value"),
        credit_quality=b.take("ratings.credit_quality", "rating.credit_quality"),
        industry_classification=b.take(
            "ratings.industry_classification", "borrower.industry_classification"
        ),
    )
    if rating.model_dump(exclude_none=True):
        facility.ratings.append(rating)

    pik = PikTerms(
        rate_pct=b.take("pik.rate_pct", "pik.rate_pct"),
        spread_pct=b.take("pik.spread_pct", "pik.spread_pct"),
    )
    if pik.model_dump(exclude_none=True):
        facility.pik = pik

    return FacilityExport(
        facility=facility, withheld=b.withheld, provenance=b.provenance
    )


#: The tranches the field registry can describe, and the fields that carry
#: each one's size and maturity. A facility whose commitment field is neither
#: confirmed nor confirmed-absent is not exported at all: a tranche with no
#: settled size is a tranche nobody established the existence of.
_TRANCHES: tuple[tuple[str, FacilityType, str, str], ...] = (
    ("revolver", "revolver", "revolver.commitment", "revolver.maturity_date"),
    ("initial_term_loan", "term_loan",
     "initial_term_loan.commitment", "initial_term_loan.maturity_date"),
    ("delayed_draw", "delayed_draw_term_loan",
     "delayed_draw.commitment", "initial_term_loan.maturity_date"),
)


def build_facilities(
    fields: dict[str, ExtractedField],
) -> list[FacilityExport]:
    """Every tranche this record establishes, in FpML shape.

    A tranche appears only where its commitment is settled. That is a stricter
    test than "some field mentioned it", and deliberately so: the export is
    the artefact somebody downstream would act on, and a facility that exists
    in it because one unresolved candidate named a number is a deal term
    invented by a threshold.
    """
    out: list[FacilityExport] = []
    for facility_id, kind, commitment, maturity in _TRANCHES:
        field = fields.get(commitment)
        if field is None or field.status not in EXPORTABLE:
            continue
        if field.value is None and field.status != "absent_from_document":
            continue
        if field.status == "absent_from_document":
            # Confirmed absent is a fact about the deal -- there is no such
            # tranche -- and the right export is no facility, not an empty one.
            continue
        out.append(_facility(fields, facility_id, kind, commitment, maturity))
    return out


def export_summary(exports: list[FacilityExport]) -> dict[str, Any]:
    """What the export earned, in the terms the blind-spot register uses."""
    populated = sum(e.populated for e in exports)
    withheld: dict[str, int] = {}
    for export in exports:
        for reason in export.withheld.values():
            key = reason.split(": ", 1)[-1]
            withheld[key] = withheld.get(key, 0) + 1
    return {
        "facilities": len(exports),
        "elements_populated": populated,
        "elements_withheld": sum(withheld.values()),
        "withheld_by_reason": dict(sorted(withheld.items())),
    }
