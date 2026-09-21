# Extraction architecture review and path forward

Date: 2026-09-20

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

The zero-silent-error result is useful but narrow. A wrong or missing result
routed to review is not counted as a silent error. That is the correct safety
metric for auto-confirmation, but it is not extraction recall and it does not
show that the system returns enough usable fields. The README is explicit that
real-document recall fell from roughly 30 of 38 fields on synthetic fixtures
to between zero and three on the initially labelled real documents.

The 100-document EDGAR corpus is a valuable stratified test population, but it
is mostly unlabelled. It reveals crashes, drafting diversity, and checks that
fire implausibly often. It cannot measure field accuracy until humans label
the expected values and evidence.

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

The offline rule backend is currently the default. In layered mode, a rule that
finds a field prevents the model from considering that field in the same
chunk. Reconciliation also ranks deterministic candidates ahead of model
candidates. This is safe only after every privileged rule has demonstrated
very high precision on held-out real documents.

Retain deterministic extraction only for forms that are mechanically
unambiguous, such as a well-formed table cell with a known header, an exact
date definition, or a signature role. Treat all other rules as candidate
generators, not authoritative answers.

### 2. Use provider-enforced structured output

The current Anthropic adapter asks for JSON and extracts the first JSON-looking
substring from free-form text. Replace that boundary with schema-constrained
output from a provider or a maintained extraction library. Preserve the
existing rule that a non-null value without a locatable quote is discarded.

### 3. Replace repeated sampling with targeted iteration

Multiple temperatures over the same document are correlated readings, not
independent evidence. Use stages with distinct jobs:

1. identify facilities, operative sections, and relevant definitions;
2. extract values, conditions, variants, and evidence into a strict schema;
3. reread only missing, conflicting, high-criticality, or low-support fields;
4. run deterministic normalization and invariants; and
5. route unresolved results to a human.

An optional verifier model should see the proposed value and its evidence and
answer a different question from the extractor. Keep it only if an ablation
shows that it catches errors at acceptable cost without merely echoing the
first model.

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

The verified standards projection proposed in
[PR #3](https://github.com/nighthawk6389/Jev-credit-agreement-parser/pull/3)
is useful infrastructure. Its benefit should be measured as mapping coverage
and interoperability, separately from extraction accuracy.

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

1. **Gold-schema and labeling guide**: freeze the first field set and
   adjudication rules.
2. **Real labels, batch 1**: label 10 diverse documents and add a document-level
   evaluation split.
3. **Model baseline**: integrate schema-constrained grounded extraction with no
   custom semantic validators beyond citation checking.
4. **Ablation harness**: compare current, model-only, and hybrid pipelines on
   identical inputs and metrics.
5. **Hybrid implementation**: implement only the stages the ablation proves
   valuable.

That ordering makes the next architectural decision evidence-driven. The
repository already has enough machinery; it needs ground truth and a strong
baseline more than another layer.
