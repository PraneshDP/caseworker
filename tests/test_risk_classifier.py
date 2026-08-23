"""Tests for the Risk Classifier — the deterministic gate."""

import pytest
from src.tasks.base import ProposedAction, RiskProfile, RiskSignals
from src.risk.classifier import classify, FORCED_REVIEW_SIGNALS
from src.config import Settings


@pytest.fixture
def settings():
    """Default settings for testing."""
    return Settings(
        risk_threshold=0.4,
        weight_reversibility=0.35,
        weight_scope=0.25,
        weight_financial=0.25,
        weight_confidence=0.15,
    )


def make_action(action_kind: str, confidence: float = 0.8, signals: RiskSignals = None) -> ProposedAction:
    """Helper to create a test action."""
    return ProposedAction(
        task_id="test_task",
        referral_id="RF-TEST-001",
        action_kind=action_kind,
        description="Test action",
        confidence=confidence,
        signals=signals or RiskSignals(),
    )


class TestAuthorityGates:
    """Authority Policy Layer tests — Section 3 restrictions are gated."""

    def test_restricted_suspension_gated(self, settings):
        action = make_action("suspend_award")
        profile = RiskProfile(reversibility=1.0)
        result = classify(action, profile, settings)
        assert result.requires_approval is True
        assert result.score == 1.0

    def test_restricted_payment_details_gated(self, settings):
        action = make_action("update_bank_details")
        profile = RiskProfile()
        result = classify(action, profile, settings)
        assert result.requires_approval is True

    def test_unknown_action_fails_closed(self, settings):
        action = make_action("unrecognised_experimental_action")
        profile = RiskProfile()
        result = classify(action, profile, settings)
        assert result.requires_approval is True
        assert "unknown" in result.gate_layer.lower()


class TestForcedReviewSignals:
    """Forced review signals mandate a human regardless of score."""

    def test_injection_signal_forces_review(self, settings):
        signals = RiskSignals(injection_suspected=True)
        action = make_action("read_referral", confidence=1.0, signals=signals)
        profile = RiskProfile(reversibility=1.0, scope_of_impact=0.0, financial_impact=0.0)
        result = classify(action, profile, settings)
        assert result.requires_approval is True
        assert result.gate_layer == "forced_review"


class TestWeightedRiskScore:
    """Weighted risk score tests."""

    def test_low_risk_auto_executes(self, settings):
        """A permitted, reversible, zero-impact action clears."""
        action = make_action("read_referral", confidence=1.0)
        profile = RiskProfile(reversibility=1.0, scope_of_impact=0.0, financial_impact=0.0)
        result = classify(action, profile, settings)
        assert result.requires_approval is False
        assert result.score < settings.risk_threshold

    def test_low_confidence_increases_risk_score(self, settings):
        profile = RiskProfile(reversibility=0.8, scope_of_impact=0.1, financial_impact=0.1)

        high_conf = classify(make_action("read_referral", confidence=0.9), profile, settings)
        low_conf = classify(make_action("read_referral", confidence=0.2), profile, settings)

        assert low_conf.score > high_conf.score
