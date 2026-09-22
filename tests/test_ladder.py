"""The extraction ladder: rules, then the definitions, then the sections.

The ladder replaces a flat walk that asked every chunk of every segmentation
for every field. What these tests pin is not the saving -- that is measured in
docs/ -- but the three things the saving must not cost: a cheap tier's answer
must stay appealable, agreement must stay evidence, and absence must stay a
claim somebody searched for.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from credit_extract.extract.ladder import (
    CORROBORATE_AT_OR_ABOVE, LadderResult, StageRecord, _anchor_groups,
    _prior_keys, _prior_spans, _prior_text, _tag, open_fields,
)
from credit_extract.extract.passes import Candidate
from credit_extract.extract.reconcile import ValueGroup, reconcile
from credit_extract.models.core import Span
from credit_extract.models.fpml_model import FIELD_REGISTRY


def _span(start: int, end: int) -> Span:
    return Span(start=start, end=end, text="x" * (end - start))


def _cand(
    field: str = "revolver.commitment",
    value=Decimal("100"),
    pass_id: str = "structural/sweep",
    segmentation: str = "structural",
    span: Span | None = None,
    confidence: float = 0.9,
) -> Candidate:
    return Candidate(
        field=field, value=value, span=span or _span(10, 40),
        confidence=confidence, pass_id=pass_id, segmentation=segmentation,
    )


# ---------------------------------------------------------------------------
# An echo is not a second reading
# ---------------------------------------------------------------------------


def test_agreeing_with_a_value_you_were_shown_at_the_same_span_is_an_echo():
    """One wrong rule, shown to three passes, must not come back as
    three-way corroboration."""
    prior = _span(10, 40)
    candidate = _cand(span=prior)
    _tag([candidate], {"revolver.commitment": candidate.key()},
         {"revolver.commitment": prior})

    assert candidate.echoes
    assert not candidate.overturns


def test_agreeing_at_a_different_span_is_not_an_echo():
    """The test is the citation, not the value. A pass that found the same
    figure somewhere else in the document has corroborated it, however it was
    prompted -- and refusing that would throw away real evidence."""
    candidate = _cand(span=_span(900, 930))
    _tag([candidate], {"revolver.commitment": candidate.key()},
         {"revolver.commitment": _span(10, 40)})

    assert not candidate.echoes


def test_a_prior_with_no_span_to_compare_is_treated_as_an_echo():
    """The conservative reading. Crediting support we cannot see is the
    failure mode that matters here."""
    candidate = _cand()
    _tag([candidate], {"revolver.commitment": candidate.key()}, {})

    assert candidate.echoes


def test_disagreeing_with_a_shown_value_is_an_overturn():
    """The reason for showing the prior at all."""
    candidate = _cand(value=Decimal("250"))
    _tag([candidate], {"revolver.commitment": "100"},
         {"revolver.commitment": _span(10, 40)})

    assert candidate.overturns
    assert not candidate.echoes


def test_an_unprompted_candidate_is_neither():
    candidate = _cand()
    assert not candidate.echoes
    assert not candidate.overturns


def test_reconciliation_excludes_echoes_from_support_but_keeps_them():
    """An echo is weak evidence, not no evidence: its span and value stay in
    the record, and only the corroboration count refuses it."""
    prior = _span(10, 40)
    independent = _cand(segmentation="structural", span=_span(900, 930))
    echo = _cand(segmentation="sliding", span=prior)
    _tag([echo], {"revolver.commitment": echo.key()},
         {"revolver.commitment": prior})

    group = ValueGroup(
        key=independent.key(), value=independent.value,
        candidates=[independent, echo],
    )
    assert group.support == 1
    assert group.echoed_segmentations == {"sliding"}
    assert len(group.candidates) == 2, "the echo stays in the record"


def test_a_group_reports_whether_some_pass_overturned_an_alternative():
    overturning = _cand(value=Decimal("250"))
    _tag([overturning], {"revolver.commitment": "100"},
         {"revolver.commitment": _span(10, 40)})
    group = ValueGroup(
        key=overturning.key(), value=overturning.value,
        candidates=[overturning],
    )
    assert group.overturned


# ---------------------------------------------------------------------------
# A cheap tier's answer stays appealable
# ---------------------------------------------------------------------------


def test_the_model_is_still_asked_about_fields_the_rules_answered():
    """Closing a field because a regex answered it would make the cheapest
    tier unappealable, which is the opposite of the trade showing priors is
    meant to buy. ``open_fields`` narrows on values found, and the ladder
    deliberately does not use it to gate the sweep's first visit."""
    specs = list(FIELD_REGISTRY.values())
    rules_found = [_cand(field="revolver.commitment")]

    still_open = open_fields(specs, rules_found)
    assert "revolver.commitment" not in {s.name for s in still_open}

    # But the deal-defining fields stay in the sweep's ask regardless, which
    # is what actually reaches the model.
    assert FIELD_REGISTRY["revolver.commitment"].criticality >= (
        CORROBORATE_AT_OR_ABOVE
    )


def test_deal_defining_fields_are_swept_even_once_answered():
    """A field answered in the definitions and skipped by all three sweeps
    has one reading behind it, from one view. For pricing, principal and
    maturity that is too thin."""
    critical = [
        name for name, spec in FIELD_REGISTRY.items()
        if spec.criticality >= CORROBORATE_AT_OR_ABOVE
    ]
    assert len(critical) >= 20, "the corroborated set should not be tiny"
    assert "revolver.commitment" in critical
    assert "initial_term_loan.maturity_date" in critical


def test_the_prior_block_asks_for_a_check_rather_than_offering_an_answer():
    """A model told 'this is the answer' confirms it. A model told 'verify
    this against the text' is being asked to do the thing that makes showing
    it worth the anchoring risk."""
    from credit_extract.extract.prompts import build_extraction_prompt

    specs = [FIELD_REGISTRY["revolver.commitment"]]
    rows = _prior_text(specs, {"revolver.commitment": "1300000000"})
    assert "1300000000" in rows

    prompt = build_extraction_prompt("excerpt", specs, priors=rows)
    assert "verify" in prompt.lower()
    assert "different value" in prompt.lower()
    assert "1300000000" in prompt


def test_priors_are_not_presented_to_the_model_as_defined_terms():
    """They were, briefly, because both went in through the same argument.
    A prior value under a heading reading DEFINED TERMS IN SCOPE tells the
    model to resolve it as a definition."""
    from credit_extract.extract.prompts import build_extraction_prompt

    specs = [FIELD_REGISTRY["revolver.commitment"]]
    prompt = build_extraction_prompt(
        "excerpt", specs,
        context="'Commitment' means the amount set out opposite each Lender.",
        priors=_prior_text(specs, {"revolver.commitment": "1300000000"}),
    )

    defined_at = prompt.index("DEFINED TERMS IN SCOPE")
    priors_at = prompt.index("ALREADY READ BY A CHEAPER PASS")
    assert defined_at < prompt.index("'Commitment' means") < priors_at, (
        "the definition must sit under the definitions heading"
    )
    assert priors_at < prompt.index("1300000000")


def test_a_prompt_with_no_priors_carries_no_priors_heading():
    from credit_extract.extract.prompts import build_extraction_prompt

    specs = [FIELD_REGISTRY["revolver.commitment"]]
    assert "ALREADY READ" not in build_extraction_prompt("excerpt", specs)


def test_the_prior_block_is_empty_when_nothing_was_found():
    specs = [FIELD_REGISTRY["revolver.commitment"]]
    assert _prior_text(specs, {}) == ""


# ---------------------------------------------------------------------------
# Priors are built from the best answer, with its span
# ---------------------------------------------------------------------------


def test_priors_take_the_most_confident_answer_per_field():
    weak = _cand(value=Decimal("100"), confidence=0.4, span=_span(10, 40))
    strong = _cand(value=Decimal("250"), confidence=0.9, span=_span(50, 80))

    assert _prior_keys([weak, strong])["revolver.commitment"] == strong.key()
    assert _prior_spans([weak, strong])["revolver.commitment"] == strong.span


def test_priors_ignore_candidates_with_no_value():
    empty = _cand(value=None, span=None)
    assert _prior_keys([empty]) == {}
    assert _prior_spans([empty]) == {}


# ---------------------------------------------------------------------------
# Orientation groups by the defined term the fields hang off
# ---------------------------------------------------------------------------


def test_fields_sharing_an_anchor_are_asked_for_together():
    """One call per defined term rather than per field. Sending the same
    closure once for every field that depends on it is the saving consulting
    the graph makes possible."""
    specs = [s for s in FIELD_REGISTRY.values() if s.definition_anchors]
    groups = _anchor_groups(specs)

    assert groups, "some registry fields name their anchors"
    assert len(groups) < len(specs), (
        "grouping should collapse fields onto shared anchors"
    )
    for anchor, group in groups.items():
        for spec in group:
            assert anchor in spec.definition_anchors


def test_fields_with_no_anchor_are_left_to_the_sweep():
    """Orientation reads definitions. A field that names none has nothing for
    it to read, and inventing an anchor would send the wrong text."""
    specs = [s for s in FIELD_REGISTRY.values() if not s.definition_anchors]
    assert specs, "the fixture assumes some fields have no anchors"
    assert _anchor_groups(specs) == {}


# ---------------------------------------------------------------------------
# Absence is only worth what the search behind it was worth
# ---------------------------------------------------------------------------


def test_the_ladder_records_which_sections_were_searched_per_field():
    """Validator C is the only thing that may call a field absent, and that
    is a confident status inside the silent-error budget. It needs to be able
    to assert coverage rather than assume it."""
    result = LadderResult()
    other = LadderResult(searched={"mfn_sunset": {"s1", "s2"}})
    result.merge(other)

    assert result.searched["mfn_sunset"] == {"s1", "s2"}


def test_merging_ladder_results_unions_the_search_record():
    a = LadderResult(searched={"mfn_sunset": {"s1"}})
    b = LadderResult(searched={"mfn_sunset": {"s2"}, "lc_sublimit": {"s3"}})
    a.merge(b)

    assert a.searched["mfn_sunset"] == {"s1", "s2"}
    assert a.searched["lc_sublimit"] == {"s3"}


def test_an_exhausted_sweep_says_so_and_a_truncated_one_does_not():
    """A budget that cut the sweep short makes every absence claim in that
    run unsafe, and the run has to say which it was."""
    full = LadderResult()
    assert full.swept_exhaustively

    full.merge(LadderResult(swept_exhaustively=False))
    assert not full.swept_exhaustively


def test_stage_records_describe_what_they_did():
    record = StageRecord(stage="sweep", calls=103, chunks_visited=103)
    record.settled.update({"a", "b"})
    assert "103 call(s)" in record.describe()
    assert "settled 2" in record.describe()
