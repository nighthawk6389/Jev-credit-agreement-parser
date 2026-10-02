"""Who guarantees the obligations, and where the text says so.

Out of sample, live Jev made 188 confident claims no label covered, and the
seven that were wrong were one field: guarantors reported absent. Six BDC
agreements define a Subsidiary Guarantor as "any Subsidiary that is a
Guarantor under the Guarantee and Security Agreement" and name none, and one
is signed "ENERGY HARDWARE HOLDINGS, INC., as Subsidiary Guarantor". Nothing
read guarantors, so validator C was asked whether the field was absent, and a
literal reader says no to a definition that names nobody and to a signature
block, which is not a provision.

Two things answer it, each pinned here: the rules tier reads a guarantor
named in its role, and validator C declines to call guarantors absent where
the agreement names, defines or grants them. What it does not do is send
guarantors a definition hands to a guarantee agreement to
``external_reference``: that was tried, and every in-sample agreement it fired
on named its guarantors somewhere after all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from credit_extract.eval.assertions import Assertion, evaluate_assertion
from credit_extract.ingest.segment import Chunk
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import FIELD_REGISTRY
from credit_extract.pipeline import run_pipeline
from credit_extract.validate import validators as V
from credit_extract.validate.calibrate import load_thresholds
from credit_extract.validate.jev import JevSession, OfflineJev

FIELD = "guarantor.legal_name"

PREAMBLE = (
    "CREDIT AGREEMENT dated as of March 1, 2026, among ACME HOLDINGS CORP., "
    "as the Borrower, the Lenders party hereto, and FIRST NATIONAL BANK, N.A., "
    "as Administrative Agent.\n\n"
    "ARTICLE I DEFINITIONS\n\n"
    'Section 1.01 Defined Terms. "Agreement" means this Credit Agreement. '
    '"Obligations" means all loans, advances and other obligations of the '
    "Borrower hereunder. "
)
BODY = (
    "\n\nARTICLE II THE CREDITS\n\n"
    "Section 2.01 Commitments. Each Lender agrees to make Loans to the "
    "Borrower in an aggregate principal amount not to exceed its Commitment. "
    "Section 2.02 Interest. The Loans shall bear interest at Term SOFR plus "
    "2.00% per annum.\n\n"
    "ARTICLE VII EVENTS OF DEFAULT\n\n"
    "Section 7.01 Events of Default. If any Event of Default occurs, the "
    "Administrative Agent may declare the Loans due. An Event of Default "
    "includes failure to pay any Obligation when due.\n\n"
    "Section 9.09 Governing Law. This Agreement shall be governed by the laws "
    "of the State of New York.\n\n"
)


def _run(tmp_path: Path, text: str):
    source = tmp_path / "agreement.txt"
    source.write_text(text)
    return run_pipeline(source, jev_backend=OfflineJev())


class _Doc:
    def __init__(self, text: str) -> None:
        self.text = text


# ---------------------------------------------------------------------------
# The rules tier reads a guarantor named in its role
# ---------------------------------------------------------------------------


def test_a_guarantor_signing_in_its_role_is_read(tmp_path):
    text = PREAMBLE + BODY + (
        "IN WITNESS WHEREOF, the parties have executed this Agreement.\n"
        "ACME HOLDINGS CORP., as the Borrower By: /s/ J. Smith Name: J. Smith "
        "Title: Chief Financial Officer\n"
        "ACME OPERATING SUBSIDIARY, LLC, as a Guarantor By: /s/ J. Smith "
        "Name: J. Smith Title: Chief Financial Officer\n"
        "FIRST NATIONAL BANK, N.A., as Administrative Agent By: /s/ K. Lee\n"
    )
    field = _run(tmp_path, text).fields[FIELD]

    assert field.value == "ACME OPERATING SUBSIDIARY, LLC"
    assert field.status != "absent_from_document"


@pytest.mark.parametrize("placeholder", [
    "THE GUARANTORS PARTY HERETO, as Guarantors",
    "CERTAIN SUBSIDIARIES OF ACME HOLDINGS CORP. IDENTIFIED HEREIN, as the Guarantors",
    "THE GUARANTORS NAMED HEREIN, as Guarantors",
])
def test_a_placeholder_where_names_go_is_not_a_name(placeholder):
    """The slot is right and what fills it is not a party."""
    import re

    from credit_extract.extract.passes import GUARANTOR_NAMED, GUARANTOR_PLACEHOLDER

    text = f"among ACME HOLDINGS CORP., as the Borrower, {placeholder}, and"
    named = [
        m.group(1) for m in re.finditer(GUARANTOR_NAMED, text)
        if not re.search(GUARANTOR_PLACEHOLDER, m.group(1))
    ]
    assert named == []


def test_someone_elses_guarantors_described_in_prose_are_not_read():
    """Camping World defines its floor plan facility by listing that
    facility's parties, "subsidiaries of Freedomroads, LLC, as guarantors".
    Lower case describes; it does not designate a party to this agreement."""
    import re

    from credit_extract.extract.passes import GUARANTOR_NAMED

    text = (
        '"Floor Plan Credit Agreement" means the credit agreement among '
        "Freedomroads, LLC, certain other direct and indirect subsidiaries of "
        "Freedomroads, LLC, as guarantors, and Bank of America, N.A."
    )
    assert re.search(GUARANTOR_NAMED, text) is None


def test_a_name_does_not_begin_with_the_previous_names_suffix():
    """Several guarantors over one role run together in a signature block."""
    import re

    from credit_extract.extract.passes import GUARANTOR_NAMED

    text = (
        "Title: Chief Financial Officer ACME BLOCKER, INC. ACME HOLDINGS "
        "BLOCKER, LLC INSTOR BLOCKER, INC., as Parent Guarantors By:"
    )
    assert [m.group(1) for m in re.finditer(GUARANTOR_NAMED, text)] == [
        "INSTOR BLOCKER, INC."
    ]


# ---------------------------------------------------------------------------
# Validator E marks what the text establishes, and settles nothing
# ---------------------------------------------------------------------------

DEFINED_ELSEWHERE = (
    '"Guarantee and Security Agreement" means that certain Guarantee, Pledge '
    "and Security Agreement among the Borrower, the Subsidiary Guarantors and "
    'the Collateral Agent. "Subsidiary Guarantor" means any Subsidiary that is '
    "a Guarantor under the Guarantee and Security Agreement. "
)


def test_guarantors_a_definition_hands_elsewhere_are_neither_absent_nor_external(tmp_path):
    """The BDC case. The names may be in a guarantee agreement, or on a
    signature page, a schedule or in a borrower's own guaranty article that
    no pattern sees; the text cannot say which, so the field goes to review."""
    field = _run(tmp_path, PREAMBLE + DEFINED_ELSEWHERE + BODY).fields[FIELD]

    assert field.status == "needs_review"
    assert field.value is None and not field.external_document
    assert "not absence" in field.notes


@pytest.mark.parametrize("text", [
    DEFINED_ELSEWHERE,
    "ACME OPERATING SUBSIDIARY, LLC, as Subsidiary Guarantor By: /s/",
    "GUARANTORS: ACME OPERATING SUBSIDIARY, LLC By: /s/ J. Smith",
    'Ares Capital CP Funding II, as the guarantor (the " Guarantor "), and',
    "ARTICLE X. CONTINUING GUARANTY 10.01 Guaranty . The Company hereby "
    "absolutely and unconditionally guarantees",
])
def test_the_text_establishes_guarantors_in_each_of_its_ways(text):
    assert V.guarantors_established(_Doc(text))


def test_a_portfolio_loans_guarantor_is_not_this_facilitys():
    """A fund facility describes the loans it holds as collateral, and their
    obligors' guarantors, in prose. That establishes nothing about who
    guarantees this borrower."""
    assert V.guarantors_established(_Doc(
        '"Eligible Collateral Obligation" means a loan as to which the Obligor '
        "or any guarantor thereof is not subject to an Insolvency Event. "
        '"Obligor" means the borrower under a Collateral Obligation.'
    )) is None


def test_a_placeholder_alone_establishes_nothing_by_naming():
    """THE GUARANTORS PARTY HERETO is not a name, and on its own it is not
    evidence either: what it stands for has to be defined or signed."""
    assert V.guarantors_established(_Doc(
        "among ACME HOLDINGS CORP., as the Borrower, THE GUARANTORS PARTY "
        "HERETO, as Guarantors, and FIRST NATIONAL BANK, N.A."
    )) is None


def test_a_name_cut_where_a_chunk_began_is_dropped():
    """Several guarantors are several candidates, and one that ends another is
    the other truncated at a chunk boundary, not a party of its own."""
    from credit_extract.extract.passes import Candidate
    from credit_extract.extract.reconcile import reconcile

    def candidate(value, start, segmentation):
        return Candidate(
            field=FIELD, value=value, confidence=0.85, pass_id=segmentation,
            segmentation=segmentation,
            span=Span(start=start, end=start + len(value), text=value),
        )

    fields = reconcile([
        candidate("THE MATTRESS VENTURE, LLC", 100, "structural"),
        candidate("THE MATTRESS VENTURE, LLC", 100, "sliding"),
        candidate("URE, LLC", 117, "sliding"),
    ], {FIELD: FIELD_REGISTRY[FIELD]}, total_passes=2).fields

    assert fields[FIELD].value == "THE MATTRESS VENTURE, LLC"
    assert fields[FIELD].status != "conflicted"


# ---------------------------------------------------------------------------
# Validator C: guarantors the agreement has are never "absent"
# ---------------------------------------------------------------------------


def _c(field: ExtractedField, text: str) -> ExtractedField:
    chunk = Chunk(
        chunk_id="c0", segmentation="structural", label="c0",
        spans=[Span(start=0, end=len(text), text=text)], text=text,
    )
    ctx = V.ValidationContext(
        doc=_Doc(text), chunks=[chunk], fields={FIELD: field},
        specs={FIELD: FIELD_REGISTRY[FIELD]},
        session=JevSession(OfflineJev()), thresholds=load_thresholds(),
    )
    V.validator_c_negative_space(ctx)
    return ctx.fields[FIELD]


CHUNK = (
    "The Borrower shall deliver its audited annual financial statements "
    "within 120 days after the end of each fiscal year."
)


def test_c_declines_absence_where_the_agreement_has_guarantors():
    field = ExtractedField.single(value=None, status="needs_review")
    field.qualifiers["guarantors_established"] = (
        '"Subsidiary Guarantor" means any Subsidiary that is a Guarantor'
    )

    field = _c(field, CHUNK)

    assert field.status == "needs_review"
    assert "not absence" in field.notes


def test_c_still_confirms_absence_where_nothing_establishes_a_guarantor():
    """The counterweight: an agreement with no guarantor is still allowed to
    say so, or the fix buys safety by never answering."""
    field = _c(ExtractedField.single(value=None, status="needs_review"), CHUNK)

    assert field.status == "absent_from_document"


# ---------------------------------------------------------------------------
# A label for a role several parties fill
# ---------------------------------------------------------------------------


def test_a_label_listing_several_guarantors_accepts_any_of_them():
    class _Result:
        document_id = "d"

        def __init__(self, value, status):
            spans = [Span(start=0, end=4, text="ACME")] if value else []
            self.fields = {FIELD: ExtractedField.single(
                value=value, status=status, spans=spans,
            )}

    label = Assertion(
        id="g", family="F01_integrity", member="undefined_term_used",
        kind="field_value", target=FIELD, note="'x'",
        expect=["ACME OPERATING SUBSIDIARY, LLC", "ACME FINANCE CORP."],
    )

    assert evaluate_assertion(label, _Result("ACME FINANCE CORP.", "confirmed")).passed
    wrong = evaluate_assertion(label, _Result("ACME HOLDINGS CORP.", "confirmed"))
    assert not wrong.passed and wrong.silent_error
    empty = evaluate_assertion(label, _Result(None, "absent_from_document"))
    assert not empty.passed
