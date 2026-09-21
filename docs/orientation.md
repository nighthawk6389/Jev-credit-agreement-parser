# Orientation

Read this first. It answers the questions that the rest of the documentation
assumes you already have answers to: what this project is optimising for, why
there are two different kinds of checked-in file that both look like "the right
answer", and why so much of the work is organised around a table of eleven
families.

## What we are trying to do

Turn a credit agreement into a structured record of its economic terms —
borrower, facility size, maturity, margin, covenant levels — exportable as
FpML.

Extraction is not the hard part. **Knowing when you are wrong is the hard
part.** A pipeline that reports the wrong maturity date with a citation
attached is worse than one that reports nothing, because the citation is what
makes it believed. So the objective is not maximum recall. It is:

> maximise coverage subject to **zero confidently-asserted-and-wrong**.

Every structural decision follows from that sentence. Statuses exist so that
"absent from the document", "stated in a document we do not have", "this deal
kind has no such term" and "we could not settle it" are four different answers
rather than one empty box. Confidence is calibrated rather than asserted. The
review queue shows competing values instead of picking one. The FpML export
withholds an externally-defined term with its reason rather than emitting
`None`, because `None` in that model reads as a term that does not exist.

The report's headline is the silent-error rate, and it is currently **0 wrong
out of 75 propositions asserted confidently**. That number must never be quoted
without the other half: only 75 of 141 assertions are answered confidently at
all. Some of the zero is earned by declining to answer. That is the intended
trade, and it is still a trade.

## Labels and recordings

These are the two files that look alike and are not. Both are checked in, both
concern one document, both carry quotes. Confusing them makes every number in
the project meaningless.

|  | **Label** | **Recording** |
| --- | --- | --- |
| lives in | `credit_extract/eval/labels/*.yaml` | `credit_extract/extract/recordings/*.json` |
| is | the answer key | a submitted exam paper |
| says | "the correct value for this field is X" | "the reader said X, quoting this passage" |
| written to | score a run | **be** a run |
| touches the pipeline | never | yes — it is a backend |
| count today | 13 documents | 3 documents |

### A label is ground truth

A label file is one reading of an agreement written down in a form a test can
check: a field, an expected value, the family member it exercises, and the
passage the answer rests on. Labels sit outside the pipeline and grade it. The
contract for writing one is `docs/labelling_guide.md`; the loader refuses a
scaffold that still says `VERIFY`, so an unreviewed file cannot reach the test
suite by accident.

Labels are scoped to the 37 fields at criticality 4 or 5, out of 56 in the
registry. Depth on the fields that carry money beats breadth: a family needs
five assertions before its row stops being reported as undersampled.

### A recording is a model's answer, frozen

The tier meant to do the hard extraction has never run. There is no API key in
this environment, so every recall figure the project has ever produced measures
anchored patterns.

A recording closes that gap without pretending the key exists. A reader reads
the document and writes each extraction out — value, verbatim quote, confidence,
qualifiers — into a JSON file. `--backend recorded` then replays it through the
*identical* path the live backend uses: same quote location (a quote the chunk
does not contain is a fabrication and the value is binned), same reconciliation,
same validators, same statuses, same export. What lands in the report is what
the model tier would have produced, reproducibly and offline.

Three things a recording is not, because each is easy to assume:

- **Not a cache.** Nothing populates these files automatically.
- **Not a measurement of any model.** One reader read one document once.
- **Not ground truth.** The whole point is that it gets scored against labels
  written separately.

### The ordering rule

> A recording must be written **before** that document's labels, by a reader
> who has not seen them.

If the same reader writes both, the score measures self-consistency. It will
read close to 100%, which is worse than no number because it looks like one.
Every recording carries `recorded_before_labels`; `check()` fails a recording
that claims otherwise, and CI runs
`python -m credit_extract.extract.recorded --check`.

The rule governs `fields` and not `findings`, and that is the reason for the
rule rather than an exception to it: nothing scores findings, no assertion
tests one, and there is no number for them to inflate.

**Read this before quoting any replay figure.** In all three recordings that
exist today, the same reader wrote both sides. So "17 of 19" and "12 of 12" are
**not recall**. What they legitimately measure is narrower and was worth
measuring: *does a correct extraction, with a correct citation, survive the
pipeline into a correct record?* Before these replays ran, frequently it did
not.

## The trap families

`credit_extract/eval/trap_families.yaml` defines F01–F11. They are not
features and not bug categories. They are a taxonomy of **ways a reasonable
parser confidently gets a credit agreement wrong**:

- **F05 versioning** — read the conformed agreement on its own and report the
  revolver expiring two years early, because an amendment moved it.
- **F07 benchmark** — post-LIBOR waterfalls. File a base-rate spread of 0.550%
  as the Eurodollar margin, in a criticality-5 field, at 0.93 confidence, where
  the answer is 1.550%.
- **F04 absence** — the field is empty for three unrelated reasons: it is in
  the fee letter, this deal kind has no such term, or you missed it. One empty
  box, three different truths.
- **F08 units** — `"1.250% of the initial principal amount"` read as **$1.25**,
  against a real $7,875,000.

Each family carries a silent-error budget (zero, for all of them), a minimum
sample size below which its metrics carry no weight, and the invariants and
validators that defend it — so a family with no defence is visible as one.

**Why coverage is never a single number.** An aggregate that averages over
untested families is worse than no number, because it will be believed. The
per-family table is printed alongside every figure, real and synthetic columns
kept apart, with the blind-spot register underneath naming what this
environment could not test at all.

## How work actually gets made here

The family table is an **instrument, not a goal**. The loop is:

1. Find the worst row in the per-family table.
2. Pick a real held-out document that stresses it.
3. Write labels. Read the document; do not trust the scaffold.
4. Run it. See what breaks.

That loop — not code review — is what found each of the following. Every one
was written, documented, and had never executed against a real document:

- a boolean registry field that **crashed the pipeline outright**, because no
  run had ever populated one
- a covenant written `"60%"` against a ratio-typed field, coerced to `None`,
  then reported as "the extractor probably missed it" on a section the
  extractor had quoted correctly
- the review queue printing `closing_date = 2019-11-26` beside status
  `conflicted`, on a field with eleven candidates at equal weight
- inline section headings: **1 section detected out of 198**, and every
  cross-reference in the document consequently unresolvable
- a money parser reading `"1.250% of..."` as one dollar twenty-five
- the credit-spread-adjustment check firing on **40 correctly-drafted
  agreements** that genuinely have none
- **tier 4 of the escalation ladder**, described in the design since the
  beginning, supplied by no caller, never executed on any document
- **zero of the 40 verified FpML element names populated by any run**, because
  nothing ever constructed a `Facility`

Reading code does not find these. Pointing the pipeline at a document chosen
because a family row looked bad does.

## Reading the report

```bash
python -m credit_extract.eval.family_report          # the table and the register
python -m credit_extract.eval.family_report --gate   # the same, as a CI gate
```

Four things to look at, in order:

1. **The silent-error column.** Zero is the budget, per family. Anything else
   is a stop-the-line event.
2. **The `real` column, not the total.** The synthetic column measures the
   fixtures, which were written to be found.
3. **The split line.** `holdout` is the honest number; `contaminated` documents
   were read while the parser was being written. The split is frozen by
   SHA-256 of the document name and CI-verified against its own derivation, so
   it cannot drift to flatter a result.
4. **The blind-spot register.** It names what could not be tested and why.
   `partially_lifted` means some of it has since become testable; the entry
   says which part, and what is still unknown.

## What is blocked

One thing, and it has not moved: **there is no API key.**

The model tier is where the design puts everything hard — the rules tier is
deliberately not extended to cover the tail, because across 100 real agreements
"is hereby amended" takes 24 distinct phrasings, 13 of them occurring once. So
the tail is the model's job, and the model has never run. Recordings are a
workaround for that, not a substitute: they prove the machinery downstream of
extraction is sound, and they say nothing about whether a model produces those
extractions in the first place.

Both routes are wired and neither has been exercised:

```bash
python -m credit_extract.cli extract <doc> --backend anthropic   # ANTHROPIC_API_KEY
python -m credit_extract.cli extract <doc> --backend vercel      # AI_GATEWAY_API_KEY
```

They are the same tier over different transports — same prompt, same schema,
same failure handling; the base URL, the credential and the model-id prefix
live on the `Route` object. Everything decidable without a network is tested.
The first live call is still a first live call.

Thirteen documents carry labels and three of those carry a recording as well,
so a live pass has somewhere to land and something to be compared against the
moment a key exists.

## Where to go next

| you want to | read |
| --- | --- |
| write a label | `docs/labelling_guide.md` |
| know what the corpus showed | `docs/corpus_findings.md` |
| understand the tiers and the flow | `README.md` |
| know what is untested and why | the blind-spot register in the family report |
