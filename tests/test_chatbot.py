from __future__ import annotations

import pytest
from src.chat.assistant import CaseworkerChatbot
from src.config import Settings


@pytest.fixture
def chatbot():
    settings = Settings()
    return CaseworkerChatbot(settings=settings)


def test_chatbot_empty_query(chatbot):
    res = chatbot.answer("")
    assert "Please enter a question" in res.reply
    assert res.mode == "deterministic"


def test_chatbot_william_iverson_query(chatbot):
    res = chatbot.answer("Why was William Iverson's case handed off to a caseworker?")
    assert "RF-2026-0412" in res.reply
    assert "William Iverson" in res.reply
    assert "Safeguarding Trigger" in res.reply or "ACA-2026/2" in res.reply
    assert len(res.sources) > 0


def test_chatbot_safeguarding_query(chatbot):
    res = chatbot.answer("What safeguarding rules apply to households with minor children?")
    assert "ACA-2026/2" in res.reply
    assert "under the age of 18" in res.reply or "minor" in res.reply
    assert "strictly prohibited" in res.reply


def test_chatbot_fraud_escalation_query(chatbot):
    res = chatbot.answer("Why was referral RF-2026-0415 escalated under Section 3.2?")
    assert "RF-2026-0415" in res.reply
    assert "Counter-Fraud" in res.reply or "Fraud" in res.reply
    assert "Section 3" in res.reply


def test_chatbot_audit_chain_query(chatbot):
    res = chatbot.answer("How do we verify the cryptographic audit ledger hash chain?")
    assert "SHA-256" in res.reply
    assert "entry_hash" in res.reply or "Verify chain" in res.reply
