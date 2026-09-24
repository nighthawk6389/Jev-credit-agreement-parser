"""Validator C: absence is a claim, and a claim needs a search behind it.

``absent_from_document`` is one of the CONFIDENT statuses, so a wrong one is a
silent error counted against the family budget. Everything here guards the
same boundary: the validator may say "the agreement has no such term" only
where it actually asked.
"""

from __future__ import annotations

import pytest

from credit_extract.ingest.segment import Chunk
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import load_thresholds
from credit_extract.validate.jev import JevSession, OfflineJev

FIELD = "mfn_threshold_pct"


class _Doc:
    """The minimum a ValidationContext needs; no text worth sweeping."""

    text = "x"

    def slice(self, start: int, end: int) -> str:  # pragma: no cover - unused
        return ""


def _chunk(text: str, chunk_id: str = "c0") -> Chunk:
    return Chunk(
        chunk_id=chunk_id, segmentation="structural", label=chunk_id,
        spans=[Span(start=0, end=max(1, len(text)), text=text or " ")],
        text=text,
    )


def _run(chunks: list[Chunk]) -> ExtractedField:
    fields = {FIELD: ExtractedField.single(value=None, status="needs_review")}
    ctx = V.ValidationContext(
        doc=_Doc(), chunks=chunks, fields=fields,
        specs={FIELD: FIELD_REGISTRY[FIELD]},
        session=JevSession(OfflineJev()), thresholds=load_thresholds(),
    )
    V.validator_c_negative_space(ctx)
    return fields[FIELD]


def test_absence_is_not_confirmable_with_nothing_swept():
    """The initialiser is 1.0 because that is the identity for a minimum --
    and 1.0 is also the most confident claim the scale can make. A field asked
    of no chunk used to keep it and sail past the threshold, arriving as
    "affirmatively confirmed absent at 1.00 across 0 chunks"."""
    field = _run([])

    assert field.status == "needs_review"
    assert field.validation_confidence is None
    assert "not testable" in field.notes


def test_blank_chunks_do_not_count_as_a_search():
    """The loop skips chunks with no text, so a run whose chunks are all
    whitespace asks nothing while looking like it swept something."""
    field = _run([_chunk("   "), _chunk("\n\t", "c1")])

    assert field.status == "needs_review"
    assert field.validation_confidence is None


def test_absence_is_confirmable_once_something_was_actually_asked():
    """The counterweight: the guard must not refuse a real search. Without
    this, the fix would buy safety by never confirming an absence at all."""
    field = _run([_chunk(
        "This agreement contains no most favoured nation provision of any kind."
    )])

    assert field.status == "absent_from_document"
    assert field.validation_confidence is not None


def test_the_note_counts_chunks_swept_rather_than_chunks_present():
    """It reported ``len(ctx.chunks)``, which counts the blank ones it
    skipped. A reader checking the claim would look for a search larger than
    the one that happened."""
    field = _run([
        _chunk("This agreement has no MFN provision.", "real"),
        _chunk("   ", "blank1"),
        _chunk("   ", "blank2"),
    ])

    assert field.status == "absent_from_document"
    assert "across 1 swept chunk(s)" in field.notes


def test_a_field_that_already_has_a_value_is_left_alone():
    """Validator C only speaks to empty fields. A field with a value is
    Validator A's business, and absence is not a question about it."""
    fields = {
        FIELD: ExtractedField.single(
            value="0.50", status="confirmed",
            spans=[Span(start=0, end=4, text="0.50")],
        )
    }
    ctx = V.ValidationContext(
        doc=_Doc(), chunks=[_chunk("no MFN here")], fields=fields,
        specs={FIELD: FIELD_REGISTRY[FIELD]},
        session=JevSession(OfflineJev()), thresholds=load_thresholds(),
    )
    V.validator_c_negative_space(ctx)

    assert fields[FIELD].status == "confirmed"
    assert fields[FIELD].value == "0.50"
