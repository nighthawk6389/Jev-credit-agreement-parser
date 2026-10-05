# The first live Jev pass

What happened the first time the validator tier asked a real scorer instead of
the lexical stand-in, on 2026-09-30, against `jev-1.13.0`. Written as a record
rather than a plan: every number here was measured, and the ones that are one
draw of a non-deterministic scorer say so.

The extraction tier is not in this. It needs `ANTHROPIC_API_KEY` or
`AI_GATEWAY_API_KEY`, neither exists here, and the ladder still has never run
against a model. Everything below is `OfflineRuleBackend` extraction with live
validation.

---

## The endpoint did not exist

The handoff said the network policy had denied `api.jev.ai`. From an
environment that reaches `jev.ai` and `www.jev.ai` with a 200, the obstacle
was different: **`api.jev.ai` does not resolve** (NXDOMAIN at 8.8.8.8), and
certificate transparency has 21 certificates for `jev.ai` and `www.jev.ai`
since 2022 and none, ever, for `api.jev.ai` or a wildcard. `jev.ai` serves a
registrar's parking page after a transfer on 2026-09-16. The default endpoint
named a host that has never served HTTPS, under a domain that is for sale.

Jev is TypeSafe AI's model. The vendor's documentation (docs.typesafe.ai/api)
and its SDK on PyPI both put System One at
`https://api.typesafe.ai/v1/systemone`. The key authenticates there.

The request was wrong too. Sent to the right host, what the client built came
back **422**: `model` is required, and `questions` is a map keyed by name, not a
list. The response had never been read: it is `answers` keyed by name, not a
`decisions` list. Neither could have been caught before, because nothing had
ever been sent.

## What the scorer is like

| | measured |
| --- | --- |
| billed overhead per request | ~256 tokens |
| billed per question | the statement's tokens + ~4 |
| legal prose | 4.5 characters per token (`estimate_tokens` assumes 4) |
| latency | 150–280 ms, flat from 2k to 60k characters of state |
| run-to-run noise | SD 0.01–0.02 mid-range, ~0.003 near 0 or 1 |

The last row matters most. Eight identical requests put one noul between 0.63
and 0.69. **Jev is not deterministic**, so a field within a few hundredths of
its threshold can be confirmed on one run and routed to review on the next,
and every live number below is one draw.

`check_context` stays conservative: it overcounts state by about 12%, which
covers the per-request overhead at the 32k ceiling.

## Calibration (steps 2 and 3)

`harness --calibrate --jev api -n 24 --version 6`: 24 synthetic documents,
1,958 requests, **$0.066**, five and a half minutes.

The first evaluation is the calibration run. With nothing fitted for
`jev-1.13.0` every field is triaged at the 0.80 default: 645 to 647 of 739
labelled fields confirmed across the three calibration runs made, 0 wrong in
each. That is not a measurement, and is not comparable to the stand-in's 693
confirmed on the same documents under its fitted v5.

On the synthetic corpus, validator A separates cleanly wherever it has data.
The lowest correct score in each class: baskets 0.88, covenant levels 0.96,
dates 0.85, parties 0.98. (The gate finds where it does not, on real filings:
text fields, below.) The fitted v6 set is in
`config/thresholds.jev-1.13.0.json`; like v5, almost nothing in it is
certified, and for the same reason: 24 synthetic documents contain too few
failures to constrain a 99% target.

### Validator E asks a question a literal reader answers wrongly

The stand-in never fired E on the synthetic corpus. Live Jev fired it on 22
fields, and all 22 are wrong:

- 19 × `consolidated_ebitda.addback_cap_pct`, asked *"The magnitude of this
  limit depends on the Sponsor Model, a document not contained in this
  agreement."* at 0.80–0.86. The cap — 20% to 35% — is written in the
  agreement. What depends on the Sponsor Model is the add-back itself, and the
  statement never says which limit "this limit" is.
- 3 × `incremental.leverage_based_test`, where the statement calls "the
  Schedule" *a document not contained in this agreement* — a premise asserted
  inside the question, and false: a schedule is part of the agreement.

A fired E sets the field `external_reference` and drops its value, which the
harness headline does not count, because it counts only `confirmed`. The
vendor's own list of failure modes names the pattern: "hiding several
judgments inside one question", and literal reading of the words written.
Rewording E is a validator change with offline consequences and is not made
here. What the fit does about it is below.

### The fit accepted every wrong claim it was shown

The first fit gave `E_external_dependency/covenant_levels` a threshold of 0.83
with held-out precision 0.000 — twelve claims, all wrong, all let through. Two
bugs, both fixed:

- with nothing correct in a class every candidate's Wilson bound is zero, and
  the tie went to the lowest threshold, the one that accepts everything;
- the bound was not zero: `centre - margin` leaves a positive rounding residue
  for 160 of the first 2,000 sample sizes (2.06e-17 at n=11), enough to make
  one permissive threshold look strongest.

Refit, both E classes sit at 1.000: E does not fire on baskets or covenant
levels against this scorer until its question can be answered. Refitting the
stand-in on the same samples is byte-identical before and after both fixes.

E's misfire also cost A its data. In the calibration run E overrode the 19
add-back caps, so they arrived at the fit as E samples, and
`A_span_support/covenant_levels` was fitted on the other 52 — all correct, all
at 0.96 or above — and set where every class with no failures is set, at its
lowest correct score: 0.96. With E off, those 19 caps are A's again, score
0.82–0.92, and go to review. v6 applied to its own corpus confirms 649 of 739,
0 wrong, against the stand-in's 693 under v5; those 19 are almost all of the
difference.

### A labelling circularity the fit inherits

All 13 of validator A's "wrong" economic-terms samples are `mfn_sunset`, at
0.51–0.73, and the values are right (12 months for "12 months after the
Closing Date"). The label carries `expected_status: confirmed`, and
`score_document` judges a field with an expected status on the status alone —
so a correct value the threshold rejected is labelled a wrong claim, and the
fit raises the threshold to reject it again. It biases only towards review,
never towards a silent error, and it predates this pass; it is recorded here
because v6's economic-terms threshold (0.77) is higher than the data behind it
warrants.

## Archetype dispatch (#35)

Measured in `docs/corpus_findings.md` under "What live Jev says about it". In
short: the three remaining silent errors are deterministic verdicts that never
reach the model; the classifier's own choice question would pick
`abl_revolver` for all three anyway; one noul about whose receivables secure
the facility separates them from real ABLs.

## The gate (step 4)

All 104 label files, 106 documents and 86 mutants, 612 assertions, against
live `jev-1.13.0` under v6: 105,276 requests, 2.48 million questions, 191.7
million billed tokens, **$8.05**, about 67 minutes on six processes. It ran in
two parts. The account's credits ran out 43 minutes into the first: from the
72nd label file on, every request came back HTTP 402, and 32 files did not
run. A label file either makes every Jev call or fails whole — nothing in the
pipeline swallows a scorer error; the two `except Exception` blocks guard
extraction-backend re-reads — so the 72 that completed stood, and once credit
was added the other 32 ran under the same code and thresholds. The merged run
reproduces the offline run's 106 documents, 86 mutants, 612 assertions and
notes exactly.

| | confident | wrong | silent-error rate | real only |
| --- | --- | --- | --- | --- |
| stand-in, v5 | 302 | 3 | 0.99% | 206 / 3 |
| **live jev-1.13.0, v6** | **425** | **54** | **12.71%** | 326 / 52 |

| split | offline | live |
| --- | --- | --- |
| holdout, 174 assertions | 75 confident, 2 wrong | 113 confident, 18 wrong |
| fit, 288 | 99, 1 | 181, 34 |
| contaminated, 50 | 32, 0 | 32, 0 |

Eight of eleven families are over budget. One draw of a non-deterministic
scorer, on thresholds fitted to synthetic documents. It still answers the
question the pass was for: **a scorer that can be confident made the pipeline
far more willing to assert, and a real share of what it asserted is wrong.**

### What got better

74 assertions moved from review to a confident right answer: 52 that the
stand-in had right and routed to review anyway, and 22 where the stand-in's
answer was wrong and the live one is right. Of the 19 governing-law answers
the stand-in left in review on a lexical technicality, **14 cleared**, 5 did
not, and no governing-law answer was confirmed wrongly. Where the two scorers
can be compared like for like, the live one is doing real work.

### What got worse

51 new silent errors, 49 of them on real filings. Every one traces to a
question or a threshold that was only ever exercised by a scorer that could
not be confident:

| mechanism | new errors |
| --- | --- |
| a value that is in the document, asserted empty | 20 |
| a wrong archetype, asserted | 18 |
| a wrong value, confirmed | 6 |
| an external reference, asserted absent | 3 |
| a conflict the label keeps open, closed | 2 |
| other status flips | 2 |

Across the 104 original documents, `absent_from_document` went from 297 to
1,414, `not_applicable_to_archetype` from 246 to 694, `conflicted` from 142 to
50 and `needs_review` from 4,844 to 3,283.

* **Archetype dispatch's model path.** When the vocabulary is silent,
  `detect_archetype` asks a `ChoiceQ` over the archetypes — with no "none of
  these" option — and accepts the top choice at 0.35, a threshold never
  fitted. The stand-in's distributions were flat and never reached it. Live
  Jev reaches it on a warrant, a registration rights agreement, press
  releases, an indenture, a note. A wrong archetype then marks applicable
  fields `not_applicable_to_archetype`, which is confident, so it also feeds
  the next row.
* **Validator C's absence claims.** v6's `C_negative_space/economic_terms` is
  0.27 — eight synthetic samples, all correct, so the fit set it at the lowest
  of them — and every other class sits at the 0.80 default. A field is
  declared absent when every chunk scores at least the threshold on "this text
  contains no X", and a grid a lexical reader cannot parse is exactly where a
  live one hedges. With the archetype cascade above, 20 fields whose labelled
  value is in the document were asserted empty; the gate does not record which
  of the two statuses carried each one.
* **Validator A's statement, on text fields.** Five of the six wrong values
  confirmed are the administrative agent, where the rules tier read a phrase
  — "Loan Documents", "Administrative Agent", "LC Disbursements", "Restricted
  Subsidiary". The registry describes the field as "the Administrative Agent",
  so A asks whether *the text supports a value of Administrative Agent for the
  Administrative Agent*, and a literal reader says yes above the 0.98 parties
  threshold. The statement never asks whether the value names an institution.
* **Conflict resolution** closes conflicts the labels keep open, among them
  the EPRT and Aspen closing dates.

The three silent errors in the offline baseline are unchanged. They are
deterministic — see "Archetype dispatch (#35)" above.

### What this does and does not say

It does not say live Jev reads worse than the stand-in; on validator A over a
cited number, and on governing law, it reads better. It says four questions and
the thresholds behind them were built against a scorer that could not be
confident, and the first pass against one that can finds every place where
confidence was never expected. That is what the handoff meant by a calibration
run. v6 does not turn it into a measurement: it was fitted on 24 synthetic
documents, which contain no warrants, no grids and no event-defined closing
dates, so it could not have learned to refuse them.

The next steps follow from the mechanisms, not the thresholds:

1. give archetype dispatch a way to say *none*; the "establishes" noul in
   `docs/corpus_findings.md` scores every non-credit document there at 0.15 or
   below;
2. fit C's thresholds on real filings — the gate's own labels — rather than on
   the synthetic corpus, which is all the harness can fit today;
3. make A's statement for a name ask whether the value names the party, and
   give `administrative_agent.legal_name` a description that says so;
4. reword validator E.

Each can now be measured: `family_report --gate --jev api --workers 6
--outcomes FILE` runs the whole gate in a little over an hour, against six in
one process, and writes every assertion's outcome, so two runs can be compared
assertion by assertion.

No assertion targets the three boolean fields, so whether "a value of True for
…" costs A any accuracy is not measured.

---

# The second pass: the questions rewritten

The first pass ended on four questions to fix. This is the pass that fixed
them, then measured again, on the same day, model and labels. Nothing in it is
a threshold change dressed up as a fix. Each change rewrites what a question
says, and each was measured live on the requests the first gate actually
sent, before the gate was run again.

The vendor's guidance is the design rule throughout: a literal reader answers
the words, so a question has to say exactly what it means. It should hold one
judgment, assume no premise, and never ask a negation when it means an
absence.

## What changed, and what each change measured

**Validator C asked the negation.** "This text contains no provision
addressing the highest Eurodollar Applicable Margin in the pricing grid" was
asked of every chunk. A literal reader agrees with that of any chunk that does
not use those words, however plainly the chunk states a margin over Term
SOFR. C now asks presence of each chunk ("this text contains a provision
addressing …"), and absence is one minus the strongest presence anywhere. The
margin is also described in the vocabulary agreements use now: "the highest
margin over Term SOFR, Eurodollar or another benchmark in the pricing grid
(the Applicable Rate or Applicable Margin)". Measured on the exact chunks C
sent:

| | old question | new question |
| --- | --- | --- |
| 15 margins the first gate asserted absent | absence 0.34–0.88 | 0.04–0.10 on 13; 0.30 and 0.59 on the other two |
| 19 fields the labels say are absent, at 0.80 | 7 confirmed | 12 confirmed |

The two exceptions are a BDC warehouse and Air T's note. The note borrows its
margin from an agreement the filing does not contain, so its label is
`external_reference`. Review is not that, but it is the safe side of it. So
the new question clears false absences and confirms more of the true ones.

**Validator A asked about a role, not a name.** The registry described the
administrative agent as "the Administrative Agent", so A asked whether the
text "supports a value of Loan Documents for the Administrative Agent". That
is true of any text that names the role. Party fields are now asked as names:
*In this text, "X" is the legal name of the institution acting as
Administrative Agent.* The 17 labelled correct names score 0.89–0.98. Six
phrases the rules tier lifted from beside a name score 0.02–0.31; the old
form gave them 0.16–0.97. The six are "Loan Documents", "Administrative
Agent", "The Other Borrowers Party Hereto" and three variants of "Certain
Subsidiaries".

**Validator E bundled two claims and a premise.** "The magnitude of this limit
depends on the X, a document not contained in this agreement" never said
which limit. It also assumed the document sits outside the agreement, which is
false of a schedule. It now names the field and makes one claim: *The amount
of {field} is set by the {document}, not stated in this text.* On 102
labelled cases, all 23 true externals score 0.81–0.94. Of the 79
non-externals, the old statement put 76 over 0.5; the new one scores them
0.02–0.51. The statement is declared an absence claim. That is how the
stand-in, which builds its score from the words the cited sentence contains,
reads it the right way round.

**Conflict resolution asked the wrong question and discarded its answer.**
"Which value does the cited text support?" cannot separate candidates that
were each extracted from their own citation. It now asks which value *is*
the field, and "none of these" is always an option. On the 53 labelled
conflicts from the first gate, the new question is right 51 times and the old
one 41. The cost is coverage: across all 142 conflicts, it declines on 66,
and those stay in review. The same audit found a bug no wording could fix. When
the model chose a candidate other than reconciliation's first, the field was
confirmed but kept the rejected value. Five administrative agents reached the
first gate's report that way. The chosen value is now applied. A numeric
overturn goes to review instead, because a candidate's string does not carry
its unit.

**Archetype dispatch asked every document what facility it establishes.**
All 18 archetypes the first gate got wrong came from that one question, whose
options were all facilities. It was asked of warrants, press releases and
notes. The model is now asked only when two archetypes' decisive vocabularies
tie, which is the case the design describes, and "neither" is always an
option. A document that is not an agreement, or says too little to classify,
is `unknown` without a request. The corpus has six ties. The three with an
archetype label all come out right: AGL's subscription line is
`nav_or_subscription` at 0.99, Cooper Standard is `abl_revolver` at 0.96, and
Bain's fund facility is *neither* at 0.72. The first gate had called Bain's
facility `nav_or_subscription`.

**And #35, which the first pass measured but did not run.** A deterministic
`abl_revolver` verdict is now checked with one noul: is the borrowing base the
borrower's own eligible receivables and inventory? It is asked of the
detection window plus the document's own Borrowing Base definition. Below 0.5
the verdict is withdrawn, and `unknown` rules nothing out. On the 26
documents the vocabulary calls an ABL:

| | own-receivables noul |
| --- | --- |
| the three #35 silent errors | 0.03–0.13 |
| fifteen fund, BDC, specialty-finance, securitisation and similar facilities | 0.03–0.32 |
| the eight corporate ABLs | 0.65–0.97 |

One of the fifteen is GBDC 4 Funding III, a BDC warehouse, and its label says
`abl_revolver`. Blue Owl's Athena Funding is the same kind of facility, and
its label says `unknown`, because an ABL verdict rules out the coverage tests a
warehouse is measured by. The two labels disagree; see below.

**One change is to the fit, not a question.** A class with no failures in it
used to be fitted to the lowest score it saw. That is how v6 set C's
economic-terms threshold to 0.27 on eight synthetic samples. Such a class now
keeps the 0.80 default whenever the fit would land lower.

The questions, the conflict fix, the dispatch and the fit are pinned
by `tests/test_question_design.py`. Each of its tests fails when the change it
pins is reverted.

## Offline

The stand-in answers the new questions too, and CI's gate is the offline one:

| | confident | wrong | silent-error rate |
| --- | --- | --- | --- |
| stand-in v5, old questions | 302 | 3 | 0.99% |
| **stand-in v5, new questions** | **294** | **0** | **0.00%** |

That is the first time the offline gate has passed with the whole corpus in
it. The three errors cleared are the #35 archetypes. It gives up five right
answers, all to review: Tailored Brands' ABL, whose vocabulary the stand-in's
lexicon misses (the live scorer keeps it at 0.89), and four GBDC assertions
that depend on its ABL label.

## Calibration

`harness --calibrate --jev api -n 24 --version 7`: 24 synthetic documents,
**$0.066**. Triaged at the unfitted 0.80 defaults, the calibration run
confirmed 664 of 739 labelled fields with none wrong; the first pass's
calibration runs confirmed 645–647. E fired on no field that was not already
an external reference, against 22 wrong fires under the old statement, so v7
has no E class and E runs at the default. C's economic-terms threshold is the
0.80 floor where v6 had 0.27. A's covenant-levels threshold falls from 0.96
to 0.84 because the 19 add-back caps E used to take are A's samples again
(see "E's misfire also cost A its data" above). The rest are close to v6's:

| class | v6 | v7 |
| --- | --- | --- |
| A / baskets | 0.88 | 0.90 |
| A / covenant levels | 0.96 | 0.84 |
| A / dates | 0.92 | 0.92 |
| A / economic terms | 0.77 | 0.77 |
| A / parties | 0.98 | 0.96 |
| C / economic terms | 0.27 | **0.80** |
| conflict choice / economic terms | 0.97 | 0.96 |
| E / baskets, covenant levels | above 1.0 (off) | default 0.80 |

As before, almost nothing is certified: 24 synthetic documents hold too few
failures to constrain a 99% target.

## The gate

It ran in two parts, like the first pass. The first 81 label files ran under
v7 through the scratch runner, which calls the same `run_coverage` as
`family_report --gate --jev api` and saves each file's outcomes as it
completes: 83 documents, 63 mutants, 474 assertions, 77,489 requests,
**$6.10**, 49 minutes. Then the account's credits ran out again. From the 77th
file on, every request came back HTTP 402 (`billing_error`). The denial was
reported, not retried. Once credit was added, the other 23 files ran under
the same questions and thresholds, and with the request savings described
below: 23 documents, 23 mutants, 138 assertions, 9,457 requests, **$1.01**, 7
minutes. The same files had taken 28,630 requests and $2.18 under v6.
Merged, the run reproduces the offline run's 106 documents, 86 mutants, 612
assertions and notes exactly. In total it cost **$7.11**, and it is one draw of
a non-deterministic scorer.

One assertion is dropped from every run below. It is the units mutant on
Tailored Brands' ABL amendment, which asserted an MFN threshold of 3.00 about
a document with no MFN provision. The generator had matched a phrase that
caps the separate term loan's rate. Live v7 said confidently there is no MFN
threshold, and the gate scored that right answer as a silent error. The
generator now requires an MFN comparison ("exceeds … by more than"), so the
gate has 611 assertions. Each mutant is its own pipeline run, so dropping one
moves nothing else.

| 611 assertions | confident | wrong | silent-error rate | real only |
| --- | --- | --- | --- | --- |
| stand-in v5, old questions | 302 | 3 | 0.99% | 206 / 3 |
| stand-in v5, new questions | 294 | 0 | 0.00% | 198 / 0 |
| live v6, old questions | 424 | 53 | 12.50% | 326 / 52 |
| **live v7, new questions** | **364** | **3** | **0.82%** | 268 / 3 |

**51 of v6's 53 silent errors are gone.** 23 now reach review with the right
answer, 24 with the wrong one, and 4 are now confidently right. One of the 4
is Janus Living's administrative agent, which v6 confirmed as "Loan
Documents" and v7 confirms as BANK OF AMERICA, N.A., through the conflict
fix. Half of the 24 are margins in pricing grids the rules tier cannot read.
v6 settled them as empty under a confident status, either absent or
inapplicable behind a wrong archetype. v7 leaves them in review, which is
where a value extraction did not find belongs. Vivid Seats' agent is still
read as "Restricted Subsidiary", but the name question no longer confirms
it. Two event-defined closing dates, KKR's and New Fortress's, now stay
conflicted.

**The three left are all literal readings.**

* EPRT's closing date. The *value* is right: 2018-06-25, the defined term,
  and the sibling value assertion passes. The status is `confirmed` where the
  label says `conflicted`. The label is a tripwire written to fire exactly
  here, and its note says it "should be replaced, not deleted" once the
  pipeline resolves to 2018-06-25 and confirms it. EPRT is held out, so the
  replacement is left for a deliberate decision, not made here after seeing
  the result.
* Evernorth's $30,000,000 original principal, asserted absent. C asks
  whether any chunk addresses "the aggregate principal amount of the Initial
  Term Loans", and a note purchase agreement has no Initial Term Loans. This
  is the field's description read literally, the same failure as the
  margin's. Evernorth is on the fit side.
* Sysco's filing on JRD Unico, new in v7. A footnote names three covenants
  of the acquired company, "EBITDA ratio, fixed charge ratio and incurrence
  of debt ratio", and states none of their levels. The label says
  `external_reference`, because the levels live in JRD Unico's note
  agreements. C asks whether the text addresses "the maximum leverage level
  under the financial covenant", and the literal answer is no. The presence
  form is confident enough to assert absence where the old negation hedged.
  C cannot say "named here, stated elsewhere". E could, but E only reads
  fields that carry a value.

**What it cost: 17 right answers moved to review.** Four are the GBDC
assertions behind its disputed ABL label. Eight are true absences the
presence form does not clear at 0.80: Athena's collateral vocabulary,
Constellation's and Evernorth's credit spread adjustments, excess cash flow
sweeps at Cooper Standard, Schneider and StepStone, Schneider's MFN, and the
fixture's MFN sunset. Four are correct administrative-agent names that v7
does not confirm. The likeliest reason is the 0.96 parties threshold fitted
on synthetic names: in the question experiment, the labelled correct names
scored 0.89–0.98. That is not measured here, because an outcome does not
record A's score. The last is one of Star Mountain's absences.

**Against the stand-in on the same questions,** live v7 turns 74 of the
stand-in's review answers into confident right ones: 52 it had right and 22
it had wrong. It sends 7 of the stand-in's confident right answers to review
and adds the 3 silent errors above. Governing law keeps the first pass's
gain: 64 of 69 confident and right, the same as v6, against the stand-in's
50.

Across the original documents, mutants excluded, the statuses moved like this:

| status | stand-in | live v6 | live v7 |
| --- | --- | --- | --- |
| `confirmed` | 291 | 363 | 326 |
| `not_applicable_to_archetype` | 119 | 694 | **182** |
| `absent_from_document` | 330 | 1,414 | **1,358** |
| `conflicted` | 142 | 50 | 83 |
| `needs_review` | 4,928 | 3,283 | 3,863 |

The archetype fix shows in the second row. The third row is **what this
gate does not measure.** Live v7 asserts absence four times as often as
the stand-in, nearly as often as v6. Of the labelled assertions, v7 settles
18 as empty and 16 of those are right; Evernorth's and Sysco's are the other
two. But most of the 1,358 are fields no label covers, and
`absent_from_document` is a confident status. It is now the largest body of
confident output the gate cannot score.

## Request volume

Three live runs made over 200,000 requests, and most of them repeated one
already made. Two mechanisms now stop that. Neither changes a question.

**Validators B and C shared chunks but not requests.** Both sweep the same
chunks: the union of the structural and sliding segmentations, which covers
the document about 2.4 times. B asks each chunk its five orphan questions.
C asked each chunk again, separately, whether it addresses each empty field,
and C was 47% of all requests. B now carries C's questions on its own
requests. A `JevSession` keeps every answer it buys, keyed on the model, the
state and the question exactly as sent, so C finds its answers already
bought and asks only what it lacks.

**Nothing was kept between documents or runs.** A mutant is its document
with one defect injected, so nearly all its chunks have been asked about
already, and mutants were 48% of all requests. Label files share documents.
Validator F's question about an empty field has the description as its whole
state, so it is identical for every document. A run cut short by a 402 lost
everything it had bought. `CachedJev` keeps answers in a SQLite file shared by
the gate's workers, `.cache/jev-answers.sqlite`, which is untracked. The eval
tools use it by default for the live scorer, and `--no-jev-cache` asks for a
fresh draw.

Measured on the offline gate, whose stand-in makes the same requests:

| | requests | tokens |
| --- | --- | --- |
| before | 108,071 | 185.1M |
| B and C share requests | 61,936 (−43%) | 125.3M (−32%) |
| and the answer cache, starting empty | 35,929 (−67%) | 88.2M (−52%) |

A rerun with the cache warm makes no requests at all. On five label files
(18 pipeline runs) the cold run made 1,929 requests, the warm rerun 0, and
the outcomes were byte-identical.

Live, on the 23 files the second gate finished with: 28,630 requests under
v6, 9,457 now (−67%), and $2.18 against $1.01.

Neither mechanism may change an answer. On the offline gate all 612 outcomes
and the status counts of all 190 pipeline runs are byte-identical with and
without them. Live, they rest on a question being answered independently of
the others in its request, which the batching has always assumed. That was
tested on 8 chunks of Sanmina's agreement, with 15 questions each. Asked
together with B's questions, the answers moved from the alone answers by a
mean of 0.0101. Asked alone twice, they moved by the same 0.0101, which is
the scorer's own noise.

What the cache gives up is a fresh draw. For mutants that is a gain, not a
loss: every chunk a mutation did not touch now gets the same answer as in the
original, so a difference between the two is the mutation's and not the
scorer's noise.

# The third pass: the thing, however it is named

The second pass left three silent errors, all literal readings. This pass
removed them and found a fourth of the same kind while doing it. Every fix
rewrites a presence question, which is what validator C asks of each chunk
before it may call a field absent. No field's description changed, and no
other validator's question did either.

## The three

**Sysco's footnote** names three covenants of the company Sysco acquired and
states none of their levels. C asked whether the text addresses "the maximum
leverage level under the financial covenant", and the literal answer is no. It
now asks whether the text contains a covenant capping debt against earnings,
or requiring a minimum coverage of interest or fixed charges, "whether or not
it states the required level". A BDC's asset coverage floor puts assets over
debt, which is neither, so the BDC labels that call such facilities
covenant-free still hold.

**Evernorth's notes.** C asked about "the aggregate principal amount of the
Initial Term Loans", and a note purchase agreement has none. It now asks about
"the term loans or notes funded at closing". A revolving commitment is not
funded at closing, so a revolver-only deal still reads as absent.

**EPRT's closing date** needed a label change, not a code change. The
held-out label was a tripwire, set to fire when the pipeline confirmed
2018-06-25. Its note said to replace it, not delete it, when that happened.
Both conditions were met, and the project owner asked for the remaining errors
to be fixed. So it now expects `confirmed`, and the note keeps its history.
That is a held-out label changed after seeing a result, and it is recorded
here as one.

## And a fourth

The gate on those fixes (v8, below) cleared all three and found another.
HealthStream's MFN protects incremental revolving commitments. C asked whether
the text addresses the MFN differential "that triggers repricing of the
Initial Term Loans". The clause scored 0.20, and on the units mutant the
threshold was reported absent beside it. v7 had left that one in review. That
made three fields read literally against one agreement's defined term, and the
gate catches such a reading only where a label happens to sit.

So every presence question whose description names such a term now asks
about the thing itself, "however named". That covers the MFN threshold, the
term loan's maturity, the revolving commitments and their maturity,
delayed-draw commitments, the letter of credit sublimit, the closing date and
the cost-savings add-back cap. With the two above, ten fields' presence
questions changed in this pass. Changing one field's question costs one new
request per chunk of every document where that field is pending. A
document's changed questions travel together, so the eight cost what one
would have. Offline the gate is unchanged: 293 confident and 0 wrong, with no
assertion moving.

## Calibration

Thresholds belong to the questions that produced the scores, so the set was
refitted twice: v8 after the first two question changes, v9 after the other
eight. Both calibration runs read their unchanged answers from the cache, and
v9's cost nothing. v8 and v9 came out identical, class for class. The
calibration run confirmed 667 of 739 labelled fields with none wrong, where
v7's confirmed 664.

| class | v7 | v8 and v9 |
| --- | --- | --- |
| A / baskets | 0.90 | 0.88 |
| A / covenant levels | 0.84 | 0.85 |
| A / dates | 0.92 | 0.88 |
| A / economic terms | 0.77 | 0.77 |
| A / parties | 0.96 | 0.96 |
| C / economic terms | 0.80 | 0.80 |
| conflict choice / economic terms | 0.96 | 0.95 |

## The gate

Both gates ran whole, on the 611 assertions every run shares:

| 611 assertions | confident | wrong | silent-error rate | real only |
| --- | --- | --- | --- | --- |
| stand-in v5, these questions | 293 | 0 | 0.00% | 197 / 0 |
| live v6 | 424 | 53 | 12.50% | 326 / 52 |
| live v7 | 364 | 3 | 0.82% | 268 / 3 |
| live v8 | 360 | 1 | 0.28% | 263 / 0 |
| **live v9** | **358** | **0** | **0.00%** | **262 / 0** |

What they cost says something about the cache:

| | requests | questions sent | tokens | cost |
| --- | --- | --- | --- | --- |
| live v7, before the cache | 86,946 | 2.47M | 169.2M | $7.11 |
| live v8 | 35,674 | 1.41M | 82.5M | $3.47 |
| live v9 | 35,049 | 0.23M | 56.3M | $2.37 |
| stand-in, from an empty cache | 35,929 | 1.88M | 88.2M | |

v8 ran nearly cold. The cache held only the 23 files v7 finished with, so
v8's saving over v7 is the B and C sharing. v9 had all of v8's answers and
sent only the rewritten questions, 84% fewer, but it made as many requests.
A changed presence question is asked of every chunk where its field is
pending, and the scorer is paid by the token, most of which are the chunk. A
question change costs about two thirds of a cold run.

**v9 is the first live gate with no silent error.** From v7 to v9:

* All three errors are gone. EPRT's is now confidently right, through the
  label change above. Evernorth's principal and Sysco's covenants now go to
  review, empty. Neither value is extracted, so review is where they belong.
* HealthStream's MFN mutant, wrong under v8, is in review.
* HealthStream's administrative agent, TRUIST BANK, is newly confirmed.
* Five right absences moved to review. Two are the covenant question's cost:
  Blue Owl's indenture and Evernorth's notes have no leverage covenant, and
  the broader question does not clear them at 0.80. One is the MFN
  question's. Compass's only "most favored nation" leaves MFN pricing on a
  future incremental facility to be agreed. Asked about an MFN provision
  however named, the scorer is no longer sure there is none. Constellation's
  two, an excess cash flow sweep and an MFN threshold, are questions no pass
  changed. Constellation's answers predate the cache, so v8 asked them again,
  and the new draws fell under the line.

The statuses across the original documents, mutants excluded:

| status | stand-in, v7 questions | stand-in, these | live v7 | live v9 |
| --- | --- | --- | --- | --- |
| `confirmed` | 291 | 291 | 326 | 325 |
| `not_applicable_to_archetype` | 119 | 119 | 182 | 182 |
| `absent_from_document` | 330 | 350 | 1,358 | **1,306** |
| `conflicted` | 142 | 142 | 83 | 84 |
| `needs_review` | 4,928 | 4,908 | 3,863 | 3,915 |

The rewritten questions took a net 52 absences off live Jev's row, seven of
them the labelled ones above. The stand-in, which counts words, moved the
other way by 20, none of them labelled. Live Jev still asserts absence
nearly four times as often as the stand-in, and most of those claims fall on
fields no label covers, so they remain the largest body of confident output
the gate cannot score.

# Out of sample: twenty BDC agreements

Everything above was measured on documents the parser, the questions and the
thresholds were built against. The frozen holdout keeps part of the corpus
out of the fit, but it was read while the parser was written, and every
question rewritten in these passes was rewritten against the whole corpus.
Twenty more agreements were harvested to measure what that is worth.

## The set

These are credit facilities filed on EDGAR by business development companies,
from twenty manager families the corpus did not hold: AB Private Credit, AMG
Comvest, Barings, Blackstone, Capital Southwest, Fortress, Goldman Sachs,
Kennedy Lewis, LGAM, Lafayette Square, MidCap, New Mountain, North Haven,
Oaktree, Overland, PGIM, Sixth Street, Stellus, Vista and Willow Tree.

A BDC is identified by its Investment Company Act file number, not by
vocabulary. Each filer's own EX-10 exhibits that mention a borrowing base,
filed 2023–2026, were searched, and one whole agreement was kept per filer.
Eleven are the BDC's own senior secured credit facility, revolving and in
two cases with a term loan beside it. Eight are financing facilities of a
special-purpose subsidiary, secured by the loans it holds.
One is a subscription line. Most arrive as an amendment carrying the full
conformed agreement, which is how BDCs file them.

They are in `corpus/real/bdc`, on the split's `out_of_sample` list, and
labelled in `credit_extract/eval/labels_out_of_sample`. CI's gate does not
read that directory, because a set tuned until it passes is no longer out of
sample. `family_report --out-of-sample` runs it on purpose.

## Labelled blind

The labels were written from each document's normalized text by readers who
did not run the pipeline or read anything it produced. They were committed
and pushed (3298bb2) before the pipeline first ran on any of these
documents.

There are 168 assertions over ten fields: the borrower, the agent, governing
law, the revolving commitment and its maturity, the top margin, the floor, the
commitment fee, the financial covenant and the closing date. Each quotes the
text it came from. A derived value, such as a maturity reached through four
definitions and a business-day step, gives its chain. A field the text does
not settle was skipped rather than guessed.

Of the eighteen financial covenants, fifteen are labelled absent. Those
agreements test asset coverage, a borrowing base, equity or liquidity, not
debt against earnings. Three have an interest coverage covenant, and its level is
labelled. Two labels follow the labelling guide over the brief the readers
were given. MidCap and New Mountain state the sum of their Dollar and
Multicurrency tranches, and the guide names exactly that case: take the
larger tranche, and do not sum. New Mountain's margin is left out. It is
priced by lender class, and for a per-tranche term that differs the guide
gives no deal-level value.

## The result

Thresholds v9, questions and code as they were, no refit:

| 168 assertions | confident | wrong | right, in review | wrong or empty, in review |
| --- | --- | --- | --- | --- |
| stand-in v5 | 45 | 0 | 15 | 108 |
| **live v9** | **59** | **0** | 6 | 103 |

**No confident answer on a labelled assertion is wrong, offline or live.**
Live turns 17 of the stand-in's review answers into confident right ones.
Twelve of the 17 are agents, five of which the stand-in had read wrongly.
It moves 3 right answers the other way. It cost **$0.69** for 6,072
requests, five minutes across six workers. The stand-in, which makes the same requests,
predicted $0.68.

Coverage is lower than in sample. Live v9 settles 35% of these assertions,
where on the corpus's real assertions it settles 51%. By field:

| field | assertions | confident and right | in review |
| --- | --- | --- | --- |
| governing law | 20 | 19 | 1 |
| administrative agent | 20 | 16 | 4 |
| borrower | 20 | 11 | 9 |
| floor | 18 | 8 | 10 |
| closing date | 11 | 5 | 6 |
| revolving maturity | 19 | 0 | 19 |
| financial covenant | 18 | 0 | 18 |
| revolving commitment | 16 | 0 | 16 |
| top margin | 14 | 0 | 14 |
| commitment fee | 12 | 0 | 12 |

The parties and governing law carry over. The economic terms do not, and not
because of Jev. The rules tier extracts no value at all for any of the 61
commitments, maturities, margins and fees, so there is nothing for a
validator to confirm. Why was not investigated, because investigating it on
these documents would tune against them. It is not only depth: MidCap's
maturity is a bare date in its own definition, and it was missed too. This
is the extraction-recall problem the README ranks second, now measured on
documents nobody tuned for.

The covenant row is the safe side of a gap. The three agreements with an
interest coverage covenant score lowest on absence, 0.03 each. They are
escalated as a value the extractor missed, which is what they are. The
fifteen without a leverage covenant score between 0.08 and 0.53, so C never
reaches the 0.80 it needs to call one absent. None is called present
either. The wrong values in review are five closing dates, each another date
from the agreement's history than the one the label chose, and Willow Tree's
borrower cut to "SPV1, LLC".

## What the labels do not cover

A labelled set cannot score a confident claim about a field it does not
label, and live Jev made 188 of them here:

* 9 confirmed values;
* 169 absences;
* 10 fields ruled out by an archetype.

The stand-in made 26 absences and no archetype claims. These were checked
against the text after the run, which makes this an audit, not a blind test.

* **The 9 values are all right.** They are five collateral agents, two
  syndication agents and two 0.10% SOFR adjustments, each where the text puts
  it.
* **Two absences are wrong in substance.** Barings' amendment is signed by
  "ENERGY HARDWARE HOLDINGS, INC., as Subsidiary Guarantor", and its preamble
  defines that party. `guarantor.legal_name` was confirmed absent at 0.85
  across 309 chunks. Sixth Street's preamble names its two: "SSLP LENDING,
  LLC and SIXTH STREET LP HOLDING II, LLC (the " Subsidiary Guarantors")".
  The first version of this audit filed Sixth Street with the next five. A
  guarantor truth table written later from the text caught it.
* **Five more are the wrong status.** AB Private Credit, Blackstone, Capital
  Southwest, Fortress and PGIM define a Subsidiary Guarantor as "any
  Subsidiary that is a Guarantor under the Guarantee and Security Agreement"
  and name none. C asks whether a chunk addresses "the legal name of each
  Guarantor", and the literal answer is no. The names are in another
  document, which the guide calls `external_reference`, and a label would
  say so. This is Sysco's case again: C cannot say "named here, stated
  elsewhere".
* **The other 162 absences hold.** Most fields have none of their
  vocabulary anywhere in the document. None of the twenty mentions a most
  favoured nation clause, for instance. Where the vocabulary does appear, the
  passages checked describe something else: PIK loans in the collateral,
  portfolio companies' leverage and add-backs, an accordion with no leverage
  test, or boilerplate.
* **The ten archetype exclusions suppress nothing.** None of the ten fields
  has a value in its document. The verdicts behind them are another matter.
  Blackstone's and Oaktree's revolvers came out `abl_revolver` at 0.70. The
  own-receivables question is meant to stop exactly that. It withdrew the
  other 15 ABL verdicts here at 0.03–0.49, but scored these two 0.54 and
  0.70. In sample, every fund facility had scored 0.32 or less. Fortress's
  revolver and two SPV facilities were called `nav_or_subscription` on tied
  vocabulary, and the one real subscription line, Overland's, was called
  `unknown`. No label here covers an archetype, so none of this is scored.

Counted strictly, as a label would count it, 7 of the 188 are wrong (3.7%);
in substance, 2. Either way, all seven errors are one field and one kind:
guarantors reported absent, a field no label in the corpus covers. The
absence claims are the place to look next on any new set of documents.

These findings are recorded, not fixed. A fix made against these twenty
documents would make them one more fit set. The fixes belong on in-sample
documents that show the same thing. Then this set is run once more, and after
that a fresh one is harvested.

# The fourth pass: guarantors

The out-of-sample audit found one kind of confident error: guarantors
reported absent. It also said a fix belonged on in-sample documents first.
This pass is that fix, measured in sample, and then the twenty BDC agreements
run once more.

## Labels first

No label covered `guarantor.legal_name`. Five readers labelled it on the 90
in-sample agreements, from the text alone and without running the pipeline.
They wrote 86 labels and skipped 4 the text does not settle:

* 55 name the guarantors;
* 4 point to another document, 3 to a guarantee or similar agreement and 1
  to a schedule the filing left out;
* 27 say there is none.

A role several parties fill is labelled with a list of names, and any one of
them is a right answer. The labelling guide has a new section on it, and
`field_value` now accepts a list.

A sixth reader wrote the same table for the twenty BDC agreements: 5 named,
6 external, 9 absent. It stays out of the blind set, because it was written
after the first run, and the rerun is scored against it.

Against the in-sample labels, live v9 had 16 guarantors confidently right,
all of them absences, and 68 in review. Two were wrong:

* IDEX's Company guarantees its co-borrowers under "ARTICLE X. CONTINUING
  GUARANTY";
* Valvoline defines its Guarantors.

Both were called absent.

## Why

There were two causes.

* **Nothing read a guarantor.** Every other party had a rule; this one did
  not. So the field was empty on every document, and validator C was asked
  whether it was absent.
* **C's question cannot see guarantors.** Asked whether a chunk addresses
  "the legal name of each Guarantor", a literal reader says no to a
  definition that names nobody. It says no to a signature block too, which
  is not a provision.

## What changed

* **The rules tier reads a guarantor named in its role:** "X, as a
  Guarantor", "as Subsidiary Guarantor", "as Parent Guarantors". The role
  must be capitalized. In lower case the phrase describes someone else's
  guarantors: Camping World's definition of its floor plan facility lists
  "subsidiaries of Freedomroads, LLC, as guarantors".
* **The rule rejects three kinds of non-name:**
  * a placeholder such as "THE GUARANTORS PARTY HERETO";
  * a name that opens on the previous name's suffix;
  * a candidate that ends a longer candidate's name, which is a fragment
    cut at a chunk boundary.

  Several guarantors make several candidates, and conflict resolution
  chooses between them.
* **Validator C may not call the guarantor absent where the text
  establishes guarantors.** That means any of:
  * a guarantor named in its role;
  * a signature block headed GUARANTORS;
  * a defined guarantor role;
  * a guarantor defined where it is introduced;
  * a guaranty article.

  None of it settles the field. It only keeps a false absence out.
* **Tried and dropped: sending guarantors that a definition hands to a
  guarantee agreement to `external_reference`.** That is right for five of
  the BDC agreements. It was wrong on all seven in-sample agreements it fired
  on, because each names its guarantors somewhere a pattern does not see:
  * an uncaptioned signature block;
  * a party the definitions themselves make a guarantor (Holdings, the
    Parent, the Company);
  * a borrower that signs "as KBR, a Borrower and a Guarantor".

  A wrong `external_reference` is a silent error, so those fields go to
  review instead.

The only new questions are about guarantors, so the live gate read nearly
every answer from the cache.

## Results

**In sample**, on the 86 guarantor labels. These figures measure fit: the
rules were written while reading these texts, the held-out ones among them.

| | confident and right | wrong | in review |
| --- | --- | --- | --- |
| live v9, before | 16 | 2 | 68 |
| **live, after** | **23** | **0** | 63 |

The 16 absences are the same 16 as before. The 7 additions are named
guarantors, confirmed:

* Accelevation's INSTOR BLOCKER, INC.;
* BlackRock Monticello's trust, in both filings;
* Hornbeck Offshore Operators;
* Janus Living;
* Lumber Liquidators Leasing;
* Schneider National Carriers.

None was confirmed until the field's description changed from "the legal
name of each Guarantor" to "of a Guarantor". The registry holds one name, so
asking whether one name is "the legal name of each Guarantor" gets a literal
no wherever there are two, and validator A scored every real guarantor it
was shown between 0.06 and 0.79. C's presence question keeps the old words.
It no longer decides guarantors, and its answers stay bought.

The whole live gate has 697 assertions, 381 confident and 0 wrong. The 611
assertions every earlier run shares come out exactly as under v9, 358
confident and 0 wrong. Each run cost under a cent. Offline, the stand-in
that CI runs passes at 304 confident and 0 wrong: the same 293 as before
and 11 guarantors.

**Out of sample**, the twenty BDC agreements were run once more. Nothing in
this pass was developed on them, but the guarantor truth table they are
scored against was written after the first run. So this is an audit, not a
second blind test.

| | confident and right | wrong | in review |
| --- | --- | --- | --- |
| guarantors, before | 5 | 7 | 8 |
| **guarantors, after** | **7** | **0** | 13 |

* All seven wrong absences are gone. Barings' ENERGY HARDWARE HOLDINGS, INC.
  is confirmed, and so is Lafayette Square's LS BDC HOLDINGS, LLC. Sixth
  Street's two, defined in its preamble, are in review. So are the five
  agreements whose definitions hand their guarantors to the Guarantee and
  Security Agreement, which were called absent before.
* Absences fell from 169 to 162, and the 162 are the ones the audit found
  to hold.
* The 168 blind labels come out as before: 59 confident, 0 wrong.
* Two truth labels are judgment calls and both are in review:
  * New Mountain, whose schedules leave no subsidiary able to be a
    guarantor;
  * Kennedy Lewis, whose only guaranty is a non-recourse carve-out.

# The fifth pass: economic terms on fund facilities

The out-of-sample run read none of the 61 commitments, maturities, top
margins and commitment fees the twenty BDC agreements' blind labels carry.
Every rule for those fields had been written against the synthetic fixture's
drafting, and fund facilities write differently. `docs/bdc_extraction_plan.md`
set out the order: labels first, in sample; readers written against the fit
documents; measure in sample; then the twenty BDCs once.

## Labels first, twice

The five fields were labelled on the twenty in-sample fund and BDC
agreements by two independent sets of readers, from the text, before any
reader for them existed. Of the 100 (document, field) slots the sets agreed
on 77 and both skipped 17. In 4 only one set wrote a label. In the last 2
the sets differed, both times on whether a payment-day clause elsewhere rolls
a weekend maturity. The brief counts only the definition's own roll, so the
unrolled dates stand.

60 assertions are new. Four existing labels on these fields were wrong:

* **BlackRock Monticello, both facilities.** The old labels said the Stated
  Maturity Date is not defined in the text, so the maturity has no answer.
  It is defined ("July 30, 2030"; "June 1, 2029"). The Maturity Date's
  self-referencing limb is real and harmless.
* **Eagle Point.** The old label said the maturity chain ends at events. Its
  last dated link is a Commitment Termination Date that survives only fused
  with the date it replaced: "NovemberJune 1226, 20262028". The label is now
  `needs_review`, the output a fused figure should get, and the fused
  commitment ("$60,000,00075,000,000") gets the same.
* **Star Mountain.** The old label said the size is not stated. It stopped at
  "Maximum Facility Amount"; "Aggregate Commitments" states $185,000,000,
  written "$ 185,000,000". An adjudicator settled it, since this is a
  holdout document.

The rules the disagreements needed are now in the labelling guide.

Against the code before this pass, the 81 assertions on these fields scored
12 confidently right, 1 wrong and 62 in review. The wrong one was a standing
bug: KKR states its unused rate (0.65% at 50% usage or less, 0.45% above),
and the fee-letter route called the fee external because the agreement also
has a Fee Letter for other fees.

## The readers

`extract/economics.py` holds the readers. Each takes a value only from the
place the agreement designates as settling the term, and only in a shape that
fixes it. Every other shape is a decline, which leaves the field open for
review.

They were written against the 14 fit and contaminated fund agreements, and
each names the one whose drafting it is for. The six holdout agreements were
measured, not read. One leak is worth recording: the labellers' reports
described some holdout drafting. No reader was written for a form seen only
there.

The near misses mattered more than the forms:

* **Commitment.** The reader takes the figure from size definitions in the
  shapes that fix it:
  * "the lesser of (a) $X and (b) the aggregate Commitments";
  * "(a) $X plus (b) New Commitments";
  * "(a) prior to the end of the Revolving Period, $X";
  * "the aggregate Commitments as then in effect, which amount shall not
    exceed $X (as such amount may be increased)".

  It follows a size definition that hands off to another one. With tranches,
  it takes a total the agreement prints in a Total row, or else the largest
  revolving tranche; it never sums them. It declines on:
  * an accordion ceiling ("may be up to", "may be increased to");
  * a figure fused with the one it replaced;
  * a term loan agreement, which states its commitments in the same words.
* **Maturity.** The reader resolves the agreement's maturity term over the
  definition graph:
  * "the earliest of" keeps its dated limbs and ignores its events;
  * "the date that is N years after" and "the Nth anniversary of" are added;
  * "the first Business Day on or after" is rolled;
  * "the last day of" a defined period is read;
  * "the date of this Agreement" is the cover date.

  The first maturity term the agreement defines decides, resolved or not.
  The Commitment Termination Date and the ends of the revolving,
  reinvestment and funding periods are never reached. A chain that ends at an
  event, a fused date or another document resolves to nothing. A date reached
  through references cites the definition that writes it. A computed one
  carries its route and stays in review (below).
* **Top margin.** The reader takes the top level of the margin schedule. It
  sets aside the base-rate margin, the default increment (including one
  inside a parenthesised proviso, and one whose proviso cites "clause (i) or
  clause (ii)"), and a rate the operative amendment superseded. It declines
  on:
  * an increment it would have to add (KKR's LTV step-up);
  * a schedule that sends part of its pricing to a table;
  * a term tranche priced beside a revolver (Latham, below);
  * a collateral loan's "Spread" (Eagle Point).

  A margin given by a fee letter is an external reference.
* **Commitment fee.** The reader takes the top tier of an unused, non-usage,
  non-utilization or undrawn fee, with its thresholds set aside. It also
  reads the BDC revolver's prose "commitment fee, which shall accrue at a
  rate per annum equal to X% on the average daily unused amount". An upfront
  "Commitment Fee" charged on the whole facility is not the unused fee. A
  rate in a fee letter, given by definition, "is defined in" or "in the
  amounts set forth in", is an external reference.
* **Floor.** The reader takes "zero (0)" with no unit, and a floor written
  into the benchmark's own definition: "the greater of (x) three percent
  (3.00%) per annum, or (y) the Term SOFR Reference Rate", or "shall at no
  time be less than 0.0%".

Measuring found two confident wrong answers, both now fixed:

* **Ares CP Funding's margin** came back `external_reference`. Validator E
  extends a span to the next ". ", and the definition ends "per annum ." with
  a space, so the window ran into "Approval Notice ... attached hereto as
  Exhibit A". A span that is a whole definition is now its own window.
* **Latham's margin** came back 4.00%, the answer its label names as the one
  to avoid. That is the term loans' flat rate; the revolver's grid is a table
  the filing omits.

The readers also run on every corporate agreement in the corpus. Outside the
fund set they confirm Air T's and Health Catalyst's margins, and Hornbeck's
and Valvoline's floors. They put seven corporate maturities into review with
the right value, three of them springing maturities read at their
unconditional date. Every value they produce on an unlabelled development
document was checked against its text.

## Computed maturities and a literal reader

Validator A asks whether the cited text supports a value. For a computed
maturity no text states the date, so a live experiment ($0.0002) asked Jev
about six computed maturities in two ways. Each was asked about the right
date and two near misses: a year later, and the last dated link.

* **Against the maturity definition alone,** which is what A sends, Jev
  cannot tell them apart: 0.12–0.19 for all three.
* **Against every definition in the chain,** it usually prefers the right
  date: 0.51–0.73, against at most 0.35 for the near misses. But nothing
  reaches the 0.94 date threshold. On the three-hop GBDC chain it rated the
  date a year late (0.35) above the right one (0.32).

So computed maturities stay in review, with the route in a `derived`
qualifier. A literal reader confirms what is written. Arithmetic is
Python's, and so far nothing checks it independently.

## Results in sample

**The live gate, 757 assertions:** 425 confident and 0 wrong, 525 passed.
On the 693 assertions it shares with the fourth pass's gate, that is 387
confident, 0 wrong and 470 passed, against 381, 0 and 452. Nothing that
passed before fails now. The offline stand-in that CI runs passes at 340
confident and 0 wrong (304 and 0 before). Every run read nearly all its
answers from the cache: the live gates cost under a cent each.

**The 81 assertions on these five fields, live:**

| | confident and right | wrong | right, in review | missed |
| --- | --- | --- | --- | --- |
| before this pass | 12 | 1 | 0 | 62 |
| **after** | **47** | **0** | 18 | 10 |

Six more pass that assert no value or review: three null labels, and three
`needs_review` labels met by review, the fused figures among them.

| field | confident and right | wrong | right, in review | missed |
| --- | --- | --- | --- | --- |
| commitment | 7 | 0 | 4 | 3 |
| maturity | 3 | 0 | 10 | 2 |
| top margin | 7 | 0 | 4 | 2 |
| floor | 15 | 0 | 0 | 2 |
| commitment fee | 15 | 0 | 0 | 1 |

By split side, the fit agreements score 34 right, 13 more in review and 2
missed. The six holdout agreements, never read while the readers were
written, score 11 right, 1 in review and 8 missed, none of them wrong.

Floors and fees clear the plan's bar: confidently right on most in-sample
fund facilities that state them. Commitments and margins reach about half.
Maturities fall short. Their values are right, but a computed date cannot
clear a literal reader, and a stated one scored 0.74–0.87 against the 0.88
bar for dates.

One change moved those scores. Validator A asks about each field by its
description, and three descriptions named a facility fund agreements do not
have: "the maturity date of the Revolving Credit Facility", "the aggregate
Revolving Credit Commitments" and "the highest margin ... in the pricing
grid". They now name the thing however it is named, as the third pass did
for absence questions. That confirmed five more right answers, put one back
in review and made nothing wrong. The thresholds were not refitted.

## Out of sample: the twenty BDCs, once

Nothing in this pass was developed on them. They were run once, on the code
that measured in sample, against their blind labels.

| | confident and right | wrong | right, in review | missed |
| --- | --- | --- | --- | --- |
| economic terms, fourth pass | 0 | 0 | 0 | 61 |
| **economic terms, now** | **28** | **0** | 10 | 23 |
| floors, fourth pass | 8 | 0 | 0 | 10 |
| **floors, now** | **12** | **0** | 0 | 6 |

The 61 economic labels split as:

* commitments: 5 right, 5 in review, 6 missed of 16;
* maturities: 8 right, 4 in review, 7 missed of 19;
* top margins: 9 right, 1 in review, 4 missed of 14;
* commitment fees: 6 right and 6 missed of 12.

All 168 blind assertions: 91 confident and 0 wrong, 107 passed, against 59, 0
and 65 in the fourth pass. The run cost $0.002.

The confident claims no label covers were audited as before. On these five
fields there are seven:

* AMG Comvest's floor, '"Floor" means zero';
* five margins and fees whose definitions hand the rate to a fee letter;
* Lafayette Square's commitment, $75,000,000.

The first six are right. The last is wrong. It is the conformed copy's "as of
the Effective Date" total of an amendment that, in its own words, increases
the Maximum Commitment. In an amendment, a stated total now counts only when
it is dated as of an amendment's or restatement's own date. No in-sample
document changes. Like every fix after a run, it was informed by these
twenty, which is why the next measurement uses a fresh set.

## A fresh out-of-sample set

The plan's last step: twenty investment-grade credit agreements, a segment
the corpus holds few of. They were found by pricing keyed to the borrower's
debt ratings ("Index Debt", "Public Debt Rating"), are whole EX-10 credit
agreements filed in 2025 and 2026, and come from companies the corpus does
not hold. The set includes Target, Uber, CVS, Illumina, ICE, Celanese and
Puget Energy, and two real estate investment trusts.

The labels were written from the text alone by readers who did not run the
pipeline, and were committed and pushed (8ac263c) before anything ran on the
set. There are 179 assertions in `credit_extract/eval/labels_out_of_sample_ig`,
on the same ten fields as the BDC set. Every quotation in every note was
checked against the text. Three things to know when reading the results:

* **One is not a revolver.** Easterly Government Properties' agreement is a
  term facility: "The Borrower shall not have the right to reborrow". Its
  revolver commitment and maturity are labelled not applicable, with the
  term facility's figures as the near misses. It stays in the set as an
  out-of-sample test of the term-loan guard.
* **Eight of the nineteen commitment-fee labels are null.** Seven of those
  agreements charge a facility fee on the whole commitment, drawn or not,
  and no unused fee; each note quotes the facility fee as the answer not to
  give. The eighth is Easterly's, which charges no fee on unused
  commitments; its extension fee is the near miss.
* **Three filings print no total commitment** and omit the schedule that
  has one: Celanese, Uber and Avnet. Celanese's is labelled an external
  reference, and the other two are left unlabelled.

Nothing in this pass has run on the set.

# The sixth pass: what validator A can confirm

The fifth pass ended with three things to do. Run the investment-grade set
once. Fit validator A's thresholds for dates and economic terms on real
labels. Check computed maturities some other way than asking a literal
reader about a date no sentence states. The second and third were done in
sample first, so that the one run of the investment-grade set measures all
three.

## Fitting validator A on real filings

A's thresholds for dates and economic terms, 0.88 and 0.77 in live v9, were
fitted on the synthetic corpus. A never met a wrong date or economic term
there, so each threshold was the lowest score a right value happened to get.
On the real in-sample filings the labels give the positives: 104 labelled
values A asked about, all but two of them right. Two failures cannot place a
threshold. So `eval/realfit.py` asks A about wrong values on purpose, in A's
own words, in A's own request and against the text A was shown:

* **in-text**: the other dates, percentages and amounts in that text, nearest
  the right one first. This is the reader's plausible mistake: the right
  clause, the wrong figure.
* **off-text**: the right value moved to figures the text does not contain:
  a year either way, a quarter point more, $25 million more. A misparse or a
  slip in arithmetic produces those.

| live score | dates | economic terms |
| --- | --- | --- |
| right values (labelled) | stated 0.67–0.99 | 0.49–0.98 |
| wrong values in the text | at most 0.43 (9 asked) | up to 0.96; 11 of 54 at 0.49 or more |
| values not in the text | at most 0.09 (73 asked) | at most 0.45 (234 asked) |

So:

* **Values not in the text:** A rejects them.
* **Wrong dates the text does contain:** A rejects almost every one.
* **A wrong economic term from the right clause:** A does not reliably reject
  it. The ones that get through are figures the field's own question does not
  rule out:
  * lower tiers of a fee grid;
  * a default rate;
  * a utilization threshold.

Each class was fitted on the fit side three ways, with the repository's own
`fit_threshold`:

1. on the labelled values alone;
2. with the off-text values added;
3. with the in-text ones added too.

A threshold is adopted only where all three agree.

* **Dates: 0.80** in all three fits, so v10 lowers it from 0.88.
  * No wrong date of either kind clears it.
  * It confirms 15 of the 25 labelled right dates, where 0.88 confirmed 9.
  * Four dates no label covers clear it too, and each was checked against its
    text:
    * Accelevation's revolver maturity, twice. It is cited from the term
      loan's definition, which states the same day.
    * Martin Marietta's facility termination date.
    * The Meridian fixture's revolver maturity.
* **Economic terms: kept at 0.77.** The fits gave 0.49, 0.49 and 0.94.
  * The labelled values alone would lower it to 0.49, which confirms every
    right value.
  * It would also let through nearly three times as many in-text near misses:
    11 of 54 against 4.
  * Whether that costs anything depends on how often the reader picks a wrong
    figure from the right clause. In sample it did so twice in 104, and A
    caught both. That is too few to set a threshold on.

The first version of this fit came out the other way, and it was wrong.

* **The bug:** each wrong value's question was built from a copy of the field
  with the value replaced. A field's value lives on its primary variant, so
  every "wrong" question asked about the right value again.
* **What it looked like:** A appeared unable to tell a right figure from any
  other, and values not in the text scored 0.98.
* **How it showed:** one question sent by itself.
* **The fix:** `validators.support_statement` now builds A's question from any
  value, and a test pins it to A's own question word for word. The probe also
  refuses a wrong value whose question equals the right one's.

## Computed maturities, premise by premise

No sentence states a computed maturity, so A's question about it has no
literal answer. The six computed fund maturities scored 0.06–0.07, right or
wrong. But each step of the computation is stated, in the definition it came
from. The resolver now writes each step down as a sentence that definition
can be checked against. For PIMCO:

> The text defines the Closing Date as June 1, 2026.
>
> The text defines the Final Maturity Date as the earliest of the 4-year
> anniversary of the Closing Date and the other dates and events it lists.

How the check works:

* **The cover date:** "The date of this Agreement" adds the cover's date as a
  premise, asked against the cover.
* **The check:** a new step, `validator_a_premises`, asks each premise against
  its own definition after A turns the value down.
* **The bar:** the date is confirmed only if every premise clears A's date
  threshold.
* **The arithmetic:** it stays in Python, where it is exact and tested.

Asked live beside wrong versions of each one, the premises separate cleanly:

| | live score |
| --- | --- |
| the true premises of the six fit-side computed maturities | 0.86–0.99 |
| wrong versions (listed below) | 0.01–0.22 |

The wrong versions were:

* a year off;
* a count one off;
* the next ordinal;
* "latest" for "earliest";
* "preceding" for "on or after";
* a period ending a day early;
* the wrong defined term.

One wrong version scored 0.93, and it was not wrong: it named the Collection
Date, which is another limb of that same definition.

## Results in sample

**The live gate, 757 assertions:**

| | confident | wrong | passed |
| --- | --- | --- | --- |
| fifth pass (thresholds v9) | 425 | 0 | 525 |
| dates refitted (v10) | 431 | 0 | 525 |
| **and premises** | **437** | **0** | **525** |

* **Nothing lost.** Nothing that passed before fails now.
* **Every change is a right answer leaving review:** six dates under v10, and
  the six computed fund maturities by their premises.
* **Nothing unlabelled.** No field without a label is confirmed by premises.
* **The offline stand-in CI runs is unchanged** at 340 confident, 0 wrong
  and 503 passed. It confirms no premise at its own date threshold of 0.94.

The new answers cost $0.0003.

**The 81 assertions on the fund fields:**

| | confident and right | wrong | right, in review | missed |
| --- | --- | --- | --- | --- |
| fifth pass | 47 | 0 | 18 | 10 |
| **now** | **57** | **0** | 8 | 10 |

* **Maturities** went from 3 confidently right to 13.
* **Commitments and margins** are where they were. Their right answers in
  review sit under the economic-term threshold, which stayed.

## The investment-grade set, once

The twenty investment-grade credit agreements labelled blind in 8ac263c were
run once, on 3644a1f, which carries both changes above. They are a segment
the readers were not written for: revolvers priced off the borrower's
ratings, from companies the corpus did not hold.

| of 179 blind assertions | confident | wrong | right, in review |
| --- | --- | --- | --- |
| live | 68 | 4 | 4 |
| offline stand-in | 45 | 2 | 19 |

The live run cost $0.54. This is the first out-of-sample run with confident
wrong answers. The stand-in's two (Celanese's fixed-charge minimum read as
its leverage cap, Dynatrace's agent read as "Loan Document") are its own;
live gets both right.

Running the same twenty through the fifth pass's code and through v10, in
worktrees and almost entirely from the cache, separates this pass from what
was there before:

| code | confident | wrong |
| --- | --- | --- |
| fifth pass (thresholds v9) | 61 | 3 |
| dates at 0.80 (v10) | 67 | 4 |
| **and premises** | **68** | **4** |

* **The refit** confirmed five more right maturities: Cboe, Enterprise
  Products, Franklin, Illumina and Teradyne.
  * It also confirmed one wrong one: Easterly Government Properties'
    2028-08-21, scored 0.82, between the old bar and the new.
  * That date is right for Easterly's term facility, and Easterly has no
    revolver. The refit's stress set held wrong dates. It held no right date
    for the wrong kind of facility.
* **The premise check** confirmed one more right maturity, ICE's
  2031-08-20, and nothing wrong.
* **Three errors were already there** under the fifth pass's code:
  * **Athene's borrower.** Four borrowers share one "as Borrowers", and the
    rule took the last.
  * **Avnet's leverage covenant.** It came back as 5.00, the first row of a
    relief-period step table. The standing level, which the labelling guide
    asks for, is 4.00.
  * **Puget Energy's maturity.** It came back as May 18, 2031, a Sunday,
    which the definition itself moves to the preceding Business Day, May 16.

Per field, live:

| field | confident and right | wrong | right, in review | missed |
| --- | --- | --- | --- | --- |
| borrower | 11 | 1 | 0 | 8 |
| agent | 16 | 0 | 2 | 2 |
| governing law | 17 | 0 | 2 | 1 |
| commitment | 1 | 0 | 0 | 17 |
| maturity | 9 | 2 | 0 | 8 |
| top margin | 1 | 0 | 0 | 18 |
| floor | 5 | 0 | 0 | 15 |
| commitment fee | 0 | 0 | 0 | 11 |
| covenant | 0 | 1 | 0 | 18 |
| closing date | 4 | 0 | 0 | 1 |

All eight null commitment-fee labels pass: no facility fee was reported as
an unused fee.

The parties, governing law and maturities carry over; the economic terms do
not. Nearly every miss is a value not read at all, rather than a wrong one:

* **Margins** are priced from ratings grids printed as tables.
* **Commitments** are stated in schedules.
* **Floors** are written as "if Term SOFR would otherwise be less than zero,
  Term SOFR shall be deemed to be zero". That is the form of 12 of the 15
  missed floors, and the fund floor reader does not read it.

The confident claims no label covers were audited as before, on the fields
that matter: 27 values and absences. 20 are right, 3 arguable and 4 wrong.

* **Three of the four wrong** repeat Athene's error:
  * the last of seven syndication agents, twice;
  * the last of six arrangers.
* **The fourth wrong one** is Globe Life's guarantor, called absent. Its
  Section 10.19 makes the borrower guarantee its co-borrower.
* **The arguable three** state a real figure more broadly than it applies:
  * a one-time fronting fee read as a rate;
  * ICE's credit spread adjustment, which applies only to its one
    non-consenting lender;
  * Uber's springing guarantee.

## Fixed after the run

Three of the misreadings were general, and are fixed in f6417b1 and
c44e2f0:

* **The term-facility guard** reads "shall not have the right to reborrow"
  (Easterly).
* **A definition's own business-day convention** is applied (Puget). A date
  it moves becomes a computed one, with the convention as a premise.
* **The party rules decline a role in the plural**, of whose list they can
  only see the last name (Athene, and the three audited).

The covenant table and Globe Life's guaranty are recorded, not fixed.

* **In sample:** nothing changed. Live is 437 confident, 0 wrong and 525
  passed; offline is 340, 0 and 503.
* **Out of sample:** replayed with the fixes, the twenty come to 66
  confident and 1 wrong, Avnet's covenant. Puget's maturity is now
  confirmed by its premises. Athene's borrower, Easterly's maturity and the
  three audited parties are in review.

That replay is not a measurement. Every fix was written against these twenty
documents, and for those rules they are now a fit set, as the BDCs became for
the amendment-date rule. The next measurement of them needs fresh documents.

# What is next

1. **Read the investment-grade economics the readers miss.** Measure them on
   a fresh set. They are:
   * the zero floor written as "deemed to be zero", which is 12 of the 15
     missed floors;
   * ratings grids printed as tables, for margins and fees;
   * commitments stated in schedules.
2. **Settle the covenant level.** The field's question asks for the level "as
   it stands at a given date", while the labels take the standing level, and
   Avnet's relief table is where the two part. Choose one, then read step
   tables to it.
3. **Fit the economic-term threshold.** It rests on how often the reader
   takes a wrong figure from the right clause, which nothing measures yet.
   Labelling a sample of the values A is asked about would.
4. **Add a near miss to the date stress set:** a right date for the wrong
   kind of facility. That is the one error the date refit let through.
5. **Ask about the commitment fee by its top level,** as the margin's question
   does. Lower tiers of a fee grid clear A today: PennantPark's 0.25% scored
   0.96.
6. **Harvest a fresh out-of-sample set.** The investment-grade set has been
   run once, and fixed against since.
7. **Decide the labels the second pass exposed:** GBDC's `abl_revolver`
   against Athena's `unknown`.
8. **Let validator E, or a status C can reach, say "named here, stated
   elsewhere".** That was Sysco's case. And read a borrower's guaranty of its
   co-borrower, which was Globe Life's.
9. **Fit A's parties threshold on real names.** `eval/realfit.py` can do it
   with the other names in the text as its near misses. Then label a sample
   of the corpus's unlabelled `absent_from_document` claims.
10. **Put the key in CI for a scheduled live gate.** With the answer cache,
    a gate after a code change costs cents. Until then, CI measures the
    stand-in, which passes at 340 confident and 0 wrong.
