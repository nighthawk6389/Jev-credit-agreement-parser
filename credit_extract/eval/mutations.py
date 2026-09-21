"""Tier 3: mutate a known-good document to inject a known defect.

Ground truth is free because we created the defect, which makes this the only
way to get coverage on families where real examples are scarce -- most of F01
and F08, and all of F11. Synthetic and real metrics are reported separately and
never blended: a pipeline that catches a defect we injected in the exact shape
we injected it has demonstrated less than one that caught the same defect in a
real filing.

Two kinds of mutation, and both matter:

* **injection** -- the document is made wrong and the pipeline must say so.
  Nothing detected is a miss.
* **invariance** -- the document is rewritten to say the same thing a different
  way, and the pipeline must produce the same answer. "50 basis points" and
  "0.50%" are the same term; a figure under a "(in thousands)" header is the
  same money. A pipeline whose answer moves has a scale bug, and that is the
  error class that looks most reasonable on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Literal

from .assertions import Assertion

MutationKind = Literal["injection", "invariance"]


@dataclass
class MutationResult:
    """A mutated document and what must then be true of it."""

    mutation_id: str
    html: str
    assertions: list[Assertion]
    applied: bool = True
    note: str = ""


@dataclass
class Mutation:
    """One defect, injectable into any document that has the right shape."""

    id: str
    family: str
    member: str
    kind: MutationKind
    description: str
    apply: Callable[[str], MutationResult | None]
    requires: str = ""

    def __call__(self, html: str) -> MutationResult | None:
        return self.apply(html)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"<tr><td>([^<]*?)</td><td>([^<]*?)</td></tr>")


def _amortization_rows(html: str) -> list[re.Match[str]]:
    """Rows of the payment table: a date and a money amount."""
    return [
        m for m in _ROW_RE.finditer(html)
        if re.search(r"\d{4}", m.group(1)) and m.group(2).startswith("$")
    ]


def _assert(
    id: str, family: str, member: str, kind: str, **kwargs: Any
) -> Assertion:
    return Assertion(id=id, family=family, member=member, kind=kind, **kwargs)


# ---------------------------------------------------------------------------
# F01 -- integrity
# ---------------------------------------------------------------------------


def _duplicate_table_row(html: str) -> MutationResult | None:
    rows = _amortization_rows(html)
    if len(rows) < 6:
        return None
    target = rows[len(rows) // 2]
    mutated = html[:target.end()] + target.group(0) + html[target.end():]
    return MutationResult(
        mutation_id="duplicate_table_row",
        html=mutated,
        assertions=[
            _assert("mut_duplicate_row_dates", "F01_integrity",
                    "duplicated_table_rows", "invariant_fired",
                    target="amortization_dates_strictly_increasing", expect=True),
            _assert("mut_duplicate_row_count", "F01_integrity",
                    "duplicated_table_rows", "invariant_fired",
                    target="amortization_row_count_matches_quarters", expect=True),
        ],
        note=f"duplicated the row for {target.group(1)}",
    )


def _drop_table_row(html: str) -> MutationResult | None:
    rows = _amortization_rows(html)
    if len(rows) < 6:
        return None
    target = rows[len(rows) // 3]
    mutated = html[:target.start()] + html[target.end():]
    return MutationResult(
        mutation_id="drop_table_row",
        html=mutated,
        assertions=[
            # A dropped row leaves a hole in the cycle: the remaining dates are
            # still strictly increasing, so only the spacing check sees it.
            _assert("mut_drop_row_spacing", "F01_integrity",
                    "dropped_table_rows", "invariant_fired",
                    target="amortization_dates_evenly_spaced", expect=True),
        ],
        note=f"removed the row for {target.group(1)}",
    )


def _break_cross_reference(html: str) -> MutationResult | None:
    match = re.search(r"Section (\d+\.\d+)", html)
    if match is None:
        return None
    from ..ingest.normalize import detect_sections, normalize_chars
    if not detect_sections(normalize_chars(re.sub(r"<[^>]+>", " ", html))):
        # An amendment has no sections of its own -- every "Section 2.10" in
        # it points into the base agreement. cross_references_resolve cannot
        # adjudicate a reference into a document it does not have, and making
        # it try would fire on every legitimate citation in every amendment,
        # which is the false-positive class this check was narrowed to avoid.
        # So the defect is not injectable here, and a mutation that cannot be
        # caught is not evidence that anything is broken.
        return None
    mutated = html[:match.start()] + "Section 99.99" + html[match.end():]
    return MutationResult(
        mutation_id="break_cross_reference",
        html=mutated,
        assertions=[
            _assert("mut_broken_xref", "F01_integrity", "broken_cross_reference",
                    "invariant_fired", target="cross_references_resolve",
                    expect=True),
        ],
        note=f"repointed a reference from {match.group(0)} to Section 99.99",
    )


def _numeral_word_mismatch(html: str) -> MutationResult | None:
    match = re.search(r"(\d+)% of Consolidated EBITDA", html)
    if match is None:
        return None
    replacement = f"twenty-five percent ({match.group(1)}%) of Consolidated EBITDA"
    mutated = html[:match.start()] + replacement + html[match.end():]
    if match.group(1) == "25":
        return None                     # would agree, and so not a defect
    return MutationResult(
        mutation_id="numeral_word_mismatch",
        html=mutated,
        assertions=[
            _assert("mut_numeral_word", "F01_integrity", "numeral_word_mismatch",
                    "invariant_fired", target="numeral_and_words_agree",
                    expect=True),
        ],
        note=f"spelled {match.group(1)}% as twenty-five percent",
    )


def _doubly_defined_term(html: str) -> MutationResult | None:
    match = re.search(
        r'<p><b>"Revolving Credit Maturity Date"</b>[^<]*</p>', html
    )
    if match is None:
        return None
    rival = (
        '<p><b>"Revolving Credit Maturity Date"</b> means August 1, 2023.</p>'
    )
    mutated = html[:match.end()] + rival + html[match.end():]
    return MutationResult(
        mutation_id="doubly_defined_term",
        html=mutated,
        assertions=[
            _assert("mut_double_definition", "F01_integrity",
                    "doubly_defined_term", "invariant_fired",
                    target="defined_terms_are_unique", expect=True),
        ],
        note="defined the revolver maturity twice, with different dates",
    )


def _referenced_schedule_absent(html: str) -> MutationResult | None:
    if "Schedule 6.01</p>" not in html:
        return None
    mutated = html.replace(
        "<p>Schedule 6.01</p>"
        "<p>Existing Indebtedness: none as of the Closing Date.</p>", "", 1
    )
    if mutated == html:
        return None
    return MutationResult(
        mutation_id="referenced_schedule_absent",
        html=mutated,
        assertions=[
            _assert("mut_absent_schedule", "F01_integrity",
                    "referenced_schedule_absent", "invariant_fired",
                    target="referenced_schedules_present", expect=True),
        ],
        note="removed a schedule the agreement cites, with no omission declared",
    )


# ---------------------------------------------------------------------------
# F08 -- units. These are invariance mutations: the document says the same
# thing a different way, and the answer must not move.
# ---------------------------------------------------------------------------


def _bps_instead_of_percent(html: str) -> MutationResult | None:
    match = re.search(r"by more than (\d)\.(\d\d)% per annum", html)
    if match is None:
        return None
    basis_points = int(match.group(1)) * 100 + int(match.group(2))
    mutated = html.replace(
        match.group(0), f"by more than {basis_points} basis points per annum"
    )
    return MutationResult(
        mutation_id="bps_instead_of_percent",
        html=mutated,
        assertions=[
            _assert("mut_bps_same_value", "F08_units", "bps_vs_percent",
                    "field_value", target="mfn_threshold_pct",
                    expect=float(f"{match.group(1)}.{match.group(2)}"),
                    note="50 basis points and 0.50% are the same term"),
        ],
        note=f"restated {match.group(0).strip()} as {basis_points} basis points",
    )


def _thousands_scale(html: str) -> MutationResult | None:
    """Rewrite the commitment table in thousands, under a scale header.

    The digits in the cell change and the money does not. A pipeline that reads
    the cell and ignores the header is wrong by a factor of a thousand, and the
    output looks entirely reasonable.
    """
    header = "<th>Aggregate Commitment</th>"
    if header not in html:
        return None
    mutated = html.replace(
        header, "<th>Aggregate Commitment (in thousands)</th>", 1
    )

    def scale_down(match: re.Match[str]) -> str:
        amount = int(match.group(1).replace(",", ""))
        if amount < 1000 or amount % 1000:
            return match.group(0)
        return f"<td>${amount // 1000:,}</td>"

    mutated, count = re.subn(
        r"<td>\$([\d,]+)</td>", scale_down, mutated
    )
    if not count:
        return None
    return MutationResult(
        mutation_id="thousands_scale",
        html=mutated,
        assertions=[
            _assert("mut_thousands_commitment", "F08_units",
                    "thousands_vs_millions", "field_value",
                    target="initial_term_loan.commitment", expect=150_500_000,
                    note="1,250 under a thousands header is $1,250,000"),
        ],
        note="restated the commitment table in thousands under a scale header",
    )


# ---------------------------------------------------------------------------
# F11 -- layout
# ---------------------------------------------------------------------------


#: The fixture's quarterly instalment. Both assertions below are about this
#: one schedule -- the amount is its amount, and the invariant is expected to
#: fire because the fixture's dates carry trap 1 -- so the mutation may only be
#: applied to a document that actually contains it. Without that check the
#: shape test is "a table mentioning Payment Date", which any real amortisation
#: schedule satisfies: Crane NXT's Sixth Amendment has one stated in
#: percentages of the original principal, and the mutation wrapped it and then
#: asserted the fixture's money amount and the fixture's date defect about it.
#: Both assertions are scored confident, so the second one landed as a silent
#: error against a document that had done nothing wrong.
_FIXTURE_INSTALMENT = "376,250"


def _nested_html_tables(html: str) -> MutationResult | None:
    """Wrap the payment table in an outer table, as filers' tooling does."""
    match = re.search(
        r'<table[^>]*><caption>?.*?Payment Date.*?</table>', html, re.DOTALL
    )
    if match is None:
        match = re.search(
            r'<table[^>]*>(?:(?!</table>).)*?Payment Date(?:(?!</table>).)*?</table>',
            html, re.DOTALL,
        )
    if match is None:
        return None
    if _FIXTURE_INSTALMENT not in match.group(0):
        # An amortisation table, but not the one these assertions describe.
        return None
    wrapped = (
        '<table border="0"><tr><td>' + match.group(0) + "</td></tr></table>"
    )
    mutated = html[:match.start()] + wrapped + html[match.end():]
    return MutationResult(
        mutation_id="nested_html_tables",
        html=mutated,
        assertions=[
            _assert("mut_nested_trap1_dates", "F11_layout", "nested_html_tables",
                    "invariant_fired",
                    target="amortization_dates_strictly_increasing", expect=True,
                    note="nesting must not disarm the table invariants"),
            _assert("mut_nested_amount", "F11_layout", "nested_html_tables",
                    "field_value", target="amortization.quarterly_amount",
                    expect=int(_FIXTURE_INSTALMENT.replace(",", ""))),
        ],
        note="wrapped the payment table inside an outer single-cell table",
    )


def _grid_rendered_as_text(html: str) -> MutationResult | None:
    """Render the pricing grid as prose, the way a PDF-to-text pass leaves it."""
    match = re.search(
        r'<table[^>]*>(?:(?!</table>).)*?Eurodollar Rate(?:(?!</table>).)*?</table>',
        html, re.DOTALL,
    )
    if match is None:
        return None
    rows = re.findall(
        r"<t[dh]>([^<]*)</t[dh]>", match.group(0)
    )
    lines = [
        "    ".join(rows[i:i + 4]) for i in range(0, len(rows) - 3, 4)
    ]
    as_text = "<p>" + "<br>".join(lines) + "</p>"
    mutated = html[:match.start()] + as_text + html[match.end():]
    return MutationResult(
        mutation_id="grid_rendered_as_text",
        html=mutated,
        assertions=[
            _assert("mut_grid_as_text_margin", "F11_layout",
                    "grid_rendered_as_text", "field_value",
                    target="applicable_margin.eurodollar_top_level_pct",
                    expect=5.00,
                    note="the grid is still a grid when the HTML stops saying so"),
        ],
        note="flattened the pricing grid into whitespace-separated text",
    )


def _table_split_across_pages(html: str) -> MutationResult | None:
    """Split the payment table in two, as a page break does in a filing."""
    rows = _amortization_rows(html)
    if len(rows) < 8:
        return None
    pivot = rows[len(rows) // 2]
    head_end = pivot.start()
    mutated = (
        html[:head_end]
        + '</table><hr><table border="1" cellpadding="4">'
          "<tr><th>Payment Date</th><th>Principal Amortization Payment</th></tr>"
        + html[head_end:]
    )
    return MutationResult(
        mutation_id="table_split_across_pages",
        html=mutated,
        assertions=[
            _assert("mut_split_amount", "F11_layout", "table_split_across_pages",
                    "field_value", target="amortization.quarterly_amount",
                    expect=376250,
                    note="a table split by a page break is still one schedule"),
        ],
        note="split the payment table across a page break",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

MUTATIONS: tuple[Mutation, ...] = (
    Mutation("duplicate_table_row", "F01_integrity", "duplicated_table_rows",
             "injection", "repeat a payment row", _duplicate_table_row,
             requires="an amortization table"),
    Mutation("drop_table_row", "F01_integrity", "dropped_table_rows",
             "injection", "remove a payment row", _drop_table_row,
             requires="an amortization table"),
    Mutation("break_cross_reference", "F01_integrity", "broken_cross_reference",
             "injection", "point a cross-reference at nothing",
             _break_cross_reference),
    Mutation("numeral_word_mismatch", "F01_integrity", "numeral_word_mismatch",
             "injection", "spell a percentage as a different number",
             _numeral_word_mismatch),
    Mutation("doubly_defined_term", "F01_integrity", "doubly_defined_term",
             "injection", "define one term twice with different values",
             _doubly_defined_term),
    Mutation("referenced_schedule_absent", "F01_integrity",
             "referenced_schedule_absent", "injection",
             "remove a cited schedule", _referenced_schedule_absent),
    Mutation("bps_instead_of_percent", "F08_units", "bps_vs_percent",
             "invariance", "restate a percentage in basis points",
             _bps_instead_of_percent),
    Mutation("thousands_scale", "F08_units", "thousands_vs_millions",
             "invariance", "restate a table in thousands under a scale header",
             _thousands_scale),
    Mutation("nested_html_tables", "F11_layout", "nested_html_tables",
             "invariance", "wrap a table inside another table",
             _nested_html_tables),
    Mutation("grid_rendered_as_text", "F11_layout", "grid_rendered_as_text",
             "invariance", "flatten a grid into text", _grid_rendered_as_text),
    Mutation("table_split_across_pages", "F11_layout",
             "table_split_across_pages", "invariance",
             "split a table across a page break", _table_split_across_pages),
)


def apply_all(html: str) -> list[MutationResult]:
    """Every mutation this document has the right shape for."""
    out: list[MutationResult] = []
    for mutation in MUTATIONS:
        result = mutation(html)
        if result is None:
            continue
        if result.html == html:
            continue                    # a no-op mutation proves nothing
        out.append(result)
    return out


def by_family() -> dict[str, list[Mutation]]:
    grouped: dict[str, list[Mutation]] = {}
    for mutation in MUTATIONS:
        grouped.setdefault(mutation.family, []).append(mutation)
    return grouped
