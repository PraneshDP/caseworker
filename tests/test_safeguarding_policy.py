"""Tests for Safeguarding Policy Amendment ACA-2026/2 and Authority Invariants."""

import pytest
from src.policy.authority import AuthorityPolicy, load_policy
from src.effects.registry import EffectRegistry, ActionNotPerformable, EffectRequest
from src.effects.permitted import build_permitted_effects
from src.history.client import ResidentHistoryClient
from src.escalation import EscalationWriter
from src.handoff.packet import CaseworkerHandoffWriter
from src.domain.referral import Referral


@pytest.fixture
def policy():
    return load_policy(
        rules_path="data/policy/authority-rules.json",
        source_path="data/policy/authority-policy.md",
    )


@pytest.fixture
def registry(policy):
    reg = EffectRegistry(policy=policy)
    reg.bind_all(build_permitted_effects(
        history_client=ResidentHistoryClient(),
        escalation_writer=EscalationWriter(),
        handoff_writer=CaseworkerHandoffWriter(),
    ))
    return reg


class TestSafeguardingPolicy:
    """Test policy rules for Policy Amendment ACA-2026/2."""

    def test_policy_loads_and_verifies_quotes(self, policy):
        """All quotes in rules file must match prose policy verbatim."""
        assert policy.policy_ref is not None
        assert any(r.provision == "3.9" for r in policy.restricted)
        assert any(r.provision == "3.2" for r in policy.permitted)

    def test_section_3_9_restriction_exists(self, policy):
        rule_3_9 = next(r for r in policy.restricted if r.provision == "3.9")
        assert rule_3_9.action_kind == "draft_triage_note_child_in_household"
        assert rule_3_9.performable is False
        assert "Drafting a triage note" in rule_3_9.quote

    def test_section_3_2_handoff_permitted(self, policy):
        rule_3_2 = next(r for r in policy.permitted if r.provision == "3.2")
        assert rule_3_2.action_kind == "handoff_to_caseworker"
        assert rule_3_2.performable is True


class TestEffectRegistryGuarantees:
    """Test structural effect registry boundaries."""

    def test_registry_verification_passes(self, registry):
        report = registry.verify()
        assert report.ok is True
        assert len(report.problems) == 0

    def test_cannot_execute_draft_note_for_child(self, registry):
        """Registry must refuse to execute restricted action kinds."""
        ref = Referral(referral_id="RF-TEST", resident_ref="R-TEST", requested_action="Review award")
        req = EffectRequest(
            referral=ref,
            action_kind="draft_triage_note_child_in_household",
            run_id="test",
            actor="test",
        )
        with pytest.raises(ActionNotPerformable) as exc:
            registry.perform(req)
        assert "3.9" in str(exc.value)

    def test_cannot_execute_section_3_terminations(self, registry):
        ref = Referral(referral_id="RF-TEST", resident_ref="R-TEST", requested_action="Terminate award")
        req = EffectRequest(
            referral=ref,
            action_kind="terminate_award",
            run_id="test",
            actor="test",
        )
        with pytest.raises(ActionNotPerformable):
            registry.perform(req)
