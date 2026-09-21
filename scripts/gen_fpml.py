"""Vendor, check, or generate bindings from the FpML loan schemas.

FpML's download page requires an authenticated session. A public mirror copy
of the published schemas is pinned here so builds stay
reproducible, while the official FpML specification page remains normative.

    python scripts/gen_fpml.py --check     # offline: do our terms resolve?
    python scripts/gen_fpml.py --drift     # online: has upstream moved?
    python scripts/gen_fpml.py --vendor    # online: refresh the snapshot
    python scripts/gen_fpml.py --verify    # alias for --drift
    python scripts/gen_fpml.py --generate  # optional xsdata generation

The two checks answer different questions and only one of them needs a
network. ``--check`` reads the checked-in index and asks whether every FpML
term this repository maps is declared in it -- a repository invariant, so it
runs on every build. ``--drift`` re-fetches the pinned bytes and asks whether
someone else's copy has changed underneath us, which is informational and
cannot be allowed to fail a pull request. Welding them together would have
made the invariant untestable without egress, which is how it ends up
untested.

    Index caveat: FpML declares the same element name in more than one scope
    -- ``delayedDraw`` is both a boolean flag on a term loan and a member of
    the facility substitution group -- and the index merges those
    declarations under one entry. So a name in the index is a name the
    schemas declare somewhere, not a name that is legal in a given position.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA_DIR = ROOT / "vendor" / "fpml"
GENERATED = ROOT / "credit_extract" / "models" / "fpml_generated"
VENDORED_INDEX = ROOT / "credit_extract" / "models" / "vendored" / "fpml_terms.json"

FPML_VERSION = "5-13-7-rec-1"
FPML_NAMESPACE = "http://www.fpml.org/FpML-5/confirmation"
OFFICIAL_SOURCE = "https://www.fpml.org/spec/fpml-5-13-7-rec-1/"
MIRROR_COMMIT = "9793d48b501e775fb6d3151ab86d91bd370e81f3"
MIRROR_ROOT = (
    "https://raw.githubusercontent.com/rosetta-models/rune-fpml/"
    f"{MIRROR_COMMIT}/rosetta-source/src/main/resources/schemas/"
    f"fpml-{FPML_VERSION}/confirmation/"
)
SCHEMAS = (
    "fpml-loan-5-13.xsd",
    "fpml-shared-5-13.xsd",
    "fpml-asset-5-13.xsd",
)
XSD = "{http://www.w3.org/2001/XMLSchema}"


def _get(url: str) -> bytes:
    response = httpx.get(url, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _documentation(node: ET.Element) -> str | None:
    docs = [
        " ".join("".join(doc.itertext()).split())
        for doc in node.findall(f"./{XSD}annotation/{XSD}documentation")
    ]
    value = " ".join(doc for doc in docs if doc)
    return value or None


def _declarations(schema: bytes, filename: str, tag: str) -> dict[str, dict]:
    root = ET.fromstring(schema)
    declarations: dict[str, dict] = {}
    for node in root.iter(f"{XSD}{tag}"):
        name = node.get("name")
        if not name:
            continue
        item = declarations.setdefault(
            name,
            {"schemas": [], "types": [], "substitution_groups": [], "descriptions": []},
        )
        if filename not in item["schemas"]:
            item["schemas"].append(filename)
        if node.get("type") and node.get("type") not in item["types"]:
            item["types"].append(node.get("type"))
        if (group := node.get("substitutionGroup")) and group not in item["substitution_groups"]:
            item["substitution_groups"].append(group)
        if (description := _documentation(node)) and description not in item["descriptions"]:
            item["descriptions"].append(description)
    return declarations


def _merge(target: dict[str, dict], source: dict[str, dict]) -> None:
    for name, incoming in source.items():
        current = target.setdefault(
            name,
            {"schemas": [], "types": [], "substitution_groups": [], "descriptions": []},
        )
        for key, values in incoming.items():
            for value in values:
                if value not in current[key]:
                    current[key].append(value)


def harvest() -> tuple[dict, dict[str, bytes]]:
    elements: dict[str, dict] = {}
    complex_types: dict[str, dict] = {}
    files: list[dict] = []
    bodies: dict[str, bytes] = {}
    for filename in SCHEMAS:
        url = MIRROR_ROOT + filename
        body = _get(url)
        ET.fromstring(body)  # reject a login/error page before recording it
        bodies[filename] = body
        files.append(
            {
                "name": filename,
                "url": url,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        _merge(elements, _declarations(body, filename, "element"))
        _merge(complex_types, _declarations(body, filename, "complexType"))

    payload = {
        "standard": "FpML",
        "publisher": "ISDA",
        "version": FPML_VERSION,
        "namespace": FPML_NAMESPACE,
        "official_source": OFFICIAL_SOURCE,
        "mirror_commit": MIRROR_COMMIT,
        "schema_files": files,
        "elements": dict(sorted(elements.items())),
        "complex_types": dict(sorted(complex_types.items())),
    }
    return payload, bodies


def encode(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _write(payload: dict, bodies: dict[str, bytes]) -> None:
    VENDORED_INDEX.parent.mkdir(parents=True, exist_ok=True)
    VENDORED_INDEX.write_text(encode(payload))
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, body in bodies.items():
        (SCHEMA_DIR / filename).write_bytes(body)
    print(f"wrote   {VENDORED_INDEX} ({len(payload['elements']):,} elements)")


def drift() -> bool:
    """Has the upstream copy moved since the snapshot was taken?"""
    payload, _ = harvest()
    if not VENDORED_INDEX.exists():
        print(f"MISSING {VENDORED_INDEX}; run --vendor")
        return False
    if VENDORED_INDEX.read_text() != encode(payload):
        print(f"DRIFT   {VENDORED_INDEX}")
        for file in payload["schema_files"]:
            print(f"  upstream {file['name']:26} sha256={file['sha256'][:16]} "
                  f"{file['bytes']:,} bytes")
        return False
    print(f"ok      {VENDORED_INDEX} matches upstream "
          f"({len(payload['elements']):,} elements)")
    return True


def check_bindings() -> bool:
    """Offline: does every mapped FpML term resolve against the snapshot?

    ``fpml()`` raises on a name outside the snapshot, so the two binding
    functions check themselves the moment they are called. ``FIELD_REGISTRY``
    is the one they do not cover: ``standard_term`` is a plain string there,
    so a term that does not exist reaches a report looking like every other
    one. That is the entry this check exists for.
    """
    from credit_extract.models.fpml_model import mapped_terms

    if not VENDORED_INDEX.exists():
        print(f"MISSING {VENDORED_INDEX}; run --vendor", file=sys.stderr)
        return False
    index = json.loads(VENDORED_INDEX.read_text())
    indexed = index["elements"]
    terms = sorted(mapped_terms())
    for term in terms:
        entry = indexed.get(term)
        where = ",".join(
            name.replace("fpml-", "").replace("-5-13.xsd", "")
            for name in entry["schemas"]
        ) if entry else "-"
        print(f"{'ok' if entry else 'MISSING':7} fpml:{term:34} [{where}]")
    missing = [term for term in terms if term not in indexed]
    print(f"\n{len(terms) - len(missing)}/{len(terms)} mapped terms declared in "
          f"{len(index['schema_files'])} pinned schemas "
          f"({len(indexed):,} elements indexed)")
    return not missing


def generate() -> int:
    try:
        import xsdata  # noqa: F401
    except ImportError:
        print("xsdata is not installed: pip install 'xsdata[cli]'", file=sys.stderr)
        return 1
    if not SCHEMA_DIR.exists():
        payload, bodies = harvest()
        _write(payload, bodies)
    GENERATED.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "xsdata", "generate",
        "--package", "credit_extract.models.fpml_generated",
        "--structure-style", "single-package",
        str(SCHEMA_DIR / "fpml-loan-5-13.xsd"),
    ]
    print(" ".join(command))
    return subprocess.call(command, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true",
                        help="offline: every mapped term resolves in the snapshot")
    action.add_argument("--drift", action="store_true",
                        help="online: the snapshot still matches upstream")
    action.add_argument("--vendor", action="store_true",
                        help="online: refresh the snapshot from the pinned commit")
    action.add_argument("--verify", action="store_true", help="alias for --drift")
    action.add_argument("--generate", action="store_true",
                        help="run xsdata over the loan schema")
    args = parser.parse_args(argv)
    if args.generate:
        return generate()
    if args.check:
        return 0 if check_bindings() else 1
    if args.vendor:
        payload, bodies = harvest()
        _write(payload, bodies)
        return 0 if check_bindings() else 1
    return 0 if drift() and check_bindings() else 1


if __name__ == "__main__":
    sys.exit(main())
