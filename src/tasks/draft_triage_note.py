from __future__ import annotations
"""Step 4 — draft the triage note. Authority Policy s.2.4.

This is the only step where a language model writes anything, and it writes one
thing: the narrative paragraph a caseworker reads first. Everything load-bearing in
the note — which provision applies, what was declined, what the next step is, the
banner saying the note is a proposal — is assembled by code in
`src/triage/note.py::build_note` from the determination the policy engine already
made.

Section 2.4 is explicit that a drafted note is a proposal and has no effect on the
case until a caseworker adopts it. That is why this step is permitted at all, and
why it is safe for a model to be involved: the worst outcome of a bad paragraph is
a caseworker reading a badly-written paragraph.

The narrative is best-effort. With no API key configured, or if the call fails, or
if the model returns something too short to be a paragraph,
`draft_narrative` falls back to a deterministic summary and records which source was
used. A run never fails because the model was unavailable.
"""

from typing import Any, Union

from src.domain.referral import Referral
from src.tasks.base import (
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)


class DraftTriageNoteTask(Task):
    id = "draft_triage_note"
    description = "Draft a triage note for caseworker review (s.2.4)"
    provision = "2.4"
    order = 40

    risk_profile = RiskProfile(
        # A draft is a proposal. Nothing is committed, so the deterministic floor is
        # zero — as it should be for the act the policy explicitly describes as
        # having no effect until a human adopts it.
        reversibility=1.0,
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="draft_triage_note",
    )

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        determination = context.determinations.get(referral.referral_id)
        if determination is None:
            # Without a determination the note cannot state which provision applies,
            # and a triage note that is vague about authority is worse than none.
            return Skip(
                "No authority determination is available for this referral, so a note "
                "stating which provision applies cannot be drafted."
            )

        history = context.history_for(referral.referral_id)

        # Safeguarding check: Policy Amendment ACA-2026/2 Section 3.9
        # Drafting a triage note in respect of a referral concerning a household that includes
        # a person under 18 is prohibited. An assistant may not produce a draft note at all.
        if history is None:
            return Skip(
                "Resident history was not retrieved. Under ACA-2026/2 s.5.2 and ACA-2026/1 s.6.1, "
                "where household composition cannot be established, section 3.9 applies and "
                "automated drafting of triage notes is prohibited. Handed to caseworker under s.3.2."
            )

        applies_3_9, reason_3_9 = history.applies_section_3_9()
        if applies_3_9:
            return Skip(
                f"Policy Amendment ACA-2026/2 s.3.9 prohibits automated drafting of a triage note: "
                f"{reason_3_9} Handed off to caseworker under s.3.2."
            )

        assessment_outcome = context.outcome_for(referral.referral_id, "assess_referral")
        assessment = None
        if assessment_outcome is not None:
            # Prefer what the effect actually recorded over the proposal's preview.
            # They agree — `categorise` is pure — but the recorded one is the fact.
            assessment = assessment_outcome.value or assessment_outcome.action.payload.get(
                "assessment"
            )

        history = context.history_for(referral.referral_id)
        notes = list(context.data_quality.get(referral.referral_id, []))

        signals = RiskSignals()
        confidence = 1.0

        if history is None or not history.available:
            signals.data_incomplete = True
            reason = history.error if history is not None else "history was not retrieved"
            signals.notes.append(f"Drafting without the resident's record: {reason}")
            # Lower confidence because the inputs are thinner, which raises the score
            # via the uncertainty term. Set by code from an observed fact about the
            # data, not reported by the model about itself.
            confidence = 0.6

        if context.is_quarantined(referral.referral_id):
            signals.injection_suspected = True
            signals.notes.append(
                "Referral text tripped the injection screen. The model receives the "
                "redacted copy inside an untrusted-content boundary; the provision "
                "cited in the note comes from the policy engine reading the original."
            )

        target = (
            "a caseworker" if determination.permitted else "the supervisor who must decide"
        )

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="draft_triage_note",
            description=(
                f"Draft a triage note for {target}, stating that "
                f"{determination.one_line()}."
            ),
            reasoning=(
                "Section 2.4 permits drafting a triage note for caseworker review, and "
                "states that a drafted note has no effect on the case until a "
                "caseworker adopts it. The note names the provision, the declined "
                "action and the next step; the model writes only the narrative."
            ),
            payload={
                "determination": determination,
                "assessment": assessment,
                "data_quality_notes": notes,
            },
            confidence=confidence,
            signals=signals,
            authority=determination.to_dict(),
        )
