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


def test_dated_as_of_is_no_longer_a_closing_date_rule():
    """It read the execution date of whatever instrument the sentence was
    about -- in an amendment, usually some other agreement's -- and every
    recital carries one."""
    from credit_extract.extract.passes import OFFLINE_RULES

    patterns = [
        getattr(r.pattern, "pattern", r.pattern)
        for r in OFFLINE_RULES if r.field == "closing_date"
    ]
    assert patterns, "closing_date should still have a rule"
    assert not any("dated as of" in p for p in patterns), (
        "the recital-date fallback manufactured 527 distinct dates across "
        "the corpus"
    )
