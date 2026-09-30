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

## The gate, on 81 of 104 label files

The gate ran under v7 through the scratch runner the first pass used. It calls
the same `run_coverage` as `family_report --gate --jev api`, spreads label
files over six processes, and saves each file's outcomes as it completes. 81
label files finished: 83 documents, 63 mutants, 474 assertions, 77,489
requests, **$6.10**, 49 minutes. Then the account's credits ran out again.
From the 77th file on, every request came back HTTP 402 (`billing_error`), and
23 files did not run. They cost $2.18 under v6. The denial was reported, not
retried. So everything below compares runs on the 81 files every run
completed, and it is one draw of a non-deterministic scorer.

| on the same 81 files | confident | wrong | silent-error rate | real only |
| --- | --- | --- | --- | --- |
| stand-in v5, old questions | 242 | 3 | 1.24% | 167 / 3 |
| stand-in v5, new questions | 235 | 0 | 0.00% | 160 / 0 |
| live v6, old questions | 335 | 42 | 12.54% | 258 / 41 |
| **live v7, new questions** | **284** | **2** | **0.70%** | 209 / 2 |

**40 of v6's 42 silent errors are gone.** 21 now reach review with the right
answer, 18 reach review with the wrong one, and one is now confidently right.
That one is Janus Living's administrative agent, which v6 confirmed as "Loan
Documents" and v7 confirms as BANK OF AMERICA, N.A., through the conflict fix.
Ten of the 18 are margins in pricing grids the rules tier cannot read. v6
settled them as empty under a confident status, either absent or
inapplicable behind a wrong archetype. v7 leaves them in review, which is
where a value extraction did not find belongs. Two event-defined closing dates,
KKR's and New Fortress's, now stay conflicted, where v6 confirmed a date for
each.

**The two left are both literal readings, and neither is a simple fix.**

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
  is the field's description read literally, the same failure as the margin's.
  Evernorth is on the fit side, so rewording the description is allowed. It
  is not done here because it would need another gate to measure.

**What it cost: 13 right answers moved to review.** Four are the GBDC
assertions behind its ABL label. Five are true absences the presence form
does not clear at 0.80: Athena's collateral vocabulary, Constellation's and
Evernorth's credit spread adjustments, Cooper Standard's excess cash flow
sweep, and the fixture's MFN sunset. Four are correct administrative-agent names
that v7 does not confirm. The likeliest reason is the 0.96 parties threshold
fitted on synthetic names: in the question experiment, the labelled correct
names scored 0.89–0.98. It is not measured here, because an outcome does not
record A's score.

**Against the stand-in on the same questions,** live v7 turns 53 of the
stand-in's review answers into confident right ones, 39 it had right and 14
it had wrong. Governing law keeps the first pass's gain: 49 of 54 confident
and right, the same as v6, against 37 for the stand-in.

Two families are over budget, F05 (EPRT) and F06 (Evernorth), one error
each. Across the original documents of the 81 files the statuses moved like
this:

| status | stand-in | live v6 | live v7 |
| --- | --- | --- | --- |
| `confirmed` | 235 | 288 | 259 |
| `not_applicable_to_archetype` | 72 | 534 | **126** |
| `absent_from_document` | 286 | 1,183 | **1,139** |
| `conflicted` | 107 | 39 | 65 |
| `needs_review` | 3,825 | 2,475 | 2,938 |

The archetype fix shows in the second row. The third row is **what this
gate does not measure.** Live v7 asserts absence four times as often as
the stand-in, nearly as often as v6. Of the labelled assertions, v7
settles 14 as empty and 13 of those are right; Evernorth's is the other. But
most of the 1,139 are fields no
label covers, and `absent_from_document` is a confident status. It is now the
largest body of confident output the gate cannot score.

## What is next

1. Run the 23 files that did not run, which needs roughly $2.30 of credit.
   Then merge, as for the first pass.
2. Decide the two labels this pass exposed and did not touch: GBDC's
   `abl_revolver` against Athena's `unknown`, and EPRT's tripwire.
3. Label a sample of the unlabelled `absent_from_document` claims. That is
   where live v7's unmeasured confident output lives.
4. Reword `initial_term_loan.commitment` so a note's principal is addressed
   (fit side), and fit A's parties threshold on real names.
5. Put the key in CI for a scheduled live gate. Until then, CI measures the
   stand-in, which on these questions passes at 294 confident / 0 wrong.
