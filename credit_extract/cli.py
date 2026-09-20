"""Command line entry point.

    credit-extract extract <file> --out result.json --passes 3 --budget 5.00

The output JSON carries, per field: value, spans with offsets into the
normalized text, the standard term it reports under, both confidence scores,
its status, and the full validation trace. Alongside it sits the document-level
report: coverage, orphan chunks with their text, unresolved conflicts, external
references, override findings, and a review queue ranked by economic
materiality.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .eval import traps as trap_checks
from .extract.passes import AnthropicBackend, OfflineRuleBackend
from .pipeline import ExtractionResult, run_document_set, run_pipeline
from .validate.calibrate import BackendMismatch, Thresholds, load_thresholds
from .validate.jev import JevClient, OfflineJev


def _build_backends(args: argparse.Namespace):
    if args.backend == "anthropic":
        extraction = AnthropicBackend(model=args.model, temperature=args.temperature)
    else:
        extraction = OfflineRuleBackend()
    jev = JevClient() if args.jev == "api" else OfflineJev()
    return extraction, jev


def _serialize(result: ExtractionResult) -> dict[str, Any]:
    payload = json.loads(result.model_dump_json())
    payload["traps"] = [t.model_dump() for t in trap_checks.check_all(result)]
    return payload


def _print_summary(result: ExtractionResult, verbose: bool) -> None:
    report = result.report
    print(f"\n{result.source_path}")
    print(f"  {report.normalized_chars:,} normalized characters, "
          f"{report.fields_total} target fields")
    print("  status: " + ", ".join(
        f"{k}={v}" for k, v in sorted(report.status_counts.items())
    ))
    print(f"  thresholds: {report.thresholds_version}")
    print(f"  cost: ${report.cost.total_usd:.4f} "
          f"({report.cost.jev_requests} Jev requests, "
          f"{report.cost.jev_questions} questions; "
          f"{report.cost.llm_calls} LLM calls)")

    if report.invariant_violations:
        print(f"\n  invariant violations ({len(report.invariant_violations)}):")
        for violation in report.invariant_violations:
            print(f"    [{violation.severity}] {violation.invariant}")
            print(f"      {violation.message}")

    if report.external_references:
        print(f"\n  external references ({len(report.external_references)}):")
        for reference in report.external_references:
            print(f"    {reference['field']} -> {reference['document']}")

    absent = [n for n, f in result.fields.items()
              if f.status == "absent_from_document"]
    if absent:
        print(f"\n  affirmatively absent ({len(absent)}):")
        for name in absent:
            field = result.fields[name]
            confidence = field.validation_confidence or 0.0
            print(f"    {name} (confirmed absent at {confidence:.2f})")

    if report.orphan_chunks:
        print(f"\n  orphan chunks ({len(report.orphan_chunks)}) -- text that "
              "says something no field captured:")
        for orphan in report.orphan_chunks[:5]:
            snippet = " ".join(orphan.text.split())[:140]
            print(f"    {orphan.chunk_id} [{orphan.top_signal} "
                  f"{orphan.score:.2f}] {snippet}")

    if report.override_findings:
        print(f"\n  override findings ({len(report.override_findings)}):")
        for finding in report.override_findings[:5]:
            snippet = " ".join(finding["governing_span"]["text"].split())[:120]
            print(f"    {finding['subject']} [{finding['probability']:.2f}] "
                  f"{snippet}")

    if report.unresolved_conflicts:
        print(f"\n  unresolved conflicts ({len(report.unresolved_conflicts)}):")
        for conflict in report.unresolved_conflicts:
            values = ", ".join(str(c["value"]) for c in conflict.candidates)
            print(f"    {conflict.field}: {values}")

    if report.review_queue:
        print(f"\n  review queue ({len(report.review_queue)}), most material "
              "first:")
        for item in report.review_queue[:10]:
            print(f"    [{item['criticality_label']}] {item['field']} "
                  f"= {item['value']}")
            print(f"      {item['why']}")

    results = trap_checks.check_all(result)
    print()
    print("  " + trap_checks.summarize(results).replace("\n", "\n  "))

    if verbose:
        stats = report.definition_graph_stats
        print(f"\n  definition graph: {stats.get('terms')} terms, "
              f"{stats.get('edges')} edges, max depth {stats.get('max_depth')}")
        if stats.get("cycles"):
            print(f"    cycles (usually drafting errors): {stats['cycles']}")
        if stats.get("external_documents"):
            print(f"    external documents: {stats['external_documents']}")


def cmd_extract(args: argparse.Namespace) -> int:
    extraction_backend, jev_backend = _build_backends(args)
    thresholds: Thresholds | None = None
    if args.thresholds:
        thresholds = load_thresholds(args.thresholds, backend=jev_backend.name)
    else:
        try:
            thresholds = load_thresholds(backend=jev_backend.name)
        except FileNotFoundError:
            print("warning: no fitted thresholds found; using the conservative "
                  "default. Run `python -m credit_extract.eval.harness "
                  "--calibrate` to fit.", file=sys.stderr)
        except BackendMismatch as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    result = run_pipeline(
        args.file,
        extraction_backend=extraction_backend,
        jev_backend=jev_backend,
        thresholds=thresholds,
        budget_usd=args.budget,
        passes=args.passes,
    )
    _print_summary(result, args.verbose)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(_serialize(result), indent=2))
        print(f"\nwrote {args.out}")

    unresolved = result.unresolved()
    if unresolved:
        print(f"\nwarning: {len(unresolved)} field(s) in an unresolved state: "
              f"{', '.join(unresolved)}", file=sys.stderr)
    if args.fail_on_violation and result.report.invariant_violations:
        return 1
    return 0


def cmd_traps(args: argparse.Namespace) -> int:
    extraction_backend, jev_backend = _build_backends(args)
    result = run_pipeline(
        args.file,
        extraction_backend=extraction_backend,
        jev_backend=jev_backend,
    )
    results = trap_checks.check_all(result)
    print(trap_checks.summarize(results))
    return 0 if all(t.caught for t in results) else 1


def cmd_chain(args: argparse.Namespace) -> int:
    extraction_backend, jev_backend = _build_backends(args)
    result = run_document_set(
        list(args.files),
        operative_as_of=args.as_of,
        extraction_backend=extraction_backend,
        jev_backend=jev_backend,
    )
    chain = result.report.chain
    print(f"\noperative text assembled from {len(chain['documents'])} document(s)")
    for document in chain["documents"]:
        label = (
            f"amendment {document['amendment_number']}"
            if document["role"] == "amendment" else document["role"]
        )
        print(f"  {label:22} {document['effective_date'] or 'undated':12} "
              f"{document['title'][:56]}")
    if chain["superseded"]:
        print(f"  superseded and excluded: {len(chain['superseded'])} document(s)")
    if chain["effects_applied"]:
        print(f"\n  amendments applied ({len(chain['effects_applied'])}):")
        for effect in chain["effects_applied"]:
            print(f"    {effect}")
    if chain["effects_unapplied"]:
        print(f"\n  COULD NOT APPLY ({len(chain['effects_unapplied'])}) -- these "
              "terms are being reported from the unamended text:")
        for item in chain["effects_unapplied"]:
            print(f"    {item['effect']}: {item['reason']}")
    for finding in chain["findings"]:
        print(f"\n  [{finding['severity']}] {finding['kind']}: "
              f"{finding['message']}")
    for disagreement in chain["amendment_effect_disagreements"]:
        print(f"\n  amendment effect disputed at Section {disagreement['section']}: "
              f"{disagreement['note']}")

    _print_summary(result, args.verbose)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(_serialize(result), indent=2))
        print(f"\nwrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credit-extract", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("file", type=Path, help=".htm/.html/.mht/.pdf/.txt")
        sub.add_argument("--backend", choices=("offline", "anthropic"),
                         default="offline",
                         help="extraction backend (default: offline, "
                              "deterministic, no API key needed)")
        sub.add_argument("--jev", choices=("offline", "api"), default="offline",
                         help="Jev backend (default: offline stand-in)")
        sub.add_argument("--model", default="claude-sonnet-5")
        sub.add_argument("--temperature", type=float, default=0.0)

    extract = subparsers.add_parser("extract", help="extract one agreement")
    add_common(extract)
    extract.add_argument("--out", type=Path, help="write the full JSON record")
    extract.add_argument("--passes", type=int, default=3,
                         help="extraction passes (one per segmentation)")
    extract.add_argument("--budget", type=float, default=5.00,
                         help="hard spend ceiling in USD")
    extract.add_argument("--thresholds", type=Path,
                         help="threshold config (default: config/thresholds.json)")
    extract.add_argument("--fail-on-violation", action="store_true",
                         help="exit non-zero if any invariant failed")
    extract.add_argument("-v", "--verbose", action="store_true")
    extract.set_defaults(func=cmd_extract)

    traps = subparsers.add_parser("traps", help="run the four trap checks")
    add_common(traps)
    traps.set_defaults(func=cmd_traps)

    chain = subparsers.add_parser(
        "chain",
        help="extract from a base agreement plus its amendments",
        description=(
            "Extraction runs on the operative text -- the base with every "
            "amendment folded in. Order is taken from effective dates, not "
            "filenames or filing dates."
        ),
    )
    chain.add_argument("files", type=Path, nargs="+",
                       help="base agreement and amendments, in any order")
    chain.add_argument("--as-of", type=date.fromisoformat, default=None,
                       help="ignore amendments effective after this date")
    chain.add_argument("--out", type=Path)
    chain.add_argument("--jev", choices=("offline", "api"), default="offline")
    chain.add_argument("--backend", choices=("offline", "anthropic"),
                       default="offline")
    chain.add_argument("--model", default="claude-sonnet-5")
    chain.add_argument("--temperature", type=float, default=0.0)
    chain.add_argument("-v", "--verbose", action="store_true")
    chain.set_defaults(func=cmd_chain)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
