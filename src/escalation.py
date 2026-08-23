from __future__ import annotations
"""Escalation packets — section 4 of Authority Policy ACA-2026/1.

Section 4.2 sets three requirements for an escalation, and they are the reason
this is a validated type rather than a log line:

    An escalation must identify the referral, state which provision of section 3
    applies, and carry sufficient context for a supervisor to act without
    re-reading the case from the beginning.

The last clause is the demanding one. "Escalated to supervisor" in a log satisfies
none of it. So `EscalationPacket.validate()` checks every requirement and raises
`IncompleteEscalationError` when one is unmet -- an escalation that would not let
a supervisor act is a bug, and it fails loudly rather than being filed and
forgotten.

Section 5.1 adds that the record must show what was done and what was declined,
so both lists are required fields too. Section 5.2 rules out an output-only
record, which is why `actions_taken` is a sequence of steps with their order and
inputs rather than a copy of the finished note.

The packet is written twice: as JSON for the console and the ledger, and as
Markdown for a human who has twelve of these to read before lunch.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.domain.referral import Referral, ResidentHistory
from src.policy.authority import AuthorityDetermination, AuthorityPolicy


class IncompleteEscalationError(RuntimeError):
    """The packet does not meet section 4.2, so it must not be filed.

    Carries the list of unmet requirements.
    """

    def __init__(self, referral_id: str, missing: list[str]) -> None:
        self.referral_id = referral_id
        self.missing = list(missing)
        detail = "; ".join(missing)
        super().__init__(
            f"escalation for {referral_id or '(no referral id)'} does not satisfy "
            f"Authority Policy s.4.2 and s.5.1 and will not be filed — a supervisor "
            f"could not act on it without re-reading the case. Unmet: {detail}"
        )


@dataclass
class DeclinedAction:
    """Something the assistant did not do, and the provision that forbade it."""

    action: str
    provision: str
    quote: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "provision": self.provision,
            "quote": self.quote,
            "reason": self.reason,
        }

    def one_line(self) -> str:
        return f"{self.action} — declined under s.{self.provision}. {self.reason}".strip()


@dataclass
class TraceStep:
    """One step of what was done, in what order, on what information.

    Section 5.1 asks for exactly these three things, so they are the fields.
    """

    order: int
    action: str
    provision: str = ""
    inputs: str = ""
    result: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "provision": self.provision,
            "inputs": self.inputs,
            "result": self.result,
            "at": self.at,
        }

    def one_line(self) -> str:
        provision = f" (s.{self.provision})" if self.provision else ""
        inputs = f" on {self.inputs}" if self.inputs else ""
        result = f" → {self.result}" if self.result else ""
        return f"{self.order}. {self.action}{provision}{inputs}{result}"


@dataclass
class EscalationPacket:
    """A section 4 escalation, complete enough for a supervisor to act on."""

    # -- 4.2(a) identify the referral
    referral_id: str
    resident_ref: str
    requested_action: str
    received_at: str = ""
    source: str = ""
    urgency: str = ""
    referral_summary: str = ""

    # -- 4.2(b) state which provision of section 3 applies
    provision: str = ""
    provision_label: str = ""
    provision_quote: str = ""
    interpretation_applied: str = ""
    related_matters: list[dict[str, Any]] = field(default_factory=list)

    # -- 4.2(c) sufficient context to act without re-reading the case
    resident_context: str = ""
    resident_snapshot: dict[str, Any] = field(default_factory=dict)
    history_source: str = ""

    # -- 5.1 what was done, in what order, on what information, what was declined
    actions_taken: list[TraceStep] = field(default_factory=list)
    declined: list[DeclinedAction] = field(default_factory=list)

    # -- what the supervisor is being asked for
    recommended_supervisor_action: str = ""
    draft_note: str = ""

    # -- provenance
    policy_ref: str = ""
    rules_version: str = ""
    run_id: str = ""
    actor: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    data_quality_notes: list[str] = field(default_factory=list)

    @property
    def is_escalation(self) -> bool:
        """Explicitly True for supervisor escalations under section 4 (distinguishable from s.3.2 hand-offs)."""
        return True

    # -- completeness --------------------------------------------------------

    def completeness(self) -> list[str]:
        """List the section 4.2 / 5.1 requirements this packet fails."""
        missing: list[str] = []

        if not self.referral_id.strip():
            missing.append("s.4.2 identify the referral: referral_id is empty")
        if not self.resident_ref.strip():
            missing.append("s.4.2 identify the referral: resident_ref is empty")
        if not self.requested_action.strip():
            missing.append(
                "s.4.2 identify the referral: requested_action is empty, so the "
                "supervisor cannot tell what was asked for"
            )

        if not self.provision.strip():
            missing.append("s.4.2 state which provision of section 3 applies: provision is empty")
        if not self.provision_quote.strip():
            missing.append(
                "s.4.2 state which provision applies: the provision is cited but not "
                "quoted, so the supervisor has to look the policy up"
            )

        if not self.resident_context.strip():
            missing.append(
                "s.4.2 sufficient context to act without re-reading the case: "
                "resident_context is empty"
            )

        if not self.actions_taken:
            missing.append(
                "s.5.1 record what was done, in what order: actions_taken is empty"
            )
        if not self.declined:
            missing.append(
                "s.5.1 record what was declined: declined is empty, but an escalation "
                "means at least one action was refused"
            )
        if not self.recommended_supervisor_action.strip():
            missing.append(
                "the packet does not say what the supervisor is being asked to decide"
            )
        return missing

    @property
    def is_complete(self) -> bool:
        return not self.completeness()

    def validate(self) -> "EscalationPacket":
        missing = self.completeness()
        if missing:
            raise IncompleteEscalationError(self.referral_id, missing)
        return self

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "referral": {
                "referral_id": self.referral_id,
                "resident_ref": self.resident_ref,
                "received_at": self.received_at,
                "source": self.source,
                "urgency": self.urgency,
                "requested_action": self.requested_action,
                "summary": self.referral_summary,
            },
            "authority": {
                "provision": self.provision,
                "label": self.provision_label,
                "quote": self.provision_quote,
                "interpretation_applied": self.interpretation_applied,
                "related_matters": list(self.related_matters),
            },
            "context": {
                "resident_context": self.resident_context,
                "resident_snapshot": self.resident_snapshot,
                "history_source": self.history_source,
                "data_quality_notes": list(self.data_quality_notes),
            },
            "traceability": {
                "actions_taken": [s.to_dict() for s in self.actions_taken],
                "declined": [d.to_dict() for d in self.declined],
            },
            "ask": {
                "recommended_supervisor_action": self.recommended_supervisor_action,
                "draft_note": self.draft_note,
            },
            "provenance": {
                "policy_ref": self.policy_ref,
                "rules_version": self.rules_version,
                "run_id": self.run_id,
                "actor": self.actor,
                "created_at": self.created_at,
            },
            "complete": self.is_complete,
        }

    def render_markdown(self) -> str:
        lines: list[str] = [
            f"# Escalation — {self.referral_id}",
            "",
            f"**Requires a decision by a supervisor.** Raised under section 4 of "
            f"Authority Policy {self.policy_ref or 'ACA-2026/1'}.",
            "",
            "## 1. The referral",
            "",
            f"- **Referral:** {self.referral_id}",
            f"- **Resident:** {self.resident_ref}",
            f"- **Received:** {self.received_at or 'not recorded'}",
            f"- **Source:** {self.source or 'not recorded'}",
            f"- **Urgency (as stated by the referrer):** {self.urgency or 'not stated'}",
            f"- **Action requested:** {self.requested_action}",
        ]
        if self.referral_summary:
            lines += ["", f"> {self.referral_summary}"]

        lines += [
            "",
            "## 2. Why this cannot be done without you",
            "",
            f"This engages **section {self.provision}**"
            + (f" — {self.provision_label}" if self.provision_label else "")
            + ".",
            "",
            f"> {self.provision_quote}",
        ]
        if self.interpretation_applied:
            lines += [
                "",
                f"Section {self.interpretation_applied} was applied: where it is "
                f"unclear whether an action falls within section 3, it is treated as "
                f"though it does. The request reads as routine, but carrying it out "
                f"would write to the record the award is calculated from.",
            ]
        if self.related_matters:
            lines += ["", "**Also engaged by this referral:**", ""]
            for matter in self.related_matters:
                provision = matter.get("provision", "?")
                label = matter.get("label", "")
                phrase = matter.get("matched_phrase", "")
                where = matter.get("matched_in", "")
                lines.append(
                    f"- section {provision} — {label} "
                    f"(from \"{phrase}\" in the {where})"
                )

        lines += [
            "",
            "## 3. Context",
            "",
            "```",
            self.resident_context or "(no resident context retrieved)",
            "```",
        ]
        if self.data_quality_notes:
            lines += ["", "**Data quality:**", ""]
            lines += [f"- {note}" for note in self.data_quality_notes]

        lines += ["", "## 4. What I did", ""]
        lines += [f"{step.one_line()}" for step in self.actions_taken]

        lines += ["", "## 5. What I declined to do", ""]
        for declined in self.declined:
            lines.append(f"- **{declined.action}** — declined under section {declined.provision}.")
            if declined.reason:
                lines.append(f"  {declined.reason}")
            if declined.quote:
                lines.append(f"  > {declined.quote}")

        lines += [
            "",
            "## 6. What I am asking you to decide",
            "",
            self.recommended_supervisor_action,
        ]

        if self.draft_note:
            lines += [
                "",
                "## 7. Draft triage note (a proposal — no effect until you adopt it)",
                "",
                "```",
                self.draft_note,
                "```",
            ]

        lines += [
            "",
            "---",
            "",
            f"Raised by `{self.actor or 'unknown actor'}` in run `{self.run_id or 'unknown run'}` "
            f"at {self.created_at}. Authority rules v{self.rules_version or 'unknown'}. "
            f"No action has been taken on this case.",
            "",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Building a packet from a determination
# ---------------------------------------------------------------------------

def build_packet(
    referral: Referral,
    determination: AuthorityDetermination,
    *,
    policy: AuthorityPolicy,
    history: Optional[ResidentHistory] = None,
    trace: Optional[list[TraceStep]] = None,
    draft_note: str = "",
    run_id: str = "",
    actor: str = "",
    data_quality_notes: Optional[list[str]] = None,
) -> EscalationPacket:
    """Assemble a section 4.2 packet. Does not validate -- the writer does that."""
    provision = determination.provision
    quote = determination.quote
    label = determination.label

    declined: list[DeclinedAction] = []
    if not determination.permitted:
        declined.append(DeclinedAction(
            action=referral.requested_action,
            provision=provision,
            quote=quote,
            reason=(
                determination.no_preparatory_version
                or "Section 4.1 forbids performing this action, and equally forbids "
                   "performing a partial or preparatory version of it, so nothing was "
                   "drafted or staged towards it."
            ),
        ))
    for matter in determination.related_restricted:
        declined.append(DeclinedAction(
            action=matter.label or f"action under section {matter.provision}",
            provision=matter.provision,
            quote=matter.quote,
            reason=(
                f"Raised by \"{matter.matched_phrase}\" in the {matter.matched_in}. "
                f"Identified and escalated under section 2.7 rather than acted on."
            ),
        ))

    ask_lines: list[str] = []
    if not determination.permitted:
        ask_lines.append(
            determination.supervisor_action
            or f"Decide whether to authorise \"{referral.requested_action}\" for "
               f"{referral.resident_ref}, and record that approval before it is "
               f"carried out."
        )
    for matter in determination.related_restricted:
        if matter.supervisor_action:
            ask_lines.append(f"Section {matter.provision}: {matter.supervisor_action}")
    ask = "\n\n".join(f"{i}. {line}" for i, line in enumerate(ask_lines, start=1)) \
        if len(ask_lines) > 1 else (ask_lines[0] if ask_lines else "")

    return EscalationPacket(
        referral_id=referral.referral_id,
        resident_ref=referral.resident_ref,
        requested_action=referral.requested_action,
        received_at=referral.received_at,
        source=referral.source,
        urgency=referral.urgency,
        referral_summary=referral.summary,
        provision=provision,
        provision_label=label,
        provision_quote=quote,
        interpretation_applied=determination.interpretation_applied,
        related_matters=[m.to_dict() for m in determination.related_restricted],
        resident_context=(history.digest() if history else ""),
        resident_snapshot=(history.to_dict() if history else {}),
        history_source=(history.source if history else ""),
        actions_taken=list(trace or []),
        declined=declined,
        recommended_supervisor_action=ask,
        draft_note=draft_note,
        policy_ref=policy.policy_ref,
        rules_version=policy.rules_version,
        run_id=run_id,
        actor=actor,
        data_quality_notes=list(data_quality_notes or []),
    )


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------

class EscalationWriter:
    """Files escalation packets to disk, refusing incomplete ones."""

    def __init__(self, directory: str = "data/escalations", *, run_id: str = "") -> None:
        self.directory = directory
        self.run_id = run_id
        self._filed: list[EscalationPacket] = []

    @property
    def run_directory(self) -> str:
        return os.path.join(self.directory, self.run_id) if self.run_id else self.directory

    @property
    def filed(self) -> list[EscalationPacket]:
        return list(self._filed)

    def begin_run(self, run_id: str) -> None:
        """Point the writer at a new run's directory and forget the last one's packets.

        The pipeline is built once and reused across runs by the web console, so
        the writer has to be told which run it is filing for. Without this, the
        second run of the morning writes into the first run's folder and its index
        lists both runs' escalations as if they were one.
        """
        self.run_id = run_id
        self._filed = []

    def write(self, packet: EscalationPacket) -> dict[str, str]:
        """Validate then file. Raises IncompleteEscalationError if inadequate."""
        packet.validate()

        os.makedirs(self.run_directory, exist_ok=True)
        stem = packet.referral_id or f"escalation-{len(self._filed) + 1}"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)

        json_path = os.path.join(self.run_directory, f"{safe}.json")
        md_path = os.path.join(self.run_directory, f"{safe}.md")

        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(packet.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(packet.render_markdown())

        self._filed.append(packet)
        return {"json": json_path, "markdown": md_path}

    def write_index(self, *, run_date: str = "") -> str:
        """One page listing the morning's escalations, newest ask first."""
        os.makedirs(self.run_directory, exist_ok=True)
        path = os.path.join(self.run_directory, "index.md")

        by_provision: dict[str, list[EscalationPacket]] = {}
        for packet in self._filed:
            by_provision.setdefault(packet.provision, []).append(packet)

        lines = [
            "# Escalations requiring a supervisor decision",
            "",
            f"Run `{self.run_id or 'unknown'}`"
            + (f" — {run_date}" if run_date else "")
            + f" — **{len(self._filed)} referral(s)** need a decision before "
              f"anything happens on them.",
            "",
            "No action has been taken on any case below. Each packet states which "
            "provision of section 3 applies, what was done, and what was declined.",
            "",
            "| Referral | Resident | Requested | Provision | Urgency |",
            "| --- | --- | --- | --- | --- |",
        ]
        for packet in sorted(self._filed, key=lambda p: (p.provision, p.referral_id)):
            safe = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in packet.referral_id
            )
            lines.append(
                f"| [{packet.referral_id}]({safe}.md) | {packet.resident_ref} "
                f"| {packet.requested_action} | s.{packet.provision} "
                f"| {packet.urgency} |"
            )

        lines += ["", "## By provision", ""]
        for provision in sorted(by_provision):
            packets = by_provision[provision]
            label = packets[0].provision_label or ""
            lines.append(f"- **section {provision}** {label} — {len(packets)} referral(s): "
                         + ", ".join(p.referral_id for p in packets))

        lines.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path
