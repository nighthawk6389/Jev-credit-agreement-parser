"""Archetype dispatch: decide what kind of deal this is, before extracting.

Two tiers, in the order the escalation ladder requires. Distinctive vocabulary
is free and decisive most of the time -- a document containing "borrowing
base", "eligible accounts" and "advance rate" is an ABL and no model is needed
to say so. Jev is asked only when the vocabulary is split, which is a real
ambiguity (a second-lien cash-flow loan hits both vocabularies) rather than a
gap to be broken by a tie-break.

When neither settles it the answer is ``unknown``, and ``unknown`` rules
nothing out. Suppressing a field on a guess about the deal kind loses more than
a spurious null does.

That paragraph was the design, and until the first live pass the code did not
follow it: every ``default`` verdict went to the model, including a document
the deterministic tier had just established is not an agreement at all. The
lexical stand-in never scored high enough for it to matter. A real scorer
asked "what kind of credit facility does this document establish?" of a
warrant answers ``venture_debt`` at 1.00 -- the question presumes a facility,
and every option is one -- and 18 of 18 archetypes the live gate got wrong
came that way.

One more question is asked, of the verdict the vocabulary is surest about. An
``abl_revolver`` verdict rests on "borrowing base", and a fund facility, a
servicing agreement and an investor rights agreement all have one: whose
facility the words describe is the question keyword counting cannot answer.
So the model is asked whether the borrowing base is the borrower's *own*
receivables and inventory, and a verdict it does not support is withdrawn.
"""

from __future__ import annotations

from typing import Any

from ..ingest.normalize import NormalizedDocument
from ..models.archetypes import (
    DETECTION_WINDOW, PROFILES, ArchetypeDetection, detect_deterministic,
)
from .jev import ChoiceQ, JevSession, Noul

ARCHETYPE_QUESTION = "What kind of credit facility does this document establish?"
#: The answer a tie can always be given.
NEITHER = "neither"
NEITHER_CRITERIA = (
    "none of these: the document does not establish a credit facility of any "
    "of these kinds, or does not say which kind"
)
#: Asked of every deterministic ``abl_revolver`` verdict, on the same window
#: plus the document's own Borrowing Base definition. Measured live on the 26
#: documents the vocabulary calls an ABL. The three the labels say are not one
#: score 0.03-0.13. Fifteen more score 0.03-0.32: fund, BDC and specialty
#: finance facilities, a mortgage lender's note, a receivables securitisation,
#: and a term loan whose borrowing base belongs to the borrower's separate ABL.
#: The eight corporate ABLs score 0.65-0.97. One of the fifteen, GBDC's BDC
#: warehouse, is labelled an ABL. Blue Owl's Athena is the same kind of
#: facility and is labelled unknown, because an ABL verdict rules out the
#: coverage tests a warehouse is measured by. The two labels disagree.
OWN_RECEIVABLES_STATEMENT = (
    "This document establishes an asset-based revolving credit facility whose "
    "availability is limited by a borrowing base of the borrower's own "
    "eligible accounts receivable and inventory."
)
#: The document's own Borrowing Base definition rides along with the window,
#: capped at this many characters. The live scorer reads the verdict the same
#: either way; the offline stand-in needs the definition's words, because a
#: corporate ABL's "Eligible Accounts" and "Eligible Inventory" are defined
#: well past the first thirty thousand characters.
DEFINITION_CAP = 6_000
#: Below this the ABL verdict is withdrawn. It sits in the gap the measurement
#: above shows, and withdrawing is safe in the direction that matters:
#: ``unknown`` rules nothing out, so a veto can cost coverage but cannot
#: suppress a field.
ABL_VETO_THRESHOLD = 0.5


def detect_archetype(
    doc: NormalizedDocument,
    session: JevSession | None = None,
    threshold: float = 0.35,
    graph: Any = None,
) -> ArchetypeDetection:
    """Classify the deal, asking the model only what vocabulary cannot settle."""
    deterministic = detect_deterministic(doc.text)
    if session is None:
        return deterministic
    state = doc.text[:DETECTION_WINDOW]

    if deterministic.basis == "deterministic":
        if deterministic.archetype != "abl_revolver":
            return deterministic
        return _confirm_own_receivables(
            deterministic, state + _borrowing_base_definition(graph), session
        )

    if not deterministic.tied:
        # Not an agreement, or too little signal to classify: unknown is the
        # answer, not the start of a question.
        return deterministic

    criteria = {
        archetype: profile.criteria
        for archetype, profile in PROFILES.items()
        if archetype != "unknown"
    }
    criteria[NEITHER] = NEITHER_CRITERIA
    result = session.ask(
        state,
        [ChoiceQ(name="archetype", question=ARCHETYPE_QUESTION, criteria=criteria)],
        label="archetype_dispatch",
    )
    decision = result.get("archetype")
    if decision is None or decision.choice is None:
        return deterministic

    confidence = decision.confidence
    if decision.choice == NEITHER or confidence < threshold:
        return ArchetypeDetection(
            archetype="unknown",
            confidence=confidence if decision.choice == NEITHER else 0.0,
            basis="model",
            distribution=decision.distribution,
            signals_found=deterministic.signals_found,
            tied=deterministic.tied,
            note=(
                f"vocabulary tied between {', '.join(deterministic.tied)}; the "
                + (
                    f"model read it as none of them at {confidence:.2f}"
                    if decision.choice == NEITHER else
                    f"model favoured {decision.choice} at {confidence:.2f}, "
                    f"below the {threshold:.2f} dispatch threshold"
                )
                + "; nothing is ruled out"
            ),
        )
    return ArchetypeDetection(
        archetype=decision.choice,  # type: ignore[arg-type]
        confidence=confidence,
        basis="model",
        distribution=decision.distribution,
        signals_found=deterministic.signals_found,
        tied=deterministic.tied,
        note=(
            f"vocabulary tied between {', '.join(deterministic.tied)}; model "
            f"chose {decision.choice}"
        ),
    )


def _borrowing_base_definition(graph: Any) -> str:
    """The document's own Borrowing Base definition, as a state suffix."""
    if graph is None or not hasattr(graph, "resolve"):
        return ""
    term = graph.resolve("Borrowing Base")
    node = graph.get(term) if term else None
    if node is None:
        return ""
    return f"\n\nDEFINITION OF {node.term}\n{node.body[:DEFINITION_CAP]}"


def _confirm_own_receivables(
    verdict: ArchetypeDetection, state: str, session: JevSession
) -> ArchetypeDetection:
    """Keep an ABL verdict only if the borrowing base is the borrower's own."""
    result = session.ask(
        state,
        [Noul(
            name="own_receivables", statement=OWN_RECEIVABLES_STATEMENT,
            concept="own_receivables_abl",
        )],
        label="archetype_dispatch",
    )
    decision = result.get("own_receivables")
    if decision is None or decision.probability is None:
        return verdict
    if decision.probability >= ABL_VETO_THRESHOLD:
        return verdict.model_copy(update={
            "note": (
                f"{verdict.note}; the borrowing base is the borrower's own "
                f"receivables and inventory at {decision.probability:.2f}"
            ),
        })
    return ArchetypeDetection(
        archetype="unknown",
        basis="model",
        signals_found=verdict.signals_found,
        note=(
            f"{verdict.note}, but the borrowing base is not the borrower's own "
            f"receivables and inventory ({decision.probability:.2f}): the "
            "vocabulary describes somebody else's facility, or a future one, "
            "so nothing is ruled out"
        ),
    )
