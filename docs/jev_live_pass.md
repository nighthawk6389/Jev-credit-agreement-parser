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
* **One absence is wrong in substance.** Barings' amendment is signed by
  "ENERGY HARDWARE HOLDINGS, INC., as Subsidiary Guarantor", and its preamble
  defines that party. `guarantor.legal_name` was confirmed absent at 0.85
  across 309 chunks.
* **Six more are the wrong status.** AB Private Credit, Blackstone, Capital
  Southwest, Fortress, PGIM and Sixth Street define a Subsidiary Guarantor as
  "any Subsidiary that is a Guarantor under the Guarantee and Security
  Agreement" and name none. C asks whether a chunk addresses "the legal name
  of each Guarantor", and the literal answer is no. The names are in another
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
in substance, 1. Either way, all seven errors are one field and one kind:
guarantors reported absent, a field no label in the corpus covers. The
absence claims are the place to look next on any new set of documents.

These findings are recorded, not fixed. A fix made against these twenty
documents would make them one more fit set. The fixes belong on in-sample
documents that show the same thing. Then this set is run once more, and after
that a fresh one is harvested.

# What is next

1. Decide the labels the second pass exposed: GBDC's `abl_revolver` against
   Athena's `unknown`. It now has out-of-sample company: two BDC revolvers
   pass the own-receivables question, and the corpus has no BDC profile to
   send them to.
2. Let validator E, or a status C can reach, say "named here, stated
   elsewhere". That was Sysco's case. Out of sample it is six guarantor
   fields, and the same question also missed a named guarantor at Barings.
   Guarantors need in-sample labels before either can be measured.
3. Extraction on BDC facilities. The rules tier found no commitment,
   maturity, margin or fee in twenty agreements.
4. Fit A's parties threshold on real names, and label a sample of the
   corpus's unlabelled `absent_from_document` claims, as the audit above did
   for these twenty.
5. Put the key in CI for a scheduled live gate. A question change costs
   about two thirds of a cold run, so a gate after one is about $2–4. Until
   then, CI measures the stand-in, which passes at 293 confident / 0 wrong.
