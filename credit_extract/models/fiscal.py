"""The borrower's fiscal calendar, modelled explicitly.

Phase 1's date invariants assumed calendar quarters. Under a 52/53-week retail
calendar they are wrong in a way that is worse than being absent: quarter ends
are 91 days apart except when they are 98, so the evenly-spaced check fires on
a perfectly correct amortization schedule and a reviewer learns to ignore it.

The fix is not a looser tolerance -- that would stop catching the real defect
Trap 1 is made of. It is to measure spacing in *fiscal quarters*, which are
evenly spaced by construction under any calendar, and to take the calendar from
the document rather than assuming one.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

WeekConvention = Literal["last_weekday_of_month", "weekday_nearest_month_end"]

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


class FiscalCalendar(BaseModel):
    """How the borrower divides its year."""

    model_config = ConfigDict(frozen=True)

    year_end_month: int = 12
    year_end_day: int = 31
    is_52_53_week: bool = False
    week_convention: WeekConvention | None = None
    #: 0 = Monday .. 6 = Sunday. Required when is_52_53_week.
    anchor_weekday: int | None = None
    #: How the fiscal year is numbered relative to the calendar year it ends in.
    label_offset: int = 0
    source: str = "default"

    @model_validator(mode="after")
    def _coherent(self) -> "FiscalCalendar":
        if not 1 <= self.year_end_month <= 12:
            raise ValueError(f"month {self.year_end_month} out of range")
        if self.is_52_53_week:
            if self.anchor_weekday is None:
                raise ValueError("a 52/53-week calendar needs an anchor weekday")
            if self.week_convention is None:
                raise ValueError("a 52/53-week calendar needs a week convention")
        return self

    @property
    def is_calendar_year(self) -> bool:
        return (
            not self.is_52_53_week
            and self.year_end_month == 12
            and self.year_end_day == 31
        )

    # -- year boundaries ----------------------------------------------------

    def year_end(self, fiscal_year: int) -> date:
        """The last day of the given fiscal year."""
        if not self.is_52_53_week:
            day = min(
                self.year_end_day,
                calendar.monthrange(fiscal_year, self.year_end_month)[1],
            )
            return date(fiscal_year, self.year_end_month, day)

        anchor = date(
            fiscal_year, self.year_end_month,
            calendar.monthrange(fiscal_year, self.year_end_month)[1],
        )
        if self.week_convention == "last_weekday_of_month":
            delta = (anchor.weekday() - self.anchor_weekday) % 7
            return anchor - timedelta(days=delta)
        back = (anchor.weekday() - self.anchor_weekday) % 7
        forward = (self.anchor_weekday - anchor.weekday()) % 7
        return (
            anchor - timedelta(days=back)
            if back <= forward
            else anchor + timedelta(days=forward)
        )

    def quarter_ends(self, fiscal_year: int) -> list[date]:
        """The four quarter-end dates of a fiscal year, in order."""
        end = self.year_end(fiscal_year)
        if not self.is_52_53_week:
            out: list[date] = []
            for quarter in range(1, 5):
                months_back = 3 * (4 - quarter)
                month = self.year_end_month - months_back
                year = fiscal_year
                while month <= 0:
                    month += 12
                    year -= 1
                day = min(self.year_end_day, calendar.monthrange(year, month)[1])
                out.append(date(year, month, day))
            return out

        # 52/53-week: quarters are 13 weeks each, counted back from year end.
        # The 53rd week, when it occurs, lands in the fourth quarter.
        previous = self.year_end(fiscal_year - 1)
        starts = [previous + timedelta(days=1)]
        out = []
        for quarter in range(1, 4):
            out.append(starts[0] + timedelta(weeks=13 * quarter) - timedelta(days=1))
        out.append(end)
        return out

    # -- locating a date ----------------------------------------------------

    def fiscal_year_of(self, when: date) -> int:
        """Which fiscal year a date falls in."""
        candidate = when.year
        while when > self.year_end(candidate):
            candidate += 1
        while when <= self.year_end(candidate - 1):
            candidate -= 1
        return candidate

    def quarter_of(self, when: date) -> int:
        """Fiscal quarter 1-4 containing this date."""
        year = self.fiscal_year_of(when)
        for index, quarter_end in enumerate(self.quarter_ends(year), start=1):
            if when <= quarter_end:
                return index
        return 4

    def quarter_index(self, when: date) -> int:
        """Absolute fiscal-quarter ordinal.

        This is the number the spacing invariants count in. Consecutive fiscal
        quarters differ by exactly one under any calendar, including a 53-week
        year, which is the whole point.
        """
        return self.fiscal_year_of(when) * 4 + (self.quarter_of(when) - 1)

    def is_quarter_end(self, when: date) -> bool:
        return when in self.quarter_ends(self.fiscal_year_of(when))


#: The default when a document says nothing: calendar quarters.
CALENDAR_YEAR = FiscalCalendar(source="default (document is silent)")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_52_53_RE = re.compile(
    r"(52|53)[\s/-]*(?:or\s*)?(?:52|53)?[\s-]*week\s+(?:fiscal\s+)?year",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(
    r"(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(?P<convention>nearest\s+to|closest\s+to|on\s+or\s+(?:before|preceding)|"
    r"last\s+\w*\s*(?:in|of))\s+"
    r"(?:the\s+(?:end\s+of\s+)?)?(?P<month>January|February|March|April|May|June|"
    r"July|August|September|October|November|December)",
    re.IGNORECASE,
)
_YEAR_END_RE = re.compile(
    r"fiscal\s+year\s+(?:of\s+\w+\s+)?(?:end(?:s|ing|ed)?)\s+"
    r"(?:on\s+)?(?:the\s+)?"
    r"(?P<month>January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(?P<day>\d{1,2})",
    re.IGNORECASE,
)


def detect_fiscal_calendar(text: str) -> FiscalCalendar:
    """Read the borrower's fiscal calendar out of the document.

    Falls back to calendar quarters, and says so in ``source`` -- a default
    that does not announce itself is how an assumed calendar becomes a silent
    false positive on every retailer in the corpus.
    """
    anchor = _ANCHOR_RE.search(text or "")
    if _52_53_RE.search(text or "") and anchor:
        convention: WeekConvention = (
            "weekday_nearest_month_end"
            if "nearest" in anchor.group("convention").lower()
            or "closest" in anchor.group("convention").lower()
            else "last_weekday_of_month"
        )
        return FiscalCalendar(
            year_end_month=_MONTHS[anchor.group("month").lower()],
            year_end_day=31,
            is_52_53_week=True,
            week_convention=convention,
            anchor_weekday=_WEEKDAYS[anchor.group("weekday").lower()],
            source=f"detected: {' '.join(anchor.group(0).split())}",
        )

    stated = _YEAR_END_RE.search(text or "")
    if stated:
        month = _MONTHS[stated.group("month").lower()]
        day = int(stated.group("day"))
        return FiscalCalendar(
            year_end_month=month,
            year_end_day=min(day, calendar.monthrange(2024, month)[1]),
            source=f"detected: {' '.join(stated.group(0).split())}",
        )
    return CALENDAR_YEAR
