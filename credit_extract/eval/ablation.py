"""Compare pipeline arms on identical inputs and identical metrics.

Roadmap step 4. The question it exists to answer is whether the validation
stack earns its cost, and it cannot be answered by the family report alone,
for a reason worth stating before any number appears:

    The family report asks "of the propositions asserted confidently, how many
    were wrong". That is the right safety question and it is monotone in
    silence. A pipeline that answers nothing scores a perfect zero. Ranking
    arms on it alone would rank the arm that extracts least above the arm that
    extracts most and errs once.

So this harness reports **recall** alongside it -- how many of the 37 critical
fields an arm actually returns a value for -- and refuses to print either one
without the other.

Three properties keep the comparison honest.

**Identical inputs.** Every arm runs over the same documents. Where an arm
cannot run on a document at all, the document leaves the comparison set for
*every* arm rather than being scored for some and skipped for others. The set
is printed with its size, because a difference measured over three documents
is a different kind of claim from one measured over a hundred.

**No silent fallback.** The model arms need a checked-in recording, since this
environment has no ``ANTHROPIC_API_KEY``. A missing recording is reported as a
missing recording. Falling back to the deterministic backend would make the
model arm quietly *be* the deterministic arm, and the ablation would then
conclude that the model tier changes nothing -- which is exactly the finding it
would have manufactured.

**Recall is counted against the registry, not against the labels.** A field an
arm returns that no label covers still counts as returned. The alternative --
counting only labelled fields -- would let an arm improve its score by
returning fewer things, which is the failure above wearing a different hat.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from ..extract.passes import LayeredBackend, OfflineRuleBackend
from ..extract.recorded import RecordedBackend, for_document
from ..models.fpml_model import FIELD_REGISTRY
from ..pipeline import ExtractionResult, run_pipeline
from .arms import ARMS, Arm
from .assertions import AssertionFile, AssertionOutcome, evaluate_file, load_assertions
from .family_report import LABELS_DIR, _resolve_document, ensure_corpus_unpacked
from .label import MIN_CRITICALITY
from .split import load_split

#: The fields recall is measured against: the registry at criticality 4+, the
#: same 37 the labelling guide freezes. Not a second list, for the reason the
#: guide gives -- a parallel schema drifts from the one the pipeline extracts.
CRITICAL = tuple(
    name for name, spec in FIELD_REGISTRY.items() if spec.criticality >= MIN_CRITICALITY
)

#: Statuses that mean the pipeline settled the field without a value. These are
#: answers, not misses, and lumping them in with review would understate every
#: arm equally and flatter none -- but it would also make the review rate
#: meaningless as a measure of human cost.
SETTLED_NULL = frozenset(
    {"absent_from_document", "external_reference", "not_applicable_to_archetype"}
)


@dataclass
class DocumentRun:
    """One arm's result on one document."""

    arm: str
    document: str
    side: str
    returned: int = 0
    confirmed: int = 0
    settled_null: int = 0
    review: int = 0
    outcomes: list[AssertionOutcome] = dc_field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0

    @property
    def confident(self) -> int:
        return sum(1 for o in self.outcomes if o.confident)

    @property
    def wrong(self) -> int:
        return sum(1 for o in self.outcomes if o.confident and not o.passed)


@dataclass
class ArmSummary:
    """One arm, aggregated over the comparison set."""

    arm: Arm
    runs: list[DocumentRun] = dc_field(default_factory=list)

    @property
    def documents(self) -> int:
        return len(self.runs)

    @property
    def recall(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.returned for r in self.runs) / (len(self.runs) * len(CRITICAL))

    @property
    def review_rate(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.review for r in self.runs) / (len(self.runs) * len(CRITICAL))

    @property
    def settled_rate(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.settled_null for r in self.runs) / (len(self.runs) * len(CRITICAL))

    @property
    def confident(self) -> int:
        return sum(r.confident for r in self.runs)

    @property
    def wrong(self) -> int:
        return sum(r.wrong for r in self.runs)

    @property
    def silent_error_rate(self) -> float:
        return self.wrong / self.confident if self.confident else 0.0

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.runs)


def _measure(result: ExtractionResult) -> tuple[int, int, int, int]:
    """Returned, confirmed, settled-null and in-review, over the 37."""
    returned = confirmed = settled = review = 0
    for name in CRITICAL:
        field = result.fields.get(name)
        if field is None:
            review += 1
            continue
        if field.value is not None:
            returned += 1
        if field.status == "confirmed":
            confirmed += 1
        elif field.status in SETTLED_NULL:
            settled += 1
        else:
            review += 1
    return returned, confirmed, settled, review


def backend_for(arm: Arm, document: str):
    """The extraction backend this arm uses on this document.

    Returns ``None`` when the arm needs a model reading the repository does not
    have. The caller drops the document from the comparison rather than
    substituting a backend, which is the whole point -- see the module
    docstring.
    """
    if arm.tier == "rules":
        return OfflineRuleBackend()
    recording = for_document(document)
    if recording is None:
        return None
    model = RecordedBackend(recording)
    if arm.tier == "model":
        return model
    return LayeredBackend(OfflineRuleBackend(), model)


def comparison_set(arms: list[Arm], files: list[AssertionFile]) -> tuple[list, list[str]]:
    """The documents every arm can run, and why the others were dropped."""
    usable, dropped = [], []
    for file in files:
        path = _resolve_document(file)
        if path is None:
            dropped.append(f"{file.document}: no source file in the corpus")
            continue
        missing = [a.name for a in arms if backend_for(a, file.document) is None]
        if missing:
            dropped.append(
                f"{file.document}: no checked-in model recording, so "
                f"{', '.join(missing)} cannot run"
            )
            continue
        usable.append((file, path))
    return usable, dropped


def run_ablation(arms: list[Arm], files: list[AssertionFile]) -> tuple[list[ArmSummary], list[str]]:
    ensure_corpus_unpacked()
    usable, dropped = comparison_set(arms, files)
    split = load_split()
    summaries = [ArmSummary(arm=arm) for arm in arms]
    for file, path in usable:
        side = split.side_of(file.corpus_name or file.document)
        for summary in summaries:
            backend = backend_for(summary.arm, file.document)
            started = time.monotonic()
            result = run_pipeline(
                path,
                extraction_backend=backend,
                document_id=file.document,
                arm=summary.arm,
            )
            returned, confirmed, settled, review = _measure(result)
            summary.runs.append(DocumentRun(
                arm=summary.arm.name,
                document=file.document,
                side=side,
                returned=returned,
                confirmed=confirmed,
                settled_null=settled,
                review=review,
                outcomes=evaluate_file(file, result),
                cost_usd=result.report.cost.total_usd,
                seconds=time.monotonic() - started,
            ))
    return summaries, dropped


def _disagreements(
    summaries: list[ArmSummary],
) -> dict[str, dict[str, tuple[bool, bool, str]]]:
    """Propositions where the arms do not agree, keyed by assertion.

    An ablation that reports only totals cannot be acted on. Two arms differing
    by one silent error is a fact about a number; *which* proposition they
    differ on is a fact about a stage, and only the second one tells you what
    to keep.
    """
    seen: dict[str, dict[str, tuple[bool, bool, str]]] = {}
    for summary in summaries:
        for run in summary.runs:
            for outcome in run.outcomes:
                seen.setdefault(outcome.assertion_id, {})[summary.arm.name] = (
                    outcome.confident, outcome.passed, str(outcome.observed)[:48],
                )
    return {
        assertion_id: rows
        for assertion_id, rows in seen.items()
        if len({(c, p) for c, p, _ in rows.values()}) > 1
    }


def print_ablation(summaries: list[ArmSummary], dropped: list[str]) -> None:
    if not summaries or not summaries[0].runs:
        print("No document can be run by every arm; there is nothing to compare.")
        for line in dropped:
            print(f"  dropped -- {line}")
        return

    n = summaries[0].documents
    print(
        f"\n{n} document(s) every arm can run, {len(CRITICAL)} critical fields each."
    )
    print(
        "Recall is values returned / fields asked for. Silent error rate is of "
        "the\npropositions asserted confidently, how many were wrong. Neither "
        "is meaningful\nwithout the other: an arm that answers nothing scores "
        "0.00 recall and a\nperfect 0.00 silent error rate."
    )

    print(f"\n{'arm':<12} {'recall':>7} {'review':>7} {'settled':>8} "
          f"{'conf':>5} {'wrong':>6} {'silent':>7} {'cost':>8} {'secs':>7}")
    for s in summaries:
        print(
            f"{s.arm.name:<12} {s.recall:>6.1%} {s.review_rate:>6.1%} "
            f"{s.settled_rate:>7.1%} {s.confident:>5d} {s.wrong:>6d} "
            f"{s.silent_error_rate:>6.1%} {s.cost_usd:>8.4f} {s.seconds:>7.1f}"
        )

    print("\nARMS")
    for s in summaries:
        print(f"  {s.arm.name} -- {s.arm.description}")
        print(f"    validators {''.join(sorted(s.arm.validators))}")
        if s.arm.hypothesis:
            print(f"    {s.arm.hypothesis}")

    disagreements = _disagreements(summaries)
    if disagreements:
        print(f"\nDISAGREEMENTS ({len(disagreements)})")
        print(
            "  Propositions the arms answer differently. This is the actionable "
            "half of\n  an ablation: a table showing one arm is worse says "
            "nothing about what to\n  change, and these say exactly which "
            "stage made the difference."
        )
        for assertion_id, rows in disagreements.items():
            print(f"\n  {assertion_id}")
            for arm_name, (confident, passed, observed) in rows.items():
                verdict = "right" if passed else "WRONG"
                weight = "asserted" if confident else "reviewed"
                print(f"    {arm_name:<14} {weight:<9} {verdict:<6} {observed}")

    if dropped:
        print(f"\nDROPPED FROM THE COMPARISON ({len(dropped)})")
        for line in dropped[:12]:
            print(f"  {line}")
        if len(dropped) > 12:
            print(f"  ... and {len(dropped) - 12} more")
        print(
            "\n  These are not failures. They are documents the comparison "
            "cannot\n  cover, and the reason this harness reports them rather "
            "than falling back\n  to a backend that would have made the model "
            "arms into the current one."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--arms", default="current,model_only,hybrid",
        help="comma-separated arm names (default: all three)",
    )
    parser.add_argument("--labels", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        arms = [ARMS[name.strip()] for name in args.arms.split(",") if name.strip()]
    except KeyError as exc:
        parser.error(f"no such arm {exc}; known arms are {', '.join(ARMS)}")

    files = load_assertions(args.labels or LABELS_DIR)
    summaries, dropped = run_ablation(arms, list(files))
    print_ablation(summaries, dropped)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
