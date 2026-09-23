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

| | calls |
|---|---|
| old flat walk: 592 chunks × 3 passes, all 56 fields every time | 1,776 |
| ladder: 5 orient + 103 + 124 + 365 sweep | 597 |

The flat walk is still reachable as `run_passes(ladder=False)`. It is the
baseline the ladder has to beat, and a change to extraction that cannot be
measured against what it replaced is a change nobody can defend.

## 5. Reconciliation

`extract/reconcile.py` folds every pass's candidates into one record per field.
Candidates are grouped by value; groups are ranked by (deterministic, support,
confidence); the winner becomes the field.

Two status rules live here, and neither is the fitted threshold:

```python
if len(ranked) > 1:
    field.status = "conflicted"           # the passes disagreed
elif winner.support < 2 and not winner.deterministic:
    field.status = "needs_review"         # "single-pass discovery"
```

`support` is the number of **independent segmentations** backing the value —
echoes excluded. The `< 2` is a hardcoded number, not a fitted one, and it is
the rule most in need of the treatment §7 gives the others.

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

**The registry is document-level where the deal is tranche-level.** One
`applicable_margin.eurodollar_top_level_pct` for the whole agreement, when a
revolver and a delayed-draw term loan price differently. The `Tranche` slots
exist; the assembler fills them from the deal-level field and marks each one
`inherited`, so the gap is visible in the output rather than papered over.

**`LadderResult.searched` is recorded and unread.** Validator C still decides
absence without consulting which sections were actually searched. Safe while
the sweep is exhaustive; unsafe the first time a budget cuts a run short.

**No live model pass has ever run.** `ANTHROPIC_API_KEY` is unset, so every
model-tier number in this repository comes from three readings made by hand and
checked in. `OfflineJev` — a deterministic lexical stand-in — answers every
validator question. The ladder described above has never been exercised against
a real model.
