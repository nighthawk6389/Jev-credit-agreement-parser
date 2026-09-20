"""Extraction passes.

Tier 1 of the escalation ladder lives at the top of this module: tables parse
deterministically in Python, for free, with exact spans. Nothing below tier 1
is asked to read a grid, and nothing at any tier is asked to do arithmetic.

The LLM passes below run the same target list against three independent
segmentations. Disagreement between them is the signal reconciliation keys on,
so the passes must stay genuinely independent -- same prompt, different view of
the document.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Protocol

from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..ingest.tables import (
    Table, parse_date, parse_money, parse_percent, parse_ratio,
)
from ..models.core import CostLedger, Span
from ..models.fpml_model import (
    FIELD_REGISTRY, AmortizationSchedule, FieldSpec, ScheduleRow,
)

# ---------------------------------------------------------------------------
# Tier 1: deterministic table parsing
# ---------------------------------------------------------------------------


def _find_table(doc: NormalizedDocument, *header_groups: tuple[str, ...]) -> Table | None:
    """First table whose headers satisfy every group (a group is an OR)."""
    for table in doc.tables:
        labels = " | ".join(table.header_labels()).lower()
        if all(any(term in labels for term in group) for group in header_groups):
            return table
    return None


def parse_amortization_schedule(
    doc: NormalizedDocument, original_principal: Decimal | None = None
) -> AmortizationSchedule | None:
    """Parse the repayment table exactly as printed -- duplicates included.

    Reading the table literally is the point. Silently de-duplicating here
    would repair Trap 1 before the invariant checker ever sees it, and the
    whole finding would vanish.
    """
    table = _find_table(
        doc,
        ("payment date", "date", "installment date"),
        ("amortization", "principal", "amount", "installment"),
    )
    if table is None:
        return None
    rows: list[ScheduleRow] = []
    for index, cells in enumerate(table.body_rows()):
        if len(cells) < 2:
            continue
        when = parse_date(cells[0].text)
        amount = parse_money(cells[1].text)
        if when is None or amount is None:
            continue
        rows.append(
            ScheduleRow(
                payment_date=when,
                amount=amount,
                row_index=len(rows),
                source_row=index,
                span_start=cells[0].start,
                span_end=cells[1].end,
            )
        )
    if not rows:
        return None
    return AmortizationSchedule(
        rows=rows, original_principal=original_principal, table_id=table.table_id
    )


def parse_hardcoded_ebitda(doc: NormalizedDocument) -> dict[str, tuple[Decimal, Span]]:
    """Trap 2: quarters whose EBITDA is fixed by table, overriding the definition."""
    table = _find_table(doc, ("fiscal quarter", "quarter", "period"), ("ebitda",))
    if table is None:
        return {}
    out: dict[str, tuple[Decimal, Span]] = {}
    for cells in table.body_rows():
        if len(cells) < 2:
            continue
        amount = parse_money(cells[1].text)
        if amount is None:
            continue
        out[cells[0].text] = (amount, doc.span(cells[0].start, cells[1].end))
    return out


def parse_covenant_grid(doc: NormalizedDocument) -> list[tuple[str, Decimal, Span]]:
    table = _find_table(doc, ("test period", "period", "fiscal quarter"),
                        ("ratio", "leverage", "covenant"))
    if table is None:
        return []
    out: list[tuple[str, Decimal, Span]] = []
    for cells in table.body_rows():
        if len(cells) < 2:
            continue
        level = parse_ratio(cells[1].text)
        if level is None:
            continue
        out.append((cells[0].text, level, doc.span(cells[0].start, cells[1].end)))
    return out


def parse_pricing_grid(doc: NormalizedDocument) -> list[dict[str, Any]]:
    table = _find_table(doc, ("level", "pricing", "tier"),
                        ("eurodollar", "libor", "rate", "margin"))
    if table is None:
        return []
    labels = [l.lower() for l in table.header_labels()]
    out: list[dict[str, Any]] = []
    for cells in table.body_rows():
        row: dict[str, Any] = {"cells": [c.text for c in cells]}
        for index, cell in enumerate(cells):
            label = labels[index] if index < len(labels) else f"col{index}"
            pct = parse_percent(cell.text)
            ratio = parse_ratio(cell.text)
            if pct is not None:
                row[label] = pct
            elif ratio is not None:
                row[label] = ratio
        row["span"] = doc.span(cells[0].start, cells[-1].end)
        out.append(row)
    return out


def parse_commitment_table(doc: NormalizedDocument) -> list[dict[str, Any]]:
    table = _find_table(doc, ("facility", "tranche", "lender"),
                        ("commitment", "amount", "principal"))
    if table is None:
        return []
    out: list[dict[str, Any]] = []
    for cells in table.body_rows():
        if len(cells) < 2:
            continue
        amount = parse_money(cells[1].text)
        if amount is None:
            continue
        maturity = parse_date(cells[2].text) if len(cells) > 2 else None
        out.append({
            "facility": cells[0].text,
            "commitment": amount,
            "maturity": maturity,
            "span": doc.span(cells[0].start, cells[-1].end),
        })
    return out


# ---------------------------------------------------------------------------
# Tier 4/5: model-backed passes
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One pass's answer for one field, with the span it cited."""

    field: str
    value: Any
    span: Span | None
    confidence: float
    pass_id: str
    segmentation: str
    qualifiers: dict[str, str] = dc_field(default_factory=dict)
    external_document: str | None = None
    notes: str | None = None

    def key(self) -> str:
        """Value identity for agreement counting. Never compared as floats."""
        if isinstance(self.value, Decimal):
            return format(self.value.normalize(), "f")
        if isinstance(self.value, date):
            return self.value.isoformat()
        if isinstance(self.value, str):
            return " ".join(self.value.split()).casefold()
        return repr(self.value)


class ExtractionBackend(Protocol):
    """A pass backend. Every implementation returns spans or nothing."""

    name: str

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Chunk,
        specs: list[FieldSpec],
        context: str,
        pass_id: str,
    ) -> tuple[list[Candidate], CostLedger]:
        ...


class AnthropicBackend:
    """Tier 4/5: a real LLM pass.

    Requires ``ANTHROPIC_API_KEY``. The prompt demands a verbatim quote for
    every value; any value whose quote cannot be located in the chunk is
    dropped rather than stored without provenance.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "the anthropic package is required for LLM passes; "
                    "install it or run with --backend offline"
                ) from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic()
        return self._client

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Chunk,
        specs: list[FieldSpec],
        context: str,
        pass_id: str,
    ) -> tuple[list[Candidate], CostLedger]:
        from .prompts import build_extraction_prompt

        client = self._ensure_client()
        prompt = build_extraction_prompt(chunk.text, specs, context)
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        ledger = CostLedger(
            llm_calls=1,
            llm_input_tokens=response.usage.input_tokens,
            llm_output_tokens=response.usage.output_tokens,
            llm_cost_usd=_anthropic_cost(
                self.model, response.usage.input_tokens, response.usage.output_tokens
            ),
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return _parse_llm_payload(text, doc, chunk, specs, pass_id), ledger


#: Published per-million-token prices, input/output.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def _anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = ANTHROPIC_PRICES.get(model, (3.00, 15.00))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def _coerce(kind: str, raw: Any) -> Any:
    if raw is None:
        return None
    text = str(raw)
    if kind == "money":
        return parse_money(text)
    if kind == "percent":
        return parse_percent(text)
    if kind == "ratio":
        return parse_ratio(text)
    if kind == "date":
        return parse_date(text)
    if kind == "int":
        try:
            return int(re.sub(r"[^\d-]", "", text))
        except ValueError:
            return None
    if kind == "bool":
        return text.strip().lower() in ("true", "yes")
    return text.strip()


def _parse_llm_payload(
    text: str,
    doc: NormalizedDocument,
    chunk: Chunk,
    specs: list[FieldSpec],
    pass_id: str,
) -> list[Candidate]:
    """Turn a model response into candidates, dropping anything unprovenanced."""
    match = re.search(r"\[.*\]|\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = payload.get("fields", [])
    if not isinstance(payload, list):
        return []
    by_name = {spec.name: spec for spec in specs}
    out: list[Candidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("field")
        spec = by_name.get(name)
        if spec is None:
            continue
        span = chunk.locate(doc, item.get("quote") or "")
        value = _coerce(spec.kind, item.get("value"))
        if value is not None and span is None:
            # No span, no value. A quote the model produced but the chunk does
            # not contain is a fabrication, so the value goes in the bin rather
            # than into the record without provenance.
            continue
        out.append(
            Candidate(
                field=name,
                value=value,
                span=span,
                confidence=float(item.get("confidence", 0.5)),
                pass_id=pass_id,
                segmentation=chunk.segmentation,
                external_document=item.get("external_document"),
                qualifiers=item.get("qualifiers") or {},
                notes=item.get("notes"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Tier 1/2: the offline deterministic backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """An anchored pattern for one field.

    Group 1 carries the value; the whole match is the span, so a reviewer sees
    the anchoring language and not a bare number floating in the document.
    """

    field: str
    pattern: str
    confidence: float = 0.80
    flags: int = re.IGNORECASE
    #: When set, the rule reports an external dependency instead of a value.
    external_document: str | None = None
    #: (probe regex, qualifier key, qualifier value) evaluated near the match.
    qualifier_probe: tuple[str, str, str] | None = None
    note: str | None = None


_ENTITY = r"([A-Z][A-Za-z0-9 ,.&'\-]{3,80}?)"

OFFLINE_RULES: tuple[Rule, ...] = (
    # -- parties -------------------------------------------------------------
    Rule("borrower.legal_name", _ENTITY + r",\s*as (?:the )?Borrower", 0.90, 0),
    Rule("holdings.legal_name", _ENTITY + r",\s*as Holdings", 0.90, 0),
    Rule("administrative_agent.legal_name",
         _ENTITY + r",\s*as Administrative Agent", 0.90, 0),
    Rule("collateral_agent.legal_name",
         _ENTITY + r",\s*as (?:Administrative Agent and )?Collateral Agent", 0.85, 0),
    Rule("arranger.legal_name",
         _ENTITY + r",\s*as (?:Lead |Sole |Joint )*(?:Lead )?Arranger", 0.85, 0),
    Rule("syndication_agent.legal_name",
         _ENTITY + r",\s*as Syndication Agent", 0.90, 0),
    # -- dates ---------------------------------------------------------------
    Rule("closing_date", r'"Closing Date"\s+means\s+([^.]+)\.', 0.95),
    Rule("closing_date", r"dated as of ([A-Z][a-z]+ \d{1,2}, \d{4})", 0.70),
    Rule("initial_term_loan.maturity_date",
         r'"Initial Term Loan Maturity Date"\s+means\s+([^.]+)\.', 0.95),
    Rule("revolver.maturity_date",
         r'"Revolving Credit Maturity Date"\s+means\s+([^.]+)\.', 0.95),
    # -- commitments ---------------------------------------------------------
    Rule("initial_term_loan.commitment",
         r"Initial Term Loan Commitments on the Closing Date is\s+(\$[\d,]+)", 0.92),
    Rule("delayed_draw.commitment",
         r"Delayed Draw Term Loan Commitments on the Closing Date are\s+(\$[\d,]+)",
         0.92),
    Rule("revolver.commitment",
         r"Revolving Credit Commitments on the Closing Date\s+are\s+(\$[\d,]+)", 0.92),
    Rule("lc_sublimit",
         r"shall not exceed\s+(\$[\d,]+)\s*\(the \"Letter of Credit Sublimit\"\)",
         0.90),
    # -- pricing and fees ----------------------------------------------------
    Rule("libor_floor_pct",
         r"LIBO Rate shall not at any time be less than\s+([\d.]+%)", 0.92),
    Rule("commitment_fee_pct", r"commitment fee equal to\s+([\d.]+%)", 0.88),
    Rule("fronting_fee_pct", r"fronting fee\s+equal to\s+([\d.]+%)", 0.88),
    Rule("ticking_fee_pct", r"ticking fee[^.]{0,120}?equal to\s+([\d.]+%)", 0.85),
    Rule("excess_cash_flow.sweep_pct",
         r"prepay the Initial Term Loans with\s+([\d.]+%)\s+of Excess Cash Flow",
         0.88),
    # -- covenant and leverage ----------------------------------------------
    Rule("opening_total_leverage_ratio",
         r"Total Leverage Ratio is\s+([\d.]+:[\d.]+)", 0.90),
    Rule("incremental.leverage_based_test",
         r"Net Leverage Ratio would not exceed\s+([\d.]+:[\d.]+)", 0.85),
    # -- baskets -------------------------------------------------------------
    Rule("indebtedness.purchase_money_basket_amount",
         r"purchase money[^.]{0,200}?greater of\s+(\$[\d,]+)", 0.82),
    Rule("indebtedness.purchase_money_basket_ebitda_pct",
         r"purchase money[^.]{0,200}?greater of \$[\d,]+ and\s+([\d.]+%)\s+of "
         r"Consolidated EBITDA", 0.82,
         qualifier_probe=(
             r"after giving effect to the add-backs described in clause \(a\)",
             "ebitda_base", "post_addback",
         )),
    Rule("incremental.free_and_clear_amount",
         r"Incremental Term Facilities in an aggregate principal amount not to "
         r"exceed\s+the greater of\s+(\$[\d,]+)", 0.85),
    # -- MFN -----------------------------------------------------------------
    Rule("mfn_threshold_pct",
         r"exceeds the All-In Yield applicable to the Initial Term Loans by more "
         r"than\s+([\d.]+%)", 0.90),
    # An MFN sunset is a period, not a date, and it is written as a carve-out
    # from the MFN clause rather than as its own provision. Without this rule
    # the field is null whether or not a sunset exists, and the negative-space
    # validator ends up asserting absence on agreements that plainly have one.
    Rule("mfn_sunset",
         r"(?:shall (?:not apply|cease to apply)|shall no longer apply)[^.]{0,160}?"
         r"(?:after|following)\s+the date that is\s+"
         r"([a-z]+|\d+)\s+months?\s+after the Closing Date", 0.88),
    # -- Consolidated EBITDA construction ------------------------------------
    Rule("consolidated_ebitda.addback_cap_pct",
         r"shall not exceed\s+([\d.]+%)\s+of Consolidated EBITDA for such period",
         0.88,
         note="stated cap; check which clauses it actually governs"),
    Rule("consolidated_ebitda.addback_cap_clause_a_xvi",
         r"\(xvi\)[^;]{0,400}?set forth in the Sponsor Model[^;]{0,200}",
         0.88, external_document="Sponsor Model",
         note="clause (a)(xvi) is capped by the Sponsor Model, not by the "
              "stated percentage cap"),
)


class OfflineRuleBackend:
    """A deterministic backend: anchored patterns, exact spans, no network.

    This is tier 1/2 of the escalation ladder, and it is what makes the
    pipeline runnable and testable without an API key. It is genuinely weaker
    at recall than an LLM pass -- which is the point of the orphan sweep, and
    why the sweep is what rescues what the rules miss.
    """

    name = "offline"

    def __init__(self, rules: tuple[Rule, ...] = OFFLINE_RULES) -> None:
        self.rules = rules
        self._compiled = [(r, re.compile(r.pattern, r.flags)) for r in rules]

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Chunk,
        specs: list[FieldSpec],
        context: str,
        pass_id: str,
    ) -> tuple[list[Candidate], CostLedger]:
        wanted = {spec.name for spec in specs}
        by_name = {spec.name: spec for spec in specs}
        out: list[Candidate] = []
        for rule, compiled in self._compiled:
            if rule.field not in wanted:
                continue
            for match in compiled.finditer(chunk.text):
                span = chunk.locate(doc, match.group(0))
                if span is None:
                    # The match straddles a chunk-region boundary, so it is an
                    # artifact of concatenation rather than real document text.
                    continue
                qualifiers: dict[str, str] = {}
                if rule.qualifier_probe:
                    probe, key, value = rule.qualifier_probe
                    window = chunk.text[
                        max(0, match.start() - 400): match.end() + 400
                    ]
                    if re.search(probe, window, re.IGNORECASE):
                        qualifiers[key] = value
                if rule.external_document:
                    out.append(Candidate(
                        field=rule.field, value=None, span=span,
                        confidence=rule.confidence, pass_id=pass_id,
                        segmentation=chunk.segmentation,
                        external_document=rule.external_document,
                        qualifiers=qualifiers, notes=rule.note,
                    ))
                    continue
                spec = by_name[rule.field]
                value = _coerce(spec.kind, match.group(1))
                if value is None:
                    continue
                out.append(Candidate(
                    field=rule.field, value=value, span=span,
                    confidence=rule.confidence, pass_id=pass_id,
                    segmentation=chunk.segmentation,
                    qualifiers=qualifiers, notes=rule.note,
                ))
        return out, CostLedger(deterministic_calls=1)


# ---------------------------------------------------------------------------
# Tier 1: candidates straight out of the tables
# ---------------------------------------------------------------------------


def table_candidates(doc: NormalizedDocument) -> list[Candidate]:
    """Fields that live in a grid, parsed for free with exact cell spans."""
    out: list[Candidate] = []

    def add(field: str, value: Any, span: Span, confidence: float,
            notes: str | None = None, qualifiers: dict[str, str] | None = None,
            external: str | None = None) -> None:
        out.append(Candidate(
            field=field, value=value, span=span, confidence=confidence,
            pass_id="deterministic:tables", segmentation="structural",
            notes=notes, qualifiers=qualifiers or {}, external_document=external,
        ))

    for row in parse_commitment_table(doc):
        name = row["facility"].lower()
        if "revolv" in name:
            add("revolver.commitment", row["commitment"], row["span"], 0.95)
            if row["maturity"]:
                add("revolver.maturity_date", row["maturity"], row["span"], 0.95)
        elif "delayed draw" in name:
            add("delayed_draw.commitment", row["commitment"], row["span"], 0.95)
        elif "term loan" in name:
            add("initial_term_loan.commitment", row["commitment"], row["span"], 0.95)
            if row["maturity"]:
                add("initial_term_loan.maturity_date", row["maturity"],
                    row["span"], 0.95)

    schedule = parse_amortization_schedule(doc)
    if schedule and schedule.rows:
        amounts = [r.amount for r in schedule.rows]
        modal = max(set(amounts), key=amounts.count)
        anchor = next(r for r in schedule.rows if r.amount == modal)
        if anchor.span_start is not None and anchor.span_end is not None:
            add("amortization.quarterly_amount", modal,
                doc.span(anchor.span_start, anchor.span_end), 0.95,
                notes=f"modal payment across {len(amounts)} printed rows")

    grid = parse_covenant_grid(doc)
    if grid:
        add("financial_covenant.opening_level", grid[0][1], grid[0][2], 0.95)
        add("financial_covenant.final_level", grid[-1][1], grid[-1][2], 0.95)

    pricing = parse_pricing_grid(doc)
    margins = [
        (value, row["span"])
        for row in pricing
        for label, value in row.items()
        if label not in ("cells", "span") and "eurodollar" in str(label)
    ]
    if margins:
        top = max(margins, key=lambda pair: pair[0])
        add("applicable_margin.eurodollar_top_level_pct", top[0], top[1], 0.93,
            notes="highest grid level")
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    candidates: list[Candidate]
    cost: CostLedger
    chunks_seen: int
    contributing_chunks: set[str]


def run_passes(
    doc: NormalizedDocument,
    segments: dict[str, list[Chunk]],
    backend: ExtractionBackend,
    specs: list[FieldSpec] | None = None,
    graph=None,
    include_tables: bool = True,
    budget_usd: float | None = None,
) -> PassResult:
    """Run the target list over every segmentation.

    One pass per segmentation, so a field found by all three has genuinely
    independent support rather than three samples of the same view.
    """
    specs = specs or list(FIELD_REGISTRY.values())
    candidates: list[Candidate] = []
    cost = CostLedger()
    contributing: set[str] = set()
    seen = 0

    if include_tables:
        table_found = table_candidates(doc)
        candidates.extend(table_found)
        cost.deterministic_calls += 1

    for kind, chunks in segments.items():
        for chunk in chunks:
            seen += 1
            if budget_usd is not None and cost.total_usd >= budget_usd:
                break
            context = ""
            if graph is not None and chunk.segmentation == "definitional":
                context = graph.context_for(chunk.label)
            found, spent = backend.extract(
                doc, chunk, specs, context, pass_id=f"{backend.name}:{kind}"
            )
            cost.merge(spent)
            if found:
                contributing.add(chunk.chunk_id)
            candidates.extend(found)
    return PassResult(
        candidates=candidates,
        cost=cost,
        chunks_seen=seen,
        contributing_chunks=contributing,
    )
