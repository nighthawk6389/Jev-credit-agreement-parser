"""Phase 2 model: variants, conditions, units, fiscal calendars, archetypes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.models.conditions import (
    ConditionSyntaxError, evaluate, parse_condition, referenced_variables,
)
from credit_extract.models.core import Condition, ExtractedField, Span, Variant
from credit_extract.models.fiscal import (
    CALENDAR_YEAR, FiscalCalendar, detect_fiscal_calendar,
)
from credit_extract.models.quantities import (
    Quantity, UnitMismatch, detect_scale, parse_quantity, quantity_for,
)

SPAN = Span(start=0, end=10, text="0123456789")


# ---------------------------------------------------------------------------
# Condition grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,state,expected",
    [
        ("ipo_completed == true", {"ipo_completed": True}, True),
        ("ipo_completed == true", {"ipo_completed": False}, False),
        ("junior_secured_debt > 25_000_000", {"junior_secured_debt": 30e6}, True),
        ("leverage <= 4.50", {"leverage": 4.5}, True),
        ("not (utilisation < 35%)", {"utilisation": 40}, True),
        ('facility in ["revolver", "delayed_draw"]', {"facility": "revolver"}, True),
        ('facility in ["revolver"]', {"facility": "term"}, False),
        ("date >= 2021-03-31", {"date": date(2021, 6, 30)}, True),
        ("date >= 2021-03-31", {"date": date(2020, 6, 30)}, False),
    ],
)
def test_conditions_evaluate(expr, state, expected):
    assert evaluate(expr, state) is expected


def test_a_missing_variable_is_undeterminable_not_false():
    """"We cannot tell" and "it does not hold" are different answers.

    Collapsing them makes a springing covenant look unconditionally inactive.
    """
    assert evaluate("ipo_completed == true", {}) is None


def test_a_definite_false_settles_an_and_despite_unknowns():
    assert evaluate("leverage > 9 and unknown_var == 1", {"leverage": 4.0}) is False


def test_a_definite_true_settles_an_or_despite_unknowns():
    assert evaluate("leverage < 9 or unknown_var == 1", {"leverage": 4.0}) is True


def test_an_unresolved_and_stays_unresolved():
    assert evaluate("leverage < 9 and unknown_var == 1", {"leverage": 4.0}) is None


@pytest.mark.parametrize(
    "bad",
    [
        '__import__("os").system("rm -rf /")',
        "obj.attr()",
        "1 + 1",
        "leverage <<< 4",
        "",
        "exec('x')",
    ],
)
def test_anything_outside_the_grammar_is_refused(bad):
    """Parsed, never eval'd: the grammar is the whole security boundary."""
    with pytest.raises(ConditionSyntaxError):
        parse_condition(bad)


def test_referenced_variables_says_what_the_caller_must_supply():
    assert referenced_variables("leverage <= 4.5 and not ipo_completed") == {
        "leverage", "ipo_completed"
    }


def test_a_condition_with_a_bad_expression_fails_at_construction():
    with pytest.raises(Exception):
        Condition(kind="state", expr="1 +")


# ---------------------------------------------------------------------------
# Variants and resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def stepdown_field() -> ExtractedField:
    """A covenant that steps down over time and converts on junior debt."""
    return ExtractedField[Decimal](
        precedence_basis="Section 6.12 conversion clause overrides the grid",
        variants=[
            Variant[Decimal](
                value=Decimal("5.00"), spans=[SPAN], status="confirmed",
                conditions=[Condition(
                    kind="state", expr="junior_secured_debt > 25_000_000",
                )],
            ),
            Variant[Decimal](
                value=Decimal("4.50"), spans=[SPAN], status="confirmed",
                effective_from=date(2021, 3, 31),
            ),
            Variant[Decimal](
                value=Decimal("6.50"), spans=[SPAN], status="confirmed",
                effective_to=date(2021, 3, 30),
            ),
        ],
    )


def test_resolution_is_executable(stepdown_field):
    assert stepdown_field.resolve(date(2020, 6, 30), {}).value == Decimal("6.50")
    assert stepdown_field.resolve(date(2021, 6, 30), {}).value == Decimal("4.50")


def test_a_condition_overrides_the_schedule_when_it_holds(stepdown_field):
    variant = stepdown_field.resolve(
        date(2021, 6, 30), {"junior_secured_debt": 30_000_000}
    )
    assert variant.value == Decimal("5.00")


def test_precedence_order_is_justified(stepdown_field):
    assert stepdown_field.precedence_basis, (
        "an ordering nobody can justify is a guess"
    )


def test_the_trace_says_why_each_variant_was_passed_over(stepdown_field):
    steps = stepdown_field.resolve_trace(date(2021, 6, 30), {})
    verdicts = [s.verdict for s in steps]
    assert verdicts == ["undeterminable", "governs", "out_of_window"]
    assert "junior_secured_debt" in steps[0].detail


def test_required_state_tells_the_caller_what_is_missing(stepdown_field):
    assert stepdown_field.required_state() == {"junior_secured_debt"}


def test_an_unconditional_single_variant_field_reads_as_a_scalar():
    field = ExtractedField[Decimal].single(
        value=Decimal("150500000"), spans=[SPAN], status="confirmed"
    )
    assert field.value == Decimal("150500000")
    assert field.is_conditional is False
    assert field.resolve(date(2030, 1, 1), {}).value == Decimal("150500000")


def test_provenance_still_binds_at_the_variant_level():
    with pytest.raises(ValueError, match="must cite at least one span"):
        Variant[Decimal](value=Decimal(1), status="confirmed")


def test_a_variant_window_must_not_run_backwards():
    with pytest.raises(ValueError, match="precedes effective_from"):
        Variant[Decimal](
            effective_from=date(2021, 1, 1), effective_to=date(2020, 1, 1)
        )


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_basis_points_and_percent_normalize_to_one_scale():
    assert (
        parse_quantity("50 bps").normalized().value
        == parse_quantity("0.50%").normalized().value
    )


def test_a_table_scale_multiplies_the_cells_beneath_it():
    """1,250 under "(in thousands)" is $1,250,000 and the cell does not say so."""
    unit, phrase = detect_scale("Commitments (in thousands, except per share)")
    assert unit == "USD_thousands"
    quantity = parse_quantity("1,250", prefer="money", scale=unit,
                              scale_source=phrase)
    assert quantity.normalized().value == Decimal(1_250_000)
    assert quantity.scale_source == phrase


def test_millions_scale_is_recognised():
    assert detect_scale("(dollars in millions)")[0] == "USD_millions"


def test_incomparable_units_refuse_to_compare():
    with pytest.raises(UnitMismatch):
        parse_quantity("50 bps").compare(parse_quantity("$100"))


def test_a_date_is_not_a_quantity():
    """Regression: "August 1, 2024" parsed as the money value 1."""
    assert parse_quantity("August 1, 2024", prefer="date") is None
    assert quantity_for("2024-08-01", "date") is None


def test_a_multiple_is_distinguished_from_a_bare_ratio():
    assert parse_quantity("4.50x").unit == "multiple_of_ebitda"
    assert parse_quantity("4.50:1.00").unit == "ratio"


# ---------------------------------------------------------------------------
# Fiscal calendars
# ---------------------------------------------------------------------------


def test_the_default_is_calendar_quarters_and_says_so():
    calendar = detect_fiscal_calendar("nothing about calendars here")
    assert calendar.is_calendar_year
    assert "silent" in calendar.source, (
        "a default that does not announce itself becomes a silent assumption"
    )


def test_a_52_53_week_calendar_is_detected():
    calendar = detect_fiscal_calendar(
        "The Borrower operates on a 52/53-week fiscal year ending on the "
        "Saturday nearest to January 31."
    )
    assert calendar.is_52_53_week
    assert calendar.anchor_weekday == 5            # Saturday
    assert calendar.year_end_month == 1


def test_fiscal_quarters_are_evenly_spaced_where_days_are_not():
    """The reason the spacing invariant counts quarters and not days."""
    calendar = detect_fiscal_calendar(
        "a 52/53-week fiscal year ending on the Saturday nearest to January 31"
    )
    ends = calendar.quarter_ends(2024)
    day_gaps = [(ends[i + 1] - ends[i]).days for i in range(3)]
    quarter_gaps = [
        calendar.quarter_index(ends[i + 1]) - calendar.quarter_index(ends[i])
        for i in range(3)
    ]
    assert len(set(day_gaps)) > 1, "expected uneven day spacing under 52/53"
    assert quarter_gaps == [1, 1, 1]


def test_a_stated_year_end_is_read():
    calendar = detect_fiscal_calendar("The fiscal year ends June 30 of each year.")
    assert (calendar.year_end_month, calendar.year_end_day) == (6, 30)
    assert not calendar.is_calendar_year


def test_calendar_year_quarters_are_the_usual_ones():
    assert CALENDAR_YEAR.quarter_ends(2018) == [
        date(2018, 3, 31), date(2018, 6, 30),
        date(2018, 9, 30), date(2018, 12, 31),
    ]


def test_a_52_53_week_calendar_needs_its_anchor():
    with pytest.raises(ValueError, match="anchor weekday"):
        FiscalCalendar(is_52_53_week=True)
