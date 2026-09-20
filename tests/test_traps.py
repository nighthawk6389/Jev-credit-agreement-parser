"""The four traps, end to end through the whole pipeline.

Trap 1 has its own file (``test_trap1_amortization.py``) because it is the
architecture's first checkpoint and is asserted at the invariant level. These
tests assert the *pipeline output* -- what a reader of result.json actually
sees.
"""

from __future__ import annotations

import pytest

from credit_extract.eval import traps
from credit_extract.pipeline import run_pipeline


@pytest.fixture(scope="module")
def result(agreement_path):
    return run_pipeline(agreement_path)


def test_all_four_traps_are_caught(result):
    checks = traps.check_all(result)
    missed = [c for c in checks if not c.caught]
    assert not missed, "\n".join(
        f"{c.trap}: {c.detail}\n  evidence: {c.evidence}" for c in missed
    )


def test_trap_2_override_is_located_not_merely_suspected(result):
    """Detecting "something overrides EBITDA" is not enough to act on."""
    check = traps.check_trap_2(result)
    assert check.caught
    findings = check.evidence["override_findings"]
    assert findings, "the governing provision must be quoted, not just counted"
    assert any(
        "deemed to be" in f["text"] or "Notwithstanding" in f["text"]
        for f in findings
    )


def test_trap_3_reports_a_document_not_a_number(result):
    """The stated 25% cap is the wrong answer, and a confident one."""
    field = result.fields["consolidated_ebitda.addback_cap_clause_a_xvi"]
    assert field.status == "external_reference"
    assert field.value is None, (
        "any number here is wrong: the cap lives in the Sponsor Model"
    )
    assert "Sponsor Model" in (field.external_document or "")
    assert field.spans, "an external reference still has to cite the clause"

    # The visible cap is a real term -- it just governs different clauses.
    visible = result.fields["consolidated_ebitda.addback_cap_pct"]
    assert visible.value is not None
    assert visible.value != field.value


def test_trap_3_is_reachable_through_the_definition_graph(doc):
    """Structure finds it, not a pattern match on the clause."""
    from credit_extract.graph.definitions import build_definition_graph

    graph = build_definition_graph(doc)
    references = graph.external_references("Consolidated EBITDA")
    assert any("Sponsor Model" in r.document for r in references)
    assert "Sponsor Model" in graph.external_document_terms()


def test_trap_4_distinguishes_silence_from_failure(result):
    """`null` conveys nothing. `absent_from_document` conveys a deal term."""
    field = result.fields["mfn_sunset"]
    assert field.status == "absent_from_document"
    assert field.value is None
    assert field.validation_source == "C_negative_space"
    assert field.validation_confidence is not None
    assert field.is_resolved, "a resolved absence is an answer, not a gap"

    # The protection itself is present at 50bps -- that is what makes the
    # missing sunset a finding rather than a missing section.
    mfn = result.fields["mfn_threshold_pct"]
    assert mfn.status == "confirmed"
    assert str(mfn.value) == "0.50"


def test_no_field_ends_as_a_bare_null(result):
    """A null is never a final answer."""
    unresolved = result.unresolved()
    assert unresolved == [], f"unresolved fields: {unresolved}"


def test_orphan_sweep_finds_a_provision_outside_the_registry(result):
    """Section 2.16 Call Protection is deliberately not an extraction target.

    Nothing in the field registry points at it, so only the inverted question
    -- "does this text say something the summary does not capture?" -- can
    surface it.
    """
    orphans = result.report.orphan_chunks
    assert orphans, "the sweep must find the provision no field targets"
    assert any("Call Protection" in o.text for o in orphans)
    for orphan in orphans:
        assert orphan.signals, "an orphan must carry the signals that flagged it"
        assert orphan.text, "an orphan is only actionable with its text attached"
