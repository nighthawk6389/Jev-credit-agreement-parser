"""Pipeline configurations an ablation can compare.

The roadmap's step 4 asks for "current, model-only, and hybrid pipelines on
identical inputs and metrics". An arm is what makes that comparison possible:
a named subset of the validation stack, chosen once and applied to every
document, so that a difference in the numbers is a difference between the arms
rather than between two runs that happened to be configured differently.

Two rules hold this together.

**The default arm is the pipeline.** ``FULL`` enables everything, and
``run_pipeline`` with no arm behaves exactly as it did before arms existed.
An ablation that changes the thing it is measuring measures nothing, so the
arm machinery must be inert until asked for.

**An arm names stages, not backends.** Which model answers is a separate axis
-- ``extraction_backend`` already carries it -- and conflating the two would
make "model-only" mean both "only the model extracted" and "only citation
checking validated", which are different claims that would then be impossible
to attribute. The ablation harness picks the backend; the arm picks the stack.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every validator the pipeline knows, in the order ``run_pipeline`` runs them.
#: A is the citation gate, B the orphan sweep, C negative space, D overrides,
#: E external dependency, F criticality triage, G amendment effect.
ALL_VALIDATORS = frozenset("ABCDEFG")


#: Which extraction tier answers. "rules" is the deterministic backend alone,
#: "model" the model alone, "layered" the two stacked as the pipeline ships
#: them. Kept separate from ``validators`` so the ablation can attribute a
#: difference to the tier or to the stack, and never has to guess which.
TIERS = frozenset({"rules", "model", "layered"})


@dataclass(frozen=True)
class Arm:
    """One configuration of the extraction tier and the validation stack."""

    name: str
    description: str
    tier: str
    validators: frozenset[str]
    #: What this arm is supposed to demonstrate, in the ablation's own terms.
    hypothesis: str = ""

    def __post_init__(self) -> None:
        unknown = self.validators - ALL_VALIDATORS
        if unknown:
            raise ValueError(f"{self.name}: no such validator(s) {sorted(unknown)}")
        if self.tier not in TIERS:
            raise ValueError(f"{self.name}: no such tier {self.tier!r}")

    @property
    def needs_model(self) -> bool:
        return self.tier in ("model", "layered")

    def runs(self, validator: str) -> bool:
        return validator in self.validators


FULL = Arm(
    name="current",
    description="both tiers, every validator -- the pipeline as designed",
    tier="layered",
    validators=ALL_VALIDATORS,
    hypothesis=(
        "the baseline the other arms are measured against. It is the only arm "
        "whose silent-error budget has ever been enforced, and whatever it "
        "costs in review rate is the price of that."
    ),
)

DETERMINISTIC = Arm(
    name="deterministic",
    description="rules only, every validator -- the pipeline as it actually runs here",
    tier="rules",
    validators=ALL_VALIDATORS,
    hypothesis=(
        "that the model tier is where the recall is. This arm is not a "
        "proposal; it is what every number in this repository was measured on, "
        "because there is no API key in this environment and the family report "
        "runs the deterministic backend. Its gap from `current` is the size of "
        "the thing the rest of the evidence base has never seen."
    ),
)

MODEL_ONLY = Arm(
    name="model_only",
    description="schema-constrained extraction, citation checking only",
    tier="model",
    validators=frozenset("A"),
    hypothesis=(
        "the roadmap's claim that most of the validation stack is unproven. If "
        "this arm matches `current` on recall and silent errors, validators B "
        "through G are not earning their cost. A is kept because a value "
        "without a locatable quote is not a value, which is a property of the "
        "output rather than a semantic judgement about the deal."
    ),
)

HYBRID = Arm(
    name="hybrid",
    description="both tiers; citation checking, the orphan sweep, negative space",
    tier="layered",
    validators=frozenset("ABC"),
    hypothesis=(
        "that the three stages with a stated mechanism earn their place and "
        "the three triage stages do not. B is the only mechanism here that "
        "addresses recall rather than precision -- it asks what the document "
        "says that the extraction missed -- and C is what turns a null into a "
        "finding. D, E and F are narrower: D fires on a vocabulary of override "
        "subjects, E on a fee-letter pattern that reaches 3 documents in 100, "
        "and F only reorders a queue. Against `current` this isolates the "
        "stack, because both arms extract the same way."
    ),
)

ARMS: dict[str, Arm] = {
    arm.name: arm for arm in (FULL, DETERMINISTIC, MODEL_ONLY, HYBRID)
}
