"""A model tier whose answers are checked in rather than fetched.

There is no ``ANTHROPIC_API_KEY`` in this environment, so the tier meant to do
the hard extraction has never run and every recall number in this repository
measures anchored patterns instead. That is recorded in the blind-spot
register, and it has stopped being a caveat and started being the finding: 22
of 75 real assertions fail and every one of them is a field the deterministic
tier did not find.

This backend closes the gap without pretending the key exists. A model reads
the document and writes its extractions into a JSON file; the file is checked
in; this backend replays it through exactly the path ``AnthropicBackend`` uses
-- same ``Candidate`` construction, same rule that a quote which cannot be
located in the chunk is a fabrication and the value goes in the bin, same
reconciliation, same validators, same statuses. What lands in the report is
what the model tier would have produced, reproducibly and without a network.

Three things this is not, stated because each is easy to assume:

*It is not a cache.* Nothing populates these files automatically. A recording
is made deliberately, by a reader, and is a claim about what a model said.

*It is not a measurement of Claude.* One model read these documents once. The
recording carries who and when, and a comparison against it is a comparison
against that reading, not against a model's average behaviour.

*It is not ground truth.* The whole point is that it is scored against labels
written separately. Which is why the ordering below is load-bearing rather
than procedural.

    THE ORDERING. A recording must be made before the document's labels are
    written, and by a reader who has not seen them. If the same reader writes
    both, the score is a measure of self-consistency and will read close to
    100%, which is worse than no number at all because it looks like one. Each
    recording carries ``recorded_before_labels``; ``check()`` fails when a
    recording claims otherwise, and the honest thing to do with a recording
    made after the labels is to throw it away.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..models.core import CostLedger
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from .passes import Candidate, _parse_llm_payload

RECORDINGS = Path(__file__).parent / "recordings"


class RecordedField(BaseModel):
    """One extraction a model made, in the shape the API schema constrains."""

    field: str
    value: str | None = None
    #: Verbatim from the document. Checked against the text on replay, so a
    #: quote that drifted by a character is dropped exactly as a live one is.
    quote: str
    confidence: float = 0.75
    external_document: str | None = None
    #: The measurement convention a bare number does not carry: which EBITDA a
    #: percentage is measured against, per annum against per quarter, the scale
    #: a table header sets. Present because ``EXTRACTION_SCHEMA`` carries it --
    #: this model is the schema's shape, and a key it cannot hold is a key a
    #: recording cannot record.
    qualifiers: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


class Recording(BaseModel):
    """What one model read out of one document."""

    document: str
    #: The harvest name, so the frozen split can be resolved for this document.
    corpus_name: str | None = None
    model: str
    recorded_on: date
    #: False is a refusal to be scored. See the module docstring.
    recorded_before_labels: bool = False
    note: str = ""
    fields: list[RecordedField] = Field(default_factory=list)

    @property
    def by_field(self) -> dict[str, RecordedField]:
        return {item.field: item for item in self.fields}


def load_recording(path: Path) -> Recording:
    return Recording.model_validate(json.loads(path.read_text()))


def load_recordings(directory: Path = RECORDINGS) -> dict[str, Recording]:
    """Every checked-in recording, keyed by the document it reads."""
    if not directory.exists():
        return {}
    out: dict[str, Recording] = {}
    for path in sorted(directory.glob("*.json")):
        recording = load_recording(path)
        out[recording.document] = recording
    return out


class RecordedBackend:
    """Replays a recording as though the model had just answered.

    Chunk-aware in the one way that matters: a recorded field is offered to
    whichever chunk actually contains its quote. That is not a convenience --
    it is what keeps the independent-segmentation support in reconciliation
    meaningful, because a recording that answered every chunk identically
    would manufacture agreement out of nothing.
    """

    name = "recorded"

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self._offered: set[str] = set()
        self._claimed: set[str] = set()

    def with_temperature(self, temperature: float) -> "RecordedBackend":
        """A recording has no temperature; the same answers come back."""
        return self

    def extract(
        self,
        doc: NormalizedDocument,
        chunk: Chunk,
        specs: list[FieldSpec],
        context: str,
        pass_id: str,
    ) -> tuple[list[Candidate], CostLedger]:
        wanted = {spec.name for spec in specs}
        items = []
        for item in self.recording.fields:
            if item.field not in wanted:
                # Either the rules tier already settled it -- LayeredBackend
                # only forwards what is left -- or it is not a target field at
                # all, which ``check`` catches. Neither is drift.
                continue
            self._offered.add(item.field)
            if chunk.locate(doc, item.quote) is not None:
                items.append(item.model_dump())
        # Reconstruct the envelope the API would have returned for this chunk
        # and hand it to the same parser. Imitating that function would have
        # been shorter and would have drifted from it; this cannot.
        out = _parse_llm_payload(
            json.dumps({"fields": items}), doc, chunk, specs, pass_id
        )
        self._claimed.update(candidate.field for candidate in out)
        # A recording costs nothing to replay, and saying so keeps the cost
        # ledger honest: this run did not spend what a live model pass would.
        return out, CostLedger(deterministic_calls=1)

    def unplaced(self) -> list[str]:
        """Recorded fields that were asked for and whose quote no chunk held.

        A non-empty list means the recording and the document have drifted --
        the quote was mistyped, or the ingester changed underneath it. Either
        way those fields silently did not count, so the caller should say so
        rather than report a lower recall than the recording actually claims.
        """
        return sorted(self._offered - self._claimed)


def for_document(document: str, directory: Path = RECORDINGS) -> Recording | None:
    """The recording that reads ``document``, matched the way labels are.

    Harvest filenames carry stratum, filer and accession prefixes, so a
    recording names the document by its short name and is matched on the tail.
    """
    recordings = load_recordings(directory)
    if document in recordings:
        return recordings[document]
    stem = Path(document).stem
    for name, recording in recordings.items():
        if stem == name or stem.endswith(name) or name.endswith(stem):
            return recording
        if recording.corpus_name and Path(recording.corpus_name).stem == stem:
            return recording
    return None


def check(directory: Path = RECORDINGS) -> list[str]:
    """Reasons the recordings should not be scored. Empty means they can be."""
    problems: list[str] = []
    for name, recording in load_recordings(directory).items():
        if not recording.recorded_before_labels:
            problems.append(
                f"{name}: recorded_before_labels is false, so this reading may "
                "have been made with the answers in view; scoring it would "
                "measure self-consistency"
            )
        if not recording.model:
            problems.append(f"{name}: no model recorded, so the reading has no author")
        for item in recording.fields:
            if item.value is not None and not item.quote.strip():
                problems.append(
                    f"{name}: {item.field} carries a value with no quote, which "
                    "is the one thing no tier here is allowed to do"
                )
            if item.field not in FIELD_REGISTRY:
                # Otherwise it would surface as an unplaced quote, which reads
                # as drift in the document rather than a name that never was.
                problems.append(
                    f"{name}: {item.field} is not a field in the registry, so "
                    "nothing would ever ask for it"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    """``python -m credit_extract.extract.recorded --check``."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if any recording is not scorable")
    parser.add_argument("--dir", type=Path, default=RECORDINGS)
    args = parser.parse_args(argv)

    recordings = load_recordings(args.dir)
    for name, recording in sorted(recordings.items()):
        print(f"{name}: {len(recording.fields)} field(s), read {recording.recorded_on} "
              f"by {recording.model}")
    problems = check(args.dir)
    for problem in problems:
        print(f"  NOT SCORABLE  {problem}")
    if not recordings:
        print("no recordings checked in")
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
