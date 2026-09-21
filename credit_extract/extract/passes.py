"""Extraction passes, and the division of labour between them.

Three tiers, cheapest first, and the boundary between them is a design
decision rather than an accident of what was easy to write:

* **tables** parse deterministically in Python, for free, with spans exact to
  the character. Nothing below this tier is ever asked to read a grid.
* **rules** (``OFFLINE_RULES``) take the fields that are cheap and
  unambiguous -- a party named beside its role, a percentage next to the words
  that anchor it. They are deliberately scoped to those; see the note above
  the rule table for why extending them to the hard cases is a losing trade.
* **the model** takes everything the first two leave. :class:`LayeredBackend`
  is what enforces that: it runs the rules first and hands the model only the
  fields they did not settle, so the two never duplicate each other's work and
  the model is spent where judgement is actually needed.

Nothing at any tier does arithmetic. Totals, date comparison and counting live
in ``validate/invariants.py``, and whatever no tier settles is caught by the
orphan sweep, which runs regardless.

The passes run the same target list against three independent segmentations.
Disagreement between them is the signal reconciliation keys on, so they must
stay genuinely independent -- same prompt, different view of the document.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..ingest.tables import (
    Table, parse_date, parse_money, parse_percent, parse_ratio,
)
from ..models.core import CostLedger, Span
from ..models.quantities import Quantity, detect_scale, parse_quantity, quantity_for
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


def table_scale(doc: NormalizedDocument, table: Table) -> tuple[str, str] | None:
    """Find a scale declaration governing a table's money cells.

    A header or lead-in reading "(in thousands)" multiplies every figure
    beneath it and nothing in the cell records that. Looked for in the caption,
    the header labels, and the text immediately preceding the table, which is
    where filers most often put it.
    """
    lead_in = doc.text[max(0, table.start - 300):table.start]
    for source in (table.caption or "", " ".join(table.header_labels()), lead_in):
        found = detect_scale(source)
        if found:
            return found
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


_TEXT_GRID_RE = re.compile(
    r"^\s*(?P<level>[IVX]+|Level\s+[IVX\d]+|\d)\s+"
    r"(?P<band>[^\n]*?\d+\.\d+[^\n]*?)\s+"
    r"(?P<margins>\d+\.\d+%(?:\s+\d+\.\d+%)*)\s*$",
    re.MULTILINE,
)


def parse_pricing_grid_text(doc: NormalizedDocument) -> list[dict[str, Any]]:
    """Read a pricing grid that lost its table markup.

    A PDF-to-text pass, or a filer's tooling, leaves the grid as
    whitespace-separated columns. It is still a grid, and a parser that only
    understands ``<table>`` reports the margin as absent on a document that
    states it plainly.
    """
    out: list[dict[str, Any]] = []
    for match in _TEXT_GRID_RE.finditer(doc.text):
        margins = [Decimal(m) for m in re.findall(r"(\d+\.\d+)%", match.group("margins"))]
        if not margins:
            continue
        band = match.group("band")
        row: dict[str, Any] = {
            "cells": [
                match.group("level"), band,
                *[f"{m}%" for m in margins],
            ],
            "span": doc.span(match.start(), match.end()),
        }
        if "%" in band:
            # The band swallowed percentage columns, so the row has more
            # columns than the two this parser can name and the trailing one
            # is not the Eurodollar margin. On the investment-grade grid this
            # was found on -- four margin columns, revolver and term loan
            # against SOFR and base rate -- it is the base rate spread, and
            # naming it the Eurodollar margin put 0.550% into a field whose
            # answer is 1.550% at 0.93 confidence, outranking every other
            # tier. A row whose layout is unknown is emitted without a claim.
            out.append(row)
            continue
        row["eurodollar rate"] = margins[0]
        if len(margins) > 1:
            row["base rate"] = margins[1]
        out.append(row)
    return out


def parse_commitment_table(doc: NormalizedDocument) -> list[dict[str, Any]]:
    table = _find_table(doc, ("facility", "tranche", "lender"),
                        ("commitment", "amount", "principal"))
    if table is None:
        return []
    # A header reading "(in thousands)" multiplies every money cell beneath it
    # and nothing in the cell records that. Reading the cell alone is wrong by
    # three orders of magnitude and looks entirely reasonable.
    scale = table_scale(doc, table)
    out: list[dict[str, Any]] = []
    for cells in table.body_rows():
        if len(cells) < 2:
            continue
        quantity = parse_quantity(
            cells[1].text, prefer="money",
            scale=scale[0] if scale else None,
            scale_source=scale[1] if scale else None,
        )
        if quantity is None:
            continue
        normalized = quantity.normalized()
        maturity = parse_date(cells[2].text) if len(cells) > 2 else None
        out.append({
            "facility": cells[0].text,
            "commitment": normalized.value,
            "quantity": normalized,
            "scale": scale,
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
    #: The value with its unit attached, resolved at the point of extraction --
    #: which is the only place that still has the raw text and the table scale.
    quantity: Quantity | None = None

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


class ExtractionFailed(RuntimeError):
    """The model tier could not answer, which is not the same as finding nothing.

    Every subclass exists so that one specific way of getting no fields back
    stops looking like the document being silent about them. The caller may
    still choose to continue -- a chunk that refuses is not a reason to abandon
    a 500,000-character agreement -- but it has to choose, and the choice is
    recorded rather than implied by an empty list.
    """


class ExtractionRefused(ExtractionFailed):
    """A safety classifier declined the chunk."""


class ExtractionTruncated(ExtractionFailed):
    """The response hit ``max_tokens`` mid-answer."""


class ExtractionUnparseable(ExtractionFailed):
    """The response was not the JSON the schema constrains it to."""


#: What the model is constrained to return. Passed as ``output_config.format``
#: so the API enforces the shape; the prompt describes the *meaning* of each
#: field and the schema guarantees the envelope, which is the division of
#: labour that keeps a parser out of the middle of it.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    # A null value with a quote is how the model reports an
                    # external reference; a null value with no quote is how it
                    # reports "not in this chunk", which the orphan sweep and
                    # the negative-space validator take from here.
                    "value": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                    "confidence": {"type": "number"},
                    "external_document": {"type": ["string", "null"]},
                    # The measurement convention a bare number does not carry:
                    # which EBITDA a percentage is measured against, per annum
                    # against per quarter, the scale a table header sets.
                    "qualifiers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "notes": {"type": ["string", "null"]},
                },
                # Every property is required and the optional ones are nullable
                # instead. That is how the documented schemas are written, and
                # the prompt's response shape has to match this exactly: a key
                # the schema forbids is a 400, and a key the schema requires and
                # the prompt never mentions is a field the model leaves out.
                "required": [
                    "field", "value", "quote", "confidence",
                    "external_document", "qualifiers", "notes",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}


class AnthropicBackend:
    """Tier 4/5: a real LLM pass.

    Requires ``ANTHROPIC_API_KEY``. The prompt demands a verbatim quote for
    every value; any value whose quote cannot be located in the chunk is
    dropped rather than stored without provenance.

    The response shape is constrained by the API rather than negotiated in
    prose. An earlier version asked for JSON in the prompt and then pulled the
    first JSON-looking substring out of free text with a greedy regex,
    returning an empty candidate list when that failed to parse -- so a
    truncated response, a refusal, or a model that wrapped its answer in
    commentary all arrived at the caller as "this chunk contains none of these
    fields". That is the one confusion this repository exists to prevent, and
    it was sitting in the tier meant to do the hard extraction.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5",
        temperature: float = 0.0,
        max_tokens: int = 16_000,
        effort: str = "high",
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        #: This was "medium" on the theory that extraction is a reading task
        #: against text already in front of the model. The held-out documents
        #: say otherwise: the mistakes that matter here are not failures to
        #: find the clause, they are failures to pick the right branch of a
        #: definition that resolves four ways, the right column of a grid that
        #: has four, or the operative figure out of a recital that also names
        #: the superseded one. That is reasoning about a document, and each
        #: wrong answer is a silent error. No live run has measured the
        #: difference -- there is no key in this environment -- so this is an
        #: argument rather than a result, and a measurement should replace it.
        self.effort = effort
        self._client = None

    def with_temperature(self, temperature: float) -> "AnthropicBackend":
        """A sibling backend at a different temperature, sharing the client."""
        clone = AnthropicBackend(
            self.model, temperature, self.max_tokens, self.effort
        )
        clone._client = self._client
        return clone

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
        from .prompts import EXTRACTION_SYSTEM, build_extraction_prompt

        client = self._ensure_client()
        prompt = build_extraction_prompt(chunk.text, specs, context)
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # The system prompt existed in the prompts module and was never
            # sent, so the one instruction that frames the whole task -- a
            # confident wrong answer is worse than no answer -- reached the
            # model in no request this repository has ever built.
            system=EXTRACTION_SYSTEM,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
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
        if response.stop_reason == "refusal":
            # A decline is not an empty document. Say so and let the caller
            # decide; swallowing it would report every field in this chunk as
            # absent on the strength of a safety classifier.
            detail = getattr(response, "stop_details", None)
            raise ExtractionRefused(
                f"the model declined to read this chunk "
                f"({getattr(detail, 'category', None) or 'no category given'})"
            )
        if response.stop_reason == "max_tokens":
            raise ExtractionTruncated(
                f"the response hit max_tokens ({self.max_tokens}); the field "
                "list for this chunk is incomplete and reporting it as-is "
                "would understate what the chunk contains"
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
    """Turn a model response into candidates, dropping anything unprovenanced.

    Raises rather than returning an empty list when the envelope is wrong.
    ``output_config.format`` makes that a should-not-happen, and a
    should-not-happen that returns "no fields here" is how a whole chunk of a
    credit agreement goes quietly missing.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionUnparseable(
            f"the response is not JSON despite output_config.format: {exc}; "
            f"first 200 characters were {text[:200]!r}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list):
        raise ExtractionUnparseable(
            "the response parsed but is not the documented envelope; expected "
            f"an object with a 'fields' array, got {type(payload).__name__}"
        )
    payload = payload["fields"]
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
        written = item.get("value")
        value = _coerce(spec.kind, written)
        if value is not None and span is None:
            # No span, no value. A quote the model produced but the chunk does
            # not contain is a fabrication, so the value goes in the bin rather
            # than into the record without provenance.
            continue
        notes = item.get("notes")
        qualifiers = dict(item.get("qualifiers") or {})
        if written is not None and str(written).strip() and value is None:
            # The reader found a value and the field's declared kind could not
            # hold it -- a covenant written as "60%" against a field typed as a
            # ratio, say. Coercing anyway would invent a number and dropping it
            # silently says the document is silent, which it is not. Keep the
            # candidate valueless so nothing is asserted, carry the written
            # form for the reader, and flag it for the validators: "no value"
            # and "no provision" are different facts and only one of them is
            # true here.
            qualifiers["untypable_value"] = str(written).strip()
            notes = (
                f"stated as {str(written).strip()!r}, which this field's "
                f"{spec.kind} type cannot hold"
                + (f"; {notes}" if notes else "")
            )
        out.append(
            Candidate(
                field=name,
                value=value,
                span=span,
                confidence=float(item.get("confidence", 0.5)),
                pass_id=pass_id,
                segmentation=chunk.segmentation,
                external_document=item.get("external_document"),
                qualifiers=qualifiers,
                notes=notes,
                quantity=(
                    parse_quantity(str(written), prefer=spec.kind)
                    or quantity_for(value, spec.kind, as_written=str(written))
                ) if value is not None else None,
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
    #: Capture group naming the external document, when it varies. A magnitude
    #: replaced by "the amount set forth on Schedule 2.14" is an external
    #: reference whose target is written into the clause, not known in advance.
    external_group: int | None = None
    #: (probe regex, qualifier key, qualifier value) evaluated near the match.
    qualifier_probe: tuple[str, str, str] | None = None
    note: str | None = None


_ENTITY = r"([A-Z][A-Za-z0-9 ,.&'\-]{3,80}?)"

#: A percentage, however the drafter chose to write it. Requiring a literal
#: "%" means every clause quoted in basis points reads as an absent field --
#: and "50 basis points" is as common as "0.50%" in pricing and MFN clauses.
_PCT = r"[\d.]+\s*(?:%|bps\b|basis\s+points)"

#: Scope, and the reason for it.
#:
#: These rules take the fields that are cheap and unambiguous: a party named
#: beside its role in the preamble, a figure in a table, a percentage next to
#: the words that anchor it. On those they are exact, free, and better than a
#: model -- a table cell has a span to the character, and no sampling variance.
#:
#: They are deliberately **not** extended to cover the hard cases, and the
#: corpus is why. Across 100 real agreements, "is hereby amended" is followed
#: by 24 distinct phrasings, 13 of which occur exactly once; the same shape
#: shows up in how floors are drafted, how a credit spread adjustment is named,
#: and how a pricing grid is laid out. A pattern set chasing that tail grows
#: without bound, gets more fragile with every addition, and still misses the
#: 25th phrasing -- while every regex added to catch a rare form is a regex
#: that can misfire on a common one.
#:
#: So a field these rules do not settle is not a gap to be closed here. It is
#: handed to the model tier by :class:`LayeredBackend`, and whatever the model
#: does not settle either is still caught by the orphan sweep. Before adding a
#: rule, the question is not "does this match the document in front of me" but
#: "is this form common and unambiguous enough that a pattern beats a model".
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
         r"LIBO Rate shall not at any time be less than\s+(" + _PCT + r")", 0.92),
    Rule("commitment_fee_pct", r"commitment fee equal to\s+(" + _PCT + r")", 0.88),
    Rule("fronting_fee_pct", r"fronting fee\s+equal to\s+(" + _PCT + r")", 0.88),
    Rule("ticking_fee_pct", r"ticking fee[^.]{0,120}?equal to\s+(" + _PCT + r")", 0.85),
    Rule("excess_cash_flow.sweep_pct",
         r"prepay the Initial Term Loans with\s+(" + _PCT + r")\s+of Excess Cash Flow",
         0.88),
    # -- covenant and leverage ----------------------------------------------
    # A restatement often replaces a step-down grid with a single prose level.
    # Without this the field is only ever readable from a table, and an amended
    # covenant reads as absent.
    Rule("financial_covenant.opening_level",
         r"Total Leverage Ratio[^.]{0,140}?to exceed\s+([\d.]+:[\d.]+)", 0.80),
    Rule("financial_covenant.final_level",
         r"Total Leverage Ratio[^.]{0,140}?to exceed\s+([\d.]+:[\d.]+)", 0.70),
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
    # A magnitude can be replaced by a pointer to a schedule. If nothing
    # recognises the pointer the field simply comes back empty, the
    # negative-space validator is asked whether the agreement is silent on it,
    # and the honest answer -- "it is in a schedule you do not have" -- is
    # never reachable.
    Rule("incremental.free_and_clear_amount",
         r"Incremental Term Facilities in an aggregate principal amount not to "
         r"exceed\s+the amounts?\s+set forth (?:on|in)\s+"
         r"(Schedule\s+[\w.()-]+|Exhibit\s+[\w.()-]+|Annex\s+[\w.()-]+)",
         0.88, external_group=1,
         note="capacity is stated in a schedule rather than in the agreement"),
    # -- MFN -----------------------------------------------------------------
    Rule("mfn_threshold_pct",
         r"exceeds the All-In Yield applicable to the Initial Term Loans by more "
         r"than\s+(" + _PCT + r")", 0.90),
    # An MFN sunset is a period, not a date, and it is written as a carve-out
    # from the MFN clause rather than as its own provision. Without this rule
    # the field is null whether or not a sunset exists, and the negative-space
    # validator ends up asserting absence on agreements that plainly have one.
    Rule("mfn_sunset",
         r"(?:shall (?:not apply|cease to apply)|shall no longer apply)[^.]{0,160}?"
         r"(?:after|following)\s+the date that is\s+"
         r"([a-z]+|\d+)\s+months?\s+after the Closing Date", 0.88),
    # -- archetype-specific (F06) --------------------------------------------
    Rule("borrowing_base.advance_rate_accounts",
         r"([\d.]+%)\s+of\s+(?:the\s+)?(?:face\s+amount\s+of\s+)?[Ee]ligible "
         r"[Aa]ccounts", 0.88),
    Rule("borrowing_base.advance_rate_inventory",
         r"([\d.]+%)\s+of\s+(?:the\s+)?(?:value\s+of\s+)?[Ee]ligible "
         r"[Ii]nventory", 0.88),
    Rule("borrowing_base.availability_block",
         r"[Aa]vailability [Bb]lock[^.]{0,80}?(\$[\d,]+)", 0.85),
    Rule("arr.leverage_covenant_level",
         r"ARR Leverage Ratio[^.]{0,120}?exceed\s+([\d.]+:[\d.]+)", 0.88),
    Rule("arr.minimum_liquidity",
         r"[Mm]inimum [Ll]iquidity[^.]{0,80}?(\$[\d,]+)", 0.85),
    Rule("nav.loan_to_value_cap",
         r"[Ll]oan.[Tt]o.[Vv]alue [Rr]atio[^.]{0,80}?exceed\s+([\d.]+%)", 0.88),
    Rule("pik.toggle_step_up_pct",
         r"paid in kind[^.]{0,120}?increased by\s+([\d.]+%)", 0.85),
    # -- Consolidated EBITDA construction ------------------------------------
    Rule("consolidated_ebitda.addback_cap_pct",
         r"shall not exceed\s+(" + _PCT + r")\s+of Consolidated EBITDA for such "
         r"period", 0.88,
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

    def with_temperature(self, temperature: float) -> "OfflineRuleBackend":
        """Deterministic by construction: temperature has nothing to vary.

        Returning self rather than a copy is the honest answer -- extra passes
        over the same segmentation with this backend produce identical
        candidates, so they add no independent support and reconciliation
        counts segmentations rather than passes.
        """
        return self

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
                external = rule.external_document
                if rule.external_group is not None:
                    external = (match.group(rule.external_group) or "").strip()
                if external:
                    out.append(Candidate(
                        field=rule.field, value=None, span=span,
                        confidence=rule.confidence, pass_id=pass_id,
                        segmentation=chunk.segmentation,
                        external_document=external,
                        qualifiers=qualifiers, notes=rule.note,
                    ))
                    continue
                spec = by_name[rule.field]
                written = match.group(1)
                value = _coerce(spec.kind, written)
                if value is None:
                    continue
                quantity = parse_quantity(written, prefer=spec.kind)
                if quantity is None:
                    quantity = quantity_for(value, spec.kind, as_written=written)
                out.append(Candidate(
                    field=rule.field, value=value, span=span,
                    confidence=rule.confidence, pass_id=pass_id,
                    segmentation=chunk.segmentation,
                    qualifiers=qualifiers, notes=rule.note, quantity=quantity,
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
            external: str | None = None, quantity: Quantity | None = None) -> None:
        spec = FIELD_REGISTRY.get(field)
        if quantity is None and spec is not None:
            quantity = quantity_for(value, spec.kind, as_written=span.text)
        out.append(Candidate(
            field=field, value=value, span=span, confidence=confidence,
            pass_id="deterministic:tables", segmentation="structural",
            notes=notes, qualifiers=qualifiers or {}, external_document=external,
            quantity=quantity,
        ))

    for row in parse_commitment_table(doc):
        scale = row.get("scale")
        name = row["facility"].lower()
        if "revolv" in name:
            add("revolver.commitment", row["commitment"], row["span"], 0.95,
                quantity=row.get("quantity"))
            if row["maturity"]:
                add("revolver.maturity_date", row["maturity"], row["span"], 0.95)
        elif "delayed draw" in name:
            add("delayed_draw.commitment", row["commitment"], row["span"],
                0.95, quantity=row.get("quantity"))
        elif "term loan" in name:
            add("initial_term_loan.commitment", row["commitment"],
                row["span"], 0.95, quantity=row.get("quantity"))
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

    pricing = parse_pricing_grid(doc) or parse_pricing_grid_text(doc)
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
# The tier boundary
# ---------------------------------------------------------------------------


class LayeredBackend:
    """Deterministic rules take the easy fields; the model takes the rest.

    The division of labour this whole module is arranged around, and the one
    it previously failed to implement: ``run_passes`` took a single backend,
    so a run was either all-patterns or all-model, and the patterns were left
    trying to cover the whole distribution on their own.

    They cannot, and the corpus says so precisely. Across a hundred real
    agreements, "is hereby amended" is followed by twenty-four distinct
    phrasings, thirteen of which occur once. A pattern set chasing that tail
    grows without bound, gets more fragile with every addition, and still
    misses the twenty-fifth phrasing -- while the *easy* cases it does handle,
    a figure in a table or a date in a preamble, it handles at a precision no
    model matches and at no cost.

    So the rules are deliberately scoped to what is cheap and unambiguous.
    Every field they do not settle in a chunk is handed to the model, with the
    chunk text and the definitional context already assembled. Anything the
    model does not settle either is still caught by the orphan sweep, which
    runs regardless.

    The tier that produced each candidate stays on its ``pass_id``, so a
    reader can see which answers were free and which were inferred.
    """

    name = "layered"

    def __init__(
        self,
        deterministic: ExtractionBackend | None = None,
        model: ExtractionBackend | None = None,
    ) -> None:
        self.deterministic = deterministic or OfflineRuleBackend()
        self.model = model
        self.name = (
            f"{self.deterministic.name}+{self.model.name}" if self.model
            else self.deterministic.name
        )

    def with_temperature(self, temperature: float) -> "LayeredBackend":
        """Only the model tier varies; the rules are deterministic."""
        if self.model is None:
            return self
        vary = getattr(self.model, "with_temperature", lambda _t: self.model)
        return LayeredBackend(self.deterministic, vary(temperature))

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Chunk,
        specs: list[FieldSpec],
        context: str,
        pass_id: str,
    ) -> tuple[list[Candidate], CostLedger]:
        found, cost = self.deterministic.extract(
            doc, chunk, specs, context, pass_id=f"{pass_id}/rules"
        )
        if self.model is None:
            return found, cost

        settled = {candidate.field for candidate in found}
        remaining = [spec for spec in specs if spec.name not in settled]
        if not remaining:
            return found, cost

        inferred, model_cost = self.model.extract(
            doc, chunk, remaining, context, pass_id=f"{pass_id}/model"
        )
        cost.merge(model_cost)
        return [*found, *inferred], cost

    def tier_of(self, candidate: Candidate) -> str:
        return "model" if candidate.pass_id.endswith("/model") else "rules"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    candidates: list[Candidate]
    cost: CostLedger
    chunks_seen: int
    contributing_chunks: set[str]
    #: Chunks the model tier could not read, and why. An empty list is the
    #: normal case and says so; a non-empty one means some of the document was
    #: never considered by the extractor, which a reader has to know before
    #: taking any absence in this run at face value.
    unread_chunks: list[str] = dc_field(default_factory=list)

    def by_tier(self) -> dict[str, int]:
        """Distinct fields each tier settled.

        Reported rather than inferred, because the division of labour is the
        thing to watch: rules answering less over time means the pattern set
        has drifted from what documents look like, and the model answering
        everything means the cheap tier has stopped earning its place.
        """
        tiers: dict[str, set[str]] = {}
        for candidate in self.candidates:
            tier = (
                "model" if candidate.pass_id.endswith("/model")
                else "tables" if candidate.pass_id.endswith(":tables")
                else "rules"
            )
            tiers.setdefault(tier, set()).add(candidate.field)
        return {tier: len(fields) for tier, fields in sorted(tiers.items())}


#: Temperatures used for passes beyond the first round over each segmentation.
PASS_TEMPERATURES: tuple[float, ...] = (0.0, 0.3, 0.7, 1.0)


def plan_passes(
    segmentations: list[str], passes: int
) -> list[tuple[str, float, str]]:
    """Lay out ``passes`` passes as (segmentation, temperature, pass id).

    Segmentations come first and temperature second. Two passes over different
    views of the document disagree for reasons that mean something; two passes
    over the same view at different temperatures mostly resample the same
    reading, so they are only worth spending on once every segmentation has
    been covered.
    """
    if not segmentations:
        return []
    plan: list[tuple[str, float, str]] = []
    for index in range(max(1, passes)):
        kind = segmentations[index % len(segmentations)]
        round_index = index // len(segmentations)
        temperature = PASS_TEMPERATURES[min(round_index, len(PASS_TEMPERATURES) - 1)]
        suffix = f"@{temperature}" if round_index else ""
        plan.append((kind, temperature, f"{kind}{suffix}"))
    return plan


def run_passes(
    doc: NormalizedDocument,
    segments: dict[str, list[Chunk]],
    backend: ExtractionBackend,
    specs: list[FieldSpec] | None = None,
    graph=None,
    include_tables: bool = True,
    budget_usd: float | None = None,
    passes: int = 3,
) -> PassResult:
    """Run the target list over every segmentation, at least ``passes`` times.

    One pass per segmentation by default, so a field found by all three has
    genuinely independent support rather than three samples of the same view.
    """
    specs = specs or list(FIELD_REGISTRY.values())
    candidates: list[Candidate] = []
    cost = CostLedger()
    contributing: set[str] = set()
    unread: list[str] = []
    seen = 0

    if include_tables:
        candidates.extend(table_candidates(doc))
        cost.deterministic_calls += 1

    populated = [kind for kind, chunks in segments.items() if chunks]
    stop = False
    for kind, temperature, pass_id in plan_passes(populated, passes):
        if stop:
            break
        worker = getattr(backend, "with_temperature", lambda _t: backend)(temperature)
        for chunk in segments[kind]:
            if budget_usd is not None and cost.total_usd >= budget_usd:
                stop = True
                break
            seen += 1
            context = ""
            if graph is not None and chunk.segmentation == "definitional":
                context = graph.context_for(chunk.label)
            try:
                found, spent = worker.extract(
                    doc, chunk, specs, context, pass_id=f"{backend.name}:{pass_id}"
                )
            except ExtractionFailed as exc:
                # One chunk that could not be read is not a reason to abandon a
                # 500,000-character agreement, but it is also not a chunk that
                # contained none of these fields. Record which chunk and why,
                # and let the report say so; the orphan sweep still runs over
                # it, so the text is not silently dropped from consideration.
                unread.append(f"{chunk.chunk_id}: {exc}")
                continue
            cost.merge(spent)
            if found:
                contributing.add(chunk.chunk_id)
            candidates.extend(found)
    return PassResult(
        candidates=candidates,
        cost=cost,
        chunks_seen=seen,
        contributing_chunks=contributing,
        unread_chunks=unread,
    )


# ---------------------------------------------------------------------------
# Conditional and time-varying fields (family F10)
# ---------------------------------------------------------------------------

_PERIOD_RANGE_RE = re.compile(
    r"(?P<from>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+(?:through|to|until)\s+"
    r"(?P<to>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
)
_PERIOD_OPEN_RE = re.compile(
    r"(?P<from>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+and\s+thereafter",
)
#: "shall apply only if ... exceeds 35% of ..." -- a springing covenant is not
#: in force until its trigger is, and a scalar cannot say that.
_SPRINGING_RE = re.compile(
    r"(?:Financial\s+Covenant|covenant\s+set\s+forth\s+in\s+this\s+Section)"
    r"[^.]{0,200}?(?:shall\s+apply|shall\s+be\s+tested)\s+only\s+"
    r"(?:if|when|during\s+any\s+period\s+(?:in\s+)?which)\s+"
    r"(?P<subject>[^.]{0,120}?)\s+exceeds?\s+(?P<threshold>[\d.]+%)",
    re.IGNORECASE,
)
_POST_IPO_RE = re.compile(
    r"(?:from\s+and\s+after|following|on\s+and\s+after)\s+(?:the\s+)?"
    r"(?:consummation\s+of\s+(?:a|an|the)\s+)?(?:Qualifying\s+)?(?:IPO|Initial\s+"
    r"Public\s+Offering)[^.]{0,160}?(?P<level>[\d.]+:[\d.]+)",
    re.IGNORECASE,
)


def parse_covenant_variants(doc: NormalizedDocument) -> list[dict[str, Any]]:
    """Read the covenant grid as time-varying variants, not as two scalars.

    A step-down grid *is* a field that changes over time. Storing only the
    opening and final levels answers "what is the covenant?" with a number that
    is wrong for most of the life of the loan; storing variants lets the
    question be asked as "what is the covenant on 30 June 2021?" and answered.
    """
    out: list[dict[str, Any]] = []
    for label, level, span in parse_covenant_grid(doc):
        effective_from: date | None = None
        effective_to: date | None = None
        ranged = _PERIOD_RANGE_RE.search(label)
        if ranged:
            effective_from = parse_date(ranged.group("from"))
            effective_to = parse_date(ranged.group("to"))
        else:
            open_ended = _PERIOD_OPEN_RE.search(label)
            if open_ended:
                effective_from = parse_date(open_ended.group("from"))
            else:
                effective_to = parse_date(label)
        out.append({
            "label": label,
            "level": level,
            "span": span,
            "effective_from": effective_from,
            "effective_to": effective_to,
        })
    # Highest precedence first, which for a pure schedule means latest first:
    # a later step-down governs once its window opens.
    out.sort(
        key=lambda row: row["effective_from"] or date.min, reverse=True
    )
    return out


def parse_springing_condition(doc: NormalizedDocument) -> dict[str, Any] | None:
    """A covenant that is not in force until a trigger fires."""
    match = _SPRINGING_RE.search(doc.text)
    if match is None:
        return None
    subject = " ".join(match.group("subject").split())
    variable = re.sub(r"[^a-z0-9]+", "_", subject.lower()).strip("_") or "trigger"
    threshold = parse_percent(match.group("threshold"))
    return {
        "expr": f"{variable} > {threshold}",
        "subject": subject,
        "threshold": threshold,
        "span": doc.span(match.start(), match.end()),
    }


def parse_post_ipo_level(doc: NormalizedDocument) -> dict[str, Any] | None:
    """A level that only applies once an IPO has happened."""
    match = _POST_IPO_RE.search(doc.text)
    if match is None:
        return None
    return {
        "expr": "ipo_completed == true",
        "level": parse_ratio(match.group("level")),
        "span": doc.span(match.start(), match.end()),
    }
