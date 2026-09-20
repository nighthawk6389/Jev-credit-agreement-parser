"""Jev System One client.

Jev takes a ``state`` plus named typed questions and returns typed decisions
with calibrated confidence. It does not generate text.

The constraints shape the design more than the API does:

* 64k context total, 32k for state plus the longest question;
* questions within one request are independent -- no chaining, so anything
  that needs a previous answer is a second request or belongs in Python;
* weak at arithmetic, date comparison and literal counting, so none of that is
  ever asked here;
* state is sent once per request regardless of how many questions ride along,
  and output is free.

That last point is the one with teeth. Fifteen questions against one clause
cost essentially the same as one, which changes what is economically sensible:
you can afford to ask every question of every chunk, and the orphan sweep does
exactly that. Batching is therefore not an optimization in this module, it is
the intended usage, and :meth:`JevSession.ask` refuses to send a request
carrying a single question when more were available to batch.

``OfflineJev`` implements the same interface deterministically so the pipeline,
its tests and its calibration all run without network or spend. It is a
lexical-evidence stand-in, not a model, and it says so: thresholds fitted
against it are tagged with its backend name and the calibration loader refuses
to apply them to a different backend.
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from ..models.core import CostLedger

# ---------------------------------------------------------------------------
# Published limits and pricing
# ---------------------------------------------------------------------------

CONTEXT_TOKENS_TOTAL = 64_000
STATE_PLUS_QUESTION_TOKENS = 32_000
#: USD per million input tokens. Output is free.
PRICE_PER_MTOK = 0.042
DEFAULT_ENDPOINT = "https://api.jev.ai/v1/systemone"


def estimate_tokens(text: str) -> int:
    """Conservative 4-characters-per-token estimate."""
    return max(1, math.ceil(len(text) / 4))


class JevBudgetExceeded(RuntimeError):
    """Raised when a request would push spend past the configured budget."""


class JevContextExceeded(ValueError):
    """Raised when state plus the longest question exceeds the 32k limit."""


# ---------------------------------------------------------------------------
# Question types
# ---------------------------------------------------------------------------


class Noul(BaseModel):
    """Probability that a statement is true.

    ``polarity`` is declared by the validator that builds the question, never
    inferred from the wording. "The magnitude of this limit depends on a
    document **not** contained in this agreement" is an affirmative claim whose
    subject happens to contain a negation; a scorer that sniffs for "not" reads
    it backwards and flags every field in the document as an external
    reference. Only the caller knows whether it is asking "is this so?" or "is
    this absent?".
    """

    name: str
    statement: str
    kind: Literal["noul"] = "noul"
    polarity: Literal["affirmative", "absence"] = "affirmative"
    #: Optional concept id for offline scoring. Local hint, never sent.
    concept: str | None = None

    def prompt_text(self) -> str:
        return self.statement

    def subject(self) -> str:
        """The thing whose presence an absence claim is about."""
        return re.sub(
            r"\b(contains no|contain no|does not|do not|is not|are not|not|no|never)\b",
            " ", self.statement, flags=re.IGNORECASE,
        )


class ChoiceQ(BaseModel):
    """Pick one option from a criteria map; returns a distribution."""

    name: str
    question: str
    criteria: dict[str, str]
    kind: Literal["choice"] = "choice"

    def prompt_text(self) -> str:
        options = " ".join(f"{k}: {v}" for k, v in self.criteria.items())
        return f"{self.question} {options}"


class ScoreQ(BaseModel):
    """Position on an ordered rubric of 2-10 levels."""

    name: str
    question: str
    rubric: list[str]
    kind: Literal["score"] = "score"

    def model_post_init(self, _context: Any) -> None:
        if not 2 <= len(self.rubric) <= 10:
            raise ValueError(
                f"a score rubric carries 2-10 levels; got {len(self.rubric)}"
            )

    def prompt_text(self) -> str:
        return f"{self.question} " + " ".join(
            f"{i + 1}: {level}" for i, level in enumerate(self.rubric)
        )


Question = Noul | ChoiceQ | ScoreQ


class Decision(BaseModel):
    """One typed answer."""

    name: str
    kind: str
    probability: float | None = None          # noul
    choice: str | None = None                 # choice
    distribution: dict[str, float] = Field(default_factory=dict)
    score: int | None = None                  # score
    label: str | None = None
    backend: str = "offline"

    @property
    def confidence(self) -> float:
        if self.probability is not None:
            return self.probability
        if self.distribution:
            return max(self.distribution.values())
        return 0.0


class JevResult(BaseModel):
    decisions: dict[str, Decision] = Field(default_factory=dict)
    input_tokens: int = 0
    cost_usd: float = 0.0
    backend: str = "offline"
    questions_asked: int = 0

    def __getitem__(self, name: str) -> Decision:
        return self.decisions[name]

    def get(self, name: str) -> Decision | None:
        return self.decisions.get(name)


class JevBackend(Protocol):
    name: str

    def ask(self, state: str, questions: list[Question]) -> JevResult: ...


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------


def check_context(state: str, questions: list[Question]) -> int:
    """Validate the 32k state-plus-longest-question limit; return input tokens."""
    state_tokens = estimate_tokens(state)
    longest = max((estimate_tokens(q.prompt_text()) for q in questions), default=0)
    if state_tokens + longest > STATE_PLUS_QUESTION_TOKENS:
        raise JevContextExceeded(
            f"state ({state_tokens} tokens) plus the longest question "
            f"({longest}) exceeds the {STATE_PLUS_QUESTION_TOKENS} token limit"
        )
    total = state_tokens + sum(estimate_tokens(q.prompt_text()) for q in questions)
    if total > CONTEXT_TOKENS_TOTAL:
        raise JevContextExceeded(
            f"request totals {total} tokens, over the {CONTEXT_TOKENS_TOTAL} limit"
        )
    return total


def split_batches(state: str, questions: list[Question]) -> list[list[Question]]:
    """Split questions into the fewest requests that fit the context limit.

    Fewest requests is the objective because state is re-sent with each one,
    and state is almost always the expensive part.
    """
    state_tokens = estimate_tokens(state)
    if state_tokens >= CONTEXT_TOKENS_TOTAL:
        raise JevContextExceeded(
            f"state alone is {state_tokens} tokens, over the context limit"
        )
    batches: list[list[Question]] = []
    current: list[Question] = []
    used = state_tokens
    for question in questions:
        cost = estimate_tokens(question.prompt_text())
        if state_tokens + cost > STATE_PLUS_QUESTION_TOKENS:
            raise JevContextExceeded(
                f"question {question.name!r} does not fit alongside the state"
            )
        if current and used + cost > CONTEXT_TOKENS_TOTAL:
            batches.append(current)
            current = []
            used = state_tokens
        current.append(question)
        used += cost
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# HTTP backend
# ---------------------------------------------------------------------------


class JevClient:
    """The real System One endpoint."""

    name = "jev"

    def __init__(
        self,
        api_key: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or os.environ.get("JEV_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "JEV_API_KEY is not set; construct OfflineJev() to run without "
                "network access"
            )
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    def ask(self, state: str, questions: list[Question]) -> JevResult:
        input_tokens = check_context(state, questions)
        payload = {
            "state": state,
            # `polarity` and `concept` are local hints for offline scoring,
            # not part of the System One request schema, so they stay off
            # the wire.
            "questions": [
                q.model_dump(exclude={"polarity", "concept"}) for q in questions
            ],
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.post(
                    self.endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                return self._parse(response.json(), input_tokens, len(questions))
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Jev request failed after {self.max_retries} attempts") \
            from last_error

    def _parse(self, body: dict, input_tokens: int, asked: int) -> JevResult:
        decisions: dict[str, Decision] = {}
        for item in body.get("decisions", []):
            decisions[item["name"]] = Decision(
                name=item["name"],
                kind=item.get("type", "noul"),
                probability=item.get("probability"),
                choice=item.get("choice"),
                distribution=item.get("distribution", {}) or {},
                score=item.get("score"),
                label=item.get("label"),
                backend=self.name,
            )
        billed = body.get("usage", {}).get("input_tokens", input_tokens)
        return JevResult(
            decisions=decisions,
            input_tokens=billed,
            cost_usd=billed * PRICE_PER_MTOK / 1_000_000,
            backend=self.name,
            questions_asked=asked,
        )

    def close(self) -> None:  # pragma: no cover - lifecycle helper
        self._client.close()


# ---------------------------------------------------------------------------
# Offline backend
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "the a an of to for in on at by and or is are was were be been this that "
    "with as from any such shall will not no it its their there "
    # Function words carry no evidence but dilute every score, and a long
    # chunk accumulates enough incidental matches to look like support.
    "which who whom whose what when where why how all each every both either "
    "neither some more most less least other another same own than then also "
    "after before during until since while upon about above below over under "
    "between among through against into onto out off up down again further "
    "may might must can could would should shall has have had having do does "
    "did done being if but so because however therefore thus hereby herein "
    "time times period periods date dates amount amounts respect thereto "
    "pursuant accordance connection foregoing following including "
    "means mean meaning apply applies applicable".split()
)

#: Concept vocabularies for the orphan-sweep signals.
#:
#: The sweep statements are abstract ("this text creates a payment obligation"),
#: and no clause contains the words "creates a payment obligation" -- a clause
#: says "the Borrower shall pay ... a fee". A lexical scorer needs the concept
#: spelled out to stand in for the semantic judgement the real model makes.
#: These lexicons are the offline backend's weakest approximation and the first
#: thing the real System One backend makes unnecessary.
CONCEPT_LEXICONS: dict[str, tuple[str, ...]] = {
    "payment_obligation": (
        "shall pay", "agrees to pay", "shall be payable", "shall repay",
        "shall prepay", "reimburse", "indemnif", " fee", "premium",
        "due and payable", "payment of interest",
    ),
    "restriction": (
        "shall not", "will not", "may not", "prohibited", "is not permitted",
        "limitation on", "restrict", "will not permit", "except:",
    ),
    "override": (
        "notwithstanding", "shall be deemed", "in lieu of", "shall not apply",
        "for the avoidance of doubt", "supersede", "to the contrary",
    ),
    "threshold": (
        "not to exceed", "shall not exceed", "greater of", "lesser of",
        "in excess of", "sublimit", "basket", "at least", "no less than",
    ),
    "schedule": (
        "quarterly", "annually", "maturity date", "installment",
        "fiscal quarter", "on or prior to", "each anniversary",
        "consecutive quarterly",
    ),
    # Amendment effect (validator G). Without these the stand-in sits at a coin
    # flip on every amendment and reports a disagreement with the parser on
    # each one, which trains a reviewer to ignore the field.
    "amendment_restates": (
        "amended and restated in its entirety", "restated in its entirety",
        "to read as follows", "is hereby amended and restated",
    ),
    "amendment_numeric": (
        "deleting the text", "inserting in lieu", "the figure", "the amount",
        "%", "$", "replacing the reference to",
    ),
    "amendment_deferred": (
        "shall become effective on", "effective as of", "on and after",
        "effective date\" means", "from and after",
    ),
}


def concept_score(state: str, concept: str) -> float | None:
    """Score a known concept by lexicon hits; None when the concept is unknown.

    Monotone in the number of *distinct* signals, so one stray "fee" is not
    enough and two independent signals are.
    """
    lexicon = CONCEPT_LEXICONS.get(concept)
    if lexicon is None:
        return None
    lower = state.lower()
    hits = sum(1 for phrase in lexicon if phrase in lower)
    return round(min(0.97, 0.02 + 0.32 * hits), 4)

#: Vocabulary that belongs to the *question*, not to the evidence. Scoring a
#: statement like "The text supports a value of 376,250 for the quarterly
#: amortization payment" against a clause would otherwise be dominated by
#: scaffolding the clause has no reason to contain, and a correct value would
#: score barely above a wrong one.
_SCAFFOLDING = frozenset(
    "text supports support value values provision provisions agreement "
    "contains contain containing addressing address addresses document "
    "documents clause section states says stating field attached summary "
    "captured whether respect thereof therein herein hereto under within "
    "following above below set forth given amount described".split()
)


def _content_words(text: str) -> list[str]:
    """Tokenize, keeping figures intact but shedding sentence punctuation.

    ``$376,250`` and ``3.72:1.00`` must survive as single tokens, while
    ``installments.`` has to match ``installments`` -- so only trailing
    sentence punctuation is stripped, never the internal kind.
    """
    tokens = (
        w.strip(".,;:").lstrip("(").rstrip(")")
        for w in re.findall(r"[a-z0-9.%$,:/()-]+", text.lower())
    )
    return [w for w in tokens if w and w not in _STOPWORDS and len(w) > 1]


_MONTH_NAMES = (
    "January February March April May June July August September October "
    "November December"
).split()


def _numeric_forms(token: str) -> set[str]:
    """Surface variants of a figure or date.

    ``0.50`` has to match ``0.5`` and ``50 bps``, and an ISO date has to match
    the way agreements actually write dates. Without the date expansion, a
    correctly extracted ``2024-08-01`` scores as unsupported against a document
    that says "August 1, 2024", and validator A rejects every maturity date in
    the deal.
    """
    forms = {token}
    cleaned = token.replace("$", "").replace(",", "").rstrip("%")
    forms.add(cleaned)
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
        name = _MONTH_NAMES[month - 1].lower()
        forms.update({
            f"{name} {day}, {year}", f"{name} {day} {year}",
            f"{day} {name} {year}", f"{month}/{day}/{year}",
            f"{month:02d}/{day:02d}/{year}",
        })
        return forms
    try:
        value = float(cleaned)
    except ValueError:
        return forms
    forms.add(f"{value:g}")
    forms.add(f"{value:,.0f}" if value == int(value) else f"{value:,.2f}")
    if value == int(value):
        forms.add(str(int(value)))
    if 0 < value < 100:
        forms.add(f"{value * 100:g}")          # 0.50% <-> 50 bps
    return {f for f in forms if f}


class OfflineJev:
    """A deterministic stand-in for System One.

    Scores a statement by how much of its content is lexically supported by the
    state, with numeric surface-form matching so ``0.50%`` and ``50 bps`` count
    as the same evidence. It is emphatically not a calibrated model: it exists
    so the pipeline, its tests and its threshold fitting are reproducible
    offline, and thresholds fitted against it are tagged ``offline`` so they
    cannot be silently applied to the real backend.
    """

    name = "offline"

    def __init__(self, seed_confidence: float = 0.5) -> None:
        self.seed_confidence = seed_confidence

    def ask(self, state: str, questions: list[Question]) -> JevResult:
        input_tokens = check_context(state, questions)
        decisions: dict[str, Decision] = {}
        for question in questions:
            if isinstance(question, Noul):
                decisions[question.name] = Decision(
                    name=question.name, kind="noul", backend=self.name,
                    probability=self._noul(state, question),
                )
            elif isinstance(question, ChoiceQ):
                distribution = self._choice(state, question)
                pick = max(distribution, key=distribution.get)
                decisions[question.name] = Decision(
                    name=question.name, kind="choice", backend=self.name,
                    choice=pick, distribution=distribution,
                )
            else:
                level = self._score(state, question)
                decisions[question.name] = Decision(
                    name=question.name, kind="score", backend=self.name,
                    score=level, label=question.rubric[level - 1],
                )
        return JevResult(
            decisions=decisions,
            input_tokens=input_tokens,
            cost_usd=input_tokens * PRICE_PER_MTOK / 1_000_000,
            backend=self.name,
            questions_asked=len(questions),
        )

    # -- scoring ------------------------------------------------------------

    def _support(self, state: str, statement: str) -> float:
        """Fraction of the statement's *evidential* content found in the state.

        Numeric evidence dominates when present: a statement asserting a
        specific figure is about that figure, and whether the surrounding
        prose happens to share vocabulary with the clause is close to noise.
        """
        state_lower = state.lower()
        state_words = set(_content_words(state))
        words = [w for w in _content_words(statement) if w not in _SCAFFOLDING]
        if not words:
            return 0.0
        numerics = [w for w in words if any(ch.isdigit() for ch in w)]
        lexical = [w for w in words if w not in numerics]

        lexical_score = (
            sum(1 for w in lexical if w in state_words) / len(lexical)
            if lexical else 0.0
        )
        if not numerics:
            return lexical_score
        numeric_score = sum(
            1 for w in numerics
            if any(form in state_lower for form in _numeric_forms(w))
        ) / len(numerics)
        return 0.75 * numeric_score + 0.25 * lexical_score

    def _noul(self, state: str, question: Noul) -> float:
        if question.concept:
            scored = concept_score(state, question.concept)
            if scored is not None:
                return scored
        if question.polarity == "absence":
            # An absence claim is supported by the *absence* of its subject, so
            # the signal inverts: measure how present the subject is.
            presence = self._support(state, question.subject())
            return round(min(0.99, max(0.01, 1.0 - presence)), 4)
        return round(min(0.99, max(0.01, self._support(state, question.statement))), 4)

    def _choice(self, state: str, question: ChoiceQ) -> dict[str, float]:
        # The option key is itself evidence -- for a choice between competing
        # extracted values, the key *is* the value being tested.
        raw = {
            option: self._support(state, f"{option} {criteria}") + 1e-6
            for option, criteria in question.criteria.items()
        }
        total = sum(raw.values())
        return {k: round(v / total, 4) for k, v in raw.items()}

    def _score(self, state: str, question: ScoreQ) -> int:
        scores = [self._support(state, level) for level in question.rubric]
        if max(scores) - min(scores) < 1e-9:
            # Nothing discriminates between levels. Returning the first level
            # would assert "least severe" on no evidence; the middle is the
            # honest answer.
            return (len(question.rubric) + 1) // 2
        return max(range(len(scores)), key=lambda i: scores[i]) + 1


# ---------------------------------------------------------------------------
# Session: budget enforcement and cost accounting
# ---------------------------------------------------------------------------


class JevSession:
    """Wraps a backend with batching, budget enforcement and a cost ledger."""

    def __init__(
        self,
        backend: JevBackend | None = None,
        budget_usd: float | None = None,
    ) -> None:
        self.backend = backend or OfflineJev()
        self.budget_usd = budget_usd
        self.ledger = CostLedger()
        self.requests: list[dict[str, Any]] = []

    @property
    def spent(self) -> float:
        return self.ledger.jev_cost_usd

    def ask(
        self, state: str, questions: list[Question], label: str = ""
    ) -> JevResult:
        """Ask every question against one state, in as few requests as fit."""
        if not questions:
            return JevResult(backend=self.backend.name)
        merged = JevResult(backend=self.backend.name)
        for batch in split_batches(state, questions):
            if self.budget_usd is not None:
                projected = estimate_tokens(state) * PRICE_PER_MTOK / 1_000_000
                if self.spent + projected > self.budget_usd:
                    raise JevBudgetExceeded(
                        f"Jev spend ${self.spent:.4f} plus ${projected:.4f} "
                        f"would exceed the ${self.budget_usd:.2f} budget"
                    )
            result = self.backend.ask(state, batch)
            merged.decisions.update(result.decisions)
            merged.input_tokens += result.input_tokens
            merged.cost_usd += result.cost_usd
            merged.questions_asked += result.questions_asked
            self.ledger.jev_requests += 1
            self.ledger.jev_questions += result.questions_asked
            self.ledger.jev_input_tokens += result.input_tokens
            self.ledger.jev_cost_usd += result.cost_usd
            self.requests.append({
                "label": label,
                "questions": [q.name for q in batch],
                "input_tokens": result.input_tokens,
                "cost_usd": result.cost_usd,
            })
        return merged

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend.name,
            "requests": self.ledger.jev_requests,
            "questions": self.ledger.jev_questions,
            "input_tokens": self.ledger.jev_input_tokens,
            "cost_usd": round(self.ledger.jev_cost_usd, 6),
            "questions_per_request": round(
                self.ledger.jev_questions / self.ledger.jev_requests, 2
            ) if self.ledger.jev_requests else 0.0,
        }
