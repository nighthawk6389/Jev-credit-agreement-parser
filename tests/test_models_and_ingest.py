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
