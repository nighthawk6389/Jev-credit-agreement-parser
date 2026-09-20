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
    """A pass backend. Both implementations return spans or nothing."""

    name: str

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Span,
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
        chunk: Span,
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
    chunk: Span,
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
    by_name = {spec.name: spec for spec in specs}
    out: list[Candidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("field")
        spec = by_name.get(name)
        if spec is None:
            continue
        quote = (item.get("quote") or "").strip()
        span = _locate_quote(doc, chunk, quote)
        value = _coerce(spec.kind, item.get("value"))
        if value is not None and span is None:
            # No span, no value. Drop it rather than ship it unprovenanced.
            continue
        out.append(
            Candidate(
                field=name,
                value=value,
                span=span,
                confidence=float(item.get("confidence", 0.5)),
                pass_id=pass_id,
                segmentation=chunk.segmentation or "unknown",
                external_document=item.get("external_document"),
                qualifiers=item.get("qualifiers") or {},
                notes=item.get("notes"),
            )
        )
    return out


def _locate_quote(
    doc: NormalizedDocument, chunk: Span, quote: str
) -> Span | None:
    """Find a verbatim quote inside the chunk, tolerating whitespace drift."""
    if not quote:
        return None
    window = doc.text[chunk.start : chunk.end]
    index = window.find(quote)
    if index >= 0:
        return doc.span(chunk.start + index, chunk.start + index + len(quote))
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    match = re.search(pattern, window)
    if match:
        return doc.span(chunk.start + match.start(), chunk.start + match.end())
    return None
