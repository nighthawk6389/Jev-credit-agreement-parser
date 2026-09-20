"""Document sets: the unit of extraction is a chain, not a file.

In private credit, agreements are amended constantly and the operative terms
live scattered across a base agreement plus however many amendments. Extracting
the base alone gives you terms that stopped being true years ago, and does it
silently -- every span is correct, every figure is quoted accurately, and the
answer is wrong.

This is the failure a single-document evaluation cannot see at all, which is
why it is worth more than anything else in Phase 2.

Four things have to work:

* **ordering** by *effective* date, which is not the filing date;
* **restatement vs patch** -- "amended and restated in its entirety to read as
  follows" replaces a section, while "by deleting X and inserting Y" edits it
  in place. The second is harder and is far more common in short amendments;
* **truncation** at an amended-and-restated agreement, which supersedes the
  whole chain before it;
* **ambiguity as a finding** -- two amendments purporting to restate the same
  section with different text is reported, never resolved by picking one.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models.core import Span
from .normalize import NormalizedDocument, ingest
from .tables import parse_date

DocumentRole = Literal[
    "base",
    "amendment",
    "amendment_and_restatement",
    "security",
    "guarantee",
    "intercreditor",
    "other",
]

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_AMENDMENT_NO_RE = re.compile(
    r"\bAMENDMENT\s+(?:NO\.|NUMBER)\s*(\d+)"
    r"|\b(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s+"
    r"AMENDMENT\b",
    re.IGNORECASE,
)
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_AR_RE = re.compile(
    r"\bAMENDED\s+AND\s+RESTATED\s+(?:\w+\s+){0,3}?CREDIT\s+AGREEMENT\b",
    re.IGNORECASE,
)
_ROLE_PATTERNS: tuple[tuple[str, DocumentRole], ...] = (
    (r"\bINTERCREDITOR\s+AGREEMENT\b", "intercreditor"),
    (r"\b(?:GUARANTY|GUARANTEE)\s+AGREEMENT\b", "guarantee"),
    (r"\b(?:SECURITY|PLEDGE)\s+AGREEMENT\b", "security"),
)

#: A facility agreement that also grants security is still the facility
#: agreement. "REVOLVING CREDIT AND SECURITY AGREEMENT" matches the standalone
#: security-agreement pattern, and classifying it as an ancillary document
#: drops the whole deal out of the chain.
_FACILITY_TITLE_RE = re.compile(
    r"\b(?:CREDIT|LOAN|FINANCING|FACILIT(?:Y|IES))\s+(?:AND\s+\w+\s+)?"
    r"(?:AND\s+SERVICING\s+)?AGREEMENT\b"
    r"|\bLOAN\s+AND\s+SECURITY\s+AGREEMENT\b"
    r"|\bNOTE\s+PURCHASE\s+AGREEMENT\b",
    re.IGNORECASE,
)

#: "dated as of August 1, 2017", "effective as of ...", "Amendment Effective
#: Date means ...". Filing date is not effective date and the two differ often.
_EFFECTIVE_RE = re.compile(
    r"(?:Amendment\s+(?:No\.\s*\d+\s+)?Effective\s+Date[\"\u201d\s]*means\s+"
    r"|(?:is\s+)?(?:entered\s+into|made|executed)\s+as\s+of\s+"
    r"|effective\s+as\s+of\s+|dated\s+as\s+of\s+)"
    r"(?P<when>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)

#: Recitals start here, and the first "dated as of" inside them is the *base*
#: agreement's date, not the amendment's. Reading it gives an amendment an
#: effective date years before its own, which then reorders the whole chain.
_RECITALS_RE = re.compile(r"\b(?:WHEREAS|RECITALS|W\s?I\s?T\s?N\s?E\s?S\s?S)\b")

#: How many characters of the head of a document the classifier reads.
HEAD_WINDOW = 4_000


class Document(BaseModel):
    """One file in a set, with its role and when it took effect."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    document_id: str
    path: str
    role: DocumentRole = "base"
    normalized: Any = None                   # NormalizedDocument
    effective_date: date | None = None
    filing_date: date | None = None
    amendment_number: int | None = None
    title: str = ""

    @property
    def text(self) -> str:
        return self.normalized.text if self.normalized is not None else ""

    @property
    def supersedes_chain(self) -> bool:
        """An amended-and-restated agreement replaces everything before it."""
        return self.role == "amendment_and_restatement"

    @property
    def sort_key(self) -> tuple[date, int, str]:
        """Effective date first, then amendment number, then id for stability.

        Deliberately *not* used to order a set on its own; see
        :func:`chain_order`. An undated document defaulting to ``date.min``
        sorts ahead of everything, and in a chain the first document is taken
        as the base -- so a single undated amendment silently became the
        agreement that every other amendment was applied to. That happened on
        a real pair: Amendment No. 5 carried no date, so it became the base
        and Amendment No. 1 was applied on top of it, reversing the deal.
        """
        return (
            self.effective_date or date.min,
            self.amendment_number if self.amendment_number is not None else 0,
            self.document_id,
        )

    @property
    def order_hint(self) -> int | None:
        """Where the drafter says this document sits in the sequence."""
        return self.amendment_number


def chain_order(documents: list[Document]) -> list[Document]:
    """Order a chain, using the numbering when the dates do not carry it.

    Dates are authoritative when every document has one. When some do not,
    amendment numbering is the drafter's own statement of sequence and is
    better evidence than a missing date -- which, defaulted to ``date.min``,
    puts the undated document first and makes it the base of the chain.

    Numbering is used only when it is complete and consistent: every
    amendment numbered, and no two numbered the same. A partly-numbered set
    falls back to dates, because a mixture of the two orderings is a guess
    dressed up as a rule, and ``undated_amendment`` already says the sequence
    could not be established from the documents.
    """
    amendments = [d for d in documents if d.role != "base"]
    numbers = [d.amendment_number for d in amendments]
    undated = [d for d in documents if d.effective_date is None]
    numbering_is_usable = (
        bool(amendments)
        and all(n is not None for n in numbers)
        and len(set(numbers)) == len(numbers)
    )
    if undated and numbering_is_usable:
        # A base agreement, if there is one, precedes every amendment
        # whatever its date says.
        return sorted(
            documents,
            key=lambda d: (
                0 if d.role == "base" else 1,
                d.amendment_number if d.amendment_number is not None else 0,
                d.document_id,
            ),
        )
    return sorted(documents, key=lambda d: d.sort_key)


def classify_role(text: str) -> tuple[DocumentRole, int | None, str]:
    """Role, amendment number and title, from the head of the document."""
    head = text[:HEAD_WINDOW]
    title = " ".join(head.split("\n\n")[0].split())[:160]

    is_facility = bool(_FACILITY_TITLE_RE.search(head))
    if not is_facility:
        for pattern, role in _ROLE_PATTERNS:
            if re.search(pattern, head, re.IGNORECASE):
                return role, None, title

    number: int | None = None
    match = _AMENDMENT_NO_RE.search(head)
    if match:
        number = (
            int(match.group(1)) if match.group(1)
            else _ORDINALS.get((match.group(2) or "").lower())
        )

    if _AR_RE.search(head):
        # "Amendment No. 3 to the Amended and Restated Credit Agreement" is an
        # amendment *to* an A&R, not an A&R itself. The amendment number is
        # what tells them apart.
        return (
            ("amendment", number, title) if number is not None
            else ("amendment_and_restatement", None, title)
        )
    if number is not None or re.search(r"\bAMENDMENT\b", head, re.IGNORECASE):
        return "amendment", number, title
    return "base", None, title


def detect_effective_date(text: str) -> date | None:
    """The document's own date, taken from its preamble.

    Searched before the recitals, because "that certain Credit Agreement dated
    as of May 14, 2015" is the first date in most amendments and it belongs to
    the agreement being amended. Taking it dates Amendment No. 3 to 2015 and
    silently reorders the chain.
    """
    window = text[:HEAD_WINDOW * 4]
    recitals = _RECITALS_RE.search(window)
    preamble = window[:recitals.start()] if recitals else window
    match = _EFFECTIVE_RE.search(preamble)
    if match:
        return parse_date(match.group("when"))
    # No date before the recitals: fall back to a defined Amendment Effective
    # Date anywhere, which is where short amendments put it.
    defined = re.search(
        r"Amendment\s+(?:No\.\s*\d+\s+)?Effective\s+Date[\"\u201d\s]*"
        r"(?:means|shall\s+mean|is)\s+(?P<when>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        window, re.IGNORECASE,
    )
    return parse_date(defined.group("when")) if defined else None


def load_document(
    path: str | Path, document_id: str | None = None, filing_date: date | None = None
) -> Document:
    normalized = ingest(path, document_id=document_id)
    role, number, title = classify_role(normalized.text)
    return Document(
        document_id=normalized.document_id,
        path=str(path),
        role=role,
        normalized=normalized,
        effective_date=detect_effective_date(normalized.text),
        filing_date=filing_date,
        amendment_number=number,
        title=title,
    )


# ---------------------------------------------------------------------------
# Amendment effects
# ---------------------------------------------------------------------------

EffectKind = Literal[
    "restate", "replace_text", "insert", "delete", "redline", "other"
]

#: Real amendments do not number their targets ``2.10``. They number them
#: ``2.1(a)(ii)(B)(3)`` -- a subsection four levels down, which is where the
#: money usually is. A pattern that stops at the decimal matches ``Section 2.1``
#: inside ``Section 2.1(a)(ii)(B)(3)`` and aims the restatement at the whole of
#: Section 2.1, or fails to anchor at all and reports the amendment as empty.
_SECTION_ID = r"\d+(?:\.\d+)?[A-Za-z]?(?:\([A-Za-z0-9]{1,5}\))*"

#: "of the Credit Agreement", "of the Loan and Servicing Agreement", and the
#: other names a base document goes by.
_OF_THE_AGREEMENT = (
    r"(?:of\s+the\s+[A-Z][A-Za-z]*(?:\s+[A-Za-z&]+){0,5}\s+Agreement\s+)?"
)

#: Amendments write "amended and restated in its entirety to read as follows"
#: about as often as they write it without the "to read". Requiring the longer
#: form is a two-word difference that matched none of the twelve restatements
#: in a real Wells Fargo amendment.
#: Counted over a hundred EDGAR agreements, "is hereby amended" is followed by
#: "and restated in its entirety" 24 times, "in its entirety to read" 6 times
#: and a bare "as follows" 5 times, with "and restated" often dropped and the
#: entirety clause often dropped. Each optional piece is optional because a
#: real amendment omitted it.
_RESTATED_AS_FOLLOWS = (
    r"(?:is|are)\s+hereby\s+(?:amended\s+and\s+restated|amended|deleted|"
    r"deleted\s+in\s+its\s+entirety\s+and\s+replaced|replaced|restated)"
    r"(?:\s+in\s+(?:its|their\s+respective)\s+entiret(?:y|ies))?"
    r"(?:\s+and\s+(?:replaced|restated))?"
    r"\s+(?:to\s+read\s+)?as\s+follows[:;]\s*"
)

#: A restatement quotes the replacement text. Preferring the quoted form is
#: what keeps the body from swallowing the clauses that follow it -- an
#: unbounded ``.*?`` to end-of-document silently absorbs the next amendment and
#: then reports that amendment as unparsed.
_RESTATE_QUOTED_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})\s+{_OF_THE_AGREEMENT}"
    rf"{_RESTATED_AS_FOLLOWS}"
    r"[\"\u201c](?P<body>.{1,8000}?)[\"\u201d]",
    re.IGNORECASE | re.DOTALL,
)
_RESTATE_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})\s+{_OF_THE_AGREEMENT}"
    rf"{_RESTATED_AS_FOLLOWS}(?P<body>.*?)"
    r"(?=\n\n\(?[a-zA-Z0-9]{1,4}\)\s|\n\n(?:SECTION|Section)\s+\d|\Z)",
    re.IGNORECASE | re.DOTALL,
)
#: "The table set forth in Section 2.2(b) ... is amended and restated in its
#: entirety as follows". The target is the table, not the section: applying it
#: as a whole-section restatement silently deletes the prose around the table.
_RESTATE_ELEMENT_RE = re.compile(
    r"The\s+(?P<element>table|schedule|chart|grid|definition|proviso|"
    r"first\s+sentence|last\s+sentence|final\s+sentence)\s+"
    r"(?:set\s+forth\s+|contained\s+|appearing\s+)?in\s+"
    rf"Section\s+(?P<section>{_SECTION_ID})\s+{_OF_THE_AGREEMENT}"
    rf"{_RESTATED_AS_FOLLOWS}(?P<body>.*?)"
    r"(?=\n\n\(?[a-zA-Z0-9]{1,4}\)\s|\n\n(?:SECTION|Section)\s+\d|\Z)",
    re.IGNORECASE | re.DOTALL,
)

#: The same thing for a list of terms: \u201cthe definitions of \u201cBorrowing
#: Base\u201d, \u201cLoans\u201d ... and \u201cTotal Usage\u201d appearing in
#: Section 1.01 are hereby amended in their respective entireties to read as
#: follows\u201d. A real amendment restates nine definitions this way, and read
#: as a restatement of Section 1.01 it replaces the entire definitions article
#: with those nine -- every other defined term in the agreement silently gone.
_DEFINITION_LIST_RE = re.compile(
    r"[Tt]he\s+definitions?\s+of\s+(?P<terms>[\"\u201c][^\n]{1,600}?)\s+"
    r"(?:set\s+forth\s+|contained\s+|appearing\s+)?in\s+"
    rf"Section\s+(?P<section>{_SECTION_ID})\s+"
    r"(?:of\s+the\s+[A-Z][A-Za-z]*(?:\s+[A-Za-z&]+){0,5}\s+Agreement\s+)?"
    rf"{_RESTATED_AS_FOLLOWS}(?P<body>.*?)"
    r"(?=\n\n(?:SECTION|Section)\s+\d|\Z)",
    re.IGNORECASE | re.DOTALL,
)
#: A preamble that is amending defined terms rather than a whole section.
_NAMES_DEFINITIONS_RE = re.compile(
    r"[Tt]he\s+definitions?\s+of\b(?![^.]{0,80}\bis\s+hereby\s+deleted\b)",
)
_QUOTED_TERM_RE = re.compile(r"[\"\u201c\u2018']\s*(?P<term>[A-Z][^\"\u201d\u2019'\n]{1,90}?)\s*[\"\u201d\u2019']")

#: A defined term introducing its own body: \u201cBorrowing Base\u201d means ...
_DEFINITION_START_RE = re.compile(
    r"[\"\u201c\u2018']\s*(?P<term>[A-Z][^\"\u201d\u2019'\n]{1,90}?)\s*[\"\u201d\u2019']\s*"
    r"(?:means|shall\s+mean|has\s+the\s+meaning|:)",
)


def split_definitions(body: str) -> dict[str, str]:
    """Split a run of replacement definitions into one body per term."""
    starts = list(_DEFINITION_START_RE.finditer(body))
    out: dict[str, str] = {}
    for index, match in enumerate(starts):
        stop = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        out[" ".join(match.group("term").split())] = body[match.start():stop].strip()
    return out


#: "the definition of the defined term \u201cRevolving Availability Period\u201d
#: set forth in Section 1.01 of the Credit Agreement is hereby deleted in its
#: entirety and replaced as follows". Changing one defined term is how most
#: deals are amended, and the target is the definition, not the section it
#: sits in: applied as a restatement of Section 1.01 this deletes the whole of
#: Article I and replaces it with a single definition.
_RESTATE_DEFINITION_RE = re.compile(
    r"[Tt]he\s+definition\s+of\s+(?:the\s+(?:defined\s+)?term\s+)?"
    r"[\"\u201c](?P<term>[^\"\u201d]{1,90})[\"\u201d]\s*"
    r"(?:set\s+forth\s+|contained\s+|appearing\s+)?"
    rf"(?:in\s+Section\s+(?P<section>{_SECTION_ID})\s+)?{_OF_THE_AGREEMENT}"
    rf"{_RESTATED_AS_FOLLOWS}(?P<body>.*?)"
    r"(?=\n\n\(?[a-zA-Z0-9]{1,4}\)\s|\n\n(?:SECTION|Section)\s+\d|\Z)",
    re.IGNORECASE | re.DOTALL,
)

#: "Section 2.15(c) is hereby amended to (i) delete the reference therein to
#: \u201cSeptember 30, 2015\u201d and insert in lieu thereof a reference to
#: \u201cDecember 31, 2016\u201d and (ii) delete ... and insert ...". One
#: sentence, one section, two independent edits -- and each one moves a date the
#: covenants are tested against. The preamble is matched separately from the
#: swaps because the second swap has no section anchor of its own.
_AMEND_PREAMBLE_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})\s+{_OF_THE_AGREEMENT}"
    r"is\s+hereby\s+amended\s+to\b",
    re.IGNORECASE,
)
_SWAP_RE = re.compile(
    r"delet(?:e|ing)\s+(?:the\s+)?"
    r"(?:reference\s+therein\s+to|references?\s+to|words?|text|figure|amount)\s*"
    r"[\"\u201c](?P<old>[^\"\u201d]{1,200})[\"\u201d]\s*,?\s*"
    r"and\s+(?:insert(?:ing)?|substitut(?:e|ing)|replac(?:e|ing))\s+"
    r"(?:in\s+(?:lieu|place)\s+thereof\s+)?"
    r"(?:a\s+reference\s+to\s+|the\s+(?:words?|text|figure|amount)\s+)?"
    r"[\"\u201c](?P<new>[^\"\u201d]{0,200})[\"\u201d]",
    re.IGNORECASE | re.DOTALL,
)

#: The blackline form: the amendment describes none of its changes in prose. It
#: attaches a marked-up copy of the whole agreement and says "take out what is
#: struck through, keep what is bold and double-underlined". Every change in the
#: deal is carried by typography, which plain-text conversion destroys -- see
#: ``RedlineRange`` in ingest.normalize. Recognising this sentence is how the
#: pipeline knows to insist the markup survived ingestion rather than reading a
#: flattened text in which the old and new figures sit side by side.
_REDLINE_RE = re.compile(
    r"(?P<subject>(?:[A-Z][A-Za-z]*\s+){0,5}Agreement|Schedules|Exhibits|Annexes)"
    r"(?:\s+to\s+the\s+(?:[A-Z][A-Za-z]*\s+){0,5}Agreement)?"
    r"\s+(?:is|are)\s+hereby\s+amended\s+to\s+"
    r"(?:delete|remove|strike)\s+the\s+"
    r"(?:bold,?\s+)?(?:stricken|struck|struck-through|deleted|lined-out)\s+text\b"
    r"(?P<tail>.{0,600}?)(?=\.\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_APPENDIX_RE = re.compile(
    r"attached\s+(?:hereto\s+)?as\s+(?P<appendix>(?:Appendix|Annex|Exhibit|Schedule)"
    r"\s+[A-Z0-9][-A-Z0-9]*)",
    re.IGNORECASE,
)

_REPLACE_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})[^.]{{0,200}}?is\s+hereby\s+amended\s+by\s+"
    r"(?:deleting|replacing)\s+(?:the\s+(?:words?|text|figure|amount|reference\s+to)\s+)?"
    r"[\"“](?P<old>[^\"”]{1,200})[\"”]\s+"
    r"and\s+(?:inserting|substituting|replacing\s+it\s+with)\s+"
    r"(?:in\s+lieu\s+thereof\s+)?(?:the\s+(?:words?|text|figure|amount)\s+)?"
    r"[\"“](?P<new>[^\"”]{0,200})[\"”]",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})[^.]{{0,200}}?is\s+hereby\s+amended\s+by\s+"
    r"(?:adding|inserting)\s+(?:the\s+following\s+)?(?:new\s+)?"
    r"(?:clause|subsection|paragraph|sentence|proviso)?[^\"“]{0,80}"
    r"[\"“](?P<new>[^\"”]{1,400})[\"”]",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_RE = re.compile(
    rf"Section\s+(?P<section>{_SECTION_ID})[^.]{{0,200}}?is\s+hereby\s+"
    r"(?:amended\s+by\s+deleting|deleted)\s+"
    r"(?:the\s+(?:words?|text|clause|proviso)\s+)?"
    r"[\"“](?P<old>[^\"”]{1,200})[\"”]"
    r"(?!\s+and\s+(?:inserting|substituting))",
    re.IGNORECASE | re.DOTALL,
)


class AmendmentEffect(BaseModel):
    """One change an amendment makes to the operative text."""

    kind: EffectKind
    target_section: str
    document_id: str
    span: Span
    #: Full replacement text, for a restatement.
    new_text: str | None = None
    #: For an in-place edit.
    old_fragment: str | None = None
    new_fragment: str | None = None
    #: Set when the restatement replaces one element of a section -- its table,
    #: its final sentence -- rather than the whole of it.
    target_element: str | None = None
    #: For a blackline, the attachment carrying the marked-up text.
    attachment: str | None = None
    effective_date: date | None = None
    amendment_number: int | None = None

    #: The section id a whole-document blackline nominally targets.
    WHOLE_AGREEMENT: ClassVar[str] = "*"

    @property
    def is_restatement(self) -> bool:
        return self.kind == "restate" and self.target_element is None

    @property
    def is_partial(self) -> bool:
        """A restatement of one element inside a section."""
        return self.kind == "restate" and self.target_element is not None

    def describe(self) -> str:
        if self.kind == "redline":
            where = f" set out in {self.attachment}" if self.attachment else ""
            return (
                "restates the agreement by blackline: deletions struck through "
                f"and insertions underlined{where}"
            )
        if self.is_partial:
            element = self.target_element or ""
            if element == "definitions:unparsed":
                return (
                    f"amends named definitions in Section {self.target_section}, "
                    "and which ones could not be read"
                )
            if element.startswith("definitions:"):
                terms = element.split(":", 1)[1].split("|")
                shown = ", ".join(f'"{t}"' for t in terms[:3])
                more = f" and {len(terms) - 3} more" if len(terms) > 3 else ""
                return (
                    f"restates {len(terms)} definitions in Section "
                    f"{self.target_section}: {shown}{more}"
                )
            if element.startswith("definition:"):
                term = element.split(":", 1)[1]
                return (
                    f'restates the definition of "{term}" in Section '
                    f"{self.target_section}"
                )
            return (
                f"restates the {element} in Section {self.target_section}"
            )
        if self.kind == "restate":
            return f"restates Section {self.target_section} in its entirety"
        if self.kind == "replace_text":
            return (
                f"in Section {self.target_section}, replaces "
                f"{self.old_fragment!r} with {self.new_fragment!r}"
            )
        if self.kind == "insert":
            return f"inserts text into Section {self.target_section}"
        if self.kind == "delete":
            return (
                f"deletes {self.old_fragment!r} from Section "
                f"{self.target_section}"
            )
        return f"modifies Section {self.target_section}"


def parse_amendment_effects(document: Document) -> list[AmendmentEffect]:
    """Read what an amendment actually does.

    Restatements are easy -- the amendment quotes the whole new section. The
    in-place edits are the hard case and the common one: a short amendment that
    swaps one figure for another changes the deal without restating anything,
    and a pipeline that only handles restatement reports the old number with
    full confidence.
    """
    normalized: NormalizedDocument = document.normalized
    if normalized is None:
        return []
    text = normalized.text
    effects: list[AmendmentEffect] = []
    claimed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in claimed)

    def add(
        kind: EffectKind,
        start: int,
        end: int,
        section: str,
        **kwargs: Any,
    ) -> None:
        if overlaps(start, end):
            return
        claimed.append((start, end))
        effects.append(AmendmentEffect(
            kind=kind,
            target_section=section,
            document_id=document.document_id,
            span=normalized.span(start, end),
            effective_date=document.effective_date,
            amendment_number=document.amendment_number,
            **kwargs,
        ))

    def add_match(kind: EffectKind, match: re.Match[str], **kwargs: Any) -> None:
        add(kind, match.start(), match.end(), match.group("section"), **kwargs)

    # A blackline claims the whole document, so it is read first and the
    # prose patterns below then find nothing left to claim -- which is correct:
    # in a blackline the prose describes the mechanism, not the changes.
    for match in _REDLINE_RE.finditer(text):
        attachment = _APPENDIX_RE.search(match.group("tail") or "")
        add("redline", match.start(), match.end(),
            AmendmentEffect.WHOLE_AGREEMENT,
            attachment=attachment.group("appendix") if attachment else None)

    # Element-first: "The table set forth in Section 2.2(b) is restated" must
    # be claimed before the plain restatement pattern reads the same sentence
    # as a restatement of all of Section 2.2(b).
    # Definitions first: they are the commonest target and the ones whose
    # mis-application destroys the most text. The list form runs before the
    # single form, and both before the plain section restatement, so a
    # sentence naming definitions can never be claimed as a whole section.
    for match in _DEFINITION_LIST_RE.finditer(text):
        named = [
            " ".join(m.group("term").split())
            for m in _QUOTED_TERM_RE.finditer(match.group("terms"))
        ]
        bodies = split_definitions(match.group("body"))
        if len(named) < 2:
            continue
        add("restate", match.start(), match.end(), match.group("section"),
            new_text=match.group("body").strip(),
            target_element="definitions:" + "|".join(named))
    for match in _RESTATE_DEFINITION_RE.finditer(text):
        term = " ".join(match.group("term").split())
        add("restate", match.start(), match.end(),
            match.group("section") or _DEFINITIONS_SECTION,
            new_text=match.group("body").strip(),
            target_element=f"definition:{term}")
    for match in _RESTATE_ELEMENT_RE.finditer(text):
        add_match("restate", match, new_text=match.group("body").strip(),
                  target_element=" ".join(match.group("element").split()).lower())
    def add_restatement(match: re.Match[str]) -> None:
        """One restatement, unless its preamble says it is amending terms.

        A sentence that names definitions is amending those definitions,
        whatever section they sit in. Applied as a section restatement it
        replaces the whole of Article I with however many definitions the
        amendment happened to list -- every other defined term in the
        agreement gone, silently, with a correct span to point at.

        One real amendment reaches here despite the two definition patterns
        above, because a DocuSign stamp and an image filename are interleaved
        into the middle of its list of terms. It cannot be parsed, and it must
        still not be applied: it is recorded as a definition restatement whose
        targets are unknown, which lands in `unapplied` with a reason.
        """
        preamble = text[max(0, match.start() - 400):match.start()]
        element = (
            "definitions:unparsed"
            if _NAMES_DEFINITIONS_RE.search(preamble) else None
        )
        add_match("restate", match, new_text=match.group("body").strip(),
                  target_element=element)

    for match in _RESTATE_QUOTED_RE.finditer(text):
        add_restatement(match)
    for match in _RESTATE_RE.finditer(text):
        add_restatement(match)
    for match in _REPLACE_RE.finditer(text):
        add_match("replace_text", match,
                  old_fragment=match.group("old").strip(),
                  new_fragment=match.group("new").strip())
    for match in _DELETE_RE.finditer(text):
        add_match("delete", match, old_fragment=match.group("old").strip())
    for match in _INSERT_RE.finditer(text):
        add_match("insert", match, new_fragment=match.group("new").strip())

    # "Section X is hereby amended to (i) delete "A" and insert "B" and (ii)
    # delete "C" and insert "D"". Each swap is its own effect; only the first
    # carries a section anchor, so the section comes from the preamble.
    preambles = list(_AMEND_PREAMBLE_RE.finditer(text))
    for index, preamble in enumerate(preambles):
        stop = (
            preambles[index + 1].start() if index + 1 < len(preambles)
            else len(text)
        )
        sentence_end = text.find("\n\n", preamble.end())
        if 0 <= sentence_end < stop:
            stop = sentence_end
        for swap in _SWAP_RE.finditer(text, preamble.end(), stop):
            add("replace_text", swap.start(), swap.end(),
                preamble.group("section"),
                old_fragment=swap.group("old").strip(),
                new_fragment=swap.group("new").strip())

    effects.sort(key=lambda e: e.span.start)
    return effects


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------


class ChainFinding(BaseModel):
    """Something about the chain that a reader has to know."""

    kind: Literal[
        "operative_version_ambiguous",
        "amendment_target_missing",
        "superseded_by_restatement",
        "undated_amendment",
        "effective_before_base",
        "blackline_markup_lost",
        "blackline_restates_agreement",
        "partial_restatement_unlocated",
        "base_agreement_absent",
    ]
    message: str
    documents: list[str] = Field(default_factory=list)
    section: str | None = None
    severity: Literal["error", "warning"] = "error"


class DocumentSet(BaseModel):
    """A base agreement, its amendments, and the other loan documents."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base: Document
    amendments: list[Document] = Field(default_factory=list)
    other_loan_documents: list[Document] = Field(default_factory=list)
    operative_as_of: date | None = None
    #: Documents dropped because an A&R superseded them.
    superseded: list[Document] = Field(default_factory=list)
    findings: list[ChainFinding] = Field(default_factory=list)

    @property
    def documents(self) -> list[Document]:
        return [self.base, *self.amendments, *self.other_loan_documents]

    @property
    def chain(self) -> list[Document]:
        """Base plus amendments, in the order they took effect."""
        return [self.base, *self.amendments]

    @property
    def base_is_missing(self) -> bool:
        """The set is amendments only, with nothing to amend.

        Exhibits are filed one at a time, so an amendment arriving on its own
        is the ordinary case, not an odd one. Taking it as the agreement hands
        a reader the amendment's own recitals as if they were the deal terms.

        A blackline is the exception and is self-sufficient: it attaches a
        conformed copy of the entire agreement, so once the struck text is
        excised the operative terms are all present in the one document.
        """
        if self.base.role != "amendment":
            return False
        marked = self.base.normalized
        return not (marked is not None and marked.is_blackline)

    def effects(self) -> list[AmendmentEffect]:
        """Every change the chain applies to the base, oldest first."""
        out: list[AmendmentEffect] = []
        for document in self.amendments:
            out.extend(parse_amendment_effects(document))
        return out

    def declared_effects(self) -> list[AmendmentEffect]:
        """Every change the set *describes*, including ones it cannot apply.

        When the base agreement is absent this is the only useful output: the
        amendment still says what it changes and to which sections, and a
        reader can act on that even though no operative text can be assembled.
        """
        out = list(self.effects())
        if self.base.role in ("amendment", "amendment_and_restatement"):
            out = parse_amendment_effects(self.base) + out
        return out

    def document(self, document_id: str) -> Document | None:
        return next(
            (d for d in self.documents if d.document_id == document_id), None
        )

    def describe(self) -> str:
        lines = [
            f"base: {self.base.title[:70]} "
            f"({self.base.effective_date or 'undated'})"
        ]
        for amendment in self.amendments:
            lines.append(
                f"  +{amendment.amendment_number or '?'}: "
                f"{amendment.title[:60]} ({amendment.effective_date or 'undated'})"
            )
        for superseded in self.superseded:
            lines.append(f"  -- superseded: {superseded.title[:60]}")
        return "\n".join(lines)


def assemble_set(
    documents: list[Document], operative_as_of: date | None = None
) -> DocumentSet:
    """Order a pile of files into a chain and report what is wrong with it."""
    if not documents:
        raise ValueError("a document set needs at least one document")

    findings: list[ChainFinding] = []
    others = [
        d for d in documents
        if d.role in ("security", "guarantee", "intercreditor", "other")
    ]
    chain_docs = [d for d in documents if d not in others]

    for document in chain_docs:
        if document.role == "amendment" and document.effective_date is None:
            findings.append(ChainFinding(
                kind="undated_amendment", severity="warning",
                message=(
                    f"{document.title[:60]!r} carries no effective date; it is "
                    "ordered by amendment number, which can disagree with the "
                    "order the changes actually took effect"
                ),
                documents=[document.document_id],
            ))

    ordered = chain_order(chain_docs)

    # An amended-and-restated agreement supersedes everything before it.
    last_restatement = max(
        (i for i, d in enumerate(ordered) if d.supersedes_chain), default=None
    )
    superseded: list[Document] = []
    if last_restatement is not None and last_restatement > 0:
        superseded = ordered[:last_restatement]
        findings.append(ChainFinding(
            kind="superseded_by_restatement", severity="warning",
            message=(
                f"{len(superseded)} document(s) precede an amended and restated "
                "agreement and are no longer operative"
            ),
            documents=[d.document_id for d in superseded],
        ))
        ordered = ordered[last_restatement:]

    base = ordered[0]
    amendments = [d for d in ordered[1:] if d.role != "base"]

    if operative_as_of is not None:
        in_force, later = [], []
        for amendment in amendments:
            target = later if (
                amendment.effective_date and amendment.effective_date > operative_as_of
            ) else in_force
            target.append(amendment)
        amendments = in_force
        superseded.extend(later)

    for amendment in amendments:
        if (
            amendment.effective_date and base.effective_date
            and amendment.effective_date < base.effective_date
        ):
            findings.append(ChainFinding(
                kind="effective_before_base",
                message=(
                    f"amendment effective {amendment.effective_date} precedes the "
                    f"base agreement dated {base.effective_date}"
                ),
                documents=[amendment.document_id],
            ))

    document_set = DocumentSet(
        base=base, amendments=amendments, other_loan_documents=others,
        operative_as_of=operative_as_of, superseded=superseded, findings=findings,
    )
    if document_set.base_is_missing:
        declared = parse_amendment_effects(base)
        targets = sorted({
            e.target_section for e in declared
            if e.target_section != AmendmentEffect.WHOLE_AGREEMENT
        })
        document_set.findings.append(ChainFinding(
            kind="base_agreement_absent",
            message=(
                f"the set is an amendment with no agreement to amend; it "
                f"declares {len(declared)} change(s)"
                + (f" to Section(s) {', '.join(targets[:8])}" if targets else "")
                + ". No operative text can be assembled and nothing in this "
                "document should be read as a current deal term"
            ),
            documents=[base.document_id],
        ))
    document_set.findings.extend(_chain_conflicts(document_set))
    return document_set


def _chain_conflicts(document_set: DocumentSet) -> list[ChainFinding]:
    """Restatements that collide, and amendments aimed at nothing."""
    findings: list[ChainFinding] = []
    base_sections = {
        s.section_id for s in (document_set.base.normalized.sections or [])
    } if document_set.base.normalized is not None else set()

    by_section: dict[str, list[AmendmentEffect]] = {}
    for effect in document_set.declared_effects():
        if effect.kind == "redline":
            source = next(
                (d for d in document_set.documents
                 if d.document_id == effect.document_id),
                None,
            )
            marked = source.normalized if source is not None else None
            if marked is not None and marked.is_blackline:
                findings.append(ChainFinding(
                    kind="blackline_restates_agreement",
                    message=(
                        f"{effect.describe()}; {len(marked.deletions())} deleted "
                        "run(s) were excised from the operative text and kept "
                        "for audit"
                    ),
                    documents=[effect.document_id],
                    severity="warning",
                ))
            else:
                findings.append(ChainFinding(
                    kind="blackline_markup_lost",
                    message=(
                        "the amendment carries its changes as strike-through and "
                        "underline and no such markup survived conversion; every "
                        "figure it changed still reads as the superseded value "
                        "beside its replacement, and no term from this document "
                        "can be relied on"
                    ),
                    documents=[effect.document_id],
                ))
            continue

        by_section.setdefault(effect.target_section, []).append(effect)

        # "Section 2.1(a)(ii)(B)(3)" is a subsection of "2.1", which is what
        # the base marks. Comparing the full identifier alone reports every
        # subsection-level amendment as aimed at nothing.
        parent = effect.target_section.split("(")[0]
        if (
            base_sections
            and not document_set.base_is_missing  # already reported, once
            and not {effect.target_section, parent} & base_sections
        ):
            # The amendment edits a section that is not in the operative text.
            # Either the target is wrong or we are amending the wrong version.
            findings.append(ChainFinding(
                kind="amendment_target_missing",
                message=(
                    f"amendment targets Section {effect.target_section}, which "
                    "does not exist in the operative text"
                ),
                documents=[effect.document_id],
                section=effect.target_section,
            ))

    for section, effects in sorted(by_section.items()):
        restatements = [e for e in effects if e.is_restatement]
        if len(restatements) < 2:
            continue
        for earlier, later in zip(restatements, restatements[1:]):
            unordered = (
                earlier.effective_date is None
                or later.effective_date is None
                or earlier.effective_date == later.effective_date
            )
            if unordered and (earlier.new_text or "") != (later.new_text or ""):
                findings.append(ChainFinding(
                    kind="operative_version_ambiguous",
                    message=(
                        f"two amendments restate Section {section} with "
                        "different text and cannot be ordered; the operative "
                        "version is undetermined and is not being guessed at"
                    ),
                    documents=[earlier.document_id, later.document_id],
                    section=section,
                ))
    return findings


def load_document_set(
    paths: list[str | Path], operative_as_of: date | None = None
) -> DocumentSet:
    return assemble_set([load_document(p) for p in paths], operative_as_of)


# ---------------------------------------------------------------------------
# Applying the chain
# ---------------------------------------------------------------------------


class OperativeSection(BaseModel):
    """A section's current text and which document last changed it."""

    section_id: str
    source_document_id: str
    history: list[str] = Field(default_factory=list)

    @property
    def amended(self) -> bool:
        return bool(self.history)


class OperativeText(BaseModel):
    """The agreement as it currently reads, with per-section provenance."""

    text: str
    base_document_id: str
    sections: dict[str, OperativeSection] = Field(default_factory=dict)
    applied: list[AmendmentEffect] = Field(default_factory=list)
    #: Effects that could not be applied, and why. Never silently dropped: an
    #: amendment the pipeline could not apply is a term it is reporting wrongly.
    unapplied: list[tuple[AmendmentEffect, str]] = Field(default_factory=list)
    #: (base_start, base_end, cumulative_delta) for re-anchoring base offsets.
    offset_map: list[tuple[int, int, int]] = Field(default_factory=list)

    def source_of(self, section_id: str | None) -> str:
        record = self.sections.get(section_id or "")
        return record.source_document_id if record else self.base_document_id

    def summary(self) -> dict[str, Any]:
        return {
            "base_document": self.base_document_id,
            "sections_amended": sorted(
                s for s, r in self.sections.items() if r.amended
            ),
            "effects_applied": len(self.applied),
            "effects_unapplied": [
                {"effect": e.describe(), "reason": why}
                for e, why in self.unapplied
            ],
        }


#: A row the normalizer wrote out of a table cell. Used to find the table
#: inside a section body when an amendment restates only the table.
_TABLE_ROW = " | "

#: Where a definition lives when the amendment does not say which section.
_DEFINITIONS_SECTION = "1.01"


def _element_bounds(body: str, element: str) -> tuple[int, int] | None:
    """Locate the element of a section a partial restatement replaces."""
    if element in ("table", "chart", "grid", "schedule"):
        rows = [
            index for index, line in enumerate(body.split("\n"))
            if _TABLE_ROW in line
        ]
        if not rows:
            return None
        lines = body.split("\n")
        start = sum(len(line) + 1 for line in lines[: rows[0]])
        end = start + sum(len(lines[i]) + 1 for i in range(rows[0], rows[-1] + 1))
        return start, min(end, len(body))
    sentences = [
        match for match in re.finditer(r"[^.;]+[.;]", body) if match.group().strip()
    ]
    if not sentences:
        return None
    if element == "first sentence":
        return sentences[0].start(), sentences[0].end()
    if element in ("last sentence", "final sentence"):
        return sentences[-1].start(), sentences[-1].end()
    if element.startswith("definitions:"):
        # Several named definitions, replaced together. Each is located and
        # replaced on its own, so the rest of the article survives; a term the
        # replacement text does not actually carry is left alone rather than
        # blanked. Bounds span the first to the last, and the caller edits
        # within them.
        terms = element.split(":", 1)[1].split("|")
        spans = [
            _element_bounds(body, f"definition:{term}") for term in terms
        ]
        found = [s for s in spans if s is not None]
        if not found:
            return None
        return min(s[0] for s in found), max(s[1] for s in found)
    if element.startswith("definition:"):
        # Locatable, unlike a bare "the definition": the amendment names the
        # term, and a defined term is introduced by its own quoted heading.
        term = element.split(":", 1)[1]
        opening = re.search(
            rf"[\"\u201c]\s*{re.escape(term)}\s*[\"\u201d]\s*"
            r"(?:means|shall\s+mean|has\s+the\s+meaning|:)",
            body, re.IGNORECASE,
        )
        if opening is None:
            return None
        # A definition runs to the next one, not to the end of the article.
        following = re.search(
            r"[\"\u201c][A-Z][^\"\u201d\n]{1,90}[\"\u201d]\s*"
            r"(?:means|shall\s+mean|has\s+the\s+meaning|:)",
            body[opening.end():],
        )
        stop = opening.end() + following.start() if following else len(body)
        return opening.start(), stop
    # "the proviso": no reliable anchor, and guessing which sentence was meant
    # is how an amendment gets applied to the wrong clause.
    return None


def _appendix_start(normalized: NormalizedDocument, attachment: str | None) -> int:
    """Where the marked-up agreement begins inside a blackline amendment."""
    if attachment:
        # The last mention: earlier ones are the cross-references that named it.
        marker = None
        for match in re.finditer(re.escape(attachment), normalized.text, re.IGNORECASE):
            marker = match
        if marker is not None:
            return marker.start()
    return 0


def _subsection_bounds(
    normalized: NormalizedDocument, section_id: str
) -> tuple[int, int] | None:
    """Locate ``2.2(a)`` inside Section 2.2, which is what the base marks.

    Only the first level is located. ``(ii)`` and ``(B)`` recur a dozen times
    in an ordinary section, and picking one occurrence is a guess -- a guess
    that would overwrite whichever clause it landed on. Deeper targets return
    None and are reported as unapplied, which is the accurate answer.
    """
    head, *rest = re.split(r"[()]+", section_id.strip())
    parts = [piece for piece in rest if piece]
    if len(parts) != 1:
        return None
    bounds = _section_bounds(normalized, head)
    if bounds is None:
        return None
    start, end = bounds
    body = normalized.text[start:end]
    opening = re.search(rf"(?:^|\n)\s*\({re.escape(parts[0])}\)\s", body)
    if opening is None:
        return None
    following = re.search(
        r"(?:^|\n)\s*\([A-Za-z0-9]{1,4}\)\s", body[opening.end():]
    )
    stop = opening.end() + following.start() if following else len(body)
    return start + opening.start(), start + stop


def _section_bounds(normalized: NormalizedDocument, section_id: str) -> tuple[int, int] | None:
    for index, marker in enumerate(normalized.sections):
        if marker.section_id != section_id:
            continue
        end = marker.end or (
            normalized.sections[index + 1].offset
            if index + 1 < len(normalized.sections) else len(normalized.text)
        )
        return marker.offset, end
    return None


def apply_chain(document_set: DocumentSet) -> OperativeText:
    """Fold every amendment into the base to get the text that is in force.

    Section bodies are edited in place and the document is then reassembled,
    rather than the whole text being mutated progressively. That keeps a map
    from base offsets to operative offsets, which is what lets the base's
    tables -- and therefore Trap 1 -- survive an amendment chain. Editing the
    string directly loses every table in the document the moment any amendment
    applies.

    Applied oldest first, so a section restated twice carries the later text.
    An effect that cannot be located is recorded in ``unapplied`` rather than
    skipped, because an amendment the pipeline silently failed to apply is a
    term it is now reporting wrongly with full confidence.
    """
    base = document_set.base
    normalized: NormalizedDocument = base.normalized
    applied: list[AmendmentEffect] = []
    unapplied: list[tuple[AmendmentEffect, str]] = []
    effects = list(document_set.effects())

    # A blackline restates the whole agreement in an attachment, so it rebases
    # the chain rather than patching it. It can only be honoured when the
    # deletion markup survived ingestion; the alternative -- reading a text in
    # which the old and new figures sit side by side -- is the one outcome that
    # must never happen quietly, so it is refused rather than approximated.
    for effect in [e for e in effects if e.kind == "redline"]:
        source = next(
            (d for d in document_set.documents if d.document_id == effect.document_id),
            None,
        )
        marked = source.normalized if source is not None else None
        if marked is None or not marked.is_blackline:
            unapplied.append((
                effect,
                "the amendment carries its changes as strike-through and "
                "underline, and no such markup survived conversion; the "
                "operative text cannot be assembled from this source",
            ))
            continue
        normalized = marked
        base = source
        applied.append(effect)

    bodies: dict[str, str] = {}
    sections: dict[str, OperativeSection] = {
        marker.section_id: OperativeSection(
            section_id=marker.section_id, source_document_id=base.document_id,
        )
        for marker in normalized.sections
    }

    for effect in effects:
        if effect.kind == "redline":
            continue
        bounds = _section_bounds(normalized, effect.target_section)
        if bounds is None and "(" in effect.target_section:
            bounds = _subsection_bounds(normalized, effect.target_section)
        if bounds is None:
            parent = effect.target_section.split("(")[0]
            unapplied.append((effect, (
                f"Section {effect.target_section} could not be located in the "
                f"base text"
                + (
                    f"; Section {parent} is present but the subsection marker "
                    "is not addressable, and restating the whole of "
                    f"Section {parent} would delete its other subsections"
                    if _section_bounds(normalized, parent) is not None
                    else ""
                )
            )))
            continue
        start_offset, end_offset = bounds
        body = bodies.get(
            effect.target_section, normalized.text[start_offset:end_offset]
        )

        if effect.is_partial:
            element = effect.target_element or ""
            inner = _element_bounds(body, element)
            if inner is None:
                unapplied.append((
                    effect,
                    f"the {element} it restates cannot be located inside "
                    f"Section {effect.target_section}; applying it as a whole-"
                    "section restatement would delete the rest of the section",
                ))
                continue
            new_body = (
                body[: inner[0]] + (effect.new_text or "").strip() + body[inner[1]:]
            )
        elif effect.kind == "restate":
            new_body = f"SECTION {effect.target_section} {(effect.new_text or '').strip()}"
        elif effect.kind in ("replace_text", "delete"):
            fragment = effect.old_fragment or ""
            if fragment not in body:
                unapplied.append((
                    effect,
                    f"text {fragment!r} is not in Section "
                    f"{effect.target_section} as it currently reads",
                ))
                continue
            new_body = body.replace(
                fragment,
                effect.new_fragment or "" if effect.kind == "replace_text" else "",
                1,
            )
        elif effect.kind == "insert":
            new_body = f"{body.rstrip()} {effect.new_fragment or ''}".strip()
        else:
            unapplied.append((effect, f"unhandled effect kind {effect.kind}"))
            continue

        bodies[effect.target_section] = new_body
        record = sections.setdefault(
            effect.target_section,
            OperativeSection(section_id=effect.target_section,
                             source_document_id=base.document_id),
        )
        record.source_document_id = effect.document_id
        record.history = [*record.history, effect.describe()]
        applied.append(effect)

    text, remap = _reassemble(normalized, bodies)
    return OperativeText(
        text=text, base_document_id=base.document_id, sections=sections,
        applied=applied, unapplied=unapplied, offset_map=remap,
    )


def _reassemble(
    normalized: NormalizedDocument, bodies: dict[str, str]
) -> tuple[str, list[tuple[int, int, int]]]:
    """Rebuild the document from its sections, recording the offset shifts.

    Returns the operative text and a list of ``(base_start, base_end, delta)``
    entries describing how offsets moved, so that anything anchored in the base
    can be re-anchored in the operative text.
    """
    if not normalized.sections:
        return normalized.text, []
    pieces: list[str] = [normalized.text[: normalized.sections[0].offset]]
    shifts: list[tuple[int, int, int]] = []
    cumulative = 0
    for index, marker in enumerate(normalized.sections):
        start = marker.offset
        end = marker.end or (
            normalized.sections[index + 1].offset
            if index + 1 < len(normalized.sections) else len(normalized.text)
        )
        original = normalized.text[start:end]
        body = bodies.get(marker.section_id, original)
        if body is not original:
            cumulative += len(body) - len(original)
            shifts.append((start, end, cumulative))
        pieces.append(body)
    return "".join(pieces), shifts


def remap_offset(offset: int, shifts: list[tuple[int, int, int]]) -> int:
    """Move a base offset into the operative text's coordinate space."""
    delta = 0
    for start, end, cumulative in shifts:
        if offset >= end:
            delta = cumulative
        elif offset >= start:
            # Inside an edited section: the interior is not addressable, so
            # anchor at the section start rather than guessing a position.
            return start + (delta if delta else 0)
    return offset + delta


def operative_document(
    document_set: DocumentSet, operative: OperativeText
) -> NormalizedDocument:
    """A NormalizedDocument over the operative text, keeping intact tables.

    A table inside an amended section is dropped rather than remapped: the
    amendment may have replaced the grid entirely, and a table whose offsets
    are a guess is worse than no table at all.
    """
    from .normalize import detect_sections

    base: NormalizedDocument = document_set.base.normalized
    edited = {
        section for section, record in operative.sections.items() if record.amended
    }
    tables = []
    for table in base.tables:
        if table.section_id in edited:
            continue
        shift = remap_offset(table.start, operative.offset_map) - table.start
        if shift == 0:
            tables.append(table)
            continue
        moved = table.model_copy(deep=True)
        moved.start += shift
        moved.end += shift
        for cell in moved.cells:
            cell.start += shift
            cell.end += shift
        tables.append(moved)

    # Deletions are carried forward and re-anchored. Losing them here would
    # leave a blackline's operative text correct but unexplained: the report
    # would show the new figure with nothing to say what it replaced.
    redlines = []
    for region in base.redlines:
        shift = remap_offset(region.start, operative.offset_map) - region.start
        moved = region.model_copy()
        moved.start += shift
        moved.end += shift
        redlines.append(moved)

    document = NormalizedDocument(
        document_id=f"{base.document_id}+operative",
        source_path=document_set.base.path,
        source_format=base.source_format,
        text=operative.text,
        tables=tables,
        pages=base.pages,
        sections=detect_sections(operative.text),
        redlines=redlines,
        meta={
            "operative_as_of": str(document_set.operative_as_of or ""),
            "chain_length": str(len(document_set.chain)),
        },
    )
    return document
