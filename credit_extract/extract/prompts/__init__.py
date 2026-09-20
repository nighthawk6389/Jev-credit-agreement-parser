"""Prompt construction for the model-backed extraction passes.

Two rules are enforced by the prompt text and again by the parser that reads
the response:

* every value must come with a verbatim quote, because a value without a span
  is discarded downstream regardless of how confident the model was;
* the model is never asked to add, subtract, compare or count. It reports what
  the document says and Python does the arithmetic.

The definition closure is pasted in ahead of the clause. Long-tail errors
cluster at depth >= 3, and a model that cannot see what ``Consolidated Total
Debt`` resolves to will confidently extract the wrong leverage ratio.
"""

from __future__ import annotations

from pathlib import Path

from ...models.fpml_model import FieldSpec

_HERE = Path(__file__).parent

EXTRACTION_SYSTEM = (
    "You extract terms from credit agreements. You quote; you never compute."
)

_EXTRACTION_TEMPLATE = """\
You are reading one excerpt of a credit agreement and reporting only what it
says. Another system does all arithmetic, date comparison and counting.

{context_block}\
EXCERPT
-------
{chunk}

FIELDS TO LOOK FOR
------------------
{fields}

RULES
1. Report a value only if this excerpt states it. Do not infer it from another
   document, from market convention, or from what is typical.
2. Every value must be accompanied by "quote": a span of text copied verbatim
   and character-for-character from the excerpt above. A value whose quote
   cannot be found in the excerpt is discarded.
3. Never add, subtract, multiply, annualize, convert or compare. Report the
   figure as written. If the excerpt says "0.25% per quarter", report that, not
   an annual figure.
4. If the excerpt makes the value depend on a document not contained in this
   agreement -- a sponsor model, a disclosure letter, a schedule that is not
   reproduced, something "as separately agreed" -- set "value" to null and name
   that document in "external_document". Do not guess the magnitude.
5. If a field is simply not addressed in this excerpt, omit it. Omission means
   "not here"; it does not mean "not in the agreement".
6. For a value qualified by a base or measurement convention -- which EBITDA a
   percentage is measured against, whether a rate is per annum or per quarter --
   record it in "qualifiers".

Reply with JSON only, no prose:

[
  {{"field": "<field name>", "value": <value or null>, "quote": "<verbatim>",
   "confidence": <0.0-1.0>, "external_document": <string or null>,
   "qualifiers": {{}}, "notes": <string or null>}}
]
"""

_CONTEXT_TEMPLATE = """\
DEFINED TERMS IN SCOPE (resolve these before reading the excerpt)
----------------------------------------------------------------
{context}

"""


def build_extraction_prompt(
    chunk_text: str, specs: list[FieldSpec], context: str = ""
) -> str:
    """Assemble the extraction prompt for one chunk."""
    lines = []
    for spec in specs:
        line = f"- {spec.name} ({spec.kind}): {spec.description}"
        if spec.definition_anchors:
            line += f" [defined terms: {', '.join(spec.definition_anchors)}]"
        lines.append(line)
    context_block = _CONTEXT_TEMPLATE.format(context=context) if context.strip() else ""
    return _EXTRACTION_TEMPLATE.format(
        context_block=context_block,
        chunk=chunk_text,
        fields="\n".join(lines),
    )


_REREAD_TEMPLATE = """\
A sweep flagged this passage as saying something the extraction does not
capture. Read it closely and report any obligation, restriction, threshold,
cap, basket, date or schedule it creates.

{context_block}\
PASSAGE
-------
{chunk}

ALREADY CAPTURED FROM THIS PASSAGE
----------------------------------
{captured}

Report anything the list above misses. Same rules as extraction: verbatim
quotes, no arithmetic, name any external document rather than guessing a
magnitude.

Reply with JSON only:

[
  {{"finding": "<short name>", "kind": "payment_obligation|restriction|override|
   threshold|date|other", "quote": "<verbatim>", "summary": "<one sentence>",
   "confidence": <0.0-1.0>, "external_document": <string or null>}}
]
"""


def build_reread_prompt(
    chunk_text: str, captured: list[str], context: str = ""
) -> str:
    """Tier 4: a targeted re-read of a chunk the orphan sweep flagged."""
    context_block = _CONTEXT_TEMPLATE.format(context=context) if context.strip() else ""
    return _REREAD_TEMPLATE.format(
        context_block=context_block,
        chunk=chunk_text,
        captured="\n".join(f"- {c}" for c in captured) or "- nothing",
    )
