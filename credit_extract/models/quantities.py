"""Units, carried explicitly and normalized at the boundary.

Every numeric extraction stores its unit. Getting scale wrong is a 1000x error
that looks completely reasonable on the page and in the output, and it is the
only class of error here that no amount of span-checking catches: the span is
correct, the digits are correct, and the number is wrong by three orders of
magnitude.

Two sources of scale, and both have to be handled:

* the figure itself -- ``50 bps`` and ``0.50%`` are the same quantity;
* the table it sits in -- a header reading "(in thousands)" multiplies every
  money cell beneath it, and nothing in the cell says so.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

Unit = Literal[
    "bps", "percent", "ratio", "multiple_of_ebitda",
    "USD", "USD_thousands", "USD_millions",
    "days", "months", "years", "count",
]

#: What each unit normalizes to. Comparisons only ever happen in canonical units.
CANONICAL: dict[str, str] = {
    "bps": "percent", "percent": "percent",
    "ratio": "ratio", "multiple_of_ebitda": "ratio",
    "USD": "USD", "USD_thousands": "USD", "USD_millions": "USD",
    "days": "days", "months": "months", "years": "months",
    "count": "count",
}

#: Multiplier to reach the canonical unit.
_FACTOR: dict[str, Decimal] = {
    "bps": Decimal("0.01"),
    "percent": Decimal(1),
    "ratio": Decimal(1),
    "multiple_of_ebitda": Decimal(1),
    "USD": Decimal(1),
    "USD_thousands": Decimal(1_000),
    "USD_millions": Decimal(1_000_000),
    "days": Decimal(1),
    "months": Decimal(1),
    "years": Decimal(12),
    "count": Decimal(1),
}


class UnitMismatch(ValueError):
    """Two quantities that do not share a canonical unit were compared."""


class Quantity(BaseModel):
    """A number that knows what it is."""

    model_config = ConfigDict(frozen=True)

    value: Decimal
    unit: Unit
    #: Exactly what the document printed, before any normalization.
    as_written: str | None = None
    #: Where a table-level scale came from, when one was applied.
    scale_source: str | None = None

    @model_validator(mode="after")
    def _finite(self) -> "Quantity":
        if not self.value.is_finite():
            raise ValueError(f"non-finite quantity {self.value}")
        return self

    @property
    def canonical_unit(self) -> str:
        return CANONICAL[self.unit]

    @property
    def canonical_value(self) -> Decimal:
        return self.value * _FACTOR[self.unit]

    def normalized(self) -> "Quantity":
        """The same quantity in its canonical unit, keeping what was written."""
        target = self.canonical_unit
        if target == "percent":
            unit: Unit = "percent"
        elif target == "USD":
            unit = "USD"
        elif target == "months":
            unit = "months"
        elif target == "ratio":
            unit = self.unit if self.unit == "multiple_of_ebitda" else "ratio"
        else:
            unit = self.unit  # type: ignore[assignment]
        return Quantity(
            value=self.canonical_value,
            unit=unit,
            as_written=self.as_written or f"{self.value} {self.unit}",
            scale_source=self.scale_source,
        )

    def to(self, unit: Unit) -> "Quantity":
        if CANONICAL[unit] != self.canonical_unit:
            raise UnitMismatch(
                f"cannot convert {self.unit} to {unit}: "
                f"{self.canonical_unit} is not {CANONICAL[unit]}"
            )
        return Quantity(
            value=self.canonical_value / _FACTOR[unit],
            unit=unit,
            as_written=self.as_written,
            scale_source=self.scale_source,
        )

    def compare(self, other: "Quantity") -> int:
        if self.canonical_unit != other.canonical_unit:
            raise UnitMismatch(
                f"{self.unit} and {other.unit} are not comparable "
                f"({self.canonical_unit} vs {other.canonical_unit})"
            )
        a, b = self.canonical_value, other.canonical_value
        return (a > b) - (a < b)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.value} {self.unit}"


# ---------------------------------------------------------------------------
# Parsing at the boundary
# ---------------------------------------------------------------------------

_BPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bps|bp\b|basis\s+points?)", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s+cent\b)", re.IGNORECASE)
_MULTIPLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\b", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d+)\s*months?\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*days?\b", re.IGNORECASE)
_YEARS_RE = re.compile(r"(\d+)\s*years?\b", re.IGNORECASE)

#: "(in thousands)", "dollars in millions, except per share", "$ in 000s"
_SCALE_RE = re.compile(
    r"\((?:[^)]*?\b)?(?:in|amounts?\s+in|dollars?\s+in|\$\s*in)\s+"
    r"(thousands|millions|000s|000’s|000's)\b[^)]*\)"
    r"|\b(?:amounts?|dollars?|figures?)\s+in\s+(thousands|millions)\b",
    re.IGNORECASE,
)


def detect_scale(text: str) -> tuple[Unit, str] | None:
    """Find a table-level scale declaration. Returns (unit, the phrase)."""
    match = _SCALE_RE.search(text or "")
    if not match:
        return None
    word = (match.group(1) or match.group(2) or "").lower()
    unit: Unit = "USD_millions" if word.startswith("million") else "USD_thousands"
    return unit, match.group(0)


def parse_quantity(
    text: str,
    prefer: str | None = None,
    scale: Unit | None = None,
    scale_source: str | None = None,
) -> Quantity | None:
    """Parse a figure into a Quantity, applying a table scale to money.

    ``prefer`` is the field's declared kind, used only to break ties -- a bare
    number with no marker takes the field's unit rather than being guessed.
    """
    from ..ingest.tables import parse_money, parse_ratio

    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    if prefer in ("date", "text", "bool"):
        # A date is not a quantity. Left to fall through, "August 1, 2024"
        # parses as the money value 1 and the field ships with a unit of USD.
        return None

    bps = _BPS_RE.search(raw)
    if bps:
        return Quantity(value=Decimal(bps.group(1)), unit="bps", as_written=raw)
    pct = _PCT_RE.search(raw)
    if pct:
        return Quantity(value=Decimal(pct.group(1)), unit="percent", as_written=raw)
    multiple = _MULTIPLE_RE.search(raw)
    if multiple:
        return Quantity(
            value=Decimal(multiple.group(1)), unit="multiple_of_ebitda",
            as_written=raw,
        )
    ratio = parse_ratio(raw)
    if ratio is not None and ":" in raw:
        return Quantity(value=ratio, unit="ratio", as_written=raw)
    for pattern, unit in ((_MONTHS_RE, "months"), (_DAYS_RE, "days"),
                          (_YEARS_RE, "years")):
        match = pattern.search(raw)
        if match:
            return Quantity(
                value=Decimal(match.group(1)), unit=unit, as_written=raw,
            )

    money = parse_money(raw)
    if money is None:
        return None
    looks_like_money = "$" in raw or prefer == "money"
    if not looks_like_money and prefer in ("percent", "ratio"):
        unit = "percent" if prefer == "percent" else "ratio"
        return Quantity(value=money, unit=unit, as_written=raw)
    if scale is not None and looks_like_money:
        # A cell under a "(in thousands)" header says 1,250 and means
        # $1,250,000. Nothing in the cell records that.
        return Quantity(
            value=money, unit=scale, as_written=raw, scale_source=scale_source,
        )
    return Quantity(value=money, unit="USD", as_written=raw)


#: Field kind -> the unit a bare number in that field should be read as.
KIND_TO_UNIT: dict[str, Unit] = {
    "money": "USD",
    "percent": "percent",
    "ratio": "ratio",
    "int": "count",
    "date": "count",
    "text": "count",
    "bool": "count",
}


def quantity_for(value: Any, kind: str, as_written: str | None = None) -> Quantity | None:
    """Attach the field's declared unit to an already-parsed value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 - non-numeric values simply have no quantity
        return None
    unit = KIND_TO_UNIT.get(kind)
    if unit is None or kind in ("date", "text", "bool"):
        return None
    return Quantity(value=number, unit=unit, as_written=as_written)
