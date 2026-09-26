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

#: How long a body may be and still be *declined out loud*, which is a weaker
#: claim than reading a value out of it and so earns a looser cap.
#:
#: The tight cap above was costing exactly the cases the decline route exists
#: for: Essential Properties defines Applicable Margin as a table indexed by
#: Credit Rating Level and the body is 3,796 characters, so the tier skipped it
#: before it could report that it is a grid. The biggest grids are the longest
#: bodies. Declining to take a value from a long body is safe in a way taking
#: one is not.
#:
#: It is not unbounded, because the span travels with the candidate and a span
#: this side of ten thousand characters is a citation while one past it is a
#: chunk wearing a citation's clothes -- which is the defect F10 was opened for.
MAX_DECLINED_BODY_CHARS = 8_000

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


#: A tranche named inside a definition, with the phrase that attributes the
#: value to it. This is the shape that lets one definition state a different
#: number for each tranche::
#:
#:     " Floor " means a rate of interest equal to (i) with respect to Term
#:     Loans, 0.75% and (ii) with respect to Revolving Loans, 0.00%.
#:
#: Read from the text, which is the only place the attribution exists. Rate
#: types -- ``for ABR Loans``, ``for Term SOFR Loans`` -- deliberately do not
#: match: a body priced at two rate types is one tranche's price under two
#: conventions, not two tranches.
_ATTRIBUTED = re.compile(
    r"(?:with respect to|in the case of|applicable to)\s+"
    r"(?:any |each |all |the )?"
    r"((?:Initial |Incremental |Delayed Draw |Revolving |Term |Tranche [A-Z] )*"
    r"(?:Term Loans?|Term Facility|Term Loan Facility|Revolving Loans?|"
    r"Revolving Credit Loans?|Revolving Credit Facility|Revolving Facility|"
    r"Letters? of Credit|Swingline Loans?))",
    re.I,
)

#: How a named loan class maps onto the tranches ``models/assemble.py`` builds.
#: Longest-first in ``_ATTRIBUTED`` above, so "Initial Term Loans" is captured
#: whole and this only has to decide which tranche it is.
def _tranche_id(named: str) -> str:
    lowered = named.lower()
    if "delayed draw" in lowered:
        return "delayed_draw"
    if "swingline" in lowered or "letter" in lowered:
        return "lc_sublimit"
    if "revolving" in lowered:
        return "revolver"
    return "initial_term_loan"


def _distinct_values(body: str, kind: str) -> list[tuple[Any, str]]:
    """Every distinct value of ``kind`` in ``body``, with the text it came from."""
    parser = _PARSERS.get(kind)
    if parser is None:
        return []
    seen: list[tuple[Any, str]] = []
    for token in _candidates_in(body, kind):
        value = parser(token)
        if value is None:
            continue
        if not any(value == held for held, _ in seen):
            seen.append((value, token))
    return seen


def _sole_value(body: str, kind: str) -> tuple[Any, str] | None:
    """The single value of ``kind`` in ``body``, with the text it came from.

    None where there is no value, and also where there is more than one: a
    definition offering two dates does not settle which is the term's, and
    guessing would be exactly the failure this module exists to fix.
    """
    seen = _distinct_values(body, kind)
    return seen[0] if len(seen) == 1 else None


def _per_tranche_values(body: str, kind: str) -> dict[str, tuple[Any, str]]:
    """One value per tranche the body attributes a number to, or ``{}``.

    Empty unless the body attributes to at least two tranches AND each of
    their segments settles on exactly one value. That is deliberately narrow.
    The alternative shapes are a pricing grid indexed by rate type, leverage
    level or period -- Somnigroup prices six loan classes at two rate types
    each, Essential Properties indexes by Credit Rating Level -- and a tranche
    id cannot answer those either, so producing a number for them would mean
    picking one cell of a table and calling it the term.
    """
    matches = list(_ATTRIBUTED.finditer(body))
    if len(matches) < 2:
        return {}
    bounds = [m.end() for m in matches] + [len(body)]
    found: dict[str, tuple[Any, str]] = {}
    for index, match in enumerate(matches):
        segment = body[bounds[index]:bounds[index + 1]]
        value = _sole_value(segment, kind)
        if value is None:
            return {}
        tranche = _tranche_id(match.group(1))
        if tranche in found and found[tranche][0] != value[0]:
            # The same tranche named twice at two prices: a period or rate-type
            # axis hiding inside the attribution. Not settleable here.
            return {}
        found[tranche] = value
    return found if len(found) >= 2 else {}


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
            if not body or len(body) > MAX_DECLINED_BODY_CHARS:
                continue
            if _SELF_REFERENTIAL.search(body):
                # Defines the term by pointing back at the agreement, so any
                # percentage in it belongs to the surrounding mechanics.
                continue
            span = _definition_span(graph, resolved)
            if span is None:
                continue
            if len(body) > MAX_BODY_CHARS:
                # Too long to read a value out of, long enough to say why.
                out.extend(_unsettled(spec, resolved, body, span, Candidate))
                break

            per_tranche = _per_tranche_values(body, spec.kind)
            if per_tranche:
                for tranche_id, (value, as_written) in sorted(per_tranche.items()):
                    out.append(Candidate(
                        field=spec.name,
                        value=value,
                        span=span,
                        confidence=CONFIDENCE,
                        pass_id="deterministic:definitions",
                        segmentation="definitional",
                        applies_to=tranche_id,
                        quantity=quantity_for(value, spec.kind,
                                              as_written=as_written),
                        notes=(
                            f"the definition of {resolved!r} states this term "
                            f"per tranche, and this is the value it gives for "
                            f"{tranche_id}"
                        ),
                    ))
                break

            found = _sole_value(body, spec.kind)
            if found is None:
                out.extend(_unsettled(spec, resolved, body, span, Candidate))
                # Still the field's own term, so no later anchor should answer
                # for it: a value read from "SOFR Adjustment" when "Term SOFR
                # Adjustment" was the term and carried a grid is the wrong
                # clause with the right shape.
                break
            value, as_written = found
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


def _unsettled(
    spec: FieldSpec, term: str, body: str, span: Span, candidate_cls: Any,
) -> list[Any]:
    """A valueless candidate where the definition carries several values.

    This tier already declined these -- a body with several percentages in it
    settles nothing -- but it declined them *silently*, and silence reads
    downstream as "no pass produced a candidate", which invites the negative
    space validator to confirm the term absent. Essential Properties states its
    Applicable Margin as a table indexed by Credit Rating Level; reporting that
    document as having no margin is the F10 failure with a probability printed
    beside it.

    So: no value, because there is no single value, and a span plus a qualifier
    saying what is actually there. ``reconcile`` keeps both, and Validator C
    reads the qualifier and refuses to call the field absent -- the same route
    ``untypable_value`` already takes.

    It asserts nothing, and it is careful not to assert a gap either. The claim
    is only "this term is defined and its definition carries these numbers",
    which is true whether the definition is a pricing grid, a step-down
    schedule or an amendment history.

    In particular it does not claim the field is unresolvable.
    ``applicable_margin.eurodollar_top_level_pct`` is *defined* as "the highest
    Eurodollar Applicable Margin in the pricing grid", so for that field a grid
    is the expected shape and a reduction over it is the answer -- and the
    reduction is not available here, because ``_distinct_values`` returns the
    percentages in reading order with no idea which column each sits in.
    Essential Properties' grid has four margin columns and the label for it
    records three wrong answers that are easier to reach than the right one:
    0.675% is the top row, 1.350% is the same cell for the revolver, 0.550% is
    the Base Rate on the right row. A max over the flattened list would
    sometimes hit 1.550% and sometimes not, at 0.90 from the definitions
    article, which is the authoritative-looking wrong answer this module's
    header warns about. Reading the grid is the model tier's job; saying it is
    a grid is this tier's.
    """
    values = _distinct_values(body, spec.kind)
    if len(values) < 2:
        return []  # nothing of this kind in the body at all: genuinely silent
    quoted = ", ".join(str(written) for _, written in values[:6])
    axis = _AXIS.search(body)
    qualifiers = {"unsettled_in_definition": quoted}
    if axis:
        # Carried as a qualifier and not only in the note, because the note is
        # overwritten by whichever validator speaks last and the axis is the
        # actionable half: it says what a reader has to supply to resolve the
        # term, rather than merely that it is unresolved.
        qualifiers["indexed_by"] = axis.group(0)
    return [candidate_cls(
        field=spec.name,
        value=None,
        span=span,
        confidence=CONFIDENCE,
        pass_id="deterministic:definitions",
        segmentation="definitional",
        qualifiers=qualifiers,
        notes=(
            f"the definition of {term!r} carries {len(values)} values "
            f"({quoted})"
            + (f" and is indexed by {axis.group(0)}" if axis else "")
            + ", so this tier settles none of them. Whether one of these is "
              "the field's value, or a reduction over them is, needs the "
              "grid's own rows and columns -- which a list of the numbers in "
              "reading order is not"
        ),
    )]


#: The axes a pricing grid is indexed by, for the note. Naming the axis is
#: worth a line because it tells a reader what would have to be supplied to
#: resolve the term -- a leverage ratio, a rating, a date.
_AXIS = re.compile(
    r"Pricing Level|Credit Rating Level|Rating Level|Leverage Ratio|"
    r"Total Net Leverage|First Lien Net Leverage|Adjustment Date|"
    r"Type of Loan|relevant Class",
    re.I,
)
