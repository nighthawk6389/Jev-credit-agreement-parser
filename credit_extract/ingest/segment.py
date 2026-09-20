"""Three independent segmentations of the same document.

This is a hard requirement, not an optimization, because different
segmentations fail differently:

* a clause that straddles a section boundary is invisible to the structural
  segmentation and obvious to the sliding window;
* a definition whose meaning is changed by a proviso forty pages later is
  invisible to the sliding window and obvious to the definitional one.

Running all three and treating their disagreement as a finding -- rather than
as noise to be averaged away -- is what makes reconciliation informative. If
all three segmentations agreed all the time, there would be no point running
more than one.
"""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from ..models.core import Span
from .normalize import NormalizedDocument

SegmentationKind = Literal["structural", "definitional", "sliding"]

#: Chunk sizing. The sliding window is deliberately indifferent to structure.
STRUCTURAL_MAX_CHARS = 12_000
SLIDING_WINDOW_CHARS = 6_000
SLIDING_OVERLAP = 0.30
USE_SITE_PAD = 600


class Chunk(BaseModel):
    """A unit of text handed to one extraction or validation pass.

    A chunk may cover several disjoint regions -- a definitional chunk covers a
    definition plus every site where the term is used, and those are scattered
    through the document. Keeping the regions separate rather than flattening
    to one span is what lets a cited quote resolve back to a true offset.
    """

    SEPARATOR: ClassVar[str] = "\n[...]\n"

    chunk_id: str
    segmentation: SegmentationKind
    label: str
    spans: list[Span] = Field(default_factory=list)
    text: str = ""
    table_ids: list[str] = Field(default_factory=list)

    @property
    def span(self) -> Span:
        return self.spans[0]

    @property
    def start(self) -> int:
        return min(s.start for s in self.spans)

    @property
    def end(self) -> int:
        return max(s.end for s in self.spans)

    def __len__(self) -> int:
        return len(self.text)

    def locate(self, doc: NormalizedDocument, quote: str) -> Span | None:
        """Resolve a verbatim quote back to real document offsets.

        Searched region by region, so a quote can never be credited to an
        offset range the chunk did not actually contain.
        """
        if not quote.strip():
            return None
        needle = quote.strip()
        loose = re.compile(r"\s+".join(re.escape(p) for p in needle.split()))
        for span in self.spans:
            window = doc.text[span.start:span.end]
            index = window.find(needle)
            if index >= 0:
                return doc.span(span.start + index, span.start + index + len(needle))
            match = loose.search(window)
            if match:
                return doc.span(span.start + match.start(), span.start + match.end())
        return None


def _tables_within(doc: NormalizedDocument, spans: list[Span]) -> list[str]:
    ids: list[str] = []
    for table in doc.tables:
        if any(table.start >= s.start and table.end <= s.end for s in spans):
            ids.append(table.table_id)
    return ids


def _build(
    doc: NormalizedDocument,
    chunk_id: str,
    kind: SegmentationKind,
    label: str,
    spans: list[Span],
) -> Chunk:
    tagged = [
        doc.span(s.start, s.end, segmentation=kind) for s in spans if s.end > s.start
    ]
    return Chunk(
        chunk_id=chunk_id,
        segmentation=kind,
        label=label,
        spans=tagged,
        text=Chunk.SEPARATOR.join(s.text for s in tagged),
        table_ids=_tables_within(doc, tagged),
    )


def _merge_spans(spans: list[Span], gap: int = 200) -> list[tuple[int, int]]:
    """Coalesce overlapping or near-touching regions."""
    if not spans:
        return []
    ordered = sorted(((s.start, s.end) for s in spans))
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


# ---------------------------------------------------------------------------
# 1. Structural
# ---------------------------------------------------------------------------


def segment_structural(
    doc: NormalizedDocument, max_chars: int = STRUCTURAL_MAX_CHARS
) -> list[Chunk]:
    """One chunk per Article/Section boundary, splitting oversized sections.

    A section longer than the budget is split on paragraph boundaries rather
    than mid-sentence, and the parts keep the section label so a finding still
    reports as "Section 2.10".
    """
    if not doc.sections:
        return [_build(doc, "struct:whole", "structural", "document",
                       [doc.span(0, len(doc.text))])]
    chunks: list[Chunk] = []
    for section in doc.sections:
        start = section.offset
        end = section.end or len(doc.text)
        if end - start <= max_chars:
            chunks.append(_build(
                doc, f"struct:{section.section_id}", "structural",
                section.section_id, [doc.span(start, end)],
            ))
            continue
        part = 1
        cursor = start
        while cursor < end:
            stop = min(cursor + max_chars, end)
            if stop < end:
                boundary = doc.text.rfind("\n\n", cursor + max_chars // 2, stop)
                if boundary > cursor:
                    stop = boundary
            chunks.append(_build(
                doc, f"struct:{section.section_id}#{part}", "structural",
                section.section_id, [doc.span(cursor, stop)],
            ))
            cursor = stop
            part += 1
    return chunks


# ---------------------------------------------------------------------------
# 2. Definitional
# ---------------------------------------------------------------------------


def segment_definitional(
    doc: NormalizedDocument,
    graph,
    pad: int = USE_SITE_PAD,
    max_use_sites: int = 12,
) -> list[Chunk]:
    """Per defined term: the definition plus a window at every use site.

    ``graph`` is a :class:`~credit_extract.graph.definitions.DefinitionGraph`;
    it is passed in rather than imported to keep ingest free of a dependency on
    the graph layer.
    """
    chunks: list[Chunk] = []
    for term, node in sorted(graph.nodes.items()):
        regions: list[Span] = [node.span]
        for site in node.use_sites[:max_use_sites]:
            lo, hi = site.expand(pad, len(doc.text))
            regions.append(doc.span(lo, hi))
        merged = _merge_spans(regions)
        chunks.append(_build(
            doc,
            f"defn:{term}",
            "definitional",
            term,
            [doc.span(a, b) for a, b in merged],
        ))
    return chunks


# ---------------------------------------------------------------------------
# 3. Sliding window
# ---------------------------------------------------------------------------


def segment_sliding(
    doc: NormalizedDocument,
    window: int = SLIDING_WINDOW_CHARS,
    overlap: float = SLIDING_OVERLAP,
) -> list[Chunk]:
    """Fixed-size windows with 30% overlap, ignoring document structure.

    Structure-blindness is the feature: this is the only segmentation that can
    see a clause spanning a section boundary.
    """
    if not 0.0 <= overlap < 1.0:
        raise ValueError(f"overlap must be in [0, 1); got {overlap}")
    stride = max(1, int(window * (1.0 - overlap)))
    length = len(doc.text)
    chunks: list[Chunk] = []
    index = 0
    cursor = 0
    while cursor < length:
        stop = min(cursor + window, length)
        chunks.append(_build(
            doc, f"slide:{index:04d}", "sliding", f"window {index}",
            [doc.span(cursor, stop)],
        ))
        if stop >= length:
            break
        cursor += stride
        index += 1
    return chunks


# ---------------------------------------------------------------------------


def segment_all(doc: NormalizedDocument, graph=None) -> dict[SegmentationKind, list[Chunk]]:
    """Run all three segmentations. The orphan sweep runs over the union."""
    out: dict[SegmentationKind, list[Chunk]] = {
        "structural": segment_structural(doc),
        "sliding": segment_sliding(doc),
    }
    out["definitional"] = segment_definitional(doc, graph) if graph else []
    return out


def coverage(doc: NormalizedDocument, chunks: list[Chunk]) -> float:
    """Fraction of the document covered by at least one chunk."""
    if not doc.text:
        return 0.0
    merged = _merge_spans([s for c in chunks for s in c.spans], gap=0)
    covered = sum(b - a for a, b in merged)
    return covered / len(doc.text)
