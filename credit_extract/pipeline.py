"""End-to-end orchestration.

    ingest -> normalize -> segment (3 ways) -> definition graph -> extract (N passes)
           -> reconcile -> Jev validate -> invariant check -> calibrate -> report

The escalation ladder is respected in order and never skipped upward: nothing
reaches a paid tier that a free one has already settled. Deterministic table
parsing and the Python invariants run first and cost nothing; the Jev sweep
runs over every chunk for cents; a targeted re-read only fires on chunks the
sweep flagged; and human review is reserved for what survives all of it, ranked
by economic materiality.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .extract.passes import (
    Candidate, ExtractionBackend, OfflineRuleBackend, parse_amortization_schedule,
    parse_covenant_grid, parse_hardcoded_ebitda, run_passes,
)
from .extract.reconcile import reconcile
from .graph.definitions import build_definition_graph
from .ingest.normalize import NormalizedDocument, ingest
from .ingest.segment import Chunk, coverage, segment_all
from .models.actus_map import (
    ActusContract, ActusMapping, diff_schedule, generate_schedule, map_facility,
)
from .models.core import (
    CostLedger, DocumentReport, ExtractedField, InvariantViolation,
)
from .models.archetypes import ArchetypeDetection, inapplicable_fields
from .models.fiscal import FiscalCalendar, detect_fiscal_calendar
from .models.fpml_model import FIELD_REGISTRY, AmortizationSchedule
from .validate.calibrate import Thresholds, load_thresholds
from .validate.invariants import (
    BasketRecord, CovenantStep, InvariantContext, check_all,
)
from .validate.archetype import detect_archetype
from .validate.jev import JevBackend, JevSession, OfflineJev
from .validate import validators as V

#: Defined terms worth testing for override. These are the quantities a
#: ``notwithstanding`` clause is most likely to displace.
OVERRIDE_SUBJECTS = ("Consolidated EBITDA", "Applicable Margin", "Total Leverage Ratio")


class ExtractionResult(BaseModel):
    """The full output: every field, the report, and the document it came from."""

    document_id: str
    source_path: str
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    report: DocumentReport
    amortization: AmortizationSchedule | None = None
    actus_mappings: dict[str, ActusMapping] = Field(default_factory=dict)
    standards: dict[str, Any] = Field(default_factory=dict)
    archetype: ArchetypeDetection = Field(default_factory=ArchetypeDetection)

    def unresolved(self) -> list[str]:
        return [name for name, f in self.fields.items() if not f.is_resolved]


def _sweep_chunks(segments: dict[str, list[Chunk]]) -> list[Chunk]:
    """Chunks the orphan sweep and negative-space check run over.

    Structural plus sliding: between them they cover the document, and they
    partition it by two different logics. Definitional chunks are excluded --
    each one exists because a defined term exists, so nearly all of them would
    look like orphans and the signal would drown.
    """
    return [*segments.get("structural", []), *segments.get("sliding", [])]


def _chunk_contributions(
    candidates: list[Candidate], chunks: list[Chunk]
) -> dict[str, set[str]]:
    """Which chunks produced which fields, by span containment."""
    out: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.span is None:
            continue
        for chunk in chunks:
            if any(
                region.start <= candidate.span.start and candidate.span.end <= region.end
                for region in chunk.spans
            ):
                out.setdefault(chunk.chunk_id, set()).add(candidate.field)
    return out


def _build_invariant_context(
    doc: NormalizedDocument,
    fields: dict[str, ExtractedField],
    schedule: AmortizationSchedule | None,
    actus_diffs: list[dict[str, Any]],
    fiscal_calendar: FiscalCalendar | None = None,
    archetype: str | None = None,
    inapplicable: frozenset[str] = frozenset(),
) -> InvariantContext:
    steps = [
        CovenantStep(label=label, level=level, span=span)
        for label, level, span in parse_covenant_grid(doc)
    ]
    hardcoded = {
        quarter: amount for quarter, (amount, _) in parse_hardcoded_ebitda(doc).items()
    }
    baskets: list[BasketRecord] = []
    for name, field in fields.items():
        spec = FIELD_REGISTRY.get(name)
        if spec is None or spec.field_class != "baskets":
            continue
        if spec.kind != "percent" or field.value is None:
            continue
        baskets.append(BasketRecord(
            name=name,
            ebitda_pct=Decimal(str(field.value)),
            ebitda_base=field.qualifiers.get("ebitda_base"),
            span=field.spans[0] if field.spans else None,
        ))
    mfn: dict[str, Decimal] = {}
    pari = fields.get("mfn_threshold_pct")
    if pari and pari.value is not None:
        mfn["pari_passu"] = Decimal(str(pari.value))
    return InvariantContext(
        document_id=doc.document_id,
        fields=fields,
        amortization=schedule,
        covenant_steps=steps,
        baskets=baskets,
        hardcoded_ebitda_quarters=hardcoded,
        mfn_triggers=mfn,
        actus_schedule_diffs=actus_diffs,
        fiscal_calendar=fiscal_calendar or detect_fiscal_calendar(doc.text),
        archetype=archetype,
        inapplicable_invariants=inapplicable,
    )


def _actus_contracts(
    fields: dict[str, ExtractedField], schedule: AmortizationSchedule | None
) -> tuple[dict[str, ActusMapping], list[dict[str, Any]]]:
    """Map facilities to ACTUS and falsify the printed schedule against one."""
    mappings = {
        "initial_term_loan": map_facility("term_loan"),
        "delayed_draw": map_facility("delayed_draw_term_loan"),
        "revolver": map_facility("revolver"),
        "letters_of_credit": map_facility("letter_of_credit"),
    }
    diffs: list[dict[str, Any]] = []
    principal = fields.get("initial_term_loan.commitment")
    maturity = fields.get("initial_term_loan.maturity_date")
    closing = fields.get("closing_date")
    if (
        schedule and schedule.rows
        and principal and principal.value is not None
        and maturity and isinstance(maturity.value, date)
        and closing and isinstance(closing.value, date)
    ):
        corrected = schedule.deduplicated()
        contract = ActusContract(
            contractType=mappings["initial_term_loan"].contract_type or "LAX",
            statusDate=closing.value,
            initialExchangeDate=closing.value,
            maturityDate=maturity.value,
            notionalPrincipal=Decimal(str(principal.value)),
            arrayCycleAnchorDateOfPrincipalRedemption=[
                r.payment_date for r in corrected.rows
            ],
            arrayNextPrincipalRedemptionPayment=[r.amount for r in corrected.rows],
        )
        try:
            events = generate_schedule(contract)
        except ValueError as exc:
            # The extracted terms do not form a runnable contract -- a payment
            # after maturity, or a redemption array that does not line up. That
            # is a finding about the document, not a reason to stop: report it
            # as a schedule disagreement and carry on.
            diffs = [{
                "row": -1,
                "field": "contract",
                "generated": None,
                "documented": str(exc),
                "message": (
                    "the extracted terms do not form a runnable ACTUS contract: "
                    f"{exc}"
                ),
            }]
        else:
            diffs = [
                d.model_dump() for d in diff_schedule(events, schedule.as_pairs())
            ]
    return mappings, diffs


def run_pipeline(
    source: str | Path,
    extraction_backend: ExtractionBackend | None = None,
    jev_backend: JevBackend | None = None,
    thresholds: Thresholds | None = None,
    budget_usd: float | None = None,
    reread: Callable[[Chunk], list[Candidate]] | None = None,
    document_id: str | None = None,
    passes: int = 3,
) -> ExtractionResult:
    """Run the whole pipeline over one document."""
    from .models import actus_map, fibo_map, fpml_model

    extraction_backend = extraction_backend or OfflineRuleBackend()
    jev_backend = jev_backend or OfflineJev()
    if thresholds is None:
        try:
            thresholds = load_thresholds(backend=jev_backend.name)
        except FileNotFoundError:
            thresholds = Thresholds(
                version="unfitted", backend=jev_backend.name,
                notes="no fitted thresholds found; using the conservative default",
            )

    # -- tiers 0-1: ingest, structure, deterministic parsing ----------------
    doc = ingest(source, document_id=document_id)
    graph = build_definition_graph(doc)
    segments = segment_all(doc, graph)
    sweep = _sweep_chunks(segments)

    extracted = run_passes(
        doc, segments, extraction_backend, graph=graph, budget_usd=budget_usd,
        passes=passes,
    )
    reconciliation = reconcile(extracted.candidates)
    fields = reconciliation.fields

    principal = fields.get("initial_term_loan.commitment")
    schedule = parse_amortization_schedule(
        doc,
        Decimal(str(principal.value))
        if principal and principal.value is not None else None,
    )

    # -- tier 2: free deterministic invariants ------------------------------
    fiscal_calendar = detect_fiscal_calendar(doc.text)
    mappings, actus_diffs = _actus_contracts(fields, schedule)
    violations: list[InvariantViolation] = []

    # -- tier 3: batched Jev validation -------------------------------------
    session = JevSession(jev_backend, budget_usd=budget_usd)

    # Archetype first: it decides which fields are even applicable, and asking
    # after extraction would mean validating fields this deal kind cannot have.
    archetype = detect_archetype(doc, session)
    profile = archetype.profile
    not_applicable = inapplicable_fields(profile, list(fields))
    for name, reason in not_applicable.items():
        field = fields[name]
        if field.value is not None:
            # The deal kind says this cannot exist and yet something extracted
            # it. That disagreement is a finding, not a field to suppress.
            field.notes = (
                f"extracted despite {archetype.archetype} profile ruling it "
                f"inapplicable: {reason}"
            )
            continue
        field.status = "not_applicable_to_archetype"
        field.archetype_note = reason
        field.notes = reason
        field.validation_confidence = archetype.confidence
        field.validation_source = "archetype_dispatch"

    ctx = V.ValidationContext(
        doc=doc,
        fields=fields,
        chunks=sweep,
        session=session,
        thresholds=thresholds,
        chunk_contributions=_chunk_contributions(extracted.candidates, sweep),
        conflicts=reconciliation.conflicts,
        graph=graph,
    )
    violations = check_all(_build_invariant_context(
        doc, fields, schedule, actus_diffs,
        fiscal_calendar=fiscal_calendar,
        archetype=archetype.archetype,
        inapplicable=frozenset(profile.inapplicable_invariants),
    ))

    V.validator_a_span_support(ctx)
    orphans = V.validator_b_orphan_sweep(ctx)
    if reread is not None and orphans:
        V.rescue_orphans(ctx, orphans, reread, graph)     # tier 4
    V.validator_e_external_dependency(ctx)
    V.validator_c_negative_space(ctx)
    overrides = V.validator_d_overrides(ctx, OVERRIDE_SUBJECTS)
    V.resolve_conflicts(ctx)
    V.validator_f_criticality(ctx)

    # -- report --------------------------------------------------------------
    cost = CostLedger()
    cost.merge(extracted.cost)
    cost.merge(session.ledger)

    report = DocumentReport(
        document_id=doc.document_id,
        source_path=str(source),
        normalized_chars=len(doc.text),
        fields_total=len(fields),
        coverage_pct=round(
            100.0 * sum(1 for f in fields.values() if f.status == "confirmed")
            / max(1, len(fields)), 2
        ),
        status_counts=reconciliation.statuses() | _status_counts(fields),
        invariant_violations=violations,
        orphan_chunks=orphans,
        unresolved_conflicts=[c for c in reconciliation.conflicts if not c.resolved],
        external_references=[
            {
                "field": name,
                "document": f.external_document,
                # by_design vs omitted_from_filing. A reader who cannot tell
                # them apart will read a missing schedule as a deal term.
                "kind": f.external_kind,
                "note": f.notes,
                "span": f.spans[0].model_dump() if f.spans else None,
            }
            for name, f in fields.items()
            if f.status == "external_reference"
        ],
        override_findings=[
            {
                "subject": pair.subject,
                "probability": round(pair.probability, 4),
                "overrides": pair.overrides,
                "note": pair.note,
                "governing_span": pair.second.model_dump(),
                "displaced_span": pair.first.model_dump(),
            }
            for pair in overrides if pair.overrides
        ],
        review_queue=_review_queue(fields),
        definition_graph_stats=graph.stats() | {
            "segment_coverage": {
                kind: round(coverage(doc, chunks), 4)
                for kind, chunks in segments.items()
            },
        },
        cost=cost,
        thresholds_version=f"{thresholds.version}@{thresholds.backend}",
        notes=[
            f"jev: {session.summary()}",
            f"chunks swept: {len(sweep)}",
            f"override findings: "
            f"{sum(1 for o in overrides if o.overrides)} of {len(overrides)} tested",
            f"fibo gaps: {sorted(fibo_map.gaps())}",
            f"fiscal calendar: {fiscal_calendar.source}",
            f"archetype: {archetype.archetype} ({archetype.basis}, "
            f"{archetype.confidence:.2f}) -- {archetype.note}",
            f"fields inapplicable to this archetype: {len(not_applicable)}",
        ],
    )

    return ExtractionResult(
        document_id=doc.document_id,
        source_path=str(source),
        fields=fields,
        report=report,
        amortization=schedule,
        actus_mappings=mappings,
        archetype=archetype,
        standards={
            "fibo": fibo_map.provenance(),
            "fpml": fpml_model.provenance(),
            "actus": actus_map.provenance(),
        },
    )


def _status_counts(fields: dict[str, ExtractedField]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field in fields.values():
        counts[field.status] = counts.get(field.status, 0) + 1
    return counts


def _review_queue(fields: dict[str, ExtractedField]) -> list[dict[str, Any]]:
    """Fields a human should look at, most economically material first."""
    queue = [
        {
            "field": name,
            "status": field.status,
            "criticality": field.criticality,
            "criticality_label": field.criticality_label,
            "value": str(field.value) if field.value is not None else None,
            "extraction_confidence": field.extraction_confidence,
            "validation_confidence": field.validation_confidence,
            "why": field.notes,
            "span": field.spans[0].model_dump() if field.spans else None,
        }
        for name, field in fields.items()
        if field.status in ("needs_review", "conflicted")
    ]
    queue.sort(
        key=lambda item: (
            -item["criticality"],
            item["validation_confidence"] if item["validation_confidence"] is not None
            else 0.0,
        )
    )
    return queue
