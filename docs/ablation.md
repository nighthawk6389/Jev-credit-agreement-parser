# The ablation: which stages earn their cost

Roadmap steps 3 and 4. Step 3 asked for a model-only baseline with no semantic
validators beyond citation checking; step 4 for a harness comparing current,
model-only and hybrid pipelines "on identical inputs and metrics".

```bash
python -m credit_extract.eval.ablation                       # all four arms
python -m credit_extract.eval.ablation --arms current,hybrid # two of them
```

## Why the family report could not answer this

The family report asks: *of the propositions asserted confidently, how many
were wrong?* That is the right safety question, and it is monotone in silence.
**A pipeline that answers nothing scores a perfect zero.** Ranking pipelines on
it alone ranks the one that extracts least above the one that extracts most and
errs once.

So the ablation reports **recall** — values returned over the 37 critical
fields — beside it, and refuses to print either without the other. Recall is
counted against the registry rather than against the labels, because counting
only labelled fields would let an arm improve its score by returning fewer
things, which is the same failure wearing a different hat.

## The arms

| arm | tier | validators | what it is |
| --- | --- | --- | --- |
| `current` | layered | ABCDEFG | the pipeline as designed |
| `deterministic` | rules | ABCDEFG | the pipeline as it actually runs here |
| `model_only` | model | A | step 3's baseline |
| `hybrid` | layered | ABC | the three stages with a stated mechanism |

Tier and validator stack are separate axes on purpose. Conflating them would
make "model-only" mean both *only the model extracted* and *only citation
checking validated* — two claims that would then be impossible to tell apart.
`current` and `hybrid` extract identically and differ only in D, E, F and G, so
a difference between them is a fact about the stack.

## What it found

Three documents, 37 critical fields each. That is the whole comparison set, and
the reason is the next section.

```
arm           recall  review  settled  conf  wrong  silent     cost    secs
current       23.4%  79.3%    8.1%    20      0   0.0%   0.0972    18.2
deterministic   7.2%  90.1%    7.2%    11      0   0.0%   0.1037    19.2
model_only    20.7%  78.4%    7.2%    22      2   9.1%   0.0011     7.3
hybrid        23.4%  79.3%    8.1%    20      1   5.0%   0.0965    18.0
```

### 1. Every number this repository has published was measured without the model tier

`deterministic` returns **7.2%** of the critical fields; `current` returns
**23.4%**. Three times as many, and 11 confident propositions against 20.

That gap is not a proposal — it is the gap between the pipeline as designed and
the pipeline as every run in this repository has actually executed it. There is
no `ANTHROPIC_API_KEY` in this environment, so the family report, all 464
assertions and the entire blind-spot register are measured on the deterministic
arm. The evidence base is sound and it has never seen two thirds of the
pipeline's recall.

The disagreements say it field by field. On Aspen and Essential Properties the
deterministic arm returns `None` for the floor, the top of the pricing grid, the
governing law, the revolver commitment and the whole-facility commitment; every
arm carrying the model returns the right value for all five.

### 2. Validator E earns its place on one document in a hundred

`current` and `hybrid` are identical on recall (23.4%), review rate (79.3%) and
confident propositions (20). They differ by exactly one silent error, and the
disagreement names it:

```
  star_non_utilization_fee_rate_is_in_the_fee_letter
    current        asserted  right  by_design
    deterministic  asserted  right  by_design
    model_only     asserted  WRONG  None
    hybrid         asserted  WRONG  None
```

Validator E is the fee-letter rule. The blind-spot register already records
that it fires on 1 of the 100 harvested documents. What the ablation adds is
what happens when it does: the difference between a settled, correct
`external_reference` and a confident wrong answer.

That is a direct answer to the roadmap's §5, *"simplify validation until each
tier proves incremental value"*, and it runs the other way. A rule that almost
never fires is not thereby cheap to remove. **E costs about a tenth of a cent
per document and buys the only thing the safety metric measures.**

### 3. The model tier and the validation stack fail in opposite directions

The sharpest pair in the run is one field, Essential Properties' closing date,
under two labels written to catch exactly this:

```
  eprt_closing_date_is_the_defined_term
    current        reviewed  WRONG  2019-11-26
    model_only     asserted  right  2018-06-25

  eprt_closing_date_is_not_asserted_wrongly
    current        reviewed  right  conflicted
    model_only     asserted  WRONG  confirmed
```

The model alone **gets the value right and wrongly calls it settled**. The
layered pipeline **gets the value wrong and correctly refuses to settle**.
Neither is acceptable and they are not the same defect:

- `model_only`'s failure is the one the validation stack exists to prevent, and
  it is why step 3's baseline is a baseline rather than a proposal.
- `current`'s failure is the roadmap's §1 defect, measured: the deterministic
  candidate is the one surfaced as the value even where the model had the right
  answer and reconciliation could not choose between them.

`model_only` is also **90× cheaper and 2.5× faster** ($0.0011 against $0.0972,
7.3s against 18.2s), because the Jev validators are nearly the entire cost of a
run. The stack is not free and it is not nearly free; it is most of the bill.

## What this does not measure

**Three documents.** The model arms replay a checked-in recording, and the
repository has three. 100 of the 103 labelled documents are dropped from the
comparison — not skipped for the model arms and scored for the others, which
would compare two populations in one table, but dropped for every arm.

The harness reports every dropped document and why. It never falls back to the
deterministic backend for a model arm, because that would make the model arms
*be* the deterministic arm and the ablation would report that the model tier
changes nothing — a finding it had manufactured itself.

**So the ranking above is a hypothesis with three data points behind it**, not a
result. What would turn it into one is more recordings, which is bounded work
that needs an API key and no new design.

## What follows

1. **Keep D, E, F and G.** Step 5 of the roadmap says to implement only the
   stages the ablation proves valuable. On this evidence E is proved and the
   others are untested rather than disproved — `current` and `hybrid` differ by
   E alone on these three documents.
2. **Fix reconciliation's preference, not the model.** Finding 3 is a
   deterministic candidate beating a correct model candidate. That is §1 of the
   review and it now has a failing assertion attached to it.
3. **Get more recordings before trusting the table.** Three documents cannot
   separate a 23.4% recall from a 20.7% one.
