"""The labelling contract: the split, the scaffold, and the guide that binds them.

These are guards on a process rather than on a computation, which makes them
easy to write loosely. They are written tightly on purpose: each one fails for
exactly one reason, and that reason is a way the evidence base could quietly
stop meaning what it says.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from credit_extract.eval import split as split_mod
from credit_extract.eval.assertions import SCAFFOLD_MARKER, load_assertion_file
from credit_extract.eval.label import MIN_CRITICALITY
from credit_extract.models.fpml_model import FIELD_REGISTRY

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "labelling_guide.md"


def test_the_split_on_disk_is_the_one_the_rule_produces():
    """The whole value of the split is that nobody chose it.

    A split someone can edit after seeing the labels is not a holdout, it is a
    preference. The assignment is a function of the document names, so this
    recomputes it and compares -- which is also what CI does.
    """
    assert split_mod.check() == []


def test_a_document_cannot_be_quietly_moved_to_the_fit_side(tmp_path):
    original = split_mod.SPLIT_FILE.read_text()
    moved = re.sub(r"( \S+): holdout", r"\1: fit", original, count=1)
    assert moved != original, "the split has no holdout documents to move"

    tampered = tmp_path / "split.yaml"
    tampered.write_text(moved)
    problems = split_mod.check(tampered)
    assert problems, "moving a document between sides must be visible"
    assert "the derivation says holdout" in problems[0]


def test_every_stratum_contributes_a_holdout_document():
    """A holdout that skips a deal type cannot measure that deal type."""
    split = split_mod.load_split()
    held = {split_mod.stratum_of(d) for d in split.documents("holdout")}
    present = {split_mod.stratum_of(d) for d in split.assignment}
    assert present - held == set(), (
        f"no holdout document for strata {sorted(present - held)}"
    )


def test_documents_used_while_building_the_parser_stay_out_of_the_holdout():
    """They were read, and their quirks are in the patterns.

    A holdout containing a document the author already studied measures
    memorisation at best. The contaminated list is the record of which ones
    those are, and it only ever grows.
    """
    split = split_mod.load_split()
    assert split.contaminated, "the list cannot be empty; seven files were hand-written"
    for document in split.contaminated:
        assert split.side_of(document) != "holdout", document


def test_a_chain_member_resolves_to_one_side_of_the_split():
    """Label files name chain members by the tail of the harvest filename.

    If that lookup misses, a chain is extracted from documents on one side and
    scored as though it came from the other.
    """
    split = split_mod.load_split()
    assert split.side_of("ex-101xwheelsupamendno5toc") == "fit"
    assert split.side_of("no-such-document-anywhere") == "unassigned"


def test_the_scaffold_selects_the_registry_and_not_a_second_list():
    """37 fields at criticality 4+, which is the roadmap's 25-40 without a
    parallel schema that then drifts from the real one."""
    critical = [s for s in FIELD_REGISTRY.values() if s.criticality >= MIN_CRITICALITY]
    assert len(critical) == 37, (
        "the registry moved; update the count in docs/labelling_guide.md with it"
    )
    assert f"{len(critical)} fields" in GUIDE.read_text()


def test_a_scaffold_cannot_be_loaded_as_ground_truth(tmp_path):
    """It holds the extractor's own answers. Scoring against them would report
    perfect agreement and measure nothing."""
    scaffold = tmp_path / "scaffold.yaml"
    scaffold.write_text(
        "document: x\nsource: real\ntier: 2\nassertions:\n"
        "  - id: x_one\n"
        "    family: F01_integrity\n"
        "    member: duplicated_table_rows\n"
        "    kind: field_value\n"
        "    target: revolver.commitment\n"
        "    expect: 1\n"
        f"    note: >-  # {SCAFFOLD_MARKER}\n"
        "      a quote\n"
    )
    with pytest.raises(ValueError, match=SCAFFOLD_MARKER):
        load_assertion_file(scaffold)


def test_the_labels_that_exist_still_load():
    """The marker check runs on every load, so a false positive would take the
    whole evidence base offline."""
    labels = Path(__file__).resolve().parents[1] / "credit_extract" / "eval" / "labels"
    files = sorted(labels.glob("*.yaml"))
    assert len(files) >= 7
    for path in files:
        load_assertion_file(path)


def test_the_guide_describes_the_statuses_the_pipeline_actually_has():
    """A guide naming a status the code does not have sends a labeller to
    write assertions that can never pass."""
    from credit_extract.eval.assertions import CONFIDENT_STATUSES

    text = GUIDE.read_text()
    for status in CONFIDENT_STATUSES:
        assert f"`{status}`" in text, f"the guide does not tell a labeller about {status}"


def test_the_split_file_is_yaml_a_human_can_read():
    raw = yaml.safe_load(split_mod.SPLIT_FILE.read_text())
    assert raw["version"] == 1
    assert raw["holdout_in"] == split_mod.HOLDOUT_IN
    assert set(raw["assignment"].values()) == {"fit", "holdout"}
