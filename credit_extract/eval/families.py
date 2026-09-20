"""The trap family register and the blind-spot register.

``trap_families.yaml`` is the single source of truth: every test case binds to
exactly one family member, and a binding to a member that is not in the
register fails loudly rather than quietly creating a new one.

The reporting rule this module exists to enforce: **coverage is never
summarised without its per-family table**. An aggregate that averages over
untested families is worse than no number, so :class:`CoverageReport` has no
accessor that yields a headline figure without the breakdown and the blind-spot
register beside it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
FAMILIES_PATH = HERE / "trap_families.yaml"
BLIND_SPOTS_PATH = HERE / "blind_spots.yaml"


class UnknownFamilyMember(KeyError):
    """A test bound to a family or member that is not in the register."""


class Family(BaseModel):
    id: str
    title: str = ""
    members: list[str] = Field(default_factory=list)
    silent_error_budget: float = 0.0
    min_n: int = 5
    defended_by: list[str] = Field(default_factory=list)
    notes: str = ""

    @property
    def undefended(self) -> bool:
        return not self.defended_by


class FamilyRegister(BaseModel):
    version: int = 2
    families: dict[str, Family] = Field(default_factory=dict)

    def __iter__(self):
        return iter(self.families.values())

    def __len__(self) -> int:
        return len(self.families)

    def get(self, family_id: str) -> Family:
        if family_id not in self.families:
            raise UnknownFamilyMember(
                f"{family_id!r} is not a trap family. Known families: "
                f"{sorted(self.families)}. Add it to trap_families.yaml rather "
                "than inventing one at the call site."
            )
        return self.families[family_id]

    def validate_member(self, family_id: str, member: str | None) -> None:
        family = self.get(family_id)
        if member is None:
            return
        if member not in family.members:
            raise UnknownFamilyMember(
                f"{member!r} is not a member of {family_id}. Known members: "
                f"{family.members}"
            )

    def member_ids(self) -> list[str]:
        return [
            f"{family.id}:{member}"
            for family in self.families.values()
            for member in family.members
        ]


@lru_cache(maxsize=1)
def load_families(path: Path = FAMILIES_PATH) -> FamilyRegister:
    raw = yaml.safe_load(path.read_text())
    families = {
        key: Family(id=key, **value)
        for key, value in (raw.get("families") or {}).items()
    }
    return FamilyRegister(version=raw.get("version", 2), families=families)


# ---------------------------------------------------------------------------
# Blind spots
# ---------------------------------------------------------------------------


class BlindSpot(BaseModel):
    id: str
    reason: str
    consequence: str = ""
    remedy: str = ""
    family: str | None = None
    member: str | None = None
    families: list[str] = Field(default_factory=list)
    category: str = "untestable_from_public_filings"
    #: ``partially_lifted`` when some real examples now exist but too few to
    #: measure anything. A blind spot that has narrowed is still a blind spot,
    #: and dropping the entry the moment one example arrives is how a register
    #: stops describing the gap it was written for.
    status: Literal["open", "partially_lifted"] = "open"

    @property
    def affected(self) -> list[str]:
        if self.families:
            return self.families
        return [self.family] if self.family else []


class BlindSpotRegister(BaseModel):
    version: int = 2
    entries: list[BlindSpot] = Field(default_factory=list)

    def by_category(self, category: str) -> list[BlindSpot]:
        return [e for e in self.entries if e.category == category]

    def affecting(self, family_id: str) -> list[BlindSpot]:
        return [e for e in self.entries if family_id in e.affected]

    def untested_members(self) -> list[str]:
        """Family members with no testable examples anywhere."""
        return sorted(
            f"{e.family}:{e.member}"
            for e in self.by_category("untestable_from_public_filings")
            if e.family and e.member
        )

    def render(self) -> str:
        lines = ["BLIND-SPOT REGISTER"]
        for category, heading in (
            ("untestable_from_public_filings",
             "Untestable from public filings (permanent):"),
            ("untestable_in_this_environment",
             "Untestable in this environment (fixable):"),
        ):
            entries = self.by_category(category)
            if not entries:
                continue
            lines.append("")
            lines.append(f"  {heading}")
            for entry in entries:
                affected = ", ".join(entry.affected) or "-"
                marker = (
                    " (PARTIALLY LIFTED)"
                    if entry.status == "partially_lifted" else ""
                )
                lines.append(f"    [{entry.id}]{marker} {affected}")
                lines.append(f"      why: {_one_line(entry.reason)}")
                if entry.consequence:
                    lines.append(f"      so:  {_one_line(entry.consequence)}")
                if entry.remedy:
                    lines.append(f"      fix: {_one_line(entry.remedy)}")
        return "\n".join(lines)

    def headline(self) -> str:
        """The one sentence the spec requires every report to carry."""
        permanent = self.by_category("untestable_from_public_filings")
        grouped: dict[str, list[str]] = {}
        for entry in permanent:
            if entry.family and entry.member:
                grouped.setdefault(entry.family.split("_")[0], []).append(entry.member)
        parts = [
            f"{family}({', '.join(sorted(members))})"
            for family, members in sorted(grouped.items())
        ]
        environment = self.by_category("untestable_in_this_environment")
        sentence = ""
        if parts:
            sentence = (
                f"Families {', '.join(parts)} are untested. Behavior on these "
                "inputs is unknown."
            )
        if environment:
            blockers = ", ".join(e.id for e in environment)
            sentence += (
                f" This environment additionally could not test: {blockers}."
            )
        return sentence.strip()


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


@lru_cache(maxsize=1)
def load_blind_spots(path: Path = BLIND_SPOTS_PATH) -> BlindSpotRegister:
    raw = yaml.safe_load(path.read_text())
    entries: list[BlindSpot] = []
    for category in ("untestable_from_public_filings",
                     "untestable_in_this_environment"):
        for item in raw.get(category) or []:
            entries.append(BlindSpot(category=category, **item))
    return BlindSpotRegister(version=raw.get("version", 2), entries=entries)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class FamilyCoverage(BaseModel):
    """Measured performance for one family. Synthetic and real stay apart."""

    family: str
    n: int = 0
    n_real: int = 0
    n_synthetic: int = 0
    passed: int = 0
    failed: int = 0
    #: Of the propositions the pipeline asserted confidently, how many were wrong.
    confirmed: int = 0
    confirmed_wrong: int = 0
    min_n: int = 5
    silent_error_budget: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.n if self.n else 0.0

    @property
    def silent_error_rate(self) -> float:
        return self.confirmed_wrong / self.confirmed if self.confirmed else 0.0

    @property
    def undersampled(self) -> bool:
        return self.n < self.min_n

    @property
    def over_budget(self) -> bool:
        return self.silent_error_rate > self.silent_error_budget + 1e-9

    @property
    def untested(self) -> bool:
        return self.n == 0


class CoverageReport(BaseModel):
    """Per-family coverage, always rendered with the blind-spot register.

    There is deliberately no method that returns a single headline number on
    its own. ``render()`` is the only way out, and it always carries the table
    and the register.
    """

    families: dict[str, FamilyCoverage] = Field(default_factory=dict)
    blind_spots: BlindSpotRegister = Field(default_factory=BlindSpotRegister)
    corpus_kind: str = "mixed"

    @property
    def untested_families(self) -> list[str]:
        return sorted(k for k, v in self.families.items() if v.untested)

    @property
    def undersampled_families(self) -> list[str]:
        return sorted(
            k for k, v in self.families.items() if v.undersampled and not v.untested
        )

    @property
    def over_budget_families(self) -> list[str]:
        return sorted(k for k, v in self.families.items() if v.over_budget)

    def render(self, corpus_label: str = "") -> str:
        lines: list[str] = []
        label = corpus_label or self.corpus_kind
        lines.append(f"PER-FAMILY COVERAGE ({label})")
        lines.append(
            f"  {'family':24} {'N':>4} {'real':>5} {'synth':>6} {'pass':>5} "
            f"{'fail':>5} {'silent':>7}  flags"
        )
        for key in sorted(self.families):
            coverage = self.families[key]
            flags: list[str] = []
            if coverage.untested:
                flags.append("UNTESTED")
            elif coverage.undersampled:
                flags.append(f"undersampled (<{coverage.min_n})")
            if coverage.over_budget:
                flags.append(
                    f"OVER BUDGET ({coverage.silent_error_rate:.3f} > "
                    f"{coverage.silent_error_budget:.3f})"
                )
            for spot in self.blind_spots.affecting(key):
                flags.append(f"blind spot: {spot.id}")
            lines.append(
                f"  {key:24} {coverage.n:4d} {coverage.n_real:5d} "
                f"{coverage.n_synthetic:6d} {coverage.passed:5d} "
                f"{coverage.failed:5d} {coverage.silent_error_rate:7.3f}"
                + ("  " + "; ".join(flags) if flags else "")
            )
        lines.append("")
        lines.append(self.blind_spots.headline())
        lines.append("")
        lines.append(self.blind_spots.render())
        return "\n".join(lines)


def empty_coverage(register: FamilyRegister | None = None) -> dict[str, FamilyCoverage]:
    register = register or load_families()
    return {
        family.id: FamilyCoverage(
            family=family.id,
            min_n=family.min_n,
            silent_error_budget=family.silent_error_budget,
        )
        for family in register
    }
