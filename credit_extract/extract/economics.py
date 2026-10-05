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
import json
import re
from decimal import Decimal
from typing import Any

from ..ingest.normalize import NormalizedDocument
from ..ingest.tables import parse_money, parse_percent
from ..models.core import Span
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from ..models.quantities import quantity_for
from .definitions import _SINCE_CHANGED

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
#: Easterly Government Properties' term facility, priced like a revolver and
#: never calling itself a term loan, says only "The Borrower shall not have the
#: right to reborrow any portion of the Advances that is repaid or prepaid".
_NO_REBORROW = re.compile(
    r"(?:may|shall)\s+not\s+be\s+re-?borrowed|"
    r"(?:shall|will)\s+not\s+have\s+the\s+right\s+to\s+re-?borrow|"
    r"(?:may|shall)\s+not\s+re-?borrow", re.I)
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
#: The investment-grade forms say the same without the Lenders: Enterprise
#: Products' "The initial aggregate amount of the Lenders' Commitments as of
#: the Effective Date is $1,000,000,000", Cencora's "The aggregate amount of
#: the Commitments as of the Restatement Effective Date is US$7,000,000,000",
#: Latham's "The aggregate amount of the Initial Revolving Credit Commitments
#: as of the Closing Date is $75,000,000".
_TRANCHE_TOTAL = re.compile(
    r"The\s+(?:initial\s+)?aggregate\s+(?:principal\s+)?amount\s+of\s+(?:the\s+)?"
    r"(?:(?:(?:Lenders['’]?|Lender['’]s)\s+)?(?P<tranche>(?:[A-Z][\w\-]*\s+){0,3})"
    r"Commitments|Commitments\s+of\s+all\s+(?:of\s+the\s+)?Lenders)"
    r"\s+(?:as\s+of\s+(?P<asof>[^.$]{0,80}?)\s+)?(?:is|was|equals|shall\s+be)\s+"
    r"(?:US)?(?P<amount>" + _MONEY + r")"
)
#: An amendment, from its title. Its conformed copy keeps statements dated
#: before it: Lafayette Square's Amendment No. 1 carries "The aggregate amount
#: of the Lenders' Multicurrency Commitments as of the Effective Date is
#: $75,000,000.00" through "the increase to the Maximum Commitment" the
#: amendment makes. Blue Owl Technology, Fidelity and KKR date theirs as of
#: the amendment's or the restatement's own date.
_AMENDMENT_TITLE = re.compile(
    r"\bAMENDMENT\s+NO\.|\b[A-Z]+(?:TH|ST|ND|RD)\s+AMENDMENT\s+TO\b|"
    r"\b(?:FIRST|SECOND|THIRD)\s+AMENDMENT\s+TO\b|\bAMENDMENT\s+TO\s+[A-Z ]*"
    r"(?:CREDIT|LOAN)\s+AGREEMENT")
_AMENDMENT_DATE = re.compile(r"Amendment|Restatement", re.I)


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


#: A commitment term of an investment-grade agreement, whose definition
#: states the total on a date: "Aggregate Commitments", "Aggregate Revolving
#: Commitment Amount", "Revolving Credit Facility", "Commitment".
_AGGREGATE_TERM = re.compile(
    r"^(?:Aggregate\s+)?(?:(?:Initial\s+)?Revolving(?:\s+Credit)?\s+)?"
    r"(?:Commitments?|Facility)(?:\s+Amount)?$")
_ON_DATE = r"the\s+(?P<date>[^,.;$]{0,60}?(?:Date|date\s+hereof))"
_DATED_AMOUNT = r"(?:US)?(?P<amount>" + _MONEY + r")"
#: The total, stated on a date. Globe Life's "As of the Effective Date, the
#: Aggregate Commitments are $1,000,000,000"; Apple Hospitality's "On the
#: Restatement Effective Date, the Revolving Credit Facility is
#: $700,000,000"; Artisan's "The Aggregate Commitments on the Closing Date is
#: $150,000,000"; Target's Commitment, "the aggregate amount of which at the
#: Effective Date is $4,000,000,000"; Cooper-Standard's Commitments, "which
#: amount shall be $ 200,000,000 on the Sixth Amendment Effective Date".
_DATED_AGGREGATE = (
    re.compile(r"\b(?:On|As\s+of|At)\s+" + _ON_DATE + r"\s*,\s*the\s+"
               r"(?P<subject>[A-Z][\w\- ]{0,60}?)\s+(?:is|are|equals?|shall\s+be)\s+"
               + _DATED_AMOUNT),
    re.compile(r"\bThe\s+(?P<subject>[A-Z][\w\- ]{0,60}?)\s+(?:on|as\s+of|at)\s+"
               + _ON_DATE + r"\s+(?:is|are|equals?|shall\s+be)\s+" + _DATED_AMOUNT),
    re.compile(r"\baggregate\s+amount\s+of\s+which\s+(?:on|as\s+of|at)\s+" + _ON_DATE
               + r"\s+(?:is|was)\s+" + _DATED_AMOUNT),
    re.compile(r"\bwhich\s+amount\s+shall\s+be\s+" + _DATED_AMOUNT
               + r"\s+(?:on|as\s+of)\s+" + _ON_DATE),
)
#: Not the revolver: a term loan's, an accordion's, a sublimit.
_NOT_REVOLVING = re.compile(
    r"\bTerm\b|Incremental|Increase|Swing|Letter|Sublimit|Delayed|Bridge", re.I)


def _dated_aggregate(doc: NormalizedDocument, graph: Any) -> list[Any]:
    """The total a commitment term's own definition states on a date.

    Investment-grade agreements print the per-lender amounts in a schedule
    that is often not in the filing, and the total in the definition, dated
    to the day the agreement or its restatement took effect. Two different
    totals settle nothing, and a definition that says its figure has since
    changed is not read, as in the definitions tier.
    """
    found: list[tuple[Decimal, str, Any, str]] = []
    for name in graph.nodes:
        if not _AGGREGATE_TERM.match(name):
            continue
        node = graph.get(name)
        body = _flat(node.body)
        if _SINCE_CHANGED.search(body):
            continue
        for shape in _DATED_AGGREGATE:
            match = shape.search(body)
            if match is None:
                continue
            subject = match.groupdict().get("subject") or ""
            if _NOT_REVOLVING.search(subject):
                continue
            value = parse_money(re.sub(r"\s+", "", match.group("amount")))
            if value is not None and value > 0:
                found.append((value, match.group("amount"), node, name))
                break
    if len({value for value, _, _, _ in found}) != 1:
        return []
    value, written, node, name = found[0]
    return [_candidate(
        "revolver.commitment", value, node.span,
        f"the definition of {name!r} states the total on the day the agreement "
        "took effect; an amount it may be increased to is not the size",
        as_written=written,
    )]


#: A commitment schedule: one column of commitments beside the lenders, and
#: never a term loan's, a letter of credit's, a swing line's or an increase.
_SCHEDULE_COMMITMENT_HEAD = re.compile(
    r"^(?:Revolving\s+(?:Credit\s+)?)?Commitments?(?:\s+Amount)?$", re.I)
_SCHEDULE_LENDER_HEAD = re.compile(r"^(?:Name\s+of\s+)?(?:Lenders?|Banks?)$", re.I)
_SCHEDULE_MONEY = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)")


def _schedule_total(doc: NormalizedDocument) -> list[Any]:
    """A commitment schedule's Total row, where the lenders' rows add up to it.

    Cboe and Franklin print their totals nowhere else: Schedule 2.01 reads
    "Lender | Commitment | Applicable Percentage" down to "Total | $ |
    400,000,000.00 | 100.000000000 | %". The rows must sum to the total, so a
    schedule split across pages, or a table that only looks like one, is not
    read; two schedules with different totals settle nothing.
    """
    totals: dict[Decimal, Any] = {}
    for table in doc.tables:
        rows = [[" ".join(c.text.split()) for c in row] for row in table.rows()]
        rows = [[text for text in row if text] for row in rows]
        rows = [row for row in rows if row]
        if len(rows) < 3:
            continue
        head = rows[0]
        # The first amount in each row is read, so the commitment column must
        # be the first after the lender's name: Limbach's schedule goes on to
        # its term and delayed draw columns.
        if not (len(head) >= 2 and _SCHEDULE_LENDER_HEAD.match(head[0])
                and _SCHEDULE_COMMITMENT_HEAD.match(head[1])):
            continue
        amounts, total, total_row = [], None, None
        for index, row in enumerate(rows[1:], start=1):
            money = _SCHEDULE_MONEY.search(" ".join(row).replace("$ ", "$"))
            if money is None:
                continue
            value = parse_money("$" + money.group(1))
            if re.match(r"(?i)total\b", row[0]):
                total, total_row = value, index
                break
            amounts.append(value)
        if total is None or not amounts or abs(sum(amounts) - total) > Decimal("1"):
            continue
        cells = [c for c in table.rows()[total_row] if _SCHEDULE_MONEY.search(
            c.text.replace("$ ", "$")) or re.fullmatch(r"[\d,]+(?:\.\d+)?", c.text.strip())]
        span = doc.span(cells[0].start, cells[0].end) if cells else doc.span(table.start, table.end)
        totals.setdefault(total, span)
    if len(totals) != 1:
        return []
    (total, span), = totals.items()
    return [_candidate(
        "revolver.commitment", total, span,
        "the Total row of the commitment schedule, which the lenders' "
        "commitments above it add up to",
        as_written=f"${total:,}",
    )]


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
    return _dated_aggregate(doc, graph) or _tranche_totals(doc) or _schedule_total(doc)


def _tranche_totals(doc: NormalizedDocument) -> list[Any]:
    """The facility's stated total, or its largest revolving tranche's.

    Never a sum: a sum is a number that appears nowhere in the agreement, and
    the labelling guide says "largest by commitment" for a deal the flat
    registry cannot hold. A term or incremental tranche is not a revolver.
    """
    totals: list[tuple[Decimal, str, re.Match]] = []
    tranches: list[tuple[Decimal, str, re.Match]] = []
    amendment = bool(_AMENDMENT_TITLE.search(doc.text[:3000]))
    for match in _TRANCHE_TOTAL.finditer(doc.text):
        if amendment and not _AMENDMENT_DATE.search(match.group("asof") or ""):
            continue                    # dated before the amendment it sits in
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
                "nowhere in the agreement") if others else (
                f"the {tranche} Commitments, the only revolving tranche whose "
                "total the agreement states")
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
_ORDINAL_WORDS = {n: w for w, n in _ORDINALS.items() if w.isalpha()}
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
#: A definition's own business-day convention: Puget Energy's "May 18, 2031
#: ... ; provided that, in each case, if such date is not a Business Day, the
#: Maturity Date shall be the next preceding Business Day". May 18, 2031 is a
#: Sunday.
_ROLL_PROVISO = re.compile(
    r"if\s+(?:any\s+)?such\s+(?:date|day)\s+is\s+not\s+a\s+Business\s+Day\b"
    r"[^.;]{0,80}?\b(?P<way>preceding|succeeding|following|next\s+Business)", re.I)
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


def _roll(date: dt.date, strictly: bool, back: bool = False) -> dt.date:
    """The next weekday on or after ``date``, or on or before it with ``back``
    (holidays are not modelled)."""
    step = dt.timedelta(days=-1 if back else 1)
    if strictly:
        date += step
    while date.weekday() >= 5:
        date += step
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

    Every definition it resolves is also written down as a premise: what the
    resolver read that definition to say, as a sentence the definition can be
    checked against ("The text defines the Final Maturity Date as the
    earliest of the 4-year anniversary of the Closing Date and the other
    dates and events it lists"). No sentence states a computed date, but each
    premise is stated, so a literal reader can confirm the parse step by step
    while the arithmetic stays here.
    """

    def __init__(self, doc: NormalizedDocument, graph: Any) -> None:
        self.doc = doc
        self.graph = graph
        self.names = sorted(graph.nodes, key=len, reverse=True)
        self.links: list[tuple[str, Span]] = []
        self.arithmetic = False
        #: (span, statement): each definition on the route, as read.
        self.premises: list[tuple[Span, str]] = []

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
        cover = self.cover()
        return None if cover is None else cover[0]

    def cover(self) -> tuple[dt.date, Span] | None:
        """The cover date and where the cover writes it."""
        found: dict[dt.date, re.Match] = {}
        for m in re.finditer(r"dated\s+as\s+of\s+(" + _DATE + r")",
                             self.doc.text[:6000]):
            date = _date(m.group(1))
            if date is not None:
                found.setdefault(date, m)
        if len(found) != 1:
            return None
        date, m = next(iter(found.items()))
        start, end = max(0, m.start() - 300), min(len(self.doc.text), m.end() + 100)
        return date, Span(start=start, end=end, text=self.doc.text[start:end])

    def term(self, name: str, stack: tuple[str, ...]) -> dt.date | None:
        if name in stack or len(stack) > 8:
            return None
        node = self.graph.get(name)
        if node is None:
            return None
        body = re.sub(r"^[\s,:]+", "", _flat(node.body))
        read = self.read(body, stack + (name,))
        if read is None:
            return None
        found, words = read
        roll = _ROLL_PROVISO.search(body)
        if roll is not None:
            back = roll.group("way").lower() == "preceding"
            words = (f"{words}, or the {'preceding' if back else 'next'} Business "
                     f"Day if that date is not a Business Day")
            rolled = _roll(found, strictly=False, back=back)
            if rolled != found:
                # The date the definition comes to is no longer one it writes.
                self.arithmetic = True
                found = rolled
        self.links.append((name, node.span))
        self.premises.append((node.span, f"The text defines the {name} as {words}."))
        return found

    def expr(self, text: str, stack: tuple[str, ...]) -> dt.date | None:
        read = self.read(text, stack)
        return None if read is None else read[0]

    def read(self, text: str, stack: tuple[str, ...]) -> tuple[dt.date, str] | None:
        """The date an expression comes to, and how it was read, in words."""
        text = re.sub(r"^(?:means|shall mean)\s+", "", text.strip())
        stated = _date(text)
        if stated is not None:
            return stated, f"{stated:%B} {stated.day}, {stated.year}"
        if _THIS_AGREEMENT.match(text):
            cover = self.cover()
            if cover is None:
                return None
            date, span = cover
            self.premises.append((
                span, f"This Agreement is dated as of {date:%B} {date.day}, {date.year}."))
            return date, "the date of this Agreement"
        match = _EARLIEST.match(text)
        if match:
            return self.earliest(text[match.end():], stack)
        match = _BUSINESS_DAY.match(text)
        if match:
            base = self.read(match.group("rest"), stack)
            if base is None:
                return None
            how = "on or after" if match.group("on") else "after"
            return (_roll(base[0], strictly=not match.group("on")),
                    f"the first Business Day {how} {base[1]}")
        for pattern in (_AFTER, _ANNIVERSARY):
            match = pattern.match(text)
            if match:
                n, base = _number(match), self.read(match.group("rest"), stack)
                if n is None or base is None:
                    return None
                self.arithmetic = True
                unit = match.group("unit").lower().rstrip("s")
                words = (f"the date {n} {unit}{'' if n == 1 else 's'} after {base[1]}"
                         if pattern is _AFTER else f"the {n}-{unit} anniversary of {base[1]}")
                return _shift(base[0], n, match.group("unit")), words
        match = _ORDINAL_ANNIVERSARY.match(text)
        if match:
            base = self.read(match.group("rest"), stack)
            if base is None:
                return None
            self.arithmetic = True
            n = _ORDINALS[match.group("ord").lower()]
            return (_shift(base[0], n, "year"),
                    f"the {_ORDINAL_WORDS[n]} anniversary of {base[1]}")
        match = _LAST_DAY.match(text)
        if match:
            name = self.term_at(match.group("rest"))
            found = None if name is None else self.period_end(name, stack)
            return None if found is None else (found, f"the last day of the {name}")
        name = self.term_at(text)
        if name is not None:
            found = self.term(name, stack)
            return None if found is None else (found, f"the {name}")
        return None

    def earliest(self, text: str, stack: tuple[str, ...]) -> tuple[dt.date, str] | None:
        marks = list(_LIMB.finditer(text))
        if len(marks) < 2:
            return None
        dated: list[tuple[dt.date, str]] = []
        for index, mark in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            limb = re.sub(r"[\s,;]+(?:and|or)?[\s,;]*$", "", text[mark.end():end])
            found = self.read(limb, stack)
            if found is not None:
                dated.append(found)
        if not dated:
            return None
        words = [w for _, w in dated]
        if len(dated) < len(marks):
            words.append("the other dates and events it lists")
        listed = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
        return min(d for d, _ in dated), f"the earliest of {listed}"

    def period_end(self, name: str, stack: tuple[str, ...]) -> dt.date | None:
        node = self.graph.get(name)
        if node is None or name in stack:
            return None
        match = _PERIOD_END.search(_flat(node.body))
        if match is None:
            return None
        read = self.read(match.group("rest"), stack + (name,))
        if read is None:
            return None
        found, words = read
        self.links.append((name, node.span))
        if match.group("how").lower().startswith("to but"):
            found -= dt.timedelta(days=1)
            words = f"the day before {words}"
        self.premises.append((node.span, f"The text defines the {name} as a period that ends on {words}."))
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
        premises = [[span.start, span.end, statement]
                    for span, statement in dict.fromkeys(chain.premises)]
        return [_candidate(
            "revolver.maturity_date", value, node.span,
            f"computed over the definitions: {route}. No sentence states this "
            "date; every link in the chain resolved to one",
            qualifiers={"derived": route, "premises": json.dumps(premises)},
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


# ---------------------------------------------------------------------------
# pricing grids
# ---------------------------------------------------------------------------

#: A grid's level column, and a level in a body row: "Level I", "1", "IV".
_LEVEL_HEAD = re.compile(r"^\s*(?:Pricing\s+)?(?:Level|Tier|Category|Status)\b", re.I)
_LEVEL_CELL = re.compile(
    r"^\s*(?:(?:Pricing\s+)?(?:Level|Tier|Category|Status)\s+)?(?:[IVX]+|\d{1,2})\.?\s*$", re.I)
#: A column that sorts the rows rather than pricing them.
_CRITERION_HEAD = re.compile(
    r"Rating|Ratio|Leverage|Availability|Usage|Utili[sz]ation|S&P|Moody|Fitch|"
    r"\bDebt\b|Excess|Capacity", re.I)
#: A heading that spans the value columns and names none of them.
_PARENT_HEAD = re.compile(
    r"^\s*(?:Applicable\s+(?:Rate|Margin|Spread|Percentage)|Rates?|Pricing|"
    r"Margins?|Spreads?|Grid)\s*(?:\(.*\))?\s*:?\s*$", re.I)
_BENCH_HEAD = re.compile(
    r"SOFR|Bench-?\s*mark|\bRFR\b|Eurodollar|Euro-?currency|LIBO|\bBSBY\b|"
    r"\bCDOR\b|\bCORRA\b|EURIBOR|SONIA|Term\s+Rate", re.I)
_BASE_HEAD = re.compile(r"Base\s+Rate|\bABR\b|Prime|Alternate\s+Base", re.I)
_FEE_HEAD = re.compile(r"\bFees?\b", re.I)
_UNUSED_HEAD = re.compile(
    r"Commitment\s+Fee|Unused|Non-?Usage|Undrawn|Unutili[sz]ed", re.I)
_VALUE_HEAD = re.compile(r"Loans?|Margin|Spread|Letters?\s+of\s+Credit|Fee", re.I)
_NUMBER_CELL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(%|bps|basis\s+points)?\s*$", re.I)
_BPS = re.compile(r"\bbps\b|basis\s+points", re.I)


def _cells(row: list[Any]) -> list[tuple[str, Any]]:
    return [(" ".join(c.text.split()), c) for c in row]


def read_grid(table: Any) -> list[tuple[str, list[tuple[Decimal, str, Any]]]] | None:
    """A pricing grid's value columns, each with its value in every row.

    Investment-grade grids are printed with their headings over two rows,
    their percent signs in cells of their own, and their columns named for
    the loans they price: Constellation's "Applicable Rate" spans "Term SOFR
    Loans | Base Rate Loans"; KBR's grid reads "Term SOFR Loans; Alternative
    Currency Loans | Base Rate | Performance Letter of Credit | Commitment
    Fee". The level and the ratings or ratio that select a row are set aside,
    and a row whose figures do not line up with the value columns is
    skipped, never guessed into place. None if the table is not a grid.
    """
    rows = [r for r in table.rows() if any(c.text.strip() for c in r)]
    header: list[list[Any]] = []
    body: list[list[tuple[Decimal, str, Any]]] = []
    for row in rows:
        cells = [(text, cell) for text, cell in _cells(row) if text and text != "%"]
        if cells and _LEVEL_CELL.match(cells[0][0]):
            cells = cells[1:]
        figures = []
        for text, cell in cells:
            match = _NUMBER_CELL.match(text)
            if match:
                figures.append((match, cell))
        if len(figures) >= 2 and (body or header):
            body.append([(Decimal(m.group(1)), cell.text.strip(), cell, m.group(2))
                         for m, cell in figures])
        elif not body:
            header.append(row)
    if not header or not body:
        return None
    labels: list[str] = []
    for row in header:
        for text, _ in _cells(row):
            if (not text or _LEVEL_HEAD.match(text) or _PARENT_HEAD.match(text)
                    or (_CRITERION_HEAD.search(text) and not (
                        _BENCH_HEAD.search(text) or _BASE_HEAD.search(text)
                        or _FEE_HEAD.search(text)))):
                continue
            if (_BENCH_HEAD.search(text) or _BASE_HEAD.search(text)
                    or _VALUE_HEAD.search(text)):
                labels.append(text)
    if len(labels) < 2:
        return None
    columns: list[tuple[str, list[tuple[Decimal, str, Any]]]] = [(l, []) for l in labels]
    aligned = 0
    for figures in body:
        if len(figures) != len(labels):
            continue
        aligned += 1
        for (label, values), (value, written, cell, unit) in zip(columns, figures):
            if unit and _BPS.match(unit) or (_BPS.search(label) and value > 10):
                value = value / 100
            values.append((value, written, cell))
    if aligned < max(2, (len(body) + 1) // 2):
        return None
    return columns


def _margin_column(columns: list[tuple[str, list]]) -> tuple[str, list] | None:
    benchmark = [(l, v) for l, v in columns
                 if _BENCH_HEAD.search(l) and not _BASE_HEAD.search(l)]
    if not benchmark:
        return None
    revolving = [(l, v) for l, v in benchmark if re.search(r"Revolv", l, re.I)]
    if revolving:
        return revolving[0]
    untermed = [(l, v) for l, v in benchmark if not re.search(r"Term\s+Loan", l, re.I)]
    return (untermed or benchmark)[0]


def _fee_column(columns: list[tuple[str, list]]) -> tuple[str, list] | None:
    unused = [(l, v) for l, v in columns
              if _UNUSED_HEAD.search(l) and not re.search(r"Facility\s+Fee|Ticking", l, re.I)]
    revolving = [(l, v) for l, v in unused if re.search(r"Revolv", l, re.I)]
    return (revolving or unused or [None])[0]


def _definition_grid(doc: NormalizedDocument, graph: Any
                     ) -> tuple[str, Any, list[tuple[str, list]]] | None:
    """The pricing grid the margin's definition carries or sends its levels to.

    A table inside the definition is its grid however the definition words it:
    Constellation's "the following percentages per annum ... as set forth
    below", KBR's and Sanmina's "the applicable percentage per annum set forth
    below", each with the table between the definition's first words and its
    last. Only a definition that names a table or grid is followed past its
    own end, to a table printed after it. The first grid wins: where a
    definition prices more than one facility, KBR's and Sanmina's put the
    revolver's grid first and a term tranche's flat rate or its own grid after.
    """
    name, node = _node(graph, *_MARGIN_TERMS)
    if node is None:
        return None
    reach = 8000 if _TABLE.search(_flat(node.body)) else 0
    for table in doc.tables_in(node.span.start, node.span.end + reach):
        columns = read_grid(table)
        if columns and (_margin_column(columns) or _fee_column(columns)):
            return name, node, columns
    return None


def _top(doc: NormalizedDocument, field: str, name: str, column: tuple[str, list],
         what: str, aside: str) -> list[Any]:
    label, values = column
    if not values:
        return []
    value, written, cell = max(values, key=lambda v: v[0])
    return [_candidate(
        field, value, doc.span(cell.start, cell.end),
        f"the highest {what} in the pricing grid of {name!r}, column "
        f"{label!r}; {aside}",
        as_written=written,
    )]


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
    if _PORTFOLIO.search(body):
        return []
    grid = _definition_grid(doc, graph)
    column = grid and _margin_column(grid[2])
    if column:
        return _top(doc, "applicable_margin.eurodollar_top_level_pct", grid[0],
                    column, "margin over the benchmark",
                    "the base-rate and fee columns, and any term loans' column, "
                    "are set aside")
    if _TABLE.search(body):
        # The levels are in a table this reader cannot line up, or one the
        # filing does not carry: Latham's is not in it, so there is nothing to
        # read and nothing is.
        return []
    if len(body) > 2500:
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
    # Investment-grade grids price the fee beside the margin: Sanmina's
    # "Commitment Fee" column tops at 0.300% and KBR's at 0.325%, and neither
    # defines a fee rate of its own. A facility fee is charged on the whole
    # commitment and is not this one.
    grid = _definition_grid(doc, graph)
    column = grid and _fee_column(grid[2])
    if column:
        return _top(doc, "commitment_fee_pct", grid[0], column,
                    "fee on unused commitments",
                    "a facility fee would be charged on the whole commitment and "
                    "is not this fee")
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
#: Constellation's Term SOFR: "if the Term SOFR determined in accordance with
#: either of the foregoing provisions ... would otherwise be less than zero,
#: the Term SOFR shall be deemed zero". Only inside the benchmark's own
#: definition: the same proviso sits on the federal funds rate, the base rate
#: and CORRA, and none of those is this floor.
_DEEMED_ZERO = re.compile(
    r"less\s+than\s+(?P<below>zero|0(?:\.0+)?\s*%)\s*,\s*(?:then\s+)?(?:the\s+"
    r"[A-Z][\w\- ]{0,40}?|such\s+rate|it)\s+shall\s+be\s+deemed\s+(?:to\s+be\s+)?"
    r"(?:equal\s+to\s+)?(?:zero|0(?:\.0+)?\s*%)", re.I)
#: SanDisk's "Floor" is the benchmark-replacement boilerplate the definitions
#: tier refuses, and then says what it is: "For the avoidance of doubt the
#: initial Floor for the Adjusted Term SOFR Rate shall be 0%".
_INITIAL_FLOOR = re.compile(
    r"initial\s+Floor\s+(?:for|with\s+respect\s+to)\s+(?:each\s+of\s+)?(?:the\s+)?"
    r"(?:Adjusted\s+)?Term\s+SOFR(?:\s+Rate)?\b[^.;]{0,160}?shall\s+be\s+"
    r"(\d+(?:\.\d+)?\s*%)", re.I)
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
        initial = _INITIAL_FLOOR.search(_flat(node.body))
        value = initial and parse_percent(initial.group(1))
        if value is not None:
            return [_candidate(
                "libor_floor_pct", value, node.span,
                "the definition of 'Floor' points back at the agreement and then "
                f"states the initial Floor for Term SOFR, {initial.group(1)}",
                as_written=initial.group(1),
            )]
        return []          # the definitions tier reads a Floor with a percentage
    for term in _BENCHMARK_TERMS:
        resolved = _resolve(graph, term)
        if resolved is None:
            continue
        node = graph.get(resolved)
        body = _flat(node.body)
        match = _GREATER_OF_FLOOR.search(body) or _NOT_LESS_THAN_FLOOR.search(body)
        if match is not None:
            value, written = parse_percent(match.group(1)), match.group(1)
        else:
            match = _DEEMED_ZERO.search(body)
            if match is None:
                continue
            value, written = Decimal("0"), match.group("below")
        if value is None:
            continue
        return [_candidate(
            "libor_floor_pct", value, node.span,
            f"{resolved!r} is defined so that it is never less than "
            f"{written}, which is a floor on the benchmark",
            as_written=written,
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
