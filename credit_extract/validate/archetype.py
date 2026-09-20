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
"""

from __future__ import annotations

from ..ingest.normalize import NormalizedDocument
from ..models.archetypes import (
    DETECTION_WINDOW, PROFILES, ArchetypeDetection, detect_deterministic,
)
from .jev import ChoiceQ, JevSession

ARCHETYPE_QUESTION = "What kind of credit facility does this document establish?"


def detect_archetype(
    doc: NormalizedDocument,
    session: JevSession | None = None,
    threshold: float = 0.35,
) -> ArchetypeDetection:
    """Classify the deal, spending nothing when the vocabulary is decisive."""
    deterministic = detect_deterministic(doc.text)
    if deterministic.basis == "deterministic":
        return deterministic
    if session is None:
        return deterministic

    state = doc.text[:DETECTION_WINDOW]
    criteria = {
        archetype: profile.criteria
        for archetype, profile in PROFILES.items()
        if archetype != "unknown"
    }
    result = session.ask(
        state,
        [ChoiceQ(name="archetype", question=ARCHETYPE_QUESTION, criteria=criteria)],
        label="archetype_dispatch",
    )
    decision = result.get("archetype")
    if decision is None or decision.choice is None:
        return deterministic

    confidence = decision.confidence
    if confidence < threshold:
        return ArchetypeDetection(
            archetype="unknown",
            confidence=confidence,
            basis="model",
            distribution=decision.distribution,
            signals_found=deterministic.signals_found,
            note=(
                f"model favoured {decision.choice} at {confidence:.2f}, below "
                f"the {threshold:.2f} dispatch threshold; nothing is ruled out"
            ),
        )
    return ArchetypeDetection(
        archetype=decision.choice,  # type: ignore[arg-type]
        confidence=confidence,
        basis="model",
        distribution=decision.distribution,
        signals_found=deterministic.signals_found,
        note=f"vocabulary was split; model chose {decision.choice}",
    )
