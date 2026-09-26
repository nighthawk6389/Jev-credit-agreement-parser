"""Read a field's value out of the defined term it hangs off.

WHY THIS EXISTS
===============

``closing_date`` was the worst field in the corpus and the reason was not that
it is hard. It is that the rule meant to read it does not fire, and the
fallback meant to catch the remainder fires everywhere::

    Rule("closing_date", r'"Closing Date"\\s+means\\s+([^.]+)\\.', 0.95)
    Rule("closing_date", r"dated as of ([A-Z][a-z]+ \\d{1,2}, \\d{4})", 0.70)

The first matches 3 of the 100 harvested agreements, because most of them
write ``" Closing Date ": June 25, 2018.`` -- a colon, not ``means``. So for
90 documents the only rule that fires is the second, and *every amendment
recital* says "dated as of". On Essential Properties it yields eleven distinct
dates from nineteen matches; across the corpus, 527 distinct dates. All of
them arrive tagged ``deterministic:``, which in ``reconcile`` means a flat
0.95 extraction confidence and the top of the ranking key.

That is how a field ends up reported as 2019-11-26 on a document whose
definitions article says, in as many words, ``" Closing Date ": June 25,
2018.``

THE PRINCIPLE
=============

    When a field's value is a defined term, the definition is the answer and
    every other occurrence is a mention.

The definition graph already knows which span is the definition -- that is
what it is for -- and the registry already records, in
``FieldSpec.definition_anchors``, which defined term each field hangs off.
Nothing was joining the two on the deterministic side. This module is that
join: for every field whose anchor resolves, parse the definition body for a
value of the field's own kind, and cite the definition's span.

It is deterministic, costs nothing, and runs before the model tier -- the same
slot ``table_candidates`` occupies, and for the same reason: a value the
document states in a place the document itself designates as authoritative
does not need a model to find it.

WHAT IT DOES NOT DO
===================

It does not guess. A definition whose body carries no value of the right kind
yields no candidate, which is the honest outcome for the many agreements whose
Closing Date is an *event* -- "the date on which the conditions in Section 4.1
are satisfied" -- rather than a date. Several labels in the corpus assert
exactly that, and a rule that produced a date for them would be manufacturing
one.

It also takes only the first value of the right kind in the body, and only
when the body is short enough to be a definition rather than a section. A
definition that carries several dates is a definition whose value this cannot
settle, and a wrong answer here would inherit the deterministic tier's 0.95.
"""

from __future__ import annotations

import re
from typing import Any

from ..ingest.normalize import NormalizedDocument
from ..ingest.tables import parse_date, parse_money, parse_percent, parse_ratio
from ..models.core import Span
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from ..models.quantities import quantity_for

#: Bodies longer than this are not definitions in the sense this relies on.
#: A defined term whose body runs to several thousand characters is a section
#: with a name, and the first date in it is not "the" value.
MAX_BODY_CHARS = 1_200

#: How confident a value read from its own definition is. Below the tables
#: tier, which cites an exact cell, and above the anchored prose rules: the
#: document designated this span as where the term is settled, and the parse
#: is ordinary.
CONFIDENCE = 0.90

_PARSERS = {
    "date": parse_date,
    "money": parse_money,
    "percent": parse_percent,
    "ratio": parse_ratio,
}


#: A definition that sends the reader back to the agreement instead of stating
#: a value. ``Floor`` is where this was found and it is the majority shape after
#: the usable one: of 100 harvested agreements, 35 define ``Floor``, 20 with a
#: single clean percentage and **13 like this** --
#:
#:     " Floor ": the benchmark rate floor, if any, provided in this Agreement
#:     initially (as of the execution of this Agreement, the modification,
#:     amendment or renewal of this Agreement ...)
#:
#: That is the Benchmark Replacement machinery defining ``Floor`` as whatever
#: floor the agreement provides, which is a circularity rather than a rate. The
#: bodies often carry a percentage from the surrounding transition mechanics,
#: so without this guard the tier would emit one as the floor -- at 0.90, from
#: the definitions article, which is the most authoritative-looking wrong
#: answer available.
_SELF_REFERENTIAL = re.compile(
    r"provided in this Agreement"
    r"|the benchmark rate floor"
    r"|as (?:otherwise )?(?:set forth|specified|provided) (?:in|under) "
    r"(?:this Agreement|Section)",
    re.I,
)


def _sole_value(body: str, kind: str) -> tuple[Any, str] | None:
    """The single value of ``kind`` in ``body``, with the text it came from.

    None where there is no value, and also where there is more than one: a
    definition offering two dates does not settle which is the term's, and
    guessing would be exactly the failure this module exists to fix.
    """
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    seen: list[tuple[Any, str]] = []
    for token in _candidates_in(body, kind):
        value = parser(token)
        if value is None:
            continue
        if not any(value == held for held, _ in seen):
            seen.append((value, token))
        if len(seen) > 1:
            return None
    return seen[0] if seen else None


def _candidates_in(body: str, kind: str) -> list[str]:
    """Substrings of ``body`` that might parse as ``kind``."""
    if kind == "date":
        return re.findall(
            r"[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{2,4}", body
        )
    if kind == "money":
        return re.findall(r"\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion))?", body)
    if kind == "percent":
        return re.findall(r"\d+(?:\.\d+)?\s*%", body)
    if kind == "ratio":
        return re.findall(r"\d+(?:\.\d+)?\s*(?:to|:)\s*\d+(?:\.\d+)?", body)
    return []


def _definition_span(graph: Any, term: str) -> Span | None:
    node = graph.get(term)
    return getattr(node, "span", None) if node is not None else None


def definition_candidates(
    doc: NormalizedDocument,
    graph: Any,
    specs: list[FieldSpec] | None = None,
) -> list[Any]:
    """One candidate per field whose defined term states its value outright."""
    from .passes import Candidate  # circular at module scope

    if graph is None:
        return []
    specs = specs or list(FIELD_REGISTRY.values())
    out: list[Candidate] = []

    for spec in specs:
        for anchor in spec.definition_anchors:
            resolved = graph.resolve(anchor)
            if resolved is None:
                continue
            node = graph.get(resolved)
            body = (getattr(node, "body", "") or "").strip()
            if not body or len(body) > MAX_BODY_CHARS:
                continue
            if _SELF_REFERENTIAL.search(body):
                # Defines the term by pointing back at the agreement, so any
                # percentage in it belongs to the surrounding mechanics.
                continue
            found = _sole_value(body, spec.kind)
            if found is None:
                continue
            value, as_written = found
            span = _definition_span(graph, resolved)
            if span is None:
                continue
            out.append(Candidate(
                field=spec.name,
                value=value,
                span=span,
                confidence=CONFIDENCE,
                pass_id="deterministic:definitions",
                segmentation="definitional",
                quantity=quantity_for(value, spec.kind, as_written=as_written),
                notes=(
                    f"read from the definition of {resolved!r}, which is where "
                    "this document settles the term; other occurrences are "
                    "mentions"
                ),
            ))
            break  # the first anchor that resolves is the field's own term
    return out
