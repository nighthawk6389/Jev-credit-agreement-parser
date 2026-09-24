"""Fit the support floor against labelled outcomes, instead of asserting it.

``reconcile`` demotes a field to ``needs_review`` when fewer than
``SUPPORT_FLOOR`` independent views backed the winning value::

    elif winner.support < SUPPORT_FLOOR and not winner.deterministic:
        field.status = "needs_review"

Every other threshold in this repository is fitted. ``validate/calibrate.py``
takes labelled samples of ``(probability, was it correct)``, picks the lowest
cut that hits a target precision for the field class, and stores the silent
error rate it achieved. This one was a literal ``2``, chosen when a run always
had three segmentations to sweep, and it governs the same decision.

Dropping the definitional sweep made that stop being harmless. With three
views ``< 2`` was a majority rule; with two it is unanimity, and on Essential
Properties it demotes three correct values -- the revolver commitment, the
term loan commitment and the credit quality -- that differ from the labels in
no way except how many views happened to reach them.

WHAT THIS MEASURES, AND WHAT IT CANNOT
======================================

For every labelled assertion about a field, the harness now records the
support behind the winning value. Bucketing those by support gives, per
bucket, the fraction that matched the label: the precision of "trust a value
backed by exactly N views".

A floor of N is then defensible when the buckets below it are materially worse
than the buckets at or above it. If support 1 is as accurate as support 2,
the floor is buying review queue and paying for it in coverage.

Two limits, stated because a fitted number invites more confidence than the
fit deserves. The corpus runs on the deterministic tier, so most fields carry
deterministic support and are exempt from the rule anyway -- the population
this fits on is the non-deterministic remainder, which is small. And the fit
is over the whole corpus, fit side included; a floor tuned on documents the
rules were built against is optimistic by construction, which is why the
report splits it.

WHAT THE FIRST RUN ACTUALLY FOUND
=================================

Not a verdict on the floor. Four outcomes fall below it, so the answer is
INCONCLUSIVE and :data:`SUPPORT_FLOOR` stays a policy. What the run did find
is a defect in ``support`` itself::

     support     n  passed  precision
           1     4       2      0.500
           2    20      18      0.900
           3    43      22      0.512

More independent views, worse precision. That inverts the premise the whole
corroboration model rests on, and it is not noise. Sixteen of the support-3
failures are one field -- ``closing_date`` -- and within that field alone the
inversion is stark::

    closing/effective date assertions    support 2:  12/13 = 0.923
                                         support 3:   9/24 = 0.375
    everything else                      support 2:   6/7  = 0.857
                                         support 3:  13/19 = 0.684

A closing date appears in the preamble, in the definitions, and in every
amendment recital, so it is found by every segmentation -- and the extractor
picks the wrong instance. High support on a recurring string is evidence that
**the string is everywhere**, not that the reading is right. For a value
stated once, agreement across views means what it is supposed to mean; for a
value stated eleven times, it means the opposite.

This is a limit of the span test in ``Candidate.echoes`` too, and worth being
plain about since that test is new. It distinguishes a parrot from a second
reading by asking whether the citation moved. For ``closing_date`` the
citations genuinely differ -- they are simply all wrong instances, so the
spans diverge and the candidates count as independent support for a value
nobody should trust.

Fixing it is not a threshold change. It needs the extractor to decide *which*
occurrence of a recurring value is the operative one, which is what the
definition graph is for: the closing date is a defined term, and the
definition is the answer while the recitals are mentions.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

from .assertions import AssertionOutcome
from .split import load_split

#: Support levels below this are demoted by ``reconcile``. Imported there so
#: there is one definition, and named so a reader can find this module from
#: the rule.
#:
#: **This is a documented policy, not a fitted number**, and the distinction is
#: the point of having run the fit. Of the labelled outcomes the floor can act
#: on -- fields that produced a value -- four fall below it, one of them on the
#: held-out side. Four outcomes settle nothing, so the honest result is
#: INCONCLUSIVE and the floor keeps its value on the grounds that a value
#: reached by one view of the document is weaker evidence than one reached by
#: several, which is an argument rather than a measurement.
#:
#: What it would take to fit it: enough labelled fields that land at support 1
#: with a value. The corpus produces almost none, because the deterministic
#: tier either finds a thing in every view or in none.
SUPPORT_FLOOR = 2

#: Kinds whose outcome is about a field's value, and so whose support means
#: something. A ``text_present`` assertion has no field behind it.
_VALUE_KINDS = frozenset({"field_value", "resolve"})


def _side_of(outcome: AssertionOutcome) -> str:
    """Which side of the frozen split this outcome was measured on.

    Same resolution the family report uses: the label file's document name,
    not the pipeline's internal id, because the split is keyed on the former
    and the latter is a hash that says nothing about it.
    """
    split = load_split()
    name = outcome.label_document or outcome.document
    if name in split.contaminated:
        return "contaminated"
    return split.side_of(name)


@dataclass
class Bucket:
    """Every labelled outcome that arrived with one particular support."""

    support: int
    passed: int = 0
    failed: int = 0
    ids: list[str] = dc_field(default_factory=list)

    @property
    def n(self) -> int:
        return self.passed + self.failed

    @property
    def precision(self) -> float | None:
        return self.passed / self.n if self.n else None


def buckets_from(
    outcomes: Iterable[AssertionOutcome], side: str | None = None
) -> dict[int, Bucket]:
    """Group labelled field outcomes by the support behind them.

    Only outcomes where the pipeline produced a **value** are counted, and
    that restriction is the whole validity of the fit.

    ``reconcile``'s floor sits on the branch that runs after a winning value
    has been chosen. A field no pass produced a candidate for takes an earlier
    branch, keeps ``pass_support`` at 0, and is never demoted by the floor
    because there is nothing to demote. Counting those outcomes measures
    whether the extractor finds things, which is a real question and not this
    one -- and on this corpus it swamps the answer: 212 of 216 below-floor
    outcomes were fields with no candidate at all, which dragged the
    below-floor precision to 0.097 and made the floor look vindicated by
    evidence that never bore on it.
    """
    out: dict[int, Bucket] = {}
    for outcome in outcomes:
        if outcome.kind not in _VALUE_KINDS or outcome.support is None:
            continue
        if outcome.observed is None:
            # No value, so the floor could not have acted on it either way.
            continue
        if outcome.source != "real":
            # A synthetic fixture's support says more about the fixture than
            # about how documents are written.
            continue
        if side is not None and _side_of(outcome) != side:
            continue
        bucket = out.setdefault(outcome.support, Bucket(support=outcome.support))
        if outcome.passed:
            bucket.passed += 1
        else:
            bucket.failed += 1
            bucket.ids.append(outcome.assertion_id)
    return out


def recommend(buckets: dict[int, Bucket], floor: int = SUPPORT_FLOOR) -> str:
    """Whether the evidence supports the floor as it stands.

    Deliberately conservative. Recommending a *lower* floor widens what the
    pipeline presents as settled, which is the direction that produces silent
    errors, so it takes a clean result to earn it: the below-floor bucket has
    to be both populated and no worse than the rest.
    """
    below = [b for s, b in buckets.items() if s < floor]
    at_or_above = [b for s, b in buckets.items() if s >= floor]
    n_below = sum(b.n for b in below)
    n_above = sum(b.n for b in at_or_above)
    if n_below < 10:
        return (
            f"INCONCLUSIVE: only {n_below} labelled outcome(s) below the floor. "
            "Keep it where it is and say so; a floor nobody can measure is a "
            "policy, not a fit, and should be documented as one."
        )
    p_below = sum(b.passed for b in below) / n_below
    p_above = sum(b.passed for b in at_or_above) / n_above if n_above else 0.0
    if p_below >= p_above:
        return (
            f"LOWER IT: below-floor precision {p_below:.3f} over {n_below} "
            f"outcomes is at least as good as {p_above:.3f} over {n_above} "
            "at or above. The floor is buying review queue and paying "
            "coverage for it."
        )
    return (
        f"KEEP IT: below-floor precision {p_below:.3f} over {n_below} outcomes "
        f"against {p_above:.3f} over {n_above} at or above. Values that only "
        "one view reached are measurably worse, which is what the floor is for."
    )


def report(outcomes: list[AssertionOutcome], floor: int = SUPPORT_FLOOR) -> str:
    lines = [
        "SUPPORT FLOOR FIT",
        f"  current floor: support < {floor} is demoted to needs_review",
        "",
    ]
    for side in (None, "fit", "holdout"):
        buckets = buckets_from(outcomes, side)
        label = side or "all real"
        lines.append(f"  [{label}]")
        if not buckets:
            lines.append("    no labelled field outcomes carried a support")
            lines.append("")
            continue
        lines.append(f"    {'support':>8s} {'n':>5s} {'passed':>7s} {'precision':>10s}")
        for level in sorted(buckets):
            b = buckets[level]
            p = b.precision
            lines.append(
                f"    {level:>8d} {b.n:>5d} {b.passed:>7d} "
                f"{(f'{p:.3f}' if p is not None else '--'):>10s}"
            )
        lines.append(f"    -> {recommend(buckets, floor)}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outcomes", type=Path,
        help="JSON array of AssertionOutcome records; omit to run the corpus",
    )
    parser.add_argument("--floor", type=int, default=SUPPORT_FLOOR)
    parser.add_argument(
        "--no-mutations", action="store_true",
        help="skip mutants; halves the runtime and changes no real outcome",
    )
    parser.add_argument(
        "--save", type=Path,
        help="write the outcomes as JSON so the fit can be re-cut without "
             "another full corpus pass -- read back with --outcomes",
    )
    args = parser.parse_args(argv)

    if args.outcomes:
        raw = json.loads(args.outcomes.read_text())
        outcomes = [AssertionOutcome.model_validate(r) for r in raw]
    else:
        from .family_report import ensure_corpus_unpacked, run_coverage

        ensure_corpus_unpacked()
        run = run_coverage(include_mutations=not args.no_mutations)
        outcomes = run.outcomes

    if args.save:
        args.save.write_text(
            json.dumps([o.model_dump(mode="json") for o in outcomes], indent=1)
        )

    print(report(outcomes, args.floor))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
