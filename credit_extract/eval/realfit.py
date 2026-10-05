"""Fit validator A's thresholds on real filings, against wrong values it must reject.

    python -m credit_extract.eval.realfit --jev api --workers 4           # report
    python -m credit_extract.eval.realfit --jev api --workers 4 --write   # and refit

The harness fits thresholds on the synthetic corpus, where validator A never
met a wrong date or economic term: those classes had no labelled failure, so
their thresholds were the lowest score a right value happened to get (0.88
and 0.77 in live v9). This fits them on the real in-sample filings instead,
and gives the fit the failures it lacked.

Positives are the labelled values validator A asked about. Negatives come
three ways:

* **real**: a value A asked about that its label contradicts. They are rare;
  two in the first fit.
* **in-text**: the other dates, percentages or dollar amounts in the very
  text A was shown, nearest the right one first, up to three per value. They
  are the reader's plausible mistake: the right clause, the wrong figure.
* **off-text**: the right value moved to figures the text does not contain:
  a year either way, a quarter point or a whole point more, $25 million more
  or twice as much. A misparse or a slip in arithmetic produces those.

Each wrong value is asked in A's own words (``validators.support_statement``),
in A's own request, against the same state, so a wrong value's question
differs from the real question only in the value. The real questions come
from the answer cache, so a run pays only for the wrong values.

Each class is fitted on the fit side (fit and contaminated documents) three
times with ``calibrate.fit_threshold``: on the labelled values alone, then
with the off-text values added, then with the in-text ones added too. A new
threshold is adopted only where all three fits agree. The in-text near misses
are hypothetical errors at a prevalence nobody has measured. A threshold that
moves depending on whether they are counted rests on that unmeasured number,
so such a class keeps its threshold, and the report says why. The holdout
side is reported and never fitted.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import re
import sys
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..models.core import RESOLVED_NULL_STATES
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from ..validate import validators as V
from ..validate.calibrate import (
    DEFAULT_PRECISION_TARGETS, DEFAULT_THRESHOLD, ClassMetrics, Sample,
    evaluate, fit_threshold, load_thresholds, thresholds_path, wilson_lower_bound,
)
from ..validate.jev import (
    JevResult, Noul, Question, answer_cache_for, build_backend, split_batches,
)
from . import split as split_mod
from .assertions import AssertionFile, load_assertion_file, values_equal
from .families import load_families
from .family_report import (
    LABELS_DIR, _resolve_document, _resolve_named, ensure_corpus_unpacked,
)

CLASSES = ("dates", "economic_terms")
VALIDATOR = "A_span_support"
#: How validator A opens every question about a date or an economic term. A
#: legal name is asked differently, and is not in these classes.
A_OPENING = "The text supports a value of "
PER_FIELD = 3
#: A label status that makes any value a wrong one. Review is one: Accelevation's
#: joinder labels its revolving commitment needs_review because the amount in
#: force is printed nowhere, and A confirmed the superseded $50,000,000.
_NO_VALUE = RESOLVED_NULL_STATES
#: Each negative set, cumulatively: the labelled values alone, then with the
#: off-text values, then with the in-text near misses as well.
FITS = (("labelled", ()), ("+off-text", ("off_text",)),
        ("+in-text", ("off_text", "in_text", "other_facility")))
#: The reader's plausible mistakes: a wrong figure from the right text, and a
#: right date for the wrong kind of facility.
MISTAKES = ("in_text", "other_facility")
#: A term tranche's maturity, by the name its definition gives it.
_TERM_MATURITY = re.compile(r"\bTerm\b|\bTranche\b|Incremental|Delayed Draw")

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\s*,\s*(\d{4})")
_PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
_MONEY = re.compile(r"\$\s*(\d{1,3}(?:\s*,\s*\d{3})+(?:\.\d+)?)(?!\d|\s*,\s*\d)")


# ---------------------------------------------------------------------------
# Wrong values
# ---------------------------------------------------------------------------


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(re.sub(r"[\s,$%]", "", text))
    except InvalidOperation:
        return None


def figures(state: str, kind: str) -> list[tuple[int, Any]]:
    """Every value of one kind the text writes, with where it writes it."""
    found: list[tuple[int, Any]] = []
    if kind == "date":
        for m in _DATE.finditer(state):
            try:
                found.append((m.start(), date(
                    int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)))))
            except ValueError:
                continue
    elif kind in ("percent", "money"):
        for m in (_PERCENT if kind == "percent" else _MONEY).finditer(state):
            value = _number(m.group(1))
            if value is not None and (kind == "money" or value <= 100):
                found.append((m.start(), value))
    return found


def _typed(expected: Any, kind: str) -> Any:
    if kind == "date":
        try:
            return date.fromisoformat(str(expected)[:10])
        except ValueError:
            return None
    return _number(str(expected))


def in_text(state: str, spec: FieldSpec, expected: Any,
            limit: int = PER_FIELD) -> list[Any]:
    """The other figures of the field's kind in the text, nearest first.

    Nearest to where the text writes the right value, or to the middle of
    the text where it does not (a computed date)."""
    found = figures(state, spec.kind)
    at = [pos for pos, value in found if values_equal(expected, value)]
    centre = at[0] if at else len(state) // 2
    out: list[Any] = []
    for _, value in sorted(found, key=lambda item: abs(item[0] - centre)):
        if values_equal(expected, value) or any(values_equal(v, value) for v in out):
            continue
        out.append(value)
        if len(out) == limit:
            break
    return out


def off_text(state: str, spec: FieldSpec, expected: Any) -> list[Any]:
    """The right value moved to figures the text does not contain."""
    right = _typed(expected, spec.kind)
    if right is None:
        return []
    if spec.kind == "date":
        moved = []
        for years in (1, -1, 2):
            try:
                moved.append(right.replace(year=right.year + years))
            except ValueError:
                continue
    elif spec.kind == "percent":
        moved = [right + Decimal("0.25"), right + Decimal("1.00"),
                 right * 2 if right else Decimal("0.50")]
    elif spec.kind == "money":
        moved = [right + Decimal("25000000"), right * 2, right / 2]
    else:
        return []
    present = [value for _, value in figures(state, spec.kind)]
    return [m for m in moved
            if not values_equal(expected, m)
            and not any(values_equal(p, m) for p in present)]


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


class ProbingBackend:
    """A backend that also asks validator A about wrong values.

    It sits between the session and the real backend. When a request carries
    A's question about a labelled field, the wrong values ride on the same
    request, against the same state, and their answers are kept here. The
    session gets back exactly the answers it asked for, so the pipeline runs
    as it always does.
    """

    def __init__(self, inner: Any, labelled: dict[str, Any]) -> None:
        self.inner = inner
        self.name = inner.name
        self.labelled = labelled
        self.found: list[dict[str, Any]] = []

    def ask(self, state: str, questions: list[Question]) -> JevResult:
        extra: list[tuple[str, str, Any, Noul]] = []
        for question in questions:
            spec = FIELD_REGISTRY.get(question.name)
            if (question.name not in self.labelled or spec is None
                    or spec.field_class not in CLASSES
                    or not getattr(question, "statement", "").startswith(A_OPENING)):
                continue
            expected = self.labelled[question.name]
            for kind, values in (("in_text", in_text(state, spec, expected)),
                                 ("off_text", off_text(state, spec, expected))):
                for index, value in enumerate(values, 1):
                    statement = V.support_statement(value, spec)
                    if statement == question.statement:
                        continue
                    extra.append((question.name, kind, value, Noul(
                        name=f"{question.name}#{kind}{index}", statement=statement)))
        if not extra:
            return self.inner.ask(state, questions)
        merged = JevResult(backend=self.name)
        for batch in split_batches(state, [*questions, *(e[3] for e in extra)]):
            result = self.inner.ask(state, batch)
            merged.decisions.update(result.decisions)
            merged.input_tokens += result.input_tokens
            merged.cost_usd += result.cost_usd
            merged.questions_asked += result.questions_asked
        for name, kind, value, question in extra:
            decision = merged.decisions.pop(question.name, None)
            if decision is not None:
                self.found.append({
                    "field": name, "kind": kind, "value": str(value),
                    "probability": decision.confidence,
                    "statement": question.statement,
                })
        return merged


@dataclass
class Row:
    """One probability A gave, and whether the value it was asked about is right."""

    label: str
    side: str
    field: str
    field_class: str
    kind: str              # "labelled", "in_text", "off_text" or "other_facility"
    probability: float
    correct: bool
    value: str
    expected: str | None

    def sample(self) -> Sample:
        return Sample(field=self.field, field_class=self.field_class,
                      probability=self.probability, correct=self.correct,
                      document_id=self.label, validator=VALIDATOR)


def _labelled(file: AssertionFile) -> dict[str, list[Any]]:
    by_field: dict[str, list[Any]] = {}
    for assertion in file.assertions:
        spec = FIELD_REGISTRY.get(assertion.target or "")
        if spec is None or spec.field_class not in CLASSES:
            continue
        if assertion.kind == "field_value" or (
                assertion.kind == "field_status" and assertion.expect in _NO_VALUE):
            by_field.setdefault(assertion.target, []).append(assertion)
    return by_field


def _trace(field: Any) -> list[Any]:
    trace = getattr(field, "trace", None)
    if trace is None:
        trace = getattr(getattr(field, "primary", None), "trace", None)
    return list(trace or [])


def probe_file(path: Path, inner: Any) -> list[Row]:
    """Run one label file's document as the gate does, asking the wrong values too."""
    from ..pipeline import run_document_set, run_pipeline

    file = load_assertion_file(path, load_families())
    side = split_mod.load_split().side_of(file.corpus_name or file.document)
    labels = _labelled(file)
    rights = {
        name: assertions[0].expect for name, assertions in labels.items()
        if assertions[0].kind == "field_value" and assertions[0].expect is not None
    }
    backend = ProbingBackend(inner, rights)
    if file.is_chain:
        paths = [_resolve_named(name) for name in file.chain]
        if any(p is None for p in paths):
            return []
        result = run_document_set(paths, jev_backend=backend)
    else:
        document = _resolve_document(file)
        if document is None:
            return []
        result = run_pipeline(document, jev_backend=backend)

    rows: list[Row] = []
    for name, assertions in labels.items():
        field = result.fields.get(name)
        if field is None:
            continue
        events = [e for e in _trace(field) if e.validator == VALIDATOR]
        if not events:
            continue
        label = assertions[0]
        value = field.value
        correct = (label.kind == "field_value" and label.expect is not None
                   and values_equal(label.expect, value))
        rows.append(Row(path.stem, side, name, FIELD_REGISTRY[name].field_class,
                        "labelled", events[-1].probability, correct,
                        str(value), None if label.expect is None else str(label.expect)))
    for found in backend.found:
        rows.append(Row(path.stem, side, found["field"],
                        FIELD_REGISTRY[found["field"]].field_class, found["kind"],
                        found["probability"], False, found["value"],
                        str(rights[found["field"]])))
    if "revolver.maturity_date" in rights:
        rows += other_facility(path.stem, side, result, rights["revolver.maturity_date"], inner)
    return rows


def other_facility(label: str, side: str, result: Any, expected: Any,
                   inner: Any, limit: int = 2) -> list[Row]:
    """A right date for the wrong kind of facility, asked as the revolver's.

    Easterly Government Properties' term facility maturity was confirmed as a
    revolver's out of sample, at 0.82, between the old date threshold and
    the new; the stress set the refit was fitted on held no such date. Here
    each term tranche maturity a definition states, where it differs from the
    revolver's, is asked in A's words for the revolver, against its own
    definition with A's padding.
    """
    from ..extract import economics as E
    from ..graph.definitions import build_definition_graph
    from ..validate.jev import JevSession

    doc = getattr(result, "document", None)
    if doc is None:
        return []
    spec = FIELD_REGISTRY["revolver.maturity_date"]
    graph = build_definition_graph(doc)
    session = JevSession(inner)
    rows: list[Row] = []
    for name in sorted(graph.nodes):
        if "Maturity Date" not in name or not _TERM_MATURITY.search(name):
            continue
        node = graph.get(name)
        # Resolved as the revolver's maturity would be: a date written, or one
        # reached through references and arithmetic.
        stated = E._Chain(doc, graph).term(name, ())
        if stated is None or values_equal(expected, stated):
            continue
        lo, hi = node.span.expand(V.SPAN_CONTEXT_PAD, len(doc.text))
        question = Noul(name=f"{spec.name}#other_facility",
                        statement=V.support_statement(stated, spec))
        decision = session.ask(doc.slice(lo, hi), [question], label="realfit").get(
            question.name)
        if decision is not None:
            rows.append(Row(label, side, spec.name, spec.field_class, "other_facility",
                            decision.confidence, False, str(stated), str(expected)))
        if len(rows) == limit:
            break
    return rows


_INNER: Any = None


def _init(jev: str, cache: str | None) -> None:
    global _INNER
    _INNER = build_backend(jev, None if cache is None else Path(cache))


def _probe(path: str) -> list[dict[str, Any]]:
    return [row.__dict__ for row in probe_file(Path(path), _INNER)]


def collect(labels_dir: Path, jev: str, cache: Path | None,
            workers: int = 1) -> list[Row]:
    """Every row, for every in-sample label file, in label order."""
    ensure_corpus_unpacked()
    paths = [str(p) for p in sorted(labels_dir.glob("*.yaml"))]
    cache_arg = None if cache is None else str(cache)
    if workers > 1:
        with mp.get_context("fork").Pool(
                workers, initializer=_init, initargs=(jev, cache_arg),
                maxtasksperchild=4) as pool:
            chunks = pool.map(_probe, paths, chunksize=1)
    else:
        _init(jev, cache_arg)
        chunks = [_probe(p) for p in paths]
    return [Row(**row) for chunk in chunks for row in chunk]


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


@dataclass
class ClassFit:
    field_class: str
    current: float
    #: The threshold each of the three fits chose, in FITS order.
    fitted: dict[str, float] = dc_field(default_factory=dict)
    adopted: float | None = None
    reason: str = ""
    metrics: ClassMetrics | None = None
    #: Where the fits disagree: the labelled values A was asked about and how
    #: many were wrong, which bound the rate wrong values reach A at.
    prevalence: tuple[int, int] | None = None


def _side(rows: list[Row], side: str) -> list[Row]:
    wanted = ("fit", "contaminated") if side == "fit" else ("holdout",)
    return [r for r in rows if r.side in wanted]


def _negatives(rows: list[Row], kinds: tuple[str, ...]) -> list[Sample]:
    return [r.sample() for r in rows
            if r.kind == "labelled" or r.kind in kinds]


def wilson_upper_bound(successes: int, n: int) -> float:
    """95% upper bound on a proportion."""
    return 1.0 - wilson_lower_bound(n - successes, n) if n else 1.0


def weighted_threshold(train: list[Row], target: float,
                       rate: float) -> tuple[float | None, float]:
    """Lowest threshold whose expected precision clears the target, when the
    reader hands A a plausible mistake at ``rate`` and a right value otherwise.

    The fits weigh every near miss as one real candidate, which is the reader
    erring on every value it reads twice over; the labels say how often it
    actually errs. A threshold must also turn down every value the text does
    not contain: those are what A reliably catches, and a fit that ignored
    them could settle below them. Returns (threshold or None, the best
    expected precision).
    """
    rights = [r.probability for r in train if r.kind == "labelled" and r.correct]
    # The reader's own wrong values are mistakes too, and can score as high
    # as any asked on purpose: nVent's margin, read from the right grid at
    # the wrong level, scored 0.84.
    wrongs = [r.probability for r in train
              if r.kind in MISTAKES or (r.kind == "labelled" and not r.correct)]
    if not rights or not wrongs:
        return None, 0.0
    floor = max((r.probability for r in train if r.kind == "off_text"), default=-1.0)
    best = 0.0
    for at in sorted({round(p, 4) for p in rights + wrongs if p > floor}):
        q_right = sum(p >= at for p in rights) / len(rights)
        q_wrong = sum(p >= at for p in wrongs) / len(wrongs)
        if not q_right:
            continue
        precision = ((1 - rate) * q_right) / ((1 - rate) * q_right + rate * q_wrong)
        best = max(best, precision)
        if precision + 1e-9 >= target:
            return at, precision
    return None, best


def fit_class(rows: list[Row], field_class: str, current: float) -> ClassFit:
    """Fit one class three ways on the fit side; adopt only an agreed threshold."""
    target = DEFAULT_PRECISION_TARGETS[field_class]
    mine = [r for r in rows if r.field_class == field_class]
    train = _side(mine, "fit")
    out = ClassFit(field_class, current)
    for name, kinds in FITS:
        samples = _negatives(train, kinds)
        if not samples:
            continue
        threshold, _ = fit_threshold(samples, target)
        if all(s.correct for s in samples) and threshold < DEFAULT_THRESHOLD:
            # No failure to say where errors begin: the default is the floor,
            # as calibrate.fit has it.
            threshold = DEFAULT_THRESHOLD
        out.fitted[name] = round(threshold, 4)
    if len(out.fitted) < len(FITS):
        out.reason = "not every fit had data"
    elif len(set(out.fitted.values())) > 1:
        labelled = [r for r in train if r.kind == "labelled"]
        wrong = sum(1 for r in labelled if not r.correct)
        rate = wilson_upper_bound(wrong, len(labelled))
        out.prevalence = (wrong, len(labelled))
        at, precision = weighted_threshold(train, target, rate)
        if at is not None:
            out.adopted = round(at, 4)
            out.reason = (
                f"the fits disagree; the reader gave A a wrong value {wrong} "
                f"times in {len(labelled)}, at most {rate:.1%} at 95%, and at "
                f"that rate {at:.2f} is the lowest threshold whose expected "
                f"precision clears {target} ({precision:.4f})")
        else:
            out.reason = (
                f"the fits disagree, and at the rate the reader gave A a wrong "
                f"value ({wrong} in {len(labelled)}, at most {rate:.1%} at 95%) "
                f"no threshold's expected precision clears {target} "
                f"(best {precision:.4f})")
    else:
        out.adopted = next(iter(out.fitted.values()))
        out.reason = "all three fits agree"
    threshold = out.adopted if out.adopted is not None else current
    held = _negatives(_side(mine, "holdout"), ("off_text",) + MISTAKES)
    scored = [s for s in held if s.probability >= threshold]
    wrong = sum(1 for s in scored if not s.correct)
    precision, recall, coverage = evaluate(held, threshold)
    out.metrics = ClassMetrics(
        field_class=field_class, validator=VALIDATOR, threshold=threshold,
        target_precision=target, precision=round(precision, 4),
        precision_lower_bound=round(
            wilson_lower_bound(len(scored) - wrong, len(scored)), 4),
        certified=False, recall=round(recall, 4), coverage=round(coverage, 4),
        silent_error_rate=round(wrong / len(scored), 4) if scored else 0.0,
        n=len(_negatives(train, ("off_text",) + MISTAKES)),
        n_accepted=len(scored), n_holdout=len(held),
        n_incorrect=sum(1 for s in _negatives(train, ("off_text",) + MISTAKES)
                        if not s.correct),
    )
    return out


def curve(rows: list[Row], field_class: str, at: float) -> dict[str, str]:
    """Share of each kind of row A clears at one threshold."""
    out = {}
    for kind, correct in (("right", True), ("labelled wrong", False),
                          ("in_text", False), ("other_facility", False),
                          ("off_text", False)):
        group = [r for r in rows if r.field_class == field_class
                 and (r.kind == "labelled" if kind in ("right", "labelled wrong")
                      else r.kind == kind)
                 and r.correct == correct]
        cleared = sum(1 for r in group if r.probability >= at)
        out[kind] = f"{cleared}/{len(group)}"
    return out


def held_out_reading(rows: list[Row], field_class: str, at: float) -> str:
    """What the threshold does to the reader's own held-out values.

    The held-out metrics count every near miss asked on purpose as if the
    reader had produced it, which is the stress the fit is built on and not
    a rate anything reaches a user at. This says what the threshold does to
    the values the reader actually gave.
    """
    own = [r for r in _side(rows, "holdout")
           if r.field_class == field_class and r.kind == "labelled"]
    right = [r for r in own if r.correct]
    wrong = [r for r in own if not r.correct]
    return (f"the reader's own held-out values: {sum(r.probability >= at for r in right)} "
            f"of {len(right)} right and {sum(r.probability >= at for r in wrong)} of "
            f"{len(wrong)} wrong cleared")


def render(fits: list[ClassFit], rows: list[Row]) -> str:
    lines = []
    counts = {}
    for row in rows:
        key = (row.field_class, row.kind, row.correct)
        counts[key] = counts.get(key, 0) + 1
    for fit in fits:
        lines.append(f"{VALIDATOR}/{fit.field_class}: now {fit.current:.2f}; fitted "
                     + ", ".join(f"{k} {v:.2f}" for k, v in fit.fitted.items()))
        lines.append(f"  {'adopt ' + format(fit.adopted, '.2f') if fit.adopted is not None else 'keep'}"
                     f": {fit.reason}")
        for at in sorted({fit.current, *(fit.fitted.values())}):
            lines.append(f"  at {at:.2f} on both sides: "
                         + ", ".join(f"{k} {v}" for k, v in curve(rows, fit.field_class, at).items()))
        m = fit.metrics
        lines.append(f"  holdout at {m.threshold:.2f}, near misses counted as answers: "
                     f"{m.n_accepted} of {m.n_holdout} cleared, silent error rate "
                     f"{m.silent_error_rate:.3f}; "
                     + held_out_reading(rows, fit.field_class, m.threshold))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jev", choices=("offline", "api"), default="offline")
    parser.add_argument("--labels", type=Path, default=LABELS_DIR)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--jev-cache", type=Path, default=None)
    parser.add_argument("--rows", type=Path, help="also write every row as JSON")
    parser.add_argument("--write", action="store_true",
                        help="write the adopted thresholds into the scorer's file")
    parser.add_argument("--version", default=None,
                        help="version for the written file (default: current + 1)")
    args = parser.parse_args(argv)

    cache = answer_cache_for(args.jev, args.jev_cache, False)
    backend_name = build_backend(args.jev, None).name
    thresholds = load_thresholds(backend=backend_name)
    rows = collect(args.labels, args.jev, cache, args.workers)
    if args.rows:
        args.rows.write_text(json.dumps([r.__dict__ for r in rows], indent=1) + "\n")
    fits = [fit_class(rows, c, thresholds.for_class(c, VALIDATOR)) for c in CLASSES]
    print(render(fits, rows))
    adopted = [f for f in fits if f.adopted is not None and f.adopted != f.current]
    if args.write and adopted:
        for fit in adopted:
            key = f"{VALIDATOR}/{fit.field_class}"
            thresholds.per_class[key] = fit.adopted
            thresholds.metrics[key] = fit.metrics
        thresholds.version = args.version or str(int(thresholds.version) + 1)
        thresholds.fitted_on = (
            f"{thresholds.fitted_on}; "
            + ", ".join(f"{VALIDATOR}/{f.field_class}" for f in adopted)
            + " refitted on the real in-sample filings by credit_extract.eval.realfit"
        )
        thresholds.notes = (
            f"{thresholds.notes} v{thresholds.version}: "
            + "; ".join(
                f"{VALIDATOR}/{f.field_class} {f.current:.2f} -> {f.adopted:.2f}, "
                f"{f.reason}, fitted on {f.metrics.n} fit-side samples of which "
                f"{f.metrics.n_incorrect} wrong; held out, with the near misses "
                f"counted as answers, {f.metrics.n_accepted} of "
                f"{f.metrics.n_holdout} cleared at a silent error rate of "
                f"{f.metrics.silent_error_rate:.3f}, and "
                + held_out_reading(rows, f.field_class, f.adopted)
                for f in adopted
            )
            + ". Kept: " + "; ".join(
                f"{VALIDATOR}/{f.field_class} at {f.current:.2f}, {f.reason}"
                for f in fits if f not in adopted
            ) + "."
        ).strip()
        path = thresholds.save(thresholds_path(backend_name))
        print(f"\nwrote v{thresholds.version} to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
