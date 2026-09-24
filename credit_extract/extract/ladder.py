"""The extraction ladder: rules, then an oriented model pass, then a sweep.

WHAT THIS REPLACES
==================

``run_passes`` walked every chunk of every segmentation and asked the backend
for every field in the registry, every time. :class:`LayeredBackend` narrowed
that per *chunk* -- the model was handed the fields the rules had not settled
**in that chunk** -- which is not the same as the fields still open in the
*document*. A field settled in chunk 3 was asked for again in chunks 4 through
592.

The cost of that is not theoretical. On Essential Properties, 519,130
characters and 365 defined terms:

    structural 103 + sliding 124 + definitional 365  =  592 chunks per pass
    592 x 3 passes                                   =  1,776 model calls

each one carrying all fifty-six targets. The model was asked whether a chunk
about notice addresses contains the MFN sunset 1,776 times.

THE LADDER
==========

**Stage 1 -- deterministic.** Tables parse in Python; ``OFFLINE_RULES`` take
the fields that are cheap and unambiguous. Unchanged, and still first.

**Stage 2 -- orientation, driven by the definition graph.** A credit agreement
puts its answers in its definitions. Twenty-three of the fifty-six registry
fields name the defined terms they depend on in ``FieldSpec.definition_anchors``,
and the graph can walk each one's closure. So rather than sending 365
definitional chunks and hoping the right one lands, this stage resolves each
open field's anchors, takes the closure, and assembles one context per group of
fields that share it. Fields whose answers live in Article I are settled here,
from the text that defines them, before a single section is swept.

**Stage 3 -- the section sweep.** Whatever stage 2 leaves open is looked for
section by section, over the structural and sliding views only -- see
:data:`SWEEP_SEGMENTATIONS` for why the definitional one is not among them.
Every section is visited -- see ABSENCE below -- but each visit asks only for
the fields still open at that point, so the schema shrinks as the document is
read rather than staying at fifty-six for half a million characters.

    flat walk   592 chunks x 3 passes  =  1,776 calls,  9.5M characters
    ladder       5 + 103 + 124         =    232 calls,  1.26M characters

WHAT THE MODEL IS SHOWN, AND WHAT THAT COSTS
============================================

The model sees what the cheaper tiers found, so it can overturn a wrong regex
instead of silently duplicating it. That is worth having and it is not free:
a pass that agrees with a value it was *shown* is not independent evidence for
that value. One wrong rule, shown to three passes, would otherwise come back
as three-way corroboration and a high confidence.

So every candidate records the prior it saw in ``Candidate.anchored_on``,
:attr:`~.passes.Candidate.echoes` is the test, and ``reconcile`` excludes
echoes from *support* while keeping them in the record. An echo is weak
evidence, not no evidence. A pass that was shown a value and returned a
different one -- :attr:`~.passes.Candidate.overturns` -- is the strongest
signal either tier produces, and is why the prior is shown at all.

The prior shown to every pass is the **deterministic tier's and the
orientation stage's**, identical for both sweeps. A sweep never sees the other
sweep's findings, or the two would stop being independent and ``support``
would mean nothing.

Corroboration is relative to the views that actually ran. With the
definitional sweep dropped there are two, so a value both sweeps reach at
different spans is fully corroborated -- ``_extraction_confidence`` divides by
the real view count rather than a literal 3, which previously reported "we ran
fewer views" as "this value was less corroborated".

ABSENCE, AND WHY THE SWEEP DOES NOT STOP EARLY
==============================================

Validator C is the only thing entitled to call a field
``absent_from_document``. That is a CONFIDENT status: a wrong one is a silent
error and counts against the family budget. A sweep that stopped as soon as a
field settled would leave the *unsettled* fields having been searched in only
part of the document, and "absent" would quietly mean "we stopped looking".

So the sweep visits every section, and :class:`LadderResult` records per field
which sections were actually searched. Validator C can then assert coverage
rather than assume it, and a run that was cut short by a budget says which
fields it cut short.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..models.core import CostLedger, Span
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from .passes import (
    Candidate, ExtractionFailed, _extract_with_priors, table_candidates,
)

#: How many characters of definition text one orientation call may carry.
#: A closure can reach the whole of Article I on a densely cross-referenced
#: agreement, and a context that large stops being a context.
ORIENTATION_MAX_CHARS = 24_000

#: Fields sharing this many anchors are asked for together. Grouping by the
#: exact anchor set would make one call per field; grouping by the first
#: anchor puts the fields that depend on the same defined term in one request,
#: which is the whole reason the graph is worth consulting.
_GROUP_ON_FIRST_ANCHOR = True

#: The views the section sweep walks. The definitional segmentation is
#: deliberately **not** among them, and this is the largest single cost
#: decision in the pipeline.
#:
#: That segmentation is one chunk per defined term, so on Essential Properties
#: it is 365 chunks and 1.9 million characters -- 61% of the calls and 60% of
#: the text of a full run -- and what it sends is mostly boilerplate that
#: cannot carry a registry field at all:
#:
#:     [Affiliate]       11,139 chars   "as to any Person, any other Person
#:                                       that, directly or indirectly, is in
#:                                       control of..."
#:     [Bail-In Action]   2,620 chars   EU resolution-authority boilerplate
#:     [Bankruptcy Code]  1,610 chars   a statutory reference
#:
#: each sent with all fifty-six targets attached. The registry names sixteen
#: definition anchors and five of them resolve in that document, so 360 of the
#: 365 chunks were sent on spec. Even the one that should pay -- Applicable
#: Margin -- opens with 600 characters of table of contents, because use-sites
#: include the TOC entry.
#:
#: The orientation stage does that job properly: it starts from the fields,
#: resolves their anchors through the graph, and sends the closure. Five calls,
#: and the Closing Date chunk is 33 characters asking for one field rather than
#: 13,944 asking for fifty-six.
#:
#: The cost of dropping it is a third independent view, so ``support`` now tops
#: out at two. That was chosen deliberately over keeping a third pass that
#: differed only by temperature, which ``plan_passes`` itself describes as the
#: weaker signal.
SWEEP_SEGMENTATIONS: tuple[str, ...] = ("structural", "sliding")


@dataclass
class StageRecord:
    """What one stage did, so the ladder can be measured rather than believed."""

    stage: str
    calls: int = 0
    settled: set[str] = dc_field(default_factory=set)
    chunks_visited: int = 0
    unread: list[str] = dc_field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.stage}: {self.calls} call(s) over {self.chunks_visited} "
            f"chunk(s), settled {len(self.settled)}"
        )


@dataclass
class LadderResult:
    """Everything one pass of the ladder produced."""

    candidates: list[Candidate] = dc_field(default_factory=list)
    cost: CostLedger = dc_field(default_factory=CostLedger)
    stages: list[StageRecord] = dc_field(default_factory=list)
    #: field -> chunk ids actually searched for it. The evidence behind any
    #: later claim that the field is absent from the document.
    searched: dict[str, set[str]] = dc_field(default_factory=dict)
    contributing_chunks: set[str] = dc_field(default_factory=set)
    #: True when every open field was offered every section. False means a
    #: budget stopped the sweep, and absence claims are not safe.
    swept_exhaustively: bool = True

    def merge(self, other: "LadderResult") -> None:
        self.candidates.extend(other.candidates)
        self.cost.merge(other.cost)
        self.stages.extend(other.stages)
        self.contributing_chunks |= other.contributing_chunks
        self.swept_exhaustively &= other.swept_exhaustively
        for name, chunks in other.searched.items():
            self.searched.setdefault(name, set()).update(chunks)

    def calls(self) -> int:
        return sum(stage.calls for stage in self.stages)


def _prior_keys(candidates: Iterable[Candidate]) -> dict[str, str]:
    """field -> the value key the cheaper tier settled on, best first.

    Only the deterministic tier's and the orientation stage's answers are
    shown, and the same ones to every pass, so a pass never sees another
    pass's sweep and the three stay independent of each other.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.value is None:
            continue
        held = best.get(candidate.field)
        if held is None or candidate.confidence > held.confidence:
            best[candidate.field] = candidate
    return {name: c.key() for name, c in best.items()}


def _prior_spans(candidates: Iterable[Candidate]) -> dict[str, Span]:
    """field -> the span the prior value was read from.

    Carried alongside the value because the span is what separates a parrot
    from a second reading. A pass shown "$1,300,000,000" that returns it while
    citing the *same* clause has told us nothing new. One that returns it
    citing a different clause has found the figure somewhere else in the
    document, which is corroboration however it was prompted.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.value is None or candidate.span is None:
            continue
        held = best.get(candidate.field)
        if held is None or candidate.confidence > held.confidence:
            best[candidate.field] = candidate
    return {name: c.span for name, c in best.items() if c.span is not None}


def _prior_text(specs: list[FieldSpec], priors: dict[str, str]) -> str:
    """The block shown to the model describing what has already been found.

    Phrased as something to check rather than something to accept. The
    difference is not cosmetic: a model told "this is the answer" confirms it,
    and a model told "an earlier pass read this, verify or correct it" is
    being asked to do the thing that makes showing it worthwhile.
    """
    rows = [
        f"  {spec.name}: an earlier pass read {priors[spec.name]!r}"
        for spec in specs if spec.name in priors
    ]
    return "\n".join(rows)


def _tag(
    candidates: list[Candidate],
    priors: dict[str, str],
    prior_spans: dict[str, Span] | None = None,
) -> list[Candidate]:
    """Record, on each candidate, the prior its pass was shown.

    Done here rather than in the backends so that every backend -- the live
    model, the recorded replay, the rules -- participates without knowing
    about it, and so that a backend cannot forget.
    """
    spans = prior_spans or {}
    for candidate in candidates:
        if candidate.field in priors:
            candidate.anchored_on = priors[candidate.field]
            candidate.anchored_span = spans.get(candidate.field)
    return candidates


#: Fields at this criticality are looked for in the sections even when the
#: definitions already answered them.
#:
#: The sweep is gap-driven, which is the point -- but a field answered in the
#: orientation stage and then skipped by all three sweeps has exactly one
#: reading behind it, from one view of the document. For pricing, principal,
#: maturity and the leverage covenant that is too thin: those are where a
#: wrong answer costs money, and where the agreement stating a figure in its
#: definitions *and* in its operative sections is the corroboration worth
#: paying for. Twenty-five of fifty-six fields, so the cost is bounded and
#: known rather than open-ended.
CORROBORATE_AT_OR_ABOVE = 5


def open_fields(
    specs: list[FieldSpec], candidates: Iterable[Candidate]
) -> list[FieldSpec]:
    """The targets no candidate has produced a value for yet.

    Document-wide, which is the whole difference from the per-chunk narrowing
    ``LayeredBackend`` does: a field settled in section 3 is not asked for
    again in section 4.
    """
    found = {c.field for c in candidates if c.value is not None}
    return [spec for spec in specs if spec.name not in found]


# ---------------------------------------------------------------------------
# Stage 2: orientation over the definition graph
# ---------------------------------------------------------------------------


def _closure_chunk(
    graph: Any, doc: NormalizedDocument, anchor: str, label: str
) -> Chunk | None:
    """A chunk covering a defined term's closure, spans intact.

    Built from the definition nodes rather than from a character window, so a
    quote cited against it resolves to a true offset in the document -- the
    same guarantee ``segment_definitional`` gives, over a set of terms chosen
    by what the open fields depend on rather than by iterating every term.
    """
    resolved = graph.resolve(anchor)
    if resolved is None:
        return None
    spans: list[Span] = []
    parts: list[str] = []
    used = 0
    for term in graph.closure(resolved):
        node = graph.get(term)
        span = getattr(node, "span", None) if node is not None else None
        if span is None:
            continue
        if used + (span.end - span.start) > ORIENTATION_MAX_CHARS:
            break
        spans.append(span)
        parts.append(doc.text[span.start:span.end])
        used += span.end - span.start
    if not spans:
        return None
    return Chunk(
        chunk_id=f"orient:{resolved}",
        segmentation="definitional",
        label=label,
        spans=spans,
        text=Chunk.SEPARATOR.join(parts),
    )


def _anchor_groups(specs: list[FieldSpec]) -> dict[str, list[FieldSpec]]:
    """Open fields grouped by the defined term they hang off.

    One call per group rather than per field: the fields that depend on
    "Applicable Margin" want the same closure in front of them, and sending it
    once is the saving the graph makes possible.
    """
    groups: dict[str, list[FieldSpec]] = {}
    for spec in specs:
        if not spec.definition_anchors:
            continue
        anchors = (
            spec.definition_anchors[:1] if _GROUP_ON_FIRST_ANCHOR
            else spec.definition_anchors
        )
        for anchor in anchors:
            groups.setdefault(anchor, []).append(spec)
    return groups


def orientation_stage(
    doc: NormalizedDocument,
    graph: Any,
    specs: list[FieldSpec],
    model: Any,
    pass_id: str,
    priors: dict[str, str],
    budget_usd: float | None = None,
    spent: CostLedger | None = None,
    prior_spans: dict[str, Span] | None = None,
) -> LadderResult:
    """Ask the definitions for the fields whose answers live in them."""
    result = LadderResult()
    record = StageRecord(stage="orient")
    result.stages.append(record)
    if graph is None or model is None or not specs:
        return result

    for anchor, group in sorted(_anchor_groups(specs).items()):
        still_open = [s for s in group if s.name not in
                      {c.field for c in result.candidates if c.value is not None}]
        if not still_open:
            continue
        chunk = _closure_chunk(graph, doc, anchor, label=anchor)
        if chunk is None:
            # The anchor is named by a field and defined nowhere in this
            # document. That is F01's undefined_term_used and is worth the
            # record rather than a silent skip.
            record.unread.append(f"{anchor}: not defined in this document")
            continue
        if _over_budget(budget_usd, spent, result.cost):
            result.swept_exhaustively = False
            break
        try:
            found, cost = _extract_with_priors(
                model, doc, chunk, still_open,
                # The closure text is the context here; it IS the definitions
                # this stage exists to read. The priors go in their own block.
                context="",
                pass_id=f"{pass_id}/orient",
                priors=_prior_text(still_open, priors),
            )
        except ExtractionFailed as exc:
            record.unread.append(f"{chunk.chunk_id}: {exc}")
            continue
        record.calls += 1
        record.chunks_visited += 1
        result.cost.merge(cost)
        for spec in still_open:
            result.searched.setdefault(spec.name, set()).add(chunk.chunk_id)
        tagged = _tag(found, priors, prior_spans)
        if tagged:
            result.contributing_chunks.add(chunk.chunk_id)
        record.settled.update(c.field for c in tagged if c.value is not None)
        result.candidates.extend(tagged)
    return result


# ---------------------------------------------------------------------------
# Stage 3: the section sweep
# ---------------------------------------------------------------------------


def _over_budget(
    budget_usd: float | None, spent: CostLedger | None, own: CostLedger
) -> bool:
    if budget_usd is None:
        return False
    total = own.total_usd + (spent.total_usd if spent is not None else 0.0)
    return total >= budget_usd


def section_stage(
    doc: NormalizedDocument,
    chunks: list[Chunk],
    specs: list[FieldSpec],
    model: Any,
    pass_id: str,
    priors: dict[str, str],
    settled_already: set[str],
    budget_usd: float | None = None,
    spent: CostLedger | None = None,
    prior_spans: dict[str, Span] | None = None,
) -> LadderResult:
    """Walk every section, asking only for what is still open.

    Exhaustive by design. The sweep does not stop when the last field settles,
    because the fields that never settle are exactly the ones an absence claim
    will be made about, and an absence claim is only worth what the search
    behind it was worth.
    """
    result = LadderResult()
    record = StageRecord(stage="sweep")
    result.stages.append(record)
    if model is None:
        return result

    found_values = set(settled_already)
    for chunk in chunks:
        remaining = [
            s for s in specs
            if s.name not in found_values
            or s.criticality >= CORROBORATE_AT_OR_ABOVE
        ]
        if not remaining:
            # Everything settled. The sweep continues anyway -- but with
            # nothing to ask, so it costs nothing and the loop just records
            # that the section was reached.
            record.chunks_visited += 1
            continue
        if _over_budget(budget_usd, spent, result.cost):
            result.swept_exhaustively = False
            break
        try:
            candidates, cost = _extract_with_priors(
                model, doc, chunk, remaining, context="",
                pass_id=f"{pass_id}/sweep",
                priors=_prior_text(remaining, priors),
            )
        except ExtractionFailed as exc:
            record.unread.append(f"{chunk.chunk_id}: {exc}")
            record.chunks_visited += 1
            continue
        record.calls += 1
        record.chunks_visited += 1
        result.cost.merge(cost)
        for spec in remaining:
            result.searched.setdefault(spec.name, set()).add(chunk.chunk_id)
        tagged = _tag(candidates, priors, prior_spans)
        if tagged:
            result.contributing_chunks.add(chunk.chunk_id)
        for candidate in tagged:
            if candidate.value is not None:
                found_values.add(candidate.field)
                record.settled.add(candidate.field)
        result.candidates.extend(tagged)
    return result


# ---------------------------------------------------------------------------
# The whole ladder, for one pass
# ---------------------------------------------------------------------------


def run_ladder(
    doc: NormalizedDocument,
    chunks: list[Chunk],
    specs: list[FieldSpec],
    model: Any,
    graph: Any,
    pass_id: str,
    priors: dict[str, str],
    prior_spans: dict[str, Span] | None = None,
    already_answered: set[str] | None = None,
    budget_usd: float | None = None,
    spent: CostLedger | None = None,
) -> LadderResult:
    """One pass: sweep this pass's sections for whatever is still missing.

    Neither stage 1 nor stage 2 runs here, and for the same reason. The rules
    read the whole document once and the orientation stage reads the
    definitions once; running either per pass would spend three times the
    calls to produce one view's worth of evidence, and would look like
    three-way corroboration for a value one reader produced once. The caller
    runs both and hands their answers in as ``priors``.

    A field either of them answered is NOT closed to the model here. It is
    shown with its value, precisely so this pass can overturn it -- a regex
    quoting the wrong span is the thing most worth overturning, and the sweep
    is where that is caught. What the sweep narrows on is its *own* progress:
    a field found in section 3 is not asked for again in section 4, so the
    schema shrinks as the document is read.

    The exception is ``CORROBORATE_AT_OR_ABOVE``. Deal-defining fields stay
    open through the whole sweep even once found, because a figure stated in
    the definitions and again in the operative sections is the corroboration
    worth paying for, and those are the fields where a wrong answer costs
    money.
    """
    return section_stage(
        doc, chunks, specs, model, pass_id, priors,
        settled_already=set(already_answered or ()),
        budget_usd=budget_usd, spent=spent, prior_spans=prior_spans,
    )


def deterministic_stage(
    doc: NormalizedDocument,
    chunks: list[Chunk],
    specs: list[FieldSpec],
    rules: Any,
    include_tables: bool = True,
) -> LadderResult:
    """Stage 1, over the whole document once.

    Run once rather than once per pass: the rules are deterministic, so three
    runs would produce three identical answers and count as corroboration for
    a value only one reader ever produced.
    """
    result = LadderResult()
    record = StageRecord(stage="rules")
    result.stages.append(record)

    if include_tables:
        found = table_candidates(doc)
        result.candidates.extend(found)
        result.cost.deterministic_calls += 1
        record.calls += 1
        record.settled.update(c.field for c in found if c.value is not None)

    if rules is None:
        return result
    for chunk in chunks:
        try:
            found, cost = rules.extract(
                doc, chunk, specs, "", pass_id="deterministic:rules"
            )
        except ExtractionFailed as exc:  # pragma: no cover - rules do not raise
            record.unread.append(f"{chunk.chunk_id}: {exc}")
            continue
        record.chunks_visited += 1
        result.cost.merge(cost)
        if found:
            result.contributing_chunks.add(chunk.chunk_id)
        record.settled.update(c.field for c in found if c.value is not None)
        result.candidates.extend(found)
    return result


def registry_specs() -> list[FieldSpec]:
    return list(FIELD_REGISTRY.values())
