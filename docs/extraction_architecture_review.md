# Extraction architecture review and path forward

Drafted 2026-09-20 against `main` at `212d7ab`; corrected 2026-09-21 against
`9d1ceeb`, after PR #3 merged; corrected again 2026-09-21 against `c4b05c4`,
after PR #6 merged and the corpus was fully labelled. Claims that did not
survive being checked against the code are marked **Correction** where they
appeared rather than quietly deleted — a review whose errors are edited out is
harder to trust than one that shows them.

The strategy is unchanged. What has changed is how much of the review is still
outstanding work: of the five recommendations in *What should change*, §2 and
§3 have been implemented since the draft, §4's premise has inverted, and §1
stands untouched and is now the one to act on. A review that is mostly overtaken
within a day is a good outcome for the repository and a reason to read its
figures with the date attached.

## Executive decision

The repository is productive as a **reliability and evaluation substrate**, but
it has not yet demonstrated production-grade extraction accuracy. Its strongest
work is deterministic: reconstructing operative documents, preserving source
spans and tables, resolving definitions, checking arithmetic, distinguishing
absence from failure to find, and routing uncertainty to review.

The extraction center should become **model-first and deterministic-verified**.
Do not replace the whole pipeline with repeated model calls, and do not keep
expanding regex coverage into the legal-language tail. Use a strong model for
semantic extraction, then make deterministic code prove provenance, normalize
units, reconstruct amendments, run calculations, detect conflicts, and decide
whether a result is safe to auto-confirm.

The immediate priority is a labelled real-document benchmark and an ablation
test. More ontology work, validators, prompts, or patterns cannot establish
accuracy without that evidence.

## What the repository is doing today

The pipeline has five broad responsibilities:

1. **Construct the document**: ingest HTML, MHTML, PDF, or text; normalize it
   into one offset space; preserve tables; remove deleted blackline text; and
   fold amendments into an operative version where possible.
2. **Build retrieval structure**: segment the document structurally,
   definitionally, and with overlapping windows; build definition and
   precedence graphs; and attach source spans.
3. **Extract target fields**: parse some tables, apply anchored rules, and,
   when explicitly enabled, ask an Anthropic model for fields the rules did
   not settle in each chunk.
4. **Reconcile and validate**: combine candidates, ask Jev or its offline
   lexical stand-in about support, omissions, overrides, dependencies, and
   amendment effects, then run Python invariants.
5. **Normalize and report**: attach FIBO, FpML, and ACTUS vocabulary; generate
   executable cash-flow checks where applicable; expose conflicts, external
   references, blind spots, and a review queue.

That decomposition is directionally right. The problem is not that the repo
contains deterministic stages. The problem is that the current deterministic
extractor is the default and the live semantic stages have not been measured.

## Evidence from the current repository

The following command was run on the latest `main` at the date above:

```bash
python -m credit_extract.eval.family_report --gate
```

It reported:

- 9 documents plus 10 mutants;
- 74 assertions, of which 50 were attached to real documents and 24 to
  synthetic cases;
- 57 propositions asserted confidently and no silent errors among those 57;
- failures in benchmark, unit, and conditionality assertions;
- six undersampled families; and
- live Jev, live Anthropic extraction, and real-corpus labels as unresolved
  environmental or evidence gaps.

Those figures held on `9d1ceeb`. The four failures were worth naming because
they were the report's most informative rows: F07 benchmark 2 pass / 1 fail,
F08 units 2 / 2, and **F10 conditionality 0 pass / 1 fail** — the family's
only assertion. None was a silent error; all four routed to review, which is
why the headline read zero while three families were measurably not working.
Read the pass/fail columns, not the headline alone.

> **Correction — the evidence base has grown by an order of magnitude, and the
> headline is no longer zero.** Every figure above is stale, including the one
> this review leans on hardest. On `claude/credit-agreement-extraction-952py3`
> at `932223f` — 21 commits past `c4b05c4`, carrying the labelling work and not
> yet merged — the same command reports:
>
> ```
> 105 document(s) and 85 mutant(s), 464 assertions (365 real / 99 synthetic)
>
> of 176 propositions asserted confidently, 3 were wrong (1.70% silent error rate)
>   F06_structure  athena_funding_is_not_an_asset_based_revolver  expected unknown, got abl_revolver
>   F06_structure  elmet_investor_rights_have_no_archetype        expected unknown, got abl_revolver
>   F06_structure  horizon_servicing_agreement_has_no_archetype   expected unknown, got abl_revolver
>
>   holdout        120 assertions, 31 confident, 2 wrong
>   fit            195 assertions, 18 confident, 1 wrong
>   contaminated    50 assertions, 32 confident, 0 wrong
> ```
>
> The shape to read is not the 1.70%. The count of confident propositions went
> from 57 to 176 while the error count went from 0 to 3: the pipeline did not
> get worse, the evidence base started measuring a class it previously could
> not see, and a zero measured over 57 propositions was never the same claim as
> a zero measured over 176. Ten of the eleven families are at zero, and the
> three failures are one defect — decisive archetype vocabulary that describes
> a facility other than the document's.
>
> The named rows have moved with it: F07 benchmark is now 5 pass / 57 fail, F08
> units 4 / 18, F10 conditionality 18 / 36, F11 layout 19 / 1. The original
> reading survives and is now much larger in absolute terms — none of those
> failures is a silent error, all route to review, and the fail column is
> dominated by absences the corpus asserts and the deterministic tier cannot
> reach. "Six undersampled families" is now three and they are named: F03
> (disclosure_letter, fee_letter, sponsor_model), F06
> (nav_or_subscription_line, unitranche_with_aal), F09
> (term_defined_in_other_loan_document).
>
> One line has to be added to the advice, because the split now exists: read
> the pass/fail columns, and then read the split. A confident answer measured
> on a document its author read while building the pipeline is worth less than
> one measured on a document they did not, and the report now separates them.

The zero-silent-error result was useful but narrow, and the same caveat applies
to the 1.70%. A wrong or missing result routed to review is not counted as a
silent error. That is the correct safety metric for auto-confirmation, but it is
not extraction recall and it does not show that the system returns enough usable
fields. The README is explicit that real-document recall fell from roughly 30 of
38 fields on synthetic fixtures to between zero and three on the initially
labelled real documents. **Nothing in this repository measures recall yet**,
which is the gap the ablation in Phase 2 exists to close.

> **Correction — the corpus is no longer mostly unlabelled.** The paragraph
> below said it could not measure field accuracy until humans label the expected
> values and evidence. All 100 documents in the frozen split now carry Tier 2
> labels, plus the 11 contaminated ones. The labelling found eight filings in
> the harvest that are not credit agreements at all — a proxy statement, an
> earnings release, three sets of financial statements, a registration rights
> agreement and two stock transfers — and four defects, three fixed and one
> open as the F06 failures above. What it does *not* yet do is measure recall:
> Tier 2 assertions are propositions about particular fields, not a count of how
> many of the 37 critical fields a run returns.

The 100-document EDGAR corpus is a valuable stratified test population. It
reveals crashes, drafting diversity, and checks that fire implausibly often.

The current thresholds are fitted to 24 synthetic documents using
`OfflineJev`. Most classes are not statistically certified at their configured
precision target. Those thresholds must not be used as evidence for a live
model backend.

## What is worth preserving

These components address real failure modes and should remain regardless of
which model performs extraction:

- normalized, addressable source text with exact offsets;
- table structure and per-cell evidence;
- blackline deletion handling;
- amendment-chain ordering and operative-text construction;
- definition closure and precedence context;
- typed money, percentages, ratios, dates, scales, and currencies;
- deterministic arithmetic and ACTUS schedule comparisons;
- explicit `external_reference`, `absent_from_document`, `conflicted`, and
  `needs_review` states;
- exact-quote validation and rejection of ungrounded values;
- archetype-aware applicability, provided archetype classification is itself
  proven reliable;
- per-field criticality and human-review ordering; and
- a frozen, stratified real corpus plus failure-family reporting.

These are not ornamental guardrails. A model can quote a deleted blackline
figure perfectly, extract a recital instead of an operative amendment, or make
an arithmetic error while sounding certain. Deterministic document state and
verification are the correct defenses.

## What should change

### 1. Make semantic extraction model-first

The offline rule backend is currently the default, and in layered mode a rule
that finds a field prevents the model from considering that field in the same
chunk. `LayeredBackend.extract` computes `settled` from whatever the rules
returned and passes the model only `remaining`, so a pattern that fires — at
whatever precision — silently removes that field from the model's view. That
is safe only after every privileged rule has demonstrated very high precision
on held-out real documents, and nothing has yet been measured on a held-out
document at all.

Retain deterministic extraction only for forms that are mechanically
unambiguous, such as a well-formed table cell with a known header, an exact
date definition, or a signature role. Treat all other rules as candidate
generators, not authoritative answers.

    Correction. An earlier draft of this section also said reconciliation
    ranks deterministic candidates ahead of model candidates, and read that as
    a second instance of the same problem. It is not. `ValueGroup.deterministic`
    is true only for the `deterministic:tables` pass id, whose sole emitter is
    the table parser; the anchored rules carry ordinary pass ids and compete on
    independent-segmentation support and confidence like any model candidate.
    So the only tier reconciliation privileges is a parsed table cell with a
    known header — precisely the one this section argues should be privileged.
    The layered suppression above is the real defect, and it is a different
    mechanism in a different module.

### 2. Use provider-enforced structured output

The current Anthropic adapter asks for JSON and extracts the first JSON-looking
substring from free-form text. Replace that boundary with schema-constrained
output from a provider or a maintained extraction library. Preserve the
existing rule that a non-null value without a locatable quote is discarded.

> **Correction — done, and it was not this review that prompted it.** The
> adapter no longer parses free text. The schema is sent as
> `output_config.format` (`passes.py:530` and `:588`), provider-enforced, which
> is what this section asked for; the comment at `passes.py:440` records what
> it replaced. The second sentence was already the rule and still is: a
> non-null value without a locatable quote is discarded.
>
> The section's remaining value is the boundary it draws, not the work it
> proposes. Nothing here is outstanding.

### 3. Give the passes distinct jobs

Passes today differ by *view* — the same specs asked of structural,
definitional and sliding segmentations — and reconciliation counts agreement
across those views as support. What they do not differ by is **question**:
every pass asks for the whole field set, so a field three passes missed was
missed three times in the same way, and a field three passes found was found
by three readings of text that overlap heavily.

Use stages with distinct jobs instead:

1. identify facilities, operative sections, and relevant definitions;
2. extract values, conditions, variants, and evidence into a strict schema;
3. reread only missing, conflicting, high-criticality, or low-support fields;
4. run deterministic normalization and invariants; and
5. route unresolved results to a human.

An optional verifier model should see the proposed value and its evidence and
answer a different question from the extractor. Keep it only if an ablation
shows that it catches errors at acceptable cost without merely echoing the
first model.

    Correction. An earlier draft argued this as "replace repeated sampling
    with targeted iteration", on the grounds that multiple temperatures over
    one document are correlated readings rather than independent evidence.
    That is true and the pipeline already acts on it. `plan_passes` walks the
    segmentations first and only raises temperature once every view has been
    covered, and its docstring gives the same reason in nearly the same words:
    "two passes over different views of the document disagree for reasons that
    mean something; two passes over the same view at different temperatures
    mostly resample the same reading". At the default `--passes 3` against
    three segmentations, temperature never leaves 0.0. The recommendation
    above stands on its own merits; the sampling criticism was aimed at a
    design this repository does not have, and rewriting `plan_passes` would
    replace a solved problem with the same problem.

    Correction, second pass. The targeted re-read this section recommends is
    also built. Tier 4 runs: `prompts/__init__.py:266` builds the re-read
    prompt for a chunk the orphan sweep flagged, and `recorded.py:199`
    supplies `reread_findings` — Tier 4's other half, what a chunk says that
    has no field at all. Of the five numbered stages above, the only one still
    unbuilt is the optional verifier model, and it is the one the section
    itself says to keep only if an ablation earns it.

### 4. Decouple the internal schema from standards export

FpML, FIBO, and ACTUS are valuable, but they do different jobs:

- FpML describes operational loan terms and lifecycle structures;
- FIBO supplies semantic entity and relationship vocabulary; and
- ACTUS provides executable cash-flow contract types.

They do not make source extraction more accurate. Use a pragmatic internal
facility/tranche schema for extraction, including conditions, variants,
provenance, and unresolved states. Map validated internal fields into standards
through explicit export adapters. A standards mapping is complete only when a
real pipeline result populates and serializes it; a verified element name or a
Pydantic class alone is not end-to-end coverage.

The standards projection in
[PR #3](https://github.com/nighthawk6389/Jev-credit-agreement-parser/pull/3)
is useful infrastructure, and it **merged** after this review was drafted. Its
benefit should be measured as mapping coverage and interoperability,
separately from extraction accuracy — and on that measure the gap was, for a
while, concrete rather than hypothetical. `Facility`, `PikTerms`,
`CommitmentTerms`, `FacilityClassification`, `CreditRating` and
`PartyReferences` were all defined in `fpml_model.py`, and
`grep -rn "Facility(" credit_extract/` returned nothing outside the tests: the
pipeline emitted a flat `fields` dictionary and never constructed one. Forty
FpML element names were verified against pinned schemas and **zero of them were
populated by a real run**, which was exactly the distinction this section
draws.

> **Correction — the gap is closed, and closing it inverted the evidence.**
> `models/export.py:130` now constructs a `Facility`, through a `_Builder` that
> records provenance per element and withholds rather than guesses. A run on
> the gold fixture reports:
>
> ```
>   fpml facilities (3):
>     revolver (revolver): 10 element(s) populated, 15 withheld
>     initial_term_loan (term_loan): 10 element(s) populated, 15 withheld
>     delayed_draw (delayed_draw_term_loan): 10 element(s) populated, 15 withheld
> ```
>
> The recommendation stands and its argument has changed sides. The interesting
> half is now the 15, not the 10. `cli.py:183` prints the *reasons* elements
> were withheld rather than summing them, because an element left empty because
> its value is in a fee letter is not the same fact as an element left empty
> because the agreement has no such term — and FpML has no way to say which.
> That is the decoupling this section argues for, arrived at from the opposite
> direction: the export is not incomplete for want of plumbing, it is
> deliberately incomplete because the standard cannot carry the distinction the
> internal schema makes.
>
> So the sentence to keep is "a standards mapping is complete only when a real
> pipeline result populates and serializes it", and the sentence to add is that
> a mapping which populates everything it is asked for is not thereby correct.

### 5. Simplify validation until each tier proves incremental value

The present pipeline has extraction passes, Jev validators, Python invariants,
targeted rereads, calibration, and trap checks. That is defensible only when
each tier has a measured contribution. For every tier, record:

- errors caught that the prior tier missed;
- correct fields unnecessarily routed to review;
- incremental recall;
- incremental cost and latency; and
- new failure modes introduced.

Remove or merge a tier if it does not improve held-out results.

## Recommended target architecture

```text
raw filing set
  -> deterministic ingest, layout, blacklines, and amendment reconstruction
  -> section/table/definition index with stable source IDs
  -> model facility discovery and archetype classification
  -> model structured extraction with verbatim evidence
  -> targeted model reread for missing/conflicting/high-risk fields
  -> deterministic normalization, precedence, arithmetic, and invariants
  -> calibrated accept/review/absent/external decision
  -> internal canonical record
  -> FpML/FIBO/ACTUS export adapters
```

## Step-by-step implementation plan

### Phase 0: freeze the question before changing code

1. Select 25-40 economically useful fields. Start with facility identity,
   commitment, maturity, benchmark, margin, floor, CSA, PIK, lien/seniority,
   ratings-based pricing, key covenant levels, and draw optionality.
2. Define field adjudication rules, including facility-level cardinality,
   allowed null states, evidence requirements, and treatment of amendments.
3. Freeze model versions, prompts, and the corpus split for each experiment.

**Exit criterion:** two reviewers can label the same five documents using the
schema without inventing case-specific rules.

### Phase 1: create real ground truth

1. Label 30-40 agreements from the existing stratified corpus.
2. Include at least two examples from each economically distinct high-priority
   family: direct lending, syndicated, ABL, ARR, PIK, fund-level, investment
   grade, LMA, DIP, amendments, and multicurrency.
3. Double-label a representative subset and record disagreements.
4. Freeze a document-level holdout before prompt or rule tuning.
5. Store expected values, expected status, facility identity, and exact source
   evidence.

**Exit criterion:** at least 500 real field-document propositions, meaningful
positive and negative cases per critical class, and a holdout untouched by
tuning.

### Phase 2: establish three comparable baselines

Run the same schema and holdout through:

1. the current deterministic/layered pipeline;
2. a minimal model-only grounded extractor; and
3. the proposed model-first hybrid.

Measure per field and per document family:

- value precision, recall, and F1;
- evidence-span correctness;
- correct abstention and external-reference classification;
- silent-error rate among auto-confirmed fields;
- review coverage and reviewer minutes;
- cost and latency; and
- robustness to amendment and blackline cases.

Use document-level splits. Report synthetic and real results separately.

**Exit criterion:** choose the simplest architecture that meets the existing
class precision targets on the 95% Wilson lower bound while providing useful
coverage. If no system is certifiable, expand labels rather than tuning to the
point estimate.

### Phase 3: implement the winning hybrid

1. Introduce a provider-neutral `StructuredExtractionBackend`.
2. Make the internal facility/tranche schema the provider contract.
3. Restrict privileged deterministic rules to held-out-proven patterns.
4. Convert all other rules into hints or candidates that can conflict.
5. Add targeted reread policies based on criticality and evidence gaps.
6. Refit thresholds for the exact extraction and validation backend versions.
7. Fail closed when model, prompt, schema, or threshold versions mismatch.

**Exit criterion:** the full real holdout passes its configured safety and
coverage gates, and every accepted value has resolvable evidence.

### Phase 4: standards export and lifecycle coverage

1. Populate facility objects from real extraction results.
2. Serialize and round-trip the supported FpML projection.
3. Emit complementary FIBO bindings without inventing ontology terms.
4. Generate ACTUS schedules only for supported contract types.
5. Publish explicit mapping coverage and unsupported concepts.

**Exit criterion:** representative documents for every supported mapping
round-trip without losing value, condition, unit, facility identity, or source
provenance.

### Phase 5: production feedback loop

1. Save reviewer corrections as new labels.
2. Track accuracy and review load by document family and model version.
3. Re-run the frozen holdout on every model, prompt, parser, or schema change.
4. Promote a new configuration only when it improves the predeclared metrics.

## Open-source components to evaluate

No mature open-source package provides the entire required pipeline for credit
agreements, amendment chains, blacklines, absence, and standards export. The
following projects can replace commodity layers:

| Project | Best use here | Important limitation |
| --- | --- | --- |
| [Google LangExtract](https://github.com/google/langextract) | Grounded, multi-pass structured extraction with source spans and multiple model providers | Domain schema, amendment semantics, and financial validation remain ours |
| [Docling](https://github.com/docling-project/docling) | PDF/layout/table parsing and document conversion | Must be benchmarked on EDGAR HTML, blacklines, and legal tables before replacing current ingestion |
| [Unstructured](https://github.com/Unstructured-IO/unstructured) | General partitioning and chunking | Same domain-specific benchmarking requirement |
| [OpenContracts](https://github.com/Open-Source-Legal/OpenContracts) | Annotation, corpus review, provenance, and human-in-the-loop workflow | A platform, not a credit-agreement schema or extractor |
| [LexNLP](https://github.com/LexPredict/lexpredict-lexnlp) | Legal tokenization and scalar primitives | Older stack and AGPL licensing |
| [CUAD](https://github.com/The-Atticus-Project/cuad) | Evaluation methodology and general contract labels | Clause categories do not cover loan economics deeply |

Domain-specific references also exist, but none is a safe turnkey dependency:

- [credit-agreement-extraction](https://github.com/saulrichardson/credit-agreement-extraction)
  has a relevant anchor-grounded and contract-IR design, but currently declares
  no repository license;
- [Covenant_Pipeline](https://github.com/EnDisciple13/Covenant_Pipeline) has a
  Gemini extraction, compilation, audit, and viewer pipeline, but also declares
  no repository license; and
- [covenant-extraction-eval](https://github.com/stendeze/covenant-extraction-eval)
  is MIT-licensed and has a useful 15-document benchmark design, but its README
  describes the extractor as not yet built.

Evaluate LangExtract first for the extraction center and OpenContracts for the
annotation workflow. Keep the existing legal/financial deterministic modules
until an ingestion or validation replacement wins on the frozen corpus.

## Work to stop doing for now

- Do not add long-tail regexes without a held-out precision result.
- Do not interpret repeated model agreement as independent validation.
- Do not report synthetic calibration as production accuracy.
- Do not add standards fields without labelled examples and an end-to-end
  populated export.
- Do not treat a successful run over unlabelled documents as an accuracy test.
- Do not expand validator count without measuring incremental errors caught.

## Immediate next pull requests

1. ~~**Gold-schema and labeling guide**: freeze the first field set and
   adjudication rules.~~ — done in
   [PR #5](https://github.com/nighthawk6389/Jev-credit-agreement-parser/pull/5),
   with one amendment: the field set is the existing registry at criticality
   4 or higher (37 fields, which is this document's 25–40) rather than a new
   list that would then drift from the one the pipeline actually extracts.
   The document-level split moved here from step 2, because it has to be
   frozen *before* any labels exist — afterwards a labeller knows which side
   would flatter the result. 27 of the 100 harvested documents are held out,
   stratified, assigned by hash of the name and re-derived in CI so no
   document can change sides later.
2. ~~**Real labels, batch 1**: label 10 diverse documents with
   `python -m credit_extract.eval.label`, following
   `docs/labelling_guide.md`.~~ — done, and overshot. Every document in the
   frozen split carries Tier 2 labels, 100 of 100, plus the 11 contaminated
   ones. Asking for ten was the right size for a first batch and the wrong
   size for the thing the batch was for: ten documents cannot tell you which
   of a family's failures are the pipeline's and which are the corpus's, and
   the eight filings in the harvest that turn out not to be credit agreements
   would all have been missed by any sample of ten.
3. **Model baseline**: integrate schema-constrained grounded extraction with no
   custom semantic validators beyond citation checking.
4. **Ablation harness**: compare current, model-only, and hybrid pipelines on
   identical inputs and metrics.
5. **Hybrid implementation**: implement only the stages the ablation proves
   valuable.

That ordering makes the next architectural decision evidence-driven. The
repository already has enough machinery; it needs ground truth and a strong
baseline more than another layer.

Steps 3 and 4 are now unblocked and step 4 is the one that matters, because it
is where recall gets measured for the first time. Two constraints shape it:

- **No live model.** `ANTHROPIC_API` is an unresolved blind spot in this
  environment, so a model arm can only replay `RecordedBackend` readings. The
  harness has to report which documents it could actually run the model on
  rather than averaging over the ones it could not.
- **The metric has to be recall, not only silent errors.** The family report
  answers "of the propositions asserted confidently, how many were wrong",
  which is the right safety question and says nothing about how many of the 37
  critical fields a run returns. An ablation scored on the family report alone
  would rank a pipeline that answers nothing above one that answers most things
  and errs once.
