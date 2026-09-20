"""Calibration assertions that run as CI tests.

A model update that shifts calibration should fail the build, not surface
months later as quietly degraded extractions. These tests assert the shape and
the honesty of the checked-in threshold config, and re-run the pipeline over a
slice of the gold corpus to confirm the headline metric has not moved.
"""

from __future__ import annotations

import pytest

from credit_extract.validate.calibrate import (
    CONFIG_PATH, BackendMismatch, Sample, Thresholds, fit, fit_threshold,
    load_thresholds, split_holdout, wilson_lower_bound,
)

#: The pipeline may route fields to review, but it must not confidently ship a
#: wrong one. This is the only number that would hurt a reader.
MAX_SILENT_ERROR_RATE = 0.0

#: Below this, the pipeline is routing so much to review that it is not doing
#: the job, even though it is not doing harm.
MIN_COVERAGE = 0.80


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return load_thresholds()


def test_threshold_config_is_checked_in():
    assert CONFIG_PATH.exists(), (
        "config/thresholds.json must be version-controlled; run "
        "`python -m credit_extract.eval.harness --calibrate`"
    )


def test_thresholds_record_what_they_were_fitted_on(thresholds):
    assert thresholds.backend
    assert thresholds.n_documents >= 20, (
        "the spec calls for a gold set of at least 20 agreements"
    )
    assert thresholds.fitted_on
    assert thresholds.metrics


def test_thresholds_are_keyed_by_validator_and_class(thresholds):
    """Span support and negative space are different classifiers."""
    assert any("/" in key for key in thresholds.per_class), (
        "thresholds must be scoped to the validator that asks the question"
    )
    for key in thresholds.per_class:
        if "/" not in key:
            continue
        validator, field_class = key.split("/", 1)
        assert validator
        assert field_class


def test_a_threshold_fitted_on_one_backend_is_refused_by_another(tmp_path):
    path = tmp_path / "thresholds.json"
    Thresholds(version="1", backend="offline", per_class={"dates": 0.9}).save(path)
    with pytest.raises(BackendMismatch, match="not transferable"):
        load_thresholds(path, backend="jev")


def test_every_class_reports_whether_it_is_certified(thresholds):
    """A threshold nobody could validate must say so."""
    for key, metric in thresholds.metrics.items():
        assert isinstance(metric.certified, bool)
        assert 0.0 <= metric.precision_lower_bound <= 1.0
        if metric.certified:
            assert metric.precision_lower_bound > 0.0


def test_unconstrained_classes_are_flagged_not_hidden(thresholds):
    """Precision 1.000 on a corpus with no failures is not evidence."""
    from credit_extract.validate.calibrate import report

    text = report(thresholds)
    unconstrained = [k for k, m in thresholds.metrics.items() if not m.constrained]
    if unconstrained:
        assert "unconstrained" in text
        assert "carries no information" in text


def test_fallback_is_conservative_for_an_unfitted_class(thresholds):
    assert thresholds.for_class("no_such_class") == thresholds.default
    assert thresholds.default >= 0.5


# ---------------------------------------------------------------------------
# Fitting mechanics
# ---------------------------------------------------------------------------


def test_wilson_bound_is_below_the_point_estimate():
    assert wilson_lower_bound(297, 297) < 1.0
    assert wilson_lower_bound(295, 297) < 295 / 297
    assert wilson_lower_bound(0, 0) == 0.0


def test_a_small_sample_cannot_certify_a_high_target():
    """Ten perfect samples are not evidence of 99% precision."""
    samples = [
        Sample(field="f", field_class="dates", probability=0.9, correct=True,
               document_id=f"d{i}")
        for i in range(10)
    ]
    _, certified = fit_threshold(samples, 0.99)
    assert certified is False


def test_fitting_prefers_coverage_among_certifiable_thresholds():
    samples = [
        Sample(field="f", field_class="parties", probability=0.95, correct=True,
               document_id=f"d{i}")
        for i in range(400)
    ]
    threshold, certified = fit_threshold(samples, 0.90)
    assert certified is True
    assert threshold <= 0.95


def test_a_threshold_excludes_the_failures_it_can():
    good = [
        Sample(field="f", field_class="parties", probability=0.95, correct=True,
               document_id=f"g{i}")
        for i in range(300)
    ]
    bad = [
        Sample(field="f", field_class="parties", probability=0.10, correct=False,
               document_id=f"b{i}")
        for i in range(40)
    ]
    threshold, certified = fit_threshold(good + bad, 0.95)
    assert certified is True
    assert 0.10 < threshold <= 0.95


def test_holdout_splits_by_document_not_by_sample():
    """Fields inside one agreement are correlated; a sample split leaks."""
    samples = [
        Sample(field=f"f{j}", field_class="dates", probability=0.9,
               correct=True, document_id=f"doc{i}")
        for i in range(12) for j in range(5)
    ]
    train, holdout = split_holdout(samples)
    train_docs = {s.document_id for s in train}
    holdout_docs = {s.document_id for s in holdout}
    assert holdout_docs
    assert not (train_docs & holdout_docs), "a document straddled the split"


def test_metrics_are_reported_on_held_out_documents():
    samples = [
        Sample(field="f", field_class="parties", probability=0.9, correct=True,
               document_id=f"doc{i}")
        for i in range(30)
    ]
    thresholds = fit(samples, backend="offline", n_documents=30)
    metric = thresholds.metrics["A_span_support/parties"]
    assert metric.n_holdout > 0, "reported numbers must be out of sample"
    assert metric.n_holdout + metric.n <= len(samples)


# ---------------------------------------------------------------------------
# The headline metric, re-measured
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_silent_error_rate_has_not_regressed(tmp_path):
    """Of the fields marked confirmed, how many were wrong?"""
    from credit_extract.eval.harness import evaluate

    evaluation = evaluate(tmp_path / "gold", n=8)
    assert evaluation.documents == 8
    assert evaluation.outcomes
    assert evaluation.silent_error_rate <= MAX_SILENT_ERROR_RATE, (
        "fields marked confirmed and wrong: "
        + "; ".join(
            f"{o.document_id}/{o.field}: expected {o.expected}, got {o.observed}"
            for o in evaluation.silent_errors[:5]
        )
    )
    assert evaluation.coverage >= MIN_COVERAGE, (
        f"coverage {evaluation.coverage:.3f} is below {MIN_COVERAGE}; the "
        "pipeline is routing too much to review to be useful"
    )
