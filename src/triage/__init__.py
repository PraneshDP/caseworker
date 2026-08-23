"""Triage — categorisation (s.2.3), note drafting (s.2.4) and flagging (s.2.6)."""

from src.triage.note import (
    PRIORITY_IMMEDIATE,
    PRIORITY_ROUTINE,
    PRIORITY_SAME_DAY,
    PROPOSAL_BANNER,
    TriageAssessment,
    TriageNote,
    build_note,
    categorise,
    deterministic_narrative,
    draft_narrative,
)

__all__ = [
    "PRIORITY_IMMEDIATE",
    "PRIORITY_ROUTINE",
    "PRIORITY_SAME_DAY",
    "PROPOSAL_BANNER",
    "TriageAssessment",
    "TriageNote",
    "build_note",
    "categorise",
    "deterministic_narrative",
    "draft_narrative",
]
