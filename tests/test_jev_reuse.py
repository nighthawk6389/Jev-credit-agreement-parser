"""No answer is bought twice.

A full gate made over 100,000 requests. Nearly half were validator C re-sending
chunks validator B had just sent, and nearly half were spent on mutants, each
of which is its document with one defect injected. Three mechanisms remove
that duplication, and each is pinned here:

* a session reuses an answer it has already bought;
* validator B carries C's questions, so C finds its answers bought;
* ``CachedJev`` keeps answers between runs, in a file the workers share.

None of this may change an answer. Reuse applies only to the identical
question, sent to the identical model about the identical state.
"""

from __future__ import annotations

import pytest

from credit_extract.ingest.segment import Chunk
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import Thresholds
from credit_extract.validate.jev import (
    ANSWER_CACHE, CachedJev, ChoiceQ, JevSession, Noul, OfflineJev,
    answer_cache_for, answer_key,
)

STATE = "This Agreement shall be governed by the laws of the State of New York."
LAW = Noul(name="law", statement="The text supports New York as the governing law.")
SEAT = Noul(name="seat", statement="The text names a forum for disputes.")


class Counting(OfflineJev):
    """The stand-in, counting what it is actually sent."""

    def __init__(self, name: str = "offline") -> None:
        super().__init__()
        self.name = name
        self.calls: list[list[str]] = []

    def ask(self, state, questions):
        self.calls.append([q.name for q in questions])
        return super().ask(state, questions)

    @property
    def questions(self) -> int:
        return sum(len(c) for c in self.calls)


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def test_the_key_is_the_model_the_state_and_the_question_as_sent():
    key = answer_key("jev-1.13.0", STATE, LAW)

    assert key == answer_key("jev-1.13.0", STATE, LAW)
    assert key != answer_key("jev-1.14.0", STATE, LAW)
    assert key != answer_key("jev-1.13.0", STATE + " ", LAW)
    assert key != answer_key("jev-1.13.0", STATE, LAW.model_copy(
        update={"statement": LAW.statement + " "}
    ))
    assert key != answer_key("jev-1.13.0", STATE, LAW.model_copy(update={"name": "l"}))


def test_the_order_of_a_choices_options_is_part_of_the_question():
    """Options travel in order, and a reader may weigh the first one more."""
    ab = ChoiceQ(name="c", question="Which?", criteria={"a": "A", "b": "B"})
    ba = ChoiceQ(name="c", question="Which?", criteria={"b": "B", "a": "A"})

    assert answer_key("m", STATE, ab) != answer_key("m", STATE, ba)


def test_hints_that_are_never_sent_do_not_split_the_key():
    """Polarity and concept steer the stand-in and are not on the wire."""
    hinted = LAW.model_copy(update={"polarity": "absence", "concept": "x"})

    assert answer_key("m", STATE, hinted) == answer_key("m", STATE, LAW)


# ---------------------------------------------------------------------------
# Within a session
# ---------------------------------------------------------------------------


def test_a_session_does_not_buy_the_same_answer_twice():
    backend = Counting()
    session = JevSession(backend)

    first = session.ask(STATE, [LAW])
    again = session.ask(STATE, [LAW])

    assert backend.calls == [["law"]]
    assert again["law"] == first["law"]
    assert session.ledger.jev_requests == 1
    assert session.ledger.jev_answers_reused == 1


def test_only_the_questions_not_yet_answered_are_sent():
    backend = Counting()
    session = JevSession(backend)
    session.ask(STATE, [LAW])

    result = session.ask(STATE, [LAW, SEAT])

    assert backend.calls == [["law"], ["seat"]]
    assert set(result.decisions) == {"law", "seat"}


def test_a_prefetched_question_rides_along_and_is_answered_later_for_free():
    backend = Counting()
    session = JevSession(backend)

    now = session.ask(STATE, [LAW], label="B", prefetch=[SEAT])
    later = session.ask(STATE, [SEAT], label="C")

    assert backend.calls == [["law", "seat"]]
    assert set(now.decisions) == {"law"}                 # not returned early
    assert later["seat"].probability is not None
    assert session.ledger.jev_requests == 1
    assert session.requests[0]["prefetched"] == 1


def test_prefetch_never_causes_a_request_of_its_own():
    backend = Counting()
    session = JevSession(backend)
    session.ask(STATE, [LAW])

    session.ask(STATE, [LAW], prefetch=[SEAT])

    assert backend.calls == [["law"]]
    session.ask(STATE, [SEAT])
    assert backend.calls == [["law"], ["seat"]]


def test_a_prefetched_question_that_shares_a_name_is_not_sent_twice():
    backend = Counting()
    session = JevSession(backend)
    impostor = SEAT.model_copy(update={"name": "law"})

    result = session.ask(STATE, [LAW], prefetch=[impostor])

    assert backend.calls == [["law"]]
    assert result["law"].probability == OfflineJev().ask(STATE, [LAW])["law"].probability


# ---------------------------------------------------------------------------
# Validator B carries validator C
# ---------------------------------------------------------------------------

MARGIN = "applicable_margin.eurodollar_top_level_pct"
TEXT = (
    "Applicable Rate means, for any day, with respect to any Term SOFR Loan, "
    "the rate per annum set forth below under the caption Term SOFR Spread, "
    "based upon the Total Net Leverage Ratio: Level I 2.25%, Level II 2.00%, "
    "Level III 1.75%. "
) * 2


def _chunk(text: str, start: int, chunk_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id, segmentation="structural", label=chunk_id,
        spans=[Span(start=start, end=start + len(text), text=text)], text=text,
    )


def _context(backend) -> V.ValidationContext:
    fields = {MARGIN: ExtractedField.single(value=None, status="needs_review")}
    return V.ValidationContext(
        doc=type("D", (), {"text": "x", "slice": lambda self, a, b: ""})(),
        chunks=[_chunk(TEXT, 0, "a"), _chunk(TEXT.upper(), 1_000, "b")],
        fields=fields, specs={MARGIN: FIELD_REGISTRY[MARGIN]},
        session=JevSession(backend),
        thresholds=Thresholds(version="t", backend=backend.name),
    )


def test_c_sends_nothing_for_the_chunks_b_already_carried_it_on():
    backend = Counting()
    ctx = _context(backend)

    V.validator_b_orphan_sweep(ctx, prefetch_absence=True)
    sent_by_b = len(backend.calls)
    absence = V.validator_c_negative_space(ctx)

    assert len(backend.calls) == sent_by_b == 2
    assert [r["label"] for r in ctx.session.requests] == ["B_orphan_sweep"] * 2
    assert MARGIN in absence


def test_carrying_c_on_b_changes_no_answer():
    """The stand-in scores each question on its own, so every answer, and
    what C concludes from them, must come out the same either way."""
    together, apart = _context(Counting()), _context(Counting())

    V.validator_b_orphan_sweep(together, prefetch_absence=True)
    V.validator_b_orphan_sweep(apart)

    assert V.validator_c_negative_space(together) == V.validator_c_negative_space(apart)
    assert together.fields[MARGIN].status == apart.fields[MARGIN].status
    assert together.orphans == apart.orphans


def test_without_prefetch_b_sends_only_its_own_questions():
    backend = Counting()
    V.validator_b_orphan_sweep(_context(backend))

    assert all(MARGIN not in call for call in backend.calls)


# ---------------------------------------------------------------------------
# Between runs
# ---------------------------------------------------------------------------


def test_a_second_run_reads_back_what_the_first_bought(tmp_path):
    path = tmp_path / "answers.sqlite"
    first, second = Counting(), Counting()

    bought = JevSession(CachedJev(first, path)).ask(STATE, [LAW, SEAT])
    session = JevSession(CachedJev(second, path))
    read = session.ask(STATE, [LAW, SEAT])

    assert first.questions == 2 and second.calls == []
    assert read.decisions == bought.decisions
    assert session.ledger.jev_requests == 0
    assert session.ledger.jev_cost_usd == 0.0
    assert session.ledger.jev_answers_reused == 2


def test_only_what_the_file_lacks_is_asked(tmp_path):
    path = tmp_path / "answers.sqlite"
    JevSession(CachedJev(Counting(), path)).ask(STATE, [LAW])
    backend = Counting()

    JevSession(CachedJev(backend, path)).ask(STATE, [LAW, SEAT])

    assert backend.calls == [["seat"]]


def test_answers_from_one_model_are_never_read_back_for_another(tmp_path):
    path = tmp_path / "answers.sqlite"
    JevSession(CachedJev(Counting("jev-1.13.0"), path)).ask(STATE, [LAW])
    newer = Counting("jev-1.14.0")

    JevSession(CachedJev(newer, path)).ask(STATE, [LAW])

    assert newer.calls == [["law"]]


def test_a_failed_request_stores_nothing(tmp_path):
    path = tmp_path / "answers.sqlite"

    class Refusing(Counting):
        def ask(self, state, questions):
            raise RuntimeError("HTTP 402")

    with pytest.raises(RuntimeError):
        JevSession(CachedJev(Refusing(), path)).ask(STATE, [LAW])
    backend = Counting()
    JevSession(CachedJev(backend, path)).ask(STATE, [LAW])

    assert backend.calls == [["law"]]


def test_the_live_scorer_is_cached_by_default_and_the_stand_in_is_not(tmp_path):
    assert answer_cache_for("api") == ANSWER_CACHE
    assert answer_cache_for("offline") is None
    assert answer_cache_for("api", disabled=True) is None
    assert answer_cache_for("offline", tmp_path / "a.sqlite") == tmp_path / "a.sqlite"


def test_every_kind_of_answer_reads_back_exactly(tmp_path):
    """The file keeps only an answer's non-default fields; the name and the
    model are restored from the question and the scorer on the way out."""
    from credit_extract.validate.jev import ScoreQ

    questions = [
        LAW,
        ChoiceQ(name="kind", question="Which?", criteria={"a": "rate", "b": "fee"}),
        ScoreQ(name="clarity", question="How clear?", rubric=["low", "mid", "high"]),
    ]
    path = tmp_path / "answers.sqlite"

    bought = JevSession(CachedJev(Counting(), path)).ask(STATE, questions)
    read = JevSession(CachedJev(Counting(), path)).ask(STATE, questions)

    assert read.decisions == bought.decisions
