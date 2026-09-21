"""The labelling contract: the split, the scaffold, and the guide that binds them.

These are guards on a process rather than on a computation, which makes them
easy to write loosely. They are written tightly on purpose: each one fails for
exactly one reason, and that reason is a way the evidence base could quietly
stop meaning what it says.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from credit_extract.eval import split as split_mod
from credit_extract.eval.assertions import SCAFFOLD_MARKER, load_assertion_file
from credit_extract.eval.label import MIN_CRITICALITY
from credit_extract.models.fpml_model import FIELD_REGISTRY

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "labelling_guide.md"


def test_the_split_on_disk_is_the_one_the_rule_produces():
    """The whole value of the split is that nobody chose it.

    A split someone can edit after seeing the labels is not a holdout, it is a
    preference. The assignment is a function of the document names, so this
    recomputes it and compares -- which is also what CI does.
    """
    assert split_mod.check() == []


def test_a_document_cannot_be_quietly_moved_to_the_fit_side(tmp_path):
    original = split_mod.SPLIT_FILE.read_text()
    moved = re.sub(r"( \S+): holdout", r"\1: fit", original, count=1)
    assert moved != original, "the split has no holdout documents to move"

    tampered = tmp_path / "split.yaml"
    tampered.write_text(moved)
    problems = split_mod.check(tampered)
    assert problems, "moving a document between sides must be visible"
    assert "the derivation says holdout" in problems[0]


def test_every_stratum_contributes_a_holdout_document():
    """A holdout that skips a deal type cannot measure that deal type."""
    split = split_mod.load_split()
    held = {split_mod.stratum_of(d) for d in split.documents("holdout")}
    present = {split_mod.stratum_of(d) for d in split.assignment}
    assert present - held == set(), (
        f"no holdout document for strata {sorted(present - held)}"
    )


def test_documents_used_while_building_the_parser_stay_out_of_the_holdout():
    """They were read, and their quirks are in the patterns.

    A holdout containing a document the author already studied measures
    memorisation at best. The contaminated list is the record of which ones
    those are, and it only ever grows.
    """
    split = split_mod.load_split()
    assert split.contaminated, "the list cannot be empty; seven files were hand-written"
    for document in split.contaminated:
        assert split.side_of(document) != "holdout", document


def test_a_chain_member_resolves_to_one_side_of_the_split():
    """Label files name chain members by the tail of the harvest filename.

    If that lookup misses, a chain is extracted from documents on one side and
    scored as though it came from the other.
    """
    split = split_mod.load_split()
    assert split.side_of("ex-101xwheelsupamendno5toc") == "fit"
    assert split.side_of("no-such-document-anywhere") == "unassigned"


def test_the_scaffold_selects_the_registry_and_not_a_second_list():
    """37 fields at criticality 4+, which is the roadmap's 25-40 without a
    parallel schema that then drifts from the real one."""
    critical = [s for s in FIELD_REGISTRY.values() if s.criticality >= MIN_CRITICALITY]
    assert len(critical) == 37, (
        "the registry moved; update the count in docs/labelling_guide.md with it"
    )
    assert f"{len(critical)} fields" in GUIDE.read_text()


def test_a_scaffold_cannot_be_loaded_as_ground_truth(tmp_path):
    """It holds the extractor's own answers. Scoring against them would report
    perfect agreement and measure nothing."""
    scaffold = tmp_path / "scaffold.yaml"
    scaffold.write_text(
        "document: x\nsource: real\ntier: 2\nassertions:\n"
        "  - id: x_one\n"
        "    family: F01_integrity\n"
        "    member: duplicated_table_rows\n"
        "    kind: field_value\n"
        "    target: revolver.commitment\n"
        "    expect: 1\n"
        f"    note: >-  # {SCAFFOLD_MARKER}\n"
        "      a quote\n"
    )
    with pytest.raises(ValueError, match=SCAFFOLD_MARKER):
        load_assertion_file(scaffold)


def test_the_labels_that_exist_still_load():
    """The marker check runs on every load, so a false positive would take the
    whole evidence base offline."""
    labels = Path(__file__).resolve().parents[1] / "credit_extract" / "eval" / "labels"
    files = sorted(labels.glob("*.yaml"))
    assert len(files) >= 7
    for path in files:
        load_assertion_file(path)


def test_the_guide_describes_the_statuses_the_pipeline_actually_has():
    """A guide naming a status the code does not have sends a labeller to
    write assertions that can never pass."""
    from credit_extract.eval.assertions import CONFIDENT_STATUSES

    text = GUIDE.read_text()
    for status in CONFIDENT_STATUSES:
        assert f"`{status}`" in text, f"the guide does not tell a labeller about {status}"


def test_the_archetype_cannot_suppress_a_term_the_document_names():
    """The first held-out document produced this, and it is the worst kind.

    Air T is a revolver, a term loan and an accordion under one bilateral
    facility. Its borrowing base led the classifier to an asset-based
    revolver, which rules the term-loan fields inapplicable; the extractor
    had missed the Consolidated Term Loan's maturity because the registry
    anchors on a phrase this agreement does not use; and suppression only
    fires on fields the extractor left empty. Two failures that are each
    visible on their own -- a field in review, an archetype the register
    already calls unreliable -- composed into ``not_applicable_to_archetype``,
    which reads as a settled answer, on a term loan maturing 27 August 2031.
    """
    from credit_extract.models.archetypes import (
        PROFILES, inapplicable_fields, suppression_vetoes,
    )

    abl = PROFILES["abl_revolver"]
    assert "term_amortization" in abl.inapplicable_groups

    silent = inapplicable_fields(abl, ["initial_term_loan.maturity_date"])
    assert silent, "without the document, the archetype still rules"

    text = "the Consolidated Term Loan shall mature on August 27, 2031"
    assert not inapplicable_fields(abl, ["initial_term_loan.maturity_date"], text)
    assert suppression_vetoes(abl, text) == {"term_amortization": "term loan"}

    # The veto is not a licence to un-suppress everything: a document that
    # never mentions the thing keeps the archetype's judgement.
    quiet = "the Borrowers shall deliver a Borrowing Base Certificate monthly"
    assert inapplicable_fields(abl, ["initial_term_loan.maturity_date"], quiet)


def test_collateral_vocabulary_does_not_veto_suppression():
    """The veto's other failure mode, found on Golub's BDC warehouse.

    A fund-level facility describes the loans it may *buy* in the vocabulary
    of the loans a company *owes*, and the veto read that as evidence this
    deal had those terms. "Delayed draw" appears nineteen times in
    cik1901612 and every one is inside "Delayed Drawdown Collateral Loan", a
    category of asset; the facility has no delayed draw of its own. All four
    of the archetype's inapplicable groups were vetoed this way, so nothing
    was suppressed and three correct answers sat in review.

    This is the same underlying mistake as the Air T case above, pointing the
    other way: the question is never whether the words are present, it is
    whose balance sheet they are on.
    """
    from credit_extract.models.archetypes import (
        PROFILES, inapplicable_fields, suppression_vetoes,
    )

    abl = PROFILES["abl_revolver"]
    portfolio = (
        'the Aggregate Adjusted Collateral Balance of Eligible Collateral '
        'Loans that are Revolving Collateral Loans or Delayed Drawdown '
        'Collateral Loans may not exceed 5% of the Maximum Portfolio Amount'
    )
    assert inapplicable_fields(
        abl, ["initial_term_loan.commitment"], portfolio
    ), "collateral vocabulary is not evidence about this facility"
    assert suppression_vetoes(abl, portfolio) == {}

    # One use away from the collateral description is enough to veto. The
    # asymmetry is the point: a missed veto ends in a confident wrong answer,
    # a spurious one only ends in review.
    mixed = portfolio + "  The Borrower shall repay the Term Loan in full."
    assert not inapplicable_fields(abl, ["initial_term_loan.commitment"], mixed)
    assert suppression_vetoes(abl, mixed) == {"term_amortization": "term loan"}


def test_the_ebitda_veto_sees_an_adjusted_ebitda_definition():
    """Health Catalyst defines EBITDA and never writes "Consolidated EBITDA".

    The evidence phrases were ["consolidated ebitda", "combined ebitda",
    "leverage ratio"], and this agreement's term is "Consolidated *Adjusted*
    EBITDA" -- 53 occurrences that none of the first two match. Only
    "leverage ratio" caught it, which is a veto firing for a reason unrelated
    to the thing it is protecting.

    It matters because two labels asserted not_applicable_to_archetype here
    on the strength of that same string search, and the document carries a
    nineteen-clause add-back ladder capped at 25%.
    """
    from credit_extract.models.archetypes import FIELD_GROUPS, PROFILES

    arr = PROFILES["recurring_revenue"]
    assert "ebitda_covenants" in arr.inapplicable_groups

    hc = (
        '" Consolidated Adjusted EBITDA ": with respect to the Borrower and '
        'its consolidated Subsidiaries for any period, the Consolidated Net '
        'Income of the Borrower and its Subsidiaries for such period'
    )
    assert FIELD_GROUPS["ebitda_covenants"].evidenced_in(hc) == "ebitda"

    # An ARR loan that genuinely has no EBITDA still gets the suppression the
    # profile is for -- widening the phrase must not empty the archetype out.
    from credit_extract.models.archetypes import inapplicable_fields

    pure = (
        "the Borrower shall maintain Annualized Recurring Revenue of not less "
        "than $40,000,000 as of the last day of each fiscal quarter"
    )
    assert inapplicable_fields(arr, ["consolidated_ebitda.addback_cap_pct"], pure)


def test_a_fee_letter_is_only_external_where_the_fees_actually_live_in_it():
    """Validator E could not fire on the case it exists for.

    It reads the sentence a figure sits in, so it only ran on fields that
    already carried a span -- and a fee fixed by a fee letter has no figure
    and so no sentence. The rule that replaces that gap has to be narrow,
    because ``external_reference`` is a status the pipeline presents as
    settled and a wrong one is a silent error: naming a fee letter in a list
    of Loan Documents is not evidence that this deal's fees live there.
    """
    from credit_extract.validate.validators import fee_letter_governs_fees

    class _Doc:
        def __init__(self, text: str) -> None:
            self.text = text

    governs = _Doc(
        '"Fee Letter" means that certain Fourth Amended and Restated Fee '
        "Letter dated as of April 17, 2018. The Lenders and the "
        "Administrative Agent shall have received all fees due and payable "
        "under the Fee Letter."
    )
    assert fee_letter_governs_fees(governs)

    # Defined, but nothing says the fees are payable under it.
    mentioned_only = _Doc(
        '"Loan Documents" means this Agreement, the Notes and the Fee Letter. '
        '"Fee Letter" means the fee letter dated as of the date hereof.'
    )
    assert fee_letter_governs_fees(mentioned_only) is None

    # Fees payable under one, but no definition -- a passing reference.
    undefined = _Doc("all fees payable under the Fee Letter shall be retained")
    assert fee_letter_governs_fees(undefined) is None


def test_a_fraction_of_one_percent_is_not_a_numeral_mismatch():
    """"one-quarter of one percent (0.25%)" says the same thing twice."""
    from credit_extract.validate.invariants import (
        _FRACTION_OF_PERCENT_RE, _NUMERAL_WORD_RE,
    )

    text = "a rate of one-quarter of one percent (0.25%) per annum"
    match = _NUMERAL_WORD_RE.search(text)
    assert match, "the bare pattern still matches -- that is why the guard exists"
    assert match.group("words").lower() == "one"
    assert _FRACTION_OF_PERCENT_RE.search(text[:match.end()])

    plain = "twenty-five percent (35%) of Consolidated EBITDA"
    assert _NUMERAL_WORD_RE.search(plain)
    assert not _FRACTION_OF_PERCENT_RE.search(plain), "a real mismatch still fires"


def test_the_split_file_is_yaml_a_human_can_read():
    raw = yaml.safe_load(split_mod.SPLIT_FILE.read_text())
    assert raw["version"] == 1
    assert raw["holdout_in"] == split_mod.HOLDOUT_IN
    assert set(raw["assignment"].values()) == {"fit", "holdout"}


def test_every_label_names_a_document_the_split_knows():
    """A label whose corpus_name is wrong scores nothing, silently.

    Five label files were written with names copied from console output that
    truncates at the terminal width, so they ended mid-token --
    "..._exhibi", "...ablamendmentn", "...vvvcredit". Each one resolved to no
    document, so every assertion in it was skipped. The per-family table still
    grew, because other labels in the same batch landed, and the report has no
    way to say "four documents were asked for and two were found".

    That is the worst failure mode this repository has: not a wrong number, an
    absent one that looks like a smaller corpus. The frozen split is the
    authoritative list of harvested documents and is checked in, so this can
    be enforced without unzipping 143MB.
    """
    import yaml

    from credit_extract.eval.split import load_split

    split = load_split()
    known = set(split.assignment) | set(split.contaminated)
    assert known, "the frozen split should list the harvested documents"

    labels = Path(__file__).resolve().parents[1] / "credit_extract" / "eval" / "labels"
    unknown: list[tuple[str, str]] = []
    for path in sorted(labels.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        name = raw.get("corpus_name")
        if name and name not in known:
            unknown.append((path.name, name))

    assert not unknown, (
        "these labels name a document the frozen split does not know, so their "
        f"assertions never run: {unknown}"
    )


def test_a_document_that_is_not_an_agreement_gets_no_archetype():
    """Greenfire Resources' Business Acquisition Report is in the harvest.

    It is a Canadian securities disclosure with financial statements attached:
    no defined terms, no borrower, no lender, no operative clause. It
    describes a $50 million revolver in note 12.1, which is enough vocabulary
    for the classifier to return second_lien at 0.70 -- above the threshold at
    which the profile begins marking fields not_applicable_to_archetype, a
    confident status.

    So the composition the blind-spot register warns about was reachable in
    its purest form: a confident classification of a document with nothing to
    classify, suppressing fields that are absent because the agreement is
    somewhere else. The corpus caught it as a silent error and this is the
    guard.
    """
    from credit_extract.models.archetypes import detect_deterministic

    report = (
        "FORM 51-102F4 BUSINESS ACQUISITION REPORT. Item 2 Details of "
        "Acquisition. 12. DEBT 12.1 Revolving Credit Facility. In Q4 2025 the "
        "Company closed a $50 million revolving reserved-based credit facility "
        "(the 'Credit Facility'). The Credit Facility is subject to "
        "semi-annual borrowing base reviews. The borrowing base determination "
        "reflects the lender's evaluation of the Company's petroleum and "
        "natural gas reserves."
    )
    assert detect_deterministic(report).archetype == "unknown"

    # The same vocabulary inside something that is an agreement still classifies.
    agreement = report + (
        " Section 8. Events of Default. Any Event of Default shall entitle the "
        "Administrative Agent to act. Upon an Event of Default the Lenders may "
        "accelerate."
    )
    assert detect_deterministic(agreement).archetype != "unknown"
