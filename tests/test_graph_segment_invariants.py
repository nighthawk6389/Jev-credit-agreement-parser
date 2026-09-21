"""Definition graph, the three segmentations, and the deterministic invariants."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.graph.definitions import build_definition_graph
from credit_extract.ingest.segment import (
    SLIDING_OVERLAP, SLIDING_WINDOW_CHARS, coverage, segment_all,
    segment_definitional, segment_sliding, segment_structural,
)
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import AmortizationSchedule, ScheduleRow
from credit_extract.validate.invariants import (
    BasketRecord, CovenantStep, InvariantContext, check_all, registered,
)


@pytest.fixture(scope="module")
def graph(doc):
    return build_definition_graph(doc)


# ---------------------------------------------------------------------------
# Definition graph
# ---------------------------------------------------------------------------


def test_definitions_parse_into_a_graph(graph):
    assert len(graph) >= 20
    assert graph.get("Consolidated EBITDA") is not None
    assert graph.stats()["edges"] > 0


def test_leverage_resolves_several_definitions_deep(graph):
    """Long-tail errors cluster at depth >= 3."""
    term = "Senior Secured First Lien Net Leverage Ratio"
    assert graph.depth(term) >= 3
    closure = graph.closure(term)
    assert closure[0] == term
    for expected in (
        "Consolidated EBITDA",
        "Consolidated Senior Secured First Lien Net Indebtedness",
        "Consolidated Total Debt",
    ):
        assert expected in closure, f"{expected} missing from the closure"


def test_closure_context_is_what_the_extractor_actually_sees(graph):
    context = graph.context_for("Senior Secured First Lien Net Leverage Ratio")
    assert "Consolidated Total Debt" in context
    assert "Sponsor Model" in context, (
        "the external dependency has to be visible in context, not just in "
        "the graph"
    )


def test_cycles_are_detected(graph):
    """They exist, and they are usually drafting errors."""
    cycles = graph.cycles()
    assert cycles
    flat = {term for cycle in cycles for term in cycle}
    assert {"Restricted Subsidiary", "Unrestricted Subsidiary"} <= flat


def test_closure_terminates_on_a_cycle(graph):
    closure = graph.closure("Restricted Subsidiary")
    assert len(closure) == len(set(closure))


def test_external_documents_are_flagged(graph):
    externals = graph.external_document_terms()
    assert "Sponsor Model" in externals
    assert "Disclosure Letter" in externals


def test_a_term_with_no_external_dependency_reports_none(graph):
    assert graph.external_references("Closing Date") == []


def test_unknown_terms_resolve_to_nothing(graph):
    assert graph.closure("Not A Defined Term") == []
    assert graph.depth("Not A Defined Term") == 0


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_the_three_segmentations_are_genuinely_different(doc, graph):
    segments = segment_all(doc, graph)
    assert set(segments) == {"structural", "sliding", "definitional"}
    boundaries = {
        kind: {(s.start, s.end) for chunk in chunks for s in chunk.spans}
        for kind, chunks in segments.items()
    }
    assert boundaries["structural"] != boundaries["sliding"]
    assert boundaries["definitional"] != boundaries["structural"]


def test_the_sliding_window_covers_everything(doc):
    """Structure-blind by design: it is the only view that sees across a
    section boundary."""
    assert coverage(doc, segment_sliding(doc)) == pytest.approx(1.0)


def test_sliding_windows_overlap_by_the_configured_fraction(doc):
    chunks = segment_sliding(doc)
    if len(chunks) < 2:
        pytest.skip("document too short for multiple windows")
    stride = chunks[1].start - chunks[0].start
    expected = int(SLIDING_WINDOW_CHARS * (1 - SLIDING_OVERLAP))
    assert stride == expected


def test_a_definitional_chunk_spans_definition_and_use_sites(doc, graph):
    chunks = segment_definitional(doc, graph)
    ebitda = next(c for c in chunks if c.label == "Consolidated EBITDA")
    assert len(ebitda.spans) > 1, (
        "a definitional chunk gathers the definition and where it is used"
    )
    assert "Consolidated EBITDA" in ebitda.text


def test_a_quote_the_chunk_never_held_cannot_be_located(doc, graph):
    """Provenance depends on this: a fabricated quote must not resolve."""
    chunk = segment_definitional(doc, graph)[0]
    assert chunk.locate(doc, "this phrase appears nowhere in the agreement") is None


def test_a_located_quote_reads_back_from_the_document(doc, graph):
    chunks = segment_definitional(doc, graph)
    ebitda = next(c for c in chunks if c.label == "Consolidated EBITDA")
    span = ebitda.locate(doc, "Sponsor Model")
    assert span is not None
    assert doc.text[span.start:span.end] == "Sponsor Model"


def test_oversized_sections_are_split_on_paragraph_boundaries(doc):
    chunks = segment_structural(doc, max_chars=1200)
    assert all(len(c.text) <= 1600 for c in chunks)
    assert any("#" in c.chunk_id for c in chunks), "expected at least one split"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def _schedule(dates: list[date], amount: int = 100) -> AmortizationSchedule:
    return AmortizationSchedule(
        rows=[
            ScheduleRow(payment_date=d, amount=Decimal(amount), row_index=i)
            for i, d in enumerate(dates)
        ],
        original_principal=Decimal(10_000),
    )


def test_every_invariant_is_registered():
    names = registered()
    for expected in (
        "amortization_dates_strictly_increasing",
        "amortization_dates_evenly_spaced",
        "amortization_row_count_matches_quarters",
        "covenant_steps_monotonic",
        "revolver_maturity_before_term_maturity",
        "percentages_and_ratios_in_range",
        "mfn_trigger_ordering",
        "opening_leverage_consistent",
        "ebitda_baskets_have_identified_base",
        "actus_schedule_matches_document",
    ):
        assert expected in names


def test_a_clean_schedule_raises_nothing():
    clean = _schedule([date(2018, 3, 31), date(2018, 6, 30), date(2018, 9, 30)])
    assert check_all(InvariantContext(amortization=clean)) == []


def test_uneven_spacing_is_measured_in_quarters_not_days():
    """A 90-day gap and a 92-day gap are both one quarter."""
    schedule = _schedule([
        date(2017, 12, 31), date(2018, 3, 31), date(2018, 6, 30),
        date(2018, 9, 30),
    ])
    violations = check_all(InvariantContext(amortization=schedule))
    assert not [
        v for v in violations if v.invariant == "amortization_dates_evenly_spaced"
    ]


def test_a_skipped_quarter_is_flagged():
    schedule = _schedule([
        date(2018, 3, 31), date(2018, 6, 30), date(2019, 6, 30),
        date(2019, 9, 30),
    ])
    violations = check_all(InvariantContext(amortization=schedule))
    assert [
        v for v in violations if v.invariant == "amortization_dates_evenly_spaced"
    ]


def test_covenant_step_ups_are_flagged():
    steps = [
        CovenantStep(label="2018", level=Decimal("6.00")),
        CovenantStep(label="2019", level=Decimal("6.25")),
    ]
    violations = check_all(InvariantContext(covenant_steps=steps))
    assert [v for v in violations if v.invariant == "covenant_steps_monotonic"]


def test_monotonic_step_downs_pass():
    steps = [
        CovenantStep(label="2018", level=Decimal("6.50")),
        CovenantStep(label="2019", level=Decimal("6.25")),
        CovenantStep(label="2020", level=Decimal("6.25")),
    ]
    violations = check_all(InvariantContext(covenant_steps=steps))
    assert not [v for v in violations if v.invariant == "covenant_steps_monotonic"]


def _field(value, kind_class="economic_terms", span=None):
    span = span or Span(start=0, end=5, text="abcde")
    return ExtractedField.single(value=value, spans=[span], status="confirmed",
                          field_class=kind_class)


def test_a_revolver_outliving_the_term_loan_is_reported():
    ctx = InvariantContext(fields={
        "revolver.maturity_date": _field(date(2025, 8, 1)),
        "initial_term_loan.maturity_date": _field(date(2024, 8, 1)),
    })
    violations = [
        v for v in check_all(ctx)
        if v.invariant == "revolver_maturity_before_term_maturity"
    ]
    assert violations
    assert violations[0].severity == "warning"


def test_an_out_of_range_percentage_is_flagged():
    ctx = InvariantContext(fields={"libor_floor_pct": _field(Decimal("150"))})
    assert [
        v for v in check_all(ctx)
        if v.invariant == "percentages_and_ratios_in_range"
    ]


def test_stated_leverage_is_checked_against_extracted_inputs():
    ctx = InvariantContext(
        fields={
            "opening_total_leverage_ratio": _field(Decimal("2.00")),
            "initial_term_loan.commitment": _field(Decimal(150_500_000)),
        },
        hardcoded_ebitda_quarters={"q": Decimal(40_470_315)},
    )
    violations = [
        v for v in check_all(ctx) if v.invariant == "opening_leverage_consistent"
    ]
    assert violations, "2.00x against 3.72x of actual leverage must be caught"
    assert "3.7" in str(violations[0].observed)


def test_a_consistent_leverage_figure_passes():
    ctx = InvariantContext(
        fields={
            "opening_total_leverage_ratio": _field(Decimal("3.72")),
            "initial_term_loan.commitment": _field(Decimal(150_500_000)),
        },
        hardcoded_ebitda_quarters={"q": Decimal(40_470_315)},
    )
    assert not [
        v for v in check_all(ctx) if v.invariant == "opening_leverage_consistent"
    ]


def test_an_ebitda_basket_without_an_identified_base_is_flagged():
    """Pre- and post-add-back EBITDA differ, and the difference is material."""
    ctx = InvariantContext(baskets=[
        BasketRecord(name="b", ebitda_pct=Decimal(35), ebitda_base=None),
    ])
    violations = [
        v for v in check_all(ctx)
        if v.invariant == "ebitda_baskets_have_identified_base"
    ]
    assert violations
    assert violations[0].severity == "warning"


def test_an_identified_base_passes():
    ctx = InvariantContext(baskets=[
        BasketRecord(name="b", ebitda_pct=Decimal(35), ebitda_base="post_addback"),
    ])
    assert not [
        v for v in check_all(ctx)
        if v.invariant == "ebitda_baskets_have_identified_base"
    ]


def test_mfn_pari_passu_must_sit_below_the_junior_trigger():
    ctx = InvariantContext(mfn_triggers={
        "pari_passu": Decimal("0.75"), "junior": Decimal("0.50"),
    })
    assert [v for v in check_all(ctx) if v.invariant == "mfn_trigger_ordering"]


# ---------------------------------------------------------------------------
# Definition depth: linear, and it has to stay that way
# ---------------------------------------------------------------------------


def _graph(edges: dict[str, list[str]]):
    """A DefinitionGraph over bare terms, with the spans the model demands."""
    from credit_extract.graph.definitions import DefinitionGraph, DefinitionNode
    from credit_extract.models.core import Span

    span = Span(start=0, end=1, text="x")
    return DefinitionGraph(nodes={
        term: DefinitionNode(term=term, span=span, body="", uses=set(uses))
        for term, uses in edges.items()
    })


def _longest_simple_path(graph, term: str) -> int:
    """The original recursive definition, kept as the oracle to check against."""
    def walk(name: str, seen: frozenset[str]) -> int:
        node = graph.nodes.get(name)
        if node is None or not node.uses:
            return 0
        best = 0
        for used in node.uses:
            if used in seen:
                continue
            best = max(best, 1 + walk(used, seen | {used}))
        return best

    return walk(term, frozenset({term}))


def test_depth_agrees_with_the_exhaustive_definition_on_a_dag():
    """Memoising by term is exact on a DAG, which is what a good draft is."""
    graph = _graph({
        "A": ["B", "C"], "B": ["D"], "C": ["D", "E"],
        "D": ["F"], "E": ["F"], "F": [],
    })
    for term in graph.nodes:
        assert graph.depth(term) == _longest_simple_path(graph, term), term
    assert graph.depth("A") == 3          # A -> C -> D -> F


def test_a_cycle_does_not_make_depth_unbounded():
    """Cycles are real -- 96 of them in one agreement -- and are findings."""
    graph = _graph({"A": ["B"], "B": ["C"], "C": ["A"]})
    assert graph.depth("A") == 2
    assert graph.cycles(), "the cycle itself is the finding, and must still show"


def test_depth_is_not_exponential_in_the_number_of_cycles():
    """The regression that cost an hour on one real agreement.

    ``depth`` carried the path as a set and explored every simple path below a
    term, and ``stats`` called it once per term. A dense definitions article
    made that astronomical: 477 terms, 1,414 edges, 96 cycles, and the run had
    not finished after an hour. Linear now, so a graph this size is instant --
    the diamond below has 2**24 simple paths from the top.
    """
    import time

    edges: dict[str, list[str]] = {}
    for level in range(24):
        for branch in ("a", "b"):
            edges[f"n{level}{branch}"] = [f"n{level + 1}a", f"n{level + 1}b"]
    edges["n24a"] = ["n0a"]              # a cycle back to the top, for good measure
    edges["n24b"] = []
    graph = _graph(edges)

    started = time.monotonic()
    depths = graph.depths()
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"depths() took {elapsed:.1f}s on a 50-node graph"
    assert max(depths.values()) >= 24


# ---------------------------------------------------------------------------
# Every chunk has to be sendable
# ---------------------------------------------------------------------------


def _headingless(chars: int):
    """A long document with nothing a heading pattern can find."""
    from credit_extract.ingest.normalize import NormalizedDocument

    body = "\n\n".join(
        f"The Borrower shall deliver the quarterly report for period {i}."
        for i in range(chars // 60)
    )
    return NormalizedDocument(
        document_id="headingless", source_path="-", source_format="txt", text=body,
    )


def test_a_document_with_no_headings_is_still_split():
    """The crash that took two real filings out of a hundred-document run.

    ``segment_structural`` returned the whole document as one chunk when no
    sections were detected, ignoring its own size budget. One filing produced
    a single 887,000-character chunk -- 222,000 tokens of state against a
    64,000-token context -- and the pipeline raised instead of reporting.
    """
    from credit_extract.ingest.segment import segment_structural

    doc = _headingless(200_000)
    assert not doc.sections, "the fixture must have no detectable headings"
    chunks = segment_structural(doc)
    assert len(chunks) > 1
    assert all(len(c.text) <= 12_000 for c in chunks)


def test_no_segmentation_emits_a_chunk_too_large_to_send():
    """The guarantee, stated once for all three segmentations.

    Every chunk becomes the state of a Jev request at some point, and an
    unsendable one takes the whole document down with it -- findings and all.
    """
    from credit_extract.ingest.segment import SENDABLE_MAX_CHARS, segment_all

    doc = _headingless(200_000)
    for kind, chunks in segment_all(doc).items():
        oversized = [c.chunk_id for c in chunks if len(c.text) > SENDABLE_MAX_CHARS]
        assert not oversized, f"{kind} emitted unsendable chunks: {oversized[:3]}"


def test_the_cap_is_derived_from_the_limit_it_protects():
    """So the two cannot drift apart silently."""
    from credit_extract.ingest.segment import SENDABLE_MAX_CHARS
    from credit_extract.validate.jev import STATE_PLUS_QUESTION_TOKENS, estimate_tokens

    assert estimate_tokens("x" * SENDABLE_MAX_CHARS) < STATE_PLUS_QUESTION_TOKENS


def test_a_split_chunk_keeps_its_segmentation():
    """A definitional chunk split for size is still definitional.

    Reconciliation counts agreement across segmentations, so a chunk
    relabelled by the splitter would inflate apparent independent support.
    """
    from credit_extract.ingest.segment import Chunk, enforce_sendable
    from credit_extract.models.core import Span

    doc = _headingless(120_000)
    big = Chunk(
        chunk_id="defn:Whatever", segmentation="definitional", label="Whatever",
        spans=[Span(start=0, end=len(doc.text), text=doc.text,
                    document_id=doc.document_id)],
        text=doc.text,
    )
    parts = enforce_sendable(doc, [big])
    assert len(parts) > 1
    assert all(p.segmentation == "definitional" for p in parts)
    assert all(p.label == "Whatever" for p in parts)


# ---------------------------------------------------------------------------
# Definitions as real agreements write them
# ---------------------------------------------------------------------------


def _doc_with(body: str):
    from credit_extract.ingest.normalize import NormalizedDocument

    return NormalizedDocument(
        document_id="d", source_path="-", source_format="txt",
        text="ARTICLE I DEFINITIONS\n\n" + body,
    )


def test_a_definition_written_with_a_colon_is_a_definition():
    """Ten of twenty-five corpus agreements use it; four almost exclusively.

    At 317 to 491 colon definitions against 5 to 9 written with "means", a
    parser that only knows "means" sees an empty definitions article.
    """
    from credit_extract.graph.definitions import build_definition_graph

    graph = build_definition_graph(_doc_with(
        '"Floor": 1.00% per annum.\n\n'
        '"Maturity Date": July 16, 2029, as extended.\n'
    ))
    assert set(graph.nodes) == {"Floor", "Maturity Date"}


def test_a_term_spaced_inside_its_quotes_is_still_the_term():
    """EDGAR sets a defined term as its own styled run.

    The quotes and the term arrive as separate nodes, so the normalizer writes
    `" Floor "`. Requiring the term to start immediately after the quote found
    one definition in a 189,000-character defined-terms article.
    """
    from credit_extract.graph.definitions import build_definition_graph

    graph = build_definition_graph(_doc_with(
        '" Floor ": 1.00% per annum.\n\n'
        '" ARR Net Leverage Ratio ": the ratio of Floor to one.\n'
    ))
    assert set(graph.nodes) == {"Floor", "ARR Net Leverage Ratio"}
    assert "Floor" in graph.get("ARR Net Leverage Ratio").uses


def test_depth_does_not_change_between_runs():
    """`uses` is a set, and set order varies under hash randomisation.

    An edge back into a term already on the path contributes nothing, so a
    different traversal breaks each cycle at a different edge. One real
    agreement reported 197, 196 and 194 terms at depth three or more on three
    consecutive runs. A number that moves on identical input is worth less
    than no number.
    """
    from credit_extract.graph.definitions import build_definition_graph

    body = "".join(
        f'"Term {i}": means Term {i + 1} and Term {(i * 7) % 40}.\n\n'
        for i in range(40)
    ) + '"Term 40": means Term 0.\n'
    runs = {
        tuple(sorted(build_definition_graph(_doc_with(body)).depths().items()))
        for _ in range(5)
    }
    assert len(runs) == 1, "depths() is not deterministic"
