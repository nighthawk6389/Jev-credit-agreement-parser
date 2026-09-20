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


class NormalizedDocument(BaseModel):
    """Normalized text plus everything needed to locate any offset."""

    document_id: str
    source_path: str
    source_format: str
    text: str
    tables: list[Table] = Field(default_factory=list)
    pages: list[PageBreak] = Field(default_factory=list)
    sections: list[SectionMarker] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)

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
        """The only sanctioned way to build a Span: text is read from offsets."""
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


def _read_mht(path: Path) -> str:
    message = email.message_from_bytes(path.read_bytes())
    best = ""
    for part in message.walk():
        if part.get_content_type() in ("text/html", "application/xhtml+xml"):
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True) or b""
            candidate = payload.decode(charset, errors="replace")
            if len(candidate) > len(best):
                best = candidate
    if not best:
        best = path.read_text(encoding="utf-8", errors="replace")
    return best


def _ingest_html(html: str, builder: _Builder) -> tuple[list[Table], list[PageBreak]]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(list(_SKIP_TAGS)):
        tag.decompose()

    tables: list[Table] = []
    pages: list[PageBreak] = [PageBreak(offset=0, page=1)]
    counters = {"table": 0, "page": 1}

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
                text = cell_node.get_text(" ", strip=True)
                if wrote_any:
                    builder._raw(" | ")
                written = builder.write(text) if text else None
                if written is None:
                    # Empty spacer cell: keep a zero-width anchor at the cursor
                    written = (builder.offset, builder.offset)
                cells.append(
                    Cell(
                        row=r_idx,
                        col=col,
                        text=normalize_chars(text).strip(),
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

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            builder.write(str(node))
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
        is_block = name in _BLOCK_TAGS
        style = (node.get("style") or "").lower()
        page_break = "page-break-before" in style or "page-break-after" in style
        if is_block:
            builder.block()
        for child in node.children:
            walk(child)
        if is_block:
            builder.block()
        if page_break:
            counters["page"] += 1
            pages.append(PageBreak(offset=builder.offset, page=counters["page"]))

    body = soup.body or soup
    for child in body.children:
        walk(child)
    return tables, pages


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
_SECTION_RE = re.compile(
    r"^[ \t]*(?:SECTION|Section)\s+(?P<num>\d+\.\d+[A-Za-z]?)\b[ \t.:]*"
    r"(?P<title>[^\n]{0,90})",
    re.MULTILINE,
)
_BARE_SECTION_RE = re.compile(
    r"^[ \t]*(?P<num>\d+\.\d{2}[A-Za-z]?)[ \t]+(?P<title>[A-Z][^\n]{2,90})",
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
    if suffix in (".htm", ".html", ".xhtml"):
        raw = path.read_bytes().decode("utf-8", errors="replace")
        tables, pages = _ingest_html(raw, builder)
        fmt = "html"
    elif suffix in (".mht", ".mhtml"):
        tables, pages = _ingest_html(_read_mht(path), builder)
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
        meta={"bytes": str(path.stat().st_size)},
    )
    for table in doc.tables:
        page, section = doc.locate(table.start)
        table.page, table.section_id = page, section
    return doc
