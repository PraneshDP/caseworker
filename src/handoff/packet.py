from __future__ import annotations
"""Caseworker Hand-off Packet — Policy Amendment ACA-2026/2 Section 3.2.

This module implements the hand-off package generated when Section 3.9 applies
(households with a person under 18 or where household composition is unestablished).

KEY POLICY REQUIREMENTS (ACA-2026/2):
1. 3.9 Prohibition: Automated drafting of the triage note is prohibited.
2. 3.2 Preservation of Work: All facts established by earlier permitted steps
   (referral read, resident history retrieved, household composition verified,
   triage categorisation/routing/priority assessed) are bundled and handed to
   the caseworker so work is not repeated and not discarded.
3. 3.3 Strict Distinction from Escalations: A hand-off says 'this is ordinary
   casework that a person must do', NOT 'the Department must decide whether
   this may happen at all'. Hand-offs are stored separately from Section 4
   supervisor escalations.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.domain.referral import Referral, ResidentHistory
from src.escalation import TraceStep
from src.triage.note import TriageAssessment

HANDOFF_BANNER = (
    "SAFEGUARDING HAND-OFF (Policy Amendment ACA-2026/2 s.3.9 / s.3.2) — "
    "Ordinary casework that a person must do. No automated draft note has been "
    "produced. Established case facts are preserved below for caseworker review."
)


@dataclass
class CaseworkerHandoffPacket:
    """Package preserving all established work handed off to a caseworker."""

    # -- Referral details
    referral_id: str
    resident_ref: str
    requested_action: str
    received_at: str = ""
    source: str = ""
    urgency: str = ""
    referral_summary: str = ""

    # -- Safeguarding & Authority
    safeguarding_reason: str = ""
    policy_reference: str = "ACA-2026/1 (as amended by ACA-2026/2 s.3.9)"
    is_escalation: bool = False  # Explicitly False per s.3.3

    # -- Preserved established work
    assessment: Optional[dict[str, Any]] = None
    resident_context: str = ""
    resident_snapshot: dict[str, Any] = field(default_factory=dict)
    household_members: list[dict[str, Any]] = field(default_factory=list)
    children_identified: list[dict[str, Any]] = field(default_factory=list)
    actions_taken: list[TraceStep] = field(default_factory=list)
    data_quality_notes: list[str] = field(default_factory=list)

    # -- Action for caseworker
    caseworker_action_required: str = (
        "Caseworker to review referral and resident history, assess safeguarding "
        "implications, and draft the triage note exercising human judgment."
    )

    # -- Provenance
    run_id: str = ""
    actor: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "hand_off_type": "safeguarding_caseworker_handoff",
            "policy_rule": "ACA-2026/2 s.3.9",
            "is_supervisor_escalation": False,
            "referral": {
                "referral_id": self.referral_id,
                "resident_ref": self.resident_ref,
                "received_at": self.received_at,
                "source": self.source,
                "urgency": self.urgency,
                "requested_action": self.requested_action,
                "summary": self.referral_summary,
            },
            "safeguarding": {
                "reason": self.safeguarding_reason,
                "children_identified": self.children_identified,
            },
            "preserved_work": {
                "assessment": self.assessment,
                "resident_context": self.resident_context,
                "household_members": self.household_members,
                "actions_taken": [s.to_dict() for s in self.actions_taken],
                "data_quality_notes": list(self.data_quality_notes),
            },
            "caseworker_action": self.caseworker_action_required,
            "provenance": {
                "run_id": self.run_id,
                "actor": self.actor,
                "created_at": self.created_at,
            },
        }

    def render_markdown(self) -> str:
        lines = [
            f"# Safeguarding Caseworker Hand-off — {self.referral_id}",
            "",
            f"> **{HANDOFF_BANNER}**",
            "",
            "## 1. Referral Overview",
            "",
            f"- **Referral ID:** {self.referral_id}",
            f"- **Resident Reference:** {self.resident_ref}",
            f"- **Source:** {self.source or 'Not recorded'}",
            f"- **Received:** {self.received_at or 'Not recorded'}",
            f"- **Referrer Urgency:** {self.urgency or 'Standard'}",
            f"- **Requested Action:** {self.requested_action}",
        ]
        if self.referral_summary:
            lines += ["", f"> {self.referral_summary}"]

        lines += [
            "",
            "## 2. Reason for Hand-off (Policy Amendment ACA-2026/2)",
            "",
            "Under **Section 3.9** of Authority Policy ACA-2026/1 (as amended by ACA-2026/2):",
            "> *Drafting a triage note in respect of a referral concerning a household that includes a person under the age of 18.*",
            "",
            f"**Determination:** {self.safeguarding_reason}",
            "",
            "An automated assistant is strictly prohibited from drafting a triage note for this case. "
            "Pursuant to section 3.2, all work already established is handed directly to the caseworker.",
        ]

        if self.children_identified:
            lines += ["", "### Minor(s) Recorded in Household:", ""]
            for child in self.children_identified:
                lines.append(
                    f"- **{child.get('name', 'Unnamed')}** — {child.get('relationship', 'Child')} "
                    f"(b. {child.get('date_of_birth', 'unknown')}, age {child.get('age', 'under 18')})"
                )

        if self.assessment:
            lines += [
                "",
                "## 3. Preserved Triage Assessment (s.2.3)",
                "",
                f"- **Category:** {self.assessment.get('category', 'Unclassified')}",
                f"- **Routing:** {self.assessment.get('routing', 'Caseworker review')}",
                f"- **Priority:** {self.assessment.get('priority', 'Routine')} "
                f"({self.assessment.get('priority_rationale', '')})",
            ]

        lines += [
            "",
            "## 4. Preserved Resident Context (s.2.2)",
            "",
            "```",
            self.resident_context or "(No resident record context)",
            "```",
        ]

        if self.actions_taken:
            lines += [
                "",
                "## 5. Work Already Completed",
                "",
            ]
            lines += [f"{step.one_line()}" for step in self.actions_taken]

        if self.data_quality_notes:
            lines += ["", "## 6. Data Quality Notes", ""]
            lines += [f"- {note}" for note in self.data_quality_notes]

        lines += [
            "",
            "## 7. Next Step for Caseworker",
            "",
            self.caseworker_action_required,
            "",
            "---",
            f"Handed off by `{self.actor or 'system:orchestrator'}` in run `{self.run_id or 'adhoc'}` "
            f"at {self.created_at}. This is ordinary casework, not a supervisor escalation.",
            "",
        ]
        return "\n".join(lines)


def build_handoff_packet(
    referral: Referral,
    history: Optional[ResidentHistory],
    assessment: Optional[TriageAssessment],
    safeguarding_reason: str,
    *,
    trace: Optional[list[TraceStep]] = None,
    run_id: str = "",
    actor: str = "",
    data_quality_notes: Optional[list[str]] = None,
) -> CaseworkerHandoffPacket:
    """Build a complete caseworker hand-off packet preserving all work."""
    children = []
    members = []
    if history is not None:
        for m in history.household:
            members.append(m.to_dict())
            if m.is_under_18() is True:
                children.append({
                    **m.to_dict(),
                    "age": m.age_as_of(),
                })

    return CaseworkerHandoffPacket(
        referral_id=referral.referral_id,
        resident_ref=referral.resident_ref,
        requested_action=referral.requested_action,
        received_at=referral.received_at,
        source=referral.source,
        urgency=referral.urgency,
        referral_summary=referral.summary,
        safeguarding_reason=safeguarding_reason,
        assessment=assessment.to_dict() if assessment else None,
        resident_context=history.digest() if history else "",
        resident_snapshot=history.to_dict() if history else {},
        household_members=members,
        children_identified=children,
        actions_taken=list(trace or []),
        data_quality_notes=list(data_quality_notes or []),
        run_id=run_id,
        actor=actor,
    )


class CaseworkerHandoffWriter:
    """Files safeguarding caseworker hand-off packets to disk."""

    def __init__(self, directory: str = "data/handoffs", *, run_id: str = "") -> None:
        self.directory = directory
        self.run_id = run_id
        self._filed: list[CaseworkerHandoffPacket] = []

    @property
    def run_directory(self) -> str:
        return os.path.join(self.directory, self.run_id) if self.run_id else self.directory

    @property
    def filed(self) -> list[CaseworkerHandoffPacket]:
        return list(self._filed)

    def begin_run(self, run_id: str) -> None:
        self.run_id = run_id
        self._filed = []

    def write(self, packet: CaseworkerHandoffPacket) -> dict[str, str]:
        os.makedirs(self.run_directory, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in packet.referral_id)

        json_path = os.path.join(self.run_directory, f"{safe}-handoff.json")
        md_path = os.path.join(self.run_directory, f"{safe}-handoff.md")

        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(packet.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(packet.render_markdown())

        self._filed.append(packet)
        return {"json": json_path, "markdown": md_path}

    def write_index(self, *, run_date: str = "") -> str:
        os.makedirs(self.run_directory, exist_ok=True)
        path = os.path.join(self.run_directory, "index.md")

        lines = [
            "# Safeguarding Caseworker Hand-offs (Policy Amendment ACA-2026/2)",
            "",
            f"Run `{self.run_id or 'unknown'}`"
            + (f" — {run_date}" if run_date else "")
            + f" — **{len(self._filed)} referral(s)** handed off for human note drafting.",
            "",
            "Under Policy Amendment ACA-2026/2 s.3.9, automated triage notes are prohibited "
            "for households containing a person under 18. Work already completed is preserved below.",
            "",
            "> **Note:** These are ordinary casework hand-offs, NOT supervisor escalations under section 4.",
            "",
            "| Referral | Resident | Requested | Safeguarding Reason | Priority |",
            "| --- | --- | --- | --- | --- |",
        ]
        for packet in sorted(self._filed, key=lambda p: p.referral_id):
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in packet.referral_id)
            priority = packet.assessment.get("priority", "Standard") if packet.assessment else "Standard"
            lines.append(
                f"| [{packet.referral_id}]({safe}-handoff.md) | {packet.resident_ref} "
                f"| {packet.requested_action} | {packet.safeguarding_reason[:60]}... "
                f"| {priority} |"
            )

        lines.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path
