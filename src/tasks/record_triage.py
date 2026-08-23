from __future__ import annotations
"""Step 7 — record that the referral was read and triaged. Authority Policy s.2.5.

Note how narrow s.2.5 is. It permits recording *that a referral has been read and
triaged*. It does not permit recording a change of address, an income change, or a
household composition — those write to the record the award is calculated from, which
is s.3.1, and the s.2.5 rule in `authority-rules.json` is written narrowly on purpose
so "Record change of address" does not match it. Three of the twelve referrals in the
supplied queue ask for exactly that, and all three escalate.

Section 5.2 is the reason this step exists at all: a record that exists only in the
run output does not satisfy s.5.1. So the trace is written to
`data/logs/triage-record.jsonl` as well as to the run file and the audit ledger —
three independent places, none of which is the console output that scrolls away.

This step runs last and runs even when earlier steps failed. A referral that fell
over halfway through still needs a record saying so; a gap in the log is
indistinguishable from a referral nobody looked at.
"""

from typing import Union

from src.domain.referral import Referral
from src.tasks.base import (
    EXECUTED_STATUSES,
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)


class RecordTriageTask(Task):
    id = "record_triage"
    description = "Record that the referral was read and triaged (s.2.5)"
    provision = "2.5"
    order = 70

    risk_profile = RiskProfile(
        reversibility=1.0,
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="record_referral_triaged",
    )

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        determination = context.determinations.get(referral.referral_id)
        outcomes = context.outcomes.get(referral.referral_id, {})

        assess = outcomes.get("assess_referral")
        assessment = None
        if assess is not None:
            assessment = assess.value or assess.action.payload.get("assessment")

        draft = outcomes.get("draft_triage_note")
        note_artifact = ""
        if draft is not None and draft.result.artifacts:
            note_artifact = draft.result.artifacts[0]

        escalation = outcomes.get("escalate_action")
        escalated = escalation is not None and escalation.status in EXECUTED_STATUSES

        handoff = outcomes.get("handoff_caseworker")
        handed_off = handoff is not None and handoff.status in EXECUTED_STATUSES
        handoff_artifact = ""
        if handoff is not None and handoff.result.artifacts:
            handoff_artifact = handoff.result.artifacts[0]

        history = context.history_for(referral.referral_id)

        declined: list[str] = []
        if handed_off:
            declined.append("draft_triage_note — s.3.9 (child in household; handed to caseworker under s.3.2)")
        if determination is not None and not determination.permitted:
            declined.append(
                f"{determination.requested_action} — s.{determination.provision}"
            )
        if determination is not None:
            for matter in determination.related_restricted:
                declined.append(f"{matter.matched_phrase} — s.{matter.provision}")

        failed_steps = [
            task_id for task_id, outcome in outcomes.items()
            if outcome.result.status_value == "error"
        ]

        signals = RiskSignals(
            data_incomplete=bool(failed_steps) or (history is not None and not history.available),
        )
        if failed_steps:
            signals.notes.append(
                f"Recording a partial triage: {', '.join(sorted(failed_steps))} failed. "
                f"The record says so rather than implying the morning went cleanly."
            )

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="record_referral_triaged",
            description=(
                f"Record that {referral.referral_id} was read and triaged. No change "
                f"to the resident's case is recorded."
            ),
            reasoning=(
                "Section 2.5 permits recording that a referral has been read and "
                "triaged. Section 5.2 says an output-only record does not satisfy "
                "s.5.1, so this is written to disk, not just printed."
            ),
            payload={
                "category": getattr(assessment, "category", ""),
                "priority": getattr(assessment, "priority", ""),
                "routing": getattr(assessment, "routing", ""),
                "authority": determination.authority.value if determination else "",
                "provision": determination.provision if determination else "",
                "escalated": escalated,
                "declined": declined,
                "history_source": history.source if history is not None else "not_retrieved",
                "note_artifact": note_artifact,
                "failed_steps": sorted(failed_steps),
            },
            confidence=1.0,
            signals=signals,
            authority=determination.to_dict() if determination else {},
        )
