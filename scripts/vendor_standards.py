"""Vendor the open standards this project composes.

FIBO term URIs and the ACTUS data dictionary are pulled from their upstream
repositories and written into ``credit_extract/models/vendored/`` so that the
term names in every output are the real published ones, and so that CI runs
offline and deterministically.

    python scripts/vendor_standards.py [--check]

``--check`` re-fetches and fails if upstream has drifted from what is checked
in, which is the signal to review the mapping rather than silently re-map.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "credit_extract" / "models" / "vendored"

FIBO_RAW = "https://raw.githubusercontent.com/edmcouncil/fibo/master/"
FIBO_MODULES = [
    "LOAN/LoansGeneral/Loans.rdf",
    "FBC/DebtAndEquities/Debt.rdf",
    "FBC/DebtAndEquities/Guaranty.rdf",
    "FBC/ProductsAndServices/FinancialProductsAndServices.rdf",
    "FBC/ProductsAndServices/ClientsAndAccounts.rdf",
    "FBC/FinancialInstruments/FinancialInstruments.rdf",
    "FND/Parties/Parties.rdf",
    "FND/ProductsAndServices/ProductsAndServices.rdf",
    "FND/ProductsAndServices/PaymentsAndSchedules.rdf",
    "FND/Agreements/Agreements.rdf",
    "FND/Agreements/Contracts.rdf",
    "FND/DatesAndTimes/FinancialDates.rdf",
    "FND/Accounting/CurrencyAmount.rdf",
]

ACTUS_RAW = "https://raw.githubusercontent.com/actusfrf/actus-dictionary/master/"
ACTUS_FILES = {
    "taxonomy": "actus-dictionary.json",
    "terms": "actus-dictionary-terms.json",
}

_ENTITY_RE = re.compile(r'<!ENTITY\s+([a-zA-Z0-9-]+)\s+"([^"]*)"')
_DECL_RE = re.compile(
    r'<owl:(Class|ObjectProperty|DatatypeProperty)\s+rdf:about="([^"]+)"'
)
_LABEL_RE = re.compile(r"<rdfs:label[^>]*>([^<]+)</rdfs:label>")


def _get(url: str) -> str:
    response = httpx.get(url, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _repair_actus_json(raw: str) -> str:
    """The upstream dictionary ships curly quotes as string delimiters.

    Left alone this is not valid JSON, so re-encode each curly-quoted run as a
    proper JSON string rather than blind-replacing the characters (descriptions
    contain straight quotes of their own).
    """
    return re.sub(
        r"“([^“”]*)”",
        lambda m: json.dumps(m.group(1)),
        raw,
    )


def harvest_fibo() -> dict:
    prefixes: dict[str, str] = {}
    terms: dict[str, dict] = {}
    for module in FIBO_MODULES:
        text = _get(FIBO_RAW + module)
        entities = dict(_ENTITY_RE.findall(text))
        prefixes.update(
            {k: v for k, v in entities.items() if k.startswith(("fibo-", "cmns-"))}
        )
        # Split on declarations so each label lands with its own subject.
        for match in _DECL_RE.finditer(text):
            kind, about = match.group(1), match.group(2)
            ref = re.match(r"&([a-zA-Z0-9-]+);(.+)$", about)
            if not ref:
                continue
            prefix, local = ref.group(1), ref.group(2)
            if not prefix.startswith("fibo-"):
                continue
            tail = text[match.end() : match.end() + 1200]
            label = _LABEL_RE.search(tail)
            curie = f"{prefix}:{local}"
            terms.setdefault(
                curie,
                {
                    "uri": entities.get(prefix, "") + local,
                    "label": label.group(1).strip() if label else local,
                    "kind": kind,
                    "module": module,
                },
            )
    return {
        "source": FIBO_RAW,
        "license": "MIT (EDM Council FIBO)",
        "modules": FIBO_MODULES,
        "prefixes": dict(sorted(prefixes.items())),
        "terms": dict(sorted(terms.items())),
    }


def harvest_actus() -> dict:
    taxonomy = json.loads(_repair_actus_json(_get(ACTUS_RAW + ACTUS_FILES["taxonomy"])))
    terms = json.loads(_repair_actus_json(_get(ACTUS_RAW + ACTUS_FILES["terms"])))
    slim_terms = {
        key: {
            "identifier": value.get("identifier", key),
            "acronym": value.get("acronym", ""),
            "name": value.get("name", ""),
            "group": value.get("group", ""),
            "type": value.get("type", ""),
            "allowedValues": value.get("allowedValues", []),
        }
        for key, value in terms.get("terms", {}).items()
    }
    slim_taxonomy = {
        key: {
            "identifier": value.get("identifier", key),
            "acronym": value.get("acronym", ""),
            "name": value.get("name", ""),
            "family": value.get("family", ""),
            "class": value.get("class", ""),
            "description": value.get("description", ""),
            "status": value.get("status", ""),
        }
        for key, value in taxonomy.get("taxonomy", {}).items()
    }
    return {
        "source": ACTUS_RAW,
        "license": "Apache-2.0 (ACTUS Financial Research Foundation)",
        "version": terms.get("version", {}),
        "contract_types": dict(sorted(slim_taxonomy.items())),
        "terms": dict(sorted(slim_terms.items())),
    }


def write(path: Path, payload: dict, check: bool) -> bool:
    body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if check:
        if not path.exists():
            print(f"MISSING {path}")
            return False
        if path.read_text() != body:
            print(f"DRIFT   {path} differs from upstream")
            return False
        print(f"ok      {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    print(f"wrote   {path} ({len(body):,} bytes)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ok = True
    ok &= write(VENDOR / "fibo_terms.json", harvest_fibo(), args.check)
    ok &= write(VENDOR / "actus_dictionary.json", harvest_actus(), args.check)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
