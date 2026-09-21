"""The model tier: what it must raise, what it must replay, what must survive.

Every test here fails for something that was true of this repository before
the first model-tier extraction was ever run through it. Five of them are
regressions on defects a single held-out document found in one afternoon,
which is the argument for having run it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from credit_extract.extract import recorded as rec
from credit_extract.extract.passes import (
    Candidate, EXTRACTION_SCHEMA, ExtractionFailed, ExtractionRefused,
    ExtractionTruncated, ExtractionUnparseable, _parse_llm_payload,
    parse_pricing_grid_text, run_passes,
)
from credit_extract.extract.reconcile import reconcile
from credit_extract.ingest.normalize import ingest
from credit_extract.ingest.segment import segment_all
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.validate.invariants import InvariantContext, check_all


# ---------------------------------------------------------------------------
# The adapter: a failure is not an empty document
# ---------------------------------------------------------------------------


def _doc_and_chunk():
    root = Path(__file__).resolve().parents[1]
    doc = ingest(root / "credit_extract" / "eval" / "gold" / "fixture_meridian_2017.html")
    chunks = segment_all(doc)["structural"]
    return doc, chunks[0]


def test_a_response_that_is_not_json_raises_rather_than_reporting_nothing():
    """The one confusion this repository exists to prevent, in the hard tier.

    The old parser scraped the first JSON-looking substring out of free text
    and returned ``[]`` when that failed, so a truncated answer, a refusal and
    a chunk genuinely containing none of these fields were indistinguishable
    at the call site -- and two of the three are wrong.
    """
    doc, chunk = _doc_and_chunk()
    specs = list(FIELD_REGISTRY.values())
    with pytest.raises(ExtractionUnparseable):
        _parse_llm_payload("I'm sorry, I can't help with that.", doc, chunk, specs, "p")


def test_a_json_response_in_the_wrong_shape_also_raises():
    doc, chunk = _doc_and_chunk()
    specs = list(FIELD_REGISTRY.values())
    with pytest.raises(ExtractionUnparseable):
        _parse_llm_payload('[{"field": "closing_date"}]', doc, chunk, specs, "p")


def test_every_extraction_failure_is_catchable_as_one_kind():
    """``run_passes`` catches ``ExtractionFailed``; a sibling that escaped it
    would abort a 500,000-character agreement over one unreadable chunk."""
    for cls in (ExtractionRefused, ExtractionTruncated, ExtractionUnparseable):
        assert issubclass(cls, ExtractionFailed)


def test_the_schema_demands_a_quote_for_every_field():
    required = EXTRACTION_SCHEMA["properties"]["fields"]["items"]["required"]
    assert "quote" in required, (
        "a value without a quote has no provenance and cannot be located, so "
        "the API contract itself has to require one"
    )
    assert EXTRACTION_SCHEMA["properties"]["fields"]["items"]["additionalProperties"] is False


def test_an_unreadable_chunk_is_recorded_rather_than_read_as_empty():
    doc, _ = _doc_and_chunk()
    segments = segment_all(doc)

    class Refuses:
        name = "refuses"

        def extract(self, *args, **kwargs):
            raise ExtractionRefused("a safety classifier declined this chunk")

    result = run_passes(doc, segments, Refuses(), include_tables=False, passes=1)
    assert result.candidates == []
    assert result.unread_chunks, (
        "every chunk failed and the result claims nothing was found in any of "
        "them; the report has to be able to say which ones were never read"
    )
    assert "declined" in result.unread_chunks[0]


# ---------------------------------------------------------------------------
# The recorded backend: a replay, not an imitation
# ---------------------------------------------------------------------------


def _recording(**overrides) -> rec.Recording:
    body = {
        "document": "fixture",
        "model": "a reader",
        "recorded_on": date(2026, 9, 21),
        "recorded_before_labels": True,
        "fields": [],
    }
    body.update(overrides)
    return rec.Recording.model_validate(body)


def test_a_recorded_quote_the_chunk_does_not_hold_is_dropped_and_reported():
    """Same rule as a live answer: a quote that is not in the text is a
    fabrication, and the value goes in the bin rather than into the record."""
    doc, chunk = _doc_and_chunk()
    recording = _recording(fields=[
        {"field": "closing_date", "value": "January 1, 2017",
         "quote": "a sentence that is nowhere in this document"},
    ])
    backend = rec.RecordedBackend(recording)
    found, ledger = backend.extract(
        doc, chunk, list(FIELD_REGISTRY.values()), "", "recorded:1"
    )
    assert found == []
    assert backend.unplaced() == ["closing_date"], (
        "a recording that drifted from the document must say so, or it reports "
        "a lower recall than it actually claims and the gap looks like a model "
        "getting things wrong"
    )
    assert ledger.llm_calls == 0, "a replay did not spend what a live pass would"


def test_a_field_the_rules_already_settled_is_not_counted_as_drift():
    """``LayeredBackend`` only forwards what is left. A recorded field that was
    never asked for is not a quote that failed to locate."""
    doc, chunk = _doc_and_chunk()
    backend = rec.RecordedBackend(_recording(fields=[
        {"field": "closing_date", "value": "January 1, 2017", "quote": "nowhere"},
    ]))
    backend.extract(doc, chunk, [], "", "recorded:1")
    assert backend.unplaced() == []


def test_check_refuses_a_recording_made_after_the_labels(tmp_path):
    """The ordering is the whole basis for the number. A recording made with
    the answers in view measures self-consistency and reads like recall."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps({
        "document": "x", "model": "a reader", "recorded_on": "2026-09-21",
        "recorded_before_labels": False, "fields": [],
    }))
    problems = rec.check(tmp_path)
    assert problems and "recorded_before_labels" in problems[0]


def test_check_refuses_a_value_with_no_quote(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({
        "document": "x", "model": "a reader", "recorded_on": "2026-09-21",
        "recorded_before_labels": True,
        "fields": [{"field": "closing_date", "value": "2017-01-01", "quote": "  "}],
    }))
    assert any("no quote" in p for p in rec.check(tmp_path))


def test_check_refuses_a_field_the_registry_does_not_have(tmp_path):
    """Otherwise it surfaces as an unplaced quote, which reads as drift in the
    document rather than as a name that never existed."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps({
        "document": "x", "model": "a reader", "recorded_on": "2026-09-21",
        "recorded_before_labels": True,
        "fields": [{"field": "not.a.field", "value": "1", "quote": "whatever"}],
    }))
    assert any("not a field in the registry" in p for p in rec.check(tmp_path))


def test_the_checked_in_recordings_are_scorable():
    assert rec.check() == []


def test_every_checked_in_recording_names_a_document_that_exists():
    for name, recording in rec.load_recordings().items():
        assert rec.for_document(name) == recording
        if recording.corpus_name:
            assert rec.for_document(recording.corpus_name + ".htm") == recording


# ---------------------------------------------------------------------------
# What the first real model-tier run broke on its way through the pipeline
# ---------------------------------------------------------------------------


def test_a_populated_boolean_field_does_not_take_the_report_down():
    """``bool`` is an ``int`` in Python and ``Decimal("False")`` raises. Three
    fields in the registry are booleans and no run had ever populated one, so
    the range check crashed the whole pipeline on the first that did."""
    field = ExtractedField[bool].single(
        value=False,
        spans=[Span(start=0, end=7, text="Dollars")],
        status="confirmed", field_class="economic_terms", criticality=3,
    )
    ctx = InvariantContext(
        document_id="d", fields={"facility.multi_currency": field},
    )
    check_all(ctx)  # must not raise


def test_a_value_the_field_type_cannot_hold_is_not_reported_as_no_value():
    """A REIT leverage covenant is written as "60%" and the field is typed as
    a ratio. Coercing it invents a leverage of sixty times; dropping it says
    the document is silent about a covenant it devotes a section to."""
    doc, chunk = _doc_and_chunk()
    spec = FIELD_REGISTRY["financial_covenant.level"]
    quote = doc.text[chunk.spans[0].start:chunk.spans[0].start + 60]
    payload = json.dumps({"fields": [
        {"field": spec.name, "value": "60%", "quote": quote, "confidence": 0.8}
    ]})
    found = _parse_llm_payload(payload, doc, chunk, [spec], "p")
    assert len(found) == 1
    assert found[0].value is None, "nothing may be asserted from a value that did not type"
    assert found[0].qualifiers.get("untypable_value") == "60%"
    assert "cannot hold" in (found[0].notes or "")


def test_reconciliation_keeps_the_reading_behind_a_valueless_field():
    """It used to flatten every valueless candidate to "produced no value",
    losing the span it cited and the reason it had none -- and the negative
    space check then wrote "the extractor probably missed it" over the top."""
    doc, chunk = _doc_and_chunk()
    span = doc.span(chunk.spans[0].start, chunk.spans[0].start + 40)
    candidate = Candidate(
        field="financial_covenant.level", value=None, span=span,
        confidence=0.8, pass_id="recorded:1", segmentation="structural",
        qualifiers={"untypable_value": "60%"},
        notes="stated as '60%', which this field's ratio type cannot hold",
    )
    name = "financial_covenant.level"
    field = reconcile([candidate], {name: FIELD_REGISTRY[name]}, 1).fields[name]
    assert field.qualifiers.get("untypable_value") == "60%"
    assert field.spans, "the passage it was read from is the whole content of the field"
    assert "60%" in (field.notes or "")


def test_a_grid_with_more_columns_than_the_parser_knows_claims_nothing():
    """Four margin columns -- revolver and term loan, against SOFR and base
    rate -- and the text parser named the last one the Eurodollar margin. It
    is the base rate spread, and it went in at 0.93 confidence, which outranks
    every other tier."""
    grid = (
        "I | Credit Rating Level 1 | 0.675% | 0.000% | 0.750% | 0.000%\n"
        "V | Credit Rating Level 5 | 1.350% | 0.750% | 1.550% | 0.550%\n"
    )

    class FakeDoc:
        text = grid

        def span(self, start, end):
            return Span(start=start, end=end, text=grid[start:end])

    rows = parse_pricing_grid_text(FakeDoc())
    assert rows, "the rows are still read; it is the column naming that was wrong"
    assert all("eurodollar rate" not in row for row in rows), (
        "with an unknown column layout the parser must not name one"
    )
