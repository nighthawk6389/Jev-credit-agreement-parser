"""FIBO term bindings for entities, roles and structural relationships.

Term URIs are resolved against the vendored snapshot of the EDM Council's
published ontologies (``scripts/vendor_standards.py``). ``fibo()`` raises on an
unknown CURIE, which is deliberate: it makes it impossible to ship an invented
term URI, and invented term URIs defeat the entire purpose of not designing a
schema from scratch.

Two corrections to the naive mapping fall out of using the real ontology:

* ``Borrower`` lives in FBC Debt (``fibo-fbc-dae-dbt:Borrower``), not in
  FND ParticipantsAndRoles -- ``fibo-fnd-pas-pas`` is the Clients module.
* FIBO publishes no class for syndicated-loan agency roles (administrative
  agent, collateral agent, arranger, syndication agent). Those are recorded as
  documented gaps with the nearest supertype rather than force-fitted, on the
  same principle that governs the ACTUS revolver mapping.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

_VENDORED = Path(__file__).parent / "vendored" / "fibo_terms.json"


@lru_cache(maxsize=1)
def _vendored() -> dict:
    if not _VENDORED.exists():  # pragma: no cover - vendoring is checked in
        raise RuntimeError(
            f"{_VENDORED} is missing; run `python scripts/vendor_standards.py`"
        )
    return json.loads(_VENDORED.read_text())


def fibo(curie: str) -> str:
    """Resolve a FIBO CURIE to its published URI. Raises on unknown terms."""
    terms = _vendored()["terms"]
    if curie not in terms:
        raise KeyError(
            f"{curie!r} is not in the vendored FIBO snapshot "
            f"({len(terms)} terms from {len(_vendored()['modules'])} modules). "
            "Add the module to scripts/vendor_standards.py rather than "
            "inventing a URI."
        )
    return terms[curie]["uri"]


def label(curie: str) -> str:
    return _vendored()["terms"][curie]["label"]


class TermBinding(BaseModel):
    """One field's binding to a published standard term."""

    standard: str                  # "fibo" | "fpml" | "actus"
    term: str | None               # CURIE or element name; None when unmapped
    uri: str | None = None
    label: str | None = None
    verified: bool = True          # resolved against a vendored source of truth
    gap_reason: str | None = None  # populated when term is None

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.term or f"<unmapped: {self.gap_reason}>"


def binding(curie: str) -> TermBinding:
    return TermBinding(
        standard="fibo", term=curie, uri=fibo(curie), label=label(curie)
    )


def gap(reason: str) -> TermBinding:
    return TermBinding(standard="fibo", term=None, verified=False, gap_reason=reason)


# ---------------------------------------------------------------------------
# Party roles
# ---------------------------------------------------------------------------

def party_roles() -> dict[str, TermBinding]:
    """Credit-agreement party role -> FIBO binding (or a documented gap)."""
    return {
        "borrower": binding("fibo-fbc-dae-dbt:Borrower"),
        "holdings": binding("fibo-fnd-agr-ctr:ContractParty"),
        "guarantor": binding("fibo-fbc-dae-gty:Guarantor"),
        "lender": binding("fibo-fbc-dae-dbt:Lender"),
        "creditor": binding("fibo-fbc-dae-dbt:Creditor"),
        "administrative_agent": gap(
            "FIBO publishes no syndicated-loan agency role class; nearest "
            "supertype is fibo-fbc-pas-fpas:ThirdPartyAgent, which does not "
            "carry the agency duties an administrative agent holds"
        ),
        "collateral_agent": gap(
            "no FIBO class for collateral agent; the collateral relationship "
            "is modelled on the instrument via fibo-fbc-dae-dbt:isCollateralizedBy"
        ),
        "arranger": gap("no FIBO class for lead arranger / bookrunner"),
        "syndication_agent": gap("no FIBO class for syndication agent"),
        "issuing_bank": gap(
            "no FIBO class for LC issuing bank; "
            "fibo-fbc-dae-gty:LetterOfCreditGuaranty models the instrument only"
        ),
    }


# ---------------------------------------------------------------------------
# Instrument / structural bindings
# ---------------------------------------------------------------------------

def instrument_terms() -> dict[str, TermBinding]:
    return {
        "credit_agreement": binding("fibo-fbc-dae-dbt:CreditAgreement"),
        "credit_facility": binding("fibo-fbc-dae-dbt:CreditFacility"),
        "committed_facility": binding("fibo-fbc-dae-dbt:CommittedCreditFacility"),
        "sub_facility": binding("fibo-fbc-dae-dbt:SubFacility"),
        "loan": binding("fibo-loan-ln-ln:Loan"),
        "secured_loan": binding("fibo-loan-ln-ln:SecuredLoan"),
        "collateralized_loan": binding("fibo-loan-ln-ln:CollateralizedLoan"),
        "guaranteed_loan": binding("fibo-loan-ln-ln:GuaranteedLoan"),
        "payment_schedule": binding("fibo-loan-ln-ln:LoanPaymentSchedule"),
        "principal_repayment_terms": binding(
            "fibo-fbc-dae-dbt:PrincipalRepaymentTerms"
        ),
        "interest_payment_terms": binding("fibo-fbc-dae-dbt:InterestPaymentTerms"),
        "floating_rate": binding("fibo-fbc-dae-dbt:FloatingInterestRate"),
        "collateral": binding("fibo-fbc-dae-dbt:Collateral"),
        "lien_position": binding("fibo-loan-ln-ln:LenderLienPosition"),
        "security_agreement": binding("fibo-fbc-dae-dbt:SecurityAgreement"),
        "maturity_date": binding("fibo-fbc-dae-dbt:hasMaturityDate"),
        "principal": binding("fibo-fbc-dae-dbt:hasPrincipal"),
        "outstanding_amount": binding("fibo-fbc-dae-dbt:hasOutstandingAmount"),
        "interest_rate": binding("fibo-fbc-dae-dbt:hasInterestRate"),
        "commitment": binding("fibo-fnd-agr-agr:Commitment"),
    }


def gaps() -> dict[str, str]:
    """Every role we could not bind, for the document-level report."""
    return {
        name: b.gap_reason
        for name, b in party_roles().items()
        if b.term is None and b.gap_reason
    }


def provenance() -> dict:
    v = _vendored()
    return {
        "standard": "FIBO",
        "publisher": "EDM Council",
        "license": v["license"],
        "source": v["source"],
        "modules": v["modules"],
        "terms_available": len(v["terms"]),
    }
