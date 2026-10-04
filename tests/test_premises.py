"""A computed maturity is confirmed by its premises, or not at all.

No sentence states a computed date. "The Final Maturity Date is the earliest
of the 4-year anniversary of the Closing Date and the events it lists" and
"the Closing Date is June 1, 2026" are each stated, and June 1, 2030 follows
from them by arithmetic the pipeline does itself. Validator A cannot confirm
the conclusion -- the live scorer gave every computed fund maturity under
0.10 -- so ``validator_a_premises`` asks the premises instead, each against
the definition that states it.
"""

from __future__ import annotations

import json
from datetime import date

from credit_extract.ingest.normalize import NormalizedDocument
from credit_extract.models.core import ExtractedField, Span, ValidationEvent
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import Thresholds
from credit_extract.validate.jev import Decision, JevResult, JevSession

FIELD = "revolver.maturity_date"
TEXT = (
    '"Closing Date" means June 1, 2026. '
    '"Final Maturity Date" means the earliest of (a) the four (4) year '
    'anniversary of the Closing Date and (b) the date of acceleration.'
)
CLOSING = (0, 34)
FINAL = (35, len(TEXT))
PREMISES = [
    [*CLOSING, "The text defines the Closing Date as June 1, 2026."],
    [*FINAL, "The text defines the Final Maturity Date as the earliest of the "
             "4-year anniversary of the Closing Date and the other dates and "
             "events it lists."],
]


class Believes:
    """A scorer with a fixed opinion of each statement, recording what it is sent."""

    name = "jev-test"

    def __init__(self, opinions: dict[str, float]) -> None:
        self.opinions = opinions
        self.sent: list[tuple[str, list[str]]] = []

    def ask(self, state, questions):
        self.sent.append((state, [q.statement for q in questions]))
        return JevResult(backend=self.name, questions_asked=len(questions), decisions={
            q.name: Decision(name=q.name, kind="noul",
                             probability=self.opinions.get(q.statement, 0.05))
            for q in questions
        })


def _field(*, premises=PREMISES, a_passed=False, status="needs_review"):
    field = ExtractedField.single(
        value=date(2030, 6, 1), status=status, field_class="dates",
        spans=[Span(start=FINAL[0], end=FINAL[1], text=TEXT[FINAL[0]:FINAL[1]])],
        qualifiers={"derived": "Closing Date <- Final Maturity Date",
                    **({"premises": json.dumps(premises)} if premises else {})},
    )
    field.record(ValidationEvent(validator="A_span_support", question="?",
                                 probability=0.07, threshold=0.8, passed=a_passed))
    return field


def _run(field, scorer):
    ctx = V.ValidationContext(
        doc=NormalizedDocument(document_id="d", source_path="d", source_format="txt",
                               text=TEXT),
        fields={FIELD: field}, chunks=[], specs={FIELD: FIELD_REGISTRY[FIELD]},
        session=JevSession(scorer),
        thresholds=Thresholds(per_class={"A_span_support/dates": 0.8}, backend="jev-test"),
    )
    return V.validator_a_premises(ctx)


def test_every_premise_supported_confirms_the_computed_date():
    scorer = Believes({PREMISES[0][2]: 0.97, PREMISES[1][2]: 0.91})
    field = _field()

    assert _run(field, scorer) == 2
    assert field.status == "confirmed"
    assert field.validation_source == "A_premises"
    assert field.validation_confidence == 0.91
    # Each premise is asked against the definition that states it, alone.
    assert scorer.sent == [(TEXT[slice(*CLOSING)], [PREMISES[0][2]]),
                           (TEXT[slice(*FINAL)], [PREMISES[1][2]])]


def test_one_weak_premise_keeps_the_date_in_review_and_says_which():
    scorer = Believes({PREMISES[0][2]: 0.97, PREMISES[1][2]: 0.40})
    field = _field()

    _run(field, scorer)
    assert field.status == "needs_review"
    assert "4-year anniversary" in field.notes and "0.40" in field.notes
    assert [e.passed for e in field.trace if e.validator == "A_premises"] == [True, False]


def test_only_a_value_validator_a_turned_down_is_asked_about():
    scorer = Believes({})
    passed = _field(a_passed=True, status="confirmed")
    no_premises = _field(premises=None)

    assert _run(passed, scorer) == 0
    assert _run(no_premises, scorer) == 0
    assert scorer.sent == []
    assert no_premises.status == "needs_review"
