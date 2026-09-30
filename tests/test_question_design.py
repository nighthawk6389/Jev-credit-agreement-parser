"""What the validators ask, pinned to the wording the live scorer needed.

The first live pass (docs/jev_live_pass.md) confirmed 54 wrong values. Most of
those did not come from a bad threshold. They came from a question a literal
reader answers differently from the way its author meant it. The offline
stand-in counts words, so it never exposed any of these. Every test here pins
one rewording, or one fix the rewording exposed, so that none of them can
drift back unnoticed.

The scorer below is scripted: each answer is written into the test. What is
tested is what the validators *ask* and what they *do* with the answer. What
the live model answers is a measurement, and it lives in the docs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

import pytest

from credit_extract.ingest.segment import Chunk
from credit_extract.models.core import ConflictRecord, ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import Thresholds
from credit_extract.validate.jev import (
    ChoiceQ, Decision, JevResult, JevSession, Noul, OfflineJev,
)

MARGIN = "applicable_margin.eurodollar_top_level_pct"
AGENT = "administrative_agent.legal_name"


class ScriptedJev:
    """A scorer whose answers the test writes. It records every question.

    ``nouls`` maps a question name to a probability, or to a function of the
    state when the answer differs from chunk to chunk. ``choices`` maps a
    question name to the distribution it returns.
    """

    name = "jev-scripted"

    def __init__(
        self,
        nouls: dict[str, float | Callable[[str], float]] | None = None,
        choices: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.nouls = nouls or {}
        self.choices = choices or {}
        self.asked: list[tuple[str, list[Any]]] = []

    def ask(self, state: str, questions: list[Any]) -> JevResult:
        self.asked.append((state, list(questions)))
        decisions: dict[str, Decision] = {}
        for q in questions:
            if isinstance(q, Noul):
                answer = self.nouls[q.name]
                decisions[q.name] = Decision(
                    name=q.name, kind="noul", backend=self.name,
                    probability=answer(state) if callable(answer) else answer,
                )
            else:
                dist = self.choices[q.name]
                decisions[q.name] = Decision(
                    name=q.name, kind="choice", backend=self.name,
                    choice=max(dist, key=dist.get), distribution=dist,
                )
        return JevResult(
            decisions=decisions, backend=self.name,
            questions_asked=len(questions),
        )

    @property
    def questions(self) -> list[Any]:
        return [q for _, batch in self.asked for q in batch]


class _Text:
    """A document that is only its text: enough for a ValidationContext."""

    def __init__(self, text: str = "x") -> None:
        self.text = text

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end]


def _chunk(text: str, start: int, chunk_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id, segmentation="structural", label=chunk_id,
        spans=[Span(start=start, end=start + len(text), text=text)],
        text=text,
    )


def _context(
    scorer: ScriptedJev, fields: dict[str, ExtractedField], *,
    doc: _Text | None = None, chunks: list[Chunk] | None = None,
    **per_class: float,
) -> V.ValidationContext:
    return V.ValidationContext(
        doc=doc or _Text(), chunks=chunks or [], fields=fields,
        session=JevSession(scorer),
        thresholds=Thresholds(
            version="t", backend=scorer.name,
            per_class={k.replace("__", "/"): v for k, v in per_class.items()},
        ),
    )


# ---------------------------------------------------------------------------
# Validator C: presence is asked, absence is computed
# ---------------------------------------------------------------------------

PRICING = "Applicable Rate: Term SOFR Loans 2.75% / 2.50% / 2.25% per annum."
COVENANTS = "The Borrower shall not permit the Total Leverage Ratio to exceed 4.00."
NOTICES = "All notices shall be in writing and delivered to the addresses below."
CHUNKS = [
    _chunk(PRICING, 0, "pricing"),
    _chunk(COVENANTS, 1_000, "covenants"),
    _chunk(NOTICES, 2_000, "notices"),
]


def _run_c(presence: dict[str, float]) -> tuple[ExtractedField, dict, ScriptedJev]:
    scorer = ScriptedJev(nouls={MARGIN: lambda state: presence[state]})
    fields = {MARGIN: ExtractedField.single(value=None, status="needs_review")}
    ctx = _context(scorer, fields, chunks=CHUNKS)
    absence = V.validator_c_negative_space(ctx)
    return fields[MARGIN], absence, scorer


def test_c_asks_each_chunk_whether_it_addresses_the_term():
    """The negation used to be the question: "this text contains no provision
    addressing the highest Eurodollar Applicable Margin". A literal reader
    agrees with that of any chunk that does not use the question's own
    words, however plainly the chunk states the term. A Term SOFR pricing
    grid scored as not containing one on 12 of 15 documents, and those
    documents were reported as having no margin."""
    _, _, scorer = _run_c({PRICING: 0.9, COVENANTS: 0.1, NOTICES: 0.1})

    statement = FIELD_REGISTRY[MARGIN].presence_statement.replace(
        "This agreement", "This text"
    )
    assert len(scorer.asked) == len(CHUNKS)
    for question in scorer.questions:
        assert question.statement == statement
        assert question.polarity == "affirmative"
        assert question.statement.startswith("This text contains a provision")


@pytest.mark.parametrize("name", sorted(FIELD_REGISTRY))
def test_no_field_asks_absence_as_a_negation(name):
    """The mfn_sunset question was the one field with its own wording, and it
    was a negation as well."""
    statement = FIELD_REGISTRY[name].presence_statement
    assert "contains no" not in statement
    assert " no provision" not in statement


def test_absence_is_one_minus_the_strongest_presence_in_any_chunk():
    """One chunk carrying the term outweighs every chunk that does not. The
    chunk that carries it is also where a reviewer should start."""
    field, absence, _ = _run_c({PRICING: 0.40, COVENANTS: 0.05, NOTICES: 0.10})

    assert absence == {MARGIN: 0.6}
    assert field.status == "needs_review"          # 0.60 < 0.80
    assert field.validation_confidence == 0.6
    assert field.review_hint == CHUNKS[0].span
    (event,) = field.trace
    assert event.question == FIELD_REGISTRY[MARGIN].presence_statement
    assert "one minus the strongest presence" in event.notes


def test_a_term_no_chunk_addresses_is_confirmed_absent():
    field, absence, _ = _run_c({PRICING: 0.08, COVENANTS: 0.03, NOTICES: 0.02})

    assert absence == {MARGIN: 0.92}
    assert field.status == "absent_from_document"
    assert "across 3 swept chunk(s)" in field.notes


# ---------------------------------------------------------------------------
# Validator A: a name is asked as a name
# ---------------------------------------------------------------------------


def _agent(value: str) -> ExtractedField:
    return ExtractedField.single(
        value=value, status="confirmed", field_class="parties",
        spans=[Span(start=10, end=10 + len(value), text=value)],
    )


def test_a_party_is_asked_whether_the_value_is_its_legal_name():
    """"The text supports a value of Loan Documents for the Administrative
    Agent" is true of any text that mentions the role. On those terms a
    phrase lifted from beside the agent's name was confirmed as its name.
    Asked whether the value *is* the institution's legal name, the live
    scorer gave true names 0.89-0.98 and such phrases 0.02-0.31."""
    statement = V._support_statement(_agent("Loan Documents"), FIELD_REGISTRY[AGENT])

    assert statement == (
        'In this text, "Loan Documents" is the legal name of the institution '
        "acting as Administrative Agent."
    )


@pytest.mark.parametrize(
    "name", sorted(n for n in FIELD_REGISTRY if n.endswith(".legal_name"))
)
def test_every_party_description_reads_as_a_legal_name(name):
    """The name form reads "... is {description}." That is a sentence only
    when the description names a legal name, not a role."""
    assert FIELD_REGISTRY[name].description.startswith("the legal name of ")


def test_values_other_than_names_keep_the_support_form():
    field = ExtractedField.single(
        value=Decimal("2.75"), status="confirmed",
        spans=[Span(start=0, end=5, text="2.75%")],
    )

    assert V._support_statement(field, FIELD_REGISTRY[MARGIN]) == (
        f"The text supports a value of 2.75% for "
        f"{FIELD_REGISTRY[MARGIN].description}."
    )


def test_validator_a_sends_the_name_form():
    text = "JPMorgan Chase Bank, N.A., as Administrative Agent under the Loan Documents"
    field = ExtractedField.single(
        value="Loan Documents", status="confirmed", field_class="parties",
        spans=[Span(start=60, end=74, text="Loan Documents")],
    )
    scorer = ScriptedJev(nouls={AGENT: 0.05})
    ctx = _context(scorer, {AGENT: field}, doc=_Text(text))

    V.validator_a_span_support(ctx)

    (question,) = scorer.questions
    assert question.statement.startswith('In this text, "Loan Documents" is ')
    assert field.status == "needs_review"


# ---------------------------------------------------------------------------
# Validator E: one claim, about this field, declared an absence
# ---------------------------------------------------------------------------

FEE = "commitment_fee_pct"
FEE_SENTENCE = (
    "The Borrower shall pay a commitment fee on the unused commitment at a "
    "rate of 0.25% per annum, calculated as set forth in Schedule 2.09. "
)


def _fee_context(external: float) -> tuple[ExtractedField, ScriptedJev, V.ValidationContext]:
    at = FEE_SENTENCE.index("0.25%")
    field = ExtractedField.single(
        value=Decimal("0.25"), status="confirmed",
        spans=[Span(start=at, end=at + 5, text="0.25%")],
    )
    scorer = ScriptedJev(
        nouls={"external": external, "omitted": 0.1, "by_design": 0.9}
    )
    ctx = _context(scorer, {FEE: field}, doc=_Text(FEE_SENTENCE))
    return field, scorer, ctx


def test_e_asks_whether_this_fields_amount_is_set_by_the_named_document():
    """The old statement never said which limit "this limit" was. It also
    assumed that the named document sits outside the agreement, which is
    false of a schedule. A literal reader accepted that premise, and the
    validator fired on add-back caps the agreement states."""
    _, scorer, ctx = _fee_context(external=0.05)
    V.validator_e_external_dependency(ctx)

    question = next(q for q in scorer.questions if q.name == "external")
    assert question.statement == (
        "The amount of the unused commitment fee is set by the Schedule, not "
        "stated in this text."
    )
    assert question.polarity == "absence"


def test_a_figure_the_scorer_finds_stated_here_stays_confirmed():
    field, _, ctx = _fee_context(external=0.05)

    assert V.validator_e_external_dependency(ctx) == []
    assert field.status == "confirmed"
    assert field.value == Decimal("0.25")


def test_a_figure_set_elsewhere_becomes_an_external_reference():
    field, _, ctx = _fee_context(external=0.97)

    assert V.validator_e_external_dependency(ctx) == [FEE]
    assert field.status == "external_reference"
    assert field.external_document == "Schedule"
    assert field.value is None


def test_the_stand_in_reads_the_statement_as_an_absence_claim():
    """The statement is built from the field's own description, and those
    are exactly the words the cited sentence contains. Read affirmatively,
    the lexical stand-in scored it as supported and forced a stated fee to
    external_reference. The declared polarity is what stops that."""
    at = FEE_SENTENCE.index("0.25%")
    field = ExtractedField.single(
        value=Decimal("0.25"), status="confirmed",
        spans=[Span(start=at, end=at + 5, text="0.25%")],
    )
    ctx = V.ValidationContext(
        doc=_Text(FEE_SENTENCE), chunks=[], fields={FEE: field},
        session=JevSession(OfflineJev()),
        thresholds=Thresholds(version="t", backend="offline"),
    )

    assert V.validator_e_external_dependency(ctx) == []
    assert field.status == "confirmed"


# ---------------------------------------------------------------------------
# conflict_choice: which candidate *is* the field, with none on offer
# ---------------------------------------------------------------------------


def _candidate(value: Any, support: int, start: int) -> dict[str, Any]:
    text = str(value)
    return {
        "value": text, "support": support, "segmentations": ["structural"],
        "confidence": 0.9,
        "span": Span(start=start, end=start + len(text), text=text).model_dump(),
    }


def _conflict(
    name: str, current: Any, candidates: list[dict[str, Any]],
    choice: dict[str, float], field_class: str,
) -> tuple[ExtractedField, ConflictRecord, ScriptedJev, V.ValidationContext]:
    field = ExtractedField.single(
        value=current, status="conflicted", field_class=field_class,
        spans=[Span(**candidates[0]["span"])],
        pass_support=candidates[0]["support"],
    )
    record = ConflictRecord(field=name, candidates=candidates)
    scorer = ScriptedJev(choices={"pick": choice})
    ctx = _context(scorer, {name: field})
    ctx.conflicts = [record]
    return field, record, scorer, ctx


NAMES = [
    _candidate("Loan Documents", 3, 400),
    _candidate("JPMorgan Chase Bank, N.A.", 2, 100),
]


def test_the_question_asks_which_value_is_the_field_and_offers_none():
    """Every candidate was extracted from its own citation, so every candidate
    is supported by it. "Which does its cited text support?" therefore could
    not tell them apart. And without a way out, the question had to pick
    one."""
    _, _, scorer, ctx = _conflict(
        AGENT, "Loan Documents", NAMES,
        {"JPMorgan Chase Bank, N.A.": 0.95, "Loan Documents": 0.03,
         V.NONE_OF_THESE: 0.02},
        "parties",
    )
    V.resolve_conflicts(ctx)

    (question,) = scorer.questions
    description = FIELD_REGISTRY[AGENT].description
    assert isinstance(question, ChoiceQ)
    assert question.question == (
        f"Which of these values is {description}, according to the text?"
    )
    assert question.criteria["Loan Documents"] == (
        f"the text states that {description} is Loan Documents"
    )
    assert V.NONE_OF_THESE in question.criteria


def test_the_chosen_candidate_becomes_the_value():
    """The field used to be confirmed without taking the chosen value. It was
    confirmed holding the very value the model had just rejected, and five
    administrative agents reached a report as "Loan Documents" and
    "Restricted Subsidiary" that way."""
    field, record, _, ctx = _conflict(
        AGENT, "Loan Documents", NAMES,
        {"JPMorgan Chase Bank, N.A.": 0.95, "Loan Documents": 0.03,
         V.NONE_OF_THESE: 0.02},
        "parties",
    )
    V.resolve_conflicts(ctx)

    assert field.status == "confirmed"
    assert field.value == "JPMorgan Chase Bank, N.A."
    assert field.spans == [Span(**NAMES[1]["span"])]
    assert field.pass_support == 2
    assert record.resolved and record.resolved_to == "JPMorgan Chase Bank, N.A."


def test_choosing_the_value_already_held_confirms_it_unchanged():
    field, _, _, ctx = _conflict(
        AGENT, "JPMorgan Chase Bank, N.A.", list(reversed(NAMES)),
        {"JPMorgan Chase Bank, N.A.": 0.95, "Loan Documents": 0.03,
         V.NONE_OF_THESE: 0.02},
        "parties",
    )
    V.resolve_conflicts(ctx)

    assert field.status == "confirmed"
    assert field.value == "JPMorgan Chase Bank, N.A."
    assert field.spans == [Span(**NAMES[1]["span"])]


def test_a_chosen_date_comes_back_as_a_date():
    candidates = [_candidate(date(2024, 3, 15), 3, 100), _candidate(date(2024, 4, 1), 2, 900)]
    field, _, _, ctx = _conflict(
        "closing_date", date(2024, 3, 15), candidates,
        {"2024-04-01": 0.9, "2024-03-15": 0.05, V.NONE_OF_THESE: 0.05},
        "dates",
    )
    V.resolve_conflicts(ctx)

    assert field.status == "confirmed"
    assert field.value == date(2024, 4, 1)


def test_none_of_these_leaves_the_conflict_open():
    """A closing date that is an event rather than a date used to be given
    whichever candidate read most like a date."""
    candidates = [_candidate(date(2024, 3, 15), 3, 100), _candidate(date(2024, 4, 1), 2, 900)]
    field, record, _, ctx = _conflict(
        "closing_date", date(2024, 3, 15), candidates,
        {V.NONE_OF_THESE: 0.9, "2024-03-15": 0.05, "2024-04-01": 0.05},
        "dates",
    )
    unresolved = V.resolve_conflicts(ctx)

    assert unresolved == [record]
    assert not record.resolved
    assert field.status == "conflicted"
    assert field.value == date(2024, 3, 15)
    assert field.notes.startswith("the candidates do not settle it")


def test_a_numeric_candidate_that_cannot_be_restored_goes_to_review():
    """A candidate carries its value as a string, without the unit. Rather
    than guess the unit back, the field is sent to review. It is never
    confirmed holding the value that was rejected."""
    candidates = [_candidate(Decimal("2.75"), 3, 100), _candidate(Decimal("3.25"), 2, 900)]
    field, _, _, ctx = _conflict(
        MARGIN, Decimal("2.75"), candidates,
        {"3.25": 0.9, "2.75": 0.05, V.NONE_OF_THESE: 0.05},
        "economic_terms",
    )
    V.resolve_conflicts(ctx)

    assert field.status == "needs_review"
    assert field.value == Decimal("2.75")
    assert "not applied" in field.notes


@pytest.mark.parametrize("kind, text, expected", [
    ("date", "2024-04-01", date(2024, 4, 1)),
    ("int", "90", 90),
    ("bool", "False", False),
    ("text", "JPMorgan Chase Bank, N.A.", "JPMorgan Chase Bank, N.A."),
    ("date", "20240401", None),          # parses, but is not what str() wrote
    ("int", "090", None),
    ("date", "the Closing Date", None),
    ("money", "300000000", None),        # the unit is not in the string
    ("percent", "3.25", None),
])
def test_only_values_whose_string_form_inverts_exactly_are_restored(kind, text, expected):
    assert V._typed_like(kind, text) == expected
