"""The scorer chosen on the command line is the one the validators ask.

Both measurement entry points used to hard-wire the offline stand-in. The
calibration harness went further and tagged whatever it fitted ``offline``, so
"fitting against live Jev" as documented would have refitted the stand-in and
written the result over the thresholds CI reads.
"""

from __future__ import annotations

import shutil

import pytest

from credit_extract.validate import calibrate
from credit_extract.validate.jev import OfflineJev


class RecordingScorer(OfflineJev):
    """The stand-in's answers under a name nothing has been fitted against."""

    name = "jev-test"

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    def ask(self, state, questions):
        self.asked += len(questions)
        return super().ask(state, questions)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Threshold files in a scratch directory, with an offline set in place."""
    monkeypatch.setattr(calibrate, "CONFIG_PATH", tmp_path / "thresholds.json")
    calibrate.Thresholds(version="5", backend="offline").save(
        calibrate.thresholds_path()
    )
    return tmp_path


def test_the_family_report_asks_the_scorer_it_is_given(tmp_path, config):
    from credit_extract.eval.family_report import LABELS_DIR, run_coverage

    labels = tmp_path / "labels"
    labels.mkdir()
    shutil.copy(LABELS_DIR / "fixture_meridian_2017.yaml", labels)
    scorer = RecordingScorer()

    run = run_coverage(labels, include_mutations=False, jev_backend=scorer)

    assert scorer.asked > 0
    assert run.thresholds == {"unfitted@jev-test"}
    assert "UNFITTED" in run.render()

    offline = run_coverage(labels, include_mutations=False)
    assert offline.thresholds == {"5@offline"}
    assert "UNFITTED" not in offline.render()


def test_the_gate_run_in_parallel_is_the_gate_run_in_sequence(tmp_path):
    """Label files are independent, so spreading them over processes may
    change how long the gate takes and nothing else."""
    from credit_extract.eval.family_report import (
        LABELS_DIR, run_coverage, run_coverage_parallel,
    )

    labels = tmp_path / "labels"
    labels.mkdir()
    for name in ("ares_cp_funding_amendment_2025", "fixture_meridian_2017"):
        shutil.copy(LABELS_DIR / f"{name}.yaml", labels)

    parallel = run_coverage_parallel(labels, False, "offline", workers=2)
    sequential = run_coverage(labels, include_mutations=False)

    assert [o.model_dump() for o in parallel.outcomes] == [
        o.model_dump() for o in sequential.outcomes
    ]
    assert parallel.render() == sequential.render()


def test_a_label_file_that_does_not_run_fails_the_gate(tmp_path):
    """A spent credit balance took out a third of the first live gate. The
    files that did run are still a measurement; the run is not a pass."""
    from credit_extract.eval.family_report import (
        LABELS_DIR, run_coverage_parallel,
    )

    labels = tmp_path / "labels"
    labels.mkdir()
    shutil.copy(LABELS_DIR / "ares_cp_funding_amendment_2025.yaml", labels)
    (labels / "zz_broken.yaml").write_text(
        "document: zz_broken\nassertions:\n  - id: x\n    family: F99_nowhere\n"
    )

    run = run_coverage_parallel(labels, False, "offline", workers=2)

    assert run.outcomes, "the file that ran is still measured"
    assert len(run.did_not_run) == 1 and run.did_not_run[0].startswith("zz_broken:")
    assert any("did not run" in failure for failure in run.gate_failures())
    assert "DID NOT RUN: 1 label file(s)" in run.render()


def test_calibrating_a_new_scorer_fits_it_once_and_spares_the_offline_set(
    tmp_path, config, monkeypatch
):
    from credit_extract.eval import harness

    scorer = RecordingScorer()
    monkeypatch.setattr(harness, "build_backend", lambda kind, cache=None: scorer)
    runs: list[str] = []
    real_run_pipeline = harness.run_pipeline

    def counting_run_pipeline(path, **kwargs):
        runs.append(str(path))
        return real_run_pipeline(path, **kwargs)

    monkeypatch.setattr(harness, "run_pipeline", counting_run_pipeline)
    argv = [
        "--calibrate", "--jev", "api", "-n", "2",
        "--corpus", str(tmp_path / "gold"), "--version", "6",
    ]

    harness.main(argv)

    fitted = calibrate.load_thresholds(backend="jev-test")
    assert (fitted.backend, fitted.version) == ("jev-test", "6")
    assert "live System One (jev-test)" in fitted.notes
    assert calibrate.load_thresholds(backend="offline").version == "5"
    # Nothing was fitted for this scorer yet, so the first evaluation already
    # ran on neutral thresholds and is the one fitted from.
    assert len(runs) == 2

    # With a fitted set on disk, a refit evaluates as deployed, then neutral.
    runs.clear()
    harness.main(argv)
    assert len(runs) == 4
