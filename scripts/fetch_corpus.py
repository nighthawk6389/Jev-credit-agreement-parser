"""Fetch real credit agreements from SEC EDGAR.

    python scripts/fetch_corpus.py --primary
    python scripts/fetch_corpus.py --agent "Antares Capital" --limit 20

The primary test document named in the spec is the Paya / GTCR-Ultra
Acquisition credit agreement, Antares Capital LP as Administrative Agent, dated
1 August 2017. Once it is on disk at ``corpus/paya_gtcr_2017.htm`` the trap
tests prefer it over the synthetic surrogate automatically -- no other change
is needed.

    This build environment has no egress to sec.gov, so nothing here has been
    exercised against the live endpoint. It is written to EDGAR's documented
    interfaces and its rate limit, and it fails loudly rather than writing a
    partial file, but treat the first real run as unverified.

EDGAR requires a descriptive User-Agent with a contact address and asks for no
more than 10 requests a second; both are honoured below.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus"

PRIMARY_URL = (
    "https://www.sec.gov/Archives/edgar/data/1819881/000181988121000021/"
    "exhibit101-creditagreement.htm"
)
PRIMARY_NAME = "paya_gtcr_2017.htm"

FULL_TEXT_SEARCH = "https://efts.sec.gov/LATEST/search-index?q={query}&forms={forms}"
SEARCH_API = "https://efts.sec.gov/LATEST/search-index"

#: Administrative agents whose deals make a useful private-credit corpus.
AGENTS = (
    "Antares Capital", "Golub Capital", "Ares Capital", "Blue Owl",
    "Twin Brook", "Churchill Asset Management", "NXT Capital", "MidCap",
    "Monroe Capital", "Jefferies Finance", "HPS Investment Partners", "Barings",
)

#: EX-10 exhibits below this are rarely whole credit agreements.
MIN_BYTES = 150_000

#: EDGAR asks for no more than 10 requests a second.
REQUEST_INTERVAL = 0.15


def _user_agent() -> str:
    contact = os.environ.get("SEC_CONTACT_EMAIL")
    if not contact:
        raise SystemExit(
            "set SEC_CONTACT_EMAIL to a real contact address; EDGAR requires a "
            "descriptive User-Agent and will refuse anonymous scraping"
        )
    return f"credit-extract research tool ({contact})"


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=60.0,
        follow_redirects=True,
    )


def fetch_one(client: httpx.Client, url: str, destination: Path) -> Path | None:
    response = client.get(url)
    if response.status_code != 200:
        print(f"  HTTP {response.status_code} for {url}", file=sys.stderr)
        return None
    if len(response.content) < MIN_BYTES:
        print(f"  skipped {url}: {len(response.content):,} bytes is below the "
              f"{MIN_BYTES:,} floor for a whole agreement", file=sys.stderr)
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temporary file so an interrupted download never leaves a
    # half-written document that later looks like a real one.
    staging = destination.with_suffix(destination.suffix + ".part")
    staging.write_bytes(response.content)
    staging.replace(destination)
    print(f"  wrote {destination} ({len(response.content):,} bytes)")
    return destination


def fetch_primary() -> int:
    with _client() as client:
        result = fetch_one(client, PRIMARY_URL, CORPUS / PRIMARY_NAME)
    if result is None:
        return 1
    print(
        "\nThe trap tests will now run against the genuine exhibit instead of "
        "the synthetic surrogate. Set CREDIT_EXTRACT_FORCE_FIXTURE=1 to keep "
        "using the surrogate."
    )
    return 0


def search_by_agent(agent: str, limit: int) -> list[dict]:
    """EDGAR full-text search, filtered to EX-10 exhibits."""
    with _client() as client:
        response = client.get(
            SEARCH_API,
            params={"q": f'"{agent}"', "forms": "8-K,10-K,10-Q,S-1", "dateRange": "custom"},
        )
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
    return hits[:limit]


def fetch_by_agent(agent: str, limit: int) -> int:
    print(f"searching EDGAR full-text for {agent!r}")
    try:
        hits = search_by_agent(agent, limit)
    except httpx.HTTPError as exc:
        print(f"search failed: {exc}", file=sys.stderr)
        return 1
    if not hits:
        print("  no hits")
        return 0
    slug = agent.lower().replace(" ", "_")
    written = 0
    with _client() as client:
        for hit in hits:
            source = hit.get("_source", {})
            adsh = source.get("adsh", "").replace("-", "")
            cik = (source.get("ciks") or [""])[0]
            name = hit.get("_id", "").split(":")[-1]
            if not (adsh and cik and name.endswith((".htm", ".html"))):
                continue
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}/{name}"
            )
            if fetch_one(client, url, CORPUS / slug / name):
                written += 1
            time.sleep(REQUEST_INTERVAL)
    print(f"  {written} document(s) written under {CORPUS / slug}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--primary", action="store_true",
                        help="fetch the spec's primary test document")
    parser.add_argument("--agent", choices=AGENTS,
                        help="harvest by administrative-agent phrase")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.primary:
        return fetch_primary()
    if args.agent:
        return fetch_by_agent(args.agent, args.limit)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
