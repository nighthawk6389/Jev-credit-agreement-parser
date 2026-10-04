"""Validator A's thresholds, fitted on real filings against wrong values.

The fit needs failures, and the real filings hold almost none: two wrong
values among a hundred A asked about. So the fit asks A about wrong values on
purpose, and everything here pins the one property that makes those answers
worth fitting on -- each is A's own question about the same text, differing
from the real one only in the value.

The first version of the fit did not have that property. It built each wrong
value's question from a copy of the field with the value replaced, and a
field's value lives on its primary variant, so every "wrong" question asked
about the right value. A read as a scorer that could not tell a right figure
from any other until one question was sent by itself.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from credit_extract.eval import realfit as R
from credit_extract.eval.realfit import ProbingBackend, Row, fit_class
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import validators as V
from credit_extract.validate.jev import Decision, JevResult, Noul

MATURITY = FIELD_REGISTRY["revolver.maturity_date"]
MARGIN = FIELD_REGISTRY["applicable_margin.eurodollar_top_level_pct"]
SIZE = FIELD_REGISTRY["revolver.commitment"]

STATE = (
    '"Applicable Margin" means 2.50% per annum for Term SOFR Loans and 1.50% '
    'per annum for Base Rate Loans, plus 2.00% during an Event of Default. '
    '"Maturity Date" means July 30, 2030. "Closing Date" means July 30, 2025. '
    'The Facility Amount is $410,000,000, and the Swingline Sublimit is '
    '$25,000,000.'
)


def _field(spec, value):
    text = str(value)
    return ExtractedField.single(
        value=value, status="confirmed", field_class=spec.field_class,
        spans=[Span(start=0, end=len(text), text=text)],
    )


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------


def test_a_wrong_value_is_asked_in_validator_a_s_own_words():
    for spec, right, wrong in ((MATURITY, date(2030, 7, 30), date(2031, 7, 30)),
                               (MARGIN, Decimal("2.50"), Decimal("1.50")),
                               (SIZE, Decimal("410000000"), Decimal("25000000"))):
        field = _field(spec, right)
        assert V.support_statement(right, spec) == V._support_statement(field, spec)
        asked = V.support_statement(wrong, spec)
        assert asked != V._support_statement(field, spec)
        assert asked == V._support_statement(_field(spec, wrong), spec)


def test_a_copy_of_a_field_with_its_value_replaced_still_holds_the_old_value():
    """Why the fit does not build questions from copies of fields."""
    field = _field(MARGIN, Decimal("2.50"))
    copy = field.model_copy(update={"value": Decimal("1.50")})
    assert copy.value == Decimal("2.50")


# ---------------------------------------------------------------------------
# The wrong values
# ---------------------------------------------------------------------------


def test_in_text_near_misses_are_the_other_figures_nearest_first():
    assert R.in_text(STATE, MARGIN, "2.50%") == [
        Decimal("1.50"), Decimal("2.00")]
    assert R.in_text(STATE, MATURITY, date(2030, 7, 30)) == [date(2025, 7, 30)]
    assert R.in_text(STATE, SIZE, "$410,000,000") == [Decimal("25000000")]


def test_in_text_near_misses_stop_at_the_limit_and_skip_repeats():
    state = "rates of 1.00%, 1.00%, 2.00%, 3.00%, 4.00% and 5.00%"
    assert R.in_text(state, MARGIN, "5.00%", limit=2) == [
        Decimal("4.00"), Decimal("3.00")]
    assert R.in_text(state, MARGIN, "9.00%") == [
        Decimal("2.00"), Decimal("3.00"), Decimal("1.00")]


def test_off_text_values_are_the_right_one_moved_where_the_text_is_not():
    assert R.off_text(STATE, MATURITY, date(2030, 7, 30)) == [
        date(2031, 7, 30), date(2029, 7, 30), date(2032, 7, 30)]
    # 2.50 + 1.00 would be 3.50, absent; but 2.50 * 2 is 5.00, absent too.
    assert R.off_text(STATE, MARGIN, "2.50%") == [
        Decimal("2.75"), Decimal("3.50"), Decimal("5.00")]
    moved = R.off_text("a floor of 0.25% and a fee of 0.50%", MARGIN, "0.25%")
    assert Decimal("0.50") not in moved          # the text has it
    assert R.off_text(STATE, SIZE, "$410,000,000") == [
        Decimal("435000000"), Decimal("820000000"), Decimal("205000000")]


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


class Scripted:
    """A scorer that believes only the right margin and the right maturity."""

    name = "jev-test"

    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    def ask(self, state, questions):
        self.sent.append([q.name for q in questions])
        right = ("2.5%", "2030-07-30")
        return JevResult(backend=self.name, questions_asked=len(questions), decisions={
            q.name: Decision(name=q.name, kind="noul", probability=(
                0.9 if any(r in q.statement for r in right) else 0.1))
            for q in questions
        })


def test_the_probe_rides_on_a_s_request_and_returns_only_what_was_asked():
    inner = Scripted()
    probe = ProbingBackend(inner, {MARGIN.name: "2.50%"})
    asked = Noul(name=MARGIN.name, statement=V.support_statement(Decimal("2.50"), MARGIN))
    other = Noul(name="present:fee", statement="This agreement contains a fee.")

    result = probe.ask(STATE, [asked, other])

    assert set(result.decisions) == {MARGIN.name, "present:fee"}
    assert result[MARGIN.name].confidence == 0.9
    assert len(inner.sent) == 1                  # one request, not two
    assert any("#in_text" in n for n in inner.sent[0])
    assert any("#off_text" in n for n in inner.sent[0])
    kinds = {(f["kind"], f["value"]): f["probability"] for f in probe.found}
    assert kinds[("in_text", "1.50")] == 0.1
    assert kinds[("off_text", "2.75")] == 0.1


def test_the_probe_leaves_other_questions_alone():
    inner = Scripted()
    probe = ProbingBackend(inner, {MARGIN.name: "2.50%"})
    unlabelled = Noul(name=MATURITY.name,
                      statement=V.support_statement(date(2030, 7, 30), MATURITY))
    presence = Noul(name=MARGIN.name, statement="This agreement states a margin.")

    probe.ask(STATE, [unlabelled])
    probe.ask(STATE, [presence])

    assert inner.sent == [[MATURITY.name], [MARGIN.name]]
    assert probe.found == []


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def _rows(cls, kind, probabilities, correct, side="fit"):
    return [Row(f"doc{i}", side, "f", cls, kind, p, correct, "v", "e")
            for i, p in enumerate(probabilities)]


def test_a_threshold_every_fit_agrees_on_is_adopted():
    rows = (_rows("dates", "labelled", [0.95, 0.9, 0.86, 0.82], True)
            + _rows("dates", "labelled", [0.05], False)
            + _rows("dates", "off_text", [0.05, 0.1], False)
            + _rows("dates", "in_text", [0.3, 0.2], False)
            + _rows("dates", "labelled", [0.84, 0.7], True, side="holdout"))
    fit = fit_class(rows, "dates", current=0.88)
    assert fit.fitted == {"labelled": 0.82, "+off-text": 0.82, "+in-text": 0.82}
    assert fit.adopted == 0.82
    assert fit.reason == "all three fits agree"
    # Reported on the holdout side only: one of its two right values clears.
    assert (fit.metrics.n_holdout, fit.metrics.n_accepted) == (2, 1)


def test_a_threshold_that_moves_with_the_near_misses_is_kept():
    """Counting the in-text near misses moves the fit from 0.50 to 0.98, so
    the answer depends on how often the reader makes that mistake."""
    rows = (_rows("economic_terms", "labelled", [0.98] * 10 + [0.6, 0.55, 0.5], True)
            + _rows("economic_terms", "labelled", [0.2], False)
            + _rows("economic_terms", "off_text", [0.05, 0.1], False)
            + _rows("economic_terms", "in_text", [0.7, 0.65, 0.62, 0.58], False))
    fit = fit_class(rows, "economic_terms", current=0.77)
    assert fit.fitted == {"labelled": 0.5, "+off-text": 0.5, "+in-text": 0.98}
    assert fit.adopted is None
    assert "disagree" in fit.reason
    assert fit.metrics.threshold == 0.77
