"""Document sets and amendment chains (family F05).

The failure this defends against is the one a single-document evaluation
cannot see: extracting the base agreement alone reports terms that stopped
being true years ago, with every span correct and every figure accurately
quoted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from credit_extract.eval.gold.build_fixture import (
    Variant, build_amendment, build_html, replace_change, restate_change,
)
from credit_extract.ingest.documents import (
    apply_chain, assemble_set, classify_role, load_document,
    operative_document, parse_amendment_effects,
)
from credit_extract.pipeline import run_document_set, run_pipeline

VARIANT = Variant(variant_id="chain_test")

RESTATED_COVENANT = (
    "SECTION 6.12 Financial Covenant. Holdings will not permit the Total "
    "Leverage Ratio as of the last day of any Test Period to exceed 7.00:1.00."
)


@pytest.fixture(scope="module")
def chain_files(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("chain")
    files = {
        "base": directory / "base.html",
        "a1": directory / "amendment_1.html",
        "a3": directory / "amendment_3.html",
    }
    files["base"].write_text(build_html(VARIANT))
    files["a1"].write_text(build_amendment(
        VARIANT, 1, date(2018, 6, 15),
        [replace_change("2.09", "0.50%", "0.375%", "a")],
    ))
    files["a3"].write_text(build_amendment(
        VARIANT, 3, date(2020, 3, 10),
        [
            restate_change("6.12", RESTATED_COVENANT),
            replace_change("2.14", "0.50%", "0.75%", "b"),
        ],
    ))
    return files


# ---------------------------------------------------------------------------
# Classification and ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head,role,number",
    [
        ("AMENDMENT NO. 4 TO CREDIT AGREEMENT", "amendment", 4),
        ("THIRD AMENDMENT TO CREDIT AGREEMENT", "amendment", 3),
        ("AMENDED AND RESTATED CREDIT AGREEMENT",
         "amendment_and_restatement", None),
        ("CREDIT AGREEMENT dated as of August 1, 2017", "base", None),
        ("INTERCREDITOR AGREEMENT", "intercreditor", None),
        ("SECURITY AGREEMENT", "security", None),
    ],
)
def test_roles_are_read_from_the_head(head, role, number):
    detected_role, detected_number, _ = classify_role(head)
    assert detected_role == role
    assert detected_number == number


def test_an_amendment_to_an_ar_is_an_amendment_not_a_restatement():
    """Both phrases appear in the title; the number is what separates them."""
    role, number, _ = classify_role(
        "AMENDMENT NO. 3 TO THE AMENDED AND RESTATED CREDIT AGREEMENT"
    )
    assert role == "amendment"
    assert number == 3


def test_the_chain_orders_by_effective_date_not_argument_order(chain_files):
    document_set = assemble_set([
        load_document(chain_files["a3"]),
        load_document(chain_files["base"]),
        load_document(chain_files["a1"]),
    ])
    assert [d.role for d in document_set.chain] == ["base", "amendment", "amendment"]
    assert [d.amendment_number for d in document_set.amendments] == [1, 3]
    dates = [d.effective_date for d in document_set.chain]
    assert dates == sorted(dates)


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


def test_both_restatement_and_in_place_edits_are_read(chain_files):
    effects = parse_amendment_effects(load_document(chain_files["a3"]))
    kinds = {e.kind for e in effects}
    assert kinds == {"restate", "replace_text"}
    assert {e.target_section for e in effects} == {"6.12", "2.14"}


def test_a_restatement_body_does_not_swallow_the_next_clause(chain_files):
    """Regression: an unbounded body absorbed the following amendment.

    The clause after a restatement then vanished, and the amendment that
    changed the MFN trigger was never applied.
    """
    effects = parse_amendment_effects(load_document(chain_files["a3"]))
    restatement = next(e for e in effects if e.kind == "restate")
    assert "Section 2.14" not in (restatement.new_text or "")
    assert any(e.target_section == "2.14" for e in effects)


def test_every_effect_applies(chain_files):
    document_set = assemble_set([
        load_document(p) for p in chain_files.values()
    ])
    operative = apply_chain(document_set)
    assert len(operative.applied) == 3
    assert operative.unapplied == []


def test_the_operative_text_carries_the_amended_terms(chain_files):
    document_set = assemble_set([load_document(p) for p in chain_files.values()])
    operative = apply_chain(document_set)
    assert "0.375% per annum" in operative.text
    assert "0.50% per annum on the average" not in operative.text
    assert "7.00:1.00" in operative.text
    assert "more than 0.75% per annum" in operative.text


def test_tables_survive_the_chain_with_valid_offsets(chain_files):
    """Regression: editing the text directly destroyed every table.

    Trap 1 lives in a table, so an amendment chain used to disarm it.
    """
    document_set = assemble_set([load_document(p) for p in chain_files.values()])
    operative = apply_chain(document_set)
    document = operative_document(document_set, operative)

    amortization = [t for t in document.tables if t.looks_like("Payment Date")]
    assert amortization, "the amortization table must survive the chain"
    for cell in amortization[0].cells:
        assert document.text[cell.start:cell.end] == cell.text


def test_provenance_points_at_the_document_that_wrote_the_text(chain_files):
    document_set = assemble_set([load_document(p) for p in chain_files.values()])
    operative = apply_chain(document_set)
    base_id = document_set.base.document_id
    assert operative.source_of("2.10") == base_id           # untouched
    assert operative.source_of("6.12") != base_id           # restated
    assert operative.source_of("2.09") != base_id           # patched


# ---------------------------------------------------------------------------
# The finding a single document cannot produce
# ---------------------------------------------------------------------------


def test_base_only_extraction_reports_superseded_terms(chain_files):
    """The whole point of F05, stated as a test."""
    base_only = run_pipeline(chain_files["base"])
    operative = run_document_set(list(chain_files.values()))

    assert str(base_only.fields["commitment_fee_pct"].value) == "0.50"
    assert str(operative.fields["commitment_fee_pct"].value) == "0.375"

    assert str(base_only.fields["mfn_threshold_pct"].value) == "0.50"
    assert str(operative.fields["mfn_threshold_pct"].value) == "0.75"

    assert base_only.fields["financial_covenant.opening_level"].value != (
        operative.fields["financial_covenant.opening_level"].value
    )


def test_spans_are_attributed_to_the_amending_document(chain_files):
    operative = run_document_set(list(chain_files.values()))
    fee = operative.fields["commitment_fee_pct"]
    assert fee.spans
    assert fee.spans[0].document_id is not None


def test_the_chain_report_says_what_was_applied(chain_files):
    result = run_document_set(list(chain_files.values()))
    chain = result.report.chain
    assert len(chain["documents"]) == 3
    assert len(chain["effects_applied"]) == 3
    assert chain["effects_unapplied"] == []
    assert set(chain["sections_amended"]) == {"2.09", "2.14", "6.12"}


def test_traps_still_fire_through_a_chain(chain_files):
    from credit_extract.eval import traps

    result = run_document_set(list(chain_files.values()))
    missed = [c for c in traps.check_all(result) if not c.caught]
    assert not missed, [c.detail for c in missed]


# ---------------------------------------------------------------------------
# Ambiguity and truncation
# ---------------------------------------------------------------------------


def test_conflicting_restatements_are_reported_not_resolved(chain_files, tmp_path):
    """Two amendments restating one section differently on the same date."""
    rival = tmp_path / "amendment_4.html"
    rival.write_text(build_amendment(
        VARIANT, 4, date(2020, 3, 10),
        [restate_change(
            "6.12",
            "SECTION 6.12 Financial Covenant. Holdings will not permit the "
            "Total Leverage Ratio to exceed 5.00:1.00.",
        )],
    ))
    document_set = assemble_set(
        [load_document(p) for p in [*chain_files.values(), rival]]
    )
    kinds = {f.kind for f in document_set.findings}
    assert "operative_version_ambiguous" in kinds
    finding = next(
        f for f in document_set.findings
        if f.kind == "operative_version_ambiguous"
    )
    assert finding.section == "6.12"
    assert len(finding.documents) == 2


def test_an_amended_and_restated_agreement_truncates_the_chain(
    chain_files, tmp_path
):
    restated = tmp_path / "ar.html"
    restated.write_text(
        build_html(VARIANT)
        .replace("<p>CREDIT AGREEMENT</p>",
                 "<p>AMENDED AND RESTATED CREDIT AGREEMENT</p>", 1)
        .replace("dated as of August 1, 2017", "dated as of June 1, 2021", 1)
    )
    document_set = assemble_set(
        [load_document(p) for p in [*chain_files.values(), restated]]
    )
    assert document_set.base.role == "amendment_and_restatement"
    assert len(document_set.superseded) == 3
    assert document_set.amendments == []


def test_operative_as_of_excludes_later_amendments(chain_files):
    document_set = assemble_set(
        [load_document(p) for p in chain_files.values()],
        operative_as_of=date(2019, 1, 1),
    )
    assert [d.amendment_number for d in document_set.amendments] == [1]
    assert len(document_set.superseded) == 1


def test_an_amendment_aimed_at_a_missing_section_is_flagged(chain_files, tmp_path):
    stray = tmp_path / "amendment_9.html"
    stray.write_text(build_amendment(
        VARIANT, 9, date(2021, 1, 5),
        [replace_change("8.44", "x", "y", "a")],
    ))
    document_set = assemble_set(
        [load_document(p) for p in [*chain_files.values(), stray]]
    )
    assert any(
        f.kind == "amendment_target_missing" and f.section == "8.44"
        for f in document_set.findings
    )


def test_an_unapplicable_effect_is_recorded_never_dropped(chain_files, tmp_path):
    """An amendment that silently fails to apply is a term reported wrongly."""
    stray = tmp_path / "amendment_8.html"
    stray.write_text(build_amendment(
        VARIANT, 8, date(2021, 2, 2),
        [replace_change("2.09", "no such text in the section", "z", "a")],
    ))
    document_set = assemble_set(
        [load_document(p) for p in [*chain_files.values(), stray]]
    )
    operative = apply_chain(document_set)
    assert operative.unapplied
    effect, reason = operative.unapplied[0]
    assert effect.target_section == "2.09"
    assert "not in Section" in reason


# ---------------------------------------------------------------------------
# Ordering, when the documents will not carry it
# ---------------------------------------------------------------------------


def _doc(document_id: str, role: str, number: int | None, when: date | None):
    from credit_extract.ingest.documents import Document

    return Document(
        document_id=document_id, path=f"{document_id}.html", role=role,
        amendment_number=number, effective_date=when, title=document_id,
    )


def test_an_undated_amendment_does_not_become_the_base():
    """The bug this guards, from a real pair of filings.

    sort_key defaults a missing effective date to date.min, which sorts ahead
    of everything, and the first document in a chain is taken as the agreement
    the rest are applied to. BRC Group filed Amendment No. 1 dated and
    Amendment No. 5 undated; No. 5 became the base and No. 1 was applied on
    top of it. Two amendments, applied in reverse, no error raised, every span
    correct.
    """
    from credit_extract.ingest.documents import chain_order

    ordered = chain_order([
        _doc("five", "amendment", 5, None),
        _doc("one", "amendment", 1, date(2026, 6, 30)),
    ])
    assert [d.document_id for d in ordered] == ["one", "five"]


def test_dates_still_win_when_every_document_has_one():
    """Numbering is the fallback, not the rule.

    An amendment can be numbered in one sequence and take effect in another --
    a No. 6 agreed before a No. 5 closes is unusual but it happens, and the
    dates are what the parties actually operated under.
    """
    from credit_extract.ingest.documents import chain_order

    ordered = chain_order([
        _doc("later_number", "amendment", 6, date(2026, 1, 1)),
        _doc("earlier_number", "amendment", 5, date(2026, 6, 1)),
    ])
    assert [d.document_id for d in ordered] == ["later_number", "earlier_number"]


def test_a_base_agreement_leads_its_chain_whatever_the_dates_say():
    from credit_extract.ingest.documents import chain_order

    ordered = chain_order([
        _doc("amendment", "amendment", 1, None),
        _doc("agreement", "base", None, date(2026, 5, 1)),
    ])
    assert ordered[0].document_id == "agreement"


def test_ambiguous_numbering_falls_back_to_dates():
    """Two amendments sharing a number order nothing; do not pretend it does."""
    from credit_extract.ingest.documents import chain_order

    ordered = chain_order([
        _doc("b", "amendment", 3, None),
        _doc("a", "amendment", 3, date(2026, 2, 1)),
    ])
    assert len(ordered) == 2      # ordered somehow, but not by the numbering


# ---------------------------------------------------------------------------
# Definitions are not the articles they live in
# ---------------------------------------------------------------------------


def _amendment_text(body: str):
    from credit_extract.ingest.documents import Document
    from credit_extract.ingest.normalize import NormalizedDocument

    return Document(
        document_id="a", path="a.html", role="amendment", amendment_number=1,
        normalized=NormalizedDocument(
            document_id="a", source_path="-", source_format="txt", text=body,
        ),
    )


def test_amending_one_definition_targets_the_definition():
    """Quoted from Wheels Up Amendment No. 5.

    Read as a restatement of Section 1.01 this replaces the whole definitions
    article with a single definition.
    """
    from credit_extract.ingest.documents import parse_amendment_effects

    effects = parse_amendment_effects(_amendment_text(
        'SECTION 2. Amendment. As of the Amendment Effective Date, the '
        'definition of the defined term "Revolving Availability Period" set '
        "forth in Section 1.01 of the Credit Agreement is hereby deleted in "
        'its entirety and replaced as follows: "" Revolving Availability '
        'Period " shall mean the period from and including the Closing Date '
        'to but excluding September 20, 2028."\n'
    ))
    assert len(effects) == 1
    assert effects[0].is_partial
    assert effects[0].target_element == "definition:Revolving Availability Period"
    assert not effects[0].is_restatement


def test_a_restatement_that_names_definitions_is_never_a_section_restatement():
    """The guard for the form that cannot be parsed.

    AIR T Amendment No. 7 restates nine named definitions in Section 1.01,
    with a DocuSign stamp and an image filename interleaved into the middle
    of the list. The list cannot be read; it must still not be applied as a
    restatement of Section 1.01.
    """
    from credit_extract.ingest.documents import parse_amendment_effects

    effects = parse_amendment_effects(_amendment_text(
        '3. Amendments. (a) The definition of the terms "Borrowing Base", '
        '"Leverage Ratio",\n\nsome-scanned-page.jpg\n\n2 "Loans", "Total '
        'Usage" appearing in Section 1.01 of the Original Agreement are '
        "hereby amended in their respective entireties to read as follows: "
        '" \'Borrowing Base\' means, at any date of determination, 85% of '
        'Eligible Accounts."\n'
    ))
    restatements = [e for e in effects if e.target_section == "1.01"]
    assert restatements, "the sentence must still produce an effect"
    assert all(e.is_partial for e in restatements), (
        "a sentence naming definitions must never restate the whole article"
    )
