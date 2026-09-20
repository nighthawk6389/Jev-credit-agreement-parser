"""Drafting conventions taken from 100 real agreements.

Every case here is a phrasing the pipeline got wrong, quoted from
``corpus/edgar``. They are unit tests rather than corpus tests so that they
run without unzipping 143MB, and the counts in the docstrings say how much of
the corpus each one stands for.

See ``docs/corpus_findings.md`` for the scan they came from.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from credit_extract.ingest.documents import parse_amendment_effects
from credit_extract.models.pricing import detect_base, parse_pricing


class _Doc:
    """The minimum ``parse_pricing`` reads: text, and no interest section."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.sections = []
        self.tables = []

    def section_span(self, section_id):     # noqa: D102, ANN001
        return None


# ---------------------------------------------------------------------------
# Benchmarks: a fifth of the corpus is not priced off SOFR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase,expected", [
    ("interest at SONIA plus the Applicable Margin", "sonia"),
    ("interest at EURIBOR plus the Margin", "euribor"),
    ("the Term CORRA Reference Rate for the relevant period", "corra"),
    ("the CDOR Rate for such Interest Period", "cdor"),
    ("TIBOR for the applicable Interest Period", "tibor"),
    ("the SARON rate on the Quotation Day", "saron"),
    ("the Canadian Prime Rate in effect", "canadian_prime"),
])
def test_non_dollar_benchmarks_are_recognised(phrase, expected):
    """SONIA 19, EURIBOR 18, CDOR/CORRA 16, TIBOR 6, SARON 2 of 100.

    All of it used to come back "unknown", which in a report reads the same as
    a document that could not be parsed at all.
    """
    base, _ = detect_base(phrase)
    assert base == expected


# ---------------------------------------------------------------------------
# Floors: usually structural, not a sentence
# ---------------------------------------------------------------------------


def test_a_floor_inside_the_benchmark_definition_is_found():
    """15 of 100 state their floor only this way.

    Quoted from the SLR HC BDC agreement. Because it sits inside the
    benchmark's own definition it applies before the margin -- a distinction
    worth 95bps at a SOFR of 0.05% with a 1.00% floor.
    """
    doc = _Doc(
        '"Term SOFR" means, for any calculation with respect to an Advance '
        "(other than an Advance bearing interest at the Alternate Base Rate), "
        "the greater of (i) 0.25% and (ii) the Term SOFR Reference Rate for a "
        "tenor of three (3) months."
    )
    pricing = parse_pricing(doc)
    assert pricing.floor is not None
    assert pricing.floor.to("percent").value == Decimal("0.25")
    assert pricing.floor_applies_to == "base_rate"


def test_a_spelled_out_structural_floor_is_found():
    """'the greater of (x) three percent (3.00%) per annum, or (y) ...'."""
    doc = _Doc(
        '"Term SOFR" means for any calculation with respect to a SOFR Advance, '
        "the greater of (x) three percent (3.00%) per annum, or (y) the Term "
        "SOFR Reference Rate for a tenor of one month."
    )
    pricing = parse_pricing(doc)
    assert pricing.floor is not None
    assert pricing.floor.to("percent").value == Decimal("3.00")


# ---------------------------------------------------------------------------
# The credit spread adjustment, under its other names
# ---------------------------------------------------------------------------


def test_credit_adjustment_spread_is_the_same_thing():
    """Two agreements define "Term SOFR Credit Adjustment Spread".

    Anchoring on "Credit Spread Adjustment" reads those as deals with no
    adjustment rather than as adjustments that were not found.
    """
    doc = _Doc(
        '"Term SOFR" means the sum of (i) Term SOFR Credit Adjustment Spread '
        "of 0.10% and (ii) the Term SOFR Reference Rate for a tenor comparable "
        "to the applicable Interest Period."
    )
    pricing = parse_pricing(doc)
    assert pricing.credit_spread_adjustment is not None
    assert pricing.credit_spread_adjustment.to("percent").value == Decimal("0.10")


def test_transition_boilerplate_is_not_a_credit_spread_adjustment():
    """Present in almost every agreement written since 2022.

    It promises an adjustment *if* the benchmark is ever replaced. Reading it
    as a term of the deal is what made this the loudest check in the suite.
    """
    doc = _Doc(
        "Upon the occurrence of a Benchmark Transition Event, the "
        "Administrative Agent may select a Benchmark Replacement, which shall "
        "include the related Benchmark Replacement Adjustment, being the "
        "spread adjustment recommended by the Relevant Governmental Body."
    )
    pricing = parse_pricing(doc)
    assert pricing.credit_spread_adjustment is None


# ---------------------------------------------------------------------------
# Amendments: the phrasings the corpus actually uses
# ---------------------------------------------------------------------------


class _Amendment:
    def __init__(self, text: str) -> None:
        from credit_extract.ingest.normalize import NormalizedDocument

        self.normalized = NormalizedDocument(
            document_id="amend", source_path="-", source_format="txt", text=text,
        )
        self.document_id = "amend"
        self.effective_date = None
        self.amendment_number = 1


@pytest.mark.parametrize("phrasing,occurrences", [
    ("is hereby amended and restated in its entirety to read as follows:", 2),
    ("is hereby amended and restated in its entirety as follows:", 6),
    ("is hereby amended in its entirety to read as follows:", 6),
    ("is hereby amended as follows:", 5),
    ("is hereby amended and restated as follows:", 3),
])
def test_every_restatement_phrasing_in_the_corpus_parses(phrasing, occurrences):
    """"to read as follows" -- the form originally required -- is the rarest.

    ``occurrences`` records how many of the 100 agreements use each form, so a
    future narrowing of these patterns has to argue with a number.
    """
    text = (
        f"Section 2.10 of the Credit Agreement {phrasing}\n\n"
        '"The Borrower shall repay the Term Loans in quarterly instalments of '
        '$376,250."\n\n'
        "(b) Section 2.11 of the Credit Agreement is hereby deleted.\n"
    )
    effects = parse_amendment_effects(_Amendment(text))
    restatements = [e for e in effects if e.kind == "restate"]
    assert restatements, f"{phrasing!r} parsed to no effect"
    assert restatements[0].target_section == "2.10"


def test_a_blackline_that_strikes_bold_text_is_still_a_blackline():
    """'delete the bold, stricken text' -- three agreements phrase it so."""
    text = (
        "SECTION 2.1. As of the Amendment Effective Date, the Credit Agreement "
        "is hereby amended to delete the bold, stricken text (indicated "
        "textually in the same manner as the following example: stricken text) "
        "and to add the bold, double-underlined text as set forth in the "
        "Conformed Agreement attached as Annex A hereto."
    )
    effects = parse_amendment_effects(_Amendment(text))
    redlines = [e for e in effects if e.kind == "redline"]
    assert redlines
    assert redlines[0].attachment == "Annex A"


# ---------------------------------------------------------------------------
# The tier boundary
# ---------------------------------------------------------------------------


class _RecordingModel:
    """Stands in for the LLM tier and records what it was asked."""

    name = "recording"

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def with_temperature(self, temperature: float):   # noqa: ANN201, D102
        return self

    def extract(self, doc, chunk, specs, context, pass_id):  # noqa: ANN001
        from credit_extract.extract.passes import CostLedger

        self.asked.append([spec.name for spec in specs])
        return [], CostLedger()


def test_the_model_is_never_asked_what_the_rules_already_settled():
    """The division of labour, as an assertion rather than a comment.

    ``run_passes`` used to take one backend, so a run was either all-patterns
    or all-model and the patterns were left covering the whole distribution
    alone. They cannot: across 100 real agreements "is hereby amended" takes
    24 distinct phrasings, 13 of them occurring once. The rules take what is
    cheap and unambiguous; everything else is the model's.
    """
    from credit_extract.extract.passes import LayeredBackend, OfflineRuleBackend
    from credit_extract.ingest.normalize import ingest
    from credit_extract.ingest.segment import segment_all
    from credit_extract.models.fpml_model import FIELD_REGISTRY

    doc = ingest("credit_extract/eval/gold/fixture_meridian_2017.html")
    segments = segment_all(doc, None)
    specs = list(FIELD_REGISTRY.values())
    model = _RecordingModel()
    backend = LayeredBackend(OfflineRuleBackend(), model)

    settled_somewhere = False
    for index, chunk in enumerate(segments["structural"]):
        found, _ = backend.extract(doc, chunk, specs, "", f"p{index}")
        settled = {c.field for c in found}
        if not settled:
            continue
        settled_somewhere = True
        # The model's call for this chunk is the last one recorded.
        assert not (settled & set(model.asked[-1])), (
            f"{sorted(settled & set(model.asked[-1]))} went to the model "
            "after the rules had already answered them"
        )
    assert settled_somewhere, "the rules settled nothing, so nothing was tested"


def test_the_report_says_which_tier_answered():
    from credit_extract.pipeline import run_pipeline

    result = run_pipeline("credit_extract/eval/gold/fixture_meridian_2017.html")
    note = next(n for n in result.report.notes if n.startswith("extraction by tier"))
    assert "rules=" in note and "tables=" in note
