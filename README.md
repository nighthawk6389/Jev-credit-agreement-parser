# credit-extract

Extracts a private-credit credit agreement into a standards-grounded data
model, then tries to prove itself wrong.

Getting 80% of the fields right is a solved problem. All the engineering value
is in the last 5%: the fields cross-referenced three definitions deep, the ones
that live in a schedule, the ones a hardcoded table overrides, the ones that
point at a document you don't have, and the ones that are *absent* in a way
that matters. Every design decision here is optimized for that 5%. A pipeline
that scores 95% and doesn't know which 5% it missed is worth less than one that
scores 92% and flags the rest.

```
ingest → normalize → segment (3 ways) → definition graph → extract (N passes)
       → reconcile → Jev validate → invariant check → calibrate → report
```

Extraction is tiered, cheapest first, and the boundary is deliberate:

| tier | takes | why |
| --- | --- | --- |
| tables | figures in a grid | free, spans exact to the character, no sampling variance |
| rules | fields that are cheap and unambiguous | better than a model where the form is fixed |
| **model** | **everything else** | the hard cases are not a pattern problem |

`LayeredBackend` enforces it: the rules run first and the model is handed only
the fields they did not settle. The rules are **not** extended to cover the
tail — across 100 real agreements "is hereby amended" takes 24 distinct
phrasings, 13 of them occurring once, and a pattern set chasing that grows
without bound while every regex added for a rare form can misfire on a common
one. Whatever no tier settles is still caught by the orphan sweep.

## Quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# Extract, with the offline backends -- no API keys, no network.
credit-extract extract credit_extract/eval/gold/fixture_meridian_2017.html \
    --out result.json --passes 3 --budget 5.00 -v

# The four traps, as an acceptance check.
credit-extract traps credit_extract/eval/gold/fixture_meridian_2017.html

pytest -q                                          # 246 tests
python -m credit_extract.eval.harness --calibrate  # refit thresholds
python -m credit_extract.eval.family_report --gate # per-family coverage + blind spots

# Labelling a new agreement.
python -m credit_extract.eval.split --show         # which side each document is on
python -m credit_extract.eval.label <document>     # scaffold a label file to correct
```

Against the live services:

```bash
export ANTHROPIC_API_KEY=... JEV_API_KEY=...
credit-extract extract agreement.htm --backend anthropic --jev api --out result.json
```

## What it produces

```
  17,031 normalized characters, 56 target fields
  status: absent_from_document=3, confirmed=30, external_reference=1,
          needs_review=13, not_applicable_to_archetype=9
  cost: $0.0024 (73 Jev requests, 565 questions; 0 LLM calls)

  invariant violations (5):
    [error] amortization_total_consistent
      amortization table sums to $11,663,750 but a level $376,250 payment over
      27 quarters is $10,158,750, a $1,505,000 discrepancy

  external references (1):
    consolidated_ebitda.addback_cap_clause_a_xvi -> Sponsor Model

  affirmatively absent (1):
    mfn_sunset (confirmed absent at 0.88)

  orphan chunks (1) -- text that says something no field captured:
    struct:2.16 [payment_obligation 0.66] SECTION 2.16 Call Protection...

  traps: 4/4 caught
```

Every field in `result.json` carries its value, spans with character offsets
into the normalized text (plus the page and section they resolve to), the
standard term it reports under, both confidence scores, its status, and the
full validation trace.

## Non-negotiables, and where they are enforced

| Rule | Enforced by |
| --- | --- |
| Every value has a span. No span, no value. | `ExtractedValue` fails construction (`models/core.py`); the LLM parser discards any value whose quote it cannot locate in the chunk |
| No model does arithmetic. | Totals, date comparison and counting live in `validate/invariants.py`; prompts forbid it; ACTUS schedules are generated in Python |
| `null` is never a final answer. | Statuses resolve to `absent_from_document`, `external_reference`, `needs_review` or `conflicted`; `ExtractionResult.unresolved()` and a test assert it |
| The orphan sweep runs every time. | `validator_b_orphan_sweep`, unconditional in `pipeline.py` |
| Thresholds are fitted, versioned, CI-tested. | `config/thresholds.json`, `validate/calibrate.py`, `tests/test_calibration.py` |
| Confidence is reported, never hidden. | `extraction_confidence` and `validation_confidence` on every field, plus the validator that produced each |
| Standard term names are recorded. | `standard_term` per field; `fibo()` and `actus_term()` raise on anything not in the vendored snapshot |

## The data model composes three standards

Nothing here is a schema someone invented.

**FIBO** supplies entities, roles and structural relationships. Terms resolve
against a vendored snapshot of the EDM Council's published ontologies, pulled
by `scripts/vendor_standards.py`. Using the real ontology corrected two natural
guesses: `Borrower` lives in FBC Debt (`fibo-fbc-dae-dbt:Borrower`), not FND
ParticipantsAndRoles — `fibo-fnd-pas-pas` is the *Clients* module — and the
Loans module prefix is `fibo-loan-ln-ln`, not `fibo-loan-loan-loan`.

FIBO also publishes **no class for syndicated-loan agency roles**. Administrative
agent, collateral agent, arranger, syndication agent and issuing bank are
recorded as documented gaps with reasons, not force-fitted onto
`ThirdPartyAgent`. `fibo()` raises on an unknown CURIE, so an invented URI
cannot ship.

**FpML** supplies operational loan structure and economics — facility type,
current and original commitments, accrual choices, SOFR credit-spread
adjustments, floors/caps, PIK options, delayed-draw optionality, lien/seniority,
ratings, currencies, party references and fee options. The checked-in
`fpml_terms.json` index contains 851 element declarations generated from the
published 5-13-7 schemas at a pinned public mirror commit. `fpml()` raises on
an unknown element just as `fibo()` does on an unknown CURIE.

The two standards are deliberately composed rather than treated as
alternatives. FpML says how a facility operates; FIBO says what the entity or
relationship means. Thus a lien resolves to both `fpml:lien` and
`fibo-loan-ln-ln:LenderLienPosition`, and a rating resolves to both
`fpml:creditRating` and `fibo-fbc-dae-crt:CreditRating`. Where only one
standard has the concept (for example PIK accrual in FpML), the other side is a
documented gap rather than an invented ontology term.

`fpml()` and `fibo()` raise on an unknown name, so the two binding functions
check themselves whenever they are called. The field registry does not go
through them — its `standard_term` is a plain string — so `scripts/gen_fpml.py
--check` resolves all 40 mapped terms against the checked-in index, offline, on
every build. `--drift` re-fetches the pinned bytes and asks the separate
question of whether the mirror still serves what the snapshot was built from;
it is informational, because a change in someone else's repository is not a
reason to fail a pull request. `--vendor` refreshes the snapshot and
`--generate` optionally runs `xsdata`.

What `verified=True` claims here is narrow and worth stating: the element name
is declared in the pinned schemas. It does not claim the element means what the
field means, or that it is legal where the mapping puts it — FpML declares some
names in more than one scope and the index merges them. The `FPML_SCHEMAS`
blind spot carries the limit into every report.

**ACTUS** supplies anything that generates a cashflow, and is the reason to
bother with standards at all. LAM and LAX are reimplemented in Python
(`models/actus_map.py`) against the vendored v1.4 data dictionary, so a
correctly extracted term loan is *executable*. The initial term loan maps to
**LAX**, not LAM: 1%/yr plus a bullet is not a linear amortizer, and LAX carries
the arbitrary redemption array that structure needs.

The revolver maps to **nothing**. `actus_mapping` comes back `None` with a
reason: CLM (Call Money) is closest and is still wrong, because a revolver's
repeated draw and repay against a commitment is not a called loan. A documented
gap beats a wrong mapping.

## The four traps

The spec names four verified traps. A pipeline that does not catch all four is
not finished, so they ship as integration tests (`credit_extract/eval/traps.py`,
`tests/test_traps.py`, `tests/test_trap1_amortization.py`).

**Trap 1 — duplicated amortization rows.** The §2.10 table prints 31 payment
dates for a period spanning 27 quarters; the four 2023 quarters appear twice.
Every row parses cleanly, so no extraction model flags it. Read literally the
table sums to $11,663,750 against a correct $10,158,750.

Caught four independent ways: strictly-increasing dates, row count against
quarters spanned, a level-payment total check that names the $1,505,000
discrepancy, and the generated ACTUS schedule diffed against the printed table
cell by cell. The table parser deliberately does **not** de-duplicate — that
would repair the trap before the invariant checker ever saw it.

**Trap 2 — hardcoded opening EBITDA.** Four pre-closing quarters are fixed by
table and override the Consolidated EBITDA definition entirely. Any pipeline
computing EBITDA from the definition gets the first four test periods wrong.
Caught by validator D, anchored on the `notwithstanding` clause that introduces
the table.

**Trap 3 — uncapped external add-back.** Clause (a)(xvi) permits add-backs set
out in the Sponsor Model, capped at the amounts in that model — a spreadsheet
delivered 31 May 2017 that is not a Loan Document and was never filed. The
visible 25% cap governs clauses (a)(xiii)–(a)(xv), so it is *not* the real cap.
Correct output is `external_reference`, not `0.25`. The definition graph
reaches it structurally (Consolidated EBITDA → Sponsor Model) and validator E
confirms the dependency.

**Trap 4 — absent MFN sunset.** MFN protection at 50bps with no expiry, which
is materially lender-favourable and unusual. `mfn_sunset: null` conveys
nothing; `absent_from_document` at 0.88 conveys a deal term. Caught by
validator C.

## Validators

Ordered by what they are worth, not alphabetically.

**B, the orphan sweep** — the highest-value mechanism here. Most pipelines are
recall-limited: they ask "where is field X?" and if X lives somewhere
unexpected it is silently missing. B inverts it and asks every chunk whether it
says something the extraction does not capture. Text scoring high while
contributing zero fields is an orphan. On the fixture it independently surfaces
§2.16 Call Protection, a 1.00% soft-call fee that no field in the registry
targets.

**C, negative space** — turns a `null` into a finding. "The document is silent"
and "we failed to find it" are indistinguishable in any pipeline that reports
`null`, and telling them apart is most of the long-tail problem. A claim about
the whole agreement does not fit in a 32k state and Jev questions cannot chain,
so C asks every chunk and takes the conjunction in Python.

**D, override detection** — credit agreements are full of `notwithstanding`,
and the long tail is disproportionately provisions that are correct in
isolation and wrong in context.

**E, external dependency** — forces `external_reference` where a magnitude
lives in a document nobody has. Two tiers: a free Python check for an external
citation in the same *sentence*, then Jev to judge dependence.

**A, span support** — the basic gate. Fields whose citations land in the same
neighbourhood share one state, so a clause carrying five figures costs one
request.

**F, criticality** — triage, so review time goes to what moves money.

## Cost

State is sent once per request and output is free, so fifteen questions against
one clause cost essentially what one does. That is not a micro-optimization; it
is what makes the orphan sweep affordable, and it is asserted as a test
(`test_fifteen_questions_cost_about_what_one_does`). A full sweep of the
fixture — 56 requests, 141 questions — costs **$0.0011**.

The escalation ladder is respected in order and never skipped upward:
deterministic parse → Python invariant → batched Jev → targeted re-read →
full-context re-read → human. `--budget` is a hard ceiling, checked before
each request rather than after.

## Calibration

Thresholds are fitted per **(validator, field class)**, not per field class.
Span-support probabilities and absence probabilities are different classifiers
on different scales; one threshold across both mis-triages both.

Selection uses the **95% Wilson lower bound**, not the point estimate. A
precision of 0.993 on 297 samples does not evidence 99% precision, and
selecting on the point estimate reliably picks whichever threshold got lucky —
usually the one that accepts everything. When no threshold can support the
target at the available sample size, the config records `certified: false` and
the report says so.

Fitting is on a **document-level split** (fields inside one agreement are
correlated, so a sample-level split leaks), and every reported number is
measured on held-out documents.

The headline metric is not accuracy. It is: of the fields marked `confirmed`,
what fraction were wrong? Silent errors are the only kind that hurt. On the
gold corpus that number is **0 of 692**.

```
validator / field class            thr   prec   p_lo  cover  silent   fit  bad  held
A_span_support/economic_terms    0.812  1.000  0.962  1.000   0.000   200    2    97
C_negative_space/economic_terms  0.875  1.000  0.566  1.000   0.000     6    0     5
```

## What the pipeline has not been tested on

Every report ends with this, and the coverage run prints the register in full
whether it passes or fails. An accuracy figure that averages over untested
document families is worse than no figure, because it will be believed.

```
python -m credit_extract.eval.family_report --gate
```

Eleven trap families, each with a silent-error budget of zero: of the
propositions the pipeline asserted *confidently*, what share were wrong. A
wrong answer routed to review was handled correctly; a wrong answer asserted
reaches a reader unannounced, and only the second one hurts. Real and
synthetic counts stay in separate columns, because catching a defect we
injected, in the shape we injected it, demonstrates less than catching the
same defect in a filing.

### Real filings, and what they showed

`corpus/edgar/` holds 100 SEC exhibits stratified across 18 strata — sponsor
direct lending, broadly syndicated, ABL, second lien, ARR, PIK toggle, venture
debt, fund-level, investment grade, LMA, DIP, amendment chains, A&R,
forbearance, SOFR/CSA, legacy LIBOR, 52/53-week calendars, multicurrency — with
every harvest target met. `corpus/real/` holds four more, read in detail.

They are **unlabelled**, so they establish that the pipeline runs and what it
claims, not whether any claim is right. That was enough to find defects no
synthetic fixture could have. [`docs/corpus_findings.md`](docs/corpus_findings.md)
has the full scan; the headline is that the pipeline's assumptions about the
market were wrong in four measurable places, including a check that was firing
on ~40 correctly-drafted agreements.

Run it yourself:

```bash
python scripts/run_corpus.py --limit 10      # unzips corpus/edgar on demand
```

### The split, and the first held-out document

Every real assertion here used to be on a filing that was read while the parser
was being written, so the figures described fit rather than generalisation. The
split is frozen **before the labels exist** — the only moment it can be frozen
honestly, because afterwards a labeller knows which side would flatter the
result. All 100 harvested documents are assigned by SHA-256 of the document
name, stratified so every deal type contributes: **27 held out, 73 fittable**,
plus 11 pinned to the fit side as already-read.

The first held-out document was labelled the moment the split existed, and it
paid for the machinery immediately. **Air T Amendment No. 7** is a bilateral
facility carrying a revolver, a term loan and an accordion. Its borrowing base
led the classifier to an asset-based revolver, which rules term-loan fields
inapplicable; the extractor had separately missed the Consolidated Term Loan's
maturity, because the registry anchors on a phrase this agreement does not use.
Suppression fires only on fields the extractor left empty — so the two misses
composed, and a term loan maturing **27 August 2031** was reported as
`not_applicable_to_archetype`: a settled answer, and wrong.

Neither half was invisible on its own. A field in review is visible; an
unreliable archetype is in the blind-spot register. It is the composition that
is silent, and no fixture would have produced it. Archetype suppression is now
vetoed by the document: a field group the text plainly discusses stays
applicable whatever the classifier concluded, and the report names the phrase
that vetoed it. The same document also produced a false positive —
`one-quarter of one percent (0.25%)` read as a numeral mismatch — and a
mutation that cannot apply to an amendment, since an amendment's
cross-references point into a base agreement it does not contain.

```bash
python -m credit_extract.eval.split --check   # recomputes and compares; CI runs it
```

The assignment is a function of the names, so a document cannot change sides
after its labels turn out inconvenient — the edit is visible and the build
rejects it. [`docs/labelling_guide.md`](docs/labelling_guide.md) is the
contract for writing the labels themselves: which fields, what counts as the
right answer on a multi-tranche deal, and how to choose between
`absent_from_document` and "I could not find it", which are not the same claim.
`python -m credit_extract.eval.label <document>` scaffolds a file filled in
with what the extractor currently says and the text it read each value from,
every line marked `VERIFY`; the loader refuses a file that still carries the
marker, so a scaffold cannot be mistaken for ground truth.

The four in `corpus/real/` are the ones with Tier 2 labels:

| filing | what it is | what it found |
| --- | --- | --- |
| Health Catalyst / Silver Point | recurring-revenue term loan | no Consolidated EBITDA anywhere; 70 redacted terms; two references to a reserved section |
| GBDC 4 Funding III / BNP Paribas | BDC warehouse revolver | borrowing base and advance rates, no term loan; 33 exhibits not attached |
| Wells Fargo Third Amendment | prose amendment | 18 effects the parser read as **zero** — "as follows" without "to read", targets like `2.1(a)(ii)(B)(3)` |
| Wheels Up No. 4 + No. 5 | a real **chain** | the conformed agreement alone reports the revolver expiring two years early |
| BRC Group, two 2026 filings | *not* a chain | same CIK, different facilities — CIK is not a chain key |
| Ares Capital CP Funding No. 18 | **blackline** | an amendment mechanism the pipeline had no concept of |

The last one is the reason the ingester no longer writes struck-through text
into the offset space. A blackline carries its changes as typography;
flattened to plain text, both the old and new figures survive, adjacent:

```
Up to U.S. $ 2,150,000,000 2,250,000,000
```

A first-match parser reports $2,150,000,000 — the figure that amendment
deleted — off a span that quotes the document accurately. Nothing about the
output looks wrong. Deleted runs are now excised during ingestion and kept in
a sidecar beside the span that replaced them, so the offset space *is* the
operative text and no downstream code needs to know a blackline was involved.

That was built from one document, on the assumption it was an oddity. **32 of
the 100 agreements amend by blackline**, against 6 that restate in prose — the
form the pipeline originally handled is the rare one.

Recall on real filings is the headline gap: roughly thirty fields of
thirty-eight on the synthetic fixtures, low single digits on these. The labels
in `credit_extract/eval/labels/` assert the real values regardless, so the gap
is measured rather than avoided.

## What this environment could not verify

Stated plainly, because a number whose provenance you can't check is worth less
than an admitted gap.

**sec.gov is blocked by this environment's egress policy.** The spec's primary
test document — the Paya / GTCR-Ultra agreement — could not be fetched. The
trap tests run against a clearly-labelled *synthetic surrogate* with fictional
parties, carrying all four traps with the spec's exact arithmetic
($11,663,750 vs $10,158,750; the four EBITDA quarters; the (a)(xvi) Sponsor
Model clause; 50bps MFN with no sunset). `scripts/fetch_corpus.py --primary`
pulls the genuine exhibit where egress allows, and the trap tests prefer it
automatically once it is on disk — no other change needed.

**The gold corpus is 24 synthetic variants plus four real filings, not 20
hand-labelled real agreements.** Synthetic labels are ground truth by
construction, which is exactly the weakness: the documents were generated by
the same repo that reads them, in the same phrasing. Four of six threshold
classes have **no labelled failures**, which means every threshold clears the
target trivially and the fitted value carries no information — the report
prints that in full rather than showing a clean 1.000 and moving on.

**No Jev API key.** `OfflineJev` is a deterministic lexical stand-in
implementing the same typed interface, so the pipeline, its tests and its
calibration all run offline. It is not a calibrated model, and the orphan
sweep's concept lexicons are its weakest part — the first thing a real System
One backend makes unnecessary. Thresholds are tagged with the backend they were
fitted against and `load_thresholds` **refuses a backend mismatch** rather than
silently applying an offline-fitted threshold to live Jev.

**No Anthropic API key**, so `AnthropicBackend` is written against the Messages
API but unexercised. `OfflineRuleBackend` is the default: anchored patterns
with exact spans, deliberately weaker at recall than an LLM — which is what the
orphan sweep exists to compensate for.

**The FpML schemas come from a mirror, not from ISDA.** fpml.org serves them
behind an authenticated session, so the bytes are fetched from a pinned commit
of a public mirror and hashed in the vendored index. That makes them
reproducible — `--drift` rebuilds the index from those URLs byte for byte — but
not authentic: nothing here compares them against ISDA. The hand-mapped names
are gone and all 40 mapped terms are declared, which is the part that lifted;
meaning and position are still unchecked, which is the part that did not.

## Layout

```
credit_extract/
  models/     core.py           ExtractedField/Variant, Span, statuses, provenance
              conditions.py     restricted condition grammar; three-valued logic
              quantities.py     units, scale declarations, bps vs percent
              fiscal.py         52/53-week calendars, fiscal-quarter arithmetic
              archetypes.py     nine deal archetypes, decisive vs supporting signals
              pricing.py        benchmark, CSA, margin, floor, grid, waterfall
              fibo_map.py       FIBO bindings + documented gaps
              fpml_model.py     facility structure + the field registry
              actus_map.py      contract mapping + LAM/LAX execution
              vendored/         checked-in FIBO, FpML and ACTUS snapshots
  ingest/     normalize.py      one offset space; blackline deletions excised
              documents.py      document sets, amendment effects, chain folding
              tables.py         grids with per-cell offsets; scalar parsers
              segment.py        three independent segmentations
  graph/      definitions.py    defined-term DAG, closure, cycles, external refs
              precedence.py     notwithstanding / subject to, as a directed graph
  extract/    passes.py         deterministic + LLM backends, pass planning
              reconcile.py      agreement, conflict, single-pass suspicion
              prompts/          extraction and re-read prompts
  validate/   jev.py            System One client, batching, budget, offline stand-in
              validators.py     A-G
              invariants.py     deterministic checks, per trap family
              omission.py       external_by_design vs omitted vs redacted
              calibrate.py      fitting, Wilson bounds, held-out scoring
  eval/       trap_families.yaml  the eleven families -- single source of truth
              blind_spots.yaml    what has no testable examples, and why
              split.yaml          frozen fit/holdout assignment, derived not chosen
              labels/             Tier 2 assertions, one file per document
              families.py         registers, coverage, consistency check
              assertions.py       assertion kinds and evaluation
              split.py            derives and re-checks the document split
              label.py            scaffolds a label file from a run
              mutations.py        Tier 3: inject a defect, check it is found
              family_report.py    the gated report
              gold/               fixture + corpus generator
              harness.py          evaluation and threshold fitting
              traps.py            the four traps as checks
  pipeline.py                   orchestration
  cli.py                        extract | traps | chain
config/thresholds.json          fitted, versioned, CI-asserted
corpus/real/                    four SEC filings, read in detail and labelled
corpus/edgar/                   100 more, stratified, zipped, unlabelled
docs/corpus_findings.md         what those 100 say about the pipeline
scripts/                        vendor_standards.py, fetch_corpus.py, gen_fpml.py
```

## Next, in order of value

1. **Label the corpus.** 104 real documents establish that the pipeline runs
   and what it claims; not one of them can say whether a claim is right. Every
   accuracy number in this repository is still measured on documents the parser's
   own author wrote. Tier 2 labels, starting with the families the coverage
   table reports as undersampled, are worth more than any other work here.
2. **Extraction recall on real filings.** Low single digits of thirty-eight
   fields, against roughly thirty on the synthetic fixtures. Every other number
   is bounded by this one.
3. **Point at live Jev** and refit. The offline stand-in's concept lexicons are
   a placeholder for the judgement the real model makes.
4. **Measure the orphan sweep** — what fraction of held-out labelled fields it
   rescues that the extractor missed. It is the highest-value mechanism here
   and it deserves its own number.
5. **Label the new structural fields** — PIK, lien/seniority, ratings,
   multicurrency and delayed-draw optionality now have verified standard
   targets, but need real-document labels before their accuracy is knowable.
