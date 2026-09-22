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
    parse_covenant_grid, parse_covenant_variants, parse_hardcoded_ebitda,
    parse_post_ipo_level, parse_pricing_grid, parse_springing_condition,
    run_passes,
)
from .extract.reconcile import reconcile
# eval/ imports pipeline, so this would be circular for anything heavier.
# arms.py imports nothing from the package, deliberately, so that the stage
# vocabulary can live with the ablation that uses it rather than here.
from .eval.arms import FULL, Arm
from .graph.definitions import build_definition_graph
from .graph.precedence import PrecedenceGraph, build_precedence_graph
from .ingest.documents import (
    DocumentSet, OperativeText, apply_chain, load_document_set,
    operative_document,
)
from .ingest.normalize import NormalizedDocument, ingest
from .ingest.segment import Chunk, coverage, segment_all
from .models.actus_map import (
    ActusContract, ActusMapping, diff_schedule, generate_schedule, map_facility,
)
from .models.core import (
    Condition, CostLedger, DocumentReport, ExtractedField, InvariantViolation,
    Variant,
)
from .models.archetypes import (
    ArchetypeDetection, inapplicable_fields, suppression_vetoes,
)
from .models.agreement import AmendmentLink, CreditAgreement
from .models.assemble import build_agreement
from .models.export import FacilityExport, build_facilities, export_summary
from .models.fiscal import FiscalCalendar, detect_fiscal_calendar
from .models.pricing import Pricing, parse_pricing
from .models.fpml_model import FIELD_REGISTRY, AmortizationSchedule
from .validate.calibrate import Thresholds, load_thresholds
from .validate.invariants import (
    BasketRecord, CovenantStep, InvariantContext, check_all,
)
from .validate.archetype import detect_archetype
from .validate.jev import JevBackend, JevSession, OfflineJev
from .validate.omission import document_omits_schedules
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
    #: The extracted record in the FpML-shaped facility model, plus an account
    #: of every element withheld and why. Forty FpML element names in this
    #: repository are verified against pinned schemas and, until this existed,
    #: none of them was ever populated by a run.
    facilities: list[FacilityExport] = Field(default_factory=list)
    #: The same record in the shape the market has: one agreement, its
    #: metadata, and a tranche per tranche with its own terms, conditions,
    #: reference data and schedules. Unlike ``facilities`` this drops nothing
    #: -- every slot carries its status -- so it is the record, and
    #: ``facilities`` is the interchange format.
    agreement: CreditAgreement | None = None
    archetype: ArchetypeDetection = Field(default_factory=ArchetypeDetection)
    #: The rate, decomposed. A CSA folded into the margin overstates the yield
    #: and then overstates every MFN comparison made against it.
    pricing: Pricing = Field(default_factory=Pricing)
    #: The operative text every span in this result indexes into. Excluded
    #: from serialization -- it is already quoted span by span, and carrying a
    #: second copy would double the size of every report on disk.
    document: Any = Field(default=None, exclude=True, repr=False)
    #: The defined-term graph. Excluded from serialization for the same reason
    #: as the document: the report already carries its statistics, and a second
    #: copy of every definition body would dwarf the fields.
    definition_graph: Any = Field(default=None, exclude=True, repr=False)

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
    definition_graph: Any = None,
    precedence_graph: Any = None,
    pricing: Pricing | None = None,
    chain_findings: list[Any] | None = None,
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
        definition_graph=definition_graph,
        precedence_graph=precedence_graph,
        pricing=pricing,
        document=doc,
        chain_findings=chain_findings or [],
        document_omits_schedules=document_omits_schedules(doc),
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
    document_set: DocumentSet | None = None,
    arm: Arm | None = None,
) -> ExtractionResult:
    """Run the whole pipeline over one document, or over a chain.

    Given a ``document_set``, extraction runs on the *operative* text -- the
    base agreement with every amendment folded in -- rather than on the base.
    Extracting the base alone reports terms that stopped being true years ago,
    and does it silently, because every span is correct and every figure is
    quoted accurately.

    ``arm`` selects which validators run, for the ablation in
    ``credit_extract.eval.ablation``. The default is every one of them, so
    omitting it is the pipeline as shipped.
    """
    arm = arm or FULL
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
    operative: OperativeText | None = None
    if document_set is None:
        doc = ingest(source, document_id=document_id)
        document_set = _implied_set(source, doc)
    if document_set is not None:
        operative = apply_chain(document_set)
        doc = operative_document(document_set, operative)
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
    precedence = build_precedence_graph(doc)
    _build_covenant_variants(doc, fields, precedence)
    pricing = parse_pricing(doc, parse_pricing_grid(doc))
    mappings, actus_diffs = _actus_contracts(fields, schedule)
    violations: list[InvariantViolation] = []

    # -- tier 3: batched Jev validation -------------------------------------
    session = JevSession(jev_backend, budget_usd=budget_usd)

    # Archetype first: it decides which fields are even applicable, and asking
    # after extraction would mean validating fields this deal kind cannot have.
    archetype = detect_archetype(doc, session)
    profile = archetype.profile
    not_applicable = inapplicable_fields(profile, list(fields), doc.text)
    vetoes = suppression_vetoes(profile, doc.text)
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
        definition_graph=graph,
        precedence_graph=precedence,
        pricing=pricing,
        chain_findings=document_set.findings if document_set else [],
    ))

    # Precedence is what justifies a variant ordering. A field whose section is
    # overridden elsewhere records the clause that does the overriding, so the
    # ordering is citable rather than asserted.
    for field in fields.values():
        if field.precedence_basis or not field.spans:
            continue
        basis = precedence.basis_for(field.spans[0].section_id)
        if basis:
            field.precedence_basis = basis

    # The arm decides which stages run. FULL is the default and enables all of
    # them, so a caller that does not know arms exist gets the pipeline it
    # always got. Tier 4 and the findings sweep hang off validator B: with no
    # orphans there is nothing to re-read and nothing to report, so they fall
    # away on their own rather than needing a second switch.
    if arm.runs("A"):
        V.validator_a_span_support(ctx)
    orphans = V.validator_b_orphan_sweep(ctx) if arm.runs("B") else []
    if reread is None:
        reread = _default_reread(doc, extraction_backend, fields, graph)
    rescued = V.rescue_orphans(ctx, orphans, reread, graph) if orphans else []
    tier4 = _fill_from_rescue(fields, rescued)             # tier 4
    # Tier 4's other half. A rescued *field* answers the sweep in the record's
    # own vocabulary; a finding answers it in the document's. The review queue
    # only ever shows fields, so an obligation the registry has no slot for
    # has been invisible in every report this project has produced -- which is
    # the exact text the sweep flags and then could say nothing about.
    findings = (
        _collect_findings(doc, extraction_backend, orphans, sweep)
        if orphans else []
    )
    if arm.runs("E"):
        V.validator_e_external_dependency(ctx)
    if arm.runs("C"):
        V.validator_c_negative_space(ctx)
    overrides = (
        V.validator_d_overrides(ctx, OVERRIDE_SUBJECTS) if arm.runs("D") else []
    )
    # Not a validator and not optional: reconciliation left conflicts behind
    # and a record that reports a field as both values is not a configuration
    # choice, it is a broken record.
    V.resolve_conflicts(ctx)
    if arm.runs("F"):
        V.validator_f_criticality(ctx)

    amendment_verdicts = (
        V.validator_g_amendment_effect(ctx, document_set)
        if arm.runs("G") and document_set is not None and document_set.amendments
        else []
    )
    if operative is not None:
        _attribute_spans(fields, operative)

    facilities = build_facilities(fields)

    # The structured record. Built from the same fields as ``facilities`` and
    # differing in what it is allowed to drop: nothing. A reading that did not
    # settle crosses carrying its status, and the tranche it belongs to is a
    # tranche rather than a copy of the deal.
    agreement = build_agreement(
        fields,
        agreement_id=doc.document_id,
        document_id=doc.document_id,
        source_path=str(source),
        archetype=archetype.model_dump(),
        chain=_amendment_links(document_set),
        # Recorded readings whose quote no chunk held. ``RecordedBackend``
        # has always been able to report these and nothing ever asked, so
        # readings that silently did not count looked like readings the model
        # never made.
        unplaced_readings=(
            extraction_backend.unplaced()
            if hasattr(extraction_backend, "unplaced") else ()
        ),
    )

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
        review_queue=_review_queue(
            fields, [c for c in reconciliation.conflicts if not c.resolved]
        ),
        chain=_chain_report(document_set, operative, amendment_verdicts),
        archetype=archetype.model_dump(),
        blind_spots=_blind_spot_notice(archetype),
        definition_graph_stats=graph.stats() | {
            "precedence": precedence.stats(),
            "pricing": pricing.describe(),
            "segment_coverage": {
                kind: round(coverage(doc, chunks), 4)
                for kind, chunks in segments.items()
            },
        },
        cost=cost,
        thresholds_version=f"{thresholds.version}@{thresholds.backend}",
        notes=[
            f"jev: {session.summary()}",
            # Which tier answered what. The cheap tiers exist to take the easy
            # fields off the model, so a run where they answer nothing is one
            # where the pattern set has drifted from what documents look like.
            "extraction by tier: " + (
                ", ".join(
                    f"{tier}={n}" for tier, n in extracted.by_tier().items()
                ) or "nothing extracted"
            ),
            f"chunks swept: {len(sweep)}",
            # What the ladder did, stage by stage. Worth printing rather than
            # inferring: the division of labour between the definitions and
            # the sections is the design, and a run where orientation settles
            # nothing means the graph is not carrying the weight it is here
            # to carry.
            *(
                [f"ladder {line}" for line in extracted.stage_log]
                if extracted.stage_log else []
            ),
            # Agreement with a value the pass was shown, at the span it was
            # shown. Not corroboration, and counted separately so a run where
            # the model mostly confirms the rules cannot look like a run where
            # two readers independently agreed.
            f"anchored agreement: {extracted.echoes()} echo(es); "
            + (
                f"overturned {', '.join(extracted.overturns())}"
                if extracted.overturns() else "nothing overturned"
            ),
            # Absence is only worth what the search behind it was worth.
            "section sweep: " + (
                "exhaustive" if extracted.swept_exhaustively
                else "CUT SHORT by the budget -- absence claims in this run "
                     "are not safe"
            ),
            # How much of the verified FpML mapping this run actually earned.
            # Zero is a real answer and the one the deterministic tier gives:
            # a facility appears only where its commitment is settled, and a
            # tranche nobody established the size of is a tranche nobody
            # established.
            "fpml export: " + ", ".join(
                f"{k}={v}" for k, v in export_summary(facilities).items()
                if k != "withheld_by_reason"
            ),
            # The structured record, which drops nothing. The three coverage
            # numbers are three different problems: settled is done, unsettled
            # with a value is a calibration question, and empty is recall.
            "agreement: " + ", ".join([
                f"tranches={len(agreement.tranches)}"
                f" (established {len(agreement.established_tranches())})",
                *(f"{k}={v}" for k, v in agreement.coverage().items()),
                f"inherited_from_deal={len(agreement.assembly.inherited_slots)}",
            ]),
            # Recorded readings whose quote no chunk held. Empty on every run
            # that is not replaying a recording, and never silent when it is:
            # a reading that did not count is not a reading nobody made.
            "unplaced model readings: " + (
                ", ".join(agreement.assembly.unplaced_readings)
                if agreement.assembly.unplaced_readings else "none"
            ),
            # Tier 4, which had never run: the ladder describes a targeted
            # re-read of the chunks the sweep flagged and nothing supplied one.
            # A line that reads "0 rescued" over a hundred orphans is a
            # statement about the pattern set, and it is worth printing.
            f"orphan re-read (tier 4): {len(orphans)} chunk(s) flagged, "
            + (f"{len(tier4)} field(s) recovered -- {', '.join(tier4)}"
               if tier4 else "nothing recovered")
            + (f"; {findings} finding(s) with no field to hold them"
               if findings else ""),
            # Empty on every offline run and on any healthy model run. When it
            # is not empty, part of the document was never read by the
            # extractor, and no absence in this report can be taken at face
            # value until a reader knows that.
            "chunks the extractor could not read: " + (
                "; ".join(extracted.unread_chunks[:5])
                + (f" (+{len(extracted.unread_chunks) - 5} more)"
                   if len(extracted.unread_chunks) > 5 else "")
                if extracted.unread_chunks else "none"
            ),
            f"override findings: "
            f"{sum(1 for o in overrides if o.overrides)} of {len(overrides)} tested",
            f"fibo gaps: {sorted(fibo_map.gaps())}",
            f"fiscal calendar: {fiscal_calendar.source}",
            f"archetype: {archetype.archetype} ({archetype.basis}, "
            f"{archetype.confidence:.2f}) -- {archetype.note}",
            f"fields inapplicable to this archetype: {len(not_applicable)}",
            "archetype suppression vetoed by the document: " + (
                ", ".join(f"{g} ({p!r})" for g, p in sorted(vetoes.items()))
                if vetoes else "none"
            ),
            f"pricing: {pricing.describe()}",
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
        facilities=facilities,
        agreement=agreement,
        pricing=pricing,
        document=doc,
        definition_graph=graph,
        standards={
            "fibo": fibo_map.provenance(),
            "fpml": fpml_model.provenance(),
            "actus": actus_map.provenance(),
        },
    )




def _build_covenant_variants(
    doc: NormalizedDocument,
    fields: dict[str, ExtractedField],
    precedence: Any,
) -> None:
    """Assemble the financial covenant as a field that varies over time.

    A step-down grid answers "what is the covenant?" differently depending on
    when you ask. Two scalars cannot represent that, and the scalar that gets
    reported is wrong for most of the life of the loan.
    """
    rows = parse_covenant_variants(doc)
    springing = parse_springing_condition(doc)
    post_ipo = parse_post_ipo_level(doc)
    if not rows and not post_ipo:
        return

    variants: list[Variant] = []
    if post_ipo and post_ipo["level"] is not None:
        variants.append(Variant[Any](
            value=post_ipo["level"], spans=[post_ipo["span"]], status="confirmed",
            extraction_confidence=0.85,
            conditions=[Condition(
                kind="event", expr=post_ipo["expr"],
                source_spans=[post_ipo["span"]],
            )],
            notes="applies only after an IPO",
        ))
    for row in rows:
        conditions = []
        if springing:
            conditions.append(Condition(
                kind="state", expr=springing["expr"],
                source_spans=[springing["span"]],
            ))
        variants.append(Variant[Any](
            value=row["level"], spans=[row["span"]], status="confirmed",
            extraction_confidence=0.93,
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            conditions=conditions,
            notes=row["label"],
        ))
    if not variants:
        return

    basis_parts = []
    if springing:
        basis_parts.append(
            f"springing: tested only while {springing['subject']} exceeds "
            f"{springing['threshold']}%"
        )
    if post_ipo:
        basis_parts.append("post-IPO level takes precedence over the schedule")
    grid_basis = precedence.basis_for("6.12") if precedence else None
    if grid_basis:
        basis_parts.append(grid_basis)

    spec = FIELD_REGISTRY.get("financial_covenant.level")
    fields["financial_covenant.level"] = ExtractedField[Any](
        variants=variants,
        standard_term=spec.standard_term if spec else None,
        field_class=spec.field_class if spec else "covenant_levels",
        criticality=spec.criticality if spec else 5,
        precedence_basis="; ".join(basis_parts) or (
            "step-down schedule, latest applicable window first"
        ),
    )


def _attribute_spans(
    fields: dict[str, ExtractedField], operative: OperativeText
) -> None:
    """Point every span at the document its text actually came from.

    After a chain is folded in, an offset alone is not a citation. A figure
    quoted from Section 6.12 may have been written by Amendment No. 3, and a
    reviewer sent to the base agreement will not find it there.
    """
    for field in fields.values():
        for variant in field.variants:
            variant.spans = [
                span.model_copy(
                    update={"document_id": operative.source_of(span.section_id)}
                )
                for span in variant.spans
            ]


def _blind_spot_notice(archetype: ArchetypeDetection) -> str:
    """What this run has not been tested on, said in the run's own output.

    The register belongs in every report, not in a design document nobody
    opens next to the numbers. A reader looking at a field list has no way to
    know that nothing resembling this deal was ever in the corpus, and the
    fields will look exactly as confident either way.

    The archetype narrows it: a reader of a NAV facility needs to be told that
    archetype dispatch has no labelled examples of one, and does not need the
    entries about fee letters repeated at them first.
    """
    from .eval.families import load_blind_spots

    register = load_blind_spots()
    detected = archetype.archetype or "unknown"
    relevant = [
        entry for entry in register.entries
        if entry.member and detected in (entry.member, entry.member.rstrip("s"))
        or (entry.member or "").startswith(detected)
    ]
    lines = [register.headline()]
    for entry in relevant:
        lines.append(
            f"This deal was read as {detected}, which is a named blind spot "
            f"[{entry.id}]: {_one_line(entry.reason)}"
        )
    if detected == "unknown":
        lines.append(
            "No archetype was dispatched, so archetype-gated fields were left "
            "applicable rather than ruled out; nothing here is marked "
            "not_applicable on a guess."
        )
    return "\n".join(line for line in lines if line)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _implied_set(
    source: str | Path, doc: NormalizedDocument
) -> DocumentSet | None:
    """Treat a lone amendment as the one-document chain that it is.

    Handed an amendment on its own -- which is how exhibits are filed, one at
    a time -- a single-document run would report the amendment's recitals as
    deal terms and say nothing about it. The chain machinery already knows how
    to say what is wrong with that, so the document is routed through it.

    Base agreements are left alone: there is nothing for the chain to add, and
    routing every document through it would cost a reassembly for nothing.
    """
    from .ingest.documents import (
        Document, assemble_set, classify_role, detect_effective_date,
    )

    role, number, title = classify_role(doc.text)
    if role not in ("amendment", "amendment_and_restatement") and not doc.is_blackline:
        return None
    return assemble_set([Document(
        document_id=doc.document_id,
        path=str(source),
        role=role,
        normalized=doc,
        effective_date=detect_effective_date(doc.text),
        amendment_number=number,
        title=title,
    )])


def _amendment_links(document_set: DocumentSet | None) -> list[AmendmentLink]:
    """The chain, in the structured record's shape.

    A single-document run has no chain and gets an empty tuple, which is not
    the same as a chain nobody looked for -- ``run_document_set`` is the only
    caller that can supply one.
    """
    if document_set is None:
        return []
    return [
        AmendmentLink(
            document_id=d.document_id,
            sequence=d.amendment_number,
            dated=d.effective_date,
            restates=d.supersedes_chain,
        )
        for d in document_set.chain
    ]


def _chain_report(
    document_set: DocumentSet | None,
    operative: OperativeText | None,
    verdicts: list[Any],
) -> dict[str, Any]:
    if document_set is None or operative is None:
        return {}
    disagreements = [v for v in verdicts if not v.agrees_with_parser]
    return {
        "operative_as_of": str(document_set.operative_as_of or ""),
        "documents": [
            {
                "document_id": d.document_id,
                "role": d.role,
                "amendment_number": d.amendment_number,
                "effective_date": str(d.effective_date or ""),
                "title": d.title[:100],
            }
            for d in document_set.chain
        ],
        "superseded": [d.document_id for d in document_set.superseded],
        "findings": [f.model_dump() for f in document_set.findings],
        "effects_applied": [e.describe() for e in operative.applied],
        "effects_unapplied": [
            {"effect": e.describe(), "reason": why}
            for e, why in operative.unapplied
        ],
        "sections_amended": sorted(
            s for s, r in operative.sections.items() if r.amended
        ),
        "amendment_effect_disagreements": [v.model_dump() for v in disagreements],
    }


def run_document_set(
    paths: list[str | Path],
    operative_as_of: date | None = None,
    **kwargs: Any,
) -> ExtractionResult:
    """Extract from a chain: base agreement plus every amendment."""
    document_set = load_document_set(paths, operative_as_of)
    return run_pipeline(
        document_set.base.path, document_set=document_set, **kwargs
    )


def _status_counts(fields: dict[str, ExtractedField]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field in fields.values():
        counts[field.status] = counts.get(field.status, 0) + 1
    return counts


def _collect_findings(
    doc: NormalizedDocument,
    backend: ExtractionBackend,
    orphans: list[Any],
    chunks: list[Chunk],
) -> int:
    """Ask the backend what each flagged passage says, in the document's terms.

    Optional on the backend: a tier that cannot answer in prose simply does
    not, and the orphan stays unreviewed, which is the honest state and was
    already the state before this existed. The deterministic backend has no
    such method and never will -- naming an obligation is not something a
    pattern does.
    """
    ask = getattr(backend, "reread_findings", None)
    if ask is None:
        return 0
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    total = 0
    # The structural and sliding segmentations both cover the document, so a
    # passage sits in at least two chunks and its finding would be reported
    # once per chunk. For a value that duplication is signal -- reconciliation
    # reads it as independent support -- but a finding has no reconciliation
    # behind it, so the same sentence would simply be printed three times.
    # Identity is the offsets it occupies.
    seen: set[tuple[str, int, int]] = set()
    for orphan in orphans:
        chunk = by_id.get(orphan.chunk_id)
        if chunk is None:
            continue
        try:
            found = ask(doc, chunk)
        except Exception:  # noqa: BLE001 - a failed re-read is not a failed run
            continue
        fresh = []
        for finding in found:
            key = (finding.name, finding.span.start, finding.span.end)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(finding)
        found = fresh
        if not found:
            continue
        orphan.findings = found
        if orphan.resolution == "unreviewed":
            # Explained, but not by a field. "rescued" would claim the record
            # now carries it, and it does not -- nothing in the registry can.
            orphan.resolution = "benign"
        total += len(found)
    return total


def _default_reread(
    doc: NormalizedDocument,
    backend: ExtractionBackend,
    fields: dict[str, ExtractedField],
    graph: Any = None,
) -> Callable[[Chunk], list[Candidate]]:
    """Tier 4's callable, when the caller supplies none.

    The ladder documents a targeted re-read that fires only on chunks the
    orphan sweep flagged, and nothing ever supplied one, so the tier had never
    run: on one held-out agreement the sweep found 118 passages that say
    something no field captured and then the pipeline moved on.

    The re-read asks the extraction question again over one chunk, with the
    fields that are still empty -- which is a different question from the one
    the main passes asked, because those walk whole segmentations and, behind
    ``LayeredBackend``, only ever show the model what the rules left. For the
    deterministic backend it is the same rules over the same text and finds
    nothing, which is not a disappointment: it is the measurement. Those 118
    passages are ones the patterns cannot explain, and running them again says
    so rather than leaving the tier's absence to be mistaken for a clean sweep.
    """
    def reread(chunk: Chunk) -> list[Candidate]:
        specs = [
            FIELD_REGISTRY[name] for name, field in fields.items()
            if field.value is None and name in FIELD_REGISTRY
        ]
        if not specs:
            return []
        context = ""
        if graph is not None and chunk.segmentation == "definitional":
            context = graph.context_for(chunk.label)
        try:
            found, _ = backend.extract(doc, chunk, specs, context, "reread")
        except Exception:  # noqa: BLE001 - a failed rescue is not a failed run
            return []
        return found

    return reread


def _fill_from_rescue(
    fields: dict[str, ExtractedField], rescued: list[Candidate]
) -> list[str]:
    """Put a rescued value into the field it names, if that field is empty.

    Never into a field that already has one: a single re-read of a single
    chunk has none of the independent support the main passes are built to
    produce, so it is evidence enough to answer a question nobody answered and
    not enough to overturn an answer. The status says so -- these arrive at
    ``needs_review`` and cannot be confirmed here -- and so does the note,
    because a value that came from the orphan sweep was found by a different
    route than the rest of the record and a reader should be told which.
    """
    filled: list[str] = []
    best: dict[str, Candidate] = {}
    for candidate in rescued:
        if candidate.value is None or candidate.span is None:
            continue
        field = fields.get(candidate.field)
        if field is None or field.value is not None:
            continue
        current = best.get(candidate.field)
        if current is None or candidate.confidence > current.confidence:
            best[candidate.field] = candidate
    for name, candidate in best.items():
        field = fields[name]
        # Span before value: the variant validates on assignment and a value
        # without provenance is rejected, which is the rule doing its job.
        field.variants[0].spans = [candidate.span]
        field.variants[0].value = candidate.value
        field.variants[0].quantity = candidate.quantity
        field.status = "needs_review"
        field.variants[0].extraction_confidence = candidate.confidence
        field.notes = (
            "found by the orphan sweep's re-read of "
            f"{candidate.span.section_id or 'an unattributed passage'}, on one "
            "chunk and one pass; the main passes over every segmentation did "
            "not produce it, so it has no independent support and is not "
            "eligible to be confirmed from here"
        )
        filled.append(name)
    return sorted(filled)


def _review_queue(
    fields: dict[str, ExtractedField],
    conflicts: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Fields a human should look at, most economically material first.

    A conflicted field has no value, and printing one anyway is how a reader
    ends up quoting a number the pipeline never asserted. Eleven candidate
    closing dates at equal weight resolve to whichever the reconciler happened
    to order first; the record says "conflicted" and the queue said
    "= 2019-11-26" three characters later. The competing values go in their
    own key and the single value stays empty.
    """
    competing = {
        c.field: [str(item.get("value")) for item in c.candidates]
        for c in (conflicts or [])
    }
    queue = [
        {
            "field": name,
            "status": field.status,
            "criticality": field.criticality,
            "criticality_label": field.criticality_label,
            "value": (
                None if field.status == "conflicted"
                else str(field.value) if field.value is not None else None
            ),
            "competing_values": competing.get(name, []),
            "extraction_confidence": field.extraction_confidence,
            "validation_confidence": field.validation_confidence,
            "why": field.notes,
            "span": field.spans[0].model_dump() if field.spans else None,
            # Separate keys because they answer different questions: "span" is
            # the text a value was read from, "review_hint" is where to start
            # looking when there is no value yet.
            "review_hint": (
                field.review_hint.model_dump() if field.review_hint else None
            ),
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
