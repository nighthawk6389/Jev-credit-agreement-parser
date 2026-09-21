"""Provenance rules, the offset space, and standards binding."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from credit_extract.ingest.normalize import ingest, normalize_chars
from credit_extract.ingest.tables import (
    parse_date, parse_money, parse_percent, parse_ratio, quarter_index,
)
from credit_extract.models.core import ExtractedField, Span
from credit_extract.models.fpml_model import AmortizationSchedule, ScheduleRow


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_a_value_without_a_span_fails_the_record():
    with pytest.raises(ValueError, match="must cite at least one span"):
        ExtractedField[Decimal].single(value=Decimal("150500000"), status="confirmed")


def test_absent_from_document_cannot_carry_a_value():
    span = Span(start=0, end=10, text="0123456789")
    with pytest.raises(ValueError, match="cannot carry a value"):
        ExtractedField[Decimal].single(
            value=Decimal(1), spans=[span], status="absent_from_document"
        )


def test_external_reference_must_name_the_document():
    with pytest.raises(ValueError, match="must name the document"):
        ExtractedField[str].single(status="external_reference")


def test_null_alone_is_not_a_resolved_state():
    field = ExtractedField[Decimal].single(value=None, status="needs_review")
    assert field.is_resolved is True          # needs_review is a real answer
    field.status = "confirmed"
    assert field.is_resolved is False, (
        "a confirmed field with no value is not a coherent output"
    )


def test_span_requires_a_positive_extent():
    with pytest.raises(ValueError, match="must exceed start"):
        Span(start=10, end=10, text="")


# ---------------------------------------------------------------------------
# Derived vs documented bullet
# ---------------------------------------------------------------------------


def test_deduplicating_a_schedule_does_not_carry_a_stale_bullet():
    """Regression: a derived bullet stored as if documented hid Trap 1.

    With the bullet computed once from the *printed* rows and then carried
    through de-duplication, principal reconciled against itself and the
    $1,505,000 overstatement vanished.
    """
    rows = [
        ScheduleRow(payment_date=date(2018, 3, 31), amount=Decimal(100), row_index=0),
        ScheduleRow(payment_date=date(2018, 3, 31), amount=Decimal(100), row_index=1),
        ScheduleRow(payment_date=date(2018, 6, 30), amount=Decimal(100), row_index=2),
    ]
    schedule = AmortizationSchedule(rows=rows, original_principal=Decimal(1000))
    assert schedule.total_scheduled == Decimal(300)
    assert schedule.derived_bullet == Decimal(700)

    corrected = schedule.deduplicated()
    assert corrected.total_scheduled == Decimal(200)
    assert corrected.derived_bullet == Decimal(800), (
        "the derived bullet must move with the rows"
    )
    assert corrected.stated_bullet_at_maturity is None


# ---------------------------------------------------------------------------
# Normalization and the offset space
# ---------------------------------------------------------------------------


def test_character_normalization_folds_edgar_noise():
    raw = "“Smart” – dash nbsp so­ft ﬁnal"
    out = normalize_chars(raw)
    assert '"Smart"' in out
    assert "- dash nbsp soft final" in out


def test_every_span_reads_back_from_its_own_offsets(doc):
    """The offset space is the contract; a span that lies about it is useless."""
    for table in doc.tables:
        for cell in table.cells:
            assert doc.text[cell.start:cell.end] == cell.text
    for section in doc.sections[:10]:
        span = doc.section_span(section.section_id)
        assert span is not None
        assert doc.text[span.start:span.end] == span.text


def test_table_of_contents_entries_are_not_mistaken_for_sections(doc):
    ids = [s.section_id for s in doc.sections]
    assert len(ids) == len(set(ids)), f"duplicate section markers: {ids}"
    section = doc.section_span("2.10")
    assert section is not None
    assert "Repayment of Loans" in section.text
    assert "Principal Amortization Payment" in section.text, (
        "the marker must point at the body, not the contents listing"
    )


def test_soft_wraps_inside_a_paragraph_do_not_fragment_sentences(tmp_path):
    """Regression: wrapped source HTML broke every anchored pattern."""
    path = tmp_path / "wrapped.htm"
    path.write_text(
        "<html><body><p>an aggregate principal amount not to exceed the\n"
        "greater of $15,000,000 and 35% of Consolidated EBITDA for the\n"
        "most recently ended Test Period.</p></body></html>"
    )
    text = ingest(path).text
    assert "the greater of $15,000,000" in text
    assert "\n" not in text.strip(), "a paragraph is one line after normalization"


def test_plain_text_and_html_agree_on_content(tmp_path):
    html = tmp_path / "a.htm"
    html.write_text("<html><body><p>SECTION 2.10 Repayment.</p>"
                    "<p>Due August 1, 2024.</p></body></html>")
    txt = tmp_path / "a.txt"
    txt.write_text("SECTION 2.10 Repayment.\n\nDue August 1, 2024.\n")
    assert ingest(html).text.split() == ingest(txt).text.split()


def test_unsupported_format_is_refused(tmp_path):
    path = tmp_path / "agreement.docx"
    path.write_text("x")
    with pytest.raises(ValueError, match="unsupported format"):
        ingest(path)


def test_offset_resolves_to_page_and_section(doc):
    mentions = doc.find_all("Repayment of Loans")
    assert len(mentions) >= 2, "expected a contents entry and a body heading"

    # The contents listing precedes every section marker, so it resolves to no
    # section at all -- which is right. Attributing it to 2.10 would make a
    # reviewer chase a citation into the table of contents.
    toc_page, toc_section = doc.locate(mentions[0].start)
    assert toc_section is None
    assert toc_page is not None

    body_page, body_section = doc.locate(mentions[-1].start)
    assert body_section == "2.10"
    assert body_page is not None and body_page >= toc_page


# ---------------------------------------------------------------------------
# Deterministic parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1,505,000", Decimal("1505000")),
        ("$10.5 million", Decimal("10500000")),
        ("(1,200)", Decimal("-1200")),
        ("no number here", None),
    ],
)
def test_parse_money(text, expected):
    assert parse_money(text) == expected


def test_basis_points_and_percentages_are_the_same_scale():
    assert parse_percent("0.50%") == Decimal("0.50")
    assert parse_percent("50 bps") == Decimal("0.50")


def test_parse_ratio_and_date():
    assert parse_ratio("4.50:1.00") == Decimal("4.5")
    assert parse_date("August 1, 2017") == date(2017, 8, 1)
    assert parse_date("12/31/2023") == date(2023, 12, 31)


def test_quarter_index_is_month_based_not_day_based():
    """Quarters differ in length; spacing checks must not use day counts."""
    q4_2017 = quarter_index(date(2017, 12, 31))
    q1_2018 = quarter_index(date(2018, 3, 31))
    q2_2018 = quarter_index(date(2018, 6, 30))
    assert q1_2018 - q4_2017 == 1
    assert q2_2018 - q1_2018 == 1
    assert (date(2018, 3, 31) - date(2017, 12, 31)).days != (
        date(2018, 6, 30) - date(2018, 3, 31)
    ).days


# ---------------------------------------------------------------------------
# Standards binding
# ---------------------------------------------------------------------------


def test_fibo_terms_resolve_against_the_vendored_ontology():
    from credit_extract.models.fibo_map import fibo, party_roles

    uri = fibo("fibo-fbc-dae-dbt:Borrower")
    assert uri.startswith("https://spec.edmcouncil.org/fibo/ontology/")
    assert party_roles()["borrower"].verified is True


def test_an_invented_fibo_term_is_refused():
    from credit_extract.models.fibo_map import fibo

    with pytest.raises(KeyError, match="not in the vendored FIBO snapshot"):
        fibo("fibo-loan-loan-loan:Loan")


def test_unmappable_roles_are_documented_gaps_not_wrong_bindings():
    from credit_extract.models.fibo_map import gaps, party_roles

    agent = party_roles()["administrative_agent"]
    assert agent.term is None
    assert agent.gap_reason
    assert "administrative_agent" in gaps()


def test_actus_terms_resolve_and_the_revolver_is_left_unmapped():
    from credit_extract.models.actus_map import acronym, map_facility

    assert acronym("notionalPrincipal") == "NT"
    assert acronym("arrayNextPrincipalRedemptionPayment") == "ARPRNXTj"

    revolver = map_facility("revolver")
    assert revolver.contract_type is None
    assert revolver.executable is False
    assert "CLM" in revolver.rationale, "the gap must say what was rejected"


def test_fpml_terms_resolve_against_the_vendored_schemas():
    from credit_extract.models.fpml_model import fpml, provenance

    term = fpml("accruingPikOption")
    assert term.verified is True
    assert term.term == "fpml:accruingPikOption"
    assert provenance()["verified"] is True
    assert provenance()["elements_available"] > 800


def test_an_invented_fpml_element_is_refused():
    from credit_extract.models.fpml_model import fpml

    with pytest.raises(KeyError, match="not in the vendored FpML snapshot"):
        fpml("paymentInKindToggleThatSoundsPlausible")


def test_every_fpml_registry_term_resolves_to_a_schema_declaration():
    from credit_extract.models.fpml_model import FIELD_REGISTRY, fpml

    terms = {
        spec.standard_term.split(":", 1)[1]
        for spec in FIELD_REGISTRY.values()
        if spec.standard_term and spec.standard_term.startswith("fpml:")
    }
    assert terms
    assert all(fpml(term).verified for term in terms)


#: FpML splits its declarations across schemas, and this project binds a loan
#: concept to an element the loan schema does not declare four times. Each one
#: reads correctly against the element's own documentation -- a party's industry
#: sector, an INVG/NIVG credit quality, a credit rating, an ISDA floating rate
#: option -- but "the name exists somewhere in three schemas" is a weak thing to
#: rest a mapping on, and it is the only thing fpml() checks. Pinning the
#: exceptions means the next loan field that resolves against an unrelated
#: asset-class element has to be argued for here rather than passing quietly.
CROSS_SCHEMA_BINDINGS = {
    "classification": {"fpml-shared-5-13.xsd"},
    "creditQuality": {"fpml-asset-5-13.xsd"},
    "creditRating": {"fpml-shared-5-13.xsd", "fpml-asset-5-13.xsd"},
    "floatingRateIndex": {"fpml-shared-5-13.xsd", "fpml-asset-5-13.xsd"},
}


def test_a_loan_binding_outside_the_loan_schema_is_named_not_assumed():
    from credit_extract.models.fpml_model import declaration, mapped_terms

    outside = {
        term: set(declaration(term)["schemas"])
        for term in mapped_terms()
        if "fpml-loan-5-13.xsd" not in declaration(term)["schemas"]
    }
    assert outside == CROSS_SCHEMA_BINDINGS, (
        "a mapped term stopped resolving in the loan schema, or a new one "
        "never did; say which schema declares it and why that is the right "
        "element before adding it above"
    )


def test_the_snapshot_records_where_each_element_came_from():
    from credit_extract.models.fpml_model import declaration

    spread_adjustment = declaration("spreadAdjustment")
    assert "fpml-loan-5-13.xsd" in spread_adjustment["schemas"]
    assert any("credit spread" in d for d in spread_adjustment["descriptions"])

    # The index merges same-named declarations from different scopes, so this
    # entry is two elements wearing one name. provenance() says so rather than
    # letting the count read as 851 distinct concepts.
    delayed_draw = declaration("delayedDraw")
    assert len(delayed_draw["types"]) > 1, "merged scopes are the documented caveat"


def test_every_fibo_registry_term_resolves_to_the_vendored_snapshot():
    from credit_extract.models.fibo_map import binding
    from credit_extract.models.fpml_model import FIELD_REGISTRY

    terms = {
        spec.standard_term
        for spec in FIELD_REGISTRY.values()
        if spec.standard_term and spec.standard_term.startswith("fibo")
    }
    assert terms
    # rating.agency resolves only because vendor_standards.py vendors
    # FND/Arrangements/Ratings.rdf. Trimming that list would otherwise leave a
    # registry entry pointing at a CURIE no snapshot contains.
    assert "fibo-fnd-arr-rt:RatingAgency" in terms
    assert all(binding(term).verified for term in terms)


def test_fpml_and_fibo_are_complementary_not_competing_mappings():
    from credit_extract.models.fpml_model import standards_bindings

    mappings = standards_bindings()
    assert {binding.standard for binding in mappings["credit_rating"]} == {"fpml", "fibo"}
    assert {binding.standard for binding in mappings["lien"]} == {"fpml", "fibo"}
    assert mappings["pik"][0].verified is True
    assert mappings["pik"][1].term is None, "a documented FIBO gap beats an invented term"


def test_facility_retains_pik_rank_rating_and_draw_optionality():
    from credit_extract.models.fpml_model import (
        CommitmentTerms, CreditRating, Facility, FacilityClassification, PikTerms,
    )

    facility = Facility(
        facility_id="TL-B",
        facility_type="delayed_draw_term_loan",
        pik=PikTerms(rate_pct=Decimal("2.00"), spread_pct=Decimal("1.00")),
        commitment=CommitmentTerms(
            current_amount=Decimal("100000000"),
            must_draw_by_date=date(2028, 6, 30),
            refusal_allowed=False,
        ),
        classification=FacilityClassification(
            lien="first lien", seniority="senior secured", multi_currency=True,
        ),
        ratings=[CreditRating(agency="S&P", rating="BB-", credit_quality="NIVG")],
    )
    assert facility.pik and facility.pik.rate_pct == Decimal("2.00")
    assert facility.classification.seniority == "senior secured"
    assert facility.commitment.must_draw_by_date == date(2028, 6, 30)
    assert facility.ratings[0].rating == "BB-"


# ---------------------------------------------------------------------------
# Two units of measure that a money-typed field cannot tell apart, and a
# heading style that made a whole agreement look structureless. Both were
# found by reading one held-out document.
# ---------------------------------------------------------------------------


def test_a_percentage_is_not_an_amount():
    """Amortisation is routinely drafted as a percentage of the initial
    principal, per instalment. The regex read the first number and ignored the
    unit, so "1.250% of the initial principal amount" came back as a quarterly
    payment of one dollar twenty-five where the real one is $7,875,000 -- with
    a citation behind it, which is the shape of a silent error rather than a
    miss."""
    assert parse_money("1.250% of the initial principal amount") is None
    assert parse_money("0.0%") is None
    assert parse_money("50%") is None


def test_the_money_parser_still_reads_money():
    assert parse_money("$630,000,000") == Decimal("630000000")
    assert parse_money("$10.5 million") == Decimal("10500000")
    assert parse_money("($2,000)") == Decimal("-2000")
    assert parse_money("$5.0 million or 2.5%") == Decimal("5000000"), (
        "a dollar figure keeps winning when both appear; only a number that is "
        "itself a percentage is rejected"
    )


def test_headings_are_found_when_the_filing_has_no_line_breaks():
    """Every heading pattern anchored to the start of a line, and some filers'
    HTML puts the whole agreement in one flow. On one real filing that was 1
    section detected out of 198 present, which made every cross-reference in
    the document unresolvable and collapsed the structural segmentation to a
    single chunk."""
    from credit_extract.ingest.normalize import detect_sections

    inline = (
        "the parties hereto agree as follows: ARTICLE I Definitions "
        "SECTION 1.01. Defined Terms. As used in this Agreement, the following "
        "terms have the meanings specified below. ARTICLE II The Credits "
        "SECTION 2.01. Commitments. Subject to the terms and conditions set "
        "forth herein, the Lender agrees to make a Loan. SECTION 2.02. Loans "
        "and Borrowings. Each Loan shall be made as part of a Borrowing."
    )
    found = {marker.section_id for marker in detect_sections(inline)}
    assert {"ARTICLE I", "ARTICLE II", "1.01", "2.01", "2.02"} <= found


def test_a_reference_in_prose_is_not_mistaken_for_a_heading():
    """The house style that needs the inline rule distinguishes the two by
    case: headings read "SECTION 2.07." and references read "Section 2.07"."""
    from credit_extract.ingest.normalize import detect_sections

    prose = (
        "Subject to Section 2.07 and Section 9.02, the Borrower shall pay the "
        "amounts described in Section 2.13 on each date specified in Section "
        "2.16 of this Agreement, as further provided in Section 6.01."
    )
    assert detect_sections(prose) == []


def test_headings_are_found_when_the_word_section_is_left_out():
    """StepStone's SPV warehouse numbers its subsections bare: "2.1. Loans and
    Commitments." The divisions above them do say SECTION, so the document
    looked structured -- 16 markers found -- while all 109 provisions the
    cross-references actually cite went undetected, for 68 unresolvable
    references in an agreement whose references are almost all sound."""
    from credit_extract.ingest.normalize import detect_sections

    bare = (
        "SECTION 1. DEFINITIONS AND INTERPRETATION 1.1. Definitions. As used "
        "herein, the following terms have the meanings set forth below. "
        "1.2. Accounting Terms. Except as otherwise expressly provided "
        "herein, all accounting terms shall be construed in conformity with "
        "GAAP. 1.4. Assumptions as to Collateral Obligations, Etc. In "
        "connection with all calculations required hereunder, the following "
        "shall apply. SECTION 2. LOANS AND COMMITMENTS 2.1. Loans and "
        "Commitments. During the Availability Period, each Lender agrees to "
        "make Loans to the Borrower."
    )
    found = {marker.section_id for marker in detect_sections(bare)}
    assert {"1.1", "1.2", "1.4", "2.1"} <= found


def test_a_bare_numbered_reference_in_prose_is_not_mistaken_for_a_heading():
    """The bare rule has no keyword to anchor on, so the title pattern and the
    lookbehinds are all that separate a heading from the cross-references --
    which are the one thing in the document guaranteed to carry the same
    numbers."""
    from credit_extract.ingest.normalize import detect_sections

    prose = (
        "Subject to Section 2.7 and Section 9.2, the Borrower shall repay the "
        "Loans in the amount of $12,500,000 as provided in Section 2.13, and "
        "the Advance Rate shall be redetermined in accordance with Section "
        "6.4. The Collateral Manager shall give notice thereof."
    )
    assert detect_sections(prose) == []


def test_the_inline_rule_leaves_a_document_that_already_parses_alone():
    """It is a fallback, gated on the anchored patterns having already failed,
    so no filing that reads correctly today can be changed by it."""
    from credit_extract.ingest.normalize import detect_sections

    anchored = "\n".join(
        [f"SECTION {n}. A Heading\nsome text about the agreement" for n in
         ("1.01", "1.02", "1.03", "2.01", "2.02", "2.03")]
    )
    ids = [marker.section_id for marker in detect_sections(anchored)]
    assert ids == ["1.01", "1.02", "1.03", "2.01", "2.02", "2.03"]


def test_lma_clause_headings_render_as_table_rows():
    """Cadeler's facility agreement produced ZERO sections in 412,976 chars.

    LMA agreements number provisions as Clauses and this filer renders each
    heading as a table row -- the number in one cell, the title in the next --
    so neither the anchored patterns nor the inline ones could see them. The
    document's 352 internal references are Clauses too, and the cross-reference
    extractor only knew "Section" and "Article", so the integrity check that
    exists to catch dangling references stayed silent on a document where
    nothing resolved. Two invisible failures composing into a clean report.
    """
    from credit_extract.ingest.normalize import detect_sections

    lma = (
        "THIS AGREEMENT is dated 11 September 2026. | 2.3 | Effectiveness | "
        "(a) | Subject to paragraph (b) below, the terms and conditions apply. "
        "| 5.4 | Lenders' participation | (a) | Each Lender shall participate. "
        "| 27.8 | Additional trustees | The Agent may appoint a co-trustee."
    )
    found = {marker.section_id for marker in detect_sections(lma)}
    assert {"2.3", "5.4", "27.8"} <= found


def test_a_pricing_grid_row_is_not_mistaken_for_a_clause_heading():
    """The table-row rule fires on documents where heading detection already
    failed, and a pricing grid is exactly the kind of table such a document
    still contains. Its number cells carry no decimal point in the clause
    sense and its second cell is a threshold rather than a title."""
    from credit_extract.ingest.normalize import detect_sections

    grid = (
        "Pricing Level | Consolidated Net Leverage Ratio | Term SOFR Loans "
        "| 1 | > 4.25:1.00 | 2.25% | 2 | < 4.25:1.00 | 2.00% "
        "| 3 | < 3.25:1.00 | 1.75% |"
    )
    assert detect_sections(grid) == []


def test_a_clause_cross_reference_is_extracted():
    """352 of Cadeler's references are "clause N.N" and none was seen."""
    from credit_extract.validate.invariants import _XREF_RE

    found = {m.group(1) for m in _XREF_RE.finditer(
        "in accordance with clause 5.4 (Lenders' participation) and Section 2.3"
    ) if m.group(1)}
    assert found == {"5.4", "2.3"}
