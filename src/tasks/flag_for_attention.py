from __future__ import annotations
"""Step 5 — flag the referral for human attention. Authority Policy s.2.6.

Section 2.6 permits flagging a referral for human attention, including as urgent. A
flag is addressed to a colleague. It is never addressed to the resident — writing to
a resident is a communication under s.3.5 regardless of how the request is phrased,
which is why the s.2.6 rule in `authority-rules.json` vetoes "contact the resident",
"notify the resident" and "send".

This step declines to act on most referrals, and that is the point. A queue where
everything is flagged urgent is a queue where nothing is. It flags when one of three
things is true:

  * priority came out Immediate or Same day
  * the resident's record could not be retrieved, so the note is thin
  * the referral text tripped the injection screen

Escalation under section 4 is handled separately in step 6. The two are not
alternatives: a referral can be both out of authority and urgent, and a supervisor
should see both facts.
"""

from typing import Union

from src.domain.referral import Referral
from src.tasks.base import (
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)
from src.triage.note import PRIORITY_IMMEDIATE, PRIORITY_SAME_DAY


class FlagForAttentionTask(Task):
    id = "flag_for_attention"
    description = "Flag the referral for human attention, including as urgent (s.2.6)"
    provision = "2.6"
    order = 50

    risk_profile = RiskProfile(
        reversibility=1.0,        # a flag can be cleared
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="flag_for_human_attention",
    )

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        outcome = context.outcome_for(referral.referral_id, "assess_referral")
        assessment = None
        if outcome is not None:
            assessment = outcome.value or outcome.action.payload.get("assessment")

        history = context.history_for(referral.referral_id)
        quarantined = context.is_quarantined(referral.referral_id)

        priority = getattr(assessment, "priority", "")
        routing = getattr(assessment, "routing", "Caseworker review")

        reasons: list[str] = []
        urgent = False

        if priority in (PRIORITY_IMMEDIATE, PRIORITY_SAME_DAY):
            urgent = priority == PRIORITY_IMMEDIATE
            rationale = getattr(assessment, "priority_rationale", "")
            reasons.append(f"Priority {priority}. {rationale}".strip())

        if history is not None and not history.available:
            reasons.append(
                f"The resident's record could not be retrieved ({history.error}), so "
                f"the triage note is working from the referral text alone. Someone "
                f"should confirm the record exists."
            )
        elif history is None:
            reasons.append(
                "No resident record was retrieved for this referral, so the triage "
                "note is working from the referral text alone."
            )

        if quarantined:
            patterns = context.quarantined_referrals.get(referral.referral_id, [])
            reasons.append(
                f"The referral text tripped the injection screen "
                f"({', '.join(patterns)}). It was redacted before reaching the model, "
                f"but a human should read the original."
            )

        if not reasons:
            return Skip(
                f"Priority is {priority or 'routine'}, the resident's record was "
                f"retrieved, and the referral text raised no concerns. Nothing here "
                f"needs a colleague's attention ahead of the ordinary queue."
            )

        signals = RiskSignals(
            injection_suspected=quarantined,
            data_incomplete=history is None or not history.available,
        )

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="flag_for_human_attention",
            description=(
                f"Flag {referral.referral_id} for {routing}"
                + (" as URGENT" if urgent else "")
                + "."
            ),
            reasoning=(
                "Section 2.6 permits flagging a referral for human attention, "
                "including as urgent. This flag is internal to the Department; "
                "contacting the resident would be a communication under s.3.5."
            ),
            payload={
                "reason": " ".join(reasons),
                "urgent": urgent,
                "routing": routing,
                "priority": priority,
            },
            confidence=1.0,
            signals=signals,
            authority=(
                context.determinations[referral.referral_id].to_dict()
                if referral.referral_id in context.determinations
                else {}
            ),
        )
