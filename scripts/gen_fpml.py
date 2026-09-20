"""Generate or verify the FpML loan product bindings.

    python scripts/gen_fpml.py --verify     # check the hand-mapped names
    python scripts/gen_fpml.py --generate   # run xsdata over the XSDs

``credit_extract/models/fpml_model.py`` hand-maps FpML 5.x loan product element
names and marks every binding ``verified=False``, because fpml.org was not
reachable from the environment this project was built in. FIBO and ACTUS terms
are vendored from upstream and carry ``verified=True``; FpML is the one
standard here whose term names have not been checked against a source of truth,
and the output says so per field rather than hiding it.

``--verify`` downloads the published schemas and reports which hand-mapped
names actually exist, so the bindings can be promoted (or corrected) from a
network that can reach fpml.org.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "vendor" / "fpml"
GENERATED = ROOT / "credit_extract" / "models" / "fpml_generated"

#: FpML publishes the confirmation view as a zip of XSDs.
FPML_ZIP = "https://www.fpml.org/spec/fpml-5-13-3-rec-1.zip"

#: Loan product schemas within that archive.
LOAN_SCHEMAS = ("fpml-loan-5-13.xsd", "fpml-business-events-5-13.xsd")


def _hand_mapped_terms() -> set[str]:
    from credit_extract.models.fpml_model import FIELD_REGISTRY, facility_bindings

    terms = {
        binding.term.split(":", 1)[1]
        for binding in facility_bindings().values()
        if binding.term
    }
    terms |= {
        spec.standard_term.split(":", 1)[1]
        for spec in FIELD_REGISTRY.values()
        if spec.standard_term and spec.standard_term.startswith("fpml:")
    }
    return terms


def download_schemas() -> Path:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"downloading {FPML_ZIP}")
    response = httpx.get(FPML_ZIP, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        archive.extractall(SCHEMA_DIR)
    print(f"extracted to {SCHEMA_DIR}")
    return SCHEMA_DIR


def verify() -> int:
    """Check every hand-mapped element name against the published XSDs."""
    try:
        directory = download_schemas()
    except httpx.HTTPError as exc:
        print(
            f"could not reach fpml.org: {exc}\n"
            "The bindings stay verified=False, which is the correct state "
            "until they can be checked. Nothing else changes.",
            file=sys.stderr,
        )
        return 1

    declared: set[str] = set()
    for xsd in directory.rglob("*.xsd"):
        text = xsd.read_text(encoding="utf-8", errors="replace")
        declared.update(re.findall(r'<xsd:element[^>]+name="([^"]+)"', text))
        declared.update(re.findall(r'<xs:element[^>]+name="([^"]+)"', text))

    mapped = _hand_mapped_terms()
    found = sorted(mapped & declared)
    missing = sorted(mapped - declared)

    print(f"\n{len(declared):,} element names declared across the schemas")
    print(f"{len(found)}/{len(mapped)} hand-mapped names confirmed")
    for name in found:
        print(f"  ok       fpml:{name}")
    for name in missing:
        print(f"  MISSING  fpml:{name}")
    if missing:
        print(
            "\nCorrect the names in credit_extract/models/fpml_model.py before "
            "promoting any binding to verified=True."
        )
        return 1
    print(
        "\nAll hand-mapped names exist. Set verified=True in fpml() and record "
        "the schema version in FPML_VERSION."
    )
    return 0


def generate() -> int:
    """Run xsdata over the loan schemas to produce real dataclasses."""
    import importlib.util

    if importlib.util.find_spec("xsdata") is None:
        print(
            "xsdata is not installed: pip install 'xsdata[cli]'",
            file=sys.stderr,
        )
        return 1
    import subprocess

    directory = download_schemas()
    GENERATED.mkdir(parents=True, exist_ok=True)
    targets = [str(directory / name) for name in LOAN_SCHEMAS
               if (directory / name).exists()]
    if not targets:
        targets = [str(p) for p in directory.rglob("*loan*.xsd")]
    if not targets:
        print("no loan schemas found in the archive", file=sys.stderr)
        return 1
    command = [
        sys.executable, "-m", "xsdata", "generate",
        "--package", "credit_extract.models.fpml_generated",
        "--structure-style", "single-package", *targets,
    ]
    print(" ".join(command))
    return subprocess.call(command, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args(argv)
    if args.verify:
        return verify()
    if args.generate:
        return generate()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
