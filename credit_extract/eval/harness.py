"""Gold-set evaluation and threshold fitting.

Run the pipeline over a labelled corpus, compare every field against ground
truth, and fit the per-class confidence threshold that hits the target
precision. Writes ``config/thresholds.json``, which CI then asserts against.

    python -m credit_extract.eval.harness                 # evaluate
    python -m credit_extract.eval.harness --calibrate     # evaluate and refit

The headline number is not accuracy. It is the **silent error rate**: of the
fields the pipeline marked ``confirmed``, what fraction were wrong. A field
routed to review was handled correctly even when its value was wrong; a field
marked confirmed and wrong is the only kind of failure that reaches a reader
unannounced.

    On the corpus: the gold set here is synthetic. The spec calls for >= 20
    hand-labelled real agreements and sec.gov is blocked from this build
    environment. What is fitted below exercises the machinery end to end and is
    reproducible, but the thresholds are only meaningful for the offline
    backend against these documents. Refit on real filings before the numbers
    say anything about production -- which is exactly why the threshold file
    records the backend and corpus it was fitted on, and why loading refuses a
    backend mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field as dc_field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..models.fpml_model import FIELD_REGISTRY
from ..pipeline import ExtractionResult, run_pipeline
from ..validate.calibrate import (
    CONFIG_PATH, DEFAULT_PRECISION_TARGETS, Sample, Thresholds, fit, report,
)
from .gold.build_fixture import write_corpus

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "corpus" / "gold"

#: Percentages and ratios are compared with tolerance; money and dates exactly.
NUMERIC_TOLERANCE = Decimal("0.005")


@dataclass
class FieldOutcome:
    document_id: str
    field: str
    field_class: str
    expected: Any
    observed: Any
    status: str
    #: Did the pipeline, as deployed, get this field right? Depends on the
    #: thresholds in force, so it measures behaviour, not the classifier.
    correct: bool
    probability: float | None
    criticality: int
    validator: str = "A_span_support"
    #: Was the underlying claim true, regardless of whether the threshold
    #: accepted it? This is what thresholds are fitted against. Fitting on
    #: ``correct`` instead makes each run's labels a function of the previous
    #: run's thresholds, and the fit walks away from the data.
    claim_correct: bool = True


@dataclass
class EvalReport:
    outcomes: list[FieldOutcome] = dc_field(default_factory=list)
    documents: int = 0
    cost_usd: float = 0.0

    # -- headline ------------------------------------------------------------

    @property
    def confirmed(self) -> list[FieldOutcome]:
        return [o for o in self.outcomes if o.status == "confirmed"]

    @property
    def silent_errors(self) -> list[FieldOutcome]:
        """Marked confirmed, and wrong. The only failures that hurt."""
        return [o for o in self.confirmed if not o.correct]

    @property
    def silent_error_rate(self) -> float:
        return len(self.silent_errors) / len(self.confirmed) if self.confirmed else 0.0

    @property
    def coverage(self) -> float:
        return len(self.confirmed) / len(self.outcomes) if self.outcomes else 0.0

    def by_class(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for field_class in sorted({o.field_class for o in self.outcomes}):
            group = [o for o in self.outcomes if o.field_class == field_class]
            confirmed = [o for o in group if o.status == "confirmed"]
            correct = [o for o in group if o.correct]
            out[field_class] = {
                "n": len(group),
                "precision": (
                    sum(1 for o in confirmed if o.correct) / len(confirmed)
                    if confirmed else 1.0
                ),
                "recall": (
                    sum(1 for o in confirmed if o.correct) / len(correct)
                    if correct else 0.0
                ),
                "coverage": len(confirmed) / len(group) if group else 0.0,
                "silent_error_rate": (
                    sum(1 for o in confirmed if not o.correct) / len(confirmed)
                    if confirmed else 0.0
                ),
            }
        return out

    def samples(self) -> list[Sample]:
        return [
            Sample(
                field=o.field,
                field_class=o.field_class,
                probability=o.probability if o.probability is not None else 0.0,
                correct=o.claim_correct,
                document_id=o.document_id,
                validator=o.validator,
            )
            for o in self.outcomes
            if o.probability is not None
        ]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def values_match(kind: str, expected: Any, observed: Any) -> bool:
    """Compare a ground-truth value to an extracted one, per field kind."""
    if expected is None or observed is None:
        return expected is None and observed is None
    if kind in ("money", "int"):
        a, b = _as_decimal(expected), _as_decimal(observed)
        return a is not None and b is not None and a == b
    if kind in ("percent", "ratio"):
        a, b = _as_decimal(expected), _as_decimal(observed)
        return a is not None and b is not None and abs(a - b) <= NUMERIC_TOLERANCE
    if kind == "date":
        return str(expected)[:10] == str(observed)[:10]
    return " ".join(str(expected).split()).casefold() == " ".join(
        str(observed).split()
    ).casefold()


def score_document(result: ExtractionResult, labels: dict) -> list[FieldOutcome]:
    """Compare one document's extraction against its labels."""
    truth = labels.get("fields", {})
    expected_status = labels.get("expected_status", {})
    outcomes: list[FieldOutcome] = []
    for name, spec in FIELD_REGISTRY.items():
        if name not in truth and name not in expected_status:
            continue
        field = result.fields.get(name)
        if field is None:
            continue
        expected = truth.get(name)
        wanted_status = expected_status.get(name)

        if wanted_status is not None:
            # The finding *is* the status: "absent" and "external" are answers,
            # not failures to produce a value.
            correct = field.status == wanted_status
        elif expected is None:
            correct = field.value is None and field.status in (
                "absent_from_document", "external_reference"
            )
        else:
            correct = values_match(spec.kind, expected, field.value)

        source = field.validation_source or "A_span_support"
        if source == "C_negative_space":
            # The claim is "this field is absent from the document". True iff
            # the ground truth has no value for it.
            claim_correct = expected is None
        elif source == "E_external_dependency":
            # The claim is "the magnitude lives in another document".
            claim_correct = wanted_status == "external_reference"
        else:
            claim_correct = correct

        outcomes.append(FieldOutcome(
            document_id=result.document_id,
            field=name,
            field_class=spec.field_class,
            expected=expected if wanted_status is None else wanted_status,
            observed=(
                str(field.value) if field.value is not None else field.status
            ),
            status=field.status,
            correct=correct,
            probability=field.validation_confidence,
            criticality=field.criticality,
            validator=source,
            claim_correct=claim_correct,
        ))
    return outcomes


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def evaluate(
    corpus_dir: Path = DEFAULT_CORPUS,
    n: int = 24,
    regenerate: bool = True,
    thresholds: Thresholds | None = None,
) -> EvalReport:
    """Run the pipeline over every labelled document in the corpus."""
    if regenerate or not corpus_dir.exists():
        write_corpus(corpus_dir, n)
    pairs = sorted(corpus_dir.glob("*.html"))
    evaluation = EvalReport()
    for html_path in pairs:
        labels_path = html_path.with_suffix("").with_suffix(".labels.json")
        if not labels_path.exists():
            labels_path = html_path.parent / f"{html_path.stem}.labels.json"
        if not labels_path.exists():
            continue
        labels = json.loads(labels_path.read_text())
        result = run_pipeline(html_path, thresholds=thresholds)
        evaluation.outcomes.extend(score_document(result, labels))
        evaluation.documents += 1
        evaluation.cost_usd += result.report.cost.total_usd
    return evaluation


def print_report(evaluation: EvalReport) -> None:
    print(
        f"\nevaluated {evaluation.documents} documents, "
        f"{len(evaluation.outcomes)} labelled fields, "
        f"${evaluation.cost_usd:.4f} total"
    )
    print(
        f"{'field class':18} {'n':>4} {'prec':>6} {'recall':>7} {'cover':>6} "
        f"{'silent':>7}"
    )
    for name, metrics in evaluation.by_class().items():
        print(
            f"{name:18} {int(metrics['n']):4d} {metrics['precision']:6.3f} "
            f"{metrics['recall']:7.3f} {metrics['coverage']:6.3f} "
            f"{metrics['silent_error_rate']:7.3f}"
        )
    print(
        f"\nheadline -- of {len(evaluation.confirmed)} fields marked confirmed, "
        f"{len(evaluation.silent_errors)} were wrong "
        f"({evaluation.silent_error_rate:.2%} silent error rate)"
    )
    for outcome in evaluation.silent_errors[:10]:
        print(
            f"  {outcome.document_id[:18]:20} {outcome.field:44} "
            f"expected={str(outcome.expected)[:18]:20} got={str(outcome.observed)[:18]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibrate", action="store_true",
                        help="refit thresholds and write the config")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("-n", type=int, default=24,
                        help="corpus size when generating")
    parser.add_argument("--out", type=Path, default=CONFIG_PATH)
    parser.add_argument("--version", default="1")
    args = parser.parse_args(argv)

    evaluation = evaluate(args.corpus, args.n)
    print_report(evaluation)

    if args.calibrate:
        # Refit from a neutral threshold state. Evaluating with the thresholds
        # already on disk and then fitting on the result makes each generation
        # a function of the last one, and the fit drifts away from the data.
        evaluation = evaluate(
            args.corpus, args.n, regenerate=False,
            thresholds=Thresholds(version="unfitted", backend="offline"),
        )
        thresholds = fit(
            evaluation.samples(),
            targets=DEFAULT_PRECISION_TARGETS,
            backend="offline",
            version=args.version,
            fitted_on=f"synthetic gold corpus, {evaluation.documents} documents",
            n_documents=evaluation.documents,
            notes=(
                "Fitted against the offline lexical Jev stand-in on synthetic "
                "variants. Not transferable to the live System One backend or "
                "to real filings; refit before relying on these numbers."
            ),
        )
        thresholds.save(args.out)
        print()
        print(report(thresholds))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
