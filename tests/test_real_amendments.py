"""The amendment conventions real filings actually use.

Every assertion here was written against a document in ``corpus/real/``, after
the synthetic fixtures had been passing for some time. The synthetic amendments
were written by the same hand that wrote the parser, so they used the parser's
phrasing; the real ones do not, and the parser found nothing in either of them.

Two gaps, both one-line differences with total consequences:

* the restatement patterns required "amended and restated in its entirety **to
  read** as follows" and section identifiers of the form ``2.10``. Real
  amendments write "as follows" and number their targets ``2.1(a)(ii)(B)(3)``;
* an entire amendment mechanism was missing. A blackline carries its changes as
  typography -- struck-through deletions, underlined insertions -- and says so
  in one sentence. Flatten it to text and both survive side by side.

The blackline case is the one worth keeping: in a real Ares Capital amendment
the facility size reads ``Up to U.S. $ 2,150,000,000 2,250,000,000`` once the
markup is gone, and a first-match parser reports the deleted figure with full
confidence. Nothing in the flattened text looks wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from credit_extract.ingest.documents import (
    AmendmentEffect, assemble_set, load_document, parse_amendment_effects,
)
from credit_extract.ingest.normalize import ingest

REAL = Path(__file__).resolve().parents[1] / "corpus" / "real"
WELLS_FARGO = REAL / "wf_third_amendment_2017.mht"
ARES = REAL / "ares_cp_funding_amendment_2025.mht"

pytestmark = pytest.mark.skipif(
    not REAL.exists(), reason="real corpus not present"
)


# ---------------------------------------------------------------------------
# Prose amendments: "as follows", and subsection targets
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wells_fargo_effects() -> list[AmendmentEffect]:
    return parse_amendment_effects(load_document(WELLS_FARGO))


def test_prose_amendment_is_not_silently_empty(wells_fargo_effects):
    """The regression this file exists for: zero effects parsed, no error."""
    assert len(wells_fargo_effects) >= 15


def test_as_follows_without_to_read(wells_fargo_effects):
    restatements = [e for e in wells_fargo_effects if e.is_restatement]
    assert len(restatements) >= 8


def test_deeply_numbered_subsection_is_the_target(wells_fargo_effects):
    """``2.1(a)(ii)(B)(3)``, not ``2.1``: the subsection is where the money is."""
    targets = {e.target_section for e in wells_fargo_effects}
    assert "2.1(a)(ii)(B)(3)" in targets
    assert "2.15(b)(iii)" in targets


def test_enumerated_reference_swaps_each_become_an_effect(wells_fargo_effects):
    """One sentence, one section anchor, two independent date changes.

    Only the first swap in "amended to (i) delete ... and (ii) delete ..."
    carries a section reference; a pattern that needs one per swap reports half
    the changes and gives no sign the other half existed.
    """
    swaps = [
        (e.old_fragment, e.new_fragment)
        for e in wells_fargo_effects
        if e.target_section == "2.15(c)"
    ]
    assert ("September 30, 2015", "December 31, 2016") in swaps
    assert ("March 31, 2020", "September 30, 2021") in swaps


def test_basket_increases_are_found(wells_fargo_effects):
    """Two covenant baskets change size in this amendment."""
    swaps = {
        (e.old_fragment, e.new_fragment) for e in wells_fargo_effects
        if e.kind == "replace_text"
    }
    assert ("$500,000", "$830,000") in swaps
    assert ("$5,000,000", "$8,300,000") in swaps


def test_table_restatement_targets_the_table_not_the_section(wells_fargo_effects):
    """"The table set forth in Section 2.2(b) is restated" replaces the table.

    Applied as a whole-section restatement it would delete the prose around the
    table and leave nothing to say so.
    """
    partial = [e for e in wells_fargo_effects if e.is_partial]
    assert {e.target_section for e in partial} >= {"2.2(b)", "2.2(d)"}
    assert all(e.target_element == "table" for e in partial)
    assert not any(e.is_restatement for e in partial)


# ---------------------------------------------------------------------------
# Blacklines: the changes are typography
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ares():
    return ingest(ARES)


def test_deleted_text_never_enters_the_offset_space(ares):
    """The headline term, and the number it replaced.

    The text carries the new figure alone. The superseded one is kept in the
    sidecar, so the change is auditable, but it cannot be quoted back as a
    term because it is not in the text any span can address.
    """
    assert ares.is_blackline
    assert "2,150,000,000" not in ares.text
    assert "2,250,000,000" in ares.text
    assert any(r.text == "2,150,000,000" for r in ares.deletions())


def test_the_deletion_is_reported_beside_the_span_that_replaced_it(ares):
    where = ares.text.find("Up to U.S.")
    nearby = [r.text for r in ares.deleted_near(where, where + 40)]
    assert "2,150,000,000" in nearby


def test_blackline_is_recognised_as_an_amendment_mechanism():
    effects = parse_amendment_effects(load_document(ARES))
    redlines = [e for e in effects if e.kind == "redline"]
    assert redlines, "a blackline that parses to no effects is a silent failure"
    assert redlines[0].attachment == "Appendix A"
    assert redlines[0].target_section == AmendmentEffect.WHOLE_AGREEMENT


def test_blackline_carrying_a_conformed_copy_is_self_sufficient():
    """It attaches the whole agreement, so there is a base after all."""
    document_set = assemble_set([load_document(ARES)])
    assert not document_set.base_is_missing
    kinds = {f.kind for f in document_set.findings}
    assert "blackline_restates_agreement" in kinds
    assert "base_agreement_absent" not in kinds


def test_amendment_with_no_agreement_to_amend_says_so():
    """A prose amendment filed alone cannot yield operative terms."""
    document_set = assemble_set([load_document(WELLS_FARGO)])
    assert document_set.base_is_missing
    finding = next(
        f for f in document_set.findings if f.kind == "base_agreement_absent"
    )
    assert finding.severity == "error"
    # The changes are still enumerated: a reader can act on what it says it
    # changes even when the pipeline cannot assemble the result.
    assert len(document_set.declared_effects()) >= 15


def test_ordinary_filings_carry_no_deletion_markup():
    """Strike-through detection must not fire on ordinary formatting."""
    for name in ("health_catalyst_2024.mht", "cik1901612_ex10-1_2024.mht"):
        document = ingest(REAL / name)
        assert not document.is_blackline, name
