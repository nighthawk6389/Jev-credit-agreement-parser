"""The ablation's own guards.

An ablation is a measuring instrument, and the ways it can quietly stop
measuring are specific: an arm that silently becomes another arm, a comparison
set that differs between arms, a metric that rewards silence. Each test below
pins one of those.
"""

from __future__ import annotations

import pytest

from credit_extract.eval import ablation
from credit_extract.eval.arms import ALL_VALIDATORS, ARMS, FULL, Arm


def test_an_arm_cannot_name_a_validator_that_does_not_exist():
    """A typo in an arm silently disables a stage, and the table still prints."""
    with pytest.raises(ValueError, match="no such validator"):
        Arm(name="typo", description="", tier="rules", validators=frozenset("AZ"))
    with pytest.raises(ValueError, match="no such tier"):
        Arm(name="typo", description="", tier="magic", validators=frozenset("A"))


def test_the_default_arm_runs_every_validator():
    """``run_pipeline`` defaults to FULL, so the arm machinery has to be inert.

    If FULL ever drops a stage, every number this repository has ever reported
    changes without anything in the report saying so.
    """
    assert FULL.validators == ALL_VALIDATORS
    assert FULL.tier == "layered"
    for letter in "ABCDEFG":
        assert FULL.runs(letter)


def test_a_model_arm_without_a_recording_gets_no_backend():
    """The property the whole comparison rests on.

    There is no ANTHROPIC_API_KEY here, so a model arm replays a checked-in
    recording or it does not run. Falling back to the deterministic backend
    would make the model arms *be* the deterministic arm, and the ablation
    would then report that the model tier changes nothing -- a finding it had
    manufactured itself.
    """
    assert ablation.backend_for(ARMS["deterministic"], "no-such-document") is not None
    assert ablation.backend_for(ARMS["model_only"], "no-such-document") is None
    assert ablation.backend_for(ARMS["current"], "no-such-document") is None

    # And with a recording, the two model arms differ: one is the model alone,
    # the other the model stacked on the rules.
    recorded = ablation.backend_for(ARMS["model_only"], "aspen_technology_2022")
    layered = ablation.backend_for(ARMS["current"], "aspen_technology_2022")
    assert recorded is not None and recorded.name == "recorded"
    assert layered is not None and layered.name != "recorded"


def test_a_document_one_arm_cannot_run_leaves_the_comparison_for_all_arms():
    """Identical inputs, or the columns are not comparable.

    Scoring the deterministic arm on a hundred documents and the model arm on
    three, then printing both in one table, compares two different populations
    and reads as though it compared two pipelines.
    """
    from credit_extract.eval.assertions import load_assertion_file
    from credit_extract.eval.family_report import LABELS_DIR, ensure_corpus_unpacked

    ensure_corpus_unpacked()
    # A real label whose document has a recording, and a real one whose
    # document does not. Stubs would not exercise the resolver.
    with_recording = load_assertion_file(LABELS_DIR / "aspen_technology_2022.yaml")
    without = load_assertion_file(LABELS_DIR / "sysco_2026.yaml")

    arms = [ARMS["deterministic"], ARMS["model_only"]]
    usable, dropped = ablation.comparison_set(arms, [with_recording, without])

    assert [f.document for f, _ in usable] == ["aspen_technology_2022"]
    assert len(dropped) == 1
    assert "sysco_2026" in dropped[0]
    assert "model_only" in dropped[0]

    # The deterministic arm could have run the dropped document on its own.
    # That it does not is the point: the columns have to describe one
    # population, not one population each.
    assert ablation.backend_for(ARMS["deterministic"], without.document) is not None


def test_recall_counts_the_registry_and_not_the_labels():
    """Otherwise an arm improves its score by returning fewer things.

    Recall is measured over the 37 critical fields whatever the labels happen
    to cover, which is the same field set the labelling guide freezes.
    """
    from credit_extract.eval.label import MIN_CRITICALITY
    from credit_extract.models.fpml_model import FIELD_REGISTRY

    expected = {
        name for name, spec in FIELD_REGISTRY.items()
        if spec.criticality >= MIN_CRITICALITY
    }
    assert set(ablation.CRITICAL) == expected
    assert len(ablation.CRITICAL) == 37


def test_an_arm_that_answers_nothing_scores_zero_recall_and_zero_silent_errors():
    """The reason recall and the silent-error rate are never printed apart.

    The safety metric is monotone in silence: say nothing and it is perfect.
    An ablation ranked on it alone would prefer the pipeline that extracts
    least, which is the opposite of what step 4 is for.
    """
    silent = ablation.ArmSummary(arm=FULL, runs=[
        ablation.DocumentRun(
            arm="current", document="d", side="fit",
            returned=0, confirmed=0, settled_null=0, review=len(ablation.CRITICAL),
        )
    ])
    assert silent.recall == 0.0
    assert silent.silent_error_rate == 0.0
    assert silent.review_rate == 1.0


def test_disagreements_name_the_proposition_and_not_just_the_count():
    """Two arms differing by one silent error is a fact about a number.

    Which proposition they differ on is a fact about a stage, and only the
    second says what to keep.
    """
    class _Outcome:
        def __init__(self, assertion_id, confident, passed, observed):
            self.assertion_id = assertion_id
            self.confident = confident
            self.passed = passed
            self.observed = observed

    def summary(name, outcome):
        arm = ARMS[name]
        return ablation.ArmSummary(arm=arm, runs=[
            ablation.DocumentRun(arm=name, document="d", side="fit",
                                 outcomes=[outcome])
        ])

    agreed = _Outcome("same", True, True, "x")
    differs_a = _Outcome("differs", True, True, "by_design")
    differs_b = _Outcome("differs", True, False, None)

    found = ablation._disagreements([
        summary("current", agreed), summary("hybrid", agreed),
    ])
    assert found == {}

    found = ablation._disagreements([
        ablation.ArmSummary(arm=ARMS["current"], runs=[
            ablation.DocumentRun(arm="current", document="d", side="fit",
                                 outcomes=[agreed, differs_a])]),
        ablation.ArmSummary(arm=ARMS["hybrid"], runs=[
            ablation.DocumentRun(arm="hybrid", document="d", side="fit",
                                 outcomes=[agreed, differs_b])]),
    ])
    assert set(found) == {"differs"}
    assert found["differs"]["current"][1] is True
    assert found["differs"]["hybrid"][1] is False
