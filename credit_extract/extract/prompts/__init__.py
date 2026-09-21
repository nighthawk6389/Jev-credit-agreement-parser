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

Everything else in the rule list is there because a real document in
``corpus/edgar`` broke on it. Each rule carries the drafting that motivates it,
because a rule stated abstractly reads as boilerplate and gets skimmed -- and
because the drafting is the part that generalises. The failure being designed
against is never "the model did not find the clause"; it is "the model found
the clause, read it the way a careful person would read it in isolation, and
produced a confidently wrong answer". Reading the right sentence and taking the
wrong number out of it is what these agreements are shaped to cause.

The response shape here must stay identical to ``EXTRACTION_SCHEMA`` in
``passes.py``. That schema is sent as ``output_config.format``, so the API
constrains the answer to it: a key the schema forbids is a 400 and a key the
schema requires but the prompt never mentions is a field the model omits.
"""

from __future__ import annotations

from pathlib import Path

from ...models.fpml_model import FieldSpec

_HERE = Path(__file__).parent

EXTRACTION_SYSTEM = (
    "You extract terms from credit agreements. You quote; you never compute. "
    "A wrong answer stated confidently is worse than no answer, because the "
    "record presents it as settled and nobody downstream re-reads the "
    "document. When the agreement is ambiguous, say so in notes and lower the "
    "confidence rather than picking the reading that makes the field look "
    "answered."
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

A. WHAT COUNTS AS A VALUE

1. Report a value only if this excerpt states it. Do not infer it from another
   document, from market convention, or from what is typical.
2. Every value must be accompanied by "quote": a span of text copied verbatim
   and character-for-character from the excerpt above. A value whose quote
   cannot be found in the excerpt is discarded.
3. The excerpt has been through an HTML-to-text conversion. Table cells are
   separated by "|", and a page break can interrupt a sentence mid-clause,
   leaving a stray page number inside it -- "the aggregate amount of the
   Initial Term Loan Commitments on the Restatement Effective | | 33 Date was
   $200,000,000". Copy those artefacts exactly if your quote spans them, or
   quote the shorter run that is clean. Do not tidy the text.
4. Never add, subtract, multiply, annualize, convert or compare. Report the
   figure as written. If the excerpt says "0.25% per quarter", report that, not
   an annual figure.

B. WHICH VALUE, WHEN THE EXCERPT OFFERS MORE THAN ONE

5. A defined term often resolves to several values, one per tranche or
   facility: '"Term Loan Maturity Date": (a) with respect to the Initial Term
   Loans, April 12, 2024, (b) with respect to the Second Tranche Term Loan,
   January 25, 2028, (c) ...'. Report the one the field names and list the
   others in notes. Never take the first branch because it is first.
6. A pricing grid usually has more columns than it appears to. One table can
   carry a benchmark margin and a base rate margin, for a revolver and for a
   term loan -- four figures on every row. Read the column heading, not the
   position, and check the row: the top row is normally the best rating level
   and therefore the lowest spread, so "the highest margin in the grid" is at
   the bottom of the table and not the top.
7. A figure in a recital describing a prior agreement is not this agreement's.
   "that certain $300,000,000 Revolving Credit Agreement, dated as of June 25,
   2018 ... the Existing Credit Agreement" can sit a few lines from the
   $1,300,000,000 facility that replaced it.
8. Where a quoted definition and a narrative sentence disagree, the definition
   governs. An amendment recites its own date, the date of the agreement it
   amends, and the date of every prior amendment; only one of those is the
   defined Closing Date, and it is frequently the least prominent.
9. If both a struck figure and its replacement survive in the text -- "the
   earlier of (i) September 16 15 , 2026 2027" -- the operative value is the
   replacement. Report it, say in notes that the excerpt is a blackline, and
   lower the confidence.

C. HOW TO WRITE THE VALUE DOWN

10. Copy the unit with the number. "22.5 bps", not "22.5". "3.50:1.00", not
    "3.50". "60%", not "60". A grid quoted in basis points against a field
    measured in percent is a hundredfold error that reads as an ordinary
    number, and it is the single cheapest mistake to make here.
11. Where a table header sets a scale -- "(in thousands)", "($ in millions)" --
    the cell alone is wrong by three orders of magnitude. Put the scale in
    qualifiers and, where you can, quote the header with the figure.
12. Zero is a value. "the initial Floor ... shall be zero", "shall be deemed to
    be zero" -- a floor set at zero is a term the drafters wrote a sentence
    for, and it is not the same fact as an agreement with no floor. Report it;
    do not treat it as an absence.
13. Report the value in the shape the document uses, not the shape the field's
    type suggests. A leverage covenant is "3.50:1.00" where it is measured
    against EBITDA and "60%" where it is measured against asset value. Do not
    convert one into the other, and do not withhold the value because it does
    not look like what the field expects -- a value the record cannot type is
    still information, and dropping it reports the agreement as silent.

D. WHEN NOT TO WRITE A VALUE

14. If the excerpt makes the value depend on a document not contained in this
    agreement -- a sponsor model, a disclosure letter, a schedule that is not
    reproduced, something "as separately agreed", fees payable "in accordance
    with the terms of each fee letter", a term that "has the meaning assigned
    to such term in the Fifth Amendment" -- set "value" to null and name that
    document in "external_document". Do not guess the magnitude.
15. A deadline computable only from a term defined elsewhere is external too.
    "the 180th day after the Fifth Amendment Effective Date" is not resolvable
    from the date the Fifth Amendment bears: an agreement's date is not the
    date it became effective, and arithmetic from it is a guess with a
    calculation in front of it.
16. A provision describing what would happen on a future event is not a term in
    force. Benchmark replacement machinery -- a "Benchmark Replacement
    Adjustment" that "may be a positive or negative value or zero" -- is
    standard drafting against the day the benchmark is discontinued, not a
    spread the facility pays today. The same applies to a pricing amendment the
    borrower "shall be entitled, but shall not be required" to negotiate.
    Report null and explain in notes; do not report the contingent figure.
17. If a value applies only while a condition holds, report the unconditional
    value and put the condition in notes -- a covenant at 60% that steps to 65%
    for four quarters after a qualifying acquisition is 60%, with the step-up
    recorded. If there is no unconditional value, report null with the quote.
18. If a field is simply not addressed in this excerpt, omit it. Omission means
    "not here"; it does not mean "not in the agreement".
19. "[Reserved]" and "[Intentionally Omitted]" are positive statements that a
    provision was removed. Report null with that quote: it is evidence, and it
    is different from silence.
20. Not every exhibit filed as a loan document is a credit agreement. If this
    excerpt is from an indenture, a note purchase agreement, a warrant, a stock
    transfer agreement, financial statements or a press release, do not map its
    figures onto facility fields. Say so in notes rather than answering.

E. NOTES, QUALIFIERS AND CONFIDENCE

21. The field names are a guide to meaning, not to vocabulary. Agreements name
    the same term many different ways, and a field's listed defined terms are a
    hint that often will not appear at all. If the excerpt states the thing the
    description describes under another name, report it and give the name the
    agreement uses in notes.
22. Use "notes" for everything the value alone cannot carry: which tranche or
    facility it belongs to, what condition governs it, which other values the
    same defined term takes, whether the clause is written in the past tense
    about a tranche that has matured, and any reading you rejected.
23. Use "qualifiers" for the measurement convention: which EBITDA a percentage
    is measured against, per annum against per quarter, the scale a table
    header sets, the facility a margin applies to.
24. Confidence is a calibration, not an enthusiasm. Use 0.9 and above only when
    the excerpt states this field's exact subject in the terms its description
    names. Use 0.5 to 0.7 when you are mapping something adjacent -- a facility
    fee charged on the whole commitment reported in a field defined as an
    unused commitment fee, say -- and say in notes what the difference is.
    Below 0.5, prefer omitting the field to answering it.

RESPONSE

Reply with JSON only, no prose. Every key below must be present on every
entry; use null rather than leaving one out, and {{}} for empty qualifiers.

{{"fields": [
  {{"field": "<field name>", "value": <string or null>, "quote": "<verbatim>",
   "confidence": <0.0-1.0>, "external_document": <string or null>,
   "qualifiers": {{}}, "notes": <string or null>}}
]}}
"""

_CONTEXT_TEMPLATE = """\
DEFINED TERMS IN SCOPE (resolve these before reading the excerpt)
----------------------------------------------------------------
{context}

"""


def _describe(spec: FieldSpec) -> str:
    """One line per field: what it means, and where it tends to live.

    The defined terms are labelled a hint rather than printed bare. Across the
    hundred real agreements in the corpus the registry's anchor for a maturity
    date appears in two of them, and a field list that presents an anchor as
    the thing to search for turns that into a systematic miss.
    """
    line = f"- {spec.name} ({spec.kind}): {spec.description}"
    if spec.definition_anchors:
        line += (
            f" [often defined as: {', '.join(spec.definition_anchors)} -- a hint"
            " only; this agreement may name it something else entirely]"
        )
    if spec.section_hints:
        line += f" [often near Section {', '.join(spec.section_hints)}]"
    if spec.criticality >= 5:
        line += " [deal-defining: a wrong answer here is a material error]"
    return line


def build_extraction_prompt(
    chunk_text: str, specs: list[FieldSpec], context: str = ""
) -> str:
    """Assemble the extraction prompt for one chunk."""
    lines = [_describe(spec) for spec in specs]
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
magnitude. Two things in particular are worth reporting even though no field
holds them, because the record is a scalar and the agreement is not: a
condition that switches a term on or off, and a term that takes different
values for different tranches.

Reply with JSON only:

{{"findings": [
  {{"finding": "<short name>", "kind": "payment_obligation|restriction|override|
   threshold|date|other", "quote": "<verbatim>", "summary": "<one sentence>",
   "confidence": <0.0-1.0>, "external_document": <string or null>}}
]}}
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
