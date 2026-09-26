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

from .agreement import FPML_BINDINGS, CreditAgreement
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
    #: Values this record settles that FpML has no element for -- an agent,
    #: a guarantor, a ticking fee. A statement about the format, not about
    #: the run, and kept apart from ``withheld`` for that reason: a reader
    #: told a confirmed agent name was "withheld" would conclude the agent
    #: was unknown.
    not_expressible: dict[str, str] = Field(default_factory=dict)

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


def _set_attr(model: Any, path: str, value: Any) -> None:
    """Assign to a dotted attribute path on an already-built model."""
    head, _, tail = path.rpartition(".")
    target = model
    for part in head.split(".") if head else []:
        target = getattr(target, part)
    setattr(target, tail, value)


def facilities_from(agreement: CreditAgreement) -> list[FacilityExport]:
    """Project every established tranche into the typed FpML model.

    Driven by ``agreement.FPML_BINDINGS``, the single declaration of
    what crosses. Before this there were two: ``build_facilities`` addressed
    26 field paths of its own and ``Tranche.to_fpml`` projected 13 element
    names, both were populated by every run, and nothing reconciled them -- so
    a consumer got a different FpML view of the same document depending on
    which one it read. Coverage cannot diverge from a table.

    A tranche appears only where its commitment is settled. That is a stricter
    test than "some field mentioned it", and deliberately so: the export is
    the artefact somebody downstream would act on, and a facility that exists
    in it because one unresolved candidate named a number is a deal term
    invented by a threshold. A tranche confirmed *absent* is likewise not
    exported as an empty one -- there is no such tranche, and the right export
    says nothing rather than saying zero.
    """
    out: list[FacilityExport] = []
    for tranche in agreement.tranches:
        commitment = tranche.terms.commitment
        if not commitment.settled or commitment.value is None:
            continue

        facility = Facility(
            facility_id=tranche.tranche_id,
            facility_type=_FACILITY_TYPE.get(tranche.kind, "term_loan"),
            currency=tranche.reference.currency,
            sublimit_of=tranche.sublimit_of,
        )
        crossing = tranche.to_fpml(agreement)
        provenance: dict[str, str] = {}
        for element, source, attr in FPML_BINDINGS:
            if element not in crossing or attr is None:
                continue
            _set_attr(facility, attr, crossing[element])
            if crossing[element] is not None:
                asserted = tranche._resolve(source, agreement)
                provenance[element] = asserted.source_field or source

        out.append(FacilityExport(
            facility=facility,
            withheld=tranche.withheld_from_fpml(agreement),
            provenance=provenance,
            not_expressible=tranche.not_expressible_in_fpml(agreement),
        ))
    return out


def build_facilities(
    fields: dict[str, ExtractedField],
) -> list[FacilityExport]:
    """Assemble the record, then project it. Kept for callers holding fields.

    The pipeline already builds a ``CreditAgreement`` and calls
    ``facilities_from`` with it directly; assembling a second one here would
    be waste and, worse, something that could drift from the first.
    """
    from .assemble import build_agreement

    return facilities_from(build_agreement(fields, agreement_id="fields"))


#: Tranche kind -> the FpML substitution element the typed model expects.
_FACILITY_TYPE: dict[str, Any] = {
    "revolver": "revolver",
    "term_loan": "term_loan",
    "delayed_draw_term_loan": "delayed_draw_term_loan",
    "letter_of_credit": "letter_of_credit",
    "swingline": "revolver",
    "incremental": "term_loan",
    "unknown": "term_loan",
}


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
