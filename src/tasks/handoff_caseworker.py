from __future__ import annotations
"""Step 4b — hand off to caseworker when s.3.9 applies. Policy Amendment ACA-2026/2 s.3.2.

WHY THIS STEP EXISTS
--------------------
Policy Amendment ACA-2026/2 Section 3.2 states:
    Where 3.9 applies, the assistant must hand the referral to a caseworker
    together with whatever it has already established, so that the caseworker
    does not have to repeat work the assistant has already done.

Section 3.3 states:
    A hand-off under 3.2 is not an escalation under section 4 and must be
    distinguishable from one. An escalation says 'the Department must decide
    whether this may happen at all'. A hand-off says 'this is ordinary casework
    that a person must do'.

Section 4.2 states:
    Where an assistant has already begun work on a referral to which 3.9 applies,
    the work already done must be preserved and handed to the caseworker. It must
    not be discarded and the run must not be restarted from the beginning.

This task executes the hand-off, preserving all established facts from steps 1-3
(referral data, resident history, household composition, triage categorisation and routing)
into a structured hand-off package filed for caseworker action.
"""

from typing import Any, Union

from src.domain.referral import Referral
from src.escalation import TraceStep
from src.handoff.packet import build_handoff_packet
from src.tasks.base import (
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)


class HandoffCaseworkerTask(Task):
    id = "handoff_caseworker"
    description = "Hand referral and established context to caseworker (ACA-2026/2 s.3.2)"
    provision = "3.2"
    order = 45

    risk_profile = RiskProfile(
        reversibility=1.0,        # hand-off preserves work; commits no award change
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="handoff_to_caseworker",
    )

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        history = context.history_for(referral.referral_id)

        if history is None:
            applies_3_9 = True
            reason_3_9 = (
                "Resident history unavailable; household composition cannot be established. "
                "Section 3.9 treated as applying per ACA-2026/2 s.5.2 & ACA-2026/1 s.6.1."
            )
        else:
            applies_3_9, reason_3_9 = history.applies_section_3_9()

        if not applies_3_9:
            return Skip(
                "Policy Amendment ACA-2026/2 s.3.9 does not apply: household composition "
                "is established and contains no persons under the age of 18."
            )

        # Retrieve assessment outcome from step 3
        assessment_outcome = context.outcome_for(referral.referral_id, "assess_referral")
        assessment = None
        if assessment_outcome is not None:
            assessment = assessment_outcome.value or assessment_outcome.action.payload.get("assessment")

        # Build trace of steps already completed to preserve work per s.4.2
        trace = self._build_trace(referral, context)

        packet = build_handoff_packet(
            referral=referral,
            history=history,
            assessment=assessment,
            safeguarding_reason=reason_3_9,
            trace=trace,
            run_id=context.run_id,
            actor=context.actor,
            data_quality_notes=context.data_quality.get(referral.referral_id, []),
        )

        signals = RiskSignals(
            data_incomplete=history is None or not history.available,
            injection_suspected=context.is_quarantined(referral.referral_id),
        )

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="handoff_to_caseworker",
            description=(
                f"Hand off {referral.referral_id} ({referral.resident_ref}) to caseworker "
                f"under ACA-2026/2 s.3.2 with all established facts preserved."
            ),
            reasoning=(
                f"Under Policy Amendment ACA-2026/2 s.3.9, drafting a triage note for a household "
                f"including a child under 18 is prohibited. Pursuant to s.3.2, all work established "
                f"so far is handed to a caseworker so ordinary casework can proceed without repeating work. "
                f"Determination: {reason_3_9}"
            ),
            payload={"packet": packet},
            confidence=1.0,
            signals=signals,
            authority={
                "provision": "3.2",
                "label": "Safeguarding Caseworker Hand-off (ACA-2026/2 s.3.2)",
                "quote": "Where 3.9 applies, the assistant must hand the referral to a caseworker together with whatever it has already established under section 3.2 of Amendment ACA-2026/2.",
                "is_escalation": False,
            },
        )

    def _build_trace(self, referral: Referral, context: RunContext) -> list[TraceStep]:
        outcomes = context.outcomes.get(referral.referral_id, {})
        steps: list[TraceStep] = []
        for index, (task_id, outcome) in enumerate(
            sorted(outcomes.items(), key=lambda kv: kv[1].action.timestamp), start=1
        ):
            steps.append(
                TraceStep(
                    order=index,
                    action=outcome.action.description or task_id,
                    provision=outcome.action.authority.get("provision", ""),
                    inputs=self._describe_inputs(task_id, referral, context),
                    result=outcome.result.execution_detail or outcome.result.detail,
                    at=outcome.result.timestamp,
                )
            )
        return steps

    def _describe_inputs(
        self, task_id: str, referral: Referral, context: RunContext
    ) -> str:
        history = context.history_for(referral.referral_id)
        if task_id == "read_referral":
            return f"The overnight queue entry for {referral.referral_id}."
        if task_id == "retrieve_history":
            if history is None:
                return f"A lookup of resident {referral.resident_ref}."
            return f"Resident {referral.resident_ref} from {history.source}" + ("" if history.available else f" — unavailable: {history.error}")
        if history is not None and history.available:
            return f"The referral text and the resident's record from {history.source}."
        return "The referral text only; the resident's record was not available."
