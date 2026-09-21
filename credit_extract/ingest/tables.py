"""Structured tables plus the deterministic scalar parsers.

Amortization schedules, covenant grids and pricing grids are tables. Flattening
them into prose is where a large share of long-tail errors come from, so a
table survives ingestion as a grid of cells, each carrying its own offsets into
the normalized character space.

Everything in this module is tier 1 of the escalation ladder: pure Python, free,
and never delegated to a model.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field


class Cell(BaseModel):
    row: int
    col: int
    text: str
    start: int
    end: int
    is_header: bool = False
    colspan: int = 1
    rowspan: int = 1


class Table(BaseModel):
    """A grid preserved from the source document, anchored in the offset space."""

    table_id: str
    start: int
    end: int
    cells: list[Cell] = Field(default_factory=list)
    caption: str | None = None
    section_id: str | None = None
    page: int | None = None

    @property
    def n_rows(self) -> int:
        return (max((c.row for c in self.cells), default=-1)) + 1

    @property
    def n_cols(self) -> int:
        return (max((c.col for c in self.cells), default=-1)) + 1

    def rows(self) -> list[list[Cell]]:
        grid: list[list[Cell]] = [[] for _ in range(self.n_rows)]
        for cell in sorted(self.cells, key=lambda c: (c.row, c.col)):
            grid[cell.row].append(cell)
        return grid

    def header_labels(self) -> list[str]:
        head = [c for c in self.cells if c.is_header]
        if not head:
            first = [c for c in self.cells if c.row == 0]
            head = first
        return [c.text for c in sorted(head, key=lambda c: c.col)]

    def body_rows(self) -> list[list[Cell]]:
        header_rows = {c.row for c in self.cells if c.is_header}
        if not header_rows:
            header_rows = {0}
        return [r for i, r in enumerate(self.rows()) if i not in header_rows and r]

    def text_block(self) -> str:
        return "\n".join(" | ".join(c.text for c in row) for row in self.rows())

    def looks_like(self, *keywords: str) -> bool:
        blob = (self.caption or "") + " " + " ".join(self.header_labels())
        blob = blob.lower()
        return any(k.lower() in blob for k in keywords)


# ---------------------------------------------------------------------------
# Deterministic scalar parsing
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(
    r"""\$?\s*
        (?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)
        (?:\s*(?P<scale>million|billion|thousand|mm|bn))?""",
    re.VERBOSE | re.IGNORECASE,
)
_SCALES = {
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "mm": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
}
_PERCENT_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*%|(?P<bps>\d+(?:\.\d+)?)\s*(?:bps|basis points)",
    re.IGNORECASE,
)
_RATIO_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?::|to)\s*(?P<b>\d+(?:\.\d+)?)", re.IGNORECASE
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july august september "
        "october november december".split()
    )
}
_DATE_RE = re.compile(
    r"(?P<month>" + "|".join(_MONTHS) + r")\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
    r"|(?P<m2>\d{1,2})/(?P<d2>\d{1,2})/(?P<y2>\d{2,4})"
    r"|(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2})",
    re.IGNORECASE,
)


def parse_money(text: str) -> Decimal | None:
    """``$1,505,000`` / ``$10.5 million`` -> Decimal. None when not a number."""
    if text is None:
        return None
    cleaned = text.strip().replace("−", "-")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    match = _MONEY_RE.search(cleaned)
    if not match:
        return None
    if cleaned[match.end("num"):match.end("num") + 1].strip()[:1] == "%":
        # A percentage is not an amount, and this returned one. Amortisation is
        # routinely drafted as "1.250% of the initial principal amount" per
        # instalment, and reading the first number out of that gives a
        # quarterly payment of one dollar twenty-five against a real one of
        # $7,875,000. The regex never looked at the unit, so every percentage
        # handed to a money-typed field became a dollar figure with a citation
        # behind it -- which is the shape of a silent error, not of a miss.
        return None
    try:
        value = Decimal(match.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    scale = match.group("scale")
    if scale:
        value *= _SCALES[scale.lower()]
    return -value if negative else value


def parse_percent(text: str) -> Decimal | None:
    """Return a percentage in [0, 100]; ``50 bps`` -> ``Decimal('0.50')``."""
    if text is None:
        return None
    match = _PERCENT_RE.search(text)
    if not match:
        return None
    if match.group("num") is not None:
        return Decimal(match.group("num"))
    return Decimal(match.group("bps")) / Decimal(100)


def parse_ratio(text: str) -> Decimal | None:
    """``4.50:1.00`` -> ``Decimal('4.50')``."""
    if text is None:
        return None
    match = _RATIO_RE.search(text)
    if not match:
        return None
    b = Decimal(match.group("b"))
    if b == 0:
        return None
    return Decimal(match.group("a")) / b


def parse_date(text: str) -> date | None:
    if text is None:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    g = match.groupdict()
    try:
        if g["month"]:
            return date(int(g["year"]), _MONTHS[g["month"].lower()], int(g["day"]))
        if g["m2"]:
            year = int(g["y2"])
            year += 2000 if year < 100 else 0
            return date(year, int(g["m2"]), int(g["d2"]))
        return date(int(g["y3"]), int(g["m3"]), int(g["d3"]))
    except ValueError:
        return None


def quarter_index(d: date) -> int:
    """Absolute quarter number, for spacing checks that survive month lengths."""
    return d.year * 4 + (d.month - 1) // 3
