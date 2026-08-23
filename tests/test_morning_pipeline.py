"""End-to-end tests for the complete Morning Run pipeline and Safeguarding rules."""

import os
import pytest
from src.config import Settings
from src.orchestrator import run_morning
from src.audit.log import verify_chain


@pytest.fixture
def settings(tmp_path):
    runs_dir = str(tmp_path / "runs")
    logs_dir = str(tmp_path / "logs")
    artifacts_dir = str(tmp_path / "artifacts")
    escalations_dir = str(tmp_path / "escalations")
    handoffs_dir = str(tmp_path / "handoffs")

    return Settings(
        referral_queue_path="data/referrals/referral-queue.json",
        policy_rules_path="data/policy/authority-rules.json",
        policy_document_path="data/policy/authority-policy.md",
        history_snapshot_path="services/_history_data.json",
        runs_dir=runs_dir,
        log_dir=logs_dir,
        artifacts_dir=artifacts_dir,
        escalations_dir=escalations_dir,
        triage_record_path=os.path.join(logs_dir, "triage-record.jsonl"),
        flag_path=os.path.join(logs_dir, "flags.jsonl"),
        risk_threshold=0.4,
    )


class TestMorningPipelineEndToEnd:
    """Run full morning routine and verify all invariants."""

    def test_full_morning_run_and_safeguarding_handoffs(self, settings):
        result = run_morning(settings, auto_approve=True, echo=False)

        assert result.stats.total_referrals == 12
        assert result.stats.errors == 0

        # Verify tamper-evident hash chain
        chain_verification = verify_chain(result.ledger_path)
        assert chain_verification["valid"] is True
        assert chain_verification["records"] > 50

        # Check safeguarding handoffs: 3 referrals have children under 18
        # RF-2026-0412 (R-20500), RF-2026-0416 (R-20528), RF-2026-0418 (R-20542)
        child_referral_ids = {"RF-2026-0412", "RF-2026-0416", "RF-2026-0418"}

        for ref_id in child_referral_ids:
            # Must have handoff_caseworker outcome
            handoff_outcome = result.context.outcome_for(ref_id, "handoff_caseworker")
            assert handoff_outcome is not None
            assert handoff_outcome.executed is True

            # Must NOT have drafted a triage note
            draft_outcome = result.context.outcome_for(ref_id, "draft_triage_note")
            assert draft_outcome is None

        # Check an adult-only permitted case received a draft note
        # RF-2026-0421 (R-20563) -> routine renewal, adult only
        adult_ref_id = "RF-2026-0421"
        draft_outcome = result.context.outcome_for(adult_ref_id, "draft_triage_note")
        assert draft_outcome is not None
        assert draft_outcome.executed is True
