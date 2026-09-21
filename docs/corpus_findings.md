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
