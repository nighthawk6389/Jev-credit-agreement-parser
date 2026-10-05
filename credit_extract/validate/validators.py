"""Validators A-F.

Ordered by what they are worth, not by the alphabet:

* **B, the orphan sweep**, is the highest-value mechanism here. Most pipelines
  are recall-limited: they ask "where is field X?" and if X lives somewhere
  unexpected it is silently missing. B inverts the question -- it asks every
  chunk whether it says something the extraction does not capture. Text that
  answers yes while contributing no fields is an orphan.
* **C, negative space**, is what turns a ``null`` into a finding. "The document
  is silent" and "we failed to find it" look identical in the output of every
  pipeline that reports ``null``, and telling them apart is most of the
  long-tail problem.
* **D, override detection**, catches provisions that are correct in isolation
  and wrong in context. Credit agreements are full of ``notwithstanding``.
* **E, external dependency**, forces ``external_reference`` where a magnitude
  lives in a document nobody has.
* **A, span support**, is the basic gate.
* **F, criticality**, is triage: it decides whose review time gets spent.

All arithmetic, date comparison and counting stay in Python. Jev is asked only
for judgements about what text says.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from pydantic import BaseModel

from ..extract.passes import GUARANTOR_NAMED, GUARANTOR_PLACEHOLDER
from ..ingest.normalize import NormalizedDocument
from ..ingest.segment import Chunk
from ..models.core import (
    CRITICALITY_RUBRIC, ConflictRecord, ExtractedField, OrphanChunk, Span,
    ValidationEvent,
)
from ..models.fpml_model import FIELD_REGISTRY, FieldSpec
from ..models.quantities import quantity_for
from .calibrate import Thresholds
from .omission import (
    BY_DESIGN_STATEMENT, OMITTED_STATEMENT, classify, document_omits_schedules,
)
from .jev import ChoiceQ, JevSession, Noul, Question, ScoreQ

#: Context either side of a cited span for validator A.
SPAN_CONTEXT_PAD = 500

#: Orphan sweep statements, asked of every chunk in one batched request.
ORPHAN_SIGNALS: dict[str, str] = {
    "payment_obligation":
        "This text creates a payment obligation not captured in the attached "
        "summary.",
    "restriction":
        "This text imposes a restriction on the borrower not captured in the "
        "summary.",
    "override":
        "This text modifies or overrides a term defined elsewhere in the "
        "agreement.",
    "threshold":
        "This text contains a numeric threshold, cap, or basket.",
    "schedule":
        "This text contains a date or schedule.",
}

#: A chunk must clear this on some signal to be considered an orphan at all.
ORPHAN_SIGNAL_FLOOR = 0.55

#: Chunks shorter than this are headings and cross-references, not provisions.
#: Sweeping them produces orphans that are real text but cannot carry a term.
ORPHAN_MIN_CHARS = 200


@dataclass
class ValidationContext:
    """Everything the validators read and write."""

    doc: NormalizedDocument
    fields: dict[str, ExtractedField]
    chunks: list[Chunk]
    session: JevSession
    thresholds: Thresholds
    #: chunk_id -> field names that chunk contributed. Drives the orphan sweep.
    chunk_contributions: dict[str, set[str]] = dc_field(default_factory=dict)
    specs: dict[str, FieldSpec] = dc_field(default_factory=lambda: FIELD_REGISTRY)
    #: Definition graph, when available. "Is this external by design?" is
    #: answered by the definition of the document being pointed at, which is
    #: nowhere near the citation.
    graph: Any = None
    orphans: list[OrphanChunk] = dc_field(default_factory=list)
    conflicts: list[ConflictRecord] = dc_field(default_factory=list)

    def threshold_for(self, name: str, validator: str = "A_span_support") -> float:
        spec = self.specs.get(name)
        return self.thresholds.for_class(
            spec.field_class if spec else "administrative", validator
        )


def _describe(field: ExtractedField, spec: FieldSpec) -> str:
    return describe_value(field.value, spec)


def describe_value(value: Any, spec: FieldSpec) -> str:
    """A value as validator A writes it into a question."""
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    elif value is None:
        text = "null"
    else:
        text = str(value)
    if spec.kind == "percent":
        text = f"{text}%"
    elif spec.kind == "money":
        text = f"${text}"
    return text


# ---------------------------------------------------------------------------
# Validator A -- span support
# ---------------------------------------------------------------------------


def _support_statement(field: ExtractedField, spec: FieldSpec) -> str:
    """What validator A asks of the cited text about one extracted value.

    A name is asked as a name. "The text supports a value of Administrative
    Agent for the Administrative Agent" is true of any text that uses the
    role, and a phrase lifted from beside the agent's name was confirmed as
    its name on those terms. Asked whether the value *is* the institution's
    legal name, a literal reader can say no.
    """
    return support_statement(field.value, spec)


def support_statement(value: Any, spec: FieldSpec) -> str:
    """Validator A's question about any value of a field, extracted or not.

    The threshold fit on real filings asks it of values that are wrong on
    purpose, and it must be A's question word for word. A copy of the field
    with ``value`` replaced is not a way to get there: a field's value lives
    on its primary variant, so the copy asks about the original value, and
    the fit counted the right answer's score as a wrong one's.
    """
    value = describe_value(value, spec)
    if spec.name.endswith(".legal_name"):
        return f'In this text, "{value}" is {spec.description}.'
    return f"The text supports a value of {value} for {spec.description}."


def validator_a_span_support(ctx: ValidationContext) -> int:
    """Does the cited text actually support the value?

    Fields whose citations land in the same neighbourhood share one state, so
    a clause carrying five extracted figures costs one request rather than
    five. State is the expensive part; questions are nearly free.
    """
    groups: dict[tuple[int, int], list[str]] = {}
    for name, field in ctx.fields.items():
        if field.status != "confirmed" or not field.spans:
            continue
        span = field.spans[0]
        lo, hi = span.expand(SPAN_CONTEXT_PAD, len(ctx.doc.text))
        placed = False
        for (glo, ghi), members in groups.items():
            if lo < ghi and glo < hi:
                merged = (min(glo, lo), max(ghi, hi))
                groups[merged] = groups.pop((glo, ghi)) + [name]
                placed = True
                break
        if not placed:
            groups[(lo, hi)] = [name]

    checked = 0
    for (lo, hi), names in groups.items():
        state = ctx.doc.slice(lo, hi)
        questions: list[Question] = []
        for name in names:
            questions.append(Noul(
                name=name,
                statement=_support_statement(ctx.fields[name], ctx.specs[name]),
            ))
        result = ctx.session.ask(state, questions, label="A_span_support")
        for name in names:
            decision = result.get(name)
            if decision is None:
                continue
            field = ctx.fields[name]
            threshold = ctx.threshold_for(name, "A_span_support")
            passed = decision.confidence >= threshold
            field.validation_confidence = decision.confidence
            field.validation_source = "A_span_support"
            field.record(ValidationEvent(
                validator="A_span_support",
                question=questions[names.index(name)].statement,
                jev_type="noul",
                probability=decision.confidence,
                threshold=threshold,
                passed=passed,
                backend=decision.backend,
            ))
            if not passed:
                field.status = "needs_review"
                field.notes = (
                    f"cited text supports the value at {decision.confidence:.2f}, "
                    f"below the {threshold:.2f} threshold for "
                    f"{ctx.specs[name].field_class}"
                )
            checked += 1
    return checked


def validator_a_premises(ctx: ValidationContext) -> int:
    """Confirm a computed value by the premises it was computed from.

    No sentence states a computed maturity, so validator A's question -- does
    the cited text support this date? -- has no literal answer. The live
    scorer gave every computed fund maturity 0.06-0.07, the right date and
    its near misses alike. What the text does state is each step: that the
    Closing Date is June 1, 2026; that the Final Maturity Date is the earliest
    of the four-year anniversary of the Closing Date and the events it lists.
    Those are literal, and the reader that cannot confirm the conclusion can
    confirm them. The arithmetic between them is Python's.

    Only a value A turned down is asked about, and only one whose extractor
    wrote down its premises (``qualifiers["premises"]``: each a definition's
    span and what the resolver read it to say). It is confirmed only if every
    premise clears A's threshold for its class. Otherwise it stays in review,
    and the note names the weakest premise.
    """
    asked = 0
    for name, field in ctx.fields.items():
        raw = field.qualifiers.get("premises")
        if not raw or field.value is None or field.status != "needs_review":
            continue
        refused = [e for e in field.trace if e.validator == "A_span_support"]
        if not refused or refused[-1].passed:
            continue
        threshold = ctx.threshold_for(name, "A_span_support")
        scored: list[tuple[float, str]] = []
        for index, (start, end, statement) in enumerate(json.loads(raw)):
            question = Noul(name=f"{name}#premise{index}", statement=statement)
            result = ctx.session.ask(ctx.doc.slice(start, end), [question],
                                     label="A_premises")
            decision = result.get(question.name)
            probability = decision.confidence if decision is not None else 0.0
            field.record(ValidationEvent(
                validator="A_premises", question=statement, jev_type="noul",
                probability=probability, threshold=threshold,
                passed=probability >= threshold,
                backend=decision.backend if decision is not None else "none",
            ))
            scored.append((probability, statement))
            asked += 1
        if not scored:
            continue
        weakest, statement = min(scored)
        if weakest >= threshold:
            field.status = "confirmed"
            field.validation_confidence = weakest
            field.validation_source = "A_premises"
            field.notes = (
                f"computed; each of the {len(scored)} premises it was computed "
                f"from is supported by its own definition (the weakest at "
                f"{weakest:.2f}, threshold {threshold:.2f})"
            )
        else:
            field.notes = (
                f"computed; the premise {statement!r} is supported at "
                f"{weakest:.2f}, below the {threshold:.2f} threshold for "
                f"{ctx.specs[name].field_class}"
            )
    return asked


# ---------------------------------------------------------------------------
# Validator B -- the orphan sweep
# ---------------------------------------------------------------------------


def validator_b_orphan_sweep(
    ctx: ValidationContext, prefetch_absence: bool = False
) -> list[OrphanChunk]:
    """Ask every chunk what it says that the extraction does not know about.

    Build this first and measure it: it is the only mechanism here that can
    find a field nobody thought to look for.

    With ``prefetch_absence``, each request also carries the questions
    validator C will ask of the same chunk. C sweeps these same chunks later,
    and sent separately, every chunk went twice; C's requests were nearly
    half of all a gate made. C then finds its answers already bought. A
    field filled by the re-read in between costs an unneeded question, not
    a request, and one C needs that was not carried is asked by C as before.
    """
    ahead = (
        _presence_questions(ctx, _awaiting_absence(ctx)) if prefetch_absence else []
    )
    orphans: list[OrphanChunk] = []
    for chunk in ctx.chunks:
        if len(chunk.text.strip()) < ORPHAN_MIN_CHARS:
            continue
        questions: list[Question] = [
            Noul(name=key, statement=statement, concept=key)
            for key, statement in ORPHAN_SIGNALS.items()
        ]
        result = ctx.session.ask(
            chunk.text, questions, label="B_orphan_sweep", prefetch=ahead
        )
        signals = {
            key: round(result[key].confidence, 4)
            for key in ORPHAN_SIGNALS
            if result.get(key) is not None
        }
        if not signals:
            continue
        contributed = ctx.chunk_contributions.get(chunk.chunk_id, set())
        top_signal = max(signals, key=signals.get)
        score = signals[top_signal]
        if contributed or score < ORPHAN_SIGNAL_FLOOR:
            continue
        orphans.append(OrphanChunk(
            chunk_id=chunk.chunk_id,
            span=chunk.span,
            text=chunk.text[:2000],
            signals=signals,
            top_signal=top_signal,
            score=score,
        ))
    orphans.sort(key=lambda o: -o.score)
    ctx.orphans = orphans
    return orphans


def rescue_orphans(
    ctx: ValidationContext,
    orphans: list[OrphanChunk],
    reread,
    graph=None,
) -> list[Any]:
    """Tier 4: targeted re-read of each orphan chunk.

    ``reread`` is a callable ``(chunk) -> list[Candidate]``. When none is
    available the orphans stay in the report as unreviewed rather than being
    quietly dropped, because an unexplained orphan is itself the finding.

    Returns the candidates, which the caller folds into the record. It used to
    return a count and drop them: a tier that re-reads a passage, finds the
    field the first pass missed, marks the orphan "rescued" and then throws
    the value away is doing the expensive half of the work and none of the
    useful half.
    """
    rescued: list[Any] = []
    by_id = {c.chunk_id: c for c in ctx.chunks}
    for orphan in orphans:
        chunk = by_id.get(orphan.chunk_id)
        if chunk is None:
            continue
        found = reread(chunk)
        if found:
            orphan.resolution = "rescued"
            orphan.rescued_fields = sorted({c.field for c in found})
            rescued.extend(found)
    return rescued


# ---------------------------------------------------------------------------
# Validator C -- negative-space confirmation
# ---------------------------------------------------------------------------

#: How far into a document its own title is expected to be.
_TITLE_WINDOW = 600

#: "AMENDMENT NO. 5 TO CREDIT AGREEMENT" and its variants. The gaps allow
#: periods, because the amendment's own number carries one and an earlier
#: version of this pattern excluded them and matched nothing at all.
_AMENDS_RE = re.compile(
    r"\bAMENDMENT\b[\s\S]{0,70}?\bTO\b[\s\S]{0,60}?\bAGREEMENT\b", re.I
)

_DEFINES_RE = re.compile(r"\"\s*(?:means|shall mean|:\s)", re.I)

#: A conformed amendment reproduces the agreement it amends and so defines
#: hundreds of terms; a bare one defines almost none. On the 100-document
#: harvest the split is not close -- every bare amendment has 11 or fewer
#: definitional operators and the smallest conformed one has 198 -- so the
#: threshold sits in a gap eighteen times its own width and no document in the
#: corpus lands near it.
_CARRIES_ITS_TERMS = 60


def _amends_an_agreement_it_does_not_carry(doc: Any) -> bool:
    """True for a bare amendment: it names an agreement and reproduces none of it.

    Absence means something different in such a document. Every term the
    amendment leaves alone is still operative and still has a value, in text
    the filing does not contain, so "not here" is not "not in the deal".
    """
    text = getattr(doc, "text", "") or ""
    if not _AMENDS_RE.search(text[:_TITLE_WINDOW]):
        return False
    return len(_DEFINES_RE.findall(text)) < _CARRIES_ITS_TERMS


def _awaiting_absence(ctx: ValidationContext) -> list[str]:
    """The fields validator C asks about: empty, unresolved, not external."""
    return [
        name for name, field in ctx.fields.items()
        if field.value is None
        and field.status in ("needs_review", "conflicted")
        and not field.external_document
    ]


def _presence_questions(ctx: ValidationContext, names: list[str]) -> list[Question]:
    """C's question for each field, exactly as C sends it.

    Validator B carries these on its own requests, so they must be the
    same bytes C would send: an answer is reused only for the identical
    question.
    """
    return [
        Noul(
            name=name,
            statement=ctx.specs[name].presence_statement.replace(
                "This agreement", "This text"
            ),
        )
        for name in names
    ]


def validator_c_negative_space(ctx: ValidationContext) -> dict[str, float]:
    """Affirmatively confirm absence, chunk by chunk, combined in Python.

    A claim about the whole agreement cannot fit in a 32k state, and Jev
    questions cannot chain, so the question is asked of every chunk and the
    conjunction is taken here: a field is absent only if no chunk carries it.
    Aggregation is arithmetic, which is exactly what stays out of the model.
    """
    pending = _awaiting_absence(ctx)
    if not pending:
        return {}
    questions = _presence_questions(ctx, pending)

    # Presence is asked; absence is computed. Each chunk is asked whether it
    # contains a provision addressing the field, and absence is one minus the
    # strongest evidence of presence anywhere. The question used to be the
    # negation -- "this text contains no provision addressing X" -- and a
    # literal reader agrees with that of any chunk that does not name X in
    # the question's own words, which is how a Term SOFR pricing grid came to
    # have no "Eurodollar Applicable Margin" in it.
    #
    # The initialiser is 0.0 presence -- absence 1.0, the most confident
    # possible claim that the field is absent. That is only safe if the loop
    # below actually runs: a field asked of no chunk keeps it and sails past
    # the threshold, and ``absent_from_document`` is a CONFIDENT status counted
    # in the silent-error budget. So the asked count is tracked and a field
    # nobody asked about is not entitled to an answer.
    max_presence: dict[str, float] = {name: 0.0 for name in pending}
    witness: dict[str, Span] = {}
    asked = 0
    for chunk in ctx.chunks:
        if not chunk.text.strip():
            continue
        asked += 1
        result = ctx.session.ask(chunk.text, questions, label="C_negative_space")
        for name in pending:
            decision = result.get(name)
            if decision is None:
                continue
            if decision.confidence > max_presence[name]:
                max_presence[name] = decision.confidence
                witness[name] = chunk.span
    min_absence = {
        name: round(1.0 - presence, 4) for name, presence in max_presence.items()
    }

    for name, probability in min_absence.items():
        field = ctx.fields[name]
        spec = ctx.specs[name]
        threshold = ctx.threshold_for(name, "C_negative_space")
        field.validation_source = "C_negative_space"
        field.record(ValidationEvent(
            validator="C_negative_space",
            question=spec.presence_statement,
            jev_type="noul",
            probability=probability,
            threshold=threshold,
            passed=probability >= threshold,
            backend=ctx.session.backend.name,
            notes="absence: one minus the strongest presence in any chunk",
        ))
        # A field can be empty because the document says nothing, or because
        # the extractor read something this field's type cannot hold. Only the
        # first is absence, and confirming the second would be a silent error
        # with a probability printed next to it.
        untypable = field.qualifiers.get("untypable_value")
        unsettled = field.qualifiers.get("unsettled_in_definition")
        per_tranche = field.qualifiers.get("priced_per_tranche")
        guarantors = field.qualifiers.get("guarantors_established")
        if not asked:
            # Nothing was swept, so 1.0 is the initialiser showing through
            # rather than evidence. "Absent from a document nobody read" is
            # the purest form of the failure this validator exists to prevent,
            # and it arrives wearing the maximum confidence the scale has.
            field.status = "needs_review"
            field.validation_confidence = None
            field.notes = (
                "absence not testable: no chunk carried any text to ask of, "
                f"so none of the {len(ctx.chunks)} chunk(s) in this run was "
                "swept. A field nobody looked for is not a field confirmed "
                "missing"
            )
        elif _amends_an_agreement_it_does_not_carry(ctx.doc):
            # The third reason a field can be empty, and the one the corpus
            # found last. Comtech's Amendment No. 5 amends sections of a credit
            # agreement the filing does not contain; "Applicable Margin" occurs
            # zero times in it. Absence confirmed across these chunks is a true
            # statement about these chunks and a false one about the facility,
            # which has a margin -- in a document that is somewhere else.
            #
            # external_reference is not available either, because the amendment
            # quotes no pointer: the labelling guide requires one, and Air T's
            # note is the contrast that has it ("the Applicable Margin (as
            # defined in the Credit Agreement)").
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                "this document amends an agreement it does not carry, so "
                f"absence is not confirmable from it; scored {probability:.2f} "
                "across its chunks and that is a fact about the amendment, "
                "not about the facility"
            )
        elif untypable:
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                f"stated as {untypable!r} and not absent: the extractor read a "
                "value this field's type cannot carry, so the record holds no "
                "number and the document holds one. Absence was scored at "
                f"{probability:.2f} and is not the question here"
            )
        elif per_tranche:
            # The fifth reason, and the only one where the record is empty
            # because the pipeline got it RIGHT. Iridium's floor is 0.75% on
            # the term loan and 0.00% on the revolver, so the deal-level field
            # declines and the tranche variants carry the values. Validator C
            # scored absence at 0.01 and wrote "the extractor probably missed
            # it -- escalate" over a field where the extractor read both values
            # and correctly refused to choose. A resolved field reported as a
            # miss sends a reviewer looking for something already found.
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                f"priced per tranche ({per_tranche}), so the deal-level field "
                "is empty by design and this is neither absence nor a miss: "
                "the values are on the tranches. Nothing to escalate unless "
                "the deal is expected to price them alike"
            )
        elif unsettled:
            # The fourth reason a field can be empty, and the one that looks
            # most like absence: the term is defined, the definition carries
            # several values, and no one of them is the answer. Essential
            # Properties' Applicable Margin is a table indexed by Credit Rating
            # Level. The document states this term emphatically; what it does
            # not state is a single number.
            field.status = "needs_review"
            field.validation_confidence = probability
            axis = field.qualifiers.get("indexed_by")
            changed = field.qualifiers.get("since_changed")
            field.notes = ((
                f"defined, and its definition states {unsettled} and then says "
                f"it {changed}, so this is not absence: the term is in the "
                "document and what is missing is the amount in force. Absence "
                f"scored {probability:.2f} and is the wrong question"
            ) if changed else (
                f"defined, and its definition carries several values "
                f"({unsettled}) rather than one"
                + (f", indexed by {axis}" if axis else "")
                + ", so this is not absence: the term is in the document and "
                  "what is missing is which of these is the value, or what "
                  f"reduction over them is. Absence scored {probability:.2f} "
                  "and is the wrong question"
            ))
        elif guarantors:
            # The sixth, and the one the out-of-sample set found. The
            # agreement defines, names or grants a guaranty -- validator E
            # marked it -- so guarantors exist and their names went unread.
            # Asked of each chunk whether it addresses "the legal name of each
            # Guarantor", a literal reader says no to a definition that names
            # nobody and to a signature block, which is not a provision:
            # Barings' guarantor was confirmed absent at 0.85 with its
            # signature in the document.
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                f"the agreement has guarantors ({guarantors[:160]}), so this "
                "is not absence: their names were not extracted. Absence "
                f"scored {probability:.2f} and is the wrong question -- "
                "escalate"
            )
        elif probability >= threshold:
            field.status = "absent_from_document"
            field.validation_confidence = probability
            field.notes = (
                f"affirmatively confirmed absent at {probability:.2f} across "
                f"{asked} swept chunk(s)"
            )
        else:
            field.status = "needs_review"
            field.validation_confidence = probability
            field.notes = (
                f"absence not confirmed ({probability:.2f} < {threshold:.2f}); "
                "some passage appears to address this, so the extractor "
                "probably missed it -- escalate"
            )
            if name in witness:
                # The witness is a chunk, not a quotation, so it goes in
                # review_hint rather than spans. See ExtractedField.review_hint.
                field.review_hint = witness[name]
                field.notes += (
                    f" (strongest signal in the chunk at offset "
                    f"{witness[name].start})"
                )
    return min_absence


# ---------------------------------------------------------------------------
# Validator D -- override and conflict detection
# ---------------------------------------------------------------------------


class OverridePair(BaseModel):
    """Two provisions that appear to govern the same quantity."""

    subject: str
    first: Span
    second: Span
    probability: float = 0.0
    overrides: bool = False
    note: str = ""


#: Language that signals one provision displacing another.
OVERRIDE_MARKERS = (
    "notwithstanding", "shall be deemed", "in lieu of", "shall not apply",
    "for the avoidance of doubt", "provided that", "except as",
)


def find_override_candidates(
    doc: NormalizedDocument, subject: str, reach: int = 700, pad: int = 400
) -> list[OverridePair]:
    """Find provisions *introduced* by override language that govern ``subject``.

    Anchored on the marker, not on the subject. A window drawn around every
    mention of "Consolidated EBITDA" catches any "provided that" within a few
    hundred characters and reports three findings where there is one, because
    the definition is full of provisos that modify other things. A
    ``notwithstanding`` governs what follows it, so the candidate provision
    starts at the marker and has to mention the subject downstream of it.
    """
    mentions = doc.find_all(subject.replace(" ", r"\s+"))
    if len(mentions) < 2:
        return []
    pattern = "|".join(re.escape(marker) for marker in OVERRIDE_MARKERS)
    pairs: list[OverridePair] = []
    claimed: list[Span] = []
    for match in re.finditer(pattern, doc.text, re.IGNORECASE):
        start = match.start()
        end = min(len(doc.text), start + reach)
        window = doc.text[start:end]
        if subject.lower() not in window.lower():
            continue
        second = doc.span(start, end)
        if any(second.jaccard(existing) > 0.5 for existing in claimed):
            continue
        earlier = [m for m in mentions if m.end <= start]
        if not earlier:
            continue
        base = earlier[-1]
        lo, hi = base.expand(pad, len(doc.text))
        pairs.append(OverridePair(
            subject=subject, first=doc.span(lo, min(hi, start)), second=second
        ))
        claimed.append(second)
    return pairs


def validator_d_overrides(
    ctx: ValidationContext, subjects: Iterable[str]
) -> list[OverridePair]:
    """Ask whether the second provision displaces the first."""
    out: list[OverridePair] = []
    for subject in subjects:
        for pair in find_override_candidates(ctx.doc, subject):
            state = (
                f"FIRST PROVISION\n{pair.first.text}\n\n"
                f"SECOND PROVISION\n{pair.second.text}"
            )
            result = ctx.session.ask(
                state,
                [Noul(
                    name="override",
                    statement=(
                        "The second provision overrides the first with respect "
                        f"to {subject}."
                    ),
                )],
                label="D_override",
            )
            decision = result.get("override")
            if decision is None:
                continue
            pair.probability = decision.confidence
            pair.overrides = decision.confidence >= ctx.thresholds.default
            if pair.overrides:
                pair.note = (
                    f"the second provision governs {subject}; any value "
                    "computed from the first alone is wrong in context"
                )
            out.append(pair)
    return out


# ---------------------------------------------------------------------------
# Validator E -- external dependency
# ---------------------------------------------------------------------------


_EXTERNAL_HINTS = (
    "Sponsor Model", "Disclosure Letter", "Fee Letter", "as separately agreed",
    "Schedule", "Exhibit", "Annex",
)

#: A defined Fee Letter, and the sentence that makes it govern the fees. Both
#: are required: an agreement can mention a fee letter in a boilerplate list of
#: Loan Documents without any fee actually living there.
_FEE_LETTER_DEFINED_RE = re.compile(
    r'"\s*Fee Letter\s*"\s*(?:means|shall mean)', re.IGNORECASE
)
_FEES_UNDER_FEE_LETTER_RE = re.compile(
    r"fees?\s+(?:payable|due and payable|owing)\s+(?:pursuant to|under)\s+"
    r"(?:a|the|any)\s+Fee Letter",
    re.IGNORECASE,
)
#: The stronger evidence, and the one the pair above misses. A fee rate whose
#: own definition says it is set out in the fee letter names the fee and points
#: at the document in the same sentence -- no inference from a defined term
#: somewhere and a payment clause somewhere else. Star Mountain's BDC warehouse
#: is the case: it defines "Fee Letters" in the plural, so the singular
#: "Fee Letter" means pattern never matched, while the rate itself reads
#: '"Non-Utilization Fee Rate" ... means the "Non-Utilization Fee Rate" as set
#: forth in such Lender's Fee Letter.' A rule bought narrow for precision
#: should still take evidence this direct.
_FEE_RATE_IN_FEE_LETTER_RE = re.compile(
    r'"\s*[A-Z][A-Za-z\- ]{0,40}Fee(?:\s+Rate)?\s*"[^.]{0,160}?'
    r"(?:means|shall mean)[^.]{0,200}?"
    r"(?:set forth|specified|set out|provided for)\s+in[^.]{0,60}?Fee Letter",
    re.IGNORECASE,
)


def fee_letter_governs_fees(doc: NormalizedDocument) -> str | None:
    """The quote establishing that this deal's fees live in a Fee Letter.

    Validator E can only speak about fields that already carry a span, because
    it reads the sentence the figure sits in. A fee fixed by a fee letter has
    no figure and therefore no sentence, so the validator built to say "this
    value is elsewhere" cannot fire on the case it was built for. Martin
    Marietta's Eighteenth Amendment names a Fee Letter twelve times, defines
    it, conditions closing on its delivery, and states no rate anywhere; the
    commitment fee came back as an ordinary missing field.

    The rule stays deliberately narrow because ``external_reference`` is a
    settled status and a wrong one is a silent error. A fee letter governs
    fees and nothing else, so this answers only for fee fields, and only when
    the agreement both defines the letter and says the fees are payable under
    it.
    """
    text = doc.text
    # Either the rate's own definition points at the letter, which needs no
    # corroboration because it names the fee and the document together...
    direct = _FEE_RATE_IN_FEE_LETTER_RE.search(text)
    if direct is not None:
        return " ".join(text[direct.start(): direct.end() + 20].split())
    # ...or the agreement defines the letter in one place and says the fees are
    # payable under it in another, which takes both halves to mean anything.
    if not _FEE_LETTER_DEFINED_RE.search(text):
        return None
    match = _FEES_UNDER_FEE_LETTER_RE.search(text)
    if match is None:
        return None
    return " ".join(text[match.start(): match.end() + 60].split())


#: A guarantor role the agreement defines. In quotes, because that is how an
#: agreement defines its terms -- and because the guarantor of a loan the
#: borrower holds as collateral ("the Obligor ... or any guarantor thereof") is
#: described in prose and never defined.
_GUARANTOR_DEFINED_RE = re.compile(
    r'"\s*((?:Subsidiary |Parent |Holdings |Company |U\.S\. |US )?Guarantors?)'
    r'\s*"\s*(?:means|shall mean|has the meaning|shall have the meaning)'
)
#: A guarantor defined where it is introduced, as parties are: "Ares Capital
#: CP Funding II, as the guarantor (the " Guarantor ")".
_GUARANTOR_DEFINED_INLINE_RE = re.compile(
    r'\(\s*(?:the|each,? a|collectively,? the)?\s*"\s*'
    r'(?:Subsidiary |Parent |Holdings )?Guarantors?\s*"\s*\)'
)
#: An article of this agreement that is itself a guaranty: IDEX's "ARTICLE
#: X. CONTINUING GUARANTY", under which the Company guarantees its
#: co-borrowers and which defines no Guarantor at all.
_GUARANTY_ARTICLE_RE = re.compile(
    r"\barticle\s+(?:[ivxlc]+|\d+)\s*[.:\-]?\s*"
    r"(?:the\s+|continuing\s+|company\s+|parent\s+|subsidiary\s+){0,2}"
    r"guarant(?:y|ee)\b",
    re.IGNORECASE,
)
#: A signature block headed for the guarantors, which names them without
#: repeating the role beside each name: "GUARANTORS: ACME HOLDINGS LLC By:".
_GUARANTOR_HEADING_RE = re.compile(
    r"\b(?:SUBSIDIARY |PARENT )?GUARANTORS?\s*:\s*"
    r"(?!By\b|Name\b|Title\b|\[)[A-Z][A-Za-z0-9&'.\-]+ [A-Z]"
)
_GUARANTOR_NAMED_RE = re.compile(GUARANTOR_NAMED)
_GUARANTOR_PLACEHOLDER_RE = re.compile(GUARANTOR_PLACEHOLDER)


def guarantors_established(doc: NormalizedDocument) -> str | None:
    """A quotation showing this agreement has guarantors, or None.

    Validator C asks of every chunk whether it addresses "the legal name of
    each Guarantor", and a literal reader answers no to a definition that
    names nobody -- '"Subsidiary Guarantor" means any Subsidiary that is a
    Guarantor under the Guarantee and Security Agreement' -- and to a
    signature block, which is not a provision. So C confirmed the field
    absent on agreements that plainly have guarantors: out of sample, on six
    BDC agreements that define them and on one that is signed by its
    guarantor.

    The evidence here is the text's own: a guarantor named in its role, a
    signature block headed for guarantors, a defined guarantor role, or an
    article that is a guaranty. None of it says *who* the guarantors are,
    and an earlier version that sent definitions pointing at a guarantee
    agreement to ``external_reference`` was wrong on all seven in-sample
    agreements it fired on: each names its guarantors somewhere this cannot
    see -- a signature block with no caption, a party the definitions make a
    guarantor (Holdings, the Parent, the Company), a borrower signing "as
    KBR, a Borrower and a Guarantor". So the evidence only stops C calling
    the field absent; it settles nothing.
    """
    text = doc.text
    for match in _GUARANTOR_NAMED_RE.finditer(text):
        if not _GUARANTOR_PLACEHOLDER_RE.search(match.group(1)):
            return " ".join(text[match.start(): match.end()].split())
    for pattern in (_GUARANTOR_HEADING_RE, _GUARANTOR_DEFINED_RE,
                    _GUARANTOR_DEFINED_INLINE_RE, _GUARANTY_ARTICLE_RE):
        match = pattern.search(text)
        if match is not None:
            return " ".join(
                text[max(0, match.start() - 40): match.end() + 120].split()
            )
    return None


#: A span that opens with a defined term and its verb is a whole definition.
_OPENS_A_DEFINITION = re.compile(
    r'\s*["“]\s*[A-Z][^"“”\n]{1,90}?\s*["”]\s*(?:means|shall mean|has the '
    r'meaning|shall have the meaning|:)'
)


def _sentence_window(doc: NormalizedDocument, span: Span, cap: int = 900) -> str:
    """The sentence the span sits in, not a fixed character window.

    Proximity is not dependence. A covenant grid that happens to sit within
    500 characters of "Indebtedness set forth on Schedule 6.01" does not get
    its magnitude from that schedule, and a fixed window cannot tell the
    difference. Sentence bounds can.

    A span that is a whole definition is its own window. Ares CP Funding's
    Applicable Spread ends "per annum ." with a space before the stop, so the
    next ". " was inside the following definition -- "Approval Notice ...
    attached hereto as Exhibit A" -- and the margin was asked whether Exhibit
    A sets it.
    """
    if _OPENS_A_DEFINITION.match(doc.text, span.start):
        return doc.text[span.start:span.end]
    lo = max(0, span.start - cap)
    hi = min(len(doc.text), span.end + cap)
    before = doc.text.rfind(". ", lo, span.start)
    after = doc.text.find(". ", span.end, hi)
    start = before + 2 if before != -1 else lo
    end = after + 1 if after != -1 else hi
    return doc.text[start:end]


def _with_definition(ctx: ValidationContext, document: str, state: str) -> str:
    """Append the named document's own definition to the state, when we have it.

    "the Sponsor Model" at the point of citation says nothing about whether it
    is obtainable. Its definition -- "is not a Loan Document and is not
    attached hereto" -- says everything, and sits thousands of characters away.
    """
    graph = ctx.graph
    if graph is None or not hasattr(graph, "resolve"):
        return state
    resolved = graph.resolve(document)
    node = graph.get(resolved) if resolved else None
    if node is None:
        return state
    return f"{state}\n\nDEFINITION OF {node.term}\n{node.body}"


def _classify_external(
    ctx: ValidationContext, state: str, result: Any, document_omits: bool
):
    return classify(
        state,
        omitted_probability=(
            result["omitted"].confidence if result.get("omitted") else None
        ),
        by_design_probability=(
            result["by_design"].confidence if result.get("by_design") else None
        ),
        document_omits=document_omits,
    )


def validator_e_external_dependency(ctx: ValidationContext) -> list[str]:
    """Force ``external_reference``, and say *which kind* of external it is.

    Two tiers. Python first: does the sentence carrying this figure cite
    something outside the agreement at all? Only then is Jev asked the question
    that costs money -- and it is asked with the candidate document named, so
    it is judging dependence rather than rediscovering a citation that a regex
    already found for free.

    The kind question rides on the same state, so asking whether the material
    is unobtainable in principle or merely omitted from the filing costs what
    asking the first question alone would. That distinction is the difference
    between a deal term and an artifact of the source, and a pipeline that
    reports only ``external_reference`` has thrown it away.
    """
    forced: list[str] = []
    document_omits = document_omits_schedules(ctx.doc)
    fee_letter_quote = fee_letter_governs_fees(ctx.doc)

    for name, field in ctx.fields.items():
        spec = ctx.specs.get(name)
        if spec is None:
            continue
        if not field.spans:
            if (
                name == "guarantor.legal_name"
                and field.value is None
                and field.status not in ("external_reference", "confirmed")
            ):
                # Not an external reference, whatever the definitions say:
                # see guarantors_established. Only a mark for validator C.
                established = guarantors_established(ctx.doc)
                if established:
                    field.qualifiers["guarantors_established"] = established
                continue
            if (
                fee_letter_quote
                and field.value is None
                and field.status not in ("external_reference", "confirmed")
                and "fee" in name
            ):
                field.external_document = "Fee Letter"
                field.external_kind = "by_design"
                field.status = "external_reference"
                field.validation_source = "E_external_dependency"
                field.notes = (
                    "no rate is stated in this agreement; the fees are payable "
                    "under the Fee Letter, which is never filed -- so this is "
                    "external by design and not a figure that was missed"
                )
                field.record(ValidationEvent(
                    validator="E_external_dependency",
                    question=(
                        "Are the fees for this facility fixed by a Fee Letter "
                        "rather than by this agreement?"
                    ),
                    jev_type="python",
                    result="by_design",
                    passed=True,
                    backend="deterministic",
                    notes=fee_letter_quote,
                ))
                forced.append(name)
            continue
        already_external = field.status == "external_reference"
        if already_external and field.external_kind is not None:
            continue                         # nothing left to decide
        if not already_external and spec.kind not in ("money", "percent", "ratio"):
            continue

        state = _sentence_window(ctx.doc, field.spans[0])
        document = field.external_document or next(
            (h for h in _EXTERNAL_HINTS if h.lower() in state.lower()), None
        )
        if document is None:
            continue                         # tier 1 settled it, for free
        state = _with_definition(ctx, document, state)
        # One claim, about this field, with nothing assumed. The old statement
        # never said which limit "this limit" was, and asserted as its premise
        # that the document sits outside the agreement -- false of a schedule,
        # and a literal reader took the premise as given. It fired on the
        # add-back cap the agreement states because the add-backs themselves
        # come from the Sponsor Model.
        statement = (
            f"The amount of {spec.noun} is set by the {document}, not "
            "stated in this text."
        )
        result = ctx.session.ask(
            state,
            [
                # An absence claim: the amount is not stated here. Declared,
                # because the words cannot say so -- the statement is built
                # from the field's own description, which is exactly the
                # vocabulary the cited sentence contains.
                Noul(name="external", statement=statement, polarity="absence"),
                Noul(name="omitted", statement=OMITTED_STATEMENT),
                Noul(name="by_design", statement=BY_DESIGN_STATEMENT),
            ],
            label="E_external_dependency",
        )
        decision = result.get("external")
        if decision is None:
            continue

        if already_external:
            # Externality is settled; only the kind is open.
            verdict = _classify_external(ctx, state, result, document_omits)
            field.external_kind = verdict.kind
            field.record(ValidationEvent(
                validator="E_external_kind", question=OMITTED_STATEMENT,
                jev_type="noul", result=verdict.kind,
                probability=verdict.omitted_probability, passed=verdict.certain,
                backend=ctx.session.backend.name,
                notes=f"{verdict.basis}: {verdict.evidence}",
            ))
            continue

        threshold = ctx.threshold_for(name, "E_external_dependency")
        field.record(ValidationEvent(
            validator="E_external_dependency",
            question=statement,
            jev_type="noul",
            probability=decision.confidence,
            threshold=threshold,
            passed=decision.confidence >= threshold,
            backend=decision.backend,
        ))
        if decision.confidence >= threshold:
            verdict = _classify_external(ctx, state, result, document_omits)
            field.value = None
            field.external_document = document
            field.external_kind = verdict.kind
            field.status = "external_reference"
            field.validation_confidence = decision.confidence
            field.validation_source = "E_external_dependency"
            field.record(ValidationEvent(
                validator="E_external_kind", question=OMITTED_STATEMENT,
                jev_type="noul", result=verdict.kind,
                probability=verdict.omitted_probability, passed=verdict.certain,
                backend=ctx.session.backend.name,
                notes=f"{verdict.basis}: {verdict.evidence}",
            ))
            field.notes = (
                f"magnitude is fixed by the {document}"
                + (
                    ", which the filer omitted from this filing -- the borrower "
                    "has it, so this is an artifact of the source and not a "
                    "deal term"
                    if verdict.kind == "omitted_from_filing"
                    else ", which is not part of this agreement; the figure "
                         "stated here is not the real cap"
                )
                + f" [{verdict.basis}: {verdict.evidence}]"
            )
            forced.append(name)
    return forced


# ---------------------------------------------------------------------------
# Validator F -- criticality scoring for triage
# ---------------------------------------------------------------------------

CRITICALITY_RUBRIC_TEXT: list[str] = [
    "cosmetic: wording or formatting with no effect on any obligation",
    "administrative: mechanics or notice provisions with no cash impact",
    "material: affects cash only in an edge case or on a contingency",
    "economic: affects cash in the ordinary course of the deal",
    "deal defining: pricing, maturity, principal, or the financial covenant",
]


def validator_f_criticality(ctx: ValidationContext) -> dict[str, int]:
    """Rank the review queue by what moves money."""
    scored: dict[str, int] = {}
    for name, field in ctx.fields.items():
        if field.status != "needs_review":
            continue
        spec = ctx.specs.get(name)
        if spec is None:
            continue
        span = field.spans[0] if field.spans else None
        state = (
            ctx.doc.slice(*span.expand(SPAN_CONTEXT_PAD, len(ctx.doc.text)))
            if span else spec.noun
        )
        result = ctx.session.ask(
            state,
            [ScoreQ(
                name="criticality",
                question=(
                    f"How economically material is {spec.noun} to this "
                    "credit agreement?"
                ),
                rubric=CRITICALITY_RUBRIC_TEXT,
            )],
            label="F_criticality",
        )
        decision = result.get("criticality")
        if decision is None or decision.score is None:
            continue
        # The registry's own criticality is a floor: a deal-defining field does
        # not become administrative because one clause reads quietly.
        field.criticality = max(field.criticality, decision.score)
        field.record(ValidationEvent(
            validator="F_criticality",
            question="economic materiality",
            jev_type="score",
            result=CRITICALITY_RUBRIC[field.criticality - 1],
            probability=None,
            backend=decision.backend,
        ))
        scored[name] = field.criticality
    return scored


# ---------------------------------------------------------------------------
# Conflict resolution via choice
# ---------------------------------------------------------------------------


#: The answer a conflict can always be given: that the candidates do not
#: settle it. Without it the choice has to pick one, and a closing date that is
#: an event rather than a date gets whichever candidate reads most like one.
NONE_OF_THESE = "none of these"


def _typed_like(kind: str, text: str) -> Any:
    """The typed value a candidate's ``str()`` came from, or None.

    Conflict candidates carry their values as strings. Only kinds whose string
    form inverts exactly come back; a number does not, because its unit is not
    in the string, and a candidate that cannot be restored is not applied.
    """
    try:
        if kind == "date":
            value: Any = date.fromisoformat(text)
        elif kind == "int":
            value = int(text)
        elif kind == "bool":
            value = {"True": True, "False": False}[text]
        elif kind == "text":
            value = text
        else:
            return None
    except (KeyError, ValueError):
        return None
    return value if str(value) == text else None


def resolve_conflicts(ctx: ValidationContext) -> list[ConflictRecord]:
    """Put each unresolved conflict to Jev as a choice over the candidates.

    The question asks which candidate *is* the field, not which one its own
    cited text supports -- every candidate was extracted from its citation, so
    each one is supported by it, and the old question could not tell them
    apart. And the answer that none of them is right is always on offer.
    """
    resolved: list[ConflictRecord] = []
    for record in ctx.conflicts:
        field = ctx.fields.get(record.field)
        if field is None or field.status != "conflicted":
            continue
        spec = ctx.specs.get(record.field)
        description = spec.noun if spec else record.field
        criteria: dict[str, str] = {}
        state_parts: list[str] = []
        for alternative in record.candidates:
            key = str(alternative["value"])
            span = alternative.get("span") or {}
            text = span.get("text", "")
            criteria[key] = f"the text states that {description} is {key}"
            if text:
                state_parts.append(f"[{key}] {text}")
        if len(criteria) < 2:
            continue
        criteria[NONE_OF_THESE] = (
            f"none of these values is {description}, or the text does not "
            "settle which one it is"
        )
        question = f"Which of these values is {description}, according to the text?"
        result = ctx.session.ask(
            "\n\n".join(state_parts),
            [ChoiceQ(name="pick", question=question, criteria=criteria)],
            label="conflict_choice",
        )
        decision = result.get("pick")
        if decision is None:
            continue
        record.jev_distribution = decision.distribution
        threshold = ctx.threshold_for(record.field, "conflict_choice")
        field.record(ValidationEvent(
            validator="conflict_choice",
            question=question,
            jev_type="choice",
            result=decision.choice,
            probability=decision.confidence,
            threshold=threshold,
            passed=decision.confidence >= threshold,
            backend=decision.backend,
        ))
        if decision.confidence < threshold or decision.choice == NONE_OF_THESE:
            field.notes = (
                "the candidates do not settle it"
                if decision.choice == NONE_OF_THESE else
                "passes disagreed and the distribution is flat "
                f"({decision.distribution})"
            ) + "; unresolved"
            resolved.append(record)
            continue
        winner = next(
            (a for a in record.candidates if str(a["value"]) == decision.choice),
            None,
        )
        record.resolved = True
        record.resolved_to = decision.choice
        field.validation_confidence = decision.confidence
        field.validation_source = "conflict_choice"
        if winner is None:
            field.status = "needs_review"
            field.notes = f"the choice named {decision.choice!r}, no candidate"
            continue
        if str(field.value) != decision.choice:
            # The model chose a candidate other than the one reconciliation
            # ranked first. Confirming the field without taking the chosen
            # value confirmed the value the model had just rejected -- five
            # administrative agents reached a report as "Loan Documents" and
            # "Restricted Subsidiary" that way.
            typed = _typed_like(spec.kind, decision.choice) if spec else None
            if typed is None:
                field.status = "needs_review"
                field.notes = (
                    f"the choice was {decision.choice} over {field.value}; not "
                    "applied, because the candidate's value cannot be restored "
                    "from its text"
                )
                continue
            field.value = typed
            if winner.get("span"):
                field.spans = [Span(**winner["span"])]
            field.quantity = quantity_for(typed, spec.kind)
            field.primary.pass_support = int(winner.get("support") or 0)
        field.status = "confirmed"
        field.notes = (
            f"conflict resolved to {decision.choice} at {decision.confidence:.2f}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Validator G -- amendment effect
# ---------------------------------------------------------------------------

REPLACES_STATEMENT = "This amendment replaces the quoted section in its entirety."
NUMERIC_CHANGE_STATEMENT = "This amendment changes a numeric term."
DEFERRED_EFFECT_STATEMENT = (
    "This amendment's changes take effect on a date later than its execution "
    "date."
)


class AmendmentVerdict(BaseModel):
    """What Jev thinks one amendment effect does."""

    document_id: str
    section: str
    parsed_kind: str
    replaces_entirely: float | None = None
    changes_a_number: float | None = None
    deferred_effect: float | None = None
    agrees_with_parser: bool = True
    note: str = ""


def validator_g_amendment_effect(
    ctx: ValidationContext,
    document_set: Any,
    threshold: float = 0.6,
) -> list[AmendmentVerdict]:
    """Check the parser's reading of each amendment against Jev's.

    The parser decides restatement-versus-patch from drafting formulae, which
    are conventional but not universal. Where the two disagree the amendment is
    flagged rather than applied on the parser's word: mis-reading a patch as a
    restatement replaces a whole section with a fragment, and mis-reading a
    restatement as a patch leaves the old section in force. Both produce a
    confident, wrong operative text.

    All three questions ride on one state, so checking an amendment costs one
    request.
    """
    verdicts: list[AmendmentVerdict] = []
    base = document_set.base.normalized
    for effect in document_set.effects():
        section_text = ""
        span = base.section_span(effect.target_section) if base else None
        if span is not None:
            section_text = span.text[:4000]
        state = (
            f"AMENDMENT TEXT\n{effect.span.text[:4000]}\n\n"
            f"SECTION {effect.target_section} AS IT CURRENTLY READS\n{section_text}"
        )
        result = ctx.session.ask(
            state,
            [
                Noul(name="replaces", statement=REPLACES_STATEMENT,
                     concept="amendment_restates"),
                Noul(name="numeric", statement=NUMERIC_CHANGE_STATEMENT,
                     concept="amendment_numeric"),
                Noul(name="deferred", statement=DEFERRED_EFFECT_STATEMENT,
                     concept="amendment_deferred"),
            ],
            label="G_amendment_effect",
        )
        replaces = result["replaces"].confidence if result.get("replaces") else None
        numeric = result["numeric"].confidence if result.get("numeric") else None
        deferred = result["deferred"].confidence if result.get("deferred") else None

        parser_says_restate = effect.is_restatement
        model_says_restate = replaces is not None and replaces >= threshold
        agrees = parser_says_restate == model_says_restate
        verdicts.append(AmendmentVerdict(
            document_id=effect.document_id,
            section=effect.target_section,
            parsed_kind=effect.kind,
            replaces_entirely=replaces,
            changes_a_number=numeric,
            deferred_effect=deferred,
            agrees_with_parser=agrees,
            note=(
                ""
                if agrees else
                f"parser read this as {effect.kind!r} but the text reads as "
                f"{'a full restatement' if model_says_restate else 'an in-place edit'}"
                f" ({replaces:.2f}); applying the wrong one silently changes the "
                "operative text"
            ),
        ))
    return verdicts
