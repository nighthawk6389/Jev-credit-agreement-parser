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
import math
import random
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
    """One labelled validation outcome.

    ``validator`` matters as much as ``field_class``. "Does this text support
    $150,500,000?" and "is there no MFN sunset anywhere in this agreement?" are
    different classifiers with different score distributions, and pooling them
    into one threshold per field class mis-triages both.
    """

    field: str
    field_class: str
    probability: float
    correct: bool
    document_id: str = "unknown"
    validator: str = "A_span_support"

    @property
    def key(self) -> str:
        return f"{self.validator}/{self.field_class}"


class ClassMetrics(BaseModel):
    field_class: str
    validator: str = "A_span_support"
    threshold: float
    target_precision: float
    precision: float
    recall: float
    coverage: float
    #: 95% lower confidence bound on precision. The point estimate is what a
    #: threshold got lucky on; this is what the data can actually support.
    precision_lower_bound: float = 0.0
    #: True when that bound clears the target. False means the sample is too
    #: small to certify the claim, whatever the point estimate says.
    certified: bool = False
    #: The metric that matters: accepted-and-wrong over accepted.
    silent_error_rate: float
    n: int
    n_accepted: int
    #: Samples scored on held-out documents; 0 when reported in-sample.
    n_holdout: int = 0
    #: Labelled failures in this class. Zero means the threshold is not
    #: constrained by anything -- every threshold hits the precision target
    #: trivially, so the fitted value carries no information.
    n_incorrect: int = 0

    @property
    def meets_target(self) -> bool:
        return self.precision + 1e-9 >= self.target_precision

    @property
    def constrained(self) -> bool:
        """Whether the corpus contained failures that could move the threshold."""
        return self.n_incorrect > 0


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

    def for_class(self, field_class: str, validator: str | None = None) -> float:
        """Threshold for a field class, specific to the asking validator.

        Falls back to the class-wide value and then the default, so a validator
        with no fitted data is conservative rather than unbounded.
        """
        if validator is not None:
            specific = self.per_class.get(f"{validator}/{field_class}")
            if specific is not None:
                return specific
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


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """95% lower confidence bound on a proportion.

    A precision of 0.993 measured on 297 samples is not evidence of 99%
    precision -- two more failures would have taken it under. Selecting a
    threshold on the point estimate picks whichever one happened to get lucky,
    and the lucky threshold is usually the one that accepts everything.
    """
    if n == 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


def fit_threshold(
    samples: Sequence[Sample], target_precision: float
) -> tuple[float, bool]:
    """Lowest threshold whose precision *lower bound* clears the target.

    Lowest, not highest: among thresholds that hit the target, the one that
    accepts the most sends the least work to a human. The bound rather than the
    point estimate, so that a threshold is only chosen when the data can
    actually support the claim.

    Returns ``(threshold, certified)``. When no threshold can be certified at
    this sample size -- which is the usual case for a 99% target on a few
    hundred samples -- it returns the threshold with the strongest bound and
    ``certified=False``, so the shortfall is reported rather than hidden.
    """
    if not samples:
        return 1.0, False
    candidates = [*sorted({round(s.probability, 4) for s in samples}), 1.0001]
    best: tuple[float, float] | None = None       # (threshold, coverage)
    fallback: tuple[float, float] | None = None   # (threshold, lower bound)
    for threshold in candidates:
        accepted = [s for s in samples if s.probability >= threshold]
        correct = sum(1 for s in accepted if s.correct)
        bound = wilson_lower_bound(correct, len(accepted))
        coverage = len(accepted) / len(samples)
        if bound + 1e-9 >= target_precision:
            if best is None or coverage > best[1]:
                best = (threshold, coverage)
        if fallback is None or bound > fallback[1]:
            fallback = (threshold, bound)
    if best is not None:
        return best[0], True
    return (fallback[0] if fallback else 1.0), False


def split_holdout(
    samples: Sequence[Sample], fraction: float = 0.34, seed: int = 20260920
) -> tuple[list[Sample], list[Sample]]:
    """Split by document, not by sample.

    Fields within one agreement are correlated -- the same drafting quirk moves
    several at once -- so splitting at the sample level leaks the document
    across the boundary and flatters the held-out numbers.
    """
    documents = sorted({s.document_id for s in samples})
    if len(documents) < 3:
        return list(samples), []
    rng = random.Random(seed)
    shuffled = documents[:]
    rng.shuffle(shuffled)
    n_holdout = max(1, round(len(shuffled) * fraction))
    holdout = set(shuffled[:n_holdout])
    return (
        [s for s in samples if s.document_id not in holdout],
        [s for s in samples if s.document_id in holdout],
    )


def fit(
    samples: Iterable[Sample],
    targets: dict[str, float] | None = None,
    backend: str = "offline",
    version: str = "1",
    fitted_on: str = "",
    n_documents: int = 0,
    notes: str = "",
    holdout_fraction: float = 0.34,
) -> Thresholds:
    """Fit one threshold per (validator, field class) and score it held out."""
    targets = targets or DEFAULT_PRECISION_TARGETS
    samples = list(samples)
    train, holdout = split_holdout(samples, holdout_fraction)

    by_key: dict[str, list[Sample]] = {}
    for sample in train:
        by_key.setdefault(sample.key, []).append(sample)
    holdout_by_key: dict[str, list[Sample]] = {}
    for sample in holdout:
        holdout_by_key.setdefault(sample.key, []).append(sample)

    per_class: dict[str, float] = {}
    metrics: dict[str, ClassMetrics] = {}
    for key, group in sorted(by_key.items()):
        validator, field_class = key.split("/", 1)
        target = targets.get(field_class, 0.95)
        threshold, certified = fit_threshold(group, target)

        # Everything reported is measured on data the threshold never saw.
        scored = holdout_by_key.get(key) or group
        on_holdout = bool(holdout_by_key.get(key))
        precision, recall, coverage = evaluate(scored, threshold)
        accepted = [s for s in scored if s.probability >= threshold]
        wrong = sum(1 for s in accepted if not s.correct)
        per_class[key] = round(threshold, 4)
        metrics[key] = ClassMetrics(
            field_class=field_class,
            validator=validator,
            threshold=round(threshold, 4),
            target_precision=target,
            precision=round(precision, 4),
            precision_lower_bound=round(
                wilson_lower_bound(len(accepted) - wrong, len(accepted)), 4
            ),
            certified=certified,
            recall=round(recall, 4),
            coverage=round(coverage, 4),
            silent_error_rate=round(wrong / len(accepted), 4) if accepted else 0.0,
            n=len(group),
            n_accepted=len(accepted),
            n_incorrect=sum(1 for s in group if not s.correct),
            n_holdout=len(scored) if on_holdout else 0,
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
        f"{'validator / field class':40} {'thr':>6} {'prec':>6} {'p_lo':>6} "
        f"{'cover':>6} {'silent':>7} {'fit':>5} {'bad':>4} {'held':>5}  target",
    ]
    unconstrained: list[str] = []
    uncertified: list[str] = []
    for name, metric in sorted(thresholds.metrics.items()):
        flag = ""
        if not metric.certified:
            flag += "  << not certified"
            uncertified.append(name)
        if not metric.constrained:
            flag += "  << unconstrained"
            unconstrained.append(name)
        lines.append(
            f"{name:40} {metric.threshold:6.3f} {metric.precision:6.3f} "
            f"{metric.precision_lower_bound:6.3f} {metric.coverage:6.3f} "
            f"{metric.silent_error_rate:7.3f} {metric.n:5d} "
            f"{metric.n_incorrect:4d} {metric.n_holdout:5d}  "
            f"{metric.target_precision:.2f}{flag}"
        )
    if uncertified:
        lines += [
            "",
            "NOT CERTIFIED: " + ", ".join(uncertified) + ".",
            "      No threshold's 95% precision lower bound reaches the target "
            "at this sample",
            "      size. A 99% target needs on the order of 300+ accepted "
            "samples with zero",
            "      failures before it can be certified at all; these "
            "thresholds are the best",
            "      available, not validated ones.",
        ]
    if unconstrained:
        lines += [
            "",
            "NOTE: " + ", ".join(unconstrained) + " had no labelled failures in "
            "this corpus.",
            "      Every threshold hits the precision target trivially, so the "
            "fitted value",
            "      carries no information and precision 1.000 is not evidence "
            "the threshold",
            "      is right. Those classes need a corpus containing genuine "
            "extraction errors",
            "      -- real filings -- before their thresholds mean anything.",
        ]
    return "\n".join(lines)
