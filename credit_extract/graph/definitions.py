"""The defined-term graph.

A credit agreement is a program written in English. ``Senior Secured First Lien
Net Leverage Ratio`` resolves to ``Consolidated Senior Secured First Lien Net
Indebtedness``, which resolves to ``Consolidated Total Debt``, which carries its
own provisos and cash-netting caps. Extracting the leverage covenant without
that chain in context is extracting a symbol, not a number.

Long-tail errors cluster at depth >= 3, so this module computes the transitive
closure of every extraction target and hands the whole closure to the
extractor. It also does two things that matter more than the closure itself:

* flags cycles, which exist and are usually drafting errors, and
* flags terms whose meaning escapes the four corners of the agreement.

That second one is Trap 3. ``Consolidated EBITDA`` permits add-backs "set out
in the Sponsor Model" -- a spreadsheet delivered before closing that is
expressly not a Loan Document and was never filed. The magnitude of that
add-back is unknowable from this document, so the correct output is
``external_reference``, never a number.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, PrivateAttr

from ..ingest.normalize import NormalizedDocument
from ..models.core import Span

#: ``"Term" means ...``, ``"Term" shall mean ...``, ``"Term" has the meaning``
#: -- and ``"Term": ...``, which is a whole drafting convention rather than a
#: variant.
#:
#: Measured over 25 real agreements, ten use the colon form and four use it
#: almost exclusively: 317 to 491 colon definitions against 5 to 9 written
#: with "means". Without it the graph saw one defined term in a 571,000
#: character agreement and three in a 588,000 character one -- so the closure,
#: the cycle detection and the external-reference flagging, which is most of
#: what this module is for, had nothing to work on and said so silently.
#:
#: The colon alternative is safe here only because the search is confined to
#: the definitions article: a quoted phrase followed by a colon is common
#: enough in ordinary prose, and unconstrained it would manufacture terms out
#: of every quotation in the document.
#: The whitespace inside the quotes is not cosmetic. EDGAR filings set a
#: defined term as its own styled run, so the quotes and the term arrive as
#: separate nodes and the normalizer writes ``" Floor "``, not ``"Floor"``.
#: Requiring the term to start immediately after the quote matched one
#: definition in a 571,000-character agreement whose definitions article is
#: 189,000 characters long.
#: Defined terms are quoted, and usually with double quotes. An amendment that
#: restates a section quotes the whole restated block, so every definition
#: inside it drops to single quotes:
#:
#:     Section 1.01 of the Original Agreement is hereby amended in its
#:     entirety to read as follows: " ... 'Revolving Credit Termination Date'
#:     means the earliest to occur of (a) August 27, 2029 ... "
#:
#: Requiring double quotes made that content invisible. On the one document in
#: the corpus drafted this way the graph found 3 terms in 73,805 characters,
#: against 19 definitions actually present, and five of the seven failing
#: assertions in the whole F05 family were values sitting inside those
#: restated blocks. The corpus counts say how narrow this is: 15,075
#: double-quoted definitions against 19 single-quoted, and all 19 in that one
#: filing. A rule for one document in a hundred is worth having when the
#: document is the one the family's failures come from.
#:
#: The closing quote must not be an apostrophe, so a single-quoted term may
#: not end in one, and "s' means" cannot open a definition.
_DEFINITION_RE = re.compile(
    r'(?:"\s*(?P<term>[A-Z][^"\n]{1,90}?)\s*"'
    r"|'\s*(?P<sterm>[A-Z][^'\n]{1,90}?[^'\s\n])\s*'(?!\w))\s*"
    r'(?:(?P<verb>means|shall mean|has the meaning|shall have the meaning)\b'
    r'|(?P<colon>:)\s)',
)

#: Signals that a defined term *is* a document outside this agreement.
_EXTERNAL_DOCUMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"is not a Loan Document", "expressly excluded from the Loan Documents"),
    (r"not attached hereto", "expressly not attached"),
    (r"prepared by the Sponsor", "third-party model"),
    (r"delivered to the Administrative Agent on\b", "delivered outside the agreement"),
    (r"as separately agreed", "terms agreed outside the agreement"),
    (r"\bfinancial model\b", "financial model"),
    (r"\bdisclosure letter\b", "side letter"),
)

#: Signals that a definition *depends on* something outside the agreement.
_EXTERNAL_CITATION_RE = re.compile(
    r"\bSchedule\s+[\dIVXLC][\w.()\-]*"
    r"|\bExhibit\s+[A-Z][\w.()\-]*"
    r"|\bAnnex\s+[A-Z\d][\w.()\-]*"
    r"|\bas separately agreed\b",
    re.IGNORECASE,
)


class DefinitionNode(BaseModel):
    """One defined term: where it is defined, and where it is used."""

    term: str
    span: Span                                  # the definition itself
    body: str
    uses: set[str] = Field(default_factory=set)         # terms this one cites
    used_by: set[str] = Field(default_factory=set)      # terms citing this one
    use_sites: list[Span] = Field(default_factory=list)  # every mention document-wide
    is_external_document: bool = False
    external_reasons: list[str] = Field(default_factory=list)
    external_citations: list[str] = Field(default_factory=list)


class ExternalReference(BaseModel):
    """A term whose magnitude cannot be determined from this document."""

    term: str
    via: list[str]                 # dependency path from term to the external node
    document: str                  # the document the value actually lives in
    reason: str
    span: Span


class DefinitionGraph(BaseModel):
    """A directed graph of defined terms. Edges mean "uses"."""

    nodes: dict[str, DefinitionNode] = Field(default_factory=dict)
    article_span: Span | None = None
    #: Memo for :meth:`depths`, computed once per graph. Excluded from
    #: serialization: it is derivable, and a report carrying a second copy of
    #: every term is noise.
    _depths: dict[str, int] | None = PrivateAttr(default=None)

    # -- basic access -------------------------------------------------------

    def __contains__(self, term: str) -> bool:
        return term in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def get(self, term: str) -> DefinitionNode | None:
        return self.nodes.get(term)

    def resolve(self, term: str) -> str | None:
        """Case-insensitive lookup, so callers need not match the drafting."""
        if term in self.nodes:
            return term
        folded = term.casefold()
        for name in self.nodes:
            if name.casefold() == folded:
                return name
        return None

    # -- closure ------------------------------------------------------------

    def closure(self, term: str, max_depth: int = 8) -> list[str]:
        """Every term reachable from ``term``, breadth-first, cycle-safe.

        Returned in dependency order with ``term`` first, so the context handed
        to an extractor reads top-down the way a lawyer would read it.
        """
        start = self.resolve(term)
        if start is None:
            return []
        order: list[str] = [start]
        seen = {start}
        frontier = [start]
        for _ in range(max_depth):
            nxt: list[str] = []
            for name in frontier:
                node = self.nodes.get(name)
                if node is None:
                    continue
                for used in sorted(node.uses):
                    if used in seen:
                        continue
                    seen.add(used)
                    order.append(used)
                    nxt.append(used)
            if not nxt:
                break
            frontier = nxt
        return order

    def depth(self, term: str) -> int:
        """Longest dependency chain below ``term``."""
        start = self.resolve(term)
        return self.depths().get(start, 0) if start is not None else 0

    def depths(self) -> dict[str, int]:
        """Longest chain below every term, in one linear pass.

        This was a recursive walk carrying the path as a set, which is the
        textbook longest-*simple*-path search: correct, and exponential. A
        definitions article cross-references itself densely enough that the
        number of simple paths is astronomical, and ``stats`` ran it once per
        term. One real agreement -- 944,000 characters, an ordinary Article I
        -- took over an hour and had not finished.

        Because a well-drafted definition graph is a DAG, no path can revisit
        a term anyway, so memoising by term is exact there and linear. Cycles
        do occur, and they are reported as drafting errors by ``cycles()``; an
        edge back into a term already on the current path contributes nothing
        rather than being followed, which is what the path set achieved. For a
        cyclic graph that is a lower bound rather than the true longest simple
        path, which is the right trade: the number is a readability signal,
        and the cycle itself is the finding.

        Iterative rather than recursive, because a chain a few hundred
        definitions long is ordinary and the recursion limit is not.

        ``uses`` is a set, and its iteration order varies between processes
        under hash randomisation. Because an edge back into a term already on
        the current path contributes nothing, a different traversal order
        breaks a cycle at a different edge and reports a different depth: the
        same agreement gave 197, 196 and 194 terms at depth three or more on
        three consecutive runs. Sorting fixes the order, and a number that
        moves on identical input is worth less than no number.
        """
        if self._depths is not None:
            return self._depths

        depth: dict[str, int] = {}
        on_stack: set[str] = set()
        for root in self.nodes:
            if root in depth:
                continue
            stack: list[tuple[str, bool]] = [(root, False)]
            while stack:
                name, finished = stack.pop()
                if finished:
                    on_stack.discard(name)
                    node = self.nodes.get(name)
                    best = 0
                    for used in sorted(node.uses) if node else ():
                        if used in on_stack or used not in self.nodes:
                            continue          # back edge, or a dangling cite
                        best = max(best, 1 + depth.get(used, 0))
                    depth[name] = best
                    continue
                if name in depth or name in on_stack:
                    continue
                on_stack.add(name)
                stack.append((name, True))
                node = self.nodes.get(name)
                for used in sorted(node.uses) if node else ():
                    if used in self.nodes and used not in depth:
                        stack.append((used, False))
        self._depths = depth
        return depth

    def context_for(self, term: str, max_chars: int = 24_000) -> str:
        """The closure rendered as text, for an extractor's prompt."""
        parts: list[str] = []
        budget = max_chars
        for name in self.closure(term):
            node = self.nodes.get(name)
            if node is None:
                continue
            block = f'"{node.term}" {node.body.strip()}'
            if len(block) > budget:
                block = block[: max(0, budget)] + " [...]"
            parts.append(block)
            budget -= len(block)
            if budget <= 0:
                break
        return "\n\n".join(parts)

    # -- findings -----------------------------------------------------------

    def cycles(self) -> list[list[str]]:
        """Every simple cycle, as ordered term lists. Usually drafting errors."""
        found: set[tuple[str, ...]] = set()
        stack: list[str] = []
        on_stack: set[str] = set()
        visited: set[str] = set()

        def dfs(name: str) -> None:
            visited.add(name)
            stack.append(name)
            on_stack.add(name)
            node = self.nodes.get(name)
            for used in sorted(node.uses) if node else []:
                if used not in self.nodes:
                    continue
                if used in on_stack:
                    cycle = stack[stack.index(used):]
                    rotation = cycle.index(min(cycle))
                    found.add(tuple(cycle[rotation:] + cycle[:rotation]))
                elif used not in visited:
                    dfs(used)
            stack.pop()
            on_stack.discard(name)

        for name in sorted(self.nodes):
            if name not in visited:
                dfs(name)
        return [list(c) for c in sorted(found)]

    def external_references(self, term: str) -> list[ExternalReference]:
        """Paths from ``term`` to anything outside the four corners."""
        start = self.resolve(term)
        if start is None:
            return []
        out: list[ExternalReference] = []
        seen = {start}
        queue: list[tuple[str, list[str]]] = [(start, [start])]
        while queue:
            name, path = queue.pop(0)
            node = self.nodes.get(name)
            if node is None:
                continue
            if node.is_external_document and name != start:
                out.append(ExternalReference(
                    term=start, via=path, document=node.term,
                    reason="; ".join(node.external_reasons) or "external document",
                    span=node.span,
                ))
            for citation in node.external_citations:
                out.append(ExternalReference(
                    term=start, via=path, document=citation,
                    reason="definition cites a schedule or exhibit not extracted",
                    span=node.span,
                ))
            for used in sorted(node.uses):
                if used in seen:
                    continue
                seen.add(used)
                queue.append((used, [*path, used]))
        return out

    def external_document_terms(self) -> list[str]:
        return sorted(n.term for n in self.nodes.values() if n.is_external_document)

    def stats(self) -> dict:
        depths = self.depths()
        return {
            "terms": len(self.nodes),
            "edges": sum(len(n.uses) for n in self.nodes.values()),
            "max_depth": max(depths.values(), default=0),
            "terms_at_depth_3_or_more": sorted(
                t for t, d in depths.items() if d >= 3
            ),
            "cycles": self.cycles(),
            "external_documents": self.external_document_terms(),
        }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _definitions_region(doc: NormalizedDocument) -> tuple[int, int]:
    """Bound the definitions article; fall back to the whole document."""
    for index, section in enumerate(doc.sections):
        title = section.title.lower()
        is_definitions = "defined term" in title or title.strip() == "definitions"
        if not (is_definitions or section.section_id.upper() == "ARTICLE I"):
            continue
        for later in doc.sections[index + 1:]:
            if later.level == "article":
                return section.offset, later.offset
        return section.offset, len(doc.text)
    return 0, len(doc.text)


def _term_pattern(terms: list[str]) -> re.Pattern[str] | None:
    """One alternation over every term, longest first so matches are maximal."""
    if not terms:
        return None
    ordered = sorted(terms, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b"
    )


def build_definition_graph(doc: NormalizedDocument) -> DefinitionGraph:
    """Parse the definitions article into a directed graph of defined terms."""
    start, end = _definitions_region(doc)
    region = doc.text[start:end]

    matches = list(_DEFINITION_RE.finditer(region))
    nodes: dict[str, DefinitionNode] = {}
    for index, match in enumerate(matches):
        term = (match.group("term") or match.group("sterm")).strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(region)
        body = region[body_start:body_end].strip()
        if not body:
            continue
        # Keep the first definition of a term; amendments restate, they do not
        # redefine, and the restatement is caught by the override validator.
        if term in nodes:
            continue
        nodes[term] = DefinitionNode(
            term=term,
            span=doc.span(start + match.start(), start + body_end),
            body=body,
        )

    pattern = _term_pattern(list(nodes))
    if pattern is not None:
        for node in nodes.values():
            for hit in pattern.finditer(node.body):
                used = hit.group(0)
                if used == node.term or used not in nodes:
                    continue
                node.uses.add(used)
                nodes[used].used_by.add(node.term)
        # Use sites across the whole document, not just the definitions article.
        for hit in pattern.finditer(doc.text):
            term = hit.group(0)
            node = nodes.get(term)
            if node is None:
                continue
            if node.span.start <= hit.start() < node.span.end:
                continue
            node.use_sites.append(doc.span(hit.start(), hit.end()))

    for node in nodes.values():
        for rx, reason in _EXTERNAL_DOCUMENT_PATTERNS:
            if re.search(rx, node.body, re.IGNORECASE):
                node.is_external_document = True
                node.external_reasons.append(reason)
        node.external_citations = sorted(
            {m.group(0) for m in _EXTERNAL_CITATION_RE.finditer(node.body)}
        )

    return DefinitionGraph(
        nodes=nodes,
        article_span=doc.span(start, end) if end > start else None,
    )
