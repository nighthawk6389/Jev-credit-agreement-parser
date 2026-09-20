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
from typing import Any, Literal

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

#: "dated as of August 1, 2017", "effective as of ...", "Amendment Effective
#: Date means ...". Filing date is not effective date and the two differ often.
_EFFECTIVE_RE = re.compile(
    r"(?:Amendment\s+(?:No\.\s*\d+\s+)?Effective\s+Date[\"\s]*means\s+"
    r"|effective\s+as\s+of\s+|dated\s+as\s+of\s+)"
    r"(?P<when>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)

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
        """Effective date first, then amendment number, then id for stability."""
        return (
            self.effective_date or date.min,
            self.amendment_number if self.amendment_number is not None else 0,
            self.document_id,
        )


def classify_role(text: str) -> tuple[DocumentRole, int | None, str]:
    """Role, amendment number and title, from the head of the document."""
    head = text[:HEAD_WINDOW]
    title = " ".join(head.split("\n\n")[0].split())[:160]

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
    match = _EFFECTIVE_RE.search(text[:HEAD_WINDOW * 4])
    return parse_date(match.group("when")) if match else None


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

EffectKind = Literal["restate", "replace_text", "insert", "delete", "other"]

#: A restatement quotes the replacement text. Preferring the quoted form is
#: what keeps the body from swallowing the clauses that follow it -- an
#: unbounded ``.*?`` to end-of-document silently absorbs the next amendment and
#: then reports that amendment as unparsed.
_RESTATE_QUOTED_RE = re.compile(
    r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)\s+(?:of\s+the\s+Credit\s+Agreement\s+)?"
    r"is\s+hereby\s+amended\s+and\s+restated\s+in\s+its\s+entirety\s+"
    r"to\s+read\s+as\s+follows[:;]\s*"
    r"[\"\u201c](?P<body>.{1,8000}?)[\"\u201d]",
    re.IGNORECASE | re.DOTALL,
)
_RESTATE_RE = re.compile(
    r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)\s+(?:of\s+the\s+Credit\s+Agreement\s+)?"
    r"is\s+hereby\s+amended\s+and\s+restated\s+in\s+its\s+entirety\s+"
    r"to\s+read\s+as\s+follows[:;]\s*(?P<body>.*?)"
    r"(?=\n\n\(?[a-zA-Z0-9]{1,4}\)\s|\n\n(?:SECTION|Section)\s+\d|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_REPLACE_RE = re.compile(
    r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)[^.]{0,200}?is\s+hereby\s+amended\s+by\s+"
    r"(?:deleting|replacing)\s+(?:the\s+(?:words?|text|figure|amount|reference\s+to)\s+)?"
    r"[\"“](?P<old>[^\"”]{1,200})[\"”]\s+"
    r"and\s+(?:inserting|substituting|replacing\s+it\s+with)\s+"
    r"(?:in\s+lieu\s+thereof\s+)?(?:the\s+(?:words?|text|figure|amount)\s+)?"
    r"[\"“](?P<new>[^\"”]{0,200})[\"”]",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_RE = re.compile(
    r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)[^.]{0,200}?is\s+hereby\s+amended\s+by\s+"
    r"(?:adding|inserting)\s+(?:the\s+following\s+)?(?:new\s+)?"
    r"(?:clause|subsection|paragraph|sentence|proviso)?[^\"“]{0,80}"
    r"[\"“](?P<new>[^\"”]{1,400})[\"”]",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_RE = re.compile(
    r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)[^.]{0,200}?is\s+hereby\s+"
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
    effective_date: date | None = None
    amendment_number: int | None = None

    @property
    def is_restatement(self) -> bool:
        return self.kind == "restate"

    def describe(self) -> str:
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

    def add(kind: EffectKind, match: re.Match[str], **kwargs: Any) -> None:
        if overlaps(match.start(), match.end()):
            return
        claimed.append((match.start(), match.end()))
        effects.append(AmendmentEffect(
            kind=kind,
            target_section=match.group("section"),
            document_id=document.document_id,
            span=normalized.span(match.start(), match.end()),
            effective_date=document.effective_date,
            amendment_number=document.amendment_number,
            **kwargs,
        ))

    for match in _RESTATE_QUOTED_RE.finditer(text):
        add("restate", match, new_text=match.group("body").strip())
    for match in _RESTATE_RE.finditer(text):
        add("restate", match, new_text=match.group("body").strip())
    for match in _REPLACE_RE.finditer(text):
        add("replace_text", match,
            old_fragment=match.group("old").strip(),
            new_fragment=match.group("new").strip())
    for match in _DELETE_RE.finditer(text):
        add("delete", match, old_fragment=match.group("old").strip())
    for match in _INSERT_RE.finditer(text):
        add("insert", match, new_fragment=match.group("new").strip())

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

    def effects(self) -> list[AmendmentEffect]:
        """Every change the chain makes, oldest first."""
        out: list[AmendmentEffect] = []
        for document in self.amendments:
            out.extend(parse_amendment_effects(document))
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

    ordered = sorted(chain_docs, key=lambda d: d.sort_key)

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
    document_set.findings.extend(_chain_conflicts(document_set))
    return document_set


def _chain_conflicts(document_set: DocumentSet) -> list[ChainFinding]:
    """Restatements that collide, and amendments aimed at nothing."""
    findings: list[ChainFinding] = []
    base_sections = {
        s.section_id for s in (document_set.base.normalized.sections or [])
    } if document_set.base.normalized is not None else set()

    by_section: dict[str, list[AmendmentEffect]] = {}
    for effect in document_set.effects():
        by_section.setdefault(effect.target_section, []).append(effect)

        if base_sections and effect.target_section not in base_sections:
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
    bodies: dict[str, str] = {}
    sections: dict[str, OperativeSection] = {
        marker.section_id: OperativeSection(
            section_id=marker.section_id, source_document_id=base.document_id,
        )
        for marker in normalized.sections
    }
    applied: list[AmendmentEffect] = []
    unapplied: list[tuple[AmendmentEffect, str]] = []

    for effect in document_set.effects():
        bounds = _section_bounds(normalized, effect.target_section)
        if bounds is None:
            unapplied.append((effect, "target section is not in the base text"))
            continue
        start_offset, end_offset = bounds
        body = bodies.get(
            effect.target_section, normalized.text[start_offset:end_offset]
        )

        if effect.kind == "restate":
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

    document = NormalizedDocument(
        document_id=f"{base.document_id}+operative",
        source_path=document_set.base.path,
        source_format=base.source_format,
        text=operative.text,
        tables=tables,
        pages=base.pages,
        sections=detect_sections(operative.text),
        meta={
            "operative_as_of": str(document_set.operative_as_of or ""),
            "chain_length": str(len(document_set.chain)),
        },
    )
    return document
