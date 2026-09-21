"""The document-level split: which agreements may be tuned on, which may not.

Nothing in this repository implemented one until now, and every accuracy
number it has printed was measured on the same documents the thresholds were
fitted to. That is not a small caveat. ``--calibrate`` fits on 24 documents
and the family report then scores the same 24, so a threshold that memorised
a fixture and a threshold that generalises produce identical output.

The split is frozen **before the labels exist**, which is the only moment it
can be frozen honestly. Once someone has read an agreement and written down
what it says, they know whether putting it in the holdout would help or hurt,
and no amount of good faith makes that knowledge go away. So the assignment
is derived from the document's name by SHA-256 and checked in; ``--check``
recomputes it and fails if the file and the derivation disagree. A document
cannot be moved from holdout to fit after its labels turn out to be
inconvenient, because moving it is a visible edit that CI rejects.

Two exceptions are written down rather than hidden:

``contaminated``
    Documents already used while building the parser. They were read, their
    quirks shaped patterns, and one of them is a synthetic fixture written to
    be found. They are pinned to ``fit`` whatever the hash says, because a
    holdout containing them would measure nothing.

strata with fewer than two documents
    A stratum cannot contribute a holdout document and still be measurable on
    the fit side. There are none today; the rule is stated so that adding a
    one-document stratum later does not silently produce an empty fit side.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

import yaml

SPLIT_FILE = Path(__file__).parent / "split.yaml"
CORPUS_ZIP = Path(__file__).resolve().parents[2] / "corpus" / "edgar" / "edgar_corpus_raw.zip"

#: One document in three. Enough holdout to detect a difference that matters,
#: few enough that the fit side can still be fitted on.
HOLDOUT_IN = 3

#: Harvest strata, from the corpus coverage report.
STRATA: dict[str, str] = {
    "A": "Sponsor-backed direct lending",
    "B": "Broadly syndicated term loan B",
    "C": "ABL with borrowing base",
    "D": "Second lien",
    "E": "Recurring revenue / ARR",
    "F": "PIK toggle / holdco",
    "G": "Venture debt / loan & security agreement",
    "H": "Fund-level: subscription, NAV, CLO warehouse",
    "I": "Investment grade revolver",
    "J": "European LMA facilities agreement",
    "K": "DIP financing",
    "L": "Amendment chains",
    "M": "Amended and restated",
    "N": "Forbearance and waiver",
    "O": "SOFR with credit spread adjustment",
    "P": "Legacy LIBOR with benchmark waterfall",
    "Q": "52/53-week fiscal calendar",
    "R": "Multicurrency",
}


def _rank(name: str) -> str:
    """A stable per-document sort key that no one chose."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def corpus_documents() -> list[str]:
    """Every harvested agreement, by the stem the label files would use."""
    if not CORPUS_ZIP.exists():  # pragma: no cover - corpus is optional
        return []
    with zipfile.ZipFile(CORPUS_ZIP) as archive:
        return sorted(
            Path(member).stem
            for member in archive.namelist()
            if member.startswith("edgar_corpus/")
            and not member.startswith("edgar_corpus/text/")
            and not member.endswith(("/", ".txt", ".csv", ".json"))
        )


def stratum_of(document: str) -> str:
    head = document.split("_", 1)[0]
    return head if head in STRATA else "?"


def derive(documents: list[str], contaminated: set[str]) -> dict[str, str]:
    """Assign each document to fit or holdout, stratum by stratum.

    Deterministic in the document names alone, so the result can be
    recomputed by anyone and compared against the checked-in file.
    """
    by_stratum: dict[str, list[str]] = {}
    for document in documents:
        by_stratum.setdefault(stratum_of(document), []).append(document)

    assignment: dict[str, str] = {}
    for stratum, members in sorted(by_stratum.items()):
        eligible = sorted(
            (d for d in members if d not in contaminated), key=_rank
        )
        for document in members:
            assignment[document] = "fit"
        if len(members) < 2:
            # Too small to give one away; see the module docstring.
            continue
        quota = max(1, len(eligible) // HOLDOUT_IN)
        for document in eligible[:quota]:
            assignment[document] = "holdout"
    return dict(sorted(assignment.items()))


class Split:
    """The frozen assignment, as read from disk."""

    def __init__(self, raw: dict) -> None:
        self.version: int = raw.get("version", 1)
        self.contaminated: set[str] = set(raw.get("contaminated", {}))
        self.assignment: dict[str, str] = dict(raw.get("assignment", {}))

    def side_of(self, document: str) -> str:
        """``fit``, ``holdout``, or ``unassigned`` for a document we do not know.

        Label files name chain members by the tail of the filename, because
        the harvest prefixes every document with its stratum, filer and
        accession number. ``family_report._resolve_named`` matches on that
        tail and so does this, or a chain would resolve to a document on one
        side of the split and be scored against the other.

        Unassigned is not an error at read time: a label file may name a
        document outside the harvest, and refusing to run it would be worse
        than saying which side it is not on. ``check`` is where an unassigned
        labelled document becomes a failure.
        """
        if document in self.assignment:
            return self.assignment[document]
        if document in self.contaminated:
            # Named as already used for tuning but not in the harvest -- the
            # synthetic fixture and the corpus/real filings. Fit by definition.
            return "fit"
        matches = [
            side for name, side in self.assignment.items()
            if name.endswith(document)
        ]
        if len(matches) == 1:
            return matches[0]
        return "unassigned"

    def documents(self, side: str) -> list[str]:
        return sorted(d for d, s in self.assignment.items() if s == side)


def load_split(path: Path = SPLIT_FILE) -> Split:
    if not path.exists():  # pragma: no cover - packaging regression
        raise RuntimeError(f"{path} is missing; run `python -m credit_extract.eval.split --freeze`")
    return Split(yaml.safe_load(path.read_text()) or {})


def freeze(path: Path = SPLIT_FILE, contaminated: set[str] | None = None) -> dict:
    """Write the split. Run once; after that ``--check`` is the only honest use."""
    existing = load_split(path) if path.exists() else None
    pinned = contaminated if contaminated is not None else (
        existing.contaminated if existing else set()
    )
    documents = corpus_documents()
    assignment = derive(documents, pinned)
    payload = {
        "version": 1,
        "holdout_in": HOLDOUT_IN,
        "contaminated": sorted(pinned),
        "assignment": assignment,
    }
    path.write_text(
        "# Frozen document-level split. Generated by credit_extract.eval.split\n"
        "# and verified by --check: the assignment is a function of the\n"
        "# document names, so no document can change sides after its labels\n"
        "# are written. Edit `contaminated` only to add a document that has\n"
        "# already been used for tuning.\n"
        + yaml.safe_dump(payload, sort_keys=False, width=100)
    )
    return payload


def check(path: Path = SPLIT_FILE, labelled: list[str] | None = None) -> list[str]:
    """Return the reasons the split is not trustworthy. Empty means it is."""
    problems: list[str] = []
    split = load_split(path)
    documents = corpus_documents()

    if documents:
        expected = derive(documents, split.contaminated)
        moved = [
            f"{d}: file says {split.assignment.get(d, 'missing')}, "
            f"the derivation says {side}"
            for d, side in expected.items()
            if split.assignment.get(d) != side
        ]
        problems.extend(moved[:10])
        if len(moved) > 10:
            problems.append(f"...and {len(moved) - 10} more")

    for side in ("fit", "holdout"):
        if documents and not split.documents(side):
            problems.append(f"the {side} side is empty")

    for document in split.contaminated:
        if split.assignment.get(document) == "holdout":
            problems.append(f"{document} is contaminated but sits in the holdout")

    for document in labelled or []:
        if split.side_of(document) == "unassigned":
            problems.append(
                f"{document} carries labels but has no split assignment; add it "
                "to the corpus or name it in contaminated"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true",
                        help="the file still matches its derivation")
    action.add_argument("--freeze", action="store_true",
                        help="write the split; run once, before labelling")
    action.add_argument("--show", action="store_true", help="print the split")
    args = parser.parse_args(argv)

    if args.freeze:
        payload = freeze()
        print(f"froze {len(payload['assignment'])} documents to {SPLIT_FILE}")
        return 0

    split = load_split()
    if args.show:
        fit, held = split.documents("fit"), split.documents("holdout")
        print(f"{len(fit)} fit, {len(held)} holdout, "
              f"{len(split.contaminated)} contaminated\n")
        for code, label in STRATA.items():
            members = [d for d in split.assignment if stratum_of(d) == code]
            if not members:
                continue
            held_here = [d for d in members if split.side_of(d) == "holdout"]
            print(f"  {code} {label:46} {len(members):>2} docs, "
                  f"{len(held_here)} held out")
        return 0

    from .assertions import load_assertions
    from .families import load_families

    labelled: list[str] = []
    for file in load_assertions(Path(__file__).parent / "labels", load_families()):
        labelled.extend(file.chain or [file.document])

    problems = check(labelled=labelled)
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"\nsplit is not trustworthy ({len(problems)} problem(s))")
        return 1
    print(f"split holds: {len(split.documents('fit'))} fit, "
          f"{len(split.documents('holdout'))} holdout, "
          f"{len(split.contaminated)} contaminated, {len(labelled)} labelled")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
