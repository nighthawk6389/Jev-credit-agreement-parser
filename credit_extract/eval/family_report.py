"""Per-family coverage, measured and reported the way the spec requires.

Three rules this module exists to enforce:

* **Never an aggregate alone.** The per-family table is printed with every
  summary, because an accuracy figure that averages over untested document
  families is worse than no figure -- it will be believed.
* **Synthetic and real never blended.** Catching a defect we injected, in the
  exact shape we injected it, demonstrates less than catching the same defect
  in a real filing. The counts stay in separate columns.
* **The blind-spot register is part of the report.** Families with no testable
  examples are named in every run, not footnoted in a design document.

CI gates on the silent error rate per family: of the propositions the pipeline
asserted confidently, what fraction were wrong. A proposition routed to review
and wrong was handled correctly; one asserted and wrong reaches a reader
unannounced, and that is the only failure that hurts.
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

from ..pipeline import run_document_set, run_pipeline
from ..validate.jev import answer_cache_for, build_backend
from .assertions import (
    AssertionFile, AssertionOutcome, evaluate_assertion, evaluate_file,
    load_assertion_file, load_assertions,
)
from .families import (
    CoverageReport, FamilyCoverage, empty_coverage, load_blind_spots,
    load_families,
)
from .mutations import apply_all

ROOT = Path(__file__).resolve().parents[2]
LABELS_DIR = Path(__file__).parent / "labels"
GOLD_DIR = Path(__file__).parent / "gold"
CORPUS_DIR = ROOT / "corpus"


@dataclass
class CoverageRun:
    outcomes: list[AssertionOutcome] = dc_field(default_factory=list)
    documents: int = 0
    mutants: int = 0
    cost_usd: float = 0.0
    notes: list[str] = dc_field(default_factory=list)
    #: Every ``version@backend`` the runs were triaged under.
    thresholds: set[str] = dc_field(default_factory=set)
    #: Label files that raised instead of running, each with its error. A
    #: file that did not run is not a file that passed, so each fails the gate.
    did_not_run: list[str] = dc_field(default_factory=list)

    def record(self, result: Any) -> None:
        self.cost_usd += result.report.cost.total_usd
        self.thresholds.add(result.report.thresholds_version or "unknown")

    def absorb(self, other: "CoverageRun") -> None:
        """Fold in a run over other label files, after this one's."""
        self.outcomes.extend(other.outcomes)
        self.documents += other.documents
        self.mutants += other.mutants
        self.cost_usd += other.cost_usd
        self.notes.extend(other.notes)
        self.thresholds |= other.thresholds
        self.did_not_run.extend(other.did_not_run)

    # -- the headline, and the table it is never printed without -----------

    @property
    def confident(self) -> list[AssertionOutcome]:
        return [o for o in self.outcomes if o.confident]

    @property
    def silent_errors(self) -> list[AssertionOutcome]:
        return [o for o in self.outcomes if o.silent_error]

    @property
    def silent_error_rate(self) -> float:
        confident = self.confident
        return len(self.silent_errors) / len(confident) if confident else 0.0

    def coverage(self) -> CoverageReport:
        families = empty_coverage()
        for outcome in self.outcomes:
            record = families.get(outcome.family)
            if record is None:
                continue
            record.n += 1
            if outcome.source == "real":
                record.n_real += 1
            else:
                record.n_synthetic += 1
            if outcome.passed:
                record.passed += 1
            else:
                record.failed += 1
            if outcome.confident:
                record.confirmed += 1
                if not outcome.passed:
                    record.confirmed_wrong += 1
        return CoverageReport(
            families=families,
            blind_spots=load_blind_spots(),
            corpus_kind=(
                "real + synthetic"
                if any(o.source == "real" for o in self.outcomes)
                else "synthetic only"
            ),
        )

    def render(self) -> str:
        """The whole report. There is no way to get the headline without it."""
        report = self.coverage()
        real = sum(1 for o in self.outcomes if o.source == "real")
        synthetic = len(self.outcomes) - real
        lines = [
            "",
            f"{self.documents} document(s) and {self.mutants} mutant(s), "
            f"{len(self.outcomes)} assertions "
            f"({real} real / {synthetic} synthetic), ${self.cost_usd:.4f}",
            f"thresholds: {', '.join(sorted(self.thresholds)) or 'none'}",
        ]
        if any(t.startswith("unfitted@") for t in self.thresholds):
            lines += [
                "  UNFITTED: no thresholds were fitted for this scorer, so every",
                "  field was triaged at the conservative default. This is a",
                "  calibration run. Its silent-error rate is not comparable to a",
                "  fitted run's, on this scorer or any other.",
            ]
        lines += [
            "",
            report.render(),
            "",
            "HEADLINE",
            f"  of {len(self.confident)} propositions asserted confidently, "
            f"{len(self.silent_errors)} were wrong "
            f"({self.silent_error_rate:.2%} silent error rate)",
        ]
        for outcome in self.silent_errors[:10]:
            lines.append(
                f"    {outcome.family} {outcome.assertion_id}: expected "
                f"{outcome.expected!r}, got {outcome.observed!r}"
            )
        if report.over_budget_families:
            lines += [
                "",
                "OVER BUDGET: " + ", ".join(report.over_budget_families),
            ]
        if report.untested_families:
            lines += [
                "",
                "UNTESTED FAMILIES: " + ", ".join(report.untested_families),
                "  No assertion in the corpus exercises these. Their behaviour "
                "is unknown and",
                "  nothing in the numbers above says otherwise.",
            ]
        if report.undersampled_families:
            lines += [
                "",
                "UNDERSAMPLED: " + ", ".join(report.undersampled_families),
                "  Below the family's configured minimum; treat these metrics "
                "as directional.",
            ]
        lines += self._split_lines()
        if self.did_not_run:
            lines += [
                "",
                f"DID NOT RUN: {len(self.did_not_run)} label file(s). Their "
                "assertions are in none of the numbers above.",
            ]
            lines += [f"  {entry}" for entry in self.did_not_run]
        for note in self.notes:
            lines.append(f"\n  note: {note}")
        return "\n".join(lines)

    def _split_lines(self) -> list[str]:
        """Which side of the frozen split each real assertion came from.

        Printed with the headline rather than on request, because a precision
        figure measured entirely on documents the parser was built against is
        a different claim from the same figure on held-out ones, and the two
        are indistinguishable once the number is quoted on its own.
        """
        from .split import load_split

        real = [o for o in self.outcomes if o.source == "real"]
        if not real:
            return []
        split = load_split()
        sides: dict[str, list[AssertionOutcome]] = {}
        for outcome in real:
            name = outcome.label_document or outcome.document
            side = (
                "contaminated" if name in split.contaminated
                else split.side_of(name)
            )
            sides.setdefault(side, []).append(outcome)

        lines = ["", "SPLIT (real assertions only)"]
        for side in ("holdout", "fit", "contaminated", "unassigned"):
            group = sides.get(side)
            if not group:
                continue
            wrong = sum(1 for o in group if o.silent_error)
            confident = sum(1 for o in group if o.confident)
            lines.append(
                f"  {side:14} {len(group):>3} assertions, {confident} confident, "
                f"{wrong} wrong"
            )
        if not sides.get("holdout"):
            lines += [
                "  Nothing has been measured on a held-out document. Every real",
                "  assertion above is on a filing that was read while the parser",
                "  was written, so these numbers describe fit, not generalisation.",
                f"  {len(split.documents('holdout'))} documents are reserved and",
                "  unlabelled; credit_extract/eval/split.py --check keeps them that way.",
            ]
        return lines

    def gate_failures(self) -> list[str]:
        """What should fail the build."""
        report = self.coverage()
        failures = [
            f"{family}: silent error rate "
            f"{report.families[family].silent_error_rate:.3f} exceeds budget "
            f"{report.families[family].silent_error_budget:.3f}"
            for family in report.over_budget_families
        ]
        if not report.blind_spots.entries:
            failures.append(
                "the blind-spot register is empty or missing; a report without "
                "it overstates what has been tested"
            )
        if self.did_not_run:
            failures.append(
                f"{len(self.did_not_run)} label file(s) did not run; the report "
                "covers only the rest"
            )
        return failures


#: Every format the corpus actually holds. ``.mht`` is here because that is
#: what a browser saves an EDGAR filing as, and it is how the first real
#: documents arrived.
_DOCUMENT_SUFFIXES = (".html", ".htm", ".mht", ".mhtml", ".pdf", ".txt")


#: The harvested corpus, kept zipped because it is 143MB unpacked.
CORPUS_ZIP = CORPUS_DIR / "edgar" / "edgar_corpus_raw.zip"
CORPUS_WORK = CORPUS_DIR / "edgar" / "work"


def ensure_corpus_unpacked() -> None:
    """Unpack the harvested corpus if a label needs it and it is not there.

    The unpacked corpus is generated output and is not checked in, so without
    this a chain label silently reports "chain incomplete" on any clean
    checkout -- including CI, where the whole point is that the chain
    assertions run.
    """
    import zipfile

    if (CORPUS_WORK / "edgar_corpus" / "raw").is_dir() or not CORPUS_ZIP.exists():
        return
    CORPUS_WORK.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(CORPUS_ZIP) as archive:
        archive.extractall(CORPUS_WORK)


def _resolve_named(name: str) -> Path | None:
    """Find one document by name, anywhere in the corpus.

    Chain members are named rather than pathed so a label reads as a list of
    documents and does not break when the corpus is unpacked somewhere else.
    A harvested file carries its company, date and accession number in front
    of the exhibit name, so a trailing match counts: a label naming
    ``ex-101xwheelsupamendno5toc`` should find
    ``L_wheels-up-...-26-000123_ex-101xwheelsupamendno5toc.htm`` without
    repeating the harvest metadata that a re-harvest would change anyway.
    """
    for directory in (GOLD_DIR, CORPUS_DIR, CORPUS_DIR / "gold", CORPUS_DIR / "real"):
        for suffix in _DOCUMENT_SUFFIXES:
            candidate = directory / f"{name}{suffix}"
            if candidate.exists():
                return candidate
    exact = [
        path for path in sorted(CORPUS_DIR.rglob(f"{name}.*"))
        if path.suffix.lower() in _DOCUMENT_SUFFIXES
    ]
    if exact:
        return exact[0]
    ensure_corpus_unpacked()
    suffixed = [
        path for path in sorted(CORPUS_DIR.rglob(f"*{name}.*"))
        if path.suffix.lower() in _DOCUMENT_SUFFIXES
    ]
    return suffixed[0] if suffixed else None


def _resolve_document(file: AssertionFile) -> Path | None:
    """Find the document an assertion file is about."""
    if (
        file.path is not None
        and Path(file.path).suffix.lower() in _DOCUMENT_SUFFIXES
        and Path(file.path).exists()
    ):
        return Path(file.path)
    for name in (file.corpus_name, file.document):
        if not name:
            continue
        for directory in (GOLD_DIR, CORPUS_DIR, CORPUS_DIR / "gold", CORPUS_DIR / "real"):
            for suffix in _DOCUMENT_SUFFIXES:
                candidate = directory / f"{name}{suffix}"
                if candidate.exists():
                    return candidate
        matches = [
            path for path in sorted(CORPUS_DIR.rglob(f"{name}.*"))
            if path.suffix.lower() in _DOCUMENT_SUFFIXES
        ]
        if matches:
            return matches[0]
        if file.corpus_name:
            # The harvest name is the authoritative one; fall back to the
            # readable name only when no harvest name was given, or a typo in
            # corpus_name would silently resolve to whatever `document` finds.
            ensure_corpus_unpacked()
            named = _resolve_named(name)
            if named is not None:
                return named
    return None


def run_coverage(
    labels_dir: Path = LABELS_DIR,
    include_mutations: bool = True,
    **pipeline_kwargs: Any,
) -> CoverageRun:
    """Run every labelled assertion, plus every applicable mutation."""
    register = load_families()
    return _coverage_of(
        load_assertions(labels_dir, register), register, include_mutations,
        **pipeline_kwargs,
    )


def _coverage_of(
    files: list[AssertionFile],
    register: Any,
    include_mutations: bool,
    **pipeline_kwargs: Any,
) -> CoverageRun:
    run = CoverageRun()
    for file in files:
        if file.is_chain:
            # A chain is extracted from the operative text -- the base with
            # every amendment folded in -- which is the only thing its
            # assertions can meaningfully be about.
            paths = [_resolve_named(name) for name in file.chain]
            missing = [n for n, p in zip(file.chain, paths) if p is None]
            if missing:
                run.notes.append(
                    f"{file.document}: chain incomplete, missing "
                    f"{', '.join(missing)}; its {len(file.assertions)} "
                    "assertion(s) did not run"
                )
                continue
            result = run_document_set([p for p in paths if p], **pipeline_kwargs)
            run.outcomes.extend(evaluate_file(file, result))
            run.documents += len(paths)
            run.record(result)
            continue

        path = _resolve_document(file)
        if path is None:
            run.notes.append(
                f"{file.document}: no document found; its "
                f"{len(file.assertions)} assertion(s) did not run"
            )
            continue
        result = run_pipeline(path, **pipeline_kwargs)
        run.outcomes.extend(evaluate_file(file, result))
        run.documents += 1
        run.record(result)

        if not include_mutations:
            continue
        if path.suffix.lower() not in (".html", ".htm", ".xhtml"):
            # The mutators rewrite HTML. An .mht archive is MIME-wrapped and
            # quoted-printable encoded, so editing it as text would corrupt it
            # silently and every mutant would measure the corruption.
            run.notes.append(
                f"{file.document}: {path.suffix} source, so no mutants were "
                "generated; its family coverage is Tier 2 only"
            )
            continue
        # Tier 3: mutate this document and check the injected defect is found
        # and the invariant rewrites do not move the answer.
        html = path.read_text(encoding="utf-8")
        for mutant in apply_all(html):
            mutant_path = (
                CORPUS_DIR / "mutants" / f"{file.document}__{mutant.mutation_id}.html"
            )
            mutant_path.parent.mkdir(parents=True, exist_ok=True)
            mutant_path.write_text(mutant.html, encoding="utf-8")
            mutant_result = run_pipeline(mutant_path, **pipeline_kwargs)
            for assertion in mutant.assertions:
                register.validate_member(assertion.family, assertion.member)
                run.outcomes.append(
                    evaluate_assertion(assertion, mutant_result, source="synthetic")
                )
            run.mutants += 1
            run.record(mutant_result)
    return run


def _coverage_of_one_file(
    task: tuple[str, bool, str, str | None]
) -> CoverageRun:
    """One label file, in a worker process, asking a scorer built there.

    A failure comes back rather than raising: the file is named, and the gate
    fails on it. A run that loses a file partway -- to a spent credit balance,
    say -- keeps what it measured and still cannot pass as complete.
    """
    path, include_mutations, jev, cache = task
    register = load_families()
    try:
        file = load_assertion_file(Path(path), register)
        backend = build_backend(jev, cache)
        return _coverage_of(
            [file], register, include_mutations, jev_backend=backend
        )
    except Exception as exc:  # noqa: BLE001 - named in the report, fails the gate
        run = CoverageRun()
        run.did_not_run.append(f"{Path(path).stem}: {type(exc).__name__}: {exc}")
        return run


def run_coverage_parallel(
    labels_dir: Path, include_mutations: bool, jev: str, workers: int,
    cache: Path | None = None,
) -> CoverageRun:
    """``run_coverage`` over several processes, one label file per task.

    Label files are independent -- each resolves its own documents and writes
    mutants under its own document's name -- and imap hands results back in
    the order they were submitted, so the merged run is the sequential one:
    offline, the report is identical line for line. What it buys back is
    request latency. A live gate is over 100,000 requests at about 0.2 s
    each: six hours in one process, a little over one in six.
    """
    ensure_corpus_unpacked()      # once, here, rather than raced by workers
    tasks = [
        (str(path), include_mutations, jev, str(cache) if cache else None)
        for path in sorted(labels_dir.glob("*.yaml"))
    ]
    merged = CoverageRun()
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        for run in pool.imap(_coverage_of_one_file, tasks):
            merged.absorb(run)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, default=LABELS_DIR)
    parser.add_argument("--no-mutations", action="store_true")
    parser.add_argument("--gate", action="store_true",
                        help="exit non-zero when a family is over budget")
    parser.add_argument("--jev", choices=("offline", "api"), default="offline",
                        help="the scorer the validators ask: the offline "
                             "stand-in, or live System One (needs JEV_API_KEY)")
    parser.add_argument("--workers", type=int, default=1,
                        help="run label files in this many processes; a live "
                             "gate is bound by request latency, not CPU")
    parser.add_argument("--jev-cache", type=Path, default=None,
                        help="keep live answers here and reuse them "
                             "(default for --jev api: .cache/jev-answers.sqlite)")
    parser.add_argument("--no-jev-cache", action="store_true",
                        help="ask the scorer every question, for a fresh draw")
    parser.add_argument("--outcomes", type=Path,
                        help="also write every assertion outcome as JSON lines, "
                             "for comparing two runs assertion by assertion")
    args = parser.parse_args(argv)

    cache = answer_cache_for(args.jev, args.jev_cache, args.no_jev_cache)
    if args.workers > 1:
        run = run_coverage_parallel(
            args.labels, not args.no_mutations, args.jev, args.workers, cache,
        )
    else:
        jev_backend = build_backend(args.jev, cache)
        run = run_coverage(
            args.labels, include_mutations=not args.no_mutations,
            jev_backend=jev_backend,
        )
    if args.outcomes:
        args.outcomes.write_text(
            "".join(outcome.model_dump_json() + "\n" for outcome in run.outcomes)
        )
    print(run.render())

    failures = run.gate_failures()
    if failures:
        print("\nCI GATE FAILURES")
        for failure in failures:
            print(f"  {failure}")
    return 1 if (args.gate and failures) else 0


if __name__ == "__main__":
    sys.exit(main())
