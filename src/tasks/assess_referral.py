from __future__ import annotations
"""Step 3 — categorise and prioritise the referral. Authority Policy s.2.3.

Categorisation is deterministic (`src/triage/note.py::categorise`), for two reasons.

First, it feeds routing, and a routing decision that changes between runs on the
same input is not something a team can operate. Second, priority is raise-only
relative to the referrer's own urgency: a Health Visitor marking something Standard
may be wrong about how urgent it is, but this system is not the thing that should
quietly downgrade a colleague's judgement. It can raise, never lower.

Section 6.2 is the other half of that: prioritisation has no bearing on authority.
Putting a referral at the top of the pile does not make the requested action any
more permitted, and nothing in this step touches the determination.
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
from src.triage.note import PRIORITY_IMMEDIATE, PRIORITY_SAME_DAY, categorise


class AssessReferralTask(Task):
    id = "assess_referral"
    description = "Categorise and prioritise the referral (s.2.3)"
    provision = "2.3"
    order = 30

    risk_profile = RiskProfile(
        reversibility=1.0,        # a category can be changed; nothing is committed
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="categorise_referral",
    )

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        history = context.history_for(referral.referral_id)
        determination = context.determinations.get(referral.referral_id)

        # Computed here as well as in the effect so the proposal can say what it
        # intends and the reviewer sees the category before it is recorded. The
        # function is pure, so the two agree.
        preview = categorise(referral, history)

        signals = RiskSignals()
        if history is not None and not history.available:
            signals.data_incomplete = True
            signals.notes.append(
                f"Categorised without the resident's record: {history.error}"
            )
        if preview.priority in (PRIORITY_IMMEDIATE, PRIORITY_SAME_DAY):
            signals.notes.append(
                f"Priority raised to {preview.priority}: {preview.priority_rationale}"
            )
        if determination is not None and determination.must_escalate:
            # Recorded as a note, not as `authority_restricted`. Categorising a
            # referral about a restricted matter is not performing the restricted
            # action, and treating it as risky would gate the very step that makes
            # the escalation useful.
            signals.notes.append(
                f"Subject matter engages s.{determination.provision}; the category "
                f"describes the referral, it does not authorise the action."
            )

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="categorise_referral",
            description=(
                f"Categorise as {preview.category}, route to {preview.routing}, "
                f"priority {preview.priority}."
            ),
            reasoning=(
                f"Section 2.3 permits categorising or prioritising a referral. "
                f"{preview.priority_rationale} Referrer urgency was "
                f"{preview.referrer_urgency or 'not stated'} and is never lowered by "
                f"this step."
            ),
            payload={"assessment": preview},
            confidence=1.0,
            signals=signals,
            authority=determination.to_dict() if determination else {},
        )
