"""The precedence graph: which provision wins when two of them disagree.

Credit agreements are full of cross-references that reorder themselves.
"Notwithstanding Section 6.01" means this provision beats 6.01; "subject to
Section 7.02" means 7.02 beats this one. Both are one phrase long and both
invert the plain reading of the text around them.

The graph normalises every such phrase into one direction -- an edge from A to
B means **B governs over A** -- so that precedence can be read off rather than
inferred clause by clause. It feeds ``ExtractedField.precedence_basis``, which
is how a variant ordering gets a justification instead of being a guess.

Cycles are findings. Two provisions that each claim to override the other is a
drafting error, and the honest output is to say so rather than to pick.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..ingest.normalize import NormalizedDocument
from ..models.core import Span

EdgeKind = Literal["notwithstanding", "subject_to", "except_as", "in_lieu_of"]

#: Each pattern captures the section it points at. ``inverts`` says whether the
#: *citing* provision wins (True) or the *cited* one does (False).
_EDGE_PATTERNS: tuple[tuple[str, EdgeKind, bool], ...] = (
    (r"[Nn]otwithstanding\s+(?:anything\s+(?:to\s+the\s+contrary\s+)?"
     r"(?:contained\s+)?in\s+)?Section\s+(?P<section>\d+\.\d+[A-Za-z]?)",
     "notwithstanding", True),
    (r"[Ss]ubject\s+to\s+(?:the\s+(?:terms\s+of\s+|provisions\s+of\s+)?)?"
     r"Section\s+(?P<section>\d+\.\d+[A-Za-z]?)",
     "subject_to", False),
    (r"[Ee]xcept\s+as\s+(?:otherwise\s+)?(?:set\s+forth|provided|permitted)\s+"
     r"in\s+Section\s+(?P<section>\d+\.\d+[A-Za-z]?)",
     "except_as", False),
    (r"[Ii]n\s+lieu\s+of\s+Section\s+(?P<section>\d+\.\d+[A-Za-z]?)",
     "in_lieu_of", True),
)

#: A proviso chain this deep is a family F02 finding in its own right: by the
#: fourth "provided that" nobody reads the clause correctly.
STACKED_PROVISO_DEPTH = 4

_PROVISO_RE = re.compile(r"\bprovided\s*,?\s*(?:however\s*,?\s*)?that\b",
                         re.IGNORECASE)


class PrecedenceEdge(BaseModel):
    """``governing`` wins over ``subordinate``."""

    subordinate: str
    governing: str
    kind: EdgeKind
    span: Span
    phrase: str = ""

    def describe(self) -> str:
        return (
            f"Section {self.governing} governs over Section "
            f"{self.subordinate} ({self.kind}: {self.phrase!r})"
        )


class ProvisoStack(BaseModel):
    """A run of provisos inside one provision."""

    section_id: str | None
    depth: int
    span: Span


class PrecedenceGraph(BaseModel):
    """Directed graph over sections. An edge points at the winner."""

    edges: list[PrecedenceEdge] = Field(default_factory=list)
    provisos: list[ProvisoStack] = Field(default_factory=list)

    def governing_over(self, section_id: str) -> list[PrecedenceEdge]:
        return [e for e in self.edges if e.subordinate == section_id]

    def governed_by(self, section_id: str) -> list[PrecedenceEdge]:
        return [e for e in self.edges if e.governing == section_id]

    def basis_for(self, section_id: str | None) -> str | None:
        """A citable justification for ordering a field's variants."""
        if section_id is None:
            return None
        winners = self.governing_over(section_id)
        if not winners:
            return None
        return "; ".join(edge.describe() for edge in winners[:3])

    def cycles(self) -> list[list[str]]:
        """Provisions that each claim to override the other."""
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.subordinate, set()).add(edge.governing)

        found: set[tuple[str, ...]] = set()
        stack: list[str] = []
        on_stack: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str) -> None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for successor in sorted(adjacency.get(node, ())):
                if successor in on_stack:
                    cycle = stack[stack.index(successor):]
                    rotation = cycle.index(min(cycle))
                    found.add(tuple(cycle[rotation:] + cycle[:rotation]))
                elif successor not in visited:
                    dfs(successor)
            stack.pop()
            on_stack.discard(node)

        for node in sorted(adjacency):
            if node not in visited:
                dfs(node)
        return [list(c) for c in sorted(found)]

    def deep_provisos(self, depth: int = STACKED_PROVISO_DEPTH) -> list[ProvisoStack]:
        return [p for p in self.provisos if p.depth >= depth]

    def stats(self) -> dict:
        return {
            "edges": len(self.edges),
            "sections_with_overrides": sorted(
                {e.subordinate for e in self.edges}
            ),
            "cycles": self.cycles(),
            "deepest_proviso_stack": max(
                (p.depth for p in self.provisos), default=0
            ),
            "stacked_provisos": [
                {"section": p.section_id, "depth": p.depth}
                for p in self.deep_provisos()
            ],
        }


def build_precedence_graph(doc: NormalizedDocument) -> PrecedenceGraph:
    """Index every precedence phrase in the document."""
    edges: list[PrecedenceEdge] = []
    for pattern, kind, citing_wins in _EDGE_PATTERNS:
        for match in re.finditer(pattern, doc.text):
            target = match.group("section")
            span = doc.span(match.start(), match.end())
            source = span.section_id
            if source is None or source == target:
                continue
            subordinate, governing = (
                (target, source) if citing_wins else (source, target)
            )
            edges.append(PrecedenceEdge(
                subordinate=subordinate, governing=governing, kind=kind,
                span=span, phrase=" ".join(match.group(0).split()),
            ))

    provisos: list[ProvisoStack] = []
    for index, marker in enumerate(doc.sections):
        end = marker.end or (
            doc.sections[index + 1].offset
            if index + 1 < len(doc.sections) else len(doc.text)
        )
        body = doc.text[marker.offset:end]
        # Counted within a single provision, not across a whole article. Four
        # provisos stacked in one sentence is genuinely unreadable; four spread
        # across twenty-five separate definitions is an ordinary Article I, and
        # counting the section total flags every credit agreement ever written.
        best_depth = 0
        best_bounds = (marker.offset, end)
        cursor = 0
        for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z(])", body):
            depth = len(_PROVISO_RE.findall(sentence))
            if depth > best_depth:
                best_depth = depth
                start = body.find(sentence, cursor)
                if start >= 0:
                    best_bounds = (
                        marker.offset + start,
                        marker.offset + start + len(sentence),
                    )
            cursor += len(sentence)
        if best_depth == 0:
            continue
        provisos.append(ProvisoStack(
            section_id=marker.section_id,
            depth=best_depth,
            span=doc.span(*best_bounds),
        ))
    return PrecedenceGraph(edges=edges, provisos=provisos)
