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
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from credit_extract.ingest.segment import Chunk
from credit_extract.models.archetypes import PROFILES, ArchetypeDetection
from credit_extract.models.core import ConflictRecord, ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import archetype as A
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import (
    DEFAULT_THRESHOLD, Sample, Thresholds, fit,
)
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


@pytest.mark.parametrize("name, asks_about, not_about", [
    # JRD Unico's financials name an "EBITDA ratio, fixed charge ratio and
    # incurrence of debt ratio" and state no level. Asked about "the maximum
    # leverage level", a literal reader said there was none.
    ("financial_covenant.level",
     "whether or not it states the required level", "maximum leverage level"),
    # Evernorth sells $30,000,000 of notes. Asked about "the Initial Term
    # Loans", a literal reader found none and reported no principal.
    ("initial_term_loan.commitment", "term loans or notes", "Initial Term Loans"),
])
def test_absence_is_asked_of_the_thing_not_of_its_defined_name_or_number(
    name, asks_about, not_about
):
    statement = FIELD_REGISTRY[name].presence_statement
    assert asks_about in statement
    assert not_about not in statement


@pytest.mark.parametrize("name, defined_term", [
    # HealthStream's MFN protects incremental *revolving* commitments. Asked
    # about repricing "of the Initial Term Loans", the clause scored 0.20 and
    # the threshold was reported absent beside it.
    ("mfn_threshold_pct", "Initial Term Loans"),
    ("initial_term_loan.maturity_date", "Initial Term Loans"),
    ("revolver.commitment", "Revolving Credit Commitments"),
    ("revolver.maturity_date", "Revolving Credit Facility"),
    ("delayed_draw.commitment", "Delayed Draw Term Loan Commitments"),
    ("lc_sublimit", "Letter of Credit Sublimit"),
    ("closing_date", "Closing Date"),
    ("consolidated_ebitda.addback_cap_pct", "Consolidated EBITDA"),
])
def test_a_presence_question_does_not_use_one_agreements_name_for_the_thing(
    name, defined_term
):
    assert defined_term not in FIELD_REGISTRY[name].presence_statement


@pytest.mark.parametrize("name", sorted(FIELD_REGISTRY))
def test_no_presence_question_asks_about_the_initial_term_loans_by_name(name):
    """Two of the second live gate's silent errors, and one of the third's,
    were this phrase read literally in a document that calls its debt
    something else."""
    assert "Initial Term Loans" not in FIELD_REGISTRY[name].presence_statement


def test_a_bdc_asset_coverage_floor_is_not_the_covenant_c_asks_about():
    """Assets over debt at least 1.50 is the 1940 Act limit and caps nothing
    against earnings. Seven labels call such facilities covenant-free, and
    the question has to leave them that way."""
    statement = FIELD_REGISTRY["financial_covenant.level"].presence_statement
    assert "relative to its earnings" in statement
    assert "asset" not in statement.lower()


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


# ---------------------------------------------------------------------------
# Archetype dispatch: the model is asked only what vocabulary cannot settle
# ---------------------------------------------------------------------------

AGREEMENT = "Event of Default. Event of Default. " + "x" * 200


def _dispatch(
    monkeypatch, deterministic: ArchetypeDetection, scorer: ScriptedJev,
    text: str = AGREEMENT, graph: Any = None,
) -> ArchetypeDetection:
    monkeypatch.setattr(A, "detect_deterministic", lambda _text: deterministic)
    return A.detect_archetype(
        SimpleNamespace(text=text), JevSession(scorer), graph=graph
    )


def _tie(*archetypes: str) -> ArchetypeDetection:
    return ArchetypeDetection(
        signals_found={a: ["signal"] for a in archetypes},
        note="tied", tied=list(archetypes),
    )


def _distribution(pick: str, confidence: float) -> dict[str, float]:
    options = [a for a in PROFILES if a != "unknown"] + [A.NEITHER]
    rest = (1.0 - confidence) / (len(options) - 1)
    return {o: confidence if o == pick else rest for o in options}


def test_a_document_that_is_not_an_agreement_is_not_asked_what_it_establishes():
    """"What kind of credit facility does this document establish?" assumes
    the document establishes a facility, and every option is one. Asked of a
    warrant, the live scorer answered venture_debt at 1.00. All 18 archetypes
    the live gate got wrong were reached this way."""
    scorer = ScriptedJev()
    warrant = (
        "WARRANT TO PURCHASE SHARES OF COMMON STOCK. The Holder may exercise "
        "this Warrant in connection with the Company's term loan facility."
    )
    verdict = A.detect_archetype(SimpleNamespace(text=warrant), JevSession(scorer))

    assert scorer.asked == []
    assert verdict.archetype == "unknown"


def test_a_document_with_too_little_signal_is_not_asked_either(monkeypatch):
    scorer = ScriptedJev()
    verdict = _dispatch(monkeypatch, ArchetypeDetection(note="thin"), scorer)

    assert scorer.asked == []
    assert verdict.archetype == "unknown"


def test_a_tie_is_put_to_the_model_with_neither_on_offer(monkeypatch):
    scorer = ScriptedJev(choices={"archetype": _distribution("nav_or_subscription", 0.99)})
    verdict = _dispatch(
        monkeypatch, _tie("abl_revolver", "nav_or_subscription"), scorer
    )

    (question,) = scorer.questions
    assert set(question.criteria) == (set(PROFILES) - {"unknown"}) | {A.NEITHER}
    assert verdict.archetype == "nav_or_subscription"
    assert verdict.basis == "model"
    assert verdict.tied == ["abl_revolver", "nav_or_subscription"]


def test_neither_is_an_answer_and_it_rules_nothing_out(monkeypatch):
    scorer = ScriptedJev(choices={"archetype": _distribution(A.NEITHER, 0.72)})
    verdict = _dispatch(monkeypatch, _tie("abl_revolver", "second_lien"), scorer)

    assert verdict.archetype == "unknown"
    assert verdict.basis == "model"
    assert "none of them" in verdict.note


def test_a_weak_pick_on_a_tie_rules_nothing_out(monkeypatch):
    scorer = ScriptedJev(choices={"archetype": _distribution("second_lien", 0.30)})
    verdict = _dispatch(monkeypatch, _tie("abl_revolver", "second_lien"), scorer)

    assert verdict.archetype == "unknown"
    assert "below the 0.35 dispatch threshold" in verdict.note


def test_a_decisive_verdict_other_than_abl_costs_nothing(monkeypatch):
    scorer = ScriptedJev()
    decided = ArchetypeDetection(
        archetype="second_lien", confidence=0.85, basis="deterministic",
    )
    assert _dispatch(monkeypatch, decided, scorer) is decided
    assert scorer.asked == []


ABL = ArchetypeDetection(
    archetype="abl_revolver", confidence=0.95, basis="deterministic",
    signals_found={"abl_revolver": ["borrowing base", "eligible accounts"]},
    note="decisive vocabulary: borrowing base, eligible accounts",
)


def test_an_abl_verdict_is_asked_whose_borrowing_base_it_is(monkeypatch):
    """"Borrowing base" is also in fund facilities, servicing agreements and
    investor rights agreements. Counting keywords cannot say whose facility
    the words describe (#35)."""
    scorer = ScriptedJev(nouls={"own_receivables": 0.85})
    verdict = _dispatch(monkeypatch, ABL, scorer)

    (question,) = scorer.questions
    assert question.statement == A.OWN_RECEIVABLES_STATEMENT
    assert verdict.archetype == "abl_revolver"
    assert "own receivables and inventory at 0.85" in verdict.note


def test_an_abl_verdict_the_model_does_not_support_is_withdrawn(monkeypatch):
    """Withdrawing is safe in the direction that matters. ``unknown`` rules
    nothing out, so a veto can cost coverage but cannot suppress a field."""
    scorer = ScriptedJev(nouls={"own_receivables": 0.08})
    verdict = _dispatch(monkeypatch, ABL, scorer)

    assert verdict.archetype == "unknown"
    assert verdict.basis == "model"
    assert "not the borrower's own" in verdict.note


class _Graph:
    """A definition graph holding one Borrowing Base definition."""

    def __init__(self, body: str) -> None:
        self.node = SimpleNamespace(term="Borrowing Base", body=body)

    def resolve(self, term: str) -> str | None:
        return "Borrowing Base" if term == "Borrowing Base" else None

    def get(self, term: str) -> Any:
        return self.node


def test_the_borrowing_base_definition_rides_along_capped(monkeypatch):
    """A corporate ABL defines its Eligible Accounts and Eligible Inventory
    well past the detection window. The definition is appended so that the
    evidence is in the state."""
    scorer = ScriptedJev(nouls={"own_receivables": 0.85})
    body = "the sum of 85% of Eligible Accounts plus " + "y" * A.DEFINITION_CAP
    _dispatch(monkeypatch, ABL, scorer, graph=_Graph(body))

    ((state, _),) = scorer.asked
    assert state.startswith(AGREEMENT)
    assert "DEFINITION OF Borrowing Base\nthe sum of 85% of Eligible Accounts" in state
    assert state.endswith(body[:A.DEFINITION_CAP])


# ---------------------------------------------------------------------------
# Fitting: no failures means no evidence for a lower threshold
# ---------------------------------------------------------------------------


def _samples(probabilities: list[float], correct: bool = True) -> list[Sample]:
    return [
        Sample(
            field="f", field_class="economic_terms", probability=p,
            correct=correct, document_id=f"d{i}", validator="C_negative_space",
        )
        for i, p in enumerate(probabilities)
    ]


def test_a_class_with_no_failures_is_not_fitted_below_the_default():
    """With nothing wrong in the class, every threshold meets the target, so
    the fit lands on the lowest score it saw. Fitted to eight synthetic
    absences, validator C's economic terms came out at 0.27. At that
    threshold, pricing grids were confirmed absent."""
    fitted = fit(_samples([0.27, 0.35, 0.41, 0.52, 0.60, 0.66, 0.71, 0.75]))

    assert fitted.per_class["C_negative_space/economic_terms"] == DEFAULT_THRESHOLD


def test_the_floor_never_lowers_a_threshold_the_data_put_higher():
    fitted = fit(_samples([0.91, 0.93, 0.95, 0.96, 0.97, 0.98, 0.99, 0.99]))

    assert fitted.per_class["C_negative_space/economic_terms"] > DEFAULT_THRESHOLD
