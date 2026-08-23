"""Tests verifying strict distinction between Section 3.2 Caseworker Hand-offs and Section 4 Supervisor Escalations."""

import pytest
from src.domain.referral import Referral, ResidentHistory, HouseholdMember
from src.tasks.base import RunContext, Skip, ProposedAction
from src.tasks.draft_triage_note import DraftTriageNoteTask
from src.tasks.handoff_caseworker import HandoffCaseworkerTask
from src.tasks.escalate_action import EscalateActionTask
from src.policy.authority import load_policy
from src.handoff.packet import CaseworkerHandoffWriter
from src.escalation import EscalationWriter


@pytest.fixture
def context_with_child():
    policy = load_policy("data/policy/authority-rules.json", "data/policy/authority-policy.md")
    ref = Referral(
        referral_id="RF-CHILD-01",
        resident_ref="R-CHILD",
        requested_action="Review award",
        summary="Resident requests review of award.",
    )
    hist = ResidentHistory(
        resident_ref="R-CHILD",
        available=True,
        household=[
            HouseholdMember("Parent", "1985-02-10", "Applicant"),
            HouseholdMember("Child", "2022-04-20", "Son/daughter"),  # 3yo
        ],
    )
    ctx = RunContext(referrals=[ref])
    ctx.determinations[ref.referral_id] = policy.determine(ref.requested_action)
    ctx.histories[ref.referral_id] = hist
    return ref, hist, ctx


@pytest.fixture
def context_restricted_adult():
    policy = load_policy("data/policy/authority-rules.json", "data/policy/authority-policy.md")
    ref = Referral(
        referral_id="RF-ADULT-SUSP",
        resident_ref="R-ADULT",
        requested_action="Suspend award immediately",
        summary="Suspicion of fraud; please suspend award.",
    )
    hist = ResidentHistory(
        resident_ref="R-ADULT",
        available=True,
        household=[HouseholdMember("Adult", "1970-01-01", "Applicant")],
    )
    ctx = RunContext(referrals=[ref])
    ctx.determinations[ref.referral_id] = policy.determine(ref.requested_action)
    ctx.histories[ref.referral_id] = hist
    return ref, hist, ctx


class TestHandoffVsEscalationDistinction:
    """Safeguarding hand-offs (s.3.2) must be distinguishable from supervisor escalations (s.4)."""

    def test_draft_note_prohibited_for_child_case(self, context_with_child):
        ref, hist, ctx = context_with_child
        task = DraftTriageNoteTask()
        plan_result = task.plan(ref, ctx)

        assert isinstance(plan_result, Skip)
        assert "3.9" in plan_result.reason
        assert "prohibits automated drafting" in plan_result.reason.lower()

    def test_handoff_task_produces_ordinary_casework_packet(self, context_with_child):
        ref, hist, ctx = context_with_child
        task = HandoffCaseworkerTask()
        action = task.plan(ref, ctx)

        assert isinstance(action, ProposedAction)
        assert action.action_kind == "handoff_to_caseworker"
        packet = action.payload.get("packet")
        assert packet is not None
        assert packet.is_escalation is False
        assert "SAFEGUARDING HAND-OFF" in packet.render_markdown()
        assert len(packet.children_identified) == 1
        assert packet.children_identified[0]["name"] == "Child"

    def test_section_4_escalates_restricted_action(self, context_restricted_adult):
        policy = load_policy("data/policy/authority-rules.json", "data/policy/authority-policy.md")
        ref, hist, ctx = context_restricted_adult
        task = EscalateActionTask()
        task.configure(policy=policy)
        action = task.plan(ref, ctx)

        assert isinstance(action, ProposedAction)
        assert action.action_kind == "escalate_to_supervisor"
        packet = action.payload.get("packet")
        assert packet is not None
        assert packet.is_escalation is True
        assert packet.provision == "3.2"  # Suspension is 3.2 of ACA-2026/1
        assert "supervisor" in packet.render_markdown().lower()
