"""Telling "omitted from the filing" apart from "external by design".

These look identical in the output of any pipeline that reports a missing
magnitude as ``external_reference``, and they are not the same fact:

* **external_by_design** -- the value is unobtainable in principle. The Sponsor
  Model add-back cap genuinely lives outside the loan documents and would be
  unavailable to a lender holding the complete signed set. This is a deal
  characteristic and it belongs in the record.

* **omitted_from_filing** -- the value is in a schedule the filer left out under
  the SEC's exhibit-omission rules. The borrower has it. This is an artifact of
  the *source*, not of the deal.

A large share of EDGAR credit agreements carry the second, so conflating them
corrupts a corpus in a specific and expensive way: reported external-dependency
rates come out several times higher than reality, and calibration learns to
treat a missing schedule as a deal term.

The linguistic signal differs sharply, which is why this is worth asking at
all -- but the cheapest signals are deterministic, so they run first and the
model is only asked about what they leave open.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..ingest.normalize import NormalizedDocument
from ..models.core import ExternalKind, Span

#: Filers say this when they leave a schedule out. Item 601(a)(5) of
#: Regulation S-K permits it, and the accompanying language is formulaic.
OMISSION_PATTERNS: tuple[str, ...] = (
    r"schedules?\s+(?:and\s+exhibits?\s+)?(?:have\s+been\s+|omitted|intentionally\s+omitted)",
    r"(?:schedules?|exhibits?|annexes?)[^.\n]{0,60}?omitted",
    r"omitted\s+pursuant\s+to\s+(?:item\s+)?601",
    r"omitted\s+in\s+accordance\s+with\s+(?:item\s+)?601",
    r"agrees?\s+to\s+furnish\s+(?:a\s+)?suppl(?:y|emental|ementally)",
    r"will\s+furnish\s+(?:a\s+)?cop(?:y|ies)\s+of\s+any\s+omitted",
    r"\[\s*(?:schedules?|exhibits?)\s+omitted\s*\]",
    r"omitted\s+from\s+this\s+(?:filing|exhibit)",
)

#: Language that marks a reference as external by construction.
BY_DESIGN_PATTERNS: tuple[str, ...] = (
    r"is\s+not\s+a\s+loan\s+document",
    r"are\s+not\s+loan\s+documents",
    r"as\s+separately\s+agreed",
    r"\bfee\s+letter\b",
    r"prepared\s+by\s+the\s+sponsor",
    r"\bfinancial\s+model\b",
    r"not\s+attached\s+hereto",
    r"\bside\s+letter\b",
    r"agreed\s+between\s+the\s+borrower\s+and\s+the\s+(?:administrative\s+)?agent",
)

_OMISSION_RE = re.compile("|".join(OMISSION_PATTERNS), re.IGNORECASE)
_BY_DESIGN_RE = re.compile("|".join(BY_DESIGN_PATTERNS), re.IGNORECASE)

#: Nouls asked as a pair against one state, so the pair costs one request.
OMITTED_STATEMENT = (
    "This document indicates the referenced material was omitted from the "
    "filing rather than existing outside the agreement."
)
BY_DESIGN_STATEMENT = (
    "The referenced material exists outside the loan documents by "
    "construction and would be unavailable even with the complete signed set."
)


class OmissionMarker(BaseModel):
    """A place where the filer said something was left out."""

    span: Span
    phrase: str
    scope: Literal["document", "local"] = "document"


class ExternalVerdict(BaseModel):
    """Which kind of external reference this is, and on what evidence."""

    kind: ExternalKind
    basis: Literal["deterministic", "model", "default"] = "deterministic"
    evidence: str = ""
    omitted_probability: float | None = None
    by_design_probability: float | None = None

    @property
    def certain(self) -> bool:
        return self.basis == "deterministic"


def find_omission_markers(doc: NormalizedDocument) -> list[OmissionMarker]:
    """Every place the document says material was omitted from the filing."""
    return [
        OmissionMarker(
            span=doc.span(match.start(), match.end()),
            phrase=" ".join(match.group(0).split()),
        )
        for match in _OMISSION_RE.finditer(doc.text)
    ]


def document_omits_schedules(doc: NormalizedDocument) -> bool:
    return bool(_OMISSION_RE.search(doc.text))


def classify_locally(window: str) -> ExternalVerdict | None:
    """Tier 1. Decide from the surrounding text alone, for free.

    Only returns a verdict when the language is unambiguous; anything else is
    left for the model rather than guessed at here.
    """
    omitted = _OMISSION_RE.search(window or "")
    by_design = _BY_DESIGN_RE.search(window or "")
    if omitted and not by_design:
        return ExternalVerdict(
            kind="omitted_from_filing", basis="deterministic",
            evidence=" ".join(omitted.group(0).split()),
        )
    if by_design and not omitted:
        return ExternalVerdict(
            kind="by_design", basis="deterministic",
            evidence=" ".join(by_design.group(0).split()),
        )
    return None


def classify(
    window: str,
    omitted_probability: float | None = None,
    by_design_probability: float | None = None,
    document_omits: bool = False,
) -> ExternalVerdict:
    """Combine the free signal with the model's, in that order."""
    local = classify_locally(window)
    if local is not None:
        local.omitted_probability = omitted_probability
        local.by_design_probability = by_design_probability
        return local

    if omitted_probability is not None and by_design_probability is not None:
        if abs(omitted_probability - by_design_probability) > 1e-9:
            kind: ExternalKind = (
                "omitted_from_filing"
                if omitted_probability > by_design_probability
                else "by_design"
            )
            return ExternalVerdict(
                kind=kind, basis="model",
                evidence=(
                    f"omitted={omitted_probability:.2f} vs "
                    f"by_design={by_design_probability:.2f}"
                ),
                omitted_probability=omitted_probability,
                by_design_probability=by_design_probability,
            )

    # Nothing decided it. A document that says it omitted its schedules makes
    # omission the better default; otherwise assume the harder case, because
    # calling a real deal term a filing artifact loses information that the
    # opposite error does not.
    return ExternalVerdict(
        kind="omitted_from_filing" if document_omits else "by_design",
        basis="default",
        evidence=(
            "document declares omitted schedules"
            if document_omits
            else "no omission language anywhere in the document"
        ),
        omitted_probability=omitted_probability,
        by_design_probability=by_design_probability,
    )
