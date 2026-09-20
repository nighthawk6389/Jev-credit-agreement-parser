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
