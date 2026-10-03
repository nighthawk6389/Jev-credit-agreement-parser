"""The economic terms of fund and BDC facilities, read where the agreement
settles them.

WHY THIS EXISTS
===============

The out-of-sample run used twenty BDC credit facilities, and the rules tier
read no commitment, maturity, top margin or commitment fee on any of them --
none of the 61 values their blind labels carry. Every rule for those fields
had been written against the synthetic fixture's drafting ("Revolving Credit
Commitments on the Closing Date are $X", '"Revolving Credit Maturity Date"
means ...'), and fund facilities do not write that. They write::

    " Facility Amount " means, as of any date, an amount equal to the lesser
    of (a) $200,000,000 and (b) the aggregate principal amount of the
    Commitments ...

    " Termination Date " means the earlier of (a) the date that is two (2)
    years after the Revolving Period End Date or (b) ...

    " Applicable Spread " means (a) prior to the twelve (12) month anniversary
    of the Closing Date, 1.875%; (b) ... 2.00%; and (c) ... 2.125%; provided
    that if an Event of Default has occurred ... increased by 2.00%.

and a Non-Usage Fee whose rate sits inside a sum of products. Validator A can
confirm a value someone read; it cannot confirm one nobody read.

HOW IT WAS BUILT
================

Labels first. The five fields were labelled on the twenty in-sample fund and
BDC agreements, twice and independently, before any of this was written. The
readers were then written against the fit and contaminated agreements only;
the holdout agreements were measured, never read for a form. Each reader below
names the in-sample agreement whose drafting it was written for.

THE PRINCIPLE
=============

A value is taken only from the place the agreement designates as settling the
term -- the term's own definition, or a chain of definitions that ends at a
date -- and only in a shape that fixes it. Every other shape is a decline, and
a decline produces no candidate: the field stays open for review, which is the
honest outcome, and validator C is still the only thing entitled to call it
absent.

The near misses are the point of the readers, more than the forms are. Each
one sits beside the right answer in these documents:

* an accordion ceiling is not a commitment;
* the end of the revolving, reinvestment or funding period is not a maturity;
* the base-rate margin, the default increment and a margin an amendment has
  superseded are not the top margin;
* an upfront "Commitment Fee" charged on the whole facility, a fronting fee
  and a minimum utilization fee are not the unused fee.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

from ..ingest.normalize import NormalizedDocument
from ..ingest.tables import parse_money, parse_percent
from ..models.core import Span
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from ..models.quantities import quantity_for

#: The definitions tier's pass id, deliberately. ``reconcile`` ranks a value
#: read from the term's own definition above every other reading, and these
#: are exactly that: a value the agreement settles in its definitions article.
PASS_ID = "deterministic:definitions"
SEGMENTATION = "definitional"
CONFIDENCE = 0.90

MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")
_DATE = (r"(?:January|February|March|April|May|June|July|August|September|"
         r"October|November|December)\s+\d{1,2}\s*,\s*\d{4}")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _date(text: str) -> dt.date | None:
    """A calendar date the text opens with, tolerating "August 23 , 2030"."""
    match = re.match(r"\s*(" + _DATE + r")", text)
    if match is None:
        return None
    month, rest = match.group(1).split(None, 1)
    day, year = re.match(r"(\d{1,2})\s*,\s*(\d{4})", rest).groups()
    try:
        return dt.date(int(year), MONTHS.index(month.lower()) + 1, int(day))
    except ValueError:
        return None


def _candidate(field: str, value: Any, span: Span | None, notes: str,
               as_written: str | None = None,
               qualifiers: dict[str, str] | None = None,
               external: str | None = None) -> Any:
    from .passes import Candidate  # circular at module scope

    spec = FIELD_REGISTRY[field]
    return Candidate(
        field=field,
        value=value,
        span=span,
        confidence=CONFIDENCE,
        pass_id=PASS_ID,
        segmentation=SEGMENTATION,
        qualifiers=dict(qualifiers or {}),
        external_document=external,
        notes=notes,
        quantity=quantity_for(value, spec.kind, as_written=as_written),
    )


_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-",
                         "\u2013": "-", "\u2014": "-", "\u2212": "-"})


def _key(term: str) -> str:
    return " ".join(term.translate(_DASHES).split()).casefold()


def _resolve(graph: Any, term: str) -> str | None:
    """``graph.resolve``, tolerant of how the filing set the hyphen.

    PennantPark defines '" Non‐Usage Fee "' with U+2010, which an ASCII
    lookup never finds: the fee was in the definitions article all along.
    """
    name = graph.resolve(term)
    if name is not None:
        return name
    wanted = _key(term)
    return next((n for n in graph.nodes if _key(n) == wanted), None)


def _node(graph: Any, *terms: str) -> tuple[str, Any] | tuple[None, None]:
    """The first of ``terms`` the agreement defines, with its node."""
    for term in terms:
        name = _resolve(graph, term)
        if name is not None:
            return name, graph.get(name)
    return None, None


#: A definition whose substance is "see the fee letter". The rate exists and
#: is a term of the deal; this agreement does not carry it.
_IN_FEE_LETTER = re.compile(
    r"^[\s,:]*(?:has the meaning|shall have the meaning|means the (?:rate|"
    r"percentage)|is|as)?[^.]{0,60}?(?:specified|set forth|assigned to such "
    r"term|defined|provided)\s+(?:in|under)\s+(?:the\s+applicable|the|each|"
    r"such\s+Lender['’]s|any)?\s*((?:[A-Z0-9][\w\-]*\s+){0,5}?Fee\s+Letters?)",
)


_LETTER = r"(?P<doc>(?:[A-Z0-9][\w\-]*\s+){0,5}?Fee\s+Letters?)"

#: A term the agreement defines by pointing at a fee letter in prose, outside
#: the means / has-the-meaning grammar the definition graph reads. Eagle Point:
#: '"Unused Fee" is defined in the Lender Fee Letter.' and the same for its
#: "Applicable Margin".
_DEFINED_IN_LETTER = r'["“]\s*(?P<term>{terms})\s*["”]\s+is\s+defined\s+in\s+(?:the\s+)?' + _LETTER


def _defined_in_letter(doc: NormalizedDocument, field: str, terms: tuple[str, ...]) -> list[Any]:
    names = "|".join(re.escape(t).replace(r"\-", "[-\u2010\u2011]") for t in terms)
    match = re.search(_DEFINED_IN_LETTER.replace("{terms}", names), doc.text)
    if match is None:
        return []
    return [_candidate(
        field, None, doc.span(match.start(), match.end()),
        f"{match.group('term')!r} is defined in the {match.group('doc')}, which "
        "is not part of this agreement",
        external=match.group("doc"),
    )]


# ---------------------------------------------------------------------------
# Is there a revolver at all?
# ---------------------------------------------------------------------------

#: A defined term for this facility's revolving credit, and not the
#: vocabulary of the loans a fund holds: Ares CP Funding's "Revolving Loan
#: Asset" and StepStone's "Revolving Collateral Obligation" are portfolio
#: loans, as is a "Term Loan Asset".
_REVOLVING_TERM = re.compile(
    r"^(?:Aggregate\s+|Initial\s+|Additional\s+|Committed\s+)?Revolv", re.I)
_TERM_FACILITY_TERM = re.compile(
    r"\bTerm\s+Loans?\b|\bTerm\s+Loan\s+Commitments?\b|\bAmortization\s+"
    r"(?:Amount|Payment|Date|Schedule)s?\b|\bDelayed\s+Draw\s+Term", re.I)
_PORTFOLIO_TERM = re.compile(r"Collateral|Loan\s+Asset|Obligation|Reserve", re.I)
_REBORROW = re.compile(
    r"(?:may|shall|can)(?!\s+not)[^.]{0,80}?\bbe\s+re-?borrowed|\bborrow\b[^.]{0,20}"
    r"\bre-?borrow|\brepay\b[^.]{0,20}\bre-?borrow", re.I)
_NO_REBORROW = re.compile(r"(?:may|shall)\s+not\s+be\s+re-?borrowed", re.I)
_TERM_TITLE = re.compile(r"TERM\s+LOAN\s+(?:CREDIT\s+)?AGREEMENT", re.I)


def facility_revolves(doc: NormalizedDocument, graph: Any) -> bool:
    """False only where the agreement shows a term facility and nothing that
    revolves.

    The registry's commitment and maturity are the revolver's, and a term
    loan agreement states both in the same words a revolver does: Constellation
    Brands' "TERM LOAN CREDIT AGREEMENT" says "The aggregate amount of the
    Lenders' Commitments as of the Effective Date is $300,000,000", and
    Lifetime Brands' term loan has a "Maturity Date" and says repaid amounts
    "may not be reborrowed". An SPV advance facility often says neither way --
    StepStone, Ares CP Funding and PIMCO never use the word -- and is read.
    """
    if revolving_evidence(doc, graph):
        return True
    names = [n for n in graph.nodes if not _PORTFOLIO_TERM.search(n)]
    return not (
        _NO_REBORROW.search(doc.text)
        or any(_TERM_FACILITY_TERM.search(n) for n in names)
        or _TERM_TITLE.search(doc.text[:3000])
    )


def revolving_evidence(doc: NormalizedDocument, graph: Any) -> bool:
    """A defined revolving-credit term for this facility, or leave to reborrow."""
    return (
        any(_REVOLVING_TERM.search(n) and not _PORTFOLIO_TERM.search(n)
            for n in graph.nodes)
        or bool(_REBORROW.search(doc.text))
    )


# ---------------------------------------------------------------------------
# revolver.commitment
# ---------------------------------------------------------------------------

#: The defined terms fund facilities size themselves with, in the order a
#: drafter would reach for them. "Facility Amount" is PennantPark's, 5C's and
#: SLR's; "Maximum Facility Amount" StepStone's, BlackRock's and Ares CP
#: Funding's; "Maximum Commitment" KKR's.
_SIZE_TERMS = (
    "Facility Amount", "Maximum Facility Amount", "Maximum Commitment",
    "Maximum Commitments", "Financing Limit", "Maximum Financing Amount",
    "Commitment Amount", "Facility Limit",
)

#: A dollar figure, and never one that runs straight into more digits. Eagle
#: Point's conformed copy reads "the Facility Amount is $60,000,00075,000,000":
#: the struck figure fused to the new one, from which a looser pattern reads
#: $60,000,000 -- the number the amendment deleted.
_MONEY = r"\$\s*\d{1,3}(?:\s*,\s*\d{3}){2,}(?:\.\d+)?(?!\d|\s*,\s*\d)"

#: Shapes of a size definition that fix the size. Each was found in a fit or
#: contaminated agreement, and each puts the figure where a drafter puts the
#: committed amount rather than a ceiling.
_SIZE_SHAPES = (
    # PennantPark, 5C: "the lesser of (a) $200,000,000 and (b) the aggregate
    # principal amount of the Commitments". The other limb only ever lowers it.
    re.compile(r"lesser of\s*:?\s*\(\s*(?:a|i|1)\s*\)\s*(" + _MONEY + r")\s*"
               r"(?:and|or)\s*\(\s*(?:b|ii|2)\s*\)\s*the\s+(?:aggregate|total)\b"),
    # StepStone: "(a) $250,000,000 plus (b) the aggregate amount of New
    # Commitments that have become effective after the Closing Date". The
    # second limb is the accordion, stated as growth and not as a cap.
    re.compile(r"^[\s,]*(?:at any (?:date|time)\s*,\s*)?\(\s*(?:a|i)\s*\)\s*("
               + _MONEY + r")\s*plus\s*\(\s*(?:b|ii)\s*\)\s*the\s+aggregate\s+"
               r"(?:principal\s+)?amount\s+of\s+(?:any\s+|all\s+)?(?:New|"
               r"Increased|Incremental|Additional)\s+Commitments"),
    # SLR: "(a) prior to the end of the Revolving Period, $35,000,000, unless
    # this amount is permanently reduced ... or increased ..."
    re.compile(r"^[\s,]*\(\s*(?:a|i)\s*\)\s*(?:prior to|during|before)\s+the\s+"
               r"(?:end\s+of\s+the\s+)?(?:Revolving|Reinvestment|Availability|"
               r"Funding)\s+Period\s*,\s*(" + _MONEY + r")"),
    # BlackRock Monticello: "the aggregate Commitments as then in effect,
    # which amount shall not exceed $410,000,000 (as such amount may be
    # increased ... in accordance with Section 2.13)". The figure is the size
    # as signed -- the cover page prints it and the increase option is a
    # separate amount on top -- and both labellers read it so.
    re.compile(r"^[\s,]*the\s+aggregate\s+Commitments\s+(?:of\s+the\s+Lenders\s+)?"
               r"(?:as\s+)?then\s+in\s+effect\s*,\s*which\s+amount\s+shall\s+not\s+"
               r"exceed\s+(" + _MONEY + r")\s*\(\s*as\s+such\s+amount\s+may\s+be\s+"
               r"increased"),
)

#: The tranche totals a multicurrency BDC revolver states inside each
#: tranche's definition: Blue Owl Technology's and Fidelity's "The aggregate
#: amount of the Lenders' Multicurrency Commitments as of the Second Amendment
#: Effective Date is $ 550,000,000", and KKR's single total.
_TRANCHE_TOTAL = re.compile(
    r"The\s+aggregate\s+(?:principal\s+)?amount\s+of\s+(?:the\s+)?"
    r"(?:(?:Lenders['’]?|Lender['’]s)\s+(?P<tranche>(?:[A-Z][\w\-]*\s+){0,3})"
    r"Commitments|Commitments\s+of\s+all\s+(?:of\s+the\s+)?Lenders)"
    r"\s+(?:as\s+of\s+[^.$]{0,80}?\s+)?(?:is|was|equals|shall\s+be)\s+(?P<amount>"
    + _MONEY + r")"
)


#: A size definition that hands the size to another one. The GBDC facility's
#: "Facility Amount" is, until the Facility Termination Date, "an amount equal
#: to the Maximum Facility Amount (as such amount may be reduced ...)".
_SIZE_REFERENCE = re.compile(
    r"^[\s,]*(?:\(\s*(?:a|i)\s*\)\s*(?:on\s+or\s+)?(?:prior\s+to|before|during)\s+"
    r"[^,]{0,80},\s*)?an\s+amount\s+equal\s+to\s+the\s+(?P<term>(?:[A-Z][\w\-]*\s+){0,3}"
    r"(?:Facility\s+Amount|Commitment|Commitments|Financing\s+Limit))\b")
#: A size definition that is the figure, revised only downward or by the
#: agreement's own mechanics: the GBDC facility's "Maximum Facility Amount"
#: means "$750,000,000 (as such amount may be reduced pursuant to Section
#: 2.07)".
_SIZE_STATED = re.compile(
    r"^[\s,:]*(" + _MONEY + r")\s*\(\s*as\s+such\s+amount\s+may\s+be\s+"
    r"(?:reduced|decreased|adjusted)")


def _size_from(node: Any) -> tuple[Any, str] | None:
    body = _flat(node.body)
    for shape in _SIZE_SHAPES + (_SIZE_STATED,):
        match = shape.search(body)
        if match is None:
            continue
        written = match.group(1)
        value = parse_money(re.sub(r"\s+", "", written))
        if value is not None:
            return value, written
    return None


def commitment_candidates(doc: NormalizedDocument, graph: Any) -> list[Any]:
    if not facility_revolves(doc, graph):
        return []
    name, node = _node(graph, *_SIZE_TERMS)
    if node is not None:
        found = _size_from(node)
        via = ""
        if found is None:
            ref = _SIZE_REFERENCE.match(_flat(node.body))
            target = _resolve(graph, ref.group("term")) if ref else None
            if target is not None and target != name:
                found = _size_from(graph.get(target))
                via, name, node = f" (by way of {name!r})", target, graph.get(target)
        if found is not None:
            value, written = found
            return [_candidate(
                "revolver.commitment", value, node.span,
                f"read from the definition of {name!r}{via}, which sizes the "
                "facility; a figure it may be increased to is not the size",
                as_written=written,
            )]
    return _tranche_totals(doc)


def _tranche_totals(doc: NormalizedDocument) -> list[Any]:
    """The facility's stated total, or its largest revolving tranche's.

    Never a sum: a sum is a number that appears nowhere in the agreement, and
    the labelling guide says "largest by commitment" for a deal the flat
    registry cannot hold. A term or incremental tranche is not a revolver.
    """
    totals: list[tuple[Decimal, str, re.Match]] = []
    tranches: list[tuple[Decimal, str, re.Match]] = []
    for match in _TRANCHE_TOTAL.finditer(doc.text):
        tranche = _flat(match.group("tranche") or "")
        if re.search(r"\b(?:Term|Incremental|Swingline|Swing|Delayed|Increase)",
                     tranche):
            continue
        value = parse_money(re.sub(r"\s+", "", match.group("amount")))
        if value is None or value <= 0:
            continue
        bucket = totals if tranche in ("", "Revolving", "Revolving Credit") else tranches
        bucket.append((value, tranche, match))
    if totals and _commitments_include_term(doc):
        totals = []
    pool = totals or tranches
    if not pool:
        return []
    values = {value for value, _, _ in pool}
    if totals and len(values) > 1:
        return []          # two different stated totals settle nothing
    if not totals:
        stated = _stated_sum(doc, tranches)
        if stated is not None:
            return [stated]
    value, tranche, match = max(pool, key=lambda item: item[0])
    span = doc.span(match.start(), match.end())
    if totals:
        note = "the agreement states the aggregate of the Lenders' Commitments"
    else:
        others = ", ".join(sorted({f"{t} {v:,}" for v, t, _ in pool if t != tranche}))
        note = (f"the largest revolving tranche, the {tranche} Commitments; "
                f"the others ({others}) are not added, because a sum appears "
                "nowhere in the agreement")
    return [_candidate("revolver.commitment", value, span, note,
                       as_written=match.group("amount"))]


def _commitments_include_term(doc: NormalizedDocument) -> bool:
    """Whether "Commitments" covers term commitments too, as Blue Owl
    Technology's does ("collectively, the Term Commitments and the Revolving
    Commitments"); then an untranched total of them is not the revolver's."""
    match = re.search(r'["“]\s*Commitments?\s*["”]\s*(?:means|:)([^"“]{0,400})',
                      doc.text)
    return bool(match and re.search(r"\bTerm\b", match.group(1)))


def _stated_sum(doc: NormalizedDocument, tranches: list) -> Any | None:
    """The tranches' total, where the agreement itself states it in a Total row.

    Fidelity states each tranche in its definition and then, in the restated
    Schedule I, "Total | $550,000,000 | $50,000,000 | $600,000,000". That is
    the aggregate written as one figure, which the guide prefers to the
    largest tranche. The sum is only ever a key for finding the row: a total
    the agreement does not print is not reported.
    """
    by_tranche: dict[str, Decimal] = {}
    for value, tranche, _ in tranches:
        by_tranche.setdefault(tranche, value)
    if len(by_tranche) < 2:
        return None
    total = sum(by_tranche.values())
    figure = f"{total:,.0f}"
    pattern = re.compile(r"\bTotal\b[^\n$]{0,40}(?:\$\s*[\d,]+\s*\|?\s*|[-–]\s*\|?\s*){0,6}?"
                         r"\$\s*" + re.escape(figure).replace(",", r"\s*,\s*") + r"(?![\d,])")
    match = pattern.search(doc.text)
    if match is None:
        return None
    parts = " + ".join(f"{t} {v:,.0f}" for t, v in by_tranche.items())
    return _candidate(
        "revolver.commitment", Decimal(total), doc.span(match.start(), match.end()),
        f"the agreement states the aggregate of its revolving tranches ({parts}) "
        "as one figure, in a Total row",
        as_written=f"${figure}",
    )


# ---------------------------------------------------------------------------
# revolver.maturity_date -- computed over the definition graph
# ---------------------------------------------------------------------------

#: Which defined term is the maturity, most specific first. The first one the
#: agreement defines decides, resolved or not: Eagle Point's Maturity Date is
#: three years after an undated event, and falling back to its Termination
#: Date would report the end of the revolving period as the maturity.
#: "Termination Date" is PennantPark's and 5C's maturity and only reached when
#: the agreement has no Maturity Date. "Commitment Termination Date" and the
#: ends of the revolving, reinvestment and funding periods are never reached.
_MATURITY_TERMS = (
    "Final Maturity Date", "Facility Maturity Date",
    "Revolving Credit Maturity Date", "Revolving Maturity Date",
    "Maturity Date", "Stated Maturity Date", "Scheduled Maturity Date",
    "Termination Date", "Facility Termination Date",
)

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
          "twelve": 12, "eighteen": 18, "twenty-four": 24, "thirty-six": 36,
          "forty-eight": 48, "sixty": 60}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4,
             "5th": 5, "6th": 6, "7th": 7}
_NUM = (r"(?:(?P<w>[a-z]+(?:-[a-z]+)?)\s*\(\s*(?P<d>\d+)\s*\)|(?P<n>\d+)"
        r"|(?P<w2>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
        r"|eighteen|twenty-four|thirty-six|forty-eight|sixty))")
_LIMB = re.compile(r"(?<![A-Za-z0-9.])\(\s*(?:[a-h]|[ivx]{1,4}|[xyz])\s*\)\s*")
_EARLIEST = re.compile(
    r"(?:the\s+)?(?:earliest|earlier)(?:\s+to\s+occur)?\s+of\s*:?\s*", re.I)
_AFTER = re.compile(
    r"(?:the\s+)?date\s+(?:that|which)\s+is\s+(?:the\s+)?" + _NUM
    + r"[\s-]+(?P<unit>years?|months?|days?)\s+(?:after|following|from)\s+"
    r"(?P<rest>.*)", re.I)
_ANNIVERSARY = re.compile(
    r"(?:the\s+)?(?:date\s+(?:that|which)\s+is\s+the\s+)?" + _NUM
    + r"[\s-]+(?P<unit>year|month)[\s-]+anniversary\s+(?:of|following|after)\s+"
    r"(?P<rest>.*)", re.I)
_ORDINAL_ANNIVERSARY = re.compile(
    r"(?:the\s+)?(?:date\s+(?:that|which)\s+is\s+the\s+)?(?P<ord>first|second|"
    r"third|fourth|fifth|sixth|seventh|\d(?:st|nd|rd|th))\s+anniversary\s+"
    r"(?:of|following|after)\s+(?P<rest>.*)", re.I)
_BUSINESS_DAY = re.compile(
    r"(?:the\s+)?first\s+Business\s+Day\s+(?P<on>on\s+or\s+)?(?:after|"
    r"following)\s+(?P<rest>.*)", re.I)
_LAST_DAY = re.compile(r"(?:the\s+)?last\s+day\s+of\s+(?P<rest>.*)", re.I)
_PERIOD_END = re.compile(
    r"(?P<how>ending\s+on|to\s+and\s+including|through\s+and\s+including|"
    r"to\s+but\s+excluding|through|until)\s+(?P<rest>.*)", re.I)
_THIS_AGREEMENT = re.compile(r"(?:the\s+)?(?:date\s+of\s+this\s+Agreement|"
                             r"date\s+hereof)\b", re.I)


def _number(match: re.Match) -> int | None:
    if match.group("d"):
        return int(match.group("d"))
    if match.group("n"):
        return int(match.group("n"))
    return _WORDS.get((match.group("w") or match.group("w2") or "").lower())


def _shift(date: dt.date, n: int, unit: str) -> dt.date:
    unit = unit.lower().rstrip("s")
    if unit == "day":
        return date + dt.timedelta(days=n)
    months = n * 12 if unit == "year" else n
    year, month = divmod(date.month - 1 + months, 12)
    year, month, day = date.year + year, month + 1, date.day
    while True:
        try:
            return dt.date(year, month, day)
        except ValueError:          # 29 February plus a year
            day -= 1


def _roll(date: dt.date, strictly: bool) -> dt.date:
    """The next weekday on or after ``date`` (holidays are not modelled)."""
    if strictly:
        date += dt.timedelta(days=1)
    while date.weekday() >= 5:
        date += dt.timedelta(days=1)
    return date


class _Chain:
    """Resolve a date-valued defined term to a date, or decline.

    The grammar is the handful of forms fund facilities build maturities
    from: a date; another defined term; "the earliest of" limbs, of which the
    dated ones are minimised and the events ignored; "the date that is N years
    after", "the N-year anniversary of"; "the first Business Day on or after";
    "the last day of" a defined period; and "the date of this Agreement",
    which is the cover date. Anything else in a link -- an event, a date in
    another document, a self-reference -- is not a date, and a chain with no
    dated route through it resolves to nothing.
    """

    def __init__(self, doc: NormalizedDocument, graph: Any) -> None:
        self.doc = doc
        self.graph = graph
        self.names = sorted(graph.nodes, key=len, reverse=True)
        self.links: list[tuple[str, Span]] = []
        self.arithmetic = False

    def term_at(self, text: str) -> str | None:
        t = re.sub(r"^\s*(?:the|such)\s+", "", text)
        for name in self.names:
            if t[:len(name)].casefold() == name.casefold():
                after = t[len(name):len(name) + 1]
                if not after or not after.isalnum():
                    return name
        return None

    def cover_date(self) -> dt.date | None:
        """The one "dated as of" date on the cover, or None if it is not one."""
        found = {
            _date(m.group(1))
            for m in re.finditer(r"dated\s+as\s+of\s+(" + _DATE + r")",
                                 self.doc.text[:6000])
        }
        found.discard(None)
        return next(iter(found)) if len(found) == 1 else None

    def term(self, name: str, stack: tuple[str, ...]) -> dt.date | None:
        if name in stack or len(stack) > 8:
            return None
        node = self.graph.get(name)
        if node is None:
            return None
        body = re.sub(r"^[\s,:]+", "", _flat(node.body))
        found = self.expr(body, stack + (name,))
        if found is not None:
            self.links.append((name, node.span))
        return found

    def expr(self, text: str, stack: tuple[str, ...]) -> dt.date | None:
        text = re.sub(r"^(?:means|shall mean)\s+", "", text.strip())
        stated = _date(text)
        if stated is not None:
            return stated
        if _THIS_AGREEMENT.match(text):
            return self.cover_date()
        match = _EARLIEST.match(text)
        if match:
            return self.earliest(text[match.end():], stack)
        match = _BUSINESS_DAY.match(text)
        if match:
            base = self.expr(match.group("rest"), stack)
            return None if base is None else _roll(base, strictly=not match.group("on"))
        for pattern in (_AFTER, _ANNIVERSARY):
            match = pattern.match(text)
            if match:
                n, base = _number(match), self.expr(match.group("rest"), stack)
                if n is None or base is None:
                    return None
                self.arithmetic = True
                return _shift(base, n, match.group("unit"))
        match = _ORDINAL_ANNIVERSARY.match(text)
        if match:
            base = self.expr(match.group("rest"), stack)
            if base is None:
                return None
            self.arithmetic = True
            return _shift(base, _ORDINALS[match.group("ord").lower()], "year")
        match = _LAST_DAY.match(text)
        if match:
            name = self.term_at(match.group("rest"))
            return None if name is None else self.period_end(name, stack)
        name = self.term_at(text)
        if name is not None:
            return self.term(name, stack)
        return None

    def earliest(self, text: str, stack: tuple[str, ...]) -> dt.date | None:
        marks = list(_LIMB.finditer(text))
        if len(marks) < 2:
            return None
        dates = []
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            limb = re.sub(r"[\s,;]+(?:and|or)?[\s,;]*$", "", text[mark.end():end])
            found = self.expr(limb, stack)
            if found is not None:
                dates.append(found)
        return min(dates) if dates else None

    def period_end(self, name: str, stack: tuple[str, ...]) -> dt.date | None:
        node = self.graph.get(name)
        if node is None or name in stack:
            return None
        match = _PERIOD_END.search(_flat(node.body))
        if match is None:
            return None
        found = self.expr(match.group("rest"), stack + (name,))
        if found is None:
            return None
        self.links.append((name, node.span))
        if match.group("how").lower().startswith("to but"):
            found -= dt.timedelta(days=1)
        return found


def maturity_candidates(doc: NormalizedDocument, graph: Any) -> list[Any]:
    if not facility_revolves(doc, graph):
        return []
    name, node = _node(graph, *_MATURITY_TERMS)
    if node is None:
        return []
    chain = _Chain(doc, graph)
    value = chain.term(name, ())
    if value is None:
        return []
    links = list(dict.fromkeys(term for term, _ in chain.links))
    route = " <- ".join(reversed(links))
    if chain.arithmetic:
        return [_candidate(
            "revolver.maturity_date", value, node.span,
            f"computed over the definitions: {route}. No sentence states this "
            "date; every link in the chain resolved to one",
            qualifiers={"derived": route},
        )]
    # A pure reference chain ends at a definition that states the date. Cite
    # that one: it is where the date is written, and the only place a reader
    # of the cited text can see it.
    leaf_name, leaf_span = chain.links[0]
    return [_candidate(
        "revolver.maturity_date", value, leaf_span,
        f"the maturity is the {name!r}, which resolves to the {leaf_name!r} "
        f"stated here ({route})",
    )]


# ---------------------------------------------------------------------------
# applicable_margin.eurodollar_top_level_pct
# ---------------------------------------------------------------------------

#: StepStone's is "Spread", KKR's "Margin", PennantPark's, 5C's, BlackRock's
#: and Ares CP Funding's "Applicable Spread".
_MARGIN_TERMS = ("Applicable Margin", "Applicable Spread", "Applicable Rate",
                 "Margin", "Spread", "Interest Margin")

#: A margin definition about the loans the borrower holds rather than the
#: loans it owes. Eagle Point defines "Spread" as "the cash interest spread ...
#: of such Floating Rate Loan over the Term SOFR Rate" -- a collateral
#: obligation's coupon, beside the right answer and in the right form.
_PORTFOLIO = re.compile(
    r"Floating Rate Loan|Collateral|Eligible|Obligor|Related Documents|"
    r"Loan Asset|Underlying|Portfolio", re.I)
_BASE = re.compile(
    r"\b(?:ABR|Alternate Base Rate|Base Rate|Prime Rate|Reference Rate|"
    r"Federal Funds)\b")
_BENCHMARK = re.compile(
    r"\b(?:SOFR|Term Benchmark|Benchmark|Index Rate|Term Rate|LIBOR|LIBO|"
    r"EURIBOR|Eurodollar|RFR|CDOR|CORRA|SONIA|BBSY)\b")
#: The default increment, and not "so long as no Event of Default".
_DEFAULT = re.compile(
    r"\b(?:an|any)\s+Event\s+of\s+Default\s+(?:has|shall\s+have)\s+occurred|"
    r"\b(?:an|any)\s+Event\s+of\s+Default\s+(?:is|shall\s+be)\s+continuing|"
    r"(?:during|after)\s+the\s+(?:occurrence|continuance|existence|"
    r"continuation)\b[^;]{0,60}?Event\s+of\s+Default|"
    r"at\s+any\s+time\s+an\s+Event\s+of\s+Default|"
    r"Default\s+Rate|post-default|Facility\s+Termination\s+Event|"
    r"after\s+the\s+occurrence\s+of\s+the\s+(?:Facility\s+)?Maturity\s+Date", re.I)
#: A rate this agreement's own amendment has already replaced. Ares CP
#: Funding's spread was "(i) prior to the Eighteenth Amendment Effective Date,
#: 2.00% per annum and (ii) thereafter, 1.80% per annum", and the operative
#: text is the eighteenth amendment's.
_SUPERSEDED = re.compile(
    r"prior\s+to\s+the\s+(?:[A-Z][a-z]+\s+)?(?:Amendment|Restatement)"
    r"(?:\s+No\.\s*\d+)?(?:\s+Effective)?\s+Date", re.I)
#: An increment the reader would have to add, which it does not: KKR's margin
#: is "increased by one-half percent (0.50%)" while an LTV test is failed.
_INCREMENT = re.compile(r"increased\s+by|plus\s+an\s+additional|step[\s-]?up",
                        re.I)
#: A schedule that sends part of its pricing to a table the definition does
#: not carry. Latham prices "(b) any Initial Revolving Loan, the rate per
#: annum ... in the table set forth below", and the table is not in the
#: filing; the only rate left in the prose is the term loans'.
_TABLE = re.compile(r"\btable\b|\bPricing\s+Grid\b|\bgrid\b", re.I)
#: A term-loan tranche priced in the same definition as a revolver. Latham's
#: "(a) any Initial Loan, (x) 4.00% per annum in the case of Term SOFR Rate
#: Loans" is the term loans' flat rate: real, plainly stated, and not the
#: revolver's. On a term-only agreement the term loans' margin is the answer --
#: Health Catalyst's "for the Initial Loans and the Delayed Draw Loans, (i) with
#: respect to SOFR Loans, 6.50% per annum" -- so it declines only beside one.
_TERM_TRANCHE = re.compile(
    r"\b(?:Initial\s+(?:Term\s+)?Loans?|Term(?:\s+[A-Z])?\s+Loans?|"
    r"Tranche\s+[A-Z]\b|Incremental\s+Term|Delayed\s+Draw)")
#: A rate, not preceded by a digit or a point: a fused blackline percentage
#: ("2.002.25%") must not yield its tail.
_RATE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
#: A percentage that is a threshold, not a rate: "twenty percent (20%) of the
#: rentable units", "thirty-five percent (35.00%) multiplied by the Facility
#: Amount", "≤50%".
_THRESHOLD_AFTER = re.compile(r"\s*\)?\s*(?:of\b|multiplied\s+by|times\b)", re.I)
_THRESHOLD_BEFORE = re.compile(
    r"(?:[≤≥<>]|greater\s+than|less\s+than|more\s+than|exceeds?|at\s+least|"
    r"equal\s+to\s+or\s+(?:greater|less)\s+than)\s*(?:or\s+equal\s+to\s*)?"
    r"(?:[a-z\-]+\s+(?:percent\s*)?)?\(?\s*$", re.I)


#: A marker that cites a limb rather than opening one: SLR's default proviso
#: increases "the Applicable Margin determined pursuant to the foregoing clause
#: (i) or clause (ii)", and read as limbs those two would shed the "Event of
#: Default" that governs them.
_CITATION = re.compile(r"(?:clauses?|Sections?|paragraphs?|subsections?)\s*$", re.I)


def _markers(body: str) -> list[re.Match]:
    marks: list[re.Match] = []
    cited = False
    for mark in _LIMB.finditer(body):
        before = body[max(0, mark.start() - 20):mark.start()]
        if _CITATION.search(before):
            cited = True
            continue
        if cited and re.search(r"(?:and|or|through|to)\s*$", before):
            continue                    # "clauses (b) and (c)"
        cited = False
        marks.append(mark)
    return marks


def _limbs(body: str) -> list[tuple[str, str]]:
    """Split a schedule into (context, text) limbs.

    The context is every enclosing limb's head -- the text between its marker
    and its first child -- so 5C's "(b) with respect to any Advance bearing
    interest at the Base Rate, (i) ... 0.83%" carries "Base Rate" down to the
    rate it governs. The text before the first marker heads them all, which
    is how a proviso's "after the occurrence ... of an Event of Default"
    reaches its own limbs. Markers are lettered, roman or x/y/z, in that
    nesting.
    """
    marks = _markers(body)
    head = body[:marks[0].start()] if marks else body
    out = [("", head)]
    stack: list[tuple[int, str]] = [(0, head)]
    for index, mark in enumerate(marks):
        token = mark.group(0).strip(" ()")
        level = 3 if token in ("x", "y", "z") else (2 if re.fullmatch(r"[ivx]+", token) else 1)
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        text = body[mark.start():end]
        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()
        context = " ".join(h for _, h in stack)
        out.append((context, text))
        stack.append((level, text))
    return out


def _rates(text: str) -> list[tuple[Decimal, str]]:
    found = []
    for match in _RATE.finditer(text):
        if _THRESHOLD_AFTER.match(text, match.end()):
            continue
        if _THRESHOLD_BEFORE.search(text[max(0, match.start() - 40):match.start()]):
            continue
        found.append((Decimal(match.group(1)), match.group(0)))
    return found


_PROVISO = re.compile(
    r"[;,]?\s*provided(?:\s*,\s*however)?(?:\s*,\s*further)?\s*,?\s*that\b")


def _split_provisos(body: str) -> list[str]:
    """The schedule, then each proviso, split only outside parentheses.

    5C writes its default election "(provided that in the case of any Event
    of Default described in Section 9.1(h) such election shall be automatic
    ...)" inside the default limb itself; split there, the 3.83% default rate
    lost the condition that makes it one and came back as the top margin.
    """
    parts, start = [], 0
    cuts = []
    for match in _PROVISO.finditer(body):
        depth = body.count("(", 0, match.start()) - body.count(")", 0, match.start())
        if depth <= 0:
            cuts.append(match)
    for match in cuts:
        parts.append(body[start:match.start()])
        start = match.end()
    parts.append(body[start:])
    return parts


def _benchmark_rates(body: str) -> tuple[list[tuple[Decimal, str]], bool]:
    """The benchmark-loan rates in a margin schedule, and whether it declined.

    Base-rate, default and superseded limbs are set aside; any other increment
    makes the schedule one this reader does not settle.
    """
    kept: list[tuple[Decimal, str]] = []
    for part in _split_provisos(body):
        for context, text in _limbs(part):
            scope = f"{context} {text}"
            rates = _rates(text)
            if not rates:
                continue
            if _DEFAULT.search(scope) or _SUPERSEDED.search(text):
                continue
            if _INCREMENT.search(text):
                return [], True
            if _BASE.search(scope) and not _BENCHMARK.search(scope):
                continue
            if _BASE.search(text) and _BENCHMARK.search(text):
                # Both kinds in one limb: KKR's "3.00% per annum in the case
                # of Term Rate Loans ... and 2.00% per annum in the case of
                # Base Rate Loans". Attribute each rate to the loan type
                # written nearest after it.
                for value, written in rates:
                    at = text.find(written)
                    after = text[at:at + 120]
                    base_at = _BASE.search(after)
                    bench_at = _BENCHMARK.search(after)
                    if bench_at and (not base_at or bench_at.start() < base_at.start()):
                        kept.append((value, written))
                    elif not base_at and not bench_at:
                        return [], True
                continue
            kept.extend(rates)
    return kept, False


def margin_candidates(doc: NormalizedDocument, graph: Any) -> list[Any]:
    pointed = _defined_in_letter(
        doc, "applicable_margin.eurodollar_top_level_pct",
        ("Applicable Margin", "Applicable Spread"))
    if pointed:
        return pointed
    name, node = _node(graph, *_MARGIN_TERMS)
    if node is None:
        return []
    body = _flat(node.body)
    pointer = _IN_FEE_LETTER.match(body)
    if pointer and len(body) < 200:
        # PIMCO's "Applicable Margin" is "specified in the Fee Letter", and
        # the GBDC facility's is "assigned to such term in the Lender Fee
        # Letter": a term of the deal this agreement does not carry.
        return [_candidate(
            "applicable_margin.eurodollar_top_level_pct", None, node.span,
            f"the definition of {name!r} gives the margin by reference to the "
            f"{pointer.group(1)}, which is not part of this agreement",
            external=pointer.group(1),
        )]
    if _PORTFOLIO.search(body) or len(body) > 2500:
        return []
    if _TABLE.search(body):
        return []
    if _TERM_TRANCHE.search(body) and (re.search(r"\bRevolv", body)
                                       or revolving_evidence(doc, graph)):
        return []
    rates, declined = _benchmark_rates(body)
    if declined or not rates:
        return []
    top_value, top_written = max(rates, key=lambda r: r[0])
    levels = ", ".join(dict.fromkeys(w for _, w in rates))
    return [_candidate(
        "applicable_margin.eurodollar_top_level_pct", top_value, node.span,
        f"the top level of the {name!r} schedule ({levels}); the base-rate "
        "margin, the default increment and any rate an amendment superseded "
        "are set aside",
        as_written=top_written,
    )]


# ---------------------------------------------------------------------------
# commitment_fee_pct
# ---------------------------------------------------------------------------

#: The unused fee's names. A "Commitment Fee" qualifies only if its body is
#: about the undrawn amount: BlackRock Monticello's is "the product of the
#: Maximum Facility Amount and 0.25%", paid at closing and with each Advance,
#: which is an upfront fee with the unused fee's rate.
_FEE_TERMS = (
    "Unused Rate", "Unused Fee Rate", "Unused Commitment Fee Rate",
    "Non-Usage Fee Rate", "Non-Utilization Fee Rate", "Undrawn Fee Rate",
    "Commitment Fee Rate", "Unused Fee", "Unused Commitment Fee",
    "Non-Usage Fee", "Non-Utilization Fee", "Undrawn Fee", "Commitment Fee",
)
_UNDRAWN = re.compile(
    r"unused|undrawn|unutilized|unfunded|minus\s+the\s+(?:aggregate\s+)?"
    r"(?:Advances|Loans)\s+Outstanding|excess\s+of", re.I)


def fee_candidates(doc: NormalizedDocument, graph: Any) -> list[Any]:
    for term in _FEE_TERMS:
        name = _resolve(graph, term)
        if name is None:
            continue
        node = graph.get(name)
        body = _flat(node.body)
        pointer = _IN_FEE_LETTER.match(body)
        if pointer and len(body) < 200:
            return [_candidate(
                "commitment_fee_pct", None, node.span,
                f"the definition of {name!r} sets the rate by reference to the "
                f"{pointer.group(1)}, which is not part of this agreement",
                external=pointer.group(1),
            )]
        if name.startswith("Commitment Fee") and not _UNDRAWN.search(body):
            continue
        if len(body) > 4000:
            # PennantPark's tiered Non-Usage Fee runs to 2,696 characters.
            continue
        rates = [(v, w) for v, w in _rates(body) if v <= Decimal("3")]
        if not rates:
            continue
        top_value, top_written = max(rates, key=lambda r: r[0])
        levels = ", ".join(dict.fromkeys(w for _, w in rates))
        return [_candidate(
            "commitment_fee_pct", top_value, node.span,
            f"the highest rate the {name!r} provision charges on the undrawn "
            f"amount ({levels})",
            as_written=top_written,
        )]
    pointed = _defined_in_letter(doc, "commitment_fee_pct", _FEE_TERMS)
    if pointed:
        return pointed
    match = _PAID_UNDER_LETTER.search(doc.text)
    if match is not None:
        return [_candidate(
            "commitment_fee_pct", None, doc.span(match.start(), match.end()),
            f"the unused fee is payable in the amounts set forth in the "
            f"{match.group('doc')}, which is not part of this agreement",
            external=match.group("doc"),
        )]
    return []


#: StepStone pays "non-utilization fees (the " Commitment Fees ") in the
#: amounts set forth in the Commitment Fee Letter": the operative clause,
#: where the agreement defines no fee term at all.
_PAID_UNDER_LETTER = re.compile(
    r"\b(?:non-?utilization|non-?usage|unused|undrawn|unutilized|commitment)\s+"
    r"fees?\s*(?:\([^)]{0,60}\))?\s+in\s+the\s+amounts?\s+(?:and\s+on\s+the\s+"
    r"dates\s+)?set\s+forth\s+in\s+(?:the\s+)?" + _LETTER, re.I)


# ---------------------------------------------------------------------------
# libor_floor_pct
# ---------------------------------------------------------------------------

#: KKR's "Floor" means "zero (0)": no percent sign, so the definitions tier,
#: which reads percentages, found no value in it.
_ZERO_WORDS = re.compile(
    r"^[\s,:]*(?:a\s+rate\s+(?:of\s+interest\s+)?equal\s+to\s+)?zero"
    r"(?:\s+percent)?\s*(?:\(\s*0(?:\.0+)?\s*%?\s*\))?\s*(?:per\s+annum)?\s*\.",
    re.I)
#: A floor written into the benchmark's own definition: BlackRock
#: Monticello's June facility defines Term SOFR as "the greater of (x) three
#: percent (3.00%) per annum, or (y) the Term SOFR Reference Rate".
_GREATER_OF_FLOOR = re.compile(
    r"greater\s+of\s*\(\s*(?:x|a|i|1)\s*\)\s*(?:[a-z\-]+(?:\s+[a-z\-]+)*\s+"
    r"percent\s*)?\(?\s*(\d+(?:\.\d+)?\s*%)\s*\)?\s*(?:per\s+annum)?\s*,?\s*"
    r"(?:or|and)\s*\(\s*(?:y|b|ii|2)\s*\)\s*(?:the\s+)?(?:Term\s+SOFR|SOFR|"
    r"Daily\s+Simple\s+SOFR|Benchmark|LIBO)", re.I)
#: StepStone: "Adjusted Term SOFR shall at no time be less than 0.0% per annum."
_NOT_LESS_THAN_FLOOR = re.compile(
    r"shall\s+(?:at\s+no\s+time|not\s+at\s+any\s+time|in\s+no\s+event|not)\s+"
    r"be\s+less\s+than\s+(\d+(?:\.\d+)?\s*%)", re.I)
_BENCHMARK_TERMS = ("Term SOFR", "Adjusted Term SOFR", "Term SOFR Rate",
                    "Daily Simple SOFR", "Adjusted Daily Simple SOFR", "SOFR",
                    "Benchmark", "LIBO Rate", "Adjusted LIBO Rate")


def floor_candidates(doc: NormalizedDocument, graph: Any) -> list[Any]:
    name = _resolve(graph, "Floor")
    if name is not None:
        node = graph.get(name)
        if _ZERO_WORDS.match(_flat(node.body)):
            return [_candidate(
                "libor_floor_pct", Decimal("0"), node.span,
                "the definition of 'Floor' writes zero in words, with no unit",
                as_written=_flat(node.body)[:40],
            )]
        return []          # the definitions tier reads a Floor with a percentage
    for term in _BENCHMARK_TERMS:
        resolved = _resolve(graph, term)
        if resolved is None:
            continue
        node = graph.get(resolved)
        body = _flat(node.body)
        match = _GREATER_OF_FLOOR.search(body) or _NOT_LESS_THAN_FLOOR.search(body)
        if match is None:
            continue
        value = parse_percent(match.group(1))
        if value is None:
            continue
        return [_candidate(
            "libor_floor_pct", value, node.span,
            f"{resolved!r} is defined so that it is never less than "
            f"{match.group(1)}, which is a floor on the benchmark",
            as_written=match.group(1),
        )]
    return []


# ---------------------------------------------------------------------------


_READERS = {
    "revolver.commitment": commitment_candidates,
    "revolver.maturity_date": maturity_candidates,
    "applicable_margin.eurodollar_top_level_pct": margin_candidates,
    "commitment_fee_pct": fee_candidates,
    "libor_floor_pct": floor_candidates,
}


def economic_candidates(
    doc: NormalizedDocument, graph: Any, specs: list[FieldSpec] | None = None,
) -> list[Any]:
    """Every economic-term candidate these readers can settle."""
    if graph is None:
        return []
    wanted = {spec.name for spec in specs} if specs else set(_READERS)
    out: list[Any] = []
    for field, reader in _READERS.items():
        if field in wanted:
            out.extend(reader(doc, graph))
    return out
