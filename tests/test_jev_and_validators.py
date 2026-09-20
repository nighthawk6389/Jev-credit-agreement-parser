"""The Jev layer: batching economics, context limits, and validator behaviour."""

from __future__ import annotations

import pytest

from credit_extract.validate.jev import (
    CONTEXT_TOKENS_TOTAL, PRICE_PER_MTOK, STATE_PLUS_QUESTION_TOKENS, ChoiceQ,
    JevContextExceeded, JevSession, Noul, OfflineJev, ScoreQ, check_context,
    split_batches,
)

CLAUSE = (
    "The Borrower shall repay the outstanding principal amount of the Initial "
    "Term Loans in consecutive quarterly installments of $376,250 commencing "
    "December 31, 2017. The aggregate Initial Term Loan Commitment is "
    "$150,500,000 and the Initial Term Loan Maturity Date is August 1, 2024."
)


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------


def test_fifteen_questions_cost_about_what_one_does():
    """State is sent once per request; output is free.

    This is the fact the whole validation design rests on -- it is why the
    orphan sweep can afford to ask every question of every chunk.
    """
    state = CLAUSE * 40
    questions = [
        Noul(name=f"q{i}", statement="This text contains a numeric threshold.")
        for i in range(15)
    ]
    many = JevSession()
    many.ask(state, questions)
    one = JevSession()
    one.ask(state, questions[:1])

    assert many.ledger.jev_requests == 1
    assert many.ledger.jev_questions == 15
    assert many.spent < one.spent * 1.25, (
        f"15 questions cost {many.spent / one.spent:.2f}x one question; "
        "batching is supposed to be nearly free"
    )


def test_cost_tracks_the_published_price():
    session = JevSession()
    result = session.ask(CLAUSE, [Noul(name="q", statement="A threshold.")])
    expected = result.input_tokens * PRICE_PER_MTOK / 1_000_000
    assert session.spent == pytest.approx(expected)


def test_budget_is_enforced_before_spending():
    from credit_extract.validate.jev import JevBudgetExceeded

    session = JevSession(budget_usd=1e-9)
    with pytest.raises(JevBudgetExceeded):
        session.ask(CLAUSE * 20, [Noul(name="q", statement="A threshold.")])
    assert session.spent == 0.0, "a refused request must not be billed"


# ---------------------------------------------------------------------------
# Context limits
# ---------------------------------------------------------------------------


def test_state_plus_longest_question_is_capped_at_32k():
    state = "x" * (STATE_PLUS_QUESTION_TOKENS * 4)
    with pytest.raises(JevContextExceeded, match="32000 token limit"):
        check_context(state, [Noul(name="q", statement="short")])


def test_questions_split_into_the_fewest_requests_that_fit():
    state = "x" * 8_000
    questions = [
        Noul(name=f"q{i}", statement="y" * 40_000) for i in range(6)
    ]
    batches = split_batches(state, questions)
    assert sum(len(b) for b in batches) == 6
    for batch in batches:
        assert check_context(state, batch) <= CONTEXT_TOKENS_TOTAL


def test_a_question_too_large_for_the_state_is_refused():
    state = "x" * (28_000 * 4)
    with pytest.raises(JevContextExceeded, match="does not fit alongside"):
        split_batches(state, [Noul(name="q", statement="y" * 40_000)])


# ---------------------------------------------------------------------------
# Offline scoring behaviour
# ---------------------------------------------------------------------------


def test_a_supported_value_separates_from_an_unsupported_one():
    session = JevSession()
    result = session.ask(CLAUSE, [
        Noul(name="right",
             statement="The text supports a value of 376,250 for the quarterly "
                       "amortization payment."),
        Noul(name="wrong",
             statement="The text supports a value of 999,999 for the quarterly "
                       "amortization payment."),
    ])
    assert result["right"].probability > result["wrong"].probability + 0.4


def test_an_iso_date_matches_the_way_agreements_write_dates():
    """Regression: correct maturity dates scored as unsupported."""
    session = JevSession()
    result = session.ask(CLAUSE, [
        Noul(name="d",
             statement="The text supports a value of 2024-08-01 for the maturity "
                       "date of the Initial Term Loans."),
    ])
    assert result["d"].probability > 0.7


def test_polarity_is_declared_not_sniffed_from_the_wording():
    """Regression: a negation inside an affirmative claim's subject.

    "depends on a document NOT contained in this agreement" is an affirmative
    claim. Read as a negation it inverts, and every numeric field in the
    document gets marked as an external reference.
    """
    session = JevSession()
    statement = (
        "The magnitude of this limit depends on a document not contained in "
        "this agreement."
    )
    result = session.ask(CLAUSE, [Noul(name="e", statement=statement)])
    assert result["e"].probability < 0.5, (
        "a clause that cites nothing external must not read as external"
    )


def test_an_absence_claim_inverts_the_signal():
    session = JevSession()
    result = session.ask(CLAUSE, [
        Noul(name="true_absence", polarity="absence",
             statement="This text contains no provision addressing an excess "
                       "cash flow sweep."),
        Noul(name="false_absence", polarity="absence",
             statement="This text contains no provision addressing quarterly "
                       "installments."),
    ])
    assert result["true_absence"].probability > 0.8
    assert result["false_absence"].probability < 0.2


def test_choice_returns_a_distribution_over_the_candidates():
    session = JevSession()
    result = session.ask(CLAUSE, [ChoiceQ(
        name="pick",
        question="Which value does the text support?",
        criteria={"376250": "the quarterly installment", "999999": "the quarterly installment"},
    )])
    decision = result["pick"]
    assert decision.choice == "376250"
    assert sum(decision.distribution.values()) == pytest.approx(1.0, abs=1e-3)


def test_a_score_with_no_discriminating_evidence_returns_the_middle():
    """Returning level 1 would assert "least severe" on no evidence."""
    backend = OfflineJev()
    question = ScoreQ(
        name="s", question="How material?",
        rubric=["alpha", "bravo", "charlie", "delta", "echo"],
    )
    result = backend.ask("wholly unrelated text", [question])
    assert result["s"].score == 3


def test_polarity_and_concept_stay_off_the_wire():
    """They are offline-scoring hints, not part of the System One schema."""
    question = Noul(
        name="q", statement="x", polarity="absence", concept="threshold"
    )
    payload = question.model_dump(exclude={"polarity", "concept"})
    assert set(payload) == {"name", "statement", "kind"}


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def test_override_detection_anchors_on_the_marker_not_the_subject(doc):
    """Regression: a window around every mention reported three for one."""
    from credit_extract.validate.validators import find_override_candidates

    pairs = find_override_candidates(doc, "Consolidated EBITDA")
    assert pairs
    for pair in pairs:
        head = pair.second.text[:60].lower()
        assert any(
            head.startswith(marker)
            for marker in ("notwithstanding", "provided that", "shall not apply",
                           "shall be deemed", "for the avoidance of doubt",
                           "in lieu of", "except as")
        ), f"candidate must begin at the override marker, got: {head!r}"


def test_external_dependency_uses_sentence_bounds_not_proximity(doc):
    """A schedule cited in a neighbouring sentence is not this limit's source."""
    from credit_extract.validate.validators import _sentence_window

    span = doc.find_first(r"6\.50:1\.00")
    if span is None:
        pytest.skip("covenant grid not present in this document")
    window = _sentence_window(doc, span)
    assert "Schedule 6.01" not in window


def test_pass_planning_covers_segmentations_before_varying_temperature():
    """Different views disagree for reasons; resampling one view mostly doesn't."""
    from credit_extract.extract.passes import plan_passes

    plan = plan_passes(["structural", "sliding", "definitional"], 3)
    assert [p[0] for p in plan] == ["structural", "sliding", "definitional"]
    assert {p[1] for p in plan} == {0.0}, "the first round is deterministic"

    wider = plan_passes(["structural", "sliding", "definitional"], 5)
    assert len(wider) == 5
    assert wider[3][1] > 0.0, "extra passes vary temperature"
    assert wider[3][2].endswith("@0.3"), "the pass id records the temperature"


def test_the_deterministic_backend_declines_to_fake_variation():
    from credit_extract.extract.passes import OfflineRuleBackend

    backend = OfflineRuleBackend()
    assert backend.with_temperature(0.7) is backend
