"""The prompt enforces the same rules the parser enforces downstream.

Both layers matter: the prompt is what stops a model producing an unprovenanced
value, and the parser is what stops one reaching the record when the prompt
fails. Testing the prompt text is testing the first of those two.
"""

from __future__ import annotations

from credit_extract.extract.prompts import build_extraction_prompt, build_reread_prompt
from credit_extract.models.fpml_model import FIELD_REGISTRY


def _specs(*names: str):
    return [FIELD_REGISTRY[n] for n in names]


def _flat(text: str) -> str:
    """Collapse whitespace: the prompt is wrapped, the rules are not."""
    return " ".join(text.split())


def test_the_prompt_demands_a_verbatim_quote():
    prompt = _flat(build_extraction_prompt("some clause", _specs("libor_floor_pct")))
    assert "verbatim" in prompt
    assert "character-for-character" in prompt
    assert "discarded" in prompt, (
        "the prompt must say what happens to an unquoted value"
    )


def test_the_prompt_forbids_arithmetic():
    prompt = _flat(build_extraction_prompt("some clause", _specs("libor_floor_pct")))
    assert "Never add, subtract" in prompt
    assert "annualize" in prompt
    assert "0.25% per quarter" in prompt, (
        "the rule needs a worked example or it reads as boilerplate"
    )


def test_the_prompt_routes_external_dependencies_away_from_a_number():
    prompt = _flat(build_extraction_prompt("some clause", _specs("libor_floor_pct")))
    assert "external_document" in prompt
    assert "Do not guess the magnitude" in prompt


def test_omission_is_distinguished_from_absence():
    """A chunk saying nothing is not the agreement saying nothing."""
    prompt = _flat(build_extraction_prompt("some clause", _specs("mfn_sunset")))
    assert 'Omission means "not here"' in prompt
    assert 'it does not mean "not in the agreement"' in prompt


def test_the_definition_closure_is_pasted_ahead_of_the_clause():
    closure = '"Consolidated Total Debt" means the aggregate principal amount'
    prompt = build_extraction_prompt(
        "the clause text", _specs("financial_covenant.opening_level"), closure
    )
    assert closure in prompt
    assert prompt.index(closure) < prompt.index("the clause text"), (
        "the closure must be resolvable before the clause is read"
    )


def test_no_context_block_when_there_is_no_closure():
    prompt = build_extraction_prompt("clause", _specs("libor_floor_pct"), "")
    assert "DEFINED TERMS IN SCOPE" not in prompt


def test_field_list_carries_kinds_and_definition_anchors():
    prompt = build_extraction_prompt(
        "clause", _specs("financial_covenant.opening_level")
    )
    assert "financial_covenant.opening_level (ratio)" in prompt
    assert "Total Leverage Ratio" in prompt


def test_reread_prompt_tells_the_model_what_is_already_captured():
    prompt = _flat(build_reread_prompt("a call protection clause", ["mfn_threshold_pct"]))
    assert "ALREADY CAPTURED" in prompt
    assert "mfn_threshold_pct" in prompt
    assert "Report anything the list above misses" in prompt


def test_reread_prompt_handles_a_chunk_that_captured_nothing():
    prompt = build_reread_prompt("an orphan clause", [])
    assert "- nothing" in prompt


# ---------------------------------------------------------------------------
# The prompt and the schema are one contract
# ---------------------------------------------------------------------------


def test_the_response_shape_in_the_prompt_is_the_schema_the_api_enforces():
    """The two halves of one contract, and they had drifted apart.

    ``EXTRACTION_SCHEMA`` goes to the API as ``output_config.format``, so the
    response is constrained to it. The prompt showed a bare JSON array where
    the schema requires a ``fields`` object, and asked for a ``qualifiers`` key
    the schema's ``additionalProperties: false`` forbade -- which is a 400 on
    one side and a silently dropped instruction on the other. Comparing the
    key sets is what keeps a future edit to either half honest.
    """
    import re

    from credit_extract.extract.passes import EXTRACTION_SCHEMA

    prompt = build_extraction_prompt("clause", _specs("libor_floor_pct"))
    skeleton = prompt[prompt.index('{"fields"'):]
    in_prompt = set(re.findall(r'"(\w+)":', skeleton)) - {"fields"}

    item = EXTRACTION_SCHEMA["properties"]["fields"]["items"]
    assert in_prompt == set(item["properties"]), (
        "the prompt asks for keys the schema does not declare, or omits keys "
        f"it does: prompt={sorted(in_prompt)} schema={sorted(item['properties'])}"
    )
    assert set(item["required"]) == set(item["properties"]), (
        "every documented structured-output schema lists all properties in "
        "'required' and makes the optional ones nullable instead; a property "
        "outside 'required' is what the API rejects"
    )
    assert '{"fields": [' in prompt, (
        "the schema's envelope is an object with a 'fields' array, not a bare "
        "array; a prompt showing the wrong envelope fights the constraint"
    )


def test_the_system_prompt_is_actually_sent():
    """It was defined in this module and never passed to a request."""
    import inspect

    from credit_extract.extract.passes import AnthropicBackend

    source = inspect.getsource(AnthropicBackend.extract)
    assert "system=EXTRACTION_SYSTEM" in source


def test_the_system_prompt_says_which_error_is_the_expensive_one():
    from credit_extract.extract.prompts import EXTRACTION_SYSTEM

    assert "quote" in EXTRACTION_SYSTEM and "never compute" in EXTRACTION_SYSTEM
    assert "worse than no answer" in EXTRACTION_SYSTEM


# ---------------------------------------------------------------------------
# Each rule below is in the prompt because a corpus document broke on it.
# The drafting is tested, not the rule: a rule stated abstractly gets skimmed,
# and the example is the part that generalises to the next agreement.
# ---------------------------------------------------------------------------


def _extraction_rules() -> str:
    return _flat(build_extraction_prompt("clause", _specs("libor_floor_pct")))


def test_the_prompt_warns_that_a_defined_term_resolves_per_tranche():
    rules = _extraction_rules()
    assert "Term Loan Maturity Date" in rules
    assert "Never take the first branch because it is first" in rules


def test_the_prompt_warns_that_a_grid_has_more_columns_and_rows_than_it_looks():
    rules = _extraction_rules()
    assert "Read the column heading, not the position" in rules
    assert "the top row is normally the best rating level" in rules, (
        "the highest margin in a grid is at the bottom, which is the reading "
        "that was got wrong by hand on the first document tried"
    )


def test_the_prompt_warns_that_a_recital_names_the_superseded_facility():
    rules = _extraction_rules()
    assert "Existing Credit Agreement" in rules
    assert "is not this agreement's" in rules


def test_the_prompt_prefers_a_definition_to_a_narrative_sentence():
    rules = _extraction_rules()
    assert "the definition governs" in rules
    assert "only one of those is the defined Closing Date" in rules


def test_the_prompt_handles_a_blackline_that_survived_ingestion():
    rules = _extraction_rules()
    assert "September 16 15 , 2026 2027" in rules
    assert "the operative value is the replacement" in rules


def test_the_prompt_demands_the_unit_travel_with_the_number():
    rules = _extraction_rules()
    assert '"22.5 bps", not "22.5"' in rules
    assert "hundredfold error" in rules


def test_the_prompt_covers_a_table_scale_header():
    rules = _extraction_rules()
    assert "(in thousands)" in rules
    assert "three orders of magnitude" in rules


def test_the_prompt_says_zero_is_a_value():
    rules = _extraction_rules()
    assert "Zero is a value" in rules
    assert "not the same fact as an agreement with no floor" in rules


def test_the_prompt_forbids_normalising_a_value_into_the_fields_type():
    """The covenant written as 60% against a ratio-typed field."""
    rules = _extraction_rules()
    assert '"3.50:1.00" where it is measured against EBITDA' in rules
    assert "do not withhold the value because it does not look like what the "\
           "field expects" in rules


def test_the_prompt_names_the_real_external_reference_phrasings():
    rules = _extraction_rules()
    for phrasing in (
        "as separately agreed",
        "in accordance with the terms of each fee letter",
        "has the meaning assigned to such term in the Fifth Amendment",
    ):
        assert phrasing in rules


def test_the_prompt_refuses_arithmetic_from_a_date_the_document_merely_bears():
    rules = _extraction_rules()
    assert "the 180th day after the Fifth Amendment Effective Date" in rules
    assert "an agreement's date is not the date it became effective" in rules


def test_the_prompt_separates_contingent_machinery_from_a_term_in_force():
    """The credit spread adjustment check fired on forty agreements that
    correctly have none, because benchmark replacement drafting looks like a
    spread until you read what triggers it."""
    rules = _extraction_rules()
    assert "Benchmark Replacement" in rules
    assert "may be a positive or negative value or zero" in rules
    assert "shall be entitled, but shall not be required" in rules


def test_the_prompt_keeps_a_conditional_step_up_out_of_the_scalar():
    rules = _extraction_rules()
    assert "report the unconditional value and put the condition in notes" in rules


def test_the_prompt_treats_a_reserved_heading_as_evidence():
    rules = _extraction_rules()
    assert "[Reserved]" in rules and "[Intentionally Omitted]" in rules
    assert "it is different from silence" in rules


def test_the_prompt_says_not_every_exhibit_is_a_credit_agreement():
    """Eighteen of the hundred harvested documents are not credit agreements."""
    rules = _extraction_rules()
    assert "Not every exhibit filed as a loan document is a credit agreement" in rules
    assert "do not map its figures onto facility fields" in rules


def test_the_prompt_calibrates_confidence_rather_than_asking_for_a_feeling():
    rules = _extraction_rules()
    assert "Confidence is a calibration, not an enthusiasm" in rules
    assert "Below 0.5, prefer omitting the field to answering it" in rules


def test_the_definition_anchors_are_offered_as_a_hint_not_a_search_term():
    """The registry's maturity anchor appears in two of a hundred agreements."""
    prompt = build_extraction_prompt("clause", _specs("initial_term_loan.maturity_date"))
    assert "a hint only" in prompt
    assert "may name it something else entirely" in prompt


def test_a_deal_defining_field_is_marked_as_one():
    prompt = build_extraction_prompt("clause", _specs("revolver.commitment"))
    assert "deal-defining" in prompt


def test_the_reread_prompt_asks_for_what_no_scalar_field_can_hold():
    rules = _flat(build_reread_prompt("a clause", []))
    assert "a condition that switches a term on or off" in rules
    assert "different values for different tranches" in rules
    assert '{"findings": [' in build_reread_prompt("a clause", [])
