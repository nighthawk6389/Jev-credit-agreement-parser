"""Shared fixtures.

Trap tests run against the genuine EDGAR exhibit when ``scripts/fetch_corpus.py``
has placed it on disk, and against the checked-in synthetic surrogate otherwise.
Both carry the same four traps, so the assertions are identical.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "credit_extract" / "eval" / "gold"
CORPUS = ROOT / "corpus"

REAL_DOCUMENT = CORPUS / "paya_gtcr_2017.htm"
SURROGATE = GOLD / "fixture_meridian_2017.html"


@pytest.fixture(scope="session")
def agreement_path() -> Path:
    if REAL_DOCUMENT.exists() and not os.environ.get("CREDIT_EXTRACT_FORCE_FIXTURE"):
        return REAL_DOCUMENT
    return SURROGATE


@pytest.fixture(scope="session")
def doc(agreement_path: Path):
    from credit_extract.ingest.normalize import ingest

    return ingest(agreement_path)


@pytest.fixture(scope="session")
def labels() -> dict:
    import json

    return json.loads((GOLD / "fixture_meridian_2017.labels.json").read_text())
