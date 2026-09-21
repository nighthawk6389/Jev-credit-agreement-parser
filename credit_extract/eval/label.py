"""Scaffold a Tier 2 label file from a run, for a human to correct.

    python -m credit_extract.eval.label health_catalyst_2024 > labels/x.yaml

The seven label files in ``labels/`` were written by hand against an
undocumented loader, which is fine for seven and hopeless for thirty. The
roadmap's Phase 1 wants 500-odd real propositions; at the rate of writing YAML
from scratch against a 500,000-character agreement, that is the step the whole
plan stalls on.

So this emits the file already filled in with what the pipeline currently
says, each value carrying the text it was read from, and every line marked
VERIFY. The labeller's job becomes reading the quote and either accepting the
value or replacing it -- which is a different and much smaller job than
finding the clause in the first place.

Two things it deliberately does not do.

It does not write assertions the pipeline is confident about and leave them
unmarked, because a label that agrees with the extractor by construction
measures nothing. Every emitted assertion is commented VERIFY and the file
will not load until those markers are removed.

It does not guess at a value the pipeline did not find. Where the field went
to review the stub carries ``expect: TODO`` and the review hint's offset, so
the labeller knows roughly where to look and knows that nobody has looked yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ..models.fpml_model import FIELD_REGISTRY
from ..pipeline import run_pipeline
from .family_report import _resolve_named, ensure_corpus_unpacked
from .split import load_split

#: Criticality at or above which a field is worth a human's time. The registry
#: puts 37 of its 56 fields here, which is the roadmap's "25-40 economically
#: useful fields" without anyone having to draw up a second list that then
#: drifts from the first.
MIN_CRITICALITY = 4

#: The marker a labeller deletes once they have checked a line. The loader
#: rejects a file that still contains it, so a scaffold cannot be committed as
#: though it were ground truth.
VERIFY = "VERIFY"

#: What a labeller has to write when the scaffold will not guess.
CHOOSE = "CHOOSE"

#: Field name fragment -> (family, member), for the cases where the field is
#: the family. These are the ones where guessing is safe because the field
#: exists precisely to carry that trap: a floor is the F07 floor question, a
#: borrowing base is the F06 ABL question. Everything else gets CHOOSE rather
#: than a plausible wrong answer -- an assertion filed under the wrong family
#: moves that family's coverage without testing it, which is worse for this
#: report than an assertion nobody wrote.
_SUGGESTED: tuple[tuple[str, str, str], ...] = (
    ("credit_spread_adjustment", "F07_benchmark", "sofr_credit_spread_adjustment"),
    ("floor", "F07_benchmark", "floor_on_base_vs_all_in"),
    ("applicable_margin", "F07_benchmark", "pricing_grid_stepdowns"),
    ("pik", "F06_structure", "pik_toggle"),
    ("borrowing_base", "F06_structure", "abl_borrowing_base"),
    ("arr.", "F06_structure", "recurring_revenue_arr"),
    ("delayed_draw", "F06_structure", "delayed_draw_multi_tranche"),
    ("facility.lien", "F06_structure", "second_lien"),
    ("facility.seniority", "F06_structure", "second_lien"),
    ("nav.", "F06_structure", "nav_or_subscription_line"),
    ("financial_covenant", "F10_conditionality", "time_and_leverage_stepdown"),
    ("leverage", "F10_conditionality", "time_and_leverage_stepdown"),
)


def _suggest(name: str) -> tuple[str, str]:
    for fragment, family, member in _SUGGESTED:
        if fragment in name:
            return family, member
    return CHOOSE, CHOOSE


def _quote(result: Any, name: str) -> str:
    """The text the value was read from, or where to start looking."""
    field = result.fields.get(name)
    if field is None:
        return "no such extraction target"
    if field.spans:
        span = field.spans[0]
        text = " ".join(span.text.split())
        return f"[{span.start}] {text[:200]}"
    hint = getattr(field, "review_hint", None)
    if hint is not None:
        return (
            f"not found; the strongest signal is in the chunk at "
            f"[{hint.start}:{hint.end}] -- read it and fill in expect"
        )
    return "not found, and no chunk looked like it addressed this"


def _expected(result: Any, name: str) -> tuple[Any, str]:
    """What the pipeline says, and which assertion kind states it."""
    field = result.fields.get(name)
    if field is None:
        return "TODO", "field_value"
    if field.status == "absent_from_document":
        return "absent_from_document", "field_status"
    if field.status == "external_reference":
        return "external_reference", "field_status"
    if field.value is None:
        return "TODO", "field_value"
    return field.value, "field_value"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    text = str(value)
    if text == "TODO" or any(c in text for c in ":#{}[]|>*&!%@`\"'\n"):
        return f'"{text}"'
    return text


def scaffold(path: Path, document: str, tier: int = 2) -> str:
    """Render a label file for ``path`` with every assertion marked VERIFY."""
    result = run_pipeline(path)
    split = load_split()
    side = split.side_of(document)

    specs = sorted(
        (s for s in FIELD_REGISTRY.values() if s.criticality >= MIN_CRITICALITY),
        key=lambda s: (s.field_class, -s.criticality, s.name),
    )

    lines = [
        f"# Tier {tier} assertions for {document}.",
        "#",
        f"# Scaffolded from a run of {path.name}, which means every value below",
        "# is what the extractor said, not what the document says. They agree",
        "# only where the extractor was right.",
        "#",
        f"# Split side: {side.upper()}."
        + (
            "  Labels here may be used to fit thresholds."
            if side == "fit" else
            "  These labels must not be used to fit"
            "\n#   thresholds, prompts or patterns. That is what a holdout is."
            if side == "holdout" else
            "  This document is not in the frozen split."
        ),
        "#",
        f"# Every assertion carries {VERIFY}. Delete the marker once you have read",
        "# the quote and either accepted the value or replaced it. The loader",
        f"# refuses a file that still contains {VERIFY}, so a scaffold cannot be",
        "# mistaken for ground truth.",
        "#",
        f"# {CHOOSE} means the scaffold would have had to guess which trap family a",
        "# proposition belongs to. It does not guess, because an assertion filed",
        "# under the wrong family moves that family's coverage without testing",
        "# it. See docs/labelling_guide.md for how to pick one.",
        "",
        f"document: {document}",
        "source: real",
        f"tier: {tier}",
        "",
        "assertions:",
    ]

    current_class = None
    for spec in specs:
        if spec.field_class != current_class:
            current_class = spec.field_class
            lines.append(f"\n  # -- {current_class} " + "-" * (58 - len(current_class)))
        expect, kind = _expected(result, spec.name)
        family, member = _suggest(spec.name)
        stub = spec.name.replace(".", "_")
        lines += [
            f"  - id: {document[:24]}_{stub}",
            f"    family: {family}",
            f"    member: {member}",
            f"    kind: {kind}",
            f"    target: {spec.name}",
            f"    expect: {_yaml_scalar(expect)}",
            f"    note: >-  # {VERIFY}",
            f"      {spec.description}.",
            f"      {_quote(result, spec.name)}",
        ]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="a corpus document name or a path")
    parser.add_argument("--tier", type=int, default=2)
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)

    candidate = Path(args.document)
    if candidate.exists():
        path, document = candidate, candidate.stem
    else:
        ensure_corpus_unpacked()
        path = _resolve_named(args.document)
        if path is None:
            print(f"no document named {args.document!r}", file=sys.stderr)
            return 1
        document = args.document

    text = scaffold(path, document, tier=args.tier)
    if args.out:
        args.out.write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
