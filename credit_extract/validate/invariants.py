"""Deterministic invariants. Free, and never delegated to a model.

Every check here has been violated by a real executed credit agreement. They
are tier 2 of the escalation ladder and they run before any paid validation,
because a deterministic contradiction is worth more than any amount of model
confidence: Trap 1's duplicated amortization rows all parse cleanly, so no
extraction model flags them, and only the strictly-increasing-dates check
catches the resulting $1,505,000 overstatement.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..models.core import ExtractedField, InvariantViolation, Span
from ..models.fiscal import CALENDAR_YEAR, FiscalCalendar
from ..models.quantities import Quantity
from ..models.fpml_model import FIELD_REGISTRY, AmortizationSchedule, Facility

#: How far a computed leverage ratio may sit from a stated one. Rounding to two
#: decimals in the document is normal; anything wider is a real disagreement.
LEVERAGE_TOLERANCE = Decimal("0.05")


class CovenantStep(BaseModel):
    label: str
    level: Decimal
    span: Span | None = None
    period_start: date | None = None


class BasketRecord(BaseModel):
    name: str
    fixed_amount: Decimal | None = None
    ebitda_pct: Decimal | None = None
    ebitda_base: str | None = None      # "pre_addback" | "post_addback" | None
    span: Span | None = None


class InvariantContext(BaseModel):
    """Everything the deterministic layer needs, and nothing it must infer."""

    model_config = {"arbitrary_types_allowed": True}

    document_id: str = "unknown"
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    amortization: AmortizationSchedule | None = None
    facilities: list[Facility] = Field(default_factory=list)
    covenant_steps: list[CovenantStep] = Field(default_factory=list)
    baskets: list[BasketRecord] = Field(default_factory=list)
    hardcoded_ebitda_quarters: dict[str, Decimal] = Field(default_factory=dict)
    mfn_triggers: dict[str, Decimal] = Field(default_factory=dict)
    actus_schedule_diffs: list[dict[str, Any]] = Field(default_factory=list)
    #: The borrower's fiscal calendar. Date spacing is counted in *fiscal*
    #: quarters, which are evenly spaced under any calendar; counting days
    #: fires on every correct 52/53-week schedule in the corpus.
    fiscal_calendar: FiscalCalendar = CALENDAR_YEAR
    #: Set by archetype dispatch. Invariants that assume a term this deal kind
    #: does not have are skipped rather than failed.
    archetype: str | None = None
    inapplicable_invariants: frozenset[str] = frozenset()

    def quarter_index(self, when: date) -> int:
        return self.fiscal_calendar.quarter_index(when)

    def value(self, name: str) -> Any:
        field = self.fields.get(name)
        return field.value if field else None

    def spans(self, *names: str) -> list[Span]:
        out: list[Span] = []
        for name in names:
            field = self.fields.get(name)
            if field:
                out.extend(field.spans[:1])
        return out


Invariant = Callable[[InvariantContext], list[InvariantViolation]]
_REGISTRY: list[tuple[str, Invariant]] = []


def invariant(name: str) -> Callable[[Invariant], Invariant]:
    def decorate(fn: Invariant) -> Invariant:
        _REGISTRY.append((name, fn))
        return fn
    return decorate


# ---------------------------------------------------------------------------
# Amortization
# ---------------------------------------------------------------------------


@invariant("amortization_dates_strictly_increasing")
def _dates_increasing(ctx: InvariantContext) -> list[InvariantViolation]:
    """Trap 1. Four duplicated rows make the table step backwards in time."""
    schedule = ctx.amortization
    if not schedule or len(schedule.rows) < 2:
        return []
    violations: list[InvariantViolation] = []
    for previous, current in zip(schedule.rows, schedule.rows[1:]):
        if current.payment_date > previous.payment_date:
            continue
        relation = "repeats" if current.payment_date == previous.payment_date else "precedes"
        violations.append(
            InvariantViolation(
                invariant="amortization_dates_strictly_increasing",
                message=(
                    f"amortization row {current.row_index} "
                    f"({current.payment_date.isoformat()}) {relation} row "
                    f"{previous.row_index} ({previous.payment_date.isoformat()}); "
                    "payment dates must strictly increase"
                ),
                fields=["amortization.schedule"],
                observed=current.payment_date.isoformat(),
                expected=f"> {previous.payment_date.isoformat()}",
            )
        )
    return violations


@invariant("amortization_dates_evenly_spaced")
def _dates_evenly_spaced(ctx: InvariantContext) -> list[InvariantViolation]:
    """Spacing is checked in quarters, not days: quarters differ in length."""
    schedule = ctx.amortization
    if not schedule or len(schedule.rows) < 3:
        return []
    unique = schedule.deduplicated().rows
    gaps = [
        ctx.quarter_index(b.payment_date) - ctx.quarter_index(a.payment_date)
        for a, b in zip(unique, unique[1:])
    ]
    if not gaps:
        return []
    modal = max(set(gaps), key=gaps.count)
    violations = []
    for index, gap in enumerate(gaps):
        if gap == modal:
            continue
        violations.append(
            InvariantViolation(
                invariant="amortization_dates_evenly_spaced",
                message=(
                    f"gap of {gap} quarter(s) between "
                    f"{unique[index].payment_date.isoformat()} and "
                    f"{unique[index + 1].payment_date.isoformat()} breaks the "
                    f"{modal}-quarter cycle"
                ),
                fields=["amortization.schedule"],
                observed=gap,
                expected=modal,
            )
        )
    return violations


@invariant("amortization_row_count_matches_quarters")
def _row_count(ctx: InvariantContext) -> list[InvariantViolation]:
    schedule = ctx.amortization
    if not schedule or not schedule.rows:
        return []
    rows = schedule.rows
    first, last = rows[0].payment_date, max(r.payment_date for r in rows)
    spanned = ctx.quarter_index(last) - ctx.quarter_index(first) + 1
    violations: list[InvariantViolation] = []
    if len(rows) != spanned:
        violations.append(
            InvariantViolation(
                invariant="amortization_row_count_matches_quarters",
                message=(
                    f"table prints {len(rows)} payment rows but spans only "
                    f"{spanned} quarters ({first.isoformat()} to "
                    f"{last.isoformat()}); {len(rows) - spanned} row(s) are "
                    "duplicated or mis-dated"
                ),
                fields=["amortization.schedule"],
                observed=len(rows),
                expected=spanned,
            )
        )
    maturity = ctx.value("initial_term_loan.maturity_date")
    if isinstance(maturity, date) and ctx.quarter_index(maturity) < ctx.quarter_index(last):
        violations.append(
            InvariantViolation(
                invariant="amortization_row_count_matches_quarters",
                message=(
                    f"final amortization payment {last.isoformat()} falls after "
                    f"maturity {maturity.isoformat()}"
                ),
                fields=["amortization.schedule", "initial_term_loan.maturity_date"],
                observed=last.isoformat(),
                expected=f"<= {maturity.isoformat()}",
            )
        )
    return violations


@invariant("amortization_total_consistent")
def _amortization_total(ctx: InvariantContext) -> list[InvariantViolation]:
    """Level schedule: printed total must equal amount x quarters spanned."""
    schedule = ctx.amortization
    if not schedule or len(schedule.rows) < 2:
        return []
    amounts = {r.amount for r in schedule.rows}
    if len(amounts) != 1:
        return []
    per_quarter = amounts.pop()
    first = schedule.rows[0].payment_date
    last = max(r.payment_date for r in schedule.rows)
    spanned = ctx.quarter_index(last) - ctx.quarter_index(first) + 1
    literal = schedule.total_scheduled
    expected = per_quarter * spanned
    if literal == expected:
        return []
    return [
        InvariantViolation(
            invariant="amortization_total_consistent",
            message=(
                f"amortization table sums to ${literal:,} but a level "
                f"${per_quarter:,} payment over {spanned} quarters is "
                f"${expected:,}, a ${abs(literal - expected):,} discrepancy"
            ),
            fields=["amortization.schedule", "amortization.quarterly_amount"],
            observed=str(literal),
            expected=str(expected),
        )
    ]


@invariant("amortization_sums_to_principal")
def _sums_to_principal(ctx: InvariantContext) -> list[InvariantViolation]:
    schedule = ctx.amortization
    principal = schedule.original_principal if schedule else None
    if not schedule or principal is None:
        return []
    scheduled = schedule.total_scheduled
    if scheduled > principal:
        return [
            InvariantViolation(
                invariant="amortization_sums_to_principal",
                message=(
                    f"scheduled amortization ${scheduled:,} exceeds original "
                    f"principal ${principal:,}"
                ),
                fields=["amortization.schedule", "initial_term_loan.commitment"],
                observed=str(scheduled),
                expected=f"<= {principal}",
            )
        ]
    stated = schedule.stated_bullet_at_maturity
    if stated is None:
        return []
    total = scheduled + stated
    if total != principal:
        return [
            InvariantViolation(
                invariant="amortization_sums_to_principal",
                message=(
                    f"amortization ${scheduled:,} plus the stated bullet "
                    f"${stated:,} is ${total:,}, not the original principal "
                    f"${principal:,}"
                ),
                fields=["amortization.schedule", "initial_term_loan.commitment"],
                observed=str(total),
                expected=str(principal),
            )
        ]
    return []


@invariant("actus_schedule_matches_document")
def _actus_diff(ctx: InvariantContext) -> list[InvariantViolation]:
    """The generated ACTUS schedule, diffed cell by cell against the table."""
    if not ctx.actus_schedule_diffs:
        return []
    head = ctx.actus_schedule_diffs[:6]
    return [
        InvariantViolation(
            invariant="actus_schedule_matches_document",
            message=(
                f"{len(ctx.actus_schedule_diffs)} cell(s) of the documented "
                f"amortization table disagree with the schedule generated from "
                f"the extracted ACTUS contract; first: "
                + "; ".join(
                    f"row {d['row']} {d['field']}: doc={d['documented']} "
                    f"generated={d['generated']}"
                    for d in head
                )
            ),
            fields=["amortization.schedule"],
            observed=len(ctx.actus_schedule_diffs),
            expected=0,
        )
    ]


# ---------------------------------------------------------------------------
# Covenants, maturities, ranges
# ---------------------------------------------------------------------------


@invariant("covenant_steps_monotonic")
def _covenant_monotonic(ctx: InvariantContext) -> list[InvariantViolation]:
    steps = ctx.covenant_steps
    if len(steps) < 2:
        return []
    violations = []
    for previous, current in zip(steps, steps[1:]):
        if current.level <= previous.level:
            continue
        violations.append(
            InvariantViolation(
                invariant="covenant_steps_monotonic",
                message=(
                    f"covenant level steps up from {previous.level} "
                    f"({previous.label}) to {current.level} ({current.label}); "
                    "step-downs must be monotonically non-increasing"
                ),
                fields=["financial_covenant.schedule"],
                spans=[s for s in (current.span,) if s],
                observed=str(current.level),
                expected=f"<= {previous.level}",
            )
        )
    return violations


@invariant("revolver_maturity_before_term_maturity")
def _maturity_order(ctx: InvariantContext) -> list[InvariantViolation]:
    revolver = ctx.value("revolver.maturity_date")
    term = ctx.value("initial_term_loan.maturity_date")
    if not isinstance(revolver, date) or not isinstance(term, date):
        return []
    if revolver <= term:
        return []
    return [
        InvariantViolation(
            invariant="revolver_maturity_before_term_maturity",
            severity="warning",
            message=(
                f"revolver matures {revolver.isoformat()}, after the term loan "
                f"{term.isoformat()}; unusual and materially relevant to "
                "refinancing risk"
            ),
            fields=["revolver.maturity_date", "initial_term_loan.maturity_date"],
            spans=ctx.spans("revolver.maturity_date"),
            observed=revolver.isoformat(),
            expected=f"<= {term.isoformat()}",
        )
    ]


@invariant("percentages_and_ratios_in_range")
def _ranges(ctx: InvariantContext) -> list[InvariantViolation]:
    violations: list[InvariantViolation] = []
    for name, field in ctx.fields.items():
        spec = FIELD_REGISTRY.get(name)
        if spec is None or field.value is None:
            continue
        if not isinstance(field.value, (int, float, Decimal)):
            continue
        value = Decimal(str(field.value))
        if spec.kind == "percent" and not (Decimal(0) <= value <= Decimal(100)):
            violations.append(
                InvariantViolation(
                    invariant="percentages_and_ratios_in_range",
                    message=f"{name} = {value} is outside [0, 100]",
                    fields=[name], spans=field.spans[:1],
                    observed=str(value), expected="[0, 100]",
                )
            )
        if spec.kind == "ratio" and value <= 0:
            violations.append(
                InvariantViolation(
                    invariant="percentages_and_ratios_in_range",
                    message=f"{name} = {value} must be greater than zero",
                    fields=[name], spans=field.spans[:1],
                    observed=str(value), expected="> 0",
                )
            )
    return violations


@invariant("mfn_trigger_ordering")
def _mfn_ordering(ctx: InvariantContext) -> list[InvariantViolation]:
    pari = ctx.mfn_triggers.get("pari_passu")
    junior = ctx.mfn_triggers.get("junior")
    if pari is None or junior is None:
        return []
    if pari < junior:
        return []
    return [
        InvariantViolation(
            invariant="mfn_trigger_ordering",
            message=(
                f"MFN pari passu trigger {pari} is not below the junior trigger "
                f"{junior}; junior debt must be permitted a wider spread"
            ),
            fields=["mfn_threshold_pct"],
            observed=str(pari), expected=f"< {junior}",
        )
    ]


@invariant("opening_leverage_consistent")
def _opening_leverage(ctx: InvariantContext) -> list[InvariantViolation]:
    """Compute leverage from extracted inputs; compare to the stated figure."""
    stated = ctx.value("opening_total_leverage_ratio")
    debt = ctx.value("initial_term_loan.commitment")
    if stated is None or debt is None or not ctx.hardcoded_ebitda_quarters:
        return []
    ebitda = sum(ctx.hardcoded_ebitda_quarters.values(), Decimal(0))
    if ebitda <= 0:
        return []
    computed = (Decimal(str(debt)) / ebitda).quantize(Decimal("0.0001"))
    stated_dec = Decimal(str(stated))
    if abs(computed - stated_dec) <= LEVERAGE_TOLERANCE:
        return []
    return [
        InvariantViolation(
            invariant="opening_leverage_consistent",
            message=(
                f"document states an opening leverage ratio of {stated_dec} but "
                f"${debt:,} of debt over ${ebitda:,} of LTM EBITDA computes to "
                f"{computed}"
            ),
            fields=["opening_total_leverage_ratio", "initial_term_loan.commitment"],
            spans=ctx.spans("opening_total_leverage_ratio"),
            observed=str(computed), expected=str(stated_dec),
        )
    ]


@invariant("ebitda_baskets_have_identified_base")
def _basket_base(ctx: InvariantContext) -> list[InvariantViolation]:
    """A basket quoted off EBITDA is meaningless without knowing which EBITDA."""
    violations = []
    for basket in ctx.baskets:
        if basket.ebitda_pct is None or basket.ebitda_base:
            continue
        violations.append(
            InvariantViolation(
                invariant="ebitda_baskets_have_identified_base",
                severity="warning",
                message=(
                    f"basket {basket.name!r} is denominated as "
                    f"{basket.ebitda_pct}% of Consolidated EBITDA but the "
                    "definition's add-back base (pre- or post-add-back) is not "
                    "identified; the two differ materially"
                ),
                fields=[basket.name],
                spans=[s for s in (basket.span,) if s],
                observed=None, expected="pre_addback | post_addback",
            )
        )
    return violations


@invariant("every_numeric_has_a_unit")
def _units_present(ctx: InvariantContext) -> list[InvariantViolation]:
    """F08. A number without a unit is a 1000x error waiting to be believed."""
    violations = []
    for name, field in ctx.fields.items():
        spec = FIELD_REGISTRY.get(name)
        if spec is None or field.value is None:
            continue
        if spec.kind not in ("money", "percent", "ratio"):
            continue
        quantity = field.quantity
        if isinstance(quantity, Quantity):
            continue
        violations.append(
            InvariantViolation(
                invariant="every_numeric_has_a_unit",
                message=(
                    f"{name} stores {field.value} with no unit; scale cannot be "
                    "checked and a thousands/millions confusion would be "
                    "invisible"
                ),
                fields=[name], spans=field.spans[:1],
                observed=None, expected=spec.kind,
            )
        )
    return violations


@invariant("date_invariants_use_fiscal_calendar")
def _calendar_declared(ctx: InvariantContext) -> list[InvariantViolation]:
    """F08. Assuming calendar quarters on a 52/53-week borrower is a false
    positive factory, so an undetected calendar is itself reported."""
    if ctx.amortization is None or not ctx.amortization.rows:
        return []
    if ctx.fiscal_calendar.source != "default (document is silent)":
        return []
    ends = {(r.payment_date.month, r.payment_date.day) for r in ctx.amortization.rows}
    calendar_quarter_ends = {(3, 31), (6, 30), (9, 30), (12, 31)}
    if ends <= calendar_quarter_ends:
        return []
    return [
        InvariantViolation(
            invariant="date_invariants_use_fiscal_calendar",
            severity="warning",
            message=(
                "payment dates are not calendar quarter ends "
                f"({sorted(ends - calendar_quarter_ends)[:4]}) but no fiscal "
                "calendar was detected; spacing checks are running on an "
                "assumed calendar year"
            ),
            fields=["amortization.schedule"],
            observed=ctx.fiscal_calendar.source,
            expected="a fiscal calendar read from the document",
        )
    ]


# ---------------------------------------------------------------------------


def check_all(ctx: InvariantContext) -> list[InvariantViolation]:
    """Run every applicable invariant. Order is stable for reproducible reports.

    An invariant the archetype rules out is skipped, not failed. Running an
    EBITDA leverage check on a recurring-revenue loan produces a violation on a
    perfectly correct document, and a reviewer who sees a few of those stops
    reading the rest.
    """
    out: list[InvariantViolation] = []
    for name, fn in _REGISTRY:
        if name in ctx.inapplicable_invariants:
            continue
        out.extend(fn(ctx))
    return out


def registered() -> list[str]:
    return [name for name, _ in _REGISTRY]
