from __future__ import annotations
"""Caseworker hand-off — Policy Amendment ACA-2026/2 Section 3.2.

Section 3.2 says:
    Where 3.9 applies, the assistant must hand the referral to a caseworker
    together with whatever it has already established, so that the caseworker
    does not have to repeat work the assistant has already done.

Section 3.3 adds:
    A hand-off under 3.2 is not an escalation under section 4 and must be
    distinguishable from one. An escalation says 'the Department must decide
    whether this may happen at all'. A hand-off says 'this is ordinary
    casework that a person must do'.
"""

from src.handoff.packet import (
    CaseworkerHandoffPacket,
    CaseworkerHandoffWriter,
    build_handoff_packet,
)

__all__ = [
    "CaseworkerHandoffPacket",
    "CaseworkerHandoffWriter",
    "build_handoff_packet",
]
