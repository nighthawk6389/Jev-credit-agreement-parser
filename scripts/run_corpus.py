"""Run the pipeline over the EDGAR corpus and report what happened.

    python scripts/run_corpus.py --limit 10
    python scripts/run_corpus.py --out corpus/edgar/run.json

This is a *validation* run, not an accuracy measurement. Nothing in
``corpus/edgar`` is labelled, so what it can establish is narrow and worth
stating precisely:

* whether the pipeline completes at all on a hundred real agreements, and
  where it crashes when it does not;
* what it says about each document -- archetype, statuses, findings -- and
  whether those claims are consistent with the stratum the harvest assigned;
* which invariants fire, and how often. An invariant firing on most of the
  corpus is not detecting a market-wide defect, it is miscalibrated, and that
  is visible here and nowhere else.

The last one is the reason this exists. A check that fires everywhere reads
as noise, and a reader who learns to skip it will skip it on the document
where it was right.

The raw documents are unzipped into ``corpus/edgar/work`` on demand and are
not checked in; only the zips are.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import traceback
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EDGAR = ROOT / "corpus" / "edgar"
WORK = EDGAR / "work"
RAW_ZIP = EDGAR / "edgar_corpus_raw.zip"

#: An agreement can carry several of these; the harvest records the one it was
#: selected for. Used to check the pipeline's archetype against a coarse
#: expectation, not to score it -- these are harvest strata, not labels.
STRATUM_ARCHETYPE = {
    "A": {"cash_flow_term_loan", "unitranche"},
    "B": {"cash_flow_term_loan"},
    "C": {"abl_revolver"},
    "D": {"second_lien"},
    "E": {"recurring_revenue"},
    "F": {"holdco_pik"},
    "G": {"venture_debt"},
    "H": {"nav_or_subscription"},
    "I": {"investment_grade"},
    "J": {"european_lma"},
    "K": {"dip"},
}


def ensure_unpacked() -> Path:
    raw = WORK / "edgar_corpus" / "raw"
    if raw.exists() and any(raw.iterdir()):
        return raw
    if not RAW_ZIP.exists():
        raise SystemExit(f"{RAW_ZIP} is missing; nothing to run against")
    WORK.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(RAW_ZIP) as archive:
        archive.extractall(WORK)
    return raw


def load_manifest() -> dict[str, dict[str, str]]:
    path = WORK / "edgar_corpus" / "manifest.csv"
    with path.open() as handle:
        return {row["name"]: row for row in csv.DictReader(handle)}


#: A document that takes longer than this has hit something pathological
#: rather than something large. The 13MB filings in this corpus run in about
#: nine seconds; the one that motivated this budget was 944KB and had not
#: finished after an hour, because ``DefinitionGraph.depth`` was searching
#: every simple path through a graph with 96 cycles in it. Without a budget
#: that document silently consumed the whole run and reported nothing at all.
DOCUMENT_BUDGET_SECONDS = 300


class DocumentTimeout(RuntimeError):
    pass


def _budget(seconds: int):
    """Abort a document that has stopped making progress.

    SIGALRM rather than a thread, so the interrupt lands inside whatever tight
    loop is running and the traceback names it.
    """
    import signal

    def fire(signum, frame):        # noqa: ANN001, ARG001
        raise DocumentTimeout(
            f"exceeded {seconds}s; something is pathological rather than large"
        )

    if not hasattr(signal, "SIGALRM"):
        return None, None
    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    return signal, previous


def run_one(path: Path, timeout_note: list[str]) -> dict[str, Any]:
    from credit_extract.pipeline import run_pipeline

    started = time.monotonic()
    signal_module, previous = _budget(DOCUMENT_BUDGET_SECONDS)
    try:
        result = run_pipeline(path)
    finally:
        if signal_module is not None:
            signal_module.alarm(0)
            signal_module.signal(signal_module.SIGALRM, previous)
    report = result.report
    return {
        "document": path.stem,
        "seconds": round(time.monotonic() - started, 2),
        "chars": report.normalized_chars,
        "sections": len(result.document.sections) if result.document else 0,
        "tables": len(result.document.tables) if result.document else 0,
        "blackline": bool(result.document and result.document.is_blackline),
        "deletions": len(result.document.deletions()) if result.document else 0,
        "archetype": result.archetype.archetype,
        "archetype_confidence": round(result.archetype.confidence, 3),
        "archetype_basis": result.archetype.basis,
        "base_rate": result.pricing.base,
        "csa": str(result.pricing.credit_spread_adjustment.value)
               if result.pricing.credit_spread_adjustment else None,
        "floor": str(result.pricing.floor.value) if result.pricing.floor else None,
        "floor_applies_to": result.pricing.floor_applies_to,
        "grid_levels": len(result.pricing.grid),
        "statuses": report.status_counts,
        "confirmed": report.status_counts.get("confirmed", 0),
        "fields": report.fields_total,
        "orphans": len(report.orphan_chunks),
        "invariants": sorted({v.invariant for v in report.invariant_violations}),
        "invariant_count": len(report.invariant_violations),
        "chain_findings": sorted(
            {f["kind"] for f in report.chain.get("findings", [])}
        ),
        "effects_applied": len(report.chain.get("effects_applied", [])),
        "effects_unapplied": len(report.chain.get("effects_unapplied", [])),
        "cost_usd": round(report.cost.total_usd, 4),
    }


def summarize(rows: list[dict[str, Any]], errors: list[dict[str, str]],
              manifest: dict[str, dict[str, str]]) -> str:
    total = len(rows) + len(errors)
    out: list[str] = [
        "",
        f"EDGAR CORPUS RUN -- {len(rows)} of {total} completed, "
        f"{len(errors)} failed",
        "",
    ]
    if errors:
        out.append("FAILURES")
        for error in errors:
            out.append(f"  {error['document'][:64]}")
            out.append(f"    {error['error'][:160]}")
        out.append("")

    if not rows:
        return "\n".join(out)

    seconds = [r["seconds"] for r in rows]
    chars = [r["chars"] for r in rows]
    out += [
        f"throughput: {sum(seconds):.0f}s total, "
        f"{statistics.median(seconds):.1f}s median, {max(seconds):.1f}s worst",
        f"size: {statistics.median(chars):,.0f} chars median, "
        f"{max(chars):,} worst",
        "",
    ]

    # -- what the pipeline said about each document ------------------------
    out.append("ARCHETYPE (detected, against the stratum it was harvested for)")
    per_stratum: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        stratum = manifest.get(row["document"], {}).get("stratum", "?")
        per_stratum[stratum][row["archetype"]] += 1
    for stratum in sorted(per_stratum):
        expected = STRATUM_ARCHETYPE.get(stratum)
        detected = per_stratum[stratum]
        agree = sum(n for a, n in detected.items() if expected and a in expected)
        note = (
            f"  {agree}/{sum(detected.values())} agree"
            if expected else "  (no archetype expectation for this stratum)"
        )
        top = ", ".join(f"{a}={n}" for a, n in detected.most_common(4))
        out.append(f"  {stratum}: {top}{note}")
    out.append("")

    out.append("BASE RATE")
    for base, n in Counter(r["base_rate"] for r in rows).most_common():
        out.append(f"  {base or 'unknown':22s} {n:3d}")
    out.append("")

    blacklines = [r for r in rows if r["blackline"]]
    out += [
        f"BLACKLINES: {len(blacklines)} document(s) carry deletion markup, "
        f"{sum(r['deletions'] for r in blacklines)} deleted run(s) excised",
        "",
    ]

    out.append("INVARIANTS (documents firing / documents run)")
    fired = Counter()
    for row in rows:
        fired.update(row["invariants"])
    for name, n in fired.most_common():
        share = 100 * n // len(rows)
        flag = "   <-- fires on most of the corpus; miscalibrated" if share >= 60 else ""
        out.append(f"  {name:42s} {n:3d}  {share:3d}%{flag}")
    if not fired:
        out.append("  (none)")
    out.append("")

    out.append("CHAIN FINDINGS")
    chain = Counter()
    for row in rows:
        chain.update(row["chain_findings"])
    for name, n in chain.most_common():
        out.append(f"  {name:42s} {n:3d}")
    if not chain:
        out.append("  (none)")
    out.append("")

    confirmed = [r["confirmed"] for r in rows]
    out += [
        "EXTRACTION",
        f"  confirmed fields: {sum(confirmed)} across {len(rows)} documents "
        f"({statistics.median(confirmed):.0f} median of {rows[0]['fields']} targets)",
        f"  documents with zero confirmed: "
        f"{sum(1 for c in confirmed if c == 0)}",
        f"  orphan chunks: {sum(r['orphans'] for r in rows):,} total, "
        f"{statistics.median([r['orphans'] for r in rows]):.0f} median",
        "",
        "  Nothing here is labelled, so none of this is accuracy. It says the "
        "pipeline runs,",
        "  what it claims, and which of its checks are calibrated.",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stratum", default="", help="only this harvest stratum")
    parser.add_argument("--out", type=Path, default=EDGAR / "run.json")
    args = parser.parse_args(argv)

    raw = ensure_unpacked()
    manifest = load_manifest()
    paths = sorted(raw.glob("*.htm"))
    if args.stratum:
        paths = [
            p for p in paths
            if manifest.get(p.stem, {}).get("stratum") == args.stratum
        ]
    if args.limit:
        paths = paths[: args.limit]

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, path in enumerate(paths, 1):
        size = path.stat().st_size
        print(
            f"[{index}/{len(paths)}] {size / 1e6:5.1f}MB {path.stem[:60]}",
            end="", flush=True,
        )
        try:
            row = run_one(path, [])
            rows.append(row)
            # Timed per document and printed as it finishes, because the run
            # this was written after spent an hour inside one document and the
            # only evidence was that the next line never appeared.
            print(f"  {row['seconds']:6.1f}s", flush=True)
        except Exception as exc:                      # noqa: BLE001
            errors.append({
                "document": path.stem,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            })
            print(f"  FAILED {type(exc).__name__}: {exc}", flush=True)

    report = summarize(rows, errors, manifest)
    print(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"rows": rows, "errors": errors}, indent=2, default=str
    ))
    (args.out.with_suffix(".txt")).write_text(report)
    print(f"\nwrote {args.out} and {args.out.with_suffix('.txt')}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
