"""Deal archetypes, detected before extraction and used to route it.

An ABL has a borrowing base where a term loan has a commitment. A
recurring-revenue loan is covenanted on revenue, so every EBITDA-keyed
invariant fires -- on a completely correct document. A NAV line is covenanted
on portfolio value.

What an archetype says is what a deal of this kind *usually* has, and it is
never allowed to be the last word. A profile that ruled a field out is checked
against the document before anything is suppressed, because suppression is the
one path by which a misclassification reaches a reader as a settled answer.

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

#: Compound nouns that name an asset the borrower *holds*, not a facility it
#: owes under. They exist because a fund-level or warehouse facility describes
#: its collateral in the vocabulary of ordinary corporate lending, and that
#: vocabulary then reads as though this deal had those terms. Golub's BDC
#: warehouse is the case: "delayed draw" appears nineteen times and every one
#: of them is inside "Delayed Drawdown Collateral Loan", a category of loan the
#: fund may buy. The facility has no delayed draw of its own.
#:
#: Every entry is a compound, and that is the whole of the safety argument. A
#: bare "obligor" or "loan" would match an operating-company agreement on every
#: page and cancel vetoes that ought to fire; "collateral loan" and "loan
#: asset" are terms of art that essentially only appear where a portfolio is
#: being described.
_COLLATERAL_NOUNS = (
    "collateral loan",
    "collateral obligation",
    "collateral debt",
    "loan asset",
    "portfolio investment",
    "portfolio company",
    "underlying loan",
    "eligible loan",
)

#: How far either side of an occurrence to look for one of those nouns.
#: Roughly a clause. It has to be tight: at 140 characters a genuine
#: obligation one sentence downstream of a collateral description still had a
#: collateral noun in view, so "The Borrower shall repay the Term Loan in
#: full", appearing after a paragraph about the portfolio, read as more
#: portfolio. Adjacency in characters is only a proxy for being part of the
#: same noun phrase, and the proxy holds at clause scale and breaks above it.
_COLLATERAL_WINDOW = 60


def _only_describes_collateral(lowered: str, phrase: str) -> bool:
    """True when every use of ``phrase`` sits in a collateral description.

    ``lowered`` is the document, already lowercased once by the caller --
    these documents run to half a million characters and the phrase list is
    walked per group.
    """
    start = lowered.find(phrase)
    if start < 0:
        return False
    while start >= 0:
        window = lowered[
            max(0, start - _COLLATERAL_WINDOW):
            start + len(phrase) + _COLLATERAL_WINDOW
        ]
        if not any(noun in window for noun in _COLLATERAL_NOUNS):
            return False
        start = lowered.find(phrase, start + len(phrase))
    return True


class FieldGroup(BaseModel):
    """A set of fields that stand or fall together under an archetype."""

    id: str
    patterns: list[str]
    description: str = ""
    #: Phrases whose presence in the document means the deal has this thing,
    #: whatever the archetype concluded. A veto on suppression, not a detector:
    #: it never marks a field applicable that the archetype thought applicable
    #: anyway, and it never extracts a value.
    #:
    #: It exists because two tolerable failures compose into an intolerable
    #: one. Low extraction recall is visible -- the field routes to review.
    #: A wrong archetype is visible -- the register says so. But suppression
    #: only fires on fields the extractor left empty, so a missed field under
    #: a wrong archetype becomes ``not_applicable_to_archetype``, which is a
    #: settled answer, and the two invisible halves make a confident wrong
    #: one. Air T is the case: an ABL borrowing base led the classifier to an
    #: asset-based revolver, and the Consolidated Term Loan maturing 27 August
    #: 2031 -- named nine times in the document -- was reported as a term the
    #: deal kind cannot have.
    evidence: list[str] = Field(default_factory=list)

    def matches(self, field_name: str) -> bool:
        return any(fnmatch.fnmatch(field_name, p) for p in self.patterns)

    def evidenced_in(self, text: str) -> str | None:
        """The first phrase this document uses that contradicts suppression.

        A phrase only counts where it describes *this* facility. Where every
        occurrence of it sits beside one of ``_COLLATERAL_NOUNS``, the document
        is describing what the borrower holds rather than what it owes, and the
        phrase is not evidence that this deal has the thing. One occurrence
        away from that context is enough to veto: the asymmetry is deliberate,
        because a missed veto ends in a confident wrong answer and a spurious
        one only ends in review.
        """
        lowered = text.lower()
        for phrase in self.evidence:
            if phrase in lowered and not _only_describes_collateral(lowered, phrase):
                return phrase
        return None


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
            # Bare "ebitda", not "consolidated ebitda". Health Catalyst is an
            # ARR loan that never once writes "Consolidated EBITDA" and defines
            # "Consolidated Adjusted EBITDA" 53 times, with a 25% Shared Cap
            # over five of its add-back clauses. The narrower phrases missed it
            # completely and only "leverage ratio" happened to catch it, which
            # is a veto firing for the wrong reason on the way to the right
            # answer. A document that says EBITDA at all has EBITDA machinery.
            evidence=["ebitda", "leverage ratio"],
        ),
        FieldGroup(
            id="ebitda_baskets",
            description="baskets denominated as a percentage of EBITDA",
            patterns=["indebtedness.purchase_money_basket_ebitda_pct"],
            evidence=["consolidated ebitda", "combined ebitda"],
        ),
        FieldGroup(
            id="term_amortization",
            description="scheduled principal repayment on a term loan",
            patterns=["amortization.*", "initial_term_loan.*", "delayed_draw.*"],
            evidence=["term loan", "term note", "delayed draw"],
        ),
        FieldGroup(
            id="revolver_mechanics",
            description="revolver commitment, unused fee, LC sublimit",
            patterns=["revolver.*", "commitment_fee_pct", "lc_sublimit",
                      "fronting_fee_pct"],
            evidence=["revolving credit", "revolving loan", "revolver"],
        ),
        FieldGroup(
            id="borrowing_base",
            description="advance rates against eligible collateral",
            patterns=["borrowing_base.*"],
            evidence=["borrowing base", "advance rate", "eligible accounts"],
        ),
        FieldGroup(
            id="recurring_revenue",
            description="ARR-keyed covenants and liquidity tests",
            patterns=["arr.*"],
            evidence=["recurring revenue", "annualized recurring", "arr "],
        ),
        FieldGroup(
            id="nav_tests",
            description="loan-to-value against portfolio net asset value",
            patterns=["nav.*"],
            evidence=["net asset value", "loan-to-value", "loan to value"],
        ),
        FieldGroup(
            id="pik_mechanics",
            description="payment-in-kind toggle and step-up",
            patterns=["pik.*"],
            evidence=["payment-in-kind", "payment in kind", "pik "],
        ),
        FieldGroup(
            id="mfn",
            description="most-favoured-nation pricing protection",
            patterns=["mfn_*"],
            evidence=["most favored nation", "most favoured nation", "mfn"],
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
            # "junior lien" was decisive here and went 0 for 4 on the corpus.
            # It is the wrong direction: the document that needs the phrase is
            # the *senior* one, because a first lien agreement has to describe
            # the junior debt it permits and the intercreditor form it would
            # sign. Accelevation, Latham and Hornbeck all classified second
            # lien on it while saying "second lien" 0, 0 and 45 times -- and
            # Hornbeck's 45 are all references to a separate Second Lien
            # Credit Agreement, in the phrase "this Agreement, the Second Lien
            # Credit Agreement". A second lien facility says so about itself.
            decisive_signals=["second lien credit agreement", "second priority"],
            supporting_signals=["second lien", "junior lien",
                                "intercreditor agreement",
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
                "The covenant is keyed to recurring revenue, so the "
                "EBITDA-keyed invariants fire on a correct agreement. What "
                "this profile must not assume is that EBITDA is *absent*: "
                "Health Catalyst, the corpus's only real ARR agreement, runs "
                "an ARR Leverage Covenant and defines Consolidated Adjusted "
                "EBITDA with a capped add-back ladder. ARR loans commonly "
                "carry both, one for the covenant and one for the baskets, so "
                "suppression here is left to the evidence veto rather than "
                "assumed."
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


#: The one clause every credit agreement has and no financial statement,
#: prospectus, press release or acquisition report does. Measured across the
#: corpus:
#:
#:   Greenfire business acquisition report      0
#:   Evolent press release                      0
#:   Butterfield proxy                          0
#:   McGraw-Hill press release                  0
#:   Sysco press release                        0
#:   FiscalNote equity purchase agreement       1
#:   ---------------------------------------------
#:   Martin Marietta ABL amendment              3   <- lowest real agreement
#:   Air T                                     15
#:   Evernorth PIK note                        19
#:   every other labelled agreement            25+
#:
#: So two separates them, and the margin below the line is one occurrence
#: rather than the comfortable gap the zero-count documents suggest. The
#: equity purchase agreement is the reason: it is an agreement, it has an
#: event-of-default representation about the target's contracts, and it is not
#: a credit agreement. The plural "Events of Default" would be a better marker
#: -- it is a section heading rather than a reference -- and cannot be used,
#: because filings render headings in small caps and the text arrives as
#: "E VENTS OF D EFAULT".
#:
#: The thin margin is tolerable because the two errors are not symmetric.
#: Rejecting a real agreement costs coverage: the archetype goes to unknown,
#: nothing is suppressed, and fields route to review. Accepting a document
#: that is not an agreement costs correctness, because suppression is
#: confident. Erring toward unknown is the direction that cannot produce a
#: silent error.
#:
#: Deliberately a document-kind test and not a quality score. It answers "is
#: this an agreement" and nothing else -- a real agreement that is badly
#: drafted, redacted, or in a dialect nothing here parses still passes it.
_AGREEMENT_MARKER = "event of default"
_AGREEMENT_MARKER_FLOOR = 2


def _reads_like_an_agreement(text: str) -> bool:
    return text.lower().count(_AGREEMENT_MARKER) >= _AGREEMENT_MARKER_FLOOR


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

    Before any of that, the document has to be an agreement. Greenfire
    Resources' Business Acquisition Report is in the harvest, describes a $50
    million revolver in a financial statement footnote, and was classified as
    a second lien term loan at 0.70 -- confidently, on a filing with no
    borrower, no lender and no operative clause. That is a silent error by
    construction: every field the profile then rules inapplicable is settled
    on a document that has no fields at all.
    """
    if not _reads_like_an_agreement(text):
        return ArchetypeDetection(
            note=(
                "no Event of Default anywhere in the document, so this is not "
                "a credit agreement and has no archetype"
            ),
        )
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
    profile: ArchetypeProfile, field_names: list[str], text: str = ""
) -> dict[str, str]:
    """Field name -> why this archetype does not have it.

    ``text`` is the document. Pass it: a field group the document plainly
    discusses is not suppressed, whatever the archetype concluded, because
    the classifier is wrong on about a quarter of the corpus and suppression
    is the one path by which its being wrong reaches a reader as a settled
    answer. Omitting it restores the old behaviour, which is why it defaults
    to empty rather than being required -- a caller that has no text should
    get the archetype's judgement, not a silent veto that never fires.
    """
    out: dict[str, str] = {}
    vetoed: dict[str, str] = {}
    for group_id in profile.inapplicable_groups:
        group = FIELD_GROUPS.get(group_id)
        if group and text:
            phrase = group.evidenced_in(text)
            if phrase:
                vetoed[group_id] = phrase
    for name in field_names:
        reason = profile.why_not(name)
        if not reason:
            continue
        group_id = next(
            (g for g in profile.inapplicable_groups
             if g in FIELD_GROUPS and FIELD_GROUPS[g].matches(name)),
            None,
        )
        if group_id in vetoed:
            continue
        out[name] = reason
    return out


def suppression_vetoes(profile: ArchetypeProfile, text: str) -> dict[str, str]:
    """Group id -> the phrase in this document that kept it applicable.

    Reported rather than inferred. A veto means the classifier and the
    document disagree about what kind of deal this is, which is worth a
    reader's attention in its own right -- it is the visible form of an
    archetype error that would otherwise only show up as a missing field.
    """
    found: dict[str, str] = {}
    for group_id in profile.inapplicable_groups:
        group = FIELD_GROUPS.get(group_id)
        if group:
            phrase = group.evidenced_in(text)
            if phrase:
                found[group_id] = phrase
    return found
