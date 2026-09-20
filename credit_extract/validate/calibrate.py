"""Confidence thresholds: fitted on data, versioned, and tested in CI.

A threshold is not a constant somebody picked. It is fitted per field class
against hand-labelled outcomes to hit a target precision, stored in
version-controlled config, and asserted in CI. A model update that shifts
calibration should fail the build, not surface months later as quietly
degraded extractions.

Thresholds are tagged with the backend they were fitted against. Applying a
threshold fitted on one scorer to a different one is meaningless, so
:func:`load_thresholds` refuses to do it rather than silently mis-triaging.

The headline metric is not accuracy. It is: of the fields the pipeline marked
``confirmed``, what fraction were wrong? Silent errors are the only kind that
hurt -- a field routed to review was handled correctly even if the value was
wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

from pydantic import BaseModel, Field

#: Precision targets differ by class. Pricing and maturity warrant heavy human
#: routing; a notice address does not.
DEFAULT_PRECISION_TARGETS: dict[str, float] = {
    "economic_terms": 0.99,
    "dates": 0.99,
    "covenant_levels": 0.98,
    "baskets": 0.95,
    "parties": 0.95,
    "administrative": 0.90,
}

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "thresholds.json"


class Sample(BaseModel):
    """One labelled validation outcome."""

    field: str
    field_class: str
    probability: float
    correct: bool
    document_id: str = "unknown"


class ClassMetrics(BaseModel):
    field_class: str
    threshold: float
    target_precision: float
    precision: float
    recall: float
    coverage: float
    #: The metric that matters: accepted-and-wrong over accepted.
    silent_error_rate: float
    n: int
    n_accepted: int

    @property
    def meets_target(self) -> bool:
        return self.precision + 1e-9 >= self.target_precision


class Thresholds(BaseModel):
    """A fitted, versioned threshold set."""

    version: str = "0"
    backend: str = "offline"
    fitted_on: str = ""
    n_documents: int = 0
    per_class: dict[str, float] = Field(default_factory=dict)
    default: float = 0.80
    targets: dict[str, float] = Field(default_factory=dict)
    metrics: dict[str, ClassMetrics] = Field(default_factory=dict)
    notes: str = ""

    def for_class(self, field_class: str) -> float:
        return self.per_class.get(field_class, self.default)

    def save(self, path: Path = CONFIG_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(), indent=2) + "\n")
        return path


class BackendMismatch(RuntimeError):
    """A threshold set fitted on one backend applied to another."""


def load_thresholds(
    path: Path = CONFIG_PATH, backend: str | None = None
) -> Thresholds:
    """Load fitted thresholds, refusing a backend mismatch."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found; run `python -m credit_extract.eval.harness "
            "--calibrate` to fit thresholds"
        )
    thresholds = Thresholds.model_validate_json(path.read_text())
    if backend is not None and thresholds.backend != backend:
        raise BackendMismatch(
            f"thresholds in {path} were fitted against the "
            f"{thresholds.backend!r} backend but are being applied to "
            f"{backend!r}. Confidence scales are not transferable between "
            "scorers; refit before using this backend."
        )
    return thresholds


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def evaluate(samples: Sequence[Sample], threshold: float) -> tuple[float, float, float]:
    """Return (precision, recall, coverage) at a threshold.

    ``precision`` is over accepted fields, ``recall`` is the share of the
    genuinely-correct population that is accepted, and ``coverage`` is the
    share of all fields accepted rather than routed to review.
    """
    if not samples:
        return 0.0, 0.0, 0.0
    accepted = [s for s in samples if s.probability >= threshold]
    correct_total = sum(1 for s in samples if s.correct)
    precision = (
        sum(1 for s in accepted if s.correct) / len(accepted) if accepted else 1.0
    )
    recall = (
        sum(1 for s in accepted if s.correct) / correct_total
        if correct_total else 0.0
    )
    return precision, recall, len(accepted) / len(samples)


def fit_threshold(
    samples: Sequence[Sample], target_precision: float
) -> tuple[float, float, float, float]:
    """Lowest threshold achieving the target precision.

    Lowest, not highest: among thresholds that hit the precision target, the
    one that accepts the most is the one that sends the least work to a human.
    If no threshold reaches the target, returns the best available and lets the
    caller see the shortfall in the metrics rather than hiding it.
    """
    if not samples:
        return 1.0, 0.0, 0.0, 0.0
    candidates = sorted({round(s.probability, 4) for s in samples})
    candidates = [*candidates, 1.0001]
    best: tuple[float, float, float, float] | None = None
    fallback: tuple[float, float, float, float] | None = None
    for threshold in candidates:
        precision, recall, coverage = evaluate(samples, threshold)
        if precision + 1e-9 >= target_precision:
            if best is None or coverage > best[3]:
                best = (threshold, precision, recall, coverage)
        if fallback is None or precision > fallback[1]:
            fallback = (threshold, precision, recall, coverage)
    return best or fallback or (1.0, 0.0, 0.0, 0.0)


def fit(
    samples: Iterable[Sample],
    targets: dict[str, float] | None = None,
    backend: str = "offline",
    version: str = "1",
    fitted_on: str = "",
    n_documents: int = 0,
    notes: str = "",
) -> Thresholds:
    """Fit one threshold per field class."""
    targets = targets or DEFAULT_PRECISION_TARGETS
    by_class: dict[str, list[Sample]] = {}
    for sample in samples:
        by_class.setdefault(sample.field_class, []).append(sample)

    per_class: dict[str, float] = {}
    metrics: dict[str, ClassMetrics] = {}
    for field_class, group in sorted(by_class.items()):
        target = targets.get(field_class, 0.95)
        threshold, precision, recall, coverage = fit_threshold(group, target)
        accepted = [s for s in group if s.probability >= threshold]
        wrong = sum(1 for s in accepted if not s.correct)
        per_class[field_class] = round(threshold, 4)
        metrics[field_class] = ClassMetrics(
            field_class=field_class,
            threshold=round(threshold, 4),
            target_precision=target,
            precision=round(precision, 4),
            recall=round(recall, 4),
            coverage=round(coverage, 4),
            silent_error_rate=round(wrong / len(accepted), 4) if accepted else 0.0,
            n=len(group),
            n_accepted=len(accepted),
        )
    return Thresholds(
        version=version,
        backend=backend,
        fitted_on=fitted_on,
        n_documents=n_documents,
        per_class=per_class,
        targets=targets,
        metrics=metrics,
        notes=notes,
    )


def report(thresholds: Thresholds) -> str:
    """Human-readable precision/recall/coverage triple per field class."""
    lines = [
        f"thresholds v{thresholds.version} "
        f"(backend={thresholds.backend}, n_documents={thresholds.n_documents})",
        f"{'field class':18} {'thr':>6} {'prec':>6} {'recall':>7} "
        f"{'cover':>6} {'silent':>7} {'n':>5}  target",
    ]
    for name, metric in sorted(thresholds.metrics.items()):
        flag = "" if metric.meets_target else "  << below target"
        lines.append(
            f"{name:18} {metric.threshold:6.3f} {metric.precision:6.3f} "
            f"{metric.recall:7.3f} {metric.coverage:6.3f} "
            f"{metric.silent_error_rate:7.3f} {metric.n:5d}  "
            f"{metric.target_precision:.2f}{flag}"
        )
    return "\n".join(lines)
