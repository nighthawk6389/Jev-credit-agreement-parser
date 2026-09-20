"""The checked-in fixture must stay in step with its generator.

If the generator drifts from the committed fixture, every trap assertion is
being made against a document nobody regenerated, and the arithmetic in the
spec silently stops being what the tests are checking.
"""

from __future__ import annotations

import json
from pathlib import Path

from credit_extract.eval.gold.build_fixture import (
    build_html, build_labels, gold_corpus,
)

GOLD = Path(__file__).resolve().parents[1] / "credit_extract" / "eval" / "gold"
FIXTURE = GOLD / "fixture_meridian_2017.html"
LABELS = GOLD / "fixture_meridian_2017.labels.json"


def test_the_default_variant_reproduces_the_committed_fixture():
    assert build_html() == FIXTURE.read_text(encoding="utf-8"), (
        "regenerate with `python credit_extract/eval/gold/build_fixture.py`"
    )


def test_the_committed_labels_match_the_generator():
    assert build_labels() == json.loads(LABELS.read_text())


def test_the_fixture_carries_the_arithmetic_the_spec_states():
    trap = build_labels()["traps"]["trap_1_duplicated_amortization_rows"]
    assert trap["printed_rows"] == 31
    assert trap["true_quarters"] == 27
    assert trap["literal_total"] == 11_663_750
    assert trap["correct_total"] == 10_158_750
    assert trap["literal_total"] - trap["correct_total"] == 1_505_000

    ebitda = build_labels()["traps"]["trap_2_hardcoded_opening_ebitda"]
    assert list(ebitda["quarters"].values()) == [
        11_206_749, 10_115_035, 9_449_315, 9_699_216
    ]

    mfn = build_labels()["traps"]["trap_4_absent_mfn_sunset"]
    assert mfn["mfn_threshold_pct"] == 0.50
    assert mfn["sunset"] is None


def test_the_fixture_declares_itself_synthetic():
    """It must not be mistakable for a real filing by a real issuer."""
    html = FIXTURE.read_text(encoding="utf-8")
    assert "SYNTHETIC TEST FIXTURE" in html
    assert "Not a real credit agreement" in html
    assert build_labels()["synthetic"] is True


def test_the_gold_corpus_is_deterministic():
    first = gold_corpus(12)
    second = gold_corpus(12)
    assert [v.variant_id for v in first] == [v.variant_id for v in second]
    assert build_html(first[5]) == build_html(second[5])


def test_gold_corpus_variant_ids_are_unique():
    """Colliding ids overwrite each other and shrink the corpus silently."""
    ids = [v.variant_id for v in gold_corpus(24)]
    assert len(ids) == len(set(ids))


def test_every_variant_is_internally_consistent():
    """A document that contradicts itself before ingestion tests nothing."""
    for variant in gold_corpus(24):
        payments = variant.amortization_dates()
        assert payments[-1] < variant.term_maturity, (
            f"{variant.variant_id}: last payment {payments[-1]} is not before "
            f"maturity {variant.term_maturity}"
        )
        assert variant.revolver_maturity <= variant.term_maturity
        assert variant.quarterly_amort > 0
        assert (
            variant.quarterly_amort * variant.unique_quarters
            < variant.term_principal
        ), "amortization must not exceed principal"


def test_variants_vary_where_it_matters():
    corpus = gold_corpus(24)
    assert len({v.term_principal for v in corpus}) > 10
    assert len({v.mfn_sunset_months for v in corpus}) > 1
    assert any(v.duplicated_year is not None for v in corpus)
    assert any(v.duplicated_year is None for v in corpus)
    assert any(v.decoy for v in corpus)
    assert any(not v.sponsor_model_clause for v in corpus)
