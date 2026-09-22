# How to label an agreement

The pipeline's accuracy numbers are measured against label files, and a label
file is one person's reading of a credit agreement written down in a form a
test can check. This is the contract for writing one: which fields, what
counts as the right answer, and what to do at the places two careful readers
would otherwise disagree.

It exists because the next step is thirty-odd agreements and several hundred
propositions. Seven files written by one person against an undocumented loader
was workable. Thirty written by several people against nothing is how a corpus
ends up measuring the labellers rather than the parser.

## Before you start

```bash
python -m credit_extract.eval.split --check        # the split still holds
python -m credit_extract.eval.label <document> --out credit_extract/eval/labels/<name>.yaml
```

The scaffold fills in what the extractor currently says, with the text each
value was read from, and marks every line `VERIFY`. Your job is to read the
quote and either accept the value or replace it — not to find the clause from
scratch. The loader refuses a file that still contains `VERIFY`, so a scaffold
cannot reach the test suite by accident.

**Check which side of the split your document is on.** The scaffold prints it.
If it says `HOLDOUT`, the labels you write must not be used to tune anything:
no threshold refits, no new patterns, no prompt changes justified by what you
saw. Read it, label it, and leave it alone. That restriction is the only thing
that makes a holdout number mean more than a fit number.

## Which fields

The 37 fields in `FIELD_REGISTRY` at **criticality 4 or 5**. The scaffold
selects them; you do not need a separate list, and a separate list is
precisely what would drift.

Criticality 3 and below are worth labelling once the critical set is done on
enough documents to say something. Do not spread thin across all 56 fields on
five documents — a family needs five assertions before its row stops being
marked undersampled, and depth on the fields that carry money beats breadth.

## What counts as the right answer

### Label the operative value

The value as amended, after every amendment in the chain is folded in. Not the
value in the biggest document, and not the value in the most recent one.

This is the trap the corpus exists to hold. Wheels Up Amendment No. 4 is a
547,000-character blackline that looks like the whole agreement and says the
revolving availability period runs to September 20 2026. Amendment No. 5 is
eleven thousand characters and moves it to 2028. The operative date is 2028. A
label that says 2026 is a correct transcription of the wrong document.

For a chain, the label file lists its members under `chain:` and the harness
extracts from the folded text. Label what the folded text says.

### Quote something that exists

Every value needs evidence you could show someone: a quote that occurs in the
normalized text. The extractor is held to this — a model answer with no
locatable quote is discarded — and a label that cannot meet the same bar is
not a label, it is a recollection.

If you are working from the PDF or the filing page rather than the normalized
text, check the quote survives normalization before relying on it. Blackline
deletions are excised, so struck text is not there to quote.

### Cardinality: which tranche

The registry is flat and names its tranches: `initial_term_loan.commitment`,
`revolver.commitment`, `delayed_draw.commitment`. Real deals are not so tidy.

- **One facility of a kind** — label it, no ambiguity.
- **Several of a kind** (two revolvers, a USD and a multicurrency tranche;
  Term A and Term B) — label the **largest by commitment** and say in the note
  which one you chose and what the others were. Do not sum them; a sum is a
  number that appears nowhere in the agreement.
- **A kind the registry does not name** (a swingline, an FILO tranche) —
  do not force it into the nearest slot. Leave that field out and note the
  tranche in a `note` on a field you did label.

This rule is a stopgap and it is worth saying so: the flat registry cannot
represent a multi-tranche deal, `Facility` exists in the model and is never
constructed by the pipeline, and the right fix is a facility list rather than
a labelling convention. Until then, "largest by commitment, and say so" is at
least a rule two people can follow identically.

### Nulls: four different things

`expect:` on a `field_status` assertion, not a value, when the answer is that
there is no value. Choosing between them is the single most consequential
judgement in this guide, because the pipeline treats three of them as settled
answers and one as a question.

| status | when | example |
| --- | --- | --- |
| `confirmed` | there is a value — use `field_value` and give it, not this | a $400,000,000 revolver |
| `absent_from_document` | you read the agreement and the term is genuinely not there | no MFN sunset in a deal that has MFN protection |
| `external_reference` | the agreement points at a document that carries the term | "as set forth in the Fee Letter" |
| `not_applicable_to_archetype` | the term cannot apply to this kind of deal | a borrowing base on a cash-flow term loan |
| `needs_review` | **you could not decide** | you ran out of time; the drafting is genuinely ambiguous |

Two rules that follow from the table:

**Absence is a claim about the document, not about your search.** If you did
not find it, that is `needs_review`, not `absent_from_document`. The
difference is the whole point of family F04: a pipeline that reports "absent"
when it means "not found" is confidently wrong, and a label that does the same
teaches it to be.

**`external_reference` needs the pointer quoted.** If you cannot quote the
sentence that points elsewhere, you have an absence or a not-found, not an
external reference.

That rule was written for agreements and a second pass over the corpus found
it underdetermined for everything else. A financial-statement footnote
describing a facility does not *point* anywhere — it describes an instrument —
and the rule as stated would reject three sound labels while having already
admitted one wrong one. So it is four cases, not two, and the question to ask
first is **whether the instrument exists**:

| the document | example | status |
| --- | --- | --- |
| is an agreement and cites another document | `"as set forth in the Fee Letter"` | `external_reference` — quote the citation |
| is not an agreement, and names an instrument that demonstrably exists | Greenfire's `"$50 million revolving reserved-based credit facility"`, closed in Q4 2025; Rezolve's facility amended seven times; JRD Unico's `"2018 Private Placement Notes"` | `external_reference` — quote the identification |
| defines a term for an instrument nobody has entered into | Elmet's `"Borrowing Base"`, meaning the lending value under `"a credit facility with lenders"` that does not exist | `absent_from_document` |
| is a partial amendment, silent on the field, carrying neither a citation nor an identification | Comtech's Amendment No. 5, where `"Applicable Margin"` occurs zero times | `needs_review` |

The middle two are the ones that look alike and are not. Both are documents
that are not agreements, describing credit terms; the difference is that in one
the agreement exists somewhere and in the other it does not, and
`external_reference` asserts that it does. Getting that backwards invents an
agreement or denies one.

The fourth row is the corrected Comtech label and the reason this table exists.
A bare amendment is the case where *both* of the first two tests fail: nothing
is cited and nothing is identified, so nothing is settled. Reporting
`absent_from_document` there says the facility has no margin; reporting
`external_reference` asserts a pointer the document does not contain.

**Every assertion carries a note.** It is where the quotation goes, and an
assertion with no quotation cannot be checked by anyone but its author — which
is the failure mode this whole guide exists to prevent. `tests/
test_labelling_contract.py` enforces it.

### Amendments, specifically

- A term the amendment **restates** — label the restated value.
- A term the amendment **leaves alone** — label the base agreement's value;
  it is still operative.
- A term in a **blackline** — the struck text is deleted and is not in the
  normalized text. Label the bold-double-underlined value. If both survive in
  what you are reading, you are reading the raw filing, not the normalized
  text.
- A **conditional** change ("as extended in accordance with any Extension
  Amendment") — label the unconditional value and record the condition in the
  note. A conditional variant is a different assertion kind, not a different
  date.

## Which family and member

Every assertion binds to a family and a member in `trap_families.yaml`. The
binding is not decoration: it is what moves a family's coverage row, and an
assertion filed under the wrong family makes that family look tested when it
is not.

The scaffold suggests a family only where the field *is* the family — a floor
is the F07 floor question, a borrowing base is the F06 ABL question. Everywhere
else it writes `CHOOSE` and you pick. Ask: **what would go wrong if the
pipeline got this field wrong here, and which family is that failure?**

- The agreement is internally inconsistent, or the figure parses cleanly and
  is wrong → **F01_integrity**
- A later clause overrides an earlier one → **F02_precedence**
- The value lives in a document that is not here → **F03_external_reference**
- The term is genuinely absent → **F04_absence**
- The answer depends on which version you read → **F05_versioning**
- The deal type determines whether the field applies → **F06_structure**
- Benchmarks, spreads, floors, pricing grids → **F07_benchmark**
- Basis points against percent, thousands against millions, fiscal calendars
  → **F08_units**
- The answer requires resolving a chain of definitions → **F09_definitional_depth**
- The value steps down, springs, or converts on a trigger → **F10_conditionality**
- The value is only wrong because of how the page is laid out →
  **F11_layout**

When two fit, pick the one that would catch the error *first*.

## Double-labelling

Label at least a fifth of the batch twice, independently, and record every
disagreement rather than quietly reconciling them. A disagreement is evidence
about the field, not a mistake to be tidied: if two careful readers disagree
about what the commitment is, no extraction accuracy number for that field
means anything, and this guide is missing a rule.

Add the rule here when that happens. That is how this file is supposed to
grow.

## If you are also recording a model reading

A recording (`credit_extract/extract/recordings/<document>.json`) is what a
model read out of the document, replayed by `--backend recorded`. It is not
ground truth and it is not a cache: it is one reader's claim, scored against
labels written separately.

**Make the recording first, and do not look at the labels while you make it.**
That ordering is the only thing that makes the resulting number mean anything.
If the same reader writes both, the score is a measure of self-consistency and
will read close to 100%, which is worse than no number at all because it looks
like one. Each file carries `recorded_before_labels`; CI fails on a recording
that claims otherwise, and the honest thing to do with a recording made after
the labels is to throw it away.

Where you cannot separate the readers -- as with the one checked in -- say so
in the recording's `note` and do not quote the agreement as model recall. It
still measures something worth having: whether correct extractions with
correct citations survive to a correct record.

Every value needs a quote that is verbatim from the *normalized* text, not
from the raw filing. Ingestion excises struck blackline runs and rewrites
whitespace, and a quote that does not locate is dropped exactly as a live
model's fabrication would be.

## When you are done

```bash
python -m credit_extract.eval.families --check     # families and members exist
python -m credit_extract.eval.split --check        # the document is assigned
python -m credit_extract.extract.recorded --check  # any recording is scorable
python -m credit_extract.eval.family_report        # what your labels say
```

The report prints a `SPLIT` section separating held-out assertions from the
rest. Until a holdout document is labelled it says so in as many words, which
is the honest description of every accuracy figure this repository has
published so far.
