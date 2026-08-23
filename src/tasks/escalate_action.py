from __future__ import annotations
"""Step 6 — escalate the action the assistant may not take. Authority Policy s.2.7, s.4.

This is the step that makes refusal useful. Section 4.1 says that where an action
falls within section 3, the assistant must not perform it, must not perform a partial
or preparatory version of it, and must escalate. Naming the provision is not
performing the action, which is exactly what s.2.7 permits.

WHAT THIS STEP DELIBERATELY DOES NOT DO
---------------------------------------
It does not set `escalation_requested` or `authority_restricted` on its own proposal,
even though both look superficially apt. Both are gate-triggering signals, and
gating this action would mean an escalation waits for a human before it reaches the
human — section 4.1 requires the escalation itself to happen, not to queue. The
restricted action is already unperformable by construction; the escalation is the
remedy, and remedies do not need permission.

WHAT MAKES AN ESCALATION ADEQUATE
---------------------------------
Section 4.2 requires an escalation to identify the referral, state which section 3
provision applies, and carry sufficient context for a supervisor to act without
re-reading the case. `src/escalation.py` enforces all three: an incomplete packet
raises `IncompleteEscalationError` and is not filed, so a useless escalation shows up
as a failure in the run rather than as a filed record nobody can act on.

Section 4.3 requires that escalating one referral not prevent the others being
processed. That is the orchestrator's per-referral isolation, not this file's.
"""

from typing import Any, Union

from src.domain.referral import Referral
from src.escalation import TraceStep, build_packet
from src.tasks.base import (
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)


class EscalateActionTask(Task):
    id = "escalate_action"
    description = "Identify an action outside authority and escalate it (s.2.7, s.4)"
    provision = "2.7"
    order = 60

    risk_profile = RiskProfile(
        reversibility=1.0,        # filing an escalation commits nothing on the case
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="escalate_to_supervisor",
    )

    def __init__(self) -> None:
        self._policy: Any = None

    def configure(self, **deps: Any) -> None:
        self._policy = deps.get("policy")

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        determination = context.determinations.get(referral.referral_id)
        if determination is None:
            return Skip(
                "No authority determination is available, so there is no provision to "
                "cite in an escalation."
            )

        if not determination.must_escalate:
            return Skip(
                f"The requested action is permitted under s.{determination.provision} "
                f"and raises no restricted matter, so there is nothing to escalate. "
                f"The triage note stands on its own."
            )

        history = context.history_for(referral.referral_id)
        draft_note = self._draft_note_text(referral, context)
        trace = self._build_trace(referral, context)

        packet = build_packet(
            referral,
            determination,
            policy=self._policy,
            history=history,
            trace=trace,
            draft_note=draft_note,
            run_id=context.run_id,
            actor=context.actor,
            data_quality_notes=context.data_quality.get(referral.referral_id, []),
        )

        # Report inadequacy at planning time rather than letting the writer reject it
        # silently. The effect still validates — this is the earlier of two checks,
        # not a replacement for it.
        missing = packet.completeness()
        signals = RiskSignals(
            data_incomplete=bool(missing) or (history is not None and not history.available),
        )
        if missing:
            signals.notes.append(
                "This escalation does not yet satisfy s.4.2 and will be refused by "
                "the writer: " + "; ".join(missing)
            )
        if history is not None and not history.available:
            signals.notes.append(
                f"Escalating without the resident's record ({history.error}). The "
                f"packet says so, so the supervisor knows what is missing rather than "
                f"assuming the context is complete."
            )

        provisions = ", ".join(f"s.{p}" for p in determination.escalated_provisions)

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="escalate_to_supervisor",
            description=(
                f"Escalate {referral.referral_id} to a supervisor: {provisions} "
                f"applies. The requested action was not taken, and no preparatory "
                f"version of it was taken either."
            ),
            reasoning=(
                f"{determination.rationale} Section 4.1 requires that the action not "
                f"be performed, that no partial or preparatory version be performed, "
                f"and that it be escalated. Section 2.7 permits identifying it and "
                f"escalating under section 4."
            ),
            payload={"packet": packet},
            confidence=1.0,      # the provision came from the rules engine, not a model
            signals=signals,
            authority=determination.to_dict(),
        )

    # -- helpers ------------------------------------------------------------

    def _draft_note_text(self, referral: Referral, context: RunContext) -> str:
        """The note drafted in step 4, so the supervisor sees it without a second file."""
        outcome = context.outcome_for(referral.referral_id, "draft_triage_note")
        if outcome is None or not outcome.executed:
            return ""
        note = outcome.value
        if note is not None and hasattr(note, "render_text"):
            return note.render_text()
        return ""

    def _build_trace(self, referral: Referral, context: RunContext) -> list[TraceStep]:
        """Turn this referral's completed steps into the s.5.1 record.

        Section 5.1 asks for what was done, in what order, and on what information.
        The order comes from the task's `order`, so the trace reflects the pipeline
        rather than dictionary insertion.
        """
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
        """Say what information the step worked from — the 'on what information' half."""
        history = context.history_for(referral.referral_id)
        if task_id == "read_referral":
            return f"The overnight queue entry for {referral.referral_id}."
        if task_id == "retrieve_history":
            if history is None:
                return f"A lookup of resident {referral.resident_ref}."
            return (
                f"Resident {referral.resident_ref} from {history.source}"
                + ("" if history.available else f" — unavailable: {history.error}")
            )
        if history is not None and history.available:
            return (
                f"The referral text and the resident's record from {history.source}."
            )
        return "The referral text only; the resident's record was not available."
