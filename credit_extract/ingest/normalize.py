"""Ingest .htm/.html/.mht/.pdf/.txt into one normalized character space.

Every span produced anywhere downstream indexes into
``NormalizedDocument.text``. That is the whole contract: one document, one
offset space, one way to quote it. The sidecar map turns any offset back into
(page, section) for human review.
"""

from __future__ import annotations

import email
import hashlib
import re
import unicodedata
from bisect import bisect_right
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models.core import Span
from .tables import Cell, Table

# ---------------------------------------------------------------------------
# Character-level normalization
# ---------------------------------------------------------------------------

_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-",
    "−": "-", "­": "", "​": "", "‌": "", "‍": "",
    "﻿": "", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", "　": " ",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "…": "...", "·": "*",
}
_TRANS = str.maketrans(_CHAR_MAP)


def normalize_chars(raw: str) -> str:
    """Fold the ligature/quote/space noise EDGAR HTML carries."""
    text = unicodedata.normalize("NFKC", raw.translate(_TRANS))
    return text.translate(_TRANS)


# ---------------------------------------------------------------------------
# Offset sidecar
# ---------------------------------------------------------------------------


class PageBreak(BaseModel):
    offset: int
    page: int


class SectionMarker(BaseModel):
    offset: int
    section_id: str
    title: str = ""
    level: str = "section"  # article | section
    end: int | None = None


_RESERVED_TITLE_RE = re.compile(
    r"^\[?\s*(?:Reserved|Intentionally\s+(?:Omitted|Left\s+Blank)|Omitted)\s*\]?",
    re.IGNORECASE,
)


class RedlineRange(BaseModel):
    """A run of text that a blackline marks as deleted or newly inserted.

    A blackline amendment says what it changes by *typography*: the old words
    are struck through, the new words are bold and double-underlined. Render
    that to plain text and both survive, adjacent and indistinguishable --
    ``Up to U.S. $ 2,150,000,000 2,250,000,000`` is a real line from a real
    filing, and a first-match parser reads it as $2.15bn, the number the
    amendment just deleted. Nothing in the plain text signals a problem, which
    is what makes it the worst class of error this pipeline can make.

    Struck runs are therefore never written into the normalized text at all.
    The offset space *is* the operative text, so every span downstream quotes
    what the agreement currently says with no downstream code needing to know
    a blackline was involved; ``start == end`` marks the point the deletion was
    excised at, and ``text`` keeps the deleted words for audit. Inserted runs
    are written like any other text and their range is recorded as provenance.
    """

    start: int
    end: int
    kind: Literal["struck", "inserted"]
    text: str = ""

    @property
    def excised(self) -> bool:
        return self.kind == "struck"


class NormalizedDocument(BaseModel):
    """Normalized text plus everything needed to locate any offset."""

    document_id: str
    source_path: str
    source_format: str
    text: str
    tables: list[Table] = Field(default_factory=list)
    pages: list[PageBreak] = Field(default_factory=list)
    sections: list[SectionMarker] = Field(default_factory=list)
    redlines: list[RedlineRange] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)

    # -- blackline -----------------------------------------------------------

    @property
    def is_blackline(self) -> bool:
        """True when this document struck text out, i.e. carries deletions."""
        return any(r.excised for r in self.redlines)

    def deletions(self) -> list[RedlineRange]:
        """The runs excluded from the text, in the order they were excised."""
        return [r for r in self.redlines if r.excised]

    def insertions(self) -> list[RedlineRange]:
        return [r for r in self.redlines if not r.excised]

    def deleted_near(self, start: int, end: int, pad: int = 40) -> list[RedlineRange]:
        """Deletions excised from inside or beside a span.

        What a reader needs when a figure changed: the span quotes the new
        number, and this says which number it replaced.
        """
        return [
            r for r in self.deletions()
            if start - pad <= r.start <= end + pad
        ]

    def blackline_summary(self) -> dict[str, Any]:
        return {
            "is_blackline": self.is_blackline,
            "deletions": len(self.deletions()),
            "deleted_characters": sum(len(r.text) for r in self.deletions()),
            "insertions": len(self.insertions()),
        }

    # -- lookup -------------------------------------------------------------

    def _page_at(self, offset: int) -> int | None:
        if not self.pages:
            return None
        idx = bisect_right([p.offset for p in self.pages], offset) - 1
        return self.pages[max(idx, 0)].page

    def _section_at(self, offset: int) -> str | None:
        if not self.sections:
            return None
        idx = bisect_right([s.offset for s in self.sections], offset) - 1
        return self.sections[idx].section_id if idx >= 0 else None

    def locate(self, offset: int) -> tuple[int | None, str | None]:
        """offset -> (page number, section number)."""
        return self._page_at(offset), self._section_at(offset)

    def span(self, start: int, end: int, segmentation: str | None = None) -> Span:
        """The only sanctioned way to build a Span: text is read from offsets.

        The span carries this document's id, because once a document *set* is
        in play an offset alone is not a citation -- the same offset means
        something different in the base agreement and in Amendment No. 3.
        """
        start = max(0, start)
        end = min(len(self.text), end)
        page, section = self.locate(start)
        return Span(
            start=start,
            end=end,
            text=self.text[start:end],
            page=page,
            section_id=section,
            segmentation=segmentation,
            document_id=self.document_id,
        )

    def slice(self, start: int, end: int) -> str:
        return self.text[max(0, start):min(len(self.text), end)]

    def context(self, span: Span, pad: int = 500) -> Span:
        lo, hi = span.expand(pad, len(self.text))
        return self.span(lo, hi)

    def find_all(self, pattern: str | re.Pattern[str], flags: int = 0) -> list[Span]:
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
        return [self.span(m.start(), m.end()) for m in rx.finditer(self.text)]

    def find_first(self, pattern: str | re.Pattern[str], flags: int = 0) -> Span | None:
        spans = self.find_all(pattern, flags)
        return spans[0] if spans else None

    def reserved_sections(self) -> set[str]:
        """Sections headed "[Reserved]" or "[Intentionally Omitted]".

        They exist as numbers and hold nothing. A cross-reference to one is a
        different finding from a cross-reference to a number that was never
        used, and telling a reader the section does not exist when the document
        plainly prints it is how a true finding gets dismissed as a bug.
        """
        found: set[str] = set()
        for marker in self.sections:
            if _RESERVED_TITLE_RE.match(marker.title.strip()):
                found.add(marker.section_id)
                continue
            if marker.title.strip():
                continue
            # The heading and the "[Reserved]" that follows it are often two
            # separate blocks, so the title captures nothing at all.
            line_end = self.text.find("\n", marker.offset)
            if line_end < 0:
                continue
            body = self.text[line_end: line_end + 60].strip()
            if _RESERVED_TITLE_RE.match(body):
                found.add(marker.section_id)
        return found

    def section_span(self, section_id: str) -> Span | None:
        for i, sec in enumerate(self.sections):
            if sec.section_id == section_id:
                end = sec.end or (
                    self.sections[i + 1].offset
                    if i + 1 < len(self.sections)
                    else len(self.text)
                )
                return self.span(sec.offset, end)
        return None

    def tables_in(self, start: int, end: int) -> list[Table]:
        return [t for t in self.tables if t.start >= start and t.end <= end]

    def to_sidecar(self) -> dict:
        """Serializable offset -> (page, section) map."""
        return {
            "document_id": self.document_id,
            "length": len(self.text),
            "pages": [p.model_dump() for p in self.pages],
            "sections": [s.model_dump() for s in self.sections],
            "tables": [
                {"table_id": t.table_id, "start": t.start, "end": t.end,
                 "rows": t.n_rows, "cols": t.n_cols, "caption": t.caption}
                for t in self.tables
            ],
            "redlines": [r.model_dump() for r in self.redlines],
        }


# ---------------------------------------------------------------------------
# Text builder: the single writer into the offset space
# ---------------------------------------------------------------------------


class _Builder:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._len = 0

    @property
    def offset(self) -> int:
        return self._len

    def _raw(self, chunk: str) -> tuple[int, int]:
        start = self._len
        self._parts.append(chunk)
        self._len += len(chunk)
        return start, self._len

    def write(self, text: str) -> tuple[int, int] | None:
        """Append collapsed text; returns the span it occupies.

        All whitespace inside a text node collapses, newlines included. A line
        break inside a paragraph of source HTML is soft wrapping, not
        structure, and preserving it fragments sentences ("purchase\\nmoney")
        so that no anchored pattern can match across the break. Real block
        structure is written explicitly by ``block()`` and ``linebreak()``.
        """
        collapsed = re.sub(r"\s+", " ", normalize_chars(text)).strip()
        if not collapsed:
            return None
        if self._len and not self._parts[-1].endswith(("\n", " ")):
            self._raw(" ")
        return self._raw(collapsed)

    def block(self) -> None:
        """End the current block. Collapses consecutive block breaks."""
        tail = "".join(self._parts[-3:]) if self._parts else ""
        if not self._len:
            return
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self._raw("\n")
        else:
            self._raw("\n\n")

    def linebreak(self) -> None:
        if self._len and not "".join(self._parts[-2:]).endswith("\n"):
            self._raw("\n")

    def value(self) -> str:
        return "".join(self._parts)


# ---------------------------------------------------------------------------
# Format readers
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote",
    "section", "article", "center", "dd", "dt", "pre",
}
_SKIP_TAGS = {"script", "style", "head", "meta", "link", "title"}
_PAGE_MARK_RE = re.compile(r"^\s*(?:page\s+)?(\d{1,4})\s*$", re.IGNORECASE)

_STRUCK_TAGS = {"s", "strike", "del"}
_BOLD_RE = re.compile(r"font-weight:(?:bold|[6-9]\d\d)")
#: Any declared colour that is not black. A blackline picks out its insertions
#: in colour as well as underlining them; ordinary emphasis does not.
_COLOURED_RE = re.compile(r"(?<!background-)color:#(?!000000\b|000\b)[0-9a-f]{3,6}")


def _mark_of(node) -> str | None:
    """Whether a tag marks its contents as deleted or newly inserted.

    Deletion is read strictly -- ``line-through`` and the strike tags mean one
    thing in a legal document and nothing else. Insertion is read narrowly on
    purpose: ``text-decoration:underline`` alone is how every section heading
    in an EDGAR filing is set, so an underline only counts as an insertion when
    it is also bold or coloured, which is what "bold and double-underlined"
    amounts to once a converter has flattened the double rule to a single one.
    """
    name = node.name.lower()
    if name in _STRUCK_TAGS:
        return "struck"
    style = (node.get("style") or "").lower().replace(" ", "")
    if "line-through" in style:
        return "struck"
    if name == "ins":
        return "inserted"
    if "underline" in style and (_BOLD_RE.search(style) or _COLOURED_RE.search(style)):
        return "inserted"
    return None


#: Tried in order when an MHT part declares no charset. A browser-saved
#: archive of an EDGAR filing frequently omits it, and the bytes are Windows
#: code page 1252 -- decoding them as UTF-8 turns every smart quote into a
#: replacement character, which then breaks defined-term detection because the
#: quotation marks around every defined term are gone.
_MHT_FALLBACK_CHARSETS = ("utf-8", "cp1252", "latin-1")


def _decode_part(payload: bytes, declared: str | None) -> str:
    for charset in ([declared] if declared else []) + list(_MHT_FALLBACK_CHARSETS):
        try:
            return payload.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _read_mht(path: Path) -> str:
    message = email.message_from_bytes(path.read_bytes())
    best = ""
    for part in message.walk():
        if part.get_content_type() in ("text/html", "application/xhtml+xml"):
            candidate = _decode_part(
                part.get_payload(decode=True) or b"",
                part.get_content_charset(),
            )
            if len(candidate) > len(best):
                best = candidate
    if not best:
        best = path.read_bytes().decode("utf-8", errors="replace")
    return best


def _ingest_html(
    html: str, builder: _Builder
) -> tuple[list[Table], list[PageBreak], list[RedlineRange]]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(list(_SKIP_TAGS)):
        tag.decompose()

    tables: list[Table] = []
    pages: list[PageBreak] = [PageBreak(offset=0, page=1)]
    redlines: list[RedlineRange] = []
    counters = {"table": 0, "page": 1}

    def emit(text: str, mark: str | None) -> tuple[int, int] | None:
        if mark == "struck":
            # Excised, not written. The deleted words are kept in the sidecar
            # so the change is auditable, but they never enter the offset space
            # and so can never be quoted back as an operative term.
            deleted = re.sub(r"\s+", " ", normalize_chars(text)).strip()
            if deleted:
                redlines.append(RedlineRange(
                    start=builder.offset, end=builder.offset,
                    kind="struck", text=deleted,
                ))
            return None
        written = builder.write(text)
        if written is not None and mark is not None and written[1] > written[0]:
            redlines.append(RedlineRange(
                start=written[0], end=written[1], kind=mark,
                text=builder.value()[written[0]:written[1]][:200],
            ))
        return written

    def marked_runs(node, mark: str | None = None):
        """Descendant strings, each paired with the markup governing it."""
        if isinstance(node, NavigableString):
            yield str(node), mark
            return
        if not isinstance(node, Tag) or node.name.lower() in _SKIP_TAGS:
            return
        mark = _mark_of(node) or mark
        if node.name.lower() == "br":
            yield " ", mark
            return
        for child in node.children:
            yield from marked_runs(child, mark)

    def emit_table(node: Tag) -> None:
        counters["table"] += 1
        table_id = f"T{counters['table']:03d}"
        start = builder.offset
        cells: list[Cell] = []
        rows = node.find_all("tr")
        for r_idx, row in enumerate(rows):
            raw_cells = row.find_all(["td", "th"])
            col = 0
            wrote_any = False
            for cell_node in raw_cells:
                if wrote_any:
                    builder._raw(" | ")
                # Written run by run rather than through one ``get_text`` call,
                # so that a cell holding a struck figure beside its replacement
                # keeps the two distinguishable. Flattening here is how a
                # blackline amortization table loses every old instalment into
                # the new one.
                written: tuple[int, int] | None = None
                for run, mark in marked_runs(cell_node):
                    piece = emit(run, mark)
                    if piece is not None:
                        written = (
                            piece if written is None else (written[0], piece[1])
                        )
                if written is None:
                    # Empty spacer cell: keep a zero-width anchor at the cursor
                    written = (builder.offset, builder.offset)
                cells.append(
                    Cell(
                        row=r_idx,
                        col=col,
                        text=builder.value()[written[0]:written[1]].strip(),
                        start=written[0],
                        end=max(written[1], written[0] + 1),
                        is_header=cell_node.name == "th",
                        colspan=int(cell_node.get("colspan", 1) or 1),
                        rowspan=int(cell_node.get("rowspan", 1) or 1),
                    )
                )
                col += 1
                wrote_any = True
            if wrote_any:
                builder.linebreak()
        builder.block()
        end = builder.offset
        caption_node = node.find("caption")
        table = Table(
            table_id=table_id,
            start=start,
            end=end,
            cells=[c for c in cells if c.text],
            caption=caption_node.get_text(" ", strip=True) if caption_node else None,
        )
        if table.cells:
            _infer_header_row(table)
            tables.append(table)

    def walk(node, mark: str | None = None) -> None:
        if isinstance(node, NavigableString):
            emit(str(node), mark)
            return
        if not isinstance(node, Tag):
            return
        name = node.name.lower()
        if name in _SKIP_TAGS:
            return
        if name == "table":
            builder.block()
            emit_table(node)
            return
        if name == "br":
            builder.linebreak()
            return
        if name == "hr":
            builder.block()
            counters["page"] += 1
            pages.append(PageBreak(offset=builder.offset, page=counters["page"]))
            return
        mark = _mark_of(node) or mark
        is_block = name in _BLOCK_TAGS
        style = (node.get("style") or "").lower()
        page_break = "page-break-before" in style or "page-break-after" in style
        if is_block:
            builder.block()
        for child in node.children:
            walk(child, mark)
        if is_block:
            builder.block()
        if page_break:
            counters["page"] += 1
            pages.append(PageBreak(offset=builder.offset, page=counters["page"]))

    body = soup.body or soup
    for child in body.children:
        walk(child)
    return tables, pages, _merge_redlines(redlines)


def _merge_redlines(ranges: list[RedlineRange]) -> list[RedlineRange]:
    """Coalesce adjacent runs of the same kind, keeping source order.

    A blackline splits one deleted phrase across a dozen ``<font>`` runs; left
    un-merged the register reads as a dozen deletions of two words each, which
    overstates how much changed and hides what the change was.
    """
    merged: list[RedlineRange] = []
    for region in ranges:
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.kind == region.kind
            and region.start <= last.end + 1
        ):
            last.end = max(last.end, region.end)
            joiner = "" if last.text.endswith(" ") or region.text.startswith(" ") else " "
            last.text = (last.text + joiner + region.text)[:400]
            continue
        merged.append(region.model_copy())
    return merged


def _infer_header_row(table: Table) -> None:
    """Mark row 0 as header when it is labels and row 1 is data."""
    from .tables import parse_date, parse_money

    if any(c.is_header for c in table.cells):
        return
    row0 = [c for c in table.cells if c.row == 0]
    row1 = [c for c in table.cells if c.row == 1]
    if not row0 or not row1:
        return

    def numericish(cells: list[Cell]) -> bool:
        return any(
            parse_money(c.text) is not None or parse_date(c.text) is not None
            for c in cells
        )

    if not numericish(row0) and numericish(row1):
        for cell in table.cells:
            if cell.row == 0:
                cell.is_header = True


def _ingest_pdf(path: Path, builder: _Builder) -> list[PageBreak]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[PageBreak] = []
    for index, page in enumerate(reader.pages, start=1):
        pages.append(PageBreak(offset=builder.offset, page=index))
        for line in (page.extract_text() or "").splitlines():
            builder.write(line)
            builder.linebreak()
        builder.block()
    return pages or [PageBreak(offset=0, page=1)]


def _ingest_text(raw: str, builder: _Builder) -> list[PageBreak]:
    pages = [PageBreak(offset=0, page=1)]
    page_no = 1
    for chunk in raw.split("\f"):
        if page_no > 1:
            pages.append(PageBreak(offset=builder.offset, page=page_no))
        for line in chunk.splitlines():
            if not line.strip():
                builder.block()
                continue
            builder.write(line)
            builder.linebreak()
        builder.block()
        page_no += 1
    return pages


# ---------------------------------------------------------------------------
# Section detection (post-pass over the normalized text)
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(
    r"^[ \t]*ARTICLE\s+(?P<num>[IVXLC]+|\d+)\b[ \t]*(?P<title>[^\n]{0,80})",
    re.MULTILINE,
)
#: Some agreements have no articles: their top-level divisions are "SECTION 3"
#: and the provisions inside are "3.1", "3.2". Without this, "SECTION 3
#: [RESERVED]" leaves no marker, and a reference to Section 3.1 is reported as
#: pointing at a number the document never used -- when in fact the document
#: prints the division and reserves it, which is what a reader needs told.
_NUMBERED_DIVISION_RE = re.compile(
    r"^[ \t]*SECTION\s+(?P<num>\d{1,2})(?!\.\d)\b[ \t.:]*(?P<title>[^\n]{0,80})",
    re.MULTILINE,
)
_SECTION_RE = re.compile(
    r"^[ \t]*(?:SECTION|Section)\s+(?P<num>\d+\.\d+[A-Za-z]?)\b[ \t.:]*"
    r"(?P<title>[^\n]{0,90})",
    re.MULTILINE,
)
#: Plenty of agreements head their sections "1.1 Defined Terms" with no
#: "Section" keyword at all. Requiring two digits after the decimal -- which is
#: what it takes to keep "1.5 times Consolidated EBITDA" out -- finds 2.10 and
#: misses 1.1, so in one real filing two thirds of the sections were invisible
#: and every cross-reference to Section 1.1 was reported as dangling. The title
#: carries the discriminator instead: a heading is a capitalised phrase, and a
#: multiple or a ratio is followed by a lower-case word.
#: ``[`` is in the leading character class for "6.11 [Reserved]". A reserved
#: section is still a section: skipping it makes every reference to it look
#: like a reference to nothing, which is the right finding for the wrong
#: reason and stops the report saying what a reader needs to hear.
_BARE_SECTION_RE = re.compile(
    r"^[ \t]*(?P<num>\d+\.\d{1,2}[A-Za-z]?)[ \t]+(?P<title>[A-Z\[][^\n]{2,90})",
    re.MULTILINE,
)
#: Amendments number their own provisions "1. Defined Terms." rather than
#: "2.10". Used only when no decimal-numbered sections were found at all,
#: because in a base agreement this pattern would match list items in prose.
_SIMPLE_SECTION_RE = re.compile(
    r"^[ \t]*(?P<num>\d{1,2})\.[ \t]+(?P<title>[A-Z][^\n]{2,90})",
    re.MULTILINE,
)


def detect_sections(text: str) -> list[SectionMarker]:
    markers: dict[int, SectionMarker] = {}
    for match in _ARTICLE_RE.finditer(text):
        markers[match.start()] = SectionMarker(
            offset=match.start(),
            section_id=f"ARTICLE {match.group('num')}",
            title=match.group("title").strip(" .:-"),
            level="article",
        )
    for match in _NUMBERED_DIVISION_RE.finditer(text):
        if match.start() in markers:
            continue
        markers[match.start()] = SectionMarker(
            offset=match.start(),
            section_id=f"SECTION {match.group('num')}",
            title=match.group("title").strip(" .:-"),
            level="article",
        )
    for rx in (_SECTION_RE, _BARE_SECTION_RE):
        for match in rx.finditer(text):
            if match.start() in markers:
                continue
            markers[match.start()] = SectionMarker(
                offset=match.start(),
                section_id=match.group("num"),
                title=match.group("title").strip(" .:-"),
                level="section",
            )
    if not markers:
        for match in _SIMPLE_SECTION_RE.finditer(text):
            markers[match.start()] = SectionMarker(
                offset=match.start(),
                section_id=match.group("num"),
                title=match.group("title").strip(" .:-"),
                level="section",
            )
    ordered = [markers[k] for k in sorted(markers)]
    ordered = _drop_toc_markers(text, ordered)
    for i, marker in enumerate(ordered):
        marker.end = ordered[i + 1].offset if i + 1 < len(ordered) else len(text)
    return ordered


def _drop_toc_markers(text: str, ordered: list[SectionMarker]) -> list[SectionMarker]:
    """Strip table-of-contents entries.

    A TOC line is indistinguishable from a heading by pattern alone, and taking
    the TOC entry as the section start silently mislocates every span in the
    document. The discriminator is structural: every TOC entry repeats later in
    the body, so the TOC ends at the first marker whose id never recurs.
    """
    anchor = text.upper().find("TABLE OF CONTENTS")
    if anchor < 0:
        return ordered
    later_counts: dict[str, int] = {}
    for marker in ordered:
        later_counts[marker.section_id] = later_counts.get(marker.section_id, 0) + 1
    kept: list[SectionMarker] = []
    in_toc = False
    seen: dict[str, int] = {}
    for marker in ordered:
        seen[marker.section_id] = seen.get(marker.section_id, 0) + 1
        if marker.offset < anchor:
            kept.append(marker)
            continue
        remaining = later_counts[marker.section_id] - seen[marker.section_id]
        if not in_toc and remaining > 0:
            in_toc = True
        if in_toc:
            if remaining > 0:
                continue          # still inside the contents listing
            in_toc = False         # first non-recurring id: body starts here
        kept.append(marker)
    return kept


def _detect_page_marks(text: str, pages: list[PageBreak]) -> list[PageBreak]:
    """Trust explicit page numbers over <hr> counting when they look sane."""
    found: list[PageBreak] = []
    for match in re.finditer(r"\n[ \t]*(\d{1,4})[ \t]*\n", text):
        number = int(match.group(1))
        if 1 <= number <= 2000:
            found.append(PageBreak(offset=match.start(), page=number))
    monotonic = [p for i, p in enumerate(found) if i == 0 or p.page == found[i - 1].page + 1]
    if len(monotonic) >= max(3, len(pages) // 2):
        return [PageBreak(offset=0, page=1), *monotonic]
    return pages


SUPPORTED_SUFFIXES = {".htm", ".html", ".xhtml", ".mht", ".mhtml", ".pdf", ".txt"}


def ingest(path: str | Path, document_id: str | None = None) -> NormalizedDocument:
    """Read a source document into the normalized character space."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported format {suffix!r}; expected one of "
            f"{sorted(SUPPORTED_SUFFIXES)}"
        )

    builder = _Builder()
    tables: list[Table] = []
    redlines: list[RedlineRange] = []
    if suffix in (".htm", ".html", ".xhtml"):
        raw = path.read_bytes().decode("utf-8", errors="replace")
        tables, pages, redlines = _ingest_html(raw, builder)
        fmt = "html"
    elif suffix in (".mht", ".mhtml"):
        tables, pages, redlines = _ingest_html(_read_mht(path), builder)
        fmt = "mht"
    elif suffix == ".pdf":
        pages = _ingest_pdf(path, builder)
        fmt = "pdf"
    else:
        pages = _ingest_text(
            path.read_text(encoding="utf-8", errors="replace"), builder
        )
        fmt = "txt"

    text = builder.value()
    sections = detect_sections(text)
    if fmt in ("html", "mht", "txt"):
        pages = _detect_page_marks(text, pages)

    doc = NormalizedDocument(
        document_id=document_id
        or hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        source_path=str(path),
        source_format=fmt,
        text=text,
        tables=tables,
        pages=pages,
        sections=sections,
        redlines=redlines,
        meta={"bytes": str(path.stat().st_size)},
    )
    for table in doc.tables:
        page, section = doc.locate(table.start)
        table.page, table.section_id = page, section
    return doc
