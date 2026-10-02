# Plan: economic terms on BDC and fund facilities

Status: proposed, not started.

## The problem

The out-of-sample run (`docs/jev_live_pass.md`) used twenty BDC credit
facilities. The rules tier extracted no value at all for any of the 61
labelled commitments, maturities, top margins and commitment fees, and only
some of the floors. Jev validates what was extracted; it cannot confirm a term
nobody read. So those fields sat in review on every document, and on these
facilities this is the largest coverage gap the pipeline has.

The corpus could not have shown it. Its 24 fund and BDC label files carry 21
labels on these five fields between them, so the gap was never measured in
sample.

## The constraint

The twenty out-of-sample documents stay out of the work. Everything below is
developed and measured on in-sample documents. The twenty are run once, at the
end, to measure the result. After that they have informed a fix, so a fresh
out-of-sample set is harvested for the next measurement.

## What the rules tier does today

Each field has one rule or definition anchor, and each was written against
the synthetic fixture's drafting:

| field | rule | definition anchor |
| --- | --- | --- |
| `revolver.commitment` | "Revolving Credit Commitments on the Closing Date are $X" | Revolving Credit Commitment |
| `revolver.maturity_date` | '"Revolving Credit Maturity Date" means …' | Revolving Credit Maturity Date |
| `applicable_margin.eurodollar_top_level_pct` | none | Applicable Margin |
| `libor_floor_pct` | "LIBO Rate shall not at any time be less than X%" | Floor, SOFR Floor, LIBO Rate |
| `commitment_fee_pct` | "commitment fee equal to X%" | none |

## What fund facilities write instead

Counted over the twenty in-sample fund and BDC agreements (`insample_text`,
no out-of-sample document). The forms are templated: a handful of law firms'
forms cover most of the market.

| form | in sample |
| --- | --- |
| commitment: '"Facility Amount" means …' | 10 / 20 |
| commitment: '"Maximum Commitment" / "Financing Limit" / "Maximum Facility Amount" means …' | 8 / 20 |
| commitment: "the aggregate amount of the Lenders' [Dollar \| Multicurrency] Commitments … is $X" | 2 / 20 |
| maturity: '"[Stated \| Final] Maturity Date" means <date>' | 7 / 20 |
| maturity: '"[Facility] Termination Date" means …' | 6 / 20 |
| margin: '"Applicable Margin" / "Applicable Spread" means …' | 9 / 20 |
| margin: a grid keyed to the Gross Borrowing Base over the Combined Debt Amount | 2 / 20 |
| floor: '"Floor" means …' | 12 / 20 |
| floor: "less than zero … deemed to be zero" | 6 / 20 |
| fee: an Unused, Non-Usage or Non-Utilization Fee | 11 / 20 |
| fee: "commitment fee … at a rate per annum equal to X%" | 2 / 20 |

## Steps

1. **Label first, from the text.** Write the five fields on the twenty in-sample
   fund and BDC agreements before any rule changes, with the same brief and
   conventions the out-of-sample labels used:
   - take the largest tranche, and never a sum;
   - record the accordion, the reinvestment-period end and the ABR margin as
     near misses;
   - skip a field rather than guess.

   That is about 100 assertions. Without them nothing below can be measured,
   and labels written after the rules would only describe the rules.
2. **Diagnose every miss, at no cost.** For each labelled field, run the rules
   tier and the definitional pass offline and record why it missed. The
   likely causes are:
   - no anchor for the defined term the agreement uses;
   - an anchor, but the value is several definitions deep;
   - the value is in a grid;
   - an amendment whose conformed copy is an annex;
   - a blackline that fuses the old figure with the new.

   Which of these dominates decides how much of steps 3 to 5 is worth doing.
3. **Definition anchors before patterns.** Where the definition's own body
   holds the value, an anchor is cheaper and safer than a regex. Candidates:
   - Facility Amount, Maximum Facility Amount, Maximum Commitment and
     Financing Limit;
   - Maturity Date, Stated Maturity Date and Final Maturity Date;
   - Floor;
   - the Unused, Non-Usage and Non-Utilization Fee Rate.

   Each anchor ships with a test on its near miss:
   - the Commitment Termination Date and the end of the reinvestment period
     are not maturities;
   - an accordion cap is not a commitment;
   - an LC fronting fee is not a commitment fee.
4. **A few anchored rules for the forms that are not definitions.** These are
   "the aggregate amount of the Lenders' … Commitments … is $X", resolved per
   the guide to the largest tranche, and "commitment fee … at a rate per annum
   equal to X%". The rules tier's own test applies to each: is this form
   common and unambiguous enough that a pattern beats a model? The
   borrowing-base coverage grid goes to the existing text-grid reader, if that
   can take a two-row grid, rather than to a new regex.
5. **Derived maturities, computed rather than read.** Two forms:
   - "the earliest of (a) <date> and (b) <event>" resolves to the date;
   - "the date that is N years after the <defined date>", with the next
     business day, is arithmetic over the definition graph.

   Python does it, only when every link resolves to a date, and anything
   short of that stays in review. This is family F09's depth-4 closure.
6. **Measure in sample.** Run the offline gate (CI) and the live gate on the
   answer cache. Extraction changes add validator-A questions for the new
   values only, so the live gate should cost well under $2. Report per field,
   split fit against holdout:
   - coverage on the new labels;
   - silent errors, where the budget stays zero.
7. **Then the twenty BDCs, once.** Their answers are cached, so this costs
   only the new questions, cents at most. Report against their blind labels,
   then harvest a fresh out-of-sample set, preferably from a segment the
   corpus covers thinly, such as sponsor term loans or investment-grade
   revolvers. The guarantor pass (`docs/jev_live_pass.md`) followed the same
   order: labels first, in sample, then one out-of-sample run.

An optional second phase is the model tier. `AnthropicBackend` exists and has
never run live; with an API key it would take what steps 3 to 5 cannot reach,
and the same labels would measure it.

## What could go wrong

* **Near misses become confident wrong values.** An accordion amount, a
  reinvestment-period end or an ABR margin each sits next to the right answer
  in these documents. Each needs a pinned negative test, and Jev's
  validator A is the second line, not the first.
* **One template does not generalize.** The forms above come from in-sample
  agreements. A rule that fits one law firm's form and misreads another's is
  why step 7 runs on documents the rules were not written against.
* **Blacklines.** Some conformed copies keep struck text fused with inserted
  text (`$500,000,000575,000,000`). A value read from a fused numeral must go
  to review, never to a confident status.

## Cost

* **Jev:** under $5 in all, for two or three cached live gates and one
  out-of-sample run.
* **Labelling:** about 100 assertions.
* **Code:** a few anchors, two or three rules, a date resolver over the
  definition graph, and a negative test for each near miss.

## Done when

* Each of the five fields is confidently right on most in-sample fund
  facilities that state it, with no silent error.
* The out-of-sample run has been reported against its blind labels.
* A fresh out-of-sample set has been harvested and labelled before anything
  runs on it.
