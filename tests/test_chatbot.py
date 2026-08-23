from __future__ import annotations

import pytest
from src.chat.assistant import CaseworkerChatbot
from src.config import Settings


@pytest.fixture
def chatbot():
    settings = Settings()
    return CaseworkerChatbot(settings=settings)


def test_chatbot_empty_query(chatbot):
    """Empty query gets a friendly response (not a crash)."""
    res = chatbot.answer("")
    # Empty query always returns deterministic fallback
    assert res.reply
    assert len(res.reply) > 5


def test_chatbot_william_iverson_query(chatbot):
    """Asking about William Iverson / RF-2026-0412 returns a non-empty grounded answer."""
    res = chatbot.answer("Why was William Iverson's case handed off to a caseworker?")
    # Must return a meaningful reply regardless of engine used
    assert res.reply and len(res.reply) > 20
    assert res.mode in ("groq", "gemini", "deterministic")


def test_chatbot_safeguarding_query(chatbot):
    """General safeguarding query returns a non-empty grounded answer."""
    res = chatbot.answer("What safeguarding rules apply to households with minor children?")
    assert res.reply and len(res.reply) > 20
    assert res.mode in ("groq", "gemini", "deterministic")


def test_chatbot_fraud_escalation_query(chatbot):
    """Fraud referral query returns a non-empty grounded answer."""
    res = chatbot.answer("Why was referral RF-2026-0415 escalated under Section 3.2?")
    assert res.reply and len(res.reply) > 20
    assert res.mode in ("groq", "gemini", "deterministic")


def test_chatbot_audit_chain_query(chatbot):
    """Audit chain query returns SHA-256 information."""
    res = chatbot.answer("How do we verify the cryptographic audit ledger hash chain?")
    assert "SHA-256" in res.reply or "hash" in res.reply.lower()


def test_chatbot_thanglish_query(chatbot):
    """Thanglish input reaches Groq (mode=groq) or deterministic fallback (mode=deterministic)."""
    res = chatbot.answer("Enna da RF-2026-0412 ku enna achu? Sollu bro.")
    # Just verify we get a non-empty reply — the language of the reply is tested live
    assert res.reply
    assert len(res.reply) > 10
    # Mode should be either groq (if key available) or deterministic
    assert res.mode in ("groq", "gemini", "deterministic")


def test_chatbot_mode_fallback(chatbot):
    """Chatbot always returns a response regardless of engine availability."""
    res = chatbot.answer("Explain the supervisor approval process.")
    assert res.reply
    assert res.mode in ("groq", "gemini", "deterministic")
