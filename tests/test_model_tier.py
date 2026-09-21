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


def test_a_chunk_stored_as_a_citation_still_fires():
    """The regression citations_cite_a_value was written for: a block of text
    where a quotation belongs, against a field nothing was claimed about."""
    field = ExtractedField[str].single(
        value=None,
        spans=[Span(start=0, end=9_000, text="x" * 9_000)],
        status="needs_review", field_class="economic_terms", criticality=4,
    )
    violations = check_all(InvariantContext(
        document_id="d", fields={"mfn_sunset": field},
    ))
    assert [v.invariant for v in violations if v.invariant == "citations_cite_a_value"]


def test_an_evidenced_absence_is_not_a_defect():
    """The opposite case, which only appeared once a model tier ran: no value
    and a real, short quotation that is exactly the evidence for having none.

    A maturity defined as five years after an undated event, a spread
    adjustment that applies only if the benchmark is ever replaced. A reader
    following the span gets the passage that explains the empty field, which
    is the opposite of being sent nowhere.
    """
    quote = (
        '"Maturity Date" means, with respect to each Facility, the date that '
        "is five (5) years after the Funding Date."
    )
    field = ExtractedField[str].single(
        value=None,
        spans=[Span(start=0, end=len(quote), text=quote)],
        status="needs_review", field_class="dates", criticality=5,
        notes="not a date: the Funding Date is an event, not a date",
    )
    violations = check_all(InvariantContext(
        document_id="d", fields={"initial_term_loan.maturity_date": field},
    ))
    assert not [v for v in violations if v.invariant == "citations_cite_a_value"]


# ---------------------------------------------------------------------------
# Tier 4: the targeted re-read that had never run
# ---------------------------------------------------------------------------


def test_the_orphan_sweep_re_read_reaches_the_record():
    """``run_pipeline`` accepted a ``reread`` callable, ``rescue_orphans``
    used it, ``build_reread_prompt`` existed -- and no caller ever supplied
    one, so the tier had never fired. It also discarded what it found: the
    candidates were used to label the orphan "rescued" and then dropped.
    """
    from credit_extract.models.core import CostLedger
    from credit_extract.pipeline import run_pipeline

    root = Path(__file__).resolve().parents[1]
    source = root / "credit_extract" / "eval" / "gold" / "fixture_meridian_2017.html"
    target = "mfn_sunset"

    class OnlyOnReread:
        """Silent on the main passes, talkative on the re-read."""

        name = "stub"

        def extract(self, doc, chunk, specs, context, pass_id):
            if not pass_id.startswith("reread"):
                return [], CostLedger()
            wanted = {spec.name for spec in specs}
            if target not in wanted:
                return [], CostLedger()
            quote = chunk.text.strip()[:40]
            span = chunk.locate(doc, quote)
            if span is None:
                return [], CostLedger()
            return [Candidate(
                field=target, value="eighteen months", span=span,
                confidence=0.7, pass_id=pass_id, segmentation=chunk.segmentation,
            )], CostLedger()

    result = run_pipeline(source, extraction_backend=OnlyOnReread())
    field = result.fields[target]
    assert field.value == "eighteen months", (
        "a re-read that finds the field the first pass missed has to put it in "
        "the record; labelling the orphan rescued and dropping the value is "
        "the expensive half of the work and none of the useful half"
    )
    assert field.status == "needs_review", (
        "one chunk and one pass has none of the independent support the main "
        "passes are built to produce, so it answers a question nobody answered "
        "and is never confirmed from here"
    )
    assert "orphan sweep" in (field.notes or "")
    assert any("orphan re-read (tier 4)" in note for note in result.report.notes)


def test_a_re_read_never_overturns_a_value_the_passes_agreed_on():
    from credit_extract.models.core import CostLedger, Span
    from credit_extract.pipeline import _fill_from_rescue

    settled = ExtractedField[str].single(
        value="twelve months", spans=[Span(start=0, end=5, text="hello")],
        status="confirmed", field_class="economic_terms", criticality=3,
    )
    empty = ExtractedField[str].single(
        value=None, status="needs_review", field_class="economic_terms",
        criticality=3,
    )
    fields = {"mfn_sunset": settled, "facility.feature": empty}
    span = Span(start=0, end=5, text="hello")
    filled = _fill_from_rescue(fields, [
        Candidate(field="mfn_sunset", value="six months", span=span,
                  confidence=0.99, pass_id="reread", segmentation="structural"),
        Candidate(field="facility.feature", value="delayed draw", span=span,
                  confidence=0.4, pass_id="reread", segmentation="structural"),
    ])
    assert filled == ["facility.feature"]
    assert fields["mfn_sunset"].value == "twelve months"


# ---------------------------------------------------------------------------
# Routes: the same model tier, sent somewhere else
# ---------------------------------------------------------------------------


def test_the_gateway_spells_the_model_creator_first():
    from credit_extract.extract.passes import DIRECT, VERCEL

    assert DIRECT.model_id("claude-opus-5") == "claude-opus-5"
    assert VERCEL.model_id("claude-opus-5") == "anthropic/claude-opus-5"


def test_an_already_qualified_model_is_not_prefixed_twice():
    from credit_extract.extract.passes import VERCEL

    assert VERCEL.model_id("anthropic/claude-opus-5") == "anthropic/claude-opus-5"


def test_a_routed_model_is_still_priced_as_the_model_it_is():
    """ANTHROPIC_PRICES is keyed on the bare id, and an unknown key falls
    through to a default rate. A creator-first id would miss the table and be
    costed at $3/$15 against Opus's $5/$25 -- a cost ledger that is quietly
    wrong in the cheap direction, on every run through the gateway."""
    from credit_extract.extract.passes import (
        ANTHROPIC_PRICES, VERCEL, _anthropic_cost,
    )

    routed = VERCEL.model_id("claude-opus-5")
    assert routed not in ANTHROPIC_PRICES, "the premise of this test"
    assert _anthropic_cost(VERCEL.billed_model(routed), 1_000_000, 0) == 5.00
    assert _anthropic_cost(routed, 1_000_000, 0) != 5.00, (
        "and this is what it would have cost if the id were not stripped"
    )


def test_the_backend_name_follows_the_route():
    """Thresholds are fitted per backend and load_thresholds refuses a
    mismatch, so a set fitted against one route must not be applied to the
    other without somebody deciding that it may be."""
    from credit_extract.extract.passes import VERCEL, AnthropicBackend

    assert AnthropicBackend().name == "anthropic"
    assert AnthropicBackend(route=VERCEL).name == "vercel"
    assert AnthropicBackend(route=VERCEL).with_temperature(0.4).name == "vercel"


def test_a_route_reads_its_own_credential(monkeypatch):
    from credit_extract.extract.passes import DIRECT, VERCEL

    for var in ("ANTHROPIC_API_KEY", "AI_GATEWAY_API_KEY",
                "VERCEL_AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert DIRECT.credential() is None and VERCEL.credential() is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "direct")
    assert DIRECT.credential() == "direct"
    assert VERCEL.credential() is None, (
        "the gateway must not pick up an Anthropic key and send it somewhere "
        "Anthropic is not"
    )

    monkeypatch.setenv("VERCEL_AI_GATEWAY_API_KEY", "fallback")
    assert VERCEL.credential() == "fallback"
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "preferred")
    assert VERCEL.credential() == "preferred", "first var set wins, in order"


def test_a_route_without_a_credential_fails_before_the_first_chunk(monkeypatch):
    """Not on chunk one of nine hundred: by then the cheap tiers have run and
    the partial result reports partial recall as recall."""
    import argparse

    from credit_extract.cli import _build_backends

    for var in ("AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    args = argparse.Namespace(
        backend="vercel", jev="offline", model="claude-opus-5", temperature=0.0
    )
    with pytest.raises(SystemExit) as caught:
        _build_backends(args, Path("x.htm"))
    assert "AI_GATEWAY_API_KEY" in str(caught.value)


def test_both_routes_send_the_identical_prompt_and_schema():
    """A route is a base URL, a credential and a naming rule. If it were also
    a different request then a number taken through one would not be
    comparable to a number taken through the other, which is the only reason
    to have built it this way."""
    import inspect

    from credit_extract.extract.passes import VERCEL, AnthropicBackend

    source = inspect.getsource(AnthropicBackend.extract)
    assert source.count("client.messages.create(") == 1
    assert "build_extraction_prompt" in source and "EXTRACTION_SCHEMA" in source
    assert "if self.route" not in source, (
        "the route may decide where a request goes and how the model is "
        "spelled; it may not decide what the request says"
    )
    assert "self.route.model_id" in source
    assert AnthropicBackend(route=VERCEL).effort == AnthropicBackend().effort


def test_the_client_actually_points_where_the_route_says(monkeypatch):
    """The one thing about a route that cannot be reasoned about: whether the
    SDK accepts the override and sends the request to the host we named.

    Neither endpoint is reachable from this environment -- api.anthropic.com
    needs a key and ai-gateway.vercel.sh is refused by the egress proxy -- so
    this stops at the client, which is as far as an offline test honestly
    goes. It is still the difference between having configured a base URL and
    having assumed one.
    """
    anthropic = pytest.importorskip("anthropic")
    from credit_extract.extract.passes import DIRECT, VERCEL, AnthropicBackend

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "fake-gateway-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-direct-key")

    gateway = AnthropicBackend(route=VERCEL)._ensure_client()
    assert str(gateway.base_url).rstrip("/") == "https://ai-gateway.vercel.sh"
    assert gateway.api_key == "fake-gateway-key"

    direct = AnthropicBackend(route=DIRECT)._ensure_client()
    assert "api.anthropic.com" in str(direct.base_url)
    assert direct.api_key == "fake-direct-key"
    assert isinstance(gateway, anthropic.Anthropic)
