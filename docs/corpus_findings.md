# What 100 real agreements say about this pipeline

A scan of `corpus/edgar` (100 SEC exhibits, 18 strata, every harvest target
met) before running any of them through the pipeline. Counts are documents
containing the feature, out of 100.

The corpus is unlabelled. Nothing here is an accuracy measurement — it is a
description of what the market actually writes, set against what the code
assumed it writes. Every row where those two disagree is a defect, and the
ones below were all confirmed against the source text.

## Confirmed defects, and what they cost

### The credit spread adjustment check was firing on agreements that have none

| | documents |
| --- | --- |
| price off Term SOFR | 71 |
| …of which name an adjustment at all | 31 |
| …of which state one with a figure | 12 |

`csa_not_silently_zero` assumed CSAs are near-universal on SOFR facilities.
They are not. Most post-2023 agreements fold the spread into the margin and
quote the bare rate, for which **zero is the correct reading**. The check was
firing on ~40 documents that are correctly drafted — the loudest check in the
suite and the least informative, which is how a reader learns to skip the
section it prints in and then skips it on the one document where it was right.

Narrowing it to "names an adjustment" was not enough either: every agreement
written since 2022 carries benchmark-transition boilerplate promising a spread
adjustment *if* SOFR is ever replaced. It now fires only where an adjustment is
stated with a figure and no value came back.

Two agreements define **"Term SOFR Credit Adjustment Spread"** — a CSA under a
name the pattern did not match, which read as a deal with no adjustment rather
than as one that was not found.

### Base rates stopped at SOFR

| benchmark | documents |
| --- | --- |
| Term SOFR | 71 |
| Daily Simple SOFR | 49 |
| Prime | 46 |
| ABR / Alternate Base Rate | 35 |
| SONIA | 19 |
| EURIBOR | 18 |
| CDOR / CORRA | 16 |
| TIBOR | 6 |
| LIBOR (legacy) | 6 |
| SARON | 2 |

A fifth of the corpus is multicurrency and all of it came back `unknown`,
which in a report reads the same as a document that could not be parsed.

### Floors are drafted structurally, not in a sentence

15 agreements state their floor only inside the benchmark's own definition:

> **"Term SOFR"** means … the greater of (i) 0.25% and (ii) the Term SOFR
> Reference Rate…

A pattern that only reads "shall not be less than X" reports these as having no
floor. Because the floor sits inside the benchmark definition it is by
construction a **base-rate** floor, which is the distinction that matters: at a
SOFR of 0.05% with a 1.00% floor and a 5.00% margin, a base-rate floor yields
6.00% and an all-in floor yields 5.05%.

### Eighteen of the hundred are not credit agreements

Found by trying to label one. The smallest held-out document, chosen because
it could be read end to end, turned out to be a **Stock Transfer Agreement** —
a share-for-debt exchange that mentions a term loan credit agreement and is not
one. Checking the rest:

| what it actually is | documents |
| --- | --- |
| financial statements | 3 |
| earnings press release | 2 |
| Stock Transfer Agreement | 2 |
| indenture (supplemental, and a CLO indenture) | 2 |
| Warrant, Registration Rights, Investor Rights, Equity Purchase, Placement Agency | 5 |
| Note Purchase Agreement (convertible PIK note) | 1 |
| Amendment to a Sale and Servicing Agreement | 1 |
| Business Acquisition Report | 1 |
| proxy statement | 1 |

**18 of 100**, spread across nine strata, 15 on the fit side and 3 in the
holdout. The harvest selected on filing metadata and stratum keywords, which
is how a proxy statement that discusses a credit facility ends up filed as
one.

This does not invalidate the corpus — it is still 82 real agreements and
amendments across 18 strata — but it changes three numbers that get quoted.
The holdout is 24 usable documents, not 27. Any recall figure averaged over
the whole corpus is diluted by documents where every field is correctly
absent. And "100 stratified credit agreements" should read "100 stratified
filings, 82 of them credit agreements or amendments to one".

A classifier is not the fix. The fix is a reviewed list, because the
distinction is a judgement — a Note Purchase Agreement for a PIK note carries
real debt terms and still is not a credit agreement, and no keyword settles
that.

### The field registry's anchors are named after the fixture

`initial_term_loan.maturity_date` anchors on the phrase **"Initial Term Loan
Maturity Date"**, which is what the synthetic Meridian fixture calls it. Across
the corpus:

| defined term | documents |
| --- | --- |
| Latest Maturity Date | 14 |
| Term Loan Maturity Date | 9 |
| Latest Term Loan Maturity Date | 6 |
| Stated Maturity Date | 6 |
| Revolving Credit Maturity Date | 5 |
| Final Maturity Date | 4 |
| …40 further spellings | |
| **Initial Term Loan Maturity Date** | **2** |

**44 distinct defined terms end in "Maturity Date"**, and the one the registry
anchors on is in 2 documents out of 103. Health Catalyst writes simply
`" Maturity Date ": July 16, 2029`, so the rule cannot fire and the field
routes to review — the F10 assertion it fails is measuring exactly this.

This is the mechanism behind "recall collapsed from roughly thirty fields of
thirty-eight to between zero and three". It is not a missing anchor; it is a
tail with 44 members and no upper bound, on one field, and the same shape
repeats for margin, floor and commitment. Adding the forty-fifth spelling is
the work this project decided not to do.

### "DIP" decides nothing

`DIP Financing` appears in **36** documents. **Three** are DIP facilities. The
rest carry it in intercreditor boilerplate about what happens if the borrower
files. An archetype detector keyed on the phrase would misclassify 33
agreements. Decisive signals are section 364 and the interim order.

## The largest finding: blacklines are how the market amends

| | documents |
| --- | --- |
| say verbatim "hereby amended to delete the stricken text" | **32** |
| mention stricken or double-underlined text | 43 |
| prose restatement ("amended and restated … as follows") | 6 |

A blackline describes none of its changes in prose. It attaches a marked-up
copy of the agreement and says *take out what is struck through, keep what is
bold and double-underlined*. Flattened to plain text both survive, adjacent, in
reading order — the facility size in one real amendment reads

```
Up to U.S. $ 2,150,000,000 2,250,000,000
```

and a first-match parser reports the **deleted** figure off a span that quotes
the document accurately. Nothing about the output looks wrong.

This was built from a single document on the assumption it was an oddity. It
is the dominant amendment convention in the corpus, and prose restatement — the
only form the pipeline originally handled — is the rare one.

### How amendments are actually phrased

Counting what follows "is hereby amended":

| phrasing | occurrences |
| --- | --- |
| and restated in its entirety | 24 |
| to delete the stricken text | 18 |
| in its entirety to read | 6 |
| as follows | 5 |
| and restated as follows | 3 |
| ~13 further one-off forms | 13 |

The original patterns required "to read as follows" — which appears in **2**
documents. And targets are deep: 80 of 100 use identifiers like
`2.1(a)(ii)(B)(3)`, against 86 using any subsection at all.

## What this opens up

Three blind spots the register had recorded as permanently untestable now have
real examples, and are marked `partially_lifted` rather than closed:

| entry | what the corpus has | what stays untestable |
| --- | --- | --- |
| `F03_fee_letter` | 52 agreements reference a Fee Letter, 44 say "separately agreed" | no filing states the number, so a resolved value is wrong by construction |
| `F06_nav_or_subscription_line` | stratum H: 6 fund-level facilities | they are unlabelled; a NAV line misread as a cash-flow term loan is still invisible |
| `F06_unitranche_with_aal` | 9 refer to an AAL or first-out/last-out | the economics live in the AAL, which is not a borrower exhibit |

Four archetypes the corpus contains and the model did not: **DIP**, **venture
debt**, **investment grade revolver**, **European LMA**.

## What is still missing

Labels. 100 documents establish that the pipeline runs and what it claims; they
cannot establish whether any claim is right. The families the coverage report
calls undersampled are undersampled until these are labelled, and the honest
reading of every accuracy number in this repository is still that it was
measured on documents written by the same hand that wrote the parser.

## Archetype dispatch has a measured ceiling, and it is low

The deterministic classifier was returning `unknown` on about 60% of the
corpus, including **0 of 12** sponsor-backed deals and **0 of 6** broadly
syndicated term loans — the two strata that are plainly cash-flow term loans.
Four configurations were measured against the 62 corpus documents whose
stratum implies an archetype:

| configuration | correct | **confidently wrong** | honest `unknown` |
| --- | --- | --- | --- |
| as shipped (all signals read in the first 30k) | 17 | 14 | 31 |
| supporting signals read document-wide | 25 | 21 | 16 |
| …and "borrowing base"/"advance rate" made supporting | 21 | 18 | 23 |
| …and late decisive vocabulary vetoing the residual | 17 | 14 | 31 |

Every configuration trades correct answers against confidently wrong ones at
roughly one for one. **None is an improvement**, and for this field the trade
is worse than neutral: an `unknown` archetype leaves every field applicable,
while a *wrong* one marks applicable fields `not_applicable_to_archetype` and
suppresses real terms silently.

The diagnosis underneath is structural, not a missing keyword:

* **Decisive vocabulary read early is too sparse.** `DETECTION_WINDOW` is
  30,000 characters, sized for a 16,000-character fixture. Real agreements run
  to 500,000 and beyond, and most do not announce their type on the cover in
  the words we look for.
* **Decisive vocabulary read late is too noisy.** Over the whole document
  `second_lien` fires on 19 of 30 instead of 3, `european_lma` on 19 instead
  of 0, `investment_grade` on 13 instead of 0. Every secured deal mentions
  second liens in its subordination clause; every agreement defines a debt
  rating and calls its lenders the Majority Lenders.
* **Using late vocabulary as a veto instead of a classifier fires on
  everything**, because essentially every agreement contains some decisive
  phrase for some archetype somewhere.
* **"Borrowing base" is not decisive** by this module's own criterion — a
  decisive phrase appears in one kind of deal and nowhere else, and corporate
  ABL, BDC warehouses and subscription lines all have borrowing bases. Held
  decisive it called four of six fund-level facilities corporate revolvers.
  Made supporting, stratum C collapsed from 6/8 to 0/8, because the phrases
  that *do* distinguish a corporate ABL — eligible accounts, eligible
  inventory — live in the borrowing-base definition, deep in the document.

The distinguishing vocabulary is late; the shared vocabulary is early. No
anchoring rule separates them, which is why the ceiling is where it is.

Classifying a 900,000-character credit agreement by deal type is a semantic
judgement, and it belongs in the model tier — where the escalation already
routes it. `detect_deterministic` returning `unknown` hands off to the model
path today; that path is served by `OfflineJev`, a lexical stand-in, which is
why it does not help here. This is a credential-shaped gap, not a pattern one,
and the numbers above are the argument against trying to close it with more
keywords.

## What happened the first time a model tier's output ran through the pipeline

There is no `ANTHROPIC_API_KEY` in this environment, so the tier meant to do
the hard extraction had never run, and every recall number here measured
anchored patterns. That was recorded as a blind spot and treated as a caveat.
It was not a caveat. Nothing downstream of extraction had ever been fed a
model's answers, and it showed.

The reading was made by hand, checked in as JSON with a verbatim quote for
every value, and replayed through the identical path by `--backend recorded`:
Essential Properties Realty Trust's Eighth Amendment, stratum I, held out by
the frozen split, 522,738 normalized characters, read before any label for it
existed. 24 fields, every quote locating in the document.

The first replay did not produce a report. It raised
`decimal.InvalidOperation` three frames inside the range invariant, because
`bool` is `int` in Python and `Decimal("False")` raises rather than returning
anything. Three registry fields are booleans and no run in the project's
history had populated one. **Any populated boolean field took the whole report
down.**

Behind that, four more, each of which had been invisible for the same reason:

* **A covenant written as a percentage was reported as no covenant.** The
  Consolidated Leverage Ratio covenant is "60%" — a REIT covenant is debt over
  asset value, not a multiple of EBITDA — and the field is typed as a ratio.
  Coercion returned `None`, reconciliation flattened the candidate to "passes
  reported the field present but produced no value" and dropped the span it
  cited, and the negative-space check wrote "the extractor probably missed it"
  over the top. The extractor had quoted the section correctly. A value the
  record cannot hold now keeps its citation, carries the written form, and is
  not eligible to be confirmed absent.
* **The review queue printed a value for fields the pipeline refused to
  resolve.** `closing_date` has eleven candidates at equal weight on this
  document — the amendment date, the restatement date, the defined Closing
  Date and eight recited amendment dates — and the queue read
  `closing_date = 2019-11-26` three characters after the word `conflicted`.
  Conflicted fields now print their competing values and no single one.
* **A base rate spread was filed as the Eurodollar margin.** The text
  pricing-grid parser assumes two margin columns. Investment-grade grids have
  four — revolver and term loan, against SOFR and base rate — and it named the
  last one the Eurodollar margin: 0.550% into a criticality-5 field at 0.93
  confidence, outranking every other tier, where the answer is 1.550%. It now
  reads the rows and declines to name a column it cannot identify.
* **An invariant had an unexamined case.** `citations_cite_a_value` holds that
  a span on a valueless field is not a citation of anything, which was written
  from evidence: the offending spans were whole chunks of 8,379 characters and
  up, against 162 for any real citation. The covenant above cites 112
  characters and states a value the field cannot carry. That is a third case,
  alongside external references, and it is now exempt.

On the 19 assertions labelled for that document the deterministic tier passes
10 and the replay passes 17, with no silent error either way. **That is not a
measurement of model recall and must not be quoted as one** — the same reader
wrote the reading and the labels, so agreement between them is
self-consistency. It measures something else, which nobody had measured: given
correct extractions with correct citations, does the machinery carry them to a
correct record. Before this week, on five counts, it did not.

The ordering that makes such a number mean anything is enforced rather than
documented. A recording carries `recorded_before_labels`, CI fails on one that
claims otherwise, and the honest thing to do with a reading made after the
labels is to throw it away.

### Where the corpus knowledge actually lands

Everything above is a fact about how these agreements are drafted, and the
deterministic tier is the wrong place to put most of it. Anchoring a rule on
"the top row of a pricing grid" or "the first branch of a defined term" is how
the wrong column got reported as the Eurodollar margin in the first place: the
patterns are cheap and exact on the easy fields and silently wrong on the hard
ones, which is the whole reason the escalation ladder exists.

So the findings are written into the extraction prompt, as rules with the
drafting that motivates them. Twenty-four of them, in five groups — what counts
as a value, which value when the excerpt offers more than one, how to write it
down, when not to write one, and what belongs in notes rather than in the
number. Each carries its example verbatim, because a rule stated abstractly
gets skimmed and the drafting is the part that generalises to the next
agreement:

* `"Term Loan Maturity Date": (a) with respect to the Initial Term Loans, ...`
  — never take the first branch because it is first.
* `that certain $300,000,000 Revolving Credit Agreement, dated as of June 25,
  2018` — a figure in a recital describing a prior agreement is not this
  agreement's, and it can sit a few lines from the $1,300,000,000 that replaced
  it.
* `September 16 15 , 2026 2027` — if a blackline survives ingestion, the
  operative value is the replacement.
* `"22.5 bps", not "22.5"` — a grid quoted in basis points against a field
  measured in percent is a hundredfold error that reads as an ordinary number.
* `may be a positive or negative value or zero` — benchmark replacement
  machinery is drafting against the day the benchmark is discontinued, not a
  spread the facility pays today. That distinction is what made the credit
  spread adjustment check fire on forty correctly-drafted agreements.
* `the 180th day after the Fifth Amendment Effective Date` — an agreement's
  date is not the date it became effective, so arithmetic from it is a guess
  with a calculation in front of it.

The tests in `tests/test_prompts.py` assert the drafting, not the rule. A
rewrite that keeps "watch out for superseded figures" and drops the recital is
the rewrite that stops working, and it should fail.

Two defects in the request itself turned up while writing them, both of the
never-run kind:

**The prompt and the schema were not the same contract.** `EXTRACTION_SCHEMA`
goes to the API as `output_config.format`, so the response is constrained to
it. The prompt showed a bare JSON array where the schema requires a `fields`
object, and asked for a `qualifiers` key that the schema's
`additionalProperties: false` forbade — a 400 on one side, a silently dropped
instruction on the other, and `qualifiers` is what carries the measurement
convention a bare number does not. Three properties also sat outside
`required`; every documented structured-output schema lists all of them and
makes the optional ones nullable instead. A test now compares the two key sets
directly.

**The system prompt was never sent.** `EXTRACTION_SYSTEM` was defined in the
prompts module and no request ever passed it, so the one instruction framing
the whole task — a confident wrong answer is worse than no answer — reached the
model in no request this repository has ever built.

## A second held-out reading, and four more defects

Aspen Technology / Emerson Electric, stratum P, December 2022. Bilateral
acquisition financing between a company and its controlling shareholder: one
lender, no administrative agent, no arranger, no revolver, $630,000,000 drawn
once and amortised over twenty quarters. Picked because F07 was the worst row
in the table at 3 pass / 6 fail, and it carries the complete post-LIBOR
waterfall.

The first document found five defects. This one found four more, and none of
them needed the labels — all four were visible from reading the source and
running the pipeline over it.

### A heading style that made a whole agreement look structureless

Every heading pattern in `detect_sections` anchors to the start of a line. Some
filers' HTML puts the entire agreement in one flow, with no line break before
any heading. On this document that meant **1 section detected out of 198
present**. Three consequences, all silent:

* every cross-reference in the document was unresolvable, so the F01 check
  produced **56 errors on a perfectly well-formed agreement** — the kind of
  false-positive rate that teaches a reader to skip the section;
* the structural segmentation, one of the three independent views the pass
  planner relies on, **collapsed to a single chunk**;
* `section_hints` in the field registry had nothing to point at.

Case is what separates a heading from a reference in that house style: the
headings read `SECTION 2.07. Repayment of Loans` and the references read
`Section 2.07`. In this filing all 198 upper-case occurrences were headings and
all 239 references were mixed case, with no overlap either way. An inline rule
gated on the anchored patterns having already failed takes the count to 102
sections and 114 structural chunks, and the violations from 60 to 13 — of which
the two remaining cross-reference errors are real (`Section 18.1` points at an
Article this agreement does not have, and `Section 2.1` is a typo for `2.01`).

### A percentage that became a dollar amount

`parse_money("1.250% of the initial principal amount")` returned
`Decimal("1.250")`. The regex found the first number and never looked at the
unit. Amortisation is routinely drafted as a percentage of initial principal
per instalment — it is drafted that way here, four times over — so the field
would have carried a quarterly payment of **one dollar twenty-five** against a
real first instalment of **$7,875,000**, with a citation behind it. That is the
shape of a silent error, not of a miss. A percentage is no longer an amount.

### An invariant that had outgrown its own premise

`citations_cite_a_value` held that a span on a valueless field cites nothing.
It was written from evidence: the offending spans were whole chunks, 8,379
characters and up, against 162 for any real citation. Two things had happened
since. The chunk-wide hints moved to `review_hint`, so they no longer reach
`spans` at all. And a model tier began producing the opposite case — a field
with no value and a short, real quotation that is precisely the evidence for
having none:

```
"Maturity Date" means, with respect to each Facility, the date that is
five (5) years after the Funding Date.
```

A reader following that span gets exactly the passage that explains the empty
field. The check now tests the thing that separated the two cases all along and
states it rather than inferring it: a stored span must be short enough to be a
quotation. The original regression still fires; an evidenced absence no longer
does. It had **no test at all**, which is why the change broke nothing.

### Tier 4 had never run

The escalation ladder documents a targeted re-read that fires only on chunks
the orphan sweep flagged. `run_pipeline` accepted a `reread` callable,
`rescue_orphans` used it, `build_reread_prompt` existed — and **no caller ever
supplied one**. The tier had never executed on any document. It also discarded
what it found: the candidates were used to mark the orphan "rescued" and then
dropped, which is the expensive half of the work and none of the useful half.

It is wired now, with a default that re-asks the extraction question over one
flagged chunk with the fields that are still empty, and rescued values reach
the record at `needs_review` — never confirmed, because one chunk and one pass
has none of the independent support the main passes are built to produce, and
never over a value the passes already agreed on.

Under the deterministic backend it recovers nothing, on any document, and that
is the measurement rather than a disappointment: `orphan re-read (tier 4): 94
chunk(s) flagged, nothing recovered` says that ninety-four passages of this
agreement say something the pattern set cannot explain, and now says it in the
report instead of leaving the tier's absence to be mistaken for a clean sweep.

### What the document itself says

On its twelve assertions the deterministic tier passes 8 and the checked-in
reading passes 12. The two fields that make it worth labelling are the two with
no answer:

| field | what the agreement says |
| --- | --- |
| `closing_date` | *the date on which the conditions specified in Section 4.01 are satisfied* |
| `initial_term_loan.maturity_date` | *the date that is five (5) years after the Funding Date* |

The Funding Date is itself *the date such initial Loan is funded*. Neither event
is dated anywhere in the filing. The cover says December 23, 2022, which is the
execution date and the answer to neither question — and it is the answer both
fields will attract, from a reader that takes the nearest date and, for the
maturity, adds five to it.
