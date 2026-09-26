"""The definitions tier: a defined term is settled where it is defined.

``closing_date`` was the worst field in the corpus and not because it is hard.
The rule meant to read it matched 3 of 100 agreements; the fallback meant to
catch the rest read "dated as of", which every amendment recital carries, and
manufactured 527 distinct dates corpus-wide -- all tagged ``deterministic:``,
which is a flat 0.95 and the top of the ranking key. So a recital outranked
the definitions article, and a document whose text says
``" Closing Date ": June 25, 2018.`` reported 2019-11-26.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.extract.definitions import (
    CONFIDENCE, MAX_BODY_CHARS, _sole_value, definition_candidates,
)
from credit_extract.models.core import Span
from credit_extract.models.fpml_model import FIELD_REGISTRY


class _Node:
    def __init__(self, term: str, body: str, start: int = 0) -> None:
        self.term = term
        self.body = body
        self.span = Span(start=start, end=start + max(1, len(body)), text=body)


class _Graph:
    """Enough DefinitionGraph to drive the tier."""

    def __init__(self, nodes: dict[str, _Node]) -> None:
        self.nodes = nodes

    def resolve(self, term: str) -> str | None:
        if term in self.nodes:
            return term
        folded = term.casefold()
        return next((n for n in self.nodes if n.casefold() == folded), None)

    def get(self, term: str) -> _Node | None:
        return self.nodes.get(term)


class _Doc:
    text = "x" * 4000


def _spec(name: str):
    return [FIELD_REGISTRY[name]]


def _graph_with(body: str, term: str = "Closing Date") -> _Graph:
    return _Graph({term: _Node(term, body)})


# ---------------------------------------------------------------------------
# It reads the definition
# ---------------------------------------------------------------------------


def test_a_date_stated_in_its_definition_is_the_value():
    graph = _graph_with('" Closing Date ": June 25, 2018.')
    found = definition_candidates(_Doc(), graph, _spec("closing_date"))

    assert len(found) == 1
    assert found[0].value == date(2018, 6, 25)
    assert found[0].confidence == CONFIDENCE
    assert found[0].pass_id == "deterministic:definitions"


def test_the_candidate_cites_the_definition_span():
    """A value the reader cannot take back to the clause that settles it is an
    opinion. The span is the definition's, not a mention's."""
    graph = _graph_with('" Closing Date ": June 25, 2018.')
    found = definition_candidates(_Doc(), graph, _spec("closing_date"))

    assert found[0].span is not None
    assert found[0].span.start == graph.get("Closing Date").span.start


def test_the_colon_form_is_read_as_well_as_means():
    """The rule it replaces matched ``"Closing Date" means``, which 3 of the
    100 harvested agreements use. Most write a colon."""
    for body in (
        '" Closing Date ": June 25, 2018.',
        '"Closing Date" means June 25, 2018.',
        "Closing Date: June 25, 2018",
    ):
        found = definition_candidates(_Doc(), _graph_with(body), _spec("closing_date"))
        assert found and found[0].value == date(2018, 6, 25), body


# ---------------------------------------------------------------------------
# It declines rather than guesses
# ---------------------------------------------------------------------------


def test_a_closing_date_that_is_an_event_yields_nothing():
    """Many of these agreements define the Closing Date as the date conditions
    are satisfied. Several labels assert exactly that, and a rule that
    produced a date for them would be manufacturing one."""
    graph = _graph_with(
        '" Closing Date ": the date on which the conditions precedent set '
        "forth in Section 4.1 are satisfied or waived."
    )
    assert definition_candidates(_Doc(), graph, _spec("closing_date")) == []


def test_a_definition_carrying_two_dates_settles_nothing():
    """Guessing between them would inherit the deterministic tier's 0.95,
    which is the failure this module exists to fix."""
    graph = _graph_with(
        '" Closing Date ": June 25, 2018, as amended on November 26, 2019.'
    )
    assert definition_candidates(_Doc(), graph, _spec("closing_date")) == []


def test_the_same_date_twice_is_still_one_answer():
    """Repetition inside one definition is not disagreement."""
    graph = _graph_with('" Closing Date ": June 25, 2018 (the June 25, 2018 date).')
    found = definition_candidates(_Doc(), graph, _spec("closing_date"))
    assert found and found[0].value == date(2018, 6, 25)


def test_a_body_too_long_to_be_a_definition_is_skipped():
    """A defined term whose body runs to thousands of characters is a section
    with a name, and the first date in it is not "the" value."""
    graph = _graph_with("June 25, 2018. " + "filler " * MAX_BODY_CHARS)
    assert definition_candidates(_Doc(), graph, _spec("closing_date")) == []


def test_a_term_the_document_does_not_define_yields_nothing():
    assert definition_candidates(_Doc(), _Graph({}), _spec("closing_date")) == []


def test_no_graph_is_not_a_crash():
    assert definition_candidates(_Doc(), None, _spec("closing_date")) == []


# ---------------------------------------------------------------------------
# F07: the benchmark floor, and the shape that looks like one
# ---------------------------------------------------------------------------


def test_a_floor_stated_in_its_definition_is_read():
    """"LIBO Rate" resolved as a defined term in 0 of 100 harvested
    agreements, so this field's definitional route was dead and the prose rule
    matched a phrase no document contains. "Floor" resolves in 35."""
    graph = _graph_with(
        "a rate of interest equal to one-half of one percent (0.50%) per annum.",
        term="Floor",
    )
    found = definition_candidates(_Doc(), graph, _spec("libor_floor_pct"))

    assert found and found[0].value == Decimal("0.50")


def test_a_per_tranche_floor_at_the_same_rate_is_one_answer():
    """" Floor ": (a) with respect to the Initial Term Loans, 0.00% per annum
    and (b) with respect to the Revolving Loans, 0.00% per annum.' Two
    percentages, one value -- which is the common shape and must not be
    refused as a disagreement."""
    graph = _graph_with(
        "(a) with respect to the Initial Term Loans, 0.00% per annum and "
        "(b) with respect to the Revolving Loans, 0.00% per annum.",
        term="Floor",
    )
    found = definition_candidates(_Doc(), graph, _spec("libor_floor_pct"))

    assert found and found[0].value == Decimal("0.00")


def test_a_floor_defined_by_reference_to_the_agreement_is_refused():
    """The majority shape after the usable one: 13 of 100 agreements define
    Floor as whatever floor the agreement provides. That is a circularity, not
    a rate, and the bodies carry percentages from the transition mechanics
    around them -- so without the guard the tier emits one at 0.90 from the
    definitions article, the most authoritative-looking wrong answer going.
    """
    graph = _graph_with(
        "the benchmark rate floor, if any, provided in this Agreement "
        "initially (as of the execution of this Agreement, the modification, "
        "amendment or renewal of this Agreement, 0.50%)",
        term="Floor",
    )
    assert definition_candidates(_Doc(), graph, _spec("libor_floor_pct")) == []


def test_the_csa_is_not_anchored_on_the_fallback_machinery():
    """"Benchmark Replacement Adjustment" resolves in 18 of 50 sampled
    agreements and carries a percentage in none of them: it is what computes
    an adjustment on transition, not a rate anybody pays. Anchoring there
    would hand a criticality-5 field a number from a mechanism."""
    anchors = FIELD_REGISTRY["accrual.credit_spread_adjustment_pct"].definition_anchors

    assert "Benchmark Replacement Adjustment" not in anchors
    assert "Term SOFR Adjustment" in anchors


def test_the_floor_anchors_name_terms_the_corpus_actually_defines():
    anchors = FIELD_REGISTRY["libor_floor_pct"].definition_anchors
    assert "Floor" in anchors, "the term 35 of 100 agreements define"
    assert anchors[0] == "Floor", "the one that resolves should be tried first"


# ---------------------------------------------------------------------------
# It generalises past dates
# ---------------------------------------------------------------------------


def test_the_tier_is_not_date_specific():
    """The principle is about defined terms, not about dates: whatever kind
    the field declares is what gets parsed out of its definition."""
    assert _sole_value('"Applicable Margin": 2.75% per annum.', "percent") == (
        Decimal("2.75"), "2.75%"
    )
    assert _sole_value("means $1,300,000,000 in the aggregate.", "money")[0] == (
        Decimal("1300000000")
    )
    assert _sole_value("shall be 3.50 to 1.00 at all times.", "ratio") is not None


def test_a_kind_with_no_parser_is_declined_not_guessed():
    assert _sole_value("any text at all", "text") is None
    assert _sole_value("any text at all", "bool") is None


# ---------------------------------------------------------------------------
# The rule it replaces is gone
# ---------------------------------------------------------------------------


def test_the_recital_date_fallback_ranks_below_the_definitions_tier():
    """It was deleted for one commit and the corpus said that was too blunt:
    19 assertions fixed, 14 broken, 11 of them by reporting nothing where the
    recital date had been right. So it stays, at a confidence below the
    definitions tier rather than the 0.70 it had.

    The definition cannot arbitrate its use either. "Defined but states no
    date" covers StepStone, whose closing date IS the cover date, and Janus,
    whose closing date is an event -- so presence of a definition does not
    predict which answer is wanted, and telling them apart is judgement.
    """
    from credit_extract.extract.passes import OFFLINE_RULES

    rules = [r for r in OFFLINE_RULES if r.field == "closing_date"]
    recital = [
        r for r in rules
        if "dated as of" in getattr(r.pattern, "pattern", r.pattern)
    ]
    assert recital, "the cover-date fallback earns its place on 11 documents"
    assert recital[0].confidence < CONFIDENCE, (
        "a recital date must not outrank a value read from the term's own "
        "definition"
    )


def test_a_value_from_the_definition_outranks_a_deterministic_rival():
    """The ranking, not the confidence, is what fixed closing_date. Eleven
    candidates all matched deterministically at 0.95, so before definition
    precedence the winner was effectively arbitrary -- and on Essential
    Properties it was 2019-11-26."""
    from credit_extract.extract.passes import Candidate
    from credit_extract.extract.reconcile import reconcile

    span_def = Span(start=100, end=140, text="x" * 40)
    span_recital = Span(start=900, end=940, text="y" * 40)
    candidates = [
        Candidate(
            field="closing_date", value=date(2019, 11, 26), span=span_recital,
            confidence=0.95, pass_id="deterministic:rules",
            segmentation="structural",
        ),
        Candidate(
            field="closing_date", value=date(2018, 6, 25), span=span_def,
            confidence=0.90, pass_id="deterministic:definitions",
            segmentation="definitional",
        ),
    ]
    result = reconcile(candidates, specs={"closing_date": FIELD_REGISTRY["closing_date"]})

    assert result.fields["closing_date"].value == date(2018, 6, 25), (
        "the definition must win despite the lower confidence"
    )


def test_definition_precedence_needs_a_definition_candidate():
    """The flag is about where a value was read, not about the segmentation
    it happened to arrive on."""
    from credit_extract.extract.passes import Candidate
    from credit_extract.extract.reconcile import ValueGroup

    plain = ValueGroup(key="k", value=1, candidates=[Candidate(
        field="closing_date", value=1, span=Span(start=0, end=4, text="abcd"),
        confidence=0.9, pass_id="deterministic:rules",
        segmentation="definitional",
    )])
    assert not plain.from_definition
