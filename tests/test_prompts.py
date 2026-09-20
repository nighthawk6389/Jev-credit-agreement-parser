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
