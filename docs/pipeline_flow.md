# How a document becomes a record

What actually happens between a filing on disk and a `CreditAgreement`, in
order, with the file and the reason for each step. Written because the flow is
not obvious from any one module: the pipeline builds **two different graphs**,
segments the document **three ways**, runs the extractor **three times**, and
the word *facility* means two different things depending on which file you are
reading.

Everything below is a statement about the code as it stands. Where a number
appears it was measured on Essential Properties Realty Trust's Eighth
Amendment — 519,130 characters, 365 defined terms — which is the largest
document in the corpus that also has labels and a checked-in model reading.

---

## The shape of it

```
 1  ingest(path)                   ingest/normalize.py    → NormalizedDocument
 2  build_definition_graph(doc)    graph/definitions.py   → DefinitionGraph      ★
 3  segment_all(doc, graph)        ingest/segment.py      → three views          ★ use 1
 4  run_passes(..., graph=graph)   extract/ladder.py      → candidates           ★ use 2
 5  reconcile(candidates)          extract/reconcile.py   → dict[str, ExtractedField]
 6  build_precedence_graph(doc)    graph/precedence.py    → a DIFFERENT graph
 7  validators A–G                 validate/validators.py                        ★ use 3
 8  invariants                     validate/invariants.py
 9  orphan sweep → tier 4          pipeline.py                                   ★ use 4
10  archetype dispatch             models/archetypes.py
11  build_agreement                models/assemble.py     → CreditAgreement
12  report                         pipeline.py
```

`★` marks the five places the definition graph is read. They are the least
obvious part of the flow and they are collected in their own section below.

---

## 1. Ingest

`ingest/normalize.py` turns HTML, `.mht` or text into a `NormalizedDocument`:
**one character space** that everything downstream indexes into. Every `Span`
in the system is a half-open `[start, end)` pair of offsets into that string,
which is what makes a citation checkable — a quote either resolves to the text
at those offsets or it does not.

Also recovered here: page breaks, section markers, and redline ranges. That
last one matters more than it sounds. A blackline amendment marks deletions
with strike-through and insertions with a bold double underline; flatten it to
text and both survive, adjacent, so a real filing reads

> Up to U.S. $ 2,150,000,000 2,250,000,000

and a first-match parser reports the **deleted** figure while quoting the
document accurately. That is family F11.

## 2. The definition graph is built

`graph/definitions.py` reads the definitions article and builds a
`DefinitionGraph`:

- **nodes** — one per defined term, carrying the span of its definition, its
  body, and every site in the document where the term is used
- **edges** — `uses` / `used_by`: which defined terms appear inside which other
  definitions

The useful operations are `resolve(term)` (case-insensitive lookup, so callers
need not match the drafting), `closure(term)` (every term reachable from it,
breadth-first and cycle-safe, in dependency order), and `context_for(term)`
(that closure rendered as text, capped).

Cycle-safety is not defensive programming. Cumberland Farms defines
`"Applicable ECF Percentage"` in terms of itself — each limb assumes its own
percentage in order to test whether that limb applies — and that is *correct
drafting*, standard in leveraged loans. A graph that refuses cycles reports a
criticality-4 field as unresolvable on a document that is not defective.

## 3. Segmentation: three views, on purpose

`segment_all(doc, graph)` produces three independent views:

| view | what a chunk is | count on EPRT |
|---|---|---|
| `structural` | a section, by heading | 103 |
| `sliding` | an overlapping fixed window | 124 |
| `definitional` | one defined term: its definition **plus** a padded window at up to 12 use sites | 365 |

They must stay genuinely independent, because **disagreement between them is
the signal reconciliation keys on**. Same target list, different view of the
document.

The definitional view is the first use of the graph. A definitional chunk may
cover several disjoint regions of the document, which is why `Chunk` holds a
list of spans rather than one: a quote cited against it resolves back to a true
offset instead of an offset into a concatenation.

## 4. Extraction: the ladder

`extract/ladder.py`. Cheapest first, and nothing reaches a paid tier that a
free one has settled.

```
STAGE 1  rules      tables parse in Python; OFFLINE_RULES take the cheap,
                    unambiguous fields.  Runs ONCE over the document.
STAGE 2  orient     the definition graph.  Runs ONCE.
STAGE 3  sweep      each pass walks its own segmentation for what is
                    still missing.  Runs THREE times.
```

**Stage 2 is the second use of the graph**, and it is the interesting one. It
does not iterate the 365 definitional chunks hoping the right one lands. It
starts from the *fields*: 23 of the 56 registry entries name the defined terms
they depend on in `FieldSpec.definition_anchors`, so the stage resolves each
anchor, takes its closure, and assembles a synthetic chunk from those
definitions' spans. Fields sharing an anchor are asked together — one call per
defined term rather than per field. **5 calls.**

Stages 1 and 2 run once rather than per pass because neither changes between
passes. Three runs would buy one view's worth of evidence at three times the
price, and would arrive at reconciliation looking like three-way corroboration
for something one reader produced once.

### What the model is shown

The model sees what the cheaper tiers found, so it can overturn a wrong regex
rather than silently duplicating it. That is not free: **a pass that agrees
with a value it was shown is not independent evidence for that value.** One
wrong rule, shown to three passes, would otherwise arrive as three-way
corroboration.

So every candidate records the prior it saw. The test is the **citation, not
the value**:

- same value, **overlapping** span → an *echo*. Excluded from support, kept in
  the record.
- same value, **different** span → the figure was found elsewhere in the
  document. Real support, however it was prompted.
- different value → an *overturn*, the strongest signal either tier produces,
  and the reason for showing the prior at all.

### Why the sweep never stops early

Validator C is the only thing entitled to return `absent_from_document`, and
that is a **confident** status — a wrong one is a silent error against the
family budget. A sweep that stopped when the last field settled would leave the
*unsettled* fields having been searched in only part of the document, and
"absent" would quietly mean "we stopped looking". So every section is visited,
`LadderResult.searched` records which sections were searched per field, and a
run cut short by a budget says so.

### Cost

| | calls | text sent |
|---|---|---|
| old flat walk: 592 chunks × 3 passes, all 56 fields every time | 1,776 | 9.5M chars |
| ladder, sweeping all three views | 597 | 3.18M chars |
| **ladder as it stands: 5 orient + 103 structural + 124 sliding** | **232** | **1.26M chars** |

The definitional sweep is gone, and that was the largest single cost decision
in the pipeline. It walked one chunk per defined term — 365 chunks, 1.9M
characters, 61% of the calls — and what it sent was mostly boilerplate that no
registry field could live in: 11,139 characters of `"Affiliate"`, 2,620 of
`"Bail-In Action"`, each with all fifty-six targets attached. The registry
names sixteen definition anchors and five resolve in that document, so 360 of
365 chunks were sent on spec. Even `"Applicable Margin"` opened with 600
characters of table of contents, because use-sites include the TOC entry.

The orientation stage does that job from the other end and sends 33 characters
for the closing date, asking one field rather than fifty-six.

**The cost is the third view.** `support` now tops out at 2, so
reconcile's `support < 2` has quietly become a *unanimity* rule where it used
to be a majority-of-three rule. On Essential Properties that demotes three
fields from `confirmed` to `needs_review` — `revolver.commitment`,
`initial_term_loan.commitment` and `rating.credit_quality` — with the values
unchanged. Two of those three were resting on fictitious support anyway (all
their candidates cite one span), but the tightening is real and is why that
rule needs fitting rather than inheriting.

The flat walk is still reachable as `run_passes(ladder=False)`. It is the
baseline the ladder has to beat, and a change to extraction that cannot be
measured against what it replaced is a change nobody can defend.

## 5. Reconciliation

`extract/reconcile.py` folds every pass's candidates into one record per field.
Candidates are grouped by value; groups are ranked by `(from_definition,
deterministic, support, confidence)`; the winner becomes the field.
`from_definition` heads the key because a value read from a term's own
definition outranks one read from a recital that happens to match a regex — the
failure that reported Essential Properties' closing date as `2019-11-26`.

Two status rules live here:

```python
if len(ranked) > 1:
    field.status = "conflicted"           # the passes disagreed
elif winner.support < SUPPORT_FLOOR and not winner.deterministic:
    field.status = "needs_review"         # "single-pass discovery"
```

`support` is the number of **independent segmentations** backing the value —
echoes excluded. `SUPPORT_FLOOR` lives in `eval/support_fit.py` beside the
fitter that is meant to set it; the fit is currently INCONCLUSIVE at n=4
labelled outcomes, so the value stays 2 as a stated policy rather than a
measured threshold, and it says so in that module.

### Before grouping: whose tranche is this?

Candidates are partitioned on `Candidate.applies_to` *before* they are grouped
by value. That field carries the tranche **the document itself** attributed the
value to, read from the text and never assigned by an assembler:

```
" Floor " means a rate of interest equal to (i) with respect to Term Loans,
0.75% and (ii) with respect to Revolving Loans, 0.00%.
```

Without the partition those two candidates meet in one grouping and the field
comes out `conflicted` — reporting a dispute the document does not have. With
it they are two variants, each `confirmed`, each citing the clause that
attributes it.

The deal-wide partition (`applies_to is None`) is reconciled exactly as it was
before this existed and stays `variants[0]`, which is what every validator, the
calibration fit and the labelling harness read. So a document that states each
term once produces byte-identical output — that is the property that made the
change safe to ship, and `tests/test_per_tranche.py` asserts it.

Where the *only* readings are attributed, `variants[0]` is decided by whether
they agree. Latham prices both tranches at 0.00%, so the deal-level answer is
0.00%; Iridium prices the term loan at 0.75% and the revolver at 0.00%, so the
deal-level slot **declines** and the tranche variants carry the values.
`Asserted.from_field(for_tranche=...)` is what finds them, and a value found
that way stops being counted as inherited.

Measured on the 100-document harvest, about six documents price their tranches
differently. That number is why this is a partition and not twenty-one new
registry fields: see the header of `tests/test_per_tranche.py` for the scan.

## 6. The precedence graph — a different graph

`graph/precedence.py`, built from the document text rather than from the
defined terms. It answers "notwithstanding anything to the contrary": which
clause displaces which. That is family F02, and it has nothing to do with the
definition graph beyond sharing a package.

## 7. Validation, and what the thresholds actually gate

Seven validators, each asking a different question:

| | question |
|---|---|
| **A** `span_support` | Does the cited text actually support the value? |
| **B** `orphan_sweep` | What does this chunk say that the extraction does not know about? |
| **C** `negative_space` | Affirmatively confirm absence, chunk by chunk |
| **D** `overrides` | Does the second provision displace the first? |
| **E** `external_dependency` | Force `external_reference`, and say which kind |
| **F** `criticality` | Rank the review queue by what moves money |
| **G** `amendment_effect` | Check the parser's reading of each amendment |

**The thresholds do not decide whether a value is correct.** Nothing in the
pipeline knows that. They decide `confirmed` vs `needs_review` — whether a
value is presented as settled or routed to a human.

Validator A hands Jev the value and the text it was cited from, as a statement
to score:

> "The text supports a value of $1,300,000,000 for the revolving commitment."

and compares the returned probability to a threshold:

```python
threshold = ctx.threshold_for(name, "A_span_support")
if decision.confidence < threshold:
    field.status = "needs_review"
```

Thresholds are per **validator × field class**, because "does this text support
$150m?" and "is there no MFN sunset anywhere in this agreement?" are different
classifiers with different score distributions and pooling them mis-triages
both:

```
A_span_support/dates             0.9375
A_span_support/economic_terms    0.8125
A_span_support/parties           0.80
A_span_support/covenant_levels   0.40
C_negative_space/economic_terms  0.875
```

They are **fitted**, not chosen. `validate/calibrate.py` takes labelled samples
of `(probability, was it correct)` and picks the lowest threshold that hits a
target precision for that class — 0.99 for economic terms and dates, 0.90 for
administrative. Each is stored with the silent-error rate it achieved, tagged
with the backend it was fitted against, and `load_thresholds` refuses to apply
a threshold fitted on one scorer to a different one.

The headline metric is not accuracy. It is: **of the fields marked
`confirmed`, what fraction were wrong?** A field routed to review was handled
correctly even if its value was wrong.

### Four reasons a field is empty, and only one is absence

Validator C is the only thing entitled to return `absent_from_document`, and it
has to rule out three other explanations first — each found by a document in the
corpus, in this order:

| the record is empty because | how C knows | what it says instead |
|---|---|---|
| no chunk carried any text, so nothing was swept | `asked == 0` | `needs_review`: a field nobody looked for is not a field confirmed missing |
| the filing amends an agreement it does not carry | `_amends_an_agreement_it_does_not_carry` | `needs_review`: absence here is a fact about the amendment, not the facility |
| the extractor read a value this field's type cannot hold | `untypable_value` qualifier | `needs_review`: the record holds no number and the document holds one |
| the term is defined and its definition carries several values | `unsettled_in_definition` qualifier | `needs_review`, naming the axis: the grid is the term, one cell of it is not |
| **nothing in the document addresses it** | probability ≥ threshold | `absent_from_document` — the only one that is absence |

The fourth row is the newest and the one that looks most like absence. Before
it, the definitions tier declined a multi-valued definition *silently*, and
silence reads downstream as "no pass produced a candidate" — which is exactly
what invites C to confirm a term absent that the document states emphatically,
with a probability printed beside it. Now the tier emits a candidate with no
value, the span of the definition, and the values it found; C reads the
qualifier and declines.

**Validator E is the third use of the graph** — `graph.resolve(document)` then
`graph.get(...)`, to decide whether a missing value is *external by design* (a
term whose definition points at a fee letter nobody filed) rather than
something the extractor missed. Those are different facts and conflating them
corrupts a corpus.

## 8. Invariants

`validate/invariants.py`. Deterministic checks, never produced by a model:
totals, date ordering, counting. Nothing at any extraction tier does
arithmetic — the amortization rows and the printed bullet are two independent
facts and the arithmetic between them is a *check*, not a derivation. Storing
the derived bullet would make that reconciliation self-satisfying, which is how
Trap 1 hides.

## 9. Orphan sweep and tier 4

Validator B asks every chunk what it says that no field captured. Chunks that
score high and produced nothing become `OrphanChunk`s, and tier 4 re-reads them
with the graph in hand — **the fourth use**. A line reading "0 rescued" over a
hundred orphans is a statement about the pattern set and is worth printing.

## 10. Archetype dispatch

`models/archetypes.py` classifies the deal from vocabulary in the first 30,000
characters, splitting `decisive_signals` (settles it) from `supporting_signals`
(consistent but shared). Above 0.50 the profile begins marking field groups
`not_applicable_to_archetype`.

That status is **confident**, which is why the classifier is the largest open
defect in the project. `abl_revolver` fires on "borrowing base" and rules four
groups inapplicable — and three documents in the corpus say "borrowing base"
about *somebody else's* facility: a warehouse buying other companies' loans, and
an investor rights agreement defining the term for a credit facility that does
not exist yet. The question the classifier never asks is **whose facility do
these words describe**, and keyword counting cannot answer it, because a real
ABL and an equity agreement use the same words.

## 11. Assembly

`models/assemble.py` builds the `CreditAgreement`: metadata, and a tranche per
tranche with its own terms, conditions, reference data and schedules. Every
leaf carries its status and spans, so nothing is withheld — the discipline
lives in `Asserted.status` rather than in dropping the value.

`models/export.py` is the other path: a strict FpML projection that *does*
withhold anything unsettled, because FpML's `Facility` has a bare
`Decimal | None` and no room to say how a number was arrived at.

### The word "facility" means two things

This is the single most confusing thing in the codebase and it is worth stating
plainly:

- `fpml_model.Facility` **is one tranche**. It has a `facility_type` of
  `revolver` or `term_loan`, one commitment, one maturity. `build_facilities()`
  returns a *list*.
- `agreement.CreditAgreement` **is the whole deal**, and its `tranches` are what
  the market calls facilities.

Before `CreditAgreement` existed there was no object for the agreement, so
deal-level facts had nowhere to live but on each tranche, and a three-tranche
deal reported the borrower three times.

---

## The five graph uses, collected

| # | where | what it does |
|---|---|---|
| 1 | `segment_definitional` (`ingest/segment.py`) | one chunk per defined term: definition + up to 12 use sites |
| 2 | orientation stage (`extract/ladder.py`) | field → `definition_anchors` → `resolve` → `closure` → a synthetic chunk of definition spans |
| 2b | flat path (`extract/passes.py`) | `context_for(chunk.label)` on definitional chunks only — the old, untargeted use |
| 3 | Validator E (`validate/validators.py`) | is a missing value *external by design*? |
| 4 | tier-4 re-read (`pipeline.py`) | orphan chunks re-read with definitional context |
| 5 | report (`pipeline.py`) | `graph.stats()` |

---

## Known gaps in this flow

Three, stated here because a flow diagram that omits its own weak points is
decoration.

**The registry is document-level where the deal is tranche-level, and for the
hardest documents no registry shape fixes that.** One
`applicable_margin.eurodollar_top_level_pct` for the whole agreement, when a
revolver and a delayed-draw term loan price differently. Where the document
attributes a value to a tranche in words, §5 now reads the attribution and the
tranche carries its own number. Where the document states the term as a grid —
Essential Properties indexes Applicable Margin by Credit Rating Level ×
facility × rate type, and 45 of 100 harvested agreements define their pricing
term with no percentage in it at all — there is no per-tranche field that could
hold the answer either, because the axis is not the tranche. Those are reported
as a grid with the axis named, not as a number and not as absence. Resolving
one needs a leverage ratio or a rating, which is an input the pipeline is not
given. Everything else is still filled from the deal-level field and marked
`inherited`, so the remaining gap is visible in the output rather than papered
over.

**No live model pass has ever run.** `ANTHROPIC_API_KEY` is unset, so every
model-tier number in this repository comes from three readings made by hand and
checked in. `OfflineJev` — a deterministic lexical stand-in — answers every
validator question. The ladder described above has never been exercised against
a real model.
