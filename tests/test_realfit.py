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
from credit_extract.eval.realfit import ProbingBackend, Row, fit_class, weighted_threshold
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


def test_where_the_fits_disagree_the_measured_error_rate_decides():
    """Counting the in-text near misses moves the fit from 0.50 to 0.98, so
    the answer turns on how often the reader hands A a wrong figure. The
    labels measure that, and its 95% upper bound is what the threshold must
    survive: one wrong in fourteen bounds the rate at about 31%, and only
    0.98 survives that; one in two hundred bounds it near 2.8%, and 0.70
    does -- below it, three of the four near misses clear and the expected
    precision is 0.98."""
    near = (_rows("economic_terms", "off_text", [0.05, 0.1], False)
            + _rows("economic_terms", "in_text", [0.7, 0.65, 0.62, 0.58], False))
    few = (_rows("economic_terms", "labelled", [0.98] * 10 + [0.6, 0.55, 0.5], True)
           + _rows("economic_terms", "labelled", [0.2], False) + near)
    fit = fit_class(few, "economic_terms", current=0.77)
    assert fit.fitted == {"labelled": 0.5, "+off-text": 0.5, "+in-text": 0.98}
    assert fit.prevalence == (1, 14)
    assert fit.adopted == 0.98
    assert "disagree" in fit.reason and "1 times in 14" in fit.reason

    many = (_rows("economic_terms", "labelled", [0.98] * 189 + [0.6] * 10, True)
            + _rows("economic_terms", "labelled", [0.2], False) + near)
    fit = fit_class(many, "economic_terms", current=0.77)
    assert fit.prevalence == (1, 200)
    assert fit.adopted == 0.7


def test_the_measured_rate_never_lets_a_value_the_text_lacks_through():
    rows = (_rows("economic_terms", "labelled", [0.98] * 189 + [0.6] * 10, True)
            + _rows("economic_terms", "labelled", [0.2], False)
            + _rows("economic_terms", "off_text", [0.62], False)
            + _rows("economic_terms", "in_text", [0.7, 0.65, 0.58], False))
    fit = fit_class(rows, "economic_terms", current=0.77)
    assert fit.adopted is not None and fit.adopted > 0.62


def test_a_wrong_value_the_reader_really_read_counts_as_a_mistake():
    """A labelled value the reader got wrong is a mistake A must turn down,
    like a near miss asked on purpose, and it can score higher than any of
    them: here it holds the threshold above 0.93 where the near misses alone
    would settle at 0.90."""
    rows = (_rows("economic_terms", "labelled", [0.98] * 189 + [0.9] * 10, True)
            + _rows("economic_terms", "labelled", [0.93], False)
            + _rows("economic_terms", "in_text", [0.5], False)
            + _rows("economic_terms", "off_text", [0.05], False))
    assert weighted_threshold(rows, 0.99, rate=0.028)[0] == 0.98
    near_misses_only = [r for r in rows if r.correct or r.kind != "labelled"]
    assert weighted_threshold(near_misses_only, 0.99, rate=0.028)[0] == 0.9


def test_the_held_out_report_says_what_the_reader_itself_gave():
    """Near misses asked on purpose are the fit's stress, not answers anyone
    receives; the report keeps the reader's own held-out values apart."""
    rows = (_rows("economic_terms", "labelled", [0.9, 0.7, 0.5], True, side="holdout")
            + _rows("economic_terms", "labelled", [0.65], False, side="holdout")
            + _rows("economic_terms", "in_text", [0.95], False, side="holdout"))
    assert R.held_out_reading(rows, "economic_terms", 0.6) == (
        "the reader's own held-out values: 2 of 3 right and 1 of 1 wrong cleared")


def test_a_label_that_withholds_the_value_makes_any_value_wrong():
    """Accelevation's joinder labels its revolving commitment needs_review: the
    amount in force is printed nowhere. Whatever the reader hands A for it is
    a mistake, so the field is sampled with the labelled ones."""
    from pathlib import Path

    from credit_extract.eval.assertions import load_assertion_file
    from credit_extract.eval.families import load_families

    path = Path(R.__file__).parent / "labels" / "accelevation_joinder_2026.yaml"
    labelled = R._labelled(load_assertion_file(path, load_families()))
    assert [a.expect for a in labelled["revolver.commitment"]] == ["needs_review"]


def test_a_term_tranches_maturity_is_asked_as_the_revolvers():
    """Easterly's term facility maturity was confirmed as a revolver's out of
    sample. The probe asks A about each term tranche's maturity in the
    revolver's words, against the definition that sets it."""
    from types import SimpleNamespace
    from credit_extract.ingest.normalize import NormalizedDocument

    text = ('ARTICLE I DEFINITIONS\n\n" Closing Date " means March 1, 2025.\n\n'
            '" Term Loan Maturity Date " means the date that is five (5) years '
            'after the Closing Date.\n\n" Revolving Credit Maturity Date " means '
            'March 1, 2029.\n\nARTICLE II THE LOANS\n')
    doc = NormalizedDocument(document_id="t", source_path="t", source_format="txt", text=text)
    inner = Scripted()
    rows = R.other_facility("t", "fit", SimpleNamespace(document=doc),
                            date(2029, 3, 1), inner)
    assert [(r.kind, r.value, r.correct) for r in rows] == [
        ("other_facility", "2030-03-01", False)]
    assert inner.sent == [["revolver.maturity_date#other_facility"]]
