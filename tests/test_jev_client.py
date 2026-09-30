"""The live System One client: its wire contract, pinned against the real thing.

Every earlier version of this client was written from a description of the API
rather than the API, and got three things wrong at once: the host (there is no
``api.jev.ai``), the request (questions are a map keyed by name, and ``model``
is required) and the response (``answers``, not ``decisions``). Nothing caught
it because nothing had ever been sent. The fixture below is the body the
endpoint actually returned to the first request made against it, kept verbatim,
so the parser is tested against what the server says rather than what anyone
believes it says.
"""

from __future__ import annotations

import json

import httpx
import pytest

from credit_extract.validate.jev import (
    DEFAULT_ENDPOINT, DEFAULT_MODEL, PRICE_PER_MTOK, ChoiceQ, JevClient,
    JevRequestError, JevSession, Noul, ScoreQ,
)

STATE = "This Agreement shall be governed by the laws of the State of New York."

QUESTIONS = [
    Noul(name="probe", statement=(
        "The text supports a value of New York for the governing law of the "
        "facility."
    )),
    Noul(name="probe_neg", statement=(
        "The text supports a value of Delaware for the governing law of the "
        "facility."
    )),
    ChoiceQ(
        name="law",
        question="Which jurisdiction's law governs this agreement?",
        criteria={
            "new_york": "New York", "delaware": "Delaware",
            "english": "England and Wales",
        },
    ),
    ScoreQ(
        name="clarity",
        question="How explicitly does the text state the governing law?",
        rubric=["Not stated", "Implied", "Stated explicitly"],
    ),
]

#: Returned by POST https://api.typesafe.ai/v1/systemone on 2026-09-30,
#: request id req_01a0efa9a48f7cabb6c9da0ed6d5ca88, for exactly QUESTIONS.
LIVE_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "probe": {"type": "noul", "noul": 0.95},
        "probe_neg": {"type": "noul", "noul": 0.02},
        "law": {
            "type": "choice",
            "choice": "new_york",
            "confidence": 1.0,
            "probabilities": {"new_york": 1.0, "delaware": 0.0, "english": 0.0},
        },
        "clarity": {
            "type": "score",
            "score": 2.0,
            "confidence": 1.0,
            "legend": {
                "0": "Not stated", "1": "Implied", "2": "Stated explicitly",
            },
            "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0},
        },
    },
    "usage": {"input_tokens": 436, "output_tokens": 92},
}


class Endpoint:
    """A scripted endpoint that records every request it receives."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self, **kwargs) -> JevClient:
        kwargs.setdefault("backoff_seconds", 0.0)
        return JevClient(
            api_key="test-key", transport=httpx.MockTransport(self), **kwargs
        )


def ok(body: dict = LIVE_RESPONSE) -> httpx.Response:
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_the_request_is_the_documented_schema():
    endpoint = Endpoint(ok())
    endpoint.client().ask(STATE, QUESTIONS)

    (request,) = endpoint.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "state": STATE,
        "model": "jev-1.13.0",
        "questions": {
            "probe": {"type": "noul", "instructions": QUESTIONS[0].statement},
            "probe_neg": {"type": "noul", "instructions": QUESTIONS[1].statement},
            "law": {
                "type": "choice",
                "instructions": "Which jurisdiction's law governs this agreement?",
                "criteria": {
                    "new_york": "New York", "delaware": "Delaware",
                    "english": "England and Wales",
                },
            },
            "clarity": {
                "type": "score",
                "instructions": (
                    "How explicitly does the text state the governing law?"
                ),
                "criteria": ["Not stated", "Implied", "Stated explicitly"],
            },
        },
    }


def test_the_endpoint_is_the_vendors_and_not_the_parked_domain():
    """``jev.ai`` is for sale. A key sent to a host under it goes to whoever buys it."""
    host = httpx.URL(DEFAULT_ENDPOINT).host
    assert host == "api.typesafe.ai"
    assert not host.endswith("jev.ai")


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------


def test_the_live_response_parses_into_one_decision_per_question():
    result = Endpoint(ok()).client().ask(STATE, QUESTIONS)

    assert result["probe"].probability == 0.95
    assert result["probe"].confidence == 0.95
    assert result["probe_neg"].confidence == 0.02
    assert result["law"].choice == "new_york"
    assert result["law"].distribution == {
        "new_york": 1.0, "delaware": 0.0, "english": 0.0,
    }
    assert result["law"].confidence == 1.0
    assert result.questions_asked == 4


def test_score_levels_come_back_one_indexed_as_the_validators_read_them():
    """The endpoint counts levels from 0; validator F indexes its rubric from 1."""
    result = Endpoint(ok()).client().ask(STATE, QUESTIONS)

    assert result["clarity"].score == 3
    assert result["clarity"].label == "Stated explicitly"
    assert result["clarity"].distribution == {"1": 0.0, "2": 0.0, "3": 1.0}


def test_a_score_is_the_most_probable_level_not_a_rounded_expectation():
    body = json.loads(json.dumps(LIVE_RESPONSE))
    body["answers"]["clarity"].update(
        score=1.1, probabilities={"0": 0.45, "1": 0.0, "2": 0.55},
    )
    result = Endpoint(ok(body)).client().ask(STATE, QUESTIONS)
    assert result["clarity"].score == 3          # the expectation rounds to 2

    body["answers"]["clarity"]["probabilities"] = {"0": 0.5, "1": 0.0, "2": 0.5}
    result = Endpoint(ok(body)).client().ask(STATE, QUESTIONS)
    assert result["clarity"].score == 1          # a tie takes the lower level


def test_cost_is_the_billed_tokens_not_the_estimate():
    result = Endpoint(ok()).client().ask(STATE, QUESTIONS)
    assert result.input_tokens == 436
    assert result.cost_usd == pytest.approx(436 * PRICE_PER_MTOK / 1_000_000)


def test_a_missing_answer_is_an_error_and_never_a_no():
    """Validator C would read a dropped answer as 'absent' at 1.0."""
    body = json.loads(json.dumps(LIVE_RESPONSE))
    del body["answers"]["probe_neg"]
    endpoint = Endpoint(ok(body), ok(body))

    with pytest.raises(JevRequestError, match="probe_neg"):
        endpoint.client(max_retries=1).ask(STATE, QUESTIONS)


def test_a_session_accounts_for_what_the_live_client_billed():
    session = JevSession(Endpoint(ok()).client())
    session.ask(STATE, QUESTIONS)
    assert session.ledger.jev_requests == 1
    assert session.ledger.jev_questions == 4
    assert session.ledger.jev_input_tokens == 436


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_rejected_key_raises_at_once_instead_of_looking_like_a_network_fault():
    endpoint = Endpoint(httpx.Response(401, json={"detail": "invalid key"}))

    with pytest.raises(JevRequestError) as raised:
        endpoint.client().ask(STATE, QUESTIONS)

    assert raised.value.status == 401
    assert len(endpoint.requests) == 1


def test_a_malformed_request_is_not_retried_and_says_what_was_wrong():
    detail = {"detail": [{"loc": ["body", "model"], "msg": "Field required"}]}
    endpoint = Endpoint(httpx.Response(422, json=detail))

    with pytest.raises(JevRequestError, match="Field required") as raised:
        endpoint.client().ask(STATE, QUESTIONS)

    assert raised.value.status == 422
    assert len(endpoint.requests) == 1


def test_overload_and_rate_limits_are_retried():
    endpoint = Endpoint(
        httpx.Response(529, headers={"retry-after-ms": "0"}),
        httpx.Response(429, headers={"retry-after": "0"}),
        ok(),
    )
    result = endpoint.client().ask(STATE, QUESTIONS)

    assert result["probe"].probability == 0.95
    assert len(endpoint.requests) == 3


def test_an_unreachable_host_reports_that_nothing_answered():
    """What a DNS failure or a proxy refusal looks like: no status at all."""
    endpoint = Endpoint(
        httpx.ConnectError("name does not resolve"),
        httpx.ConnectError("name does not resolve"),
    )

    with pytest.raises(JevRequestError) as raised:
        endpoint.client(max_retries=1).ask(STATE, QUESTIONS)

    assert raised.value.status is None
    assert len(endpoint.requests) == 2


# ---------------------------------------------------------------------------
# What thresholds are tagged with
# ---------------------------------------------------------------------------


def test_the_backend_is_named_for_the_model_version_it_pins(monkeypatch):
    monkeypatch.delenv("JEV_MODEL", raising=False)
    assert DEFAULT_MODEL == "jev-1.13.0"
    assert JevClient(api_key="k").name == "jev-1.13.0"
    assert JevClient(api_key="k", model="jev-preview").name == "jev-preview"


def test_thresholds_fitted_on_one_version_are_refused_on_another(tmp_path):
    from credit_extract.validate.calibrate import (
        BackendMismatch, Thresholds, load_thresholds,
    )

    path = Thresholds(version="6", backend="jev-1.13.0").save(tmp_path / "t.json")
    assert load_thresholds(path, backend="jev-1.13.0").backend == "jev-1.13.0"
    with pytest.raises(BackendMismatch):
        load_thresholds(path, backend="jev-1.14.0")
