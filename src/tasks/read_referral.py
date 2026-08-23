from __future__ import annotations
"""Step 1 — read the referral from the overnight queue. Authority Policy s.2.1.

The least interesting step and the one that has to be right. It is the first line of
the section 5.1 trace, and it is where the screening result is turned into a risk
signal: if the referral text tripped the injection screen, that travels with every
action taken on this referral for the rest of the run.
"""

from typing import Any, Optional, Union

from src.domain.referral import Referral
from src.tasks.base import (
    ProposedAction,
    RiskProfile,
    RiskSignals,
    RunContext,
    Skip,
    Task,
)


class ReadReferralTask(Task):
    id = "read_referral"
    description = "Read the referral from the overnight queue (s.2.1)"
    provision = "2.1"
    order = 10

    # Reading changes nothing and can be repeated. The deterministic floor is 0,
    # which is correct: this step should never gate on its own account.
    risk_profile = RiskProfile(
        reversibility=1.0,
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="read_referral",
    )

    def __init__(self) -> None:
        self._policy: Any = None

    def configure(self, **deps: Any) -> None:
        self._policy = deps.get("policy")

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        patterns = context.quarantined_referrals.get(referral.referral_id, [])
        signals = RiskSignals(
            injection_suspected=bool(patterns),
            data_incomplete=not referral.summary,
        )
        if patterns:
            signals.notes.append(
                f"Injection screen matched {', '.join(patterns)}; the referral text "
                f"was redacted before any of it reached the model."
            )
        if not referral.summary:
            signals.notes.append("Referral carries no summary text.")

        determination: Optional[Any] = context.determinations.get(referral.referral_id)
        source = referral.source or "source not recorded"

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="read_referral",
            description=(
                f"Read {referral.referral_id} ({source}) for resident "
                f"{referral.resident_ref}."
            ),
            reasoning=(
                "Section 2.1 permits reading a referral from the overnight queue. "
                "Reading is where the morning starts and it changes nothing on the "
                "case."
            ),
            payload={"fields": sorted(referral.to_dict().keys())},
            confidence=1.0,          # deterministic: the record either parsed or it did not
            signals=signals,
            authority=determination.to_dict() if determination else {},
        )
