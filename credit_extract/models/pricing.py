"""The all-in rate is a composed object, not a scalar.

A credit spread adjustment folded into the margin produces a wrong yield, and
the wrong yield then produces a wrong MFN trigger comparison -- which is how a
scale error in one field becomes a wrong answer about whether an incremental
facility repriced the existing loans. The components stay separate.

The floor is the other one that has to be kept straight. A floor on the base
rate and a floor on the all-in rate are different instruments: at a SOFR of
0.05% with a 1.00% floor and a 5.00% margin, a base-rate floor yields 6.00%
and an all-in floor yields 5.05%. Both appear in the market and documents say
which in one clause.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from .quantities import Quantity

#: Counted across 100 EDGAR agreements: Term SOFR 71, Daily Simple SOFR 49,
#: Prime 46, ABR 35, SONIA 19, EURIBOR 18, CDOR/CORRA 16, TIBOR 6, LIBOR 6,
#: SARON 2. A model that stops at SOFR describes two thirds of the market and
#: silently reports "unknown" for the rest, which reads the same as a document
#: it could not parse at all.
BenchmarkBase = Literal[
    "term_sofr", "daily_simple_sofr", "abr", "prime", "libor", "euribor",
    "sonia", "saron", "tibor", "cdor", "corra", "bbsw", "canadian_prime",
    "unknown",
]

FloorApplies = Literal["base_rate", "all_in"]


class GridLevel(BaseModel):
    """One row of a pricing grid, with the leverage band it applies in."""

    level: str
    leverage_from: Decimal | None = None      # exclusive lower bound
    leverage_to: Decimal | None = None        # inclusive upper bound
    margin: Quantity | None = None
    base_rate_margin: Quantity | None = None
    text: str = ""

    @property
    def band(self) -> tuple[Decimal | None, Decimal | None]:
        return self.leverage_from, self.leverage_to


class Pricing(BaseModel):
    """Everything that composes into the rate actually paid."""

    base: BenchmarkBase = "unknown"
    tenor: str | None = None
    #: Kept separate from the margin on purpose. Folding a CSA into the margin
    #: overstates the spread and then overstates the MFN comparison.
    credit_spread_adjustment: Quantity | None = None
    margin: Quantity | None = None
    floor: Quantity | None = None
    floor_applies_to: FloorApplies | None = None
    grid: list[GridLevel] = Field(default_factory=list)
    benchmark_waterfall: list[str] = Field(default_factory=list)
    spans: list[dict] = Field(default_factory=list)

    @property
    def is_flat(self) -> bool:
        return not self.grid

    @property
    def csa_bps(self) -> Decimal | None:
        if self.credit_spread_adjustment is None:
            return None
        return self.credit_spread_adjustment.to("bps").value

    def all_in(self, base_rate: Quantity) -> Quantity:
        """Compute the rate paid. Python does this; no model is asked to.

        Order matters and is the whole reason the parts are kept apart: the
        base-rate floor applies before the margin is added, the all-in floor
        applies after.
        """
        rate = base_rate.to("percent").value
        csa = (
            self.credit_spread_adjustment.to("percent").value
            if self.credit_spread_adjustment else Decimal(0)
        )
        margin = self.margin.to("percent").value if self.margin else Decimal(0)
        floor = self.floor.to("percent").value if self.floor else None

        if floor is not None and self.floor_applies_to == "base_rate":
            rate = max(rate, floor)
        total = rate + csa + margin
        if floor is not None and self.floor_applies_to == "all_in":
            total = max(total, floor)
        return Quantity(value=total, unit="percent", as_written="computed")

    def describe(self) -> str:
        parts = [self.base + (f" ({self.tenor})" if self.tenor else "")]
        if self.credit_spread_adjustment:
            parts.append(f"+ CSA {self.credit_spread_adjustment.value}"
                         f"{self.credit_spread_adjustment.unit}")
        if self.margin:
            parts.append(f"+ margin {self.margin.value}%")
        if self.floor:
            parts.append(
                f"floor {self.floor.value}% on "
                f"{self.floor_applies_to or 'unspecified'}"
            )
        if self.grid:
            parts.append(f"{len(self.grid)}-level grid")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_BASE_PATTERNS: tuple[tuple[str, BenchmarkBase], ...] = (
    (r"\bDaily\s+Simple\s+SOFR\b", "daily_simple_sofr"),
    (r"\b(?:Adjusted\s+)?Term\s+SOFR\b", "term_sofr"),
    (r"\bSOFR\b", "term_sofr"),
    (r"\bEURIBOR\b", "euribor"),
    (r"\bSONIA\b", "sonia"),
    (r"\bSARON\b", "saron"),
    (r"\bTIBOR\b", "tibor"),
    (r"\bTerm\s+CORRA\b|\bCORRA\b", "corra"),
    (r"\bCDOR\b", "cdor"),
    (r"\bBBSW\b", "bbsw"),
    (r"\bLIBO(?:R)?\s+Rate\b", "libor"),
    (r"\bCanadian\s+Prime(?:\s+Rate)?\b", "canadian_prime"),
    (r"\bAlternate\s+Base\s+Rate\b|\bABR\b", "abr"),
    (r"\bPrime\s+Rate\b", "prime"),
)

#: Every name the market gives the same thing. "Term SOFR Credit Adjustment
#: Spread" is a real definition in two of the hundred agreements sampled, and a
#: pattern anchored on "Credit Spread Adjustment" misses it -- which reads as a
#: deal with no adjustment rather than as an adjustment that was not found.
#: Deliberately does not include a bare "Spread Adjustment" or "Benchmark
#: Replacement Adjustment". Every modern agreement carries benchmark-transition
#: boilerplate promising a spread adjustment *if* SOFR is ever replaced, and
#: matching that reads a contingency as a term of the deal -- which is how this
#: check came to fire on two agreements that plainly have no adjustment today.
_CSA_NAME = (
    r"(?:Credit\s+Spread\s+Adjustment|Credit\s+Adjustment\s+Spread"
    r"|SOFR\s+Adjustment|Adjusted\s+Term\s+SOFR)"
)
_CSA_MENTION_RE = re.compile(_CSA_NAME, re.IGNORECASE)
_CSA_RE = re.compile(
    _CSA_NAME + r"[^.]{0,400}?"
    r"(?P<value>\d+(?:\.\d+)?\s*(?:%|basis\s+points|bps))",
    re.IGNORECASE | re.DOTALL,
)
_TENOR_RE = re.compile(
    r"\b(one|two|three|six|twelve|1|2|3|6|12)[\s-]*month\b", re.IGNORECASE
)
_FLOOR_RE = re.compile(
    r"(?P<subject>[A-Z][\w\s-]{0,40}?)\s+shall\s+(?:not\s+(?:at\s+any\s+time\s+)?"
    r"be\s+less\s+than|be\s+deemed\s+to\s+be\s+not\s+less\s+than)\s+"
    r"(?P<value>\d+(?:\.\d+)?%)",
    re.IGNORECASE,
)
_ALL_IN_FLOOR_RE = re.compile(
    r"All-?In\s+(?:Yield|Rate)[^.]{0,120}?"
    r"shall\s+not\s+be\s+less\s+than\s+(\d+(?:\.\d+)?%)",
    re.IGNORECASE,
)
#: The commonest way a base-rate floor is actually drafted: not as a sentence
#: saying the rate shall not be less than X, but structurally, inside the
#: benchmark's own definition -- '"Term SOFR" means ... the greater of (i)
#: 0.25% and (ii) the Term SOFR Reference Rate'. Fifteen of the hundred
#: agreements sampled state their floor this way and no other, so a pattern
#: that only reads the sentence form reports them as having no floor.
_GREATER_OF_FLOOR_RE = re.compile(
    r"\bmeans?\b[^.]{0,200}?the\s+greater\s+of\s*"
    r"(?:\([ivx\d]{1,3}\)|\([a-z]\))?\s*"
    r"(?:(?P<words>[a-z\- ]{3,30})\s*\(\s*)?"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:%|percent)"
    r"[^.]{0,160}?(?:Reference\s+Rate|Screen\s+Rate|SOFR|EURIBOR|SONIA)",
    re.IGNORECASE | re.DOTALL,
)
_WATERFALL_RE = re.compile(
    r"Benchmark\s+Replacement[^.]{0,900}",
    re.IGNORECASE | re.DOTALL,
)
_WATERFALL_ITEM_RE = re.compile(
    r"\((?:[a-z]|\d)\)\s*(?P<item>"
    r"Adjusted\s+Term\s+SOFR|Daily\s+Simple\s+SOFR|Daily\s+Compounded\s+SOFR|"
    r"Term\s+SOFR|ISDA\s+Fallback\s+Rate|"
    r"the\s+alternate\s+rate[^;.)]{0,80}|SOFR[^;.)]{0,40}"
    r")",
    re.IGNORECASE,
)
_BAND_RE = re.compile(
    r"(?P<direction>greater\s+than|less\s+than\s+or\s+equal\s+to|"
    r"less\s+than|equal\s+to\s+or\s+greater\s+than)\s+"
    r"(?P<value>\d+(?:\.\d+)?)(?::\d+(?:\.\d+)?)?",
    re.IGNORECASE,
)


def detect_base(text: str) -> tuple[BenchmarkBase, str | None]:
    for pattern, base in _BASE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        window = text[max(0, match.start() - 120): match.end() + 120]
        tenor_match = _TENOR_RE.search(window)
        tenor = (
            f"{tenor_match.group(1).lower()}-month" if tenor_match else None
        )
        return base, tenor
    return "unknown", None


def parse_band(text: str) -> tuple[Decimal | None, Decimal | None]:
    """Read a grid row's leverage band. Bands are inclusive at the top."""
    lower: Decimal | None = None
    upper: Decimal | None = None
    for match in _BAND_RE.finditer(text):
        direction = match.group("direction").lower()
        value = Decimal(match.group("value"))
        if direction.startswith("greater"):
            lower = value
        elif direction.startswith("equal to or greater"):
            lower = value
        else:
            upper = value
    return lower, upper


def _interest_text(doc) -> str:
    """The clause that states the rate actually charged.

    Searched in preference to the whole document, because the benchmark
    replacement waterfall names every fallback rate there is. Reading the base
    off the document at large returns whichever fallback is listed first and
    reports a Term SOFR loan as Daily Simple SOFR.
    """
    for marker in getattr(doc, "sections", []):
        title = (marker.title or "").lower()
        if "interest" in title and "period" not in title:
            span = doc.section_span(marker.section_id)
            if span is not None:
                return span.text
    return ""


def parse_pricing(doc, pricing_rows: list[dict] | None = None) -> Pricing:
    """Decompose the rate from the document, keeping the parts apart."""
    text = doc.text
    base, tenor = detect_base(_interest_text(doc))
    if base == "unknown":
        base, tenor = detect_base(text)

    csa: Quantity | None = None
    csa_match = _CSA_RE.search(text)
    if csa_match:
        from .quantities import parse_quantity
        csa = parse_quantity(csa_match.group("value"))

    floor: Quantity | None = None
    floor_applies: FloorApplies | None = None
    all_in_floor = _ALL_IN_FLOOR_RE.search(text)
    if all_in_floor:
        from .quantities import parse_quantity
        floor = parse_quantity(all_in_floor.group(1))
        floor_applies = "all_in"
    else:
        floor_match = _FLOOR_RE.search(text)
        if floor_match:
            from .quantities import parse_quantity
            floor = parse_quantity(floor_match.group("value"))
            subject = floor_match.group("subject").lower()
            floor_applies = (
                "all_in" if "all-in" in subject or "all in" in subject
                else "base_rate"
            )
        else:
            structural = _GREATER_OF_FLOOR_RE.search(text)
            if structural:
                from .quantities import parse_quantity
                floor = parse_quantity(structural.group("value") + "%")
                # Inside the benchmark's own definition, so by construction it
                # applies before the margin.
                floor_applies = "base_rate"

    waterfall: list[str] = []
    waterfall_match = _WATERFALL_RE.search(text)
    if waterfall_match:
        seen: set[str] = set()
        for item in _WATERFALL_ITEM_RE.finditer(waterfall_match.group(0)):
            candidate = " ".join(item.group("item").split())
            key = candidate.lower()
            if key not in seen:
                seen.add(key)
                waterfall.append(candidate)

    grid: list[GridLevel] = []
    for row in pricing_rows or []:
        cells = row.get("cells") or []
        label = cells[0] if cells else ""
        band_text = " ".join(cells[1:2]) if len(cells) > 1 else ""
        lower, upper = parse_band(band_text)
        margin = next(
            (v for k, v in row.items()
             if isinstance(k, str) and "eurodollar" in k.lower()
             and isinstance(v, Decimal)),
            None,
        )
        base_margin = next(
            (v for k, v in row.items()
             if isinstance(k, str) and "base" in k.lower()
             and isinstance(v, Decimal)),
            None,
        )
        grid.append(GridLevel(
            level=label,
            leverage_from=lower,
            leverage_to=upper,
            margin=Quantity(value=margin, unit="percent") if margin is not None else None,
            base_rate_margin=(
                Quantity(value=base_margin, unit="percent")
                if base_margin is not None else None
            ),
            text=" | ".join(cells),
        ))

    margin: Quantity | None = None
    if grid:
        margins = [level.margin for level in grid if level.margin is not None]
        if margins:
            margin = max(margins, key=lambda q: q.value)

    return Pricing(
        base=base, tenor=tenor, credit_spread_adjustment=csa, margin=margin,
        floor=floor, floor_applies_to=floor_applies, grid=grid,
        benchmark_waterfall=waterfall,
    )
