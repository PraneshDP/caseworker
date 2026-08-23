from __future__ import annotations
"""Step 2 — retrieve the resident's history. Authority Policy s.2.2.

The only step that leaves the process. `services/history_service.py` is a real HTTP
service with real latency, and it may simply not be running.

A failed lookup is NOT a failed step. Section 4.3 says escalating one referral must
not prevent the others being processed, and the same principle applies to data: a
resident whose record cannot be fetched should still get a triage note, one that
says plainly which context is missing. So this step always proposes, and the
history's own `available` flag carries the bad news forward into the note and into
the escalation packet.

What it does raise is `data_incomplete`, which feeds the risk score and appears in
the section 5.1 trace as "planned on incomplete information".
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


class RetrieveHistoryTask(Task):
    id = "retrieve_history"
    description = "Retrieve resident history, household and case events (s.2.2)"
    provision = "2.2"
    order = 20

    risk_profile = RiskProfile(
        reversibility=1.0,        # a read changes nothing
        scope_of_impact=0.0,
        financial_impact=0.0,
        default_action_kind="retrieve_resident_history",
    )

    def __init__(self) -> None:
        self._client: Any = None

    def configure(self, **deps: Any) -> None:
        self._client = deps.get("history_client")

    def plan(
        self, referral: Referral, context: RunContext
    ) -> Union[ProposedAction, Skip, None]:
        if not referral.resident_ref:
            return Skip(
                "Referral carries no resident reference, so there is no record to "
                "retrieve. Triage continues on the referral text alone."
            )

        signals = RiskSignals()
        # The lookup has not happened yet — that is the effect's job. What we can
        # say here is whether the service was already known to be down, so the
        # signal is honest at planning time rather than backfilled.
        if self._client is not None and getattr(self._client, "api_unreachable", False):
            signals.data_incomplete = True
            signals.notes.append(
                "The resident history service was unreachable earlier in this run; "
                "this lookup will fall back to the local snapshot."
            )

        determination = context.determinations.get(referral.referral_id)

        return ProposedAction(
            task_id=self.id,
            referral_id=referral.referral_id,
            action_kind="retrieve_resident_history",
            description=(
                f"Retrieve history, household composition and case events for "
                f"{referral.resident_ref}."
            ),
            reasoning=(
                "Section 2.2 permits retrieving a resident's history, household "
                "composition and case events. Section 4.2(c) then requires any "
                "escalation to carry enough context for a supervisor to act without "
                "re-reading the case, so this read is what makes a useful escalation "
                "possible."
            ),
            payload={"resident_ref": referral.resident_ref},
            confidence=1.0,
            signals=signals,
            authority=determination.to_dict() if determination else {},
        )
