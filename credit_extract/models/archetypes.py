"""Deal archetypes, detected before extraction and used to route it.

An ABL has a borrowing base where a term loan has a commitment. A
recurring-revenue loan has no EBITDA anywhere, so every EBITDA-keyed extractor
returns null and every EBITDA-keyed invariant fires -- on a completely correct
document. A NAV line is covenanted on portfolio value.

Without this, recall on a mixed corpus means nothing: the pipeline is penalised
for not finding an EBITDA covenant in a loan that has none by design, and a
reviewer who sees a few of those stops reading the rest.

A field the archetype rules out resolves to ``not_applicable_to_archetype``,
which is a fifth status and deliberately distinct from
``absent_from_document``. "This deal kind has no such term" and "this agreement
is silent on a term it could have had" are different findings.
"""

from __future__ import annotations

import fnmatch
from typing import Literal

from pydantic import BaseModel, Field

Archetype = Literal[
    "cash_flow_term_loan",
    "abl_revolver",
    "second_lien",
    "recurring_revenue",
    "nav_or_subscription",
    "unitranche",
    "holdco_pik",
    "project_finance",
    "dip",
    "venture_debt",
    "investment_grade",
    "european_lma",
    "unknown",
]

#: How many characters of the document the classifier reads. Archetype is
#: usually decided by the title page and the first few definitions.
DETECTION_WINDOW = 30_000


class FieldGroup(BaseModel):
    """A set of fields that stand or fall together under an archetype."""

    id: str
    patterns: list[str]
    description: str = ""

    def matches(self, field_name: str) -> bool:
        return any(fnmatch.fnmatch(field_name, p) for p in self.patterns)


FIELD_GROUPS: dict[str, FieldGroup] = {
    group.id: group
    for group in [
        FieldGroup(
            id="ebitda_covenants",
            description="anything measured against Consolidated EBITDA",
            patterns=[
                "financial_covenant.*", "opening_total_leverage_ratio",
                "consolidated_ebitda.*", "incremental.leverage_based_test",
            ],
        ),
        FieldGroup(
            id="ebitda_baskets",
            description="baskets denominated as a percentage of EBITDA",
            patterns=["indebtedness.purchase_money_basket_ebitda_pct"],
        ),
        FieldGroup(
            id="term_amortization",
            description="scheduled principal repayment on a term loan",
            patterns=["amortization.*", "initial_term_loan.*", "delayed_draw.*"],
        ),
        FieldGroup(
            id="revolver_mechanics",
            description="revolver commitment, unused fee, LC sublimit",
            patterns=["revolver.*", "commitment_fee_pct", "lc_sublimit",
                      "fronting_fee_pct"],
        ),
        FieldGroup(
            id="borrowing_base",
            description="advance rates against eligible collateral",
            patterns=["borrowing_base.*"],
        ),
        FieldGroup(
            id="recurring_revenue",
            description="ARR-keyed covenants and liquidity tests",
            patterns=["arr.*"],
        ),
        FieldGroup(
            id="nav_tests",
            description="loan-to-value against portfolio net asset value",
            patterns=["nav.*"],
        ),
        FieldGroup(
            id="pik_mechanics",
            description="payment-in-kind toggle and step-up",
            patterns=["pik.*"],
        ),
        FieldGroup(
            id="mfn",
            description="most-favoured-nation pricing protection",
            patterns=["mfn_*"],
        ),
    ]
}


class ArchetypeProfile(BaseModel):
    """What a deal of this kind does and does not have."""

    archetype: Archetype
    title: str
    expected_groups: list[str] = Field(default_factory=list)
    inapplicable_groups: list[str] = Field(default_factory=list)
    #: Invariants that assume a term this archetype lacks.
    inapplicable_invariants: list[str] = Field(default_factory=list)
    #: Vocabulary that essentially settles the classification. "Borrowing
    #: base" appears in ABLs and almost nowhere else.
    decisive_signals: list[str] = Field(default_factory=list)
    #: Vocabulary consistent with this archetype but shared with others.
    #: "Consolidated EBITDA" is in nearly every cash-flow-style agreement
    #: including second-lien and unitranche ones, so counting it decides
    #: nothing.
    supporting_signals: list[str] = Field(default_factory=list)
    #: True for the archetype that wins when nothing decisive fires.
    residual: bool = False
    criteria: str = ""
    rationale: str = ""

    def applies_to(self, field_name: str) -> bool:
        return not any(
            FIELD_GROUPS[g].matches(field_name)
            for g in self.inapplicable_groups
            if g in FIELD_GROUPS
        )

    def why_not(self, field_name: str) -> str | None:
        for group_id in self.inapplicable_groups:
            group = FIELD_GROUPS.get(group_id)
            if group and group.matches(field_name):
                return (
                    f"{self.title} deals have no {group.description}; "
                    f"field group {group_id!r} does not apply"
                )
        return None


_EBITDA_INVARIANTS = [
    "opening_leverage_consistent",
    "ebitda_baskets_have_identified_base",
]

PROFILES: dict[str, ArchetypeProfile] = {
    profile.archetype: profile
    for profile in [
        ArchetypeProfile(
            archetype="cash_flow_term_loan",
            title="cash-flow term loan",
            expected_groups=["ebitda_covenants", "term_amortization",
                             "revolver_mechanics", "mfn"],
            inapplicable_groups=["borrowing_base", "recurring_revenue",
                                 "nav_tests", "pik_mechanics"],
            supporting_signals=["consolidated ebitda", "term loan",
                                "leverage ratio", "excess cash flow"],
            residual=True,
            criteria=(
                "a cash flow term loan covenanted on Consolidated EBITDA with "
                "scheduled amortization and a leverage ratio"
            ),
        ),
        ArchetypeProfile(
            archetype="abl_revolver",
            title="asset-based revolver",
            expected_groups=["borrowing_base", "revolver_mechanics"],
            inapplicable_groups=["term_amortization", "recurring_revenue",
                                 "nav_tests", "pik_mechanics"],
            inapplicable_invariants=[
                "amortization_dates_strictly_increasing",
                "amortization_dates_evenly_spaced",
                "amortization_row_count_matches_quarters",
                "amortization_total_consistent",
                "amortization_sums_to_principal",
                "actus_schedule_matches_document",
            ],
            decisive_signals=["borrowing base", "eligible accounts",
                              "eligible inventory", "advance rate"],
            supporting_signals=["availability block", "dominion",
                                "reserves established"],
            criteria=(
                "an asset-based revolving facility sized by a borrowing base "
                "of eligible accounts and inventory with advance rates"
            ),
            rationale=(
                "An ABL has no scheduled amortization, so the amortization "
                "invariants would fire on every correct document."
            ),
        ),
        ArchetypeProfile(
            archetype="second_lien",
            title="second lien term loan",
            expected_groups=["ebitda_covenants", "term_amortization", "mfn"],
            inapplicable_groups=["borrowing_base", "recurring_revenue",
                                 "nav_tests"],
            decisive_signals=["second lien credit agreement", "junior lien",
                              "second priority"],
            supporting_signals=["second lien", "intercreditor agreement",
                                "first lien obligations"],
            criteria=(
                "a second lien facility subordinated by an intercreditor "
                "agreement to first lien obligations"
            ),
        ),
        ArchetypeProfile(
            archetype="recurring_revenue",
            title="recurring-revenue loan",
            expected_groups=["recurring_revenue", "term_amortization"],
            inapplicable_groups=["ebitda_covenants", "ebitda_baskets",
                                 "borrowing_base", "nav_tests"],
            inapplicable_invariants=_EBITDA_INVARIANTS,
            decisive_signals=["annualized recurring revenue",
                              "recurring revenue loan", "arr leverage"],
            supporting_signals=["minimum liquidity"],
            criteria=(
                "a loan sized and covenanted on annualized recurring revenue "
                "rather than EBITDA"
            ),
            rationale=(
                "There is no EBITDA in the document at all, so every "
                "EBITDA-keyed extractor returns null spuriously and every "
                "EBITDA-keyed invariant fires on a correct agreement."
            ),
        ),
        ArchetypeProfile(
            archetype="nav_or_subscription",
            title="NAV or subscription line",
            expected_groups=["nav_tests", "revolver_mechanics"],
            inapplicable_groups=["ebitda_covenants", "ebitda_baskets",
                                 "term_amortization", "borrowing_base",
                                 "recurring_revenue"],
            inapplicable_invariants=_EBITDA_INVARIANTS + [
                "amortization_dates_strictly_increasing",
                "amortization_row_count_matches_quarters",
            ],
            decisive_signals=["net asset value", "uncalled capital",
                              "subscription facility", "portfolio investments"],
            supporting_signals=["capital commitments", "limited partners"],
            criteria=(
                "a fund-level facility secured by uncalled capital commitments "
                "or portfolio net asset value"
            ),
        ),
        ArchetypeProfile(
            archetype="unitranche",
            title="unitranche",
            expected_groups=["ebitda_covenants", "term_amortization"],
            inapplicable_groups=["borrowing_base", "nav_tests",
                                 "recurring_revenue"],
            decisive_signals=["agreement among lenders", "unitranche"],
            supporting_signals=["first out", "last out"],
            criteria=(
                "a unitranche facility whose first-out and last-out economics "
                "are set in an agreement among lenders"
            ),
            rationale=(
                "The AAL is between lenders and is not a borrower exhibit, so "
                "the split economics are not in this document."
            ),
        ),
        ArchetypeProfile(
            archetype="holdco_pik",
            title="holdco PIK",
            expected_groups=["pik_mechanics", "ebitda_covenants"],
            inapplicable_groups=["borrowing_base", "nav_tests",
                                 "recurring_revenue"],
            decisive_signals=["pik toggle", "pik interest", "paid in kind"],
            supporting_signals=["holdco", "structurally subordinated"],
            criteria=(
                "a holding company facility whose interest may be paid in kind "
                "rather than in cash"
            ),
        ),
        ArchetypeProfile(
            archetype="project_finance",
            title="project finance",
            expected_groups=["term_amortization"],
            inapplicable_groups=["ebitda_baskets", "borrowing_base",
                                 "nav_tests", "recurring_revenue"],
            decisive_signals=["offtake agreement", "epc contract",
                              "project company"],
            supporting_signals=["debt service coverage ratio",
                                "completion guarantee"],
            criteria=(
                "a project financing repaid from project cashflows and "
                "covenanted on debt service coverage"
            ),
        ),
        ArchetypeProfile(
            archetype="dip",
            title="debtor-in-possession financing",
            expected_groups=["term_amortization"],
            inapplicable_groups=["nav_tests", "recurring_revenue"],
            # "DIP Financing" appears in 36 of a hundred agreements and only
            # three of them are DIP facilities: the rest carry it in
            # intercreditor boilerplate about what happens if the borrower
            # files. Presence of the phrase decides nothing; being *made under*
            # section 364 does.
            decisive_signals=["debtor-in-possession credit agreement",
                              "section 364", "superpriority claim",
                              "interim order", "final order"],
            supporting_signals=["bankruptcy court", "carve-out",
                                "chapter 11 cases", "petition date",
                                "budget variance"],
            criteria=(
                "a facility extended to a debtor in possession under section "
                "364 of the Bankruptcy Code, approved by an interim or final "
                "order and covenanted on a budget rather than on earnings"
            ),
        ),
        ArchetypeProfile(
            archetype="venture_debt",
            title="venture debt / loan and security agreement",
            expected_groups=["term_amortization"],
            inapplicable_groups=["ebitda_baskets", "nav_tests"],
            decisive_signals=["loan and security agreement", "warrant to purchase",
                              "preferred stock financing"],
            supporting_signals=["material adverse change", "investor abandonment",
                                "minimum cash", "performance milestone"],
            criteria=(
                "a growth-stage facility documented as a loan and security "
                "agreement, secured on all assets and often carrying warrants, "
                "covenanted on cash and milestones rather than on leverage"
            ),
        ),
        ArchetypeProfile(
            archetype="investment_grade",
            title="investment grade revolver",
            expected_groups=["revolver"],
            inapplicable_groups=["borrowing_base", "nav_tests",
                                 "recurring_revenue", "ebitda_baskets"],
            decisive_signals=["ratings-based pricing", "debt rating",
                              "index debt", "s&p and moody"],
            supporting_signals=["facility fee", "no borrowing base",
                                "negative pledge"],
            criteria=(
                "an unsecured revolver priced off the borrower's public debt "
                "ratings rather than off leverage, with a facility fee and a "
                "single financial covenant"
            ),
        ),
        ArchetypeProfile(
            archetype="european_lma",
            title="European LMA facilities agreement",
            expected_groups=["term_amortization", "revolver"],
            inapplicable_groups=["borrowing_base", "nav_tests"],
            decisive_signals=["facilities agreement", "loan market association",
                              "majority lenders", "utilisation request"],
            supporting_signals=["utilisation date", "rollover loan",
                                "agent's spot rate of exchange", "quotation day",
                                "break costs"],
            criteria=(
                "an LMA-style facilities agreement, recognisable from its "
                "British spelling and its own vocabulary -- utilisation, "
                "Majority Lenders, break costs -- rather than from its economics"
            ),
        ),
        ArchetypeProfile(
            archetype="unknown",
            title="unclassified",
            expected_groups=[],
            inapplicable_groups=[],
            criteria="none of the above, or not enough signal to tell",
            rationale=(
                "Nothing is ruled inapplicable when the archetype is unknown: "
                "suppressing a field on a guess loses more than a spurious null."
            ),
        ),
    ]
}


class ArchetypeDetection(BaseModel):
    """What the document was classified as, and how sure that is."""

    archetype: Archetype = "unknown"
    confidence: float = 0.0
    basis: Literal["deterministic", "model", "default"] = "default"
    distribution: dict[str, float] = Field(default_factory=dict)
    signals_found: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ""

    @property
    def confident(self) -> bool:
        return self.archetype != "unknown" and self.confidence >= 0.5

    @property
    def profile(self) -> ArchetypeProfile:
        return PROFILES[self.archetype]


def signal_counts(text: str) -> dict[str, list[str]]:
    """Which archetypes' vocabulary is present, decisive and supporting alike."""
    window = text[:DETECTION_WINDOW].lower()
    found: dict[str, list[str]] = {}
    for archetype, profile in PROFILES.items():
        hits = [
            s for s in (*profile.decisive_signals, *profile.supporting_signals)
            if s in window
        ]
        if hits:
            found[archetype] = hits
    return found


def decisive_counts(text: str) -> dict[str, list[str]]:
    window = text[:DETECTION_WINDOW].lower()
    return {
        archetype: hits
        for archetype, profile in PROFILES.items()
        if (hits := [s for s in profile.decisive_signals if s in window])
    }


def detect_deterministic(text: str) -> ArchetypeDetection:
    """Classify on *distinctive* vocabulary alone, for free.

    Counting shared vocabulary cannot separate these archetypes, because a
    second-lien loan and a unitranche genuinely are cash-flow term loans
    structurally -- they carry the same EBITDA covenants and the same
    amortization. What separates them is a handful of phrases that appear in
    one kind of deal and essentially nowhere else.

    So: a decisive phrase decides. The cash-flow term loan is the residual and
    wins only when nothing decisive fires, which is the right default because
    it is also the most common deal. Two archetypes firing decisively at equal
    strength is a real ambiguity and goes to the model.
    """
    found = signal_counts(text)
    decisive = decisive_counts(text)
    if decisive:
        ranked = sorted(decisive.items(), key=lambda kv: -len(kv[1]))
        best, best_hits = ranked[0]
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if len(best_hits) > runner_up:
            return ArchetypeDetection(
                archetype=best,  # type: ignore[arg-type]
                confidence=min(0.95, 0.55 + 0.15 * len(best_hits)),
                basis="deterministic",
                signals_found=found,
                note=f"decisive vocabulary: {', '.join(best_hits[:4])}",
            )
        return ArchetypeDetection(
            signals_found=found,
            note=(
                f"{ranked[0][0]} and {ranked[1][0]} both fire decisively at "
                f"{len(best_hits)} signals"
            ),
        )

    residual = next(
        (a for a, p in PROFILES.items() if p.residual), "cash_flow_term_loan"
    )
    supporting = found.get(residual, [])
    if len(supporting) >= 3:
        return ArchetypeDetection(
            archetype=residual,  # type: ignore[arg-type]
            confidence=min(0.90, 0.5 + 0.12 * len(supporting)),
            basis="deterministic",
            signals_found=found,
            note=(
                "no decisive vocabulary for any specialised archetype; "
                f"residual classification on {', '.join(supporting[:4])}"
            ),
        )
    return ArchetypeDetection(
        signals_found=found, note="insufficient signal to classify",
    )


def inapplicable_fields(
    profile: ArchetypeProfile, field_names: list[str]
) -> dict[str, str]:
    """Field name -> why this archetype does not have it."""
    out: dict[str, str] = {}
    for name in field_names:
        reason = profile.why_not(name)
        if reason:
            out[name] = reason
    return out
