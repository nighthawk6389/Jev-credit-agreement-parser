"""Deterministic invariants. Free, and never delegated to a model.

Every check here has been violated by a real executed credit agreement. They
are tier 2 of the escalation ladder and they run before any paid validation,
because a deterministic contradiction is worth more than any amount of model
confidence: Trap 1's duplicated amortization rows all parse cleanly, so no
extraction model flags them, and only the strictly-increasing-dates check
catches the resulting $1,505,000 overstatement.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..models.core import ExtractedField, InvariantViolation, Span
from ..models.fiscal import CALENDAR_YEAR, FiscalCalendar
from ..models.quantities import Quantity
from ..models.pricing import _CSA_RE as _CSA_STATED_RE
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
    #: Populated by the pipeline. Typed loosely to keep this module free of a
    #: dependency on the graph and pricing layers.
    definition_graph: Any = None
    precedence_graph: Any = None
    pricing: Any = None
    document: Any = None
    chain_findings: list[Any] = Field(default_factory=list)
    document_omits_schedules: bool = False

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
#: invariant name -> the trap family it defends. Keeps the register in
#: trap_families.yaml honest: a family whose `defended_by` names an invariant
#: that does not exist is caught by a test.
FAMILY_OF: dict[str, str] = {}


def invariant(name: str, family: str = "F01_integrity") -> Callable[[Invariant], Invariant]:
    def decorate(fn: Invariant) -> Invariant:
        _REGISTRY.append((name, fn))
        FAMILY_OF[name] = family
        return fn
    return decorate


# ---------------------------------------------------------------------------
# Amortization
# ---------------------------------------------------------------------------


@invariant("amortization_dates_strictly_increasing", "F01_integrity")
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


@invariant("amortization_dates_evenly_spaced", "F08_units")
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


@invariant("amortization_row_count_matches_quarters", "F01_integrity")
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


@invariant("amortization_total_consistent", "F01_integrity")
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


@invariant("amortization_sums_to_principal", "F01_integrity")
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


@invariant("actus_schedule_matches_document", "F01_integrity")
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


@invariant("covenant_steps_monotonic", "F07_benchmark")
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


@invariant("revolver_maturity_before_term_maturity", "F01_integrity")
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


@invariant("percentages_and_ratios_in_range", "F08_units")
def _ranges(ctx: InvariantContext) -> list[InvariantViolation]:
    violations: list[InvariantViolation] = []
    for name, field in ctx.fields.items():
        spec = FIELD_REGISTRY.get(name)
        if spec is None or field.value is None:
            continue
        if spec.kind not in ("percent", "ratio"):
            continue
        # A bool is an int in Python and Decimal("False") raises rather than
        # returning anything, so a single populated boolean field took the
        # whole report down with a ConversionSyntax three frames deep. Nothing
        # in this check applies to one: booleans have their own kind and no
        # range to be outside of.
        if isinstance(field.value, bool):
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


@invariant("mfn_trigger_ordering", "F07_benchmark")
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


@invariant("opening_leverage_consistent", "F01_integrity")
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


@invariant("ebitda_baskets_have_identified_base", "F09_definitional_depth")
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


@invariant("every_numeric_has_a_unit", "F08_units")
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


#: Above this, a stored span is not a quotation. The measured separation on the
#: documents that produced this check is absolute and nowhere near the line:
#: every value-bearing span was at most 162 characters and every offending
#: null-value span at least 8,379.
_QUOTATION_CEILING = 1_000


@invariant("citations_cite_a_value", "F01_integrity")
def _citations_cite_a_value(ctx: InvariantContext) -> list[InvariantViolation]:
    """F01. A span too large to be a quotation is not a citation.

    Spans are the mechanism the whole pipeline rests on: a value is only
    trustworthy because the text it was read from can be reread.

    This began as "a span on a field with no value cites nothing", written
    from the case that prompted it -- whole chunks, 8,379 characters and up,
    stored where a citation belongs, against fields the extractor had said
    nothing about. Two things have happened since. The chunk-wide hints moved
    to ``review_hint``, which says what they are, so they no longer reach
    ``spans`` at all. And a model tier started producing the opposite case: a
    field with no value and a real, short quotation that is precisely the
    evidence for having none -- a maturity defined as five years after an
    undated event, a spread adjustment that applies only if the benchmark is
    ever replaced, a covenant stated in a unit the field cannot hold. A reader
    following one of those spans gets exactly the passage that explains the
    empty field, which is the opposite of being sent nowhere.

    So the test is the thing that actually separated the two all along, and it
    is stated rather than inferred: a stored span must be short enough to be a
    quotation. That keeps the original regression caught -- a chunk in
    ``spans`` still fires -- without calling an evidenced absence a defect.
    """
    violations = []
    for name, field in ctx.fields.items():
        for variant in field.variants:
            if variant.value is not None or not variant.spans:
                continue
            span = variant.spans[0]
            length = span.end - span.start
            if length <= _QUOTATION_CEILING:
                continue
            violations.append(
                InvariantViolation(
                    invariant="citations_cite_a_value",
                    message=(
                        f"{name} has no value but carries a {length:,}-character "
                        f"span, past the {_QUOTATION_CEILING:,} a quotation can "
                        "run to; a reader following it is sent to a block of "
                        "text rather than to a passage. Where to look next "
                        "belongs in review_hint, which says so"
                    ),
                    fields=[name], spans=[span],
                    observed=str(length), expected=f"<= {_QUOTATION_CEILING}",
                )
            )
    return violations


@invariant("date_invariants_use_fiscal_calendar", "F08_units")
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


# ---------------------------------------------------------------------------
# F01 -- integrity: defects that parse cleanly
# ---------------------------------------------------------------------------

_XREF_RE = re.compile(
    r"\bSection\s+(\d+\.\d+[A-Za-z]?)\b|\bArticle\s+([IVXLC]+)\b"
)
_SCHEDULE_REF_RE = re.compile(
    r"\b(Schedule|Exhibit|Annex)\s+([\w.()-]+)", re.IGNORECASE
)
#: "twenty-five percent (35%)" -- the words and the numeral disagree.
#: A fraction *of* a spelled percentage: "one-quarter of one percent (0.25%)".
#: The market writes sub-1% rates this way and the figure in brackets is the
#: product, not the spelled number -- so a check that reads only the words next
#: to "percent" sees "one percent (0.25%)" and reports a document that says
#: exactly the right thing. Air T states its unused commitment fee this way,
#: and it was the first held-out document to be labelled.
_FRACTION_OF_PERCENT_RE = re.compile(
    r"\b(?:one[\s-])?(?:quarter|half|third|eighth|tenth)s?\s+of\s+(?:one|a)\s+percent",
    re.IGNORECASE,
)

_NUMERAL_WORD_RE = re.compile(
    r"\b(?P<words>(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"ten|eleven|twelve|fifteen|one|two|three|four|five|six|seven|eight|nine)"
    r"(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?)\s+"
    r"percent\s*\(\s*(?P<numeral>\d+(?:\.\d+)?)\s*%\s*\)",
    re.IGNORECASE,
)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _words_to_number(words: str) -> int | None:
    parts = re.split(r"[\s-]+", words.strip().lower())
    total = 0
    for part in parts:
        if part not in _WORD_NUMBERS:
            return None
        total += _WORD_NUMBERS[part]
    return total


#: Citations to statute and regulation use the same word. "Section 1.163-5 of
#: the proposed Treasury Regulations" and "29 C.F.R. Section 2510.3-101" are
#: not dangling references to the agreement, and reporting them as such buries
#: the ones that are: in one real filing two genuine broken references were
#: sitting among thirty-odd of these.
_REGULATION_BEFORE_RE = re.compile(
    r"(?:C\.?F\.?R\.?|U\.?S\.?C\.?|Treasury\s+Regulations?|Regulations?\s+[A-Z]\b"
    r"|ERISA|Internal\s+Revenue\s+Code|the\s+Code|Securities\s+Act"
    r"|Exchange\s+Act|Investment\s+Company\s+Act|Bankruptcy\s+Code)"
    r"[^.]{0,60}$",
    re.IGNORECASE,
)
_REGULATION_AFTER_RE = re.compile(
    r"^(?:-\d|\s*(?:of|under)\s+(?:the\s+)?(?:proposed\s+)?"
    r"(?:United\s+States\s+)?(?:Treasury\s+Regulations?|C\.?F\.?R\.?|U\.?S\.?C\.?"
    r"|Internal\s+Revenue\s+Code|Code\b|ERISA|Securities\s+Act|Exchange\s+Act"
    r"|Investment\s+Company\s+Act|Bankruptcy\s+Code))",
    re.IGNORECASE,
)


def _cite(target: str) -> str:
    """Render a cross-reference target the way the document writes it."""
    return target if target.upper().startswith(("ARTICLE", "SECTION")) else f"Section {target}"


def _cites_a_regulation(text: str, match: re.Match[str]) -> bool:
    """Whether this "Section N" points outside the agreement entirely."""
    return bool(
        _REGULATION_BEFORE_RE.search(text[max(0, match.start() - 80):match.start()])
        or _REGULATION_AFTER_RE.match(text[match.end():match.end() + 80])
    )


@invariant("cross_references_resolve", "F01_integrity")
def _cross_references(ctx: InvariantContext) -> list[InvariantViolation]:
    """Every internal cross-reference points at a section that exists.

    A citation to a section that was renumbered in a restatement reads
    perfectly and sends the reader nowhere.
    """
    doc = ctx.document
    if doc is None or not doc.sections:
        return []
    known = {marker.section_id for marker in doc.sections}
    known |= {s.upper() for s in known}
    reserved = doc.reserved_sections()
    reserved |= {s.upper() for s in reserved}
    missing: dict[str, Span] = {}
    empty: dict[str, Span] = {}
    for match in _XREF_RE.finditer(doc.text):
        target = match.group(1) or f"ARTICLE {match.group(2)}"
        if _cites_a_regulation(doc.text, match):
            continue
        if target in reserved or target.upper() in reserved:
            empty.setdefault(target, doc.span(match.start(), match.end()))
            continue
        if target in known or target.upper() in known:
            continue
        # "Section 3.1" where the document prints "SECTION 3 [RESERVED]": the
        # subsection is gone because its parent was reserved, which is a more
        # useful thing to say than that the number was never used.
        parent = target.split(".")[0]
        if reserved & {parent, f"ARTICLE {parent}", f"SECTION {parent}"}:
            empty.setdefault(target, doc.span(match.start(), match.end()))
            continue
        missing.setdefault(target, doc.span(match.start(), match.end()))
    return [
        InvariantViolation(
            invariant="cross_references_resolve",
            # "Section ARTICLE I" was the old wording, because the target
            # already carries its own kind. A finding a reader cannot read is
            # a finding they will not act on.
            message=(
                f"cross-reference to {_cite(target)} does not resolve; no such "
                "provision exists in this document"
            ),
            fields=["cross_references"], spans=[span],
            observed=target, expected="an existing section",
        )
        for target, span in sorted(missing.items())
    ] + [
        InvariantViolation(
            invariant="cross_references_resolve",
            # A reference into a reserved provision is ordinary drafting
            # residue -- the section was emptied during negotiation and the
            # citation left behind -- so it is reported and not treated as an
            # error. A reference to a number the document never used is not.
            severity="warning",
            message=(
                f"cross-reference to {_cite(target)}, which the document "
                "prints as reserved; the provision it points to is empty"
            ),
            fields=["cross_references"], spans=[span],
            observed=f"{target} (reserved)", expected="a section with content",
        )
        for target, span in sorted(empty.items())
    ]


@invariant("numeral_and_words_agree", "F01_integrity")
def _numeral_words(ctx: InvariantContext) -> list[InvariantViolation]:
    """"twenty-five percent (35%)" -- both halves parse and they disagree."""
    doc = ctx.document
    if doc is None:
        return []
    violations = []
    for match in _NUMERAL_WORD_RE.finditer(doc.text):
        spelled = _words_to_number(match.group("words"))
        numeral = Decimal(match.group("numeral"))
        if spelled is None or Decimal(spelled) == numeral:
            continue
        # "one-quarter of one percent (0.25%)" agrees with itself; the words
        # this pattern captured are the tail of a fraction, not the figure.
        lead = doc.text[max(0, match.start() - 40): match.end()]
        if _FRACTION_OF_PERCENT_RE.search(lead):
            continue
        violations.append(InvariantViolation(
            invariant="numeral_and_words_agree",
            message=(
                f"{match.group('words')!r} does not equal {numeral}% in "
                f"{' '.join(match.group(0).split())!r}"
            ),
            fields=["numeral_word_agreement"],
            spans=[doc.span(match.start(), match.end())],
            observed=str(numeral), expected=str(spelled),
        ))
    return violations


@invariant("defined_terms_are_unique", "F01_integrity")
def _terms_unique(ctx: InvariantContext) -> list[InvariantViolation]:
    """A term defined twice means half the document reads the wrong one."""
    graph = ctx.definition_graph
    doc = ctx.document
    if graph is None or doc is None:
        return []
    violations = []
    for term, node in sorted(graph.nodes.items()):
        pattern = re.compile(
            r'"' + re.escape(term) + r'"\s*(?:means|shall mean)\b'
        )
        hits = list(pattern.finditer(doc.text))
        if len(hits) <= 1:
            continue
        violations.append(InvariantViolation(
            invariant="defined_terms_are_unique",
            message=(
                f'"{term}" is defined {len(hits)} times; which definition '
                "governs depends on where the reader is in the document"
            ),
            fields=["definitions"],
            spans=[doc.span(h.start(), h.end()) for h in hits[:3]],
            observed=len(hits), expected=1,
        ))
    return violations


@invariant("referenced_schedules_present", "F01_integrity")
def _schedules_present(ctx: InvariantContext) -> list[InvariantViolation]:
    """A schedule a *field depends on* is cited and not attached.

    The unqualified version of this check fired on 78 of 98 real agreements,
    which is not a market-wide defect -- it is how EDGAR works. Exhibits and
    schedules are filed as separate documents, so essentially every credit
    agreement references twenty-odd attachments that are not in the same file.
    Reporting that as an integrity violation buries the cases where the
    missing schedule actually carries a term, and those are the point.

    The discriminator is what the filer did with the *other* schedules. A
    document that attaches none of them is a document whose schedules are
    filed separately, and a missing one says nothing about the agreement. A
    document that attaches twelve and cites a thirteenth has a gap, and that
    is worth reporting -- which is also what an injected-defect test exercises
    when it deletes one.

    A schedule some extracted field actually depends on is a defect either
    way, because then the missing attachment is hiding a term.
    """
    doc = ctx.document
    if doc is None or ctx.document_omits_schedules:
        return []
    # Schedules some extracted field points at. Only these can hide a term.
    depended_on = {
        (field.external_document or "").strip()
        for field in ctx.fields.values()
        if field.external_document
    }
    depended_on |= {
        span.text.strip()
        for field in ctx.fields.values()
        for span in field.spans
        if _SCHEDULE_REF_RE.fullmatch(span.text.strip())
    }
    cited: dict[str, Span] = {}
    for match in _SCHEDULE_REF_RE.finditer(doc.text):
        # The identifier runs up to the sentence, so "Schedule 6.01." at the end
        # of a sentence captures the full stop and then never matches the
        # heading "Schedule 6.01" that is sitting right there.
        identifier = match.group(2).rstrip(".,;:")
        if not identifier:
            continue
        cited.setdefault(
            f"{match.group(1).title()} {identifier}",
            doc.span(match.start(), match.end()),
        )
    if not cited:
        return []
    present = {
        name for name in cited
        if re.search(
            r"^\s*" + re.escape(name) + r"\s*$", doc.text, re.MULTILINE | re.IGNORECASE
        )
    }
    absent = sorted(set(cited) - present)
    depended = sorted(
        name for name in absent
        if any(name.casefold() in d.casefold() for d in depended_on if d)
    )
    # Nothing attached at all: the schedules are in a separate filing, which
    # is ordinary. Only a term that needs one makes it a defect.
    # Proportion, not presence. Nearly every filing matches one or two
    # schedule headings in passing -- one real agreement cites 34 and matches
    # 1 -- so "attaches any" put almost the whole corpus back in the strict
    # branch. A filer who attached most of what they cite and left one out has
    # a gap; a filer who attached almost none files their schedules separately.
    attaches_its_schedules = len(present) >= len(cited) / 2
    needed = absent if attaches_its_schedules else depended
    if not needed:
        return []
    why = (
        f"the document attaches {len(present)} of the {len(cited)} schedule(s) "
        "it cites, so these are gaps rather than a separate filing"
        if attaches_its_schedules else
        "an extracted term depends on them and they are not in the document"
    )
    return [InvariantViolation(
        invariant="referenced_schedules_present",
        severity="warning",
        message=(
            f"{len(needed)} referenced schedule(s) are missing and no omission "
            f"is declared -- {why}: {', '.join(needed[:6])}"
        ),
        fields=["schedules"], spans=[cited[needed[0]]],
        observed=len(needed), expected=0,
    )]


# ---------------------------------------------------------------------------
# F02 -- precedence
# ---------------------------------------------------------------------------


@invariant("precedence_graph_is_acyclic", "F02_precedence")
def _precedence_acyclic(ctx: InvariantContext) -> list[InvariantViolation]:
    """Two provisions each claiming to override the other is a drafting error."""
    graph = ctx.precedence_graph
    if graph is None:
        return []
    return [
        InvariantViolation(
            invariant="precedence_graph_is_acyclic",
            message=(
                "precedence cycle: "
                + " -> ".join(f"Section {s}" for s in cycle)
                + " -> " + f"Section {cycle[0]}; which governs is undetermined"
            ),
            fields=["precedence"], observed=cycle, expected="acyclic",
        )
        for cycle in graph.cycles()
    ]


@invariant("proviso_depth_is_readable", "F02_precedence")
def _proviso_depth(ctx: InvariantContext) -> list[InvariantViolation]:
    graph = ctx.precedence_graph
    if graph is None:
        return []
    return [
        InvariantViolation(
            invariant="proviso_depth_is_readable",
            severity="warning",
            message=(
                f"Section {stack.section_id} stacks {stack.depth} provisos; at "
                "this depth the operative meaning is not readable from the "
                "clause and any extraction from it should be reviewed"
            ),
            fields=["provisos"], spans=[stack.span],
            observed=stack.depth, expected=f"< {4}",
        )
        for stack in graph.deep_provisos()
    ]


# ---------------------------------------------------------------------------
# F05 -- versioning
# ---------------------------------------------------------------------------


@invariant("amendment_targets_exist", "F05_versioning")
def _amendment_targets(ctx: InvariantContext) -> list[InvariantViolation]:
    return [
        InvariantViolation(
            invariant="amendment_targets_exist",
            message=finding.message,
            fields=["amendment_chain"],
            observed=finding.section, expected="an existing section",
        )
        for finding in ctx.chain_findings
        if getattr(finding, "kind", "") == "amendment_target_missing"
    ]


@invariant("restatements_do_not_conflict", "F05_versioning")
def _restatement_conflicts(ctx: InvariantContext) -> list[InvariantViolation]:
    return [
        InvariantViolation(
            invariant="restatements_do_not_conflict",
            message=finding.message,
            fields=["amendment_chain"],
            observed=finding.documents, expected="one operative version",
        )
        for finding in ctx.chain_findings
        if getattr(finding, "kind", "") == "operative_version_ambiguous"
    ]


# ---------------------------------------------------------------------------
# F07 -- benchmark
# ---------------------------------------------------------------------------


@invariant("csa_not_silently_zero", "F07_benchmark")
def _csa_present(ctx: InvariantContext) -> list[InvariantViolation]:
    """A CSA the document names but the pipeline did not read.

    This check used to fire whenever a SOFR deal carried no credit spread
    adjustment, on the theory that CSAs are near-universal. Measured across a
    hundred EDGAR agreements they are not: 71 price off Term SOFR and only 31
    name an adjustment at all -- the rest fold the spread into the margin and
    quote the bare rate, for which zero is the correct reading. Firing on the
    other 40 made this the loudest check in the suite and the least
    informative, which is how a reader learns to skip the section it prints in.

    So the condition is now the one that indicates an error: the agreement
    states an adjustment *with a figure* and none came back. A silent zero
    there still understates the yield, and the understated yield understates
    every MFN comparison made against it.

    Naming alone is not enough either. Benchmark-transition boilerplate
    promises a spread adjustment if SOFR is ever replaced, and it is in almost
    every agreement written since 2022; reading that as a term of the deal is
    how this check went on firing after the first fix.
    """
    pricing = ctx.pricing
    document = ctx.document
    if pricing is None or pricing.base not in ("term_sofr", "daily_simple_sofr"):
        return []
    if pricing.credit_spread_adjustment is not None:
        return []
    named = _CSA_STATED_RE.search(document.text) if document is not None else None
    if named is None:
        return []
    closing = ctx.value("closing_date")
    if isinstance(closing, date) and closing < date(2021, 1, 1):
        return []
    return [InvariantViolation(
        invariant="csa_not_silently_zero",
        severity="warning",
        message=(
            f"the agreement states a credit spread adjustment "
            f"({' '.join(named.group(0).split())[:90]!r}) and no value for it "
            f"was read, with pricing off {pricing.base}; an adjustment treated "
            "as zero understates the all-in yield and therefore understates "
            "every MFN comparison made against it"
        ),
        fields=["pricing.credit_spread_adjustment"],
        spans=[document.span(named.start(), named.end())],
        observed=None, expected="the stated adjustment",
    )]


@invariant("grid_levels_monotonic", "F07_benchmark")
def _grid_monotonic(ctx: InvariantContext) -> list[InvariantViolation]:
    """Margin must fall as leverage falls; a grid that inverts is an error."""
    pricing = ctx.pricing
    if pricing is None or len(pricing.grid) < 2:
        return []
    priced = [
        level for level in pricing.grid
        if level.margin is not None and level.leverage_to is not None
    ]
    priced.sort(key=lambda level: level.leverage_to, reverse=True)
    violations = []
    for higher, lower in zip(priced, priced[1:]):
        if lower.margin.value <= higher.margin.value:
            continue
        violations.append(InvariantViolation(
            invariant="grid_levels_monotonic",
            message=(
                f"grid level {lower.level!r} prices at {lower.margin.value}% at "
                f"lower leverage than level {higher.level!r} at "
                f"{higher.margin.value}%; margin must not rise as leverage falls"
            ),
            fields=["pricing.grid"],
            observed=str(lower.margin.value), expected=f"<= {higher.margin.value}",
        ))
    return violations


@invariant("grid_bands_contiguous", "F07_benchmark")
def _grid_bands(ctx: InvariantContext) -> list[InvariantViolation]:
    """Bands must tile the range: a gap or overlap leaves the rate undefined."""
    pricing = ctx.pricing
    if pricing is None or len(pricing.grid) < 2:
        return []
    banded = [
        level for level in pricing.grid
        if level.leverage_from is not None or level.leverage_to is not None
    ]
    banded.sort(
        key=lambda level: level.leverage_from
        if level.leverage_from is not None else Decimal("-1")
    )
    violations = []
    for lower, higher in zip(banded, banded[1:]):
        if lower.leverage_to is None or higher.leverage_from is None:
            continue
        if lower.leverage_to == higher.leverage_from:
            continue
        relation = "gap" if lower.leverage_to < higher.leverage_from else "overlap"
        violations.append(InvariantViolation(
            invariant="grid_bands_contiguous",
            message=(
                f"{relation} between grid level {lower.level!r} (up to "
                f"{lower.leverage_to}) and {higher.level!r} (above "
                f"{higher.leverage_from}); the applicable margin is "
                f"{'undefined' if relation == 'gap' else 'ambiguous'} in that range"
            ),
            fields=["pricing.grid"],
            observed=f"{lower.leverage_to} / {higher.leverage_from}",
            expected="contiguous bands",
        ))
    return violations


# ---------------------------------------------------------------------------
# F10 -- conditionality
# ---------------------------------------------------------------------------


@invariant("conditions_parse", "F10_conditionality")
def _conditions_parse(ctx: InvariantContext) -> list[InvariantViolation]:
    """A condition that does not parse cannot be evaluated at resolution time."""
    from ..models.conditions import ConditionSyntaxError, parse_condition

    violations = []
    for name, field in ctx.fields.items():
        for index, variant in enumerate(field.variants):
            for condition in variant.conditions:
                try:
                    parse_condition(condition.expr)
                except ConditionSyntaxError as exc:
                    violations.append(InvariantViolation(
                        invariant="conditions_parse",
                        message=(
                            f"{name} variant {index} carries an unparseable "
                            f"condition {condition.expr!r}: {exc}"
                        ),
                        fields=[name], spans=condition.source_spans[:1],
                        observed=condition.expr, expected="the condition grammar",
                    ))
    return violations


@invariant("variant_ranges_do_not_overlap", "F10_conditionality")
def _variant_ranges(ctx: InvariantContext) -> list[InvariantViolation]:
    """Two unconditional variants covering one date make resolution arbitrary."""
    violations = []
    for name, field in ctx.fields.items():
        unconditional = [
            v for v in field.variants if v.unconditional and v.value is not None
        ]
        for index, first in enumerate(unconditional):
            for second in unconditional[index + 1:]:
                if not _windows_overlap(first, second):
                    continue
                violations.append(InvariantViolation(
                    invariant="variant_ranges_do_not_overlap",
                    message=(
                        f"{name} has two unconditional variants whose date "
                        f"windows overlap ({first.effective_from}..."
                        f"{first.effective_to} and {second.effective_from}..."
                        f"{second.effective_to}); which governs is decided by "
                        "list order rather than by the document"
                    ),
                    fields=[name], spans=first.spans[:1],
                    observed=[str(first.value), str(second.value)],
                    expected="disjoint windows or an explicit condition",
                ))
    return violations


def _windows_overlap(a: Any, b: Any) -> bool:
    a_start = a.effective_from or date.min
    a_end = a.effective_to or date.max
    b_start = b.effective_from or date.min
    b_end = b.effective_to or date.max
    return a_start <= b_end and b_start <= a_end


# ---------------------------------------------------------------------------
# F11 -- layout: structure destroyed by rendering
# ---------------------------------------------------------------------------

#: The sentence a blackline uses to say its changes are typographic.
_BLACKLINE_CLAIM_RE = re.compile(
    r"(?:delete|remove|strike)\s+the\s+(?:bold,?\s+)?"
    r"(?:stricken|struck|struck-through|deleted|lined-out)\s+text",
    re.IGNORECASE,
)


@invariant("blackline_deletions_excised", "F11_layout")
def _blackline_excised(ctx: InvariantContext) -> list[InvariantViolation]:
    """A document that says it is a blackline, with no markup to show for it.

    32 of 100 real agreements amend by blackline -- more than amend in prose.
    The changes are carried entirely by strike-through and underline, so a
    converter that drops the styling leaves both the old and new figures in
    the text, adjacent and in reading order. ``Up to U.S. $ 2,150,000,000
    2,250,000,000`` is a real line, and a first-match parser reports the
    deleted figure off a span that quotes the document accurately.

    Nothing downstream can detect that. The only place it is visible is here,
    comparing what the document says it is against what survived ingestion.
    """
    document = ctx.document
    if document is None or not _BLACKLINE_CLAIM_RE.search(document.text):
        return []
    if getattr(document, "is_blackline", False):
        return []
    claim = _BLACKLINE_CLAIM_RE.search(document.text)
    return [InvariantViolation(
        invariant="blackline_deletions_excised",
        message=(
            "the document states that its amendments are marked by "
            "strike-through, and no deletion markup survived conversion; every "
            "figure it changed still reads as the superseded value beside its "
            "replacement, so no term extracted from it can be relied on"
        ),
        fields=["*"],
        spans=[document.span(claim.start(), claim.end())],
        observed="no deletion markup",
        expected="the struck runs, excised and recorded",
    )]


@invariant("table_scale_declarations", "F11_layout")
def _table_scale(ctx: InvariantContext) -> list[InvariantViolation]:
    """"(in thousands)" above a table the cells were not scaled by.

    A thousand-fold error that looks entirely reasonable: $376 reads as a
    plausible figure, and so does $376,250.
    """
    document = ctx.document
    if document is None:
        return []
    from ..models.quantities import detect_scale

    factors = {"USD_thousands": 1_000, "USD_millions": 1_000_000}
    violations: list[InvariantViolation] = []
    for table in getattr(document, "tables", []):
        header = document.text[max(0, table.start - 300):table.start]
        declared = detect_scale(header)
        if declared is None:
            continue
        unit, phrase = declared
        if getattr(table, "scale", None) == unit:
            continue
        violations.append(InvariantViolation(
            invariant="table_scale_declarations",
            severity="warning",
            message=(
                f"table {table.table_id} is headed "
                f"{' '.join(phrase.split())!r} but its cells carry no scale; "
                f"read literally every figure in it is out by "
                f"{factors.get(unit, 1_000):,}x"
            ),
            fields=["*"],
            spans=[document.span(table.start, min(table.end, table.start + 200))],
            observed=getattr(table, "scale", None),
            expected=unit,
        ))
    return violations


@invariant("nested_table_flattening", "F11_layout")
def _nested_tables(ctx: InvariantContext) -> list[InvariantViolation]:
    """One table rendered inside another reads as a single grid.

    The inner table's rows interleave with the outer one's, so a schedule
    parsed from the result is a mixture of two schedules and every row of it
    parses cleanly.
    """
    document = ctx.document
    if document is None:
        return []
    tables = sorted(getattr(document, "tables", []), key=lambda t: t.start)
    violations: list[InvariantViolation] = []
    for index, outer in enumerate(tables):
        for inner in tables[index + 1:]:
            if inner.start >= outer.end:
                break
            if inner.end > outer.end:
                continue
            violations.append(InvariantViolation(
                invariant="nested_table_flattening",
                severity="warning",
                message=(
                    f"table {inner.table_id} is rendered inside table "
                    f"{outer.table_id}; their rows interleave, and a schedule "
                    "read from the outer one mixes both"
                ),
                fields=["*"],
                spans=[document.span(inner.start, min(inner.end, inner.start + 200))],
                observed=inner.table_id, expected="one grid per table",
            ))
    return violations


# ---------------------------------------------------------------------------
# F09 -- definitional depth
# ---------------------------------------------------------------------------


@invariant("conflicting_definitions_across_documents", "F09_definitional_depth")
def _conflicting_definitions(ctx: InvariantContext) -> list[InvariantViolation]:
    """One term, two documents in the set, two different definitions.

    Ordinary in a loan-document package: the credit agreement and the security
    agreement each define Permitted Liens, and they do not always agree. A
    pipeline that resolves the term against whichever document it read last
    reports a definition that is right about half the time and never says so.
    """
    graph = ctx.definition_graph
    if graph is None:
        return []
    conflicts = getattr(graph, "cross_document_conflicts", None)
    if not callable(conflicts):
        return []
    violations: list[InvariantViolation] = []
    for conflict in conflicts():
        violations.append(InvariantViolation(
            invariant="conflicting_definitions_across_documents",
            message=(
                f"{conflict['term']!r} is defined differently in "
                f"{' and '.join(conflict['documents'])}; the operative meaning "
                "depends on which document governs and is not being guessed at"
            ),
            fields=[conflict["term"]],
            spans=conflict.get("spans", [])[:2],
            observed=conflict["documents"],
            expected="one definition, or a stated order of precedence",
        ))
    return violations
