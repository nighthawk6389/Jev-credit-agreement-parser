"""ACTUS contract-type mapping and a Python reimplementation of LAM / LAX.

This is the module that converts "extraction" into "extraction that can be
falsified". A correctly extracted term loan is executable: generate the payment
schedule from the extracted terms and diff it, cell by cell, against the table
the document prints. Trap 1 -- a table with four duplicated rows, every one of
which parses cleanly -- is caught here and in the invariant checker, and
nowhere else.

Only the two contract types this pipeline needs are implemented (LAM and its
exotic variant LAX). The reference Java ``actus-core`` remains authoritative;
``ACTUS_REFERENCE`` records where it lives.

The revolver is deliberately left unmapped. ACTUS has no clean fit -- CLM (Call
Money) is closest and is still wrong, because a revolver's repeated draw and
repay against a commitment is not a called loan. A documented gap beats a wrong
mapping, so ``actus_mapping`` comes back ``None`` with a reason.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_VENDORED = Path(__file__).parent / "vendored" / "actus_dictionary.json"

ACTUS_REFERENCE = "https://github.com/actusfrf/actus-core"


@lru_cache(maxsize=1)
def _vendored() -> dict:
    if not _VENDORED.exists():  # pragma: no cover - vendoring is checked in
        raise RuntimeError(
            f"{_VENDORED} is missing; run `python scripts/vendor_standards.py`"
        )
    return json.loads(_VENDORED.read_text())


def actus_term(identifier: str) -> dict:
    """Resolve an ACTUS term. Raises on anything not in the dictionary."""
    terms = _vendored()["terms"]
    if identifier not in terms:
        raise KeyError(
            f"{identifier!r} is not an ACTUS dictionary term "
            f"(v{_vendored()['version'].get('Version')})"
        )
    return terms[identifier]


def acronym(identifier: str) -> str:
    return actus_term(identifier)["acronym"]


def contract_type(key: str) -> dict:
    types = _vendored()["contract_types"]
    if key not in types:
        raise KeyError(f"{key!r} is not an ACTUS contract type")
    return types[key]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

FacilityKind = Literal[
    "term_loan", "delayed_draw_term_loan", "revolver", "letter_of_credit"
]


class ActusMapping(BaseModel):
    """Which ACTUS contract type a facility maps to -- or why none does."""

    facility_kind: str
    contract_type: str | None            # "LAX", "LAM", ... or None
    contract_type_name: str | None = None
    rationale: str
    executable: bool = False
    #: ACTUS term identifiers this mapping populates, for the output trace.
    terms_used: dict[str, str] = Field(default_factory=dict)


def map_facility(kind: FacilityKind, has_level_amortization: bool = False) -> ActusMapping:
    """Map a facility to an ACTUS contract type, or record a documented gap."""
    if kind in ("term_loan", "delayed_draw_term_loan"):
        # A 1%/yr-plus-bullet structure is not a pure linear amortizer: the
        # final payment is an order of magnitude larger than the periodic
        # ones. LAX carries an explicit array of redemption amounts, so it is
        # the honest mapping even though LAM looks superficially right.
        use_lam = has_level_amortization and kind == "term_loan"
        key = "linearAmortizer" if use_lam else "exoticLinearAmortizer"
        spec = contract_type(key)
        rationale = (
            "level amortization to a zero balance at maturity maps cleanly to LAM"
            if use_lam
            else "amortization is a small periodic percentage plus a bullet at "
                 "maturity; LAX carries the arbitrary redemption array that "
                 "structure requires, LAM would misstate the final payment"
        )
        if kind == "delayed_draw_term_loan":
            rationale += "; deferred draws are modelled as later principal draws"
        return ActusMapping(
            facility_kind=kind,
            contract_type=spec["acronym"],
            contract_type_name=spec["name"],
            rationale=rationale,
            executable=True,
            terms_used={
                t: acronym(t)
                for t in (
                    "contractType", "statusDate", "initialExchangeDate",
                    "maturityDate", "notionalPrincipal", "nominalInterestRate",
                    "currency", "dayCountConvention",
                    "arrayCycleAnchorDateOfPrincipalRedemption",
                    "arrayNextPrincipalRedemptionPayment",
                    "cycleOfInterestPayment",
                )
            },
        )
    if kind == "revolver":
        return ActusMapping(
            facility_kind=kind,
            contract_type=None,
            rationale=(
                "ACTUS has no clean fit for a revolving credit facility. CLM "
                "(Call Money) is the closest contract type and is still wrong: "
                "CLM models a loan rolled over until called, not repeated draws "
                "and repayments against a commitment with an unused-line fee. "
                "Recorded as a gap rather than force-fitted."
            ),
            executable=False,
        )
    return ActusMapping(
        facility_kind=kind,
        contract_type=None,
        rationale=(
            "letters of credit are contingent obligations with no scheduled "
            "cashflow until drawn; no ACTUS contract type applies pre-drawing"
        ),
        executable=False,
    )


# ---------------------------------------------------------------------------
# Contract terms and the state-transition engine
# ---------------------------------------------------------------------------

DayCount = Literal["30E360", "A360", "A365"]


class ActusContract(BaseModel):
    """LAM/LAX terms, named with ACTUS dictionary identifiers."""

    contractType: str                                   # CT
    statusDate: date                                    # SD
    initialExchangeDate: date                           # IED
    maturityDate: date                                  # MD
    notionalPrincipal: Decimal                          # NT
    nominalInterestRate: Decimal = Decimal(0)           # IPNR (as a decimal)
    currency: str = "USD"                               # CUR
    dayCountConvention: DayCount = "A360"               # IPDC
    #: LAX redemption array: (date, amount) pairs. ARPRANXj / ARPRNXTj.
    arrayCycleAnchorDateOfPrincipalRedemption: list[date] = Field(
        default_factory=list
    )
    arrayNextPrincipalRedemptionPayment: list[Decimal] = Field(default_factory=list)
    #: LAM constant redemption. PRNXT / PRCL.
    nextPrincipalRedemptionPayment: Decimal | None = None
    cycleOfPrincipalRedemption: str | None = None       # e.g. "P3ML1"

    def redemptions(self) -> list[tuple[date, Decimal]]:
        dates = self.arrayCycleAnchorDateOfPrincipalRedemption
        amounts = self.arrayNextPrincipalRedemptionPayment
        if dates and amounts:
            if len(dates) != len(amounts):
                raise ValueError(
                    "ARPRANXj and ARPRNXTj must be the same length; got "
                    f"{len(dates)} dates and {len(amounts)} amounts"
                )
            return list(zip(dates, amounts))
        if self.nextPrincipalRedemptionPayment is not None:
            raise ValueError(
                "LAM cycle expansion needs cycleOfPrincipalRedemption; supply "
                "the redemption array instead"
            )
        return []


class CashflowEvent(BaseModel):
    """One ACTUS event. Event types are the published four-letter codes."""

    event_date: date
    event_type: Literal["IED", "PR", "IP", "MD"]
    principal: Decimal = Decimal(0)
    interest: Decimal = Decimal(0)
    notional_after: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.principal + self.interest


def year_fraction(start: date, end: date, convention: DayCount) -> Decimal:
    if convention == "A360":
        return Decimal((end - start).days) / Decimal(360)
    if convention == "A365":
        return Decimal((end - start).days) / Decimal(365)
    d1, d2 = min(start.day, 30), min(end.day, 30)
    days = 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)
    return Decimal(days) / Decimal(360)


def generate_schedule(contract: ActusContract) -> list[CashflowEvent]:
    """Run the contract forward and emit its full event sequence.

    Implements the LAM/LAX principal and interest transitions: IED opens the
    notional, each PR reduces it, interest accrues on the balance between
    events, and MD repays whatever remains.
    """
    redemptions = sorted(contract.redemptions(), key=lambda r: r[0])
    notional = contract.notionalPrincipal
    events = [
        CashflowEvent(
            event_date=contract.initialExchangeDate,
            event_type="IED",
            principal=-notional,
            notional_after=notional,
        )
    ]
    cursor = contract.initialExchangeDate
    for when, amount in redemptions:
        if when > contract.maturityDate:
            raise ValueError(
                f"redemption {when} falls after maturity {contract.maturityDate}"
            )
        interest = (
            notional
            * contract.nominalInterestRate
            * year_fraction(cursor, when, contract.dayCountConvention)
        )
        paid = min(amount, notional)
        notional -= paid
        events.append(
            CashflowEvent(
                event_date=when,
                event_type="PR",
                principal=paid,
                interest=interest.quantize(Decimal("0.01")),
                notional_after=notional,
            )
        )
        cursor = when
    interest = (
        notional
        * contract.nominalInterestRate
        * year_fraction(cursor, contract.maturityDate, contract.dayCountConvention)
    )
    events.append(
        CashflowEvent(
            event_date=contract.maturityDate,
            event_type="MD",
            principal=notional,
            interest=interest.quantize(Decimal("0.01")),
            notional_after=Decimal(0),
        )
    )
    return events


class ScheduleDiff(BaseModel):
    """One cell of disagreement between generated and documented schedules."""

    row: int
    field: str
    generated: str | None
    documented: str | None
    message: str


def diff_schedule(
    generated: list[CashflowEvent],
    documented: list[tuple[date, Decimal]],
) -> list[ScheduleDiff]:
    """Diff generated principal redemptions against the document's table."""
    gen_rows = [(e.event_date, e.principal) for e in generated if e.event_type == "PR"]
    diffs: list[ScheduleDiff] = []
    for index in range(max(len(gen_rows), len(documented))):
        g = gen_rows[index] if index < len(gen_rows) else None
        d = documented[index] if index < len(documented) else None
        if g is None:
            diffs.append(ScheduleDiff(
                row=index, field="row",
                generated=None, documented=f"{d[0]} {d[1]}",
                message="document prints a payment the contract does not generate",
            ))
            continue
        if d is None:
            diffs.append(ScheduleDiff(
                row=index, field="row",
                generated=f"{g[0]} {g[1]}", documented=None,
                message="contract generates a payment the document does not print",
            ))
            continue
        if g[0] != d[0]:
            diffs.append(ScheduleDiff(
                row=index, field="date",
                generated=g[0].isoformat(), documented=d[0].isoformat(),
                message="payment date disagrees",
            ))
        if g[1] != d[1]:
            diffs.append(ScheduleDiff(
                row=index, field="principal",
                generated=str(g[1]), documented=str(d[1]),
                message="payment amount disagrees",
            ))
    return diffs


def provenance() -> dict:
    v = _vendored()
    return {
        "standard": "ACTUS",
        "publisher": "ACTUS Financial Research Foundation",
        "license": v["license"],
        "source": v["source"],
        "dictionary_version": v["version"],
        "reference_implementation": ACTUS_REFERENCE,
        "implemented_here": ["LAM", "LAX"],
    }
