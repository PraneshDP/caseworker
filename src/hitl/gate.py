from __future__ import annotations
"""Human-in-the-loop approval gate.

The gate is an interface with three implementations, because the same
orchestrator has to serve a terminal demo, a web console and an automated test
without any of them special-casing the others:

  CLIGate         Blocks on stdin. Used by `caseworker run-morning`.
  QueueGate       Publishes to a thread-safe queue and blocks until a decision
                  arrives from the web console. Used by `caseworker serve`.
  AutoApproveGate Non-interactive. Used by tests and by --auto-approve.

Three properties every implementation must hold:

  1. FAIL CLOSED. If no decision can be obtained — EOF, Ctrl-C, timeout — the
     action is NOT executed. The default on ambiguity is "don't".
  2. IDENTITY. Every decision carries the identity of whoever made it. An audit
     entry saying "approved" with no actor is not an audit entry.
  3. POLICY BLOCKS ARE NOT BYPASSABLE. `AutoApproveGate` refuses to wave through
     an action gated by Authority Policy ACA-2026/1, and records what it refused.
     A test switch that disables the most important guardrail is not a test switch.

A note on what this gate is and is not, because it is easy to overstate it.

    Section 3 of the policy lists eight acts the agent may not perform without a
    recorded supervisor approval. Those acts are not gated here — they are gated
    in `src/effects/registry.py`, by not existing. There is no function to call,
    so there is no decision for this gate to make and no flag that could bypass
    it. See `EffectRegistry.capability_statement()`.

    What reaches this gate is the residue: an action the policy permits in
    principle, which the risk classifier nonetheless wants a human to look at —
    an unrecognised action kind, a suspected injection, a thin record, a score
    over threshold. Real oversight, but oversight of the permitted set.
"""

import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.domain.referral import Referral
from src.observability.logging_setup import get_logger, log_event
from src.tasks.base import (
    ActionStatus,
    GateLayer,
    ProposedAction,
    RiskResult,
)

logger = get_logger(__name__)


@dataclass
class ApprovalDecision:
    """A human's decision about one gated action."""
    status: ActionStatus
    actor: str
    reason: str = ""
    edited_payload: Optional[dict[str, Any]] = None
    decided_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def approved(self) -> bool:
        return self.status in (ActionStatus.APPROVED, ActionStatus.EDITED,
                               ActionStatus.AUTO_APPROVED_BYPASS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "actor": self.actor,
            "reason": self.reason,
            "edited_payload": self.edited_payload,
            "decided_at": self.decided_at,
        }


def describe_gate_layer(layer: str) -> str:
    """Plain-English explanation of why the gate fired. Shown to the human."""
    return {
        GateLayer.AUTHORITY_POLICY.value:
            "Authority Policy ACA-2026/1 reserves this act to a supervisor. It "
            "requires a recorded approval before the action, and no risk score "
            "can clear it.",
        GateLayer.UNKNOWN_ACTION.value:
            "The requested action does not match any provision of the authority "
            "policy. Section 6.1 says an unclear case is treated as restricted, "
            "so it fails closed rather than being guessed at.",
        GateLayer.FORCED_REVIEW.value:
            "A mandatory-review signal fired. Signals like suspected prompt "
            "injection or an unverifiable policy citation require a human "
            "regardless of the computed score.",
        GateLayer.SCORE_THRESHOLD.value:
            "The weighted risk score reached the review threshold.",
    }.get(layer, "This action requires review.")


def action_review_payload(
    action: ProposedAction,
    risk: RiskResult,
    referral: Optional[Referral] = None,
) -> dict[str, Any]:
    """Everything a reviewer needs, as plain data.

    Shared by the CLI renderer and the web console so the two can never drift
    into showing a reviewer different facts about the same decision.
    """
    return {
        "action_id": action.id,
        "referral_id": action.referral_id,
        "task_id": action.task_id,
        "action_kind": action.action_kind,
        "raw_action_kind": action.raw_action_kind,
        "description": action.description,
        "reasoning": action.reasoning,
        "confidence": action.confidence,
        # Payloads carry live objects between steps; the reviewer's copy has to be
        # plain JSON, and `to_dict` is where that projection already lives.
        "payload": action.to_dict()["payload"],
        "authority": action.authority or {},
        "citations": [c.to_dict() for c in action.citations],
        "signals": action.signals.to_dict(),
        "proposed_at": action.timestamp,
        "risk": {
            "score": risk.score,
            "threshold": risk.threshold,
            "hard_blocked": risk.hard_blocked,
            "gate_layer": risk.gate_layer,
            "gate_layer_explanation": describe_gate_layer(risk.gate_layer),
            "reason": risk.reason,
            "components": risk.components,
            "triggered_signals": list(risk.triggered_signals),
        },
        "referral": {
            "id": referral.referral_id,
            "resident_ref": referral.resident_ref,
            "requested_action": referral.requested_action,
            "urgency": referral.urgency,
            "received_at": referral.received_at,
            "source": referral.source,
            "summary": referral.summary,
            "redacted_fields": list(referral.redacted_fields),
        } if referral is not None else {},
    }


class ApprovalGate(ABC):
    """Interface for obtaining a human decision on a gated action."""

    #: Human-readable identity of the decision-maker for this gate.
    actor: str = "unknown"

    @abstractmethod
    def request(
        self,
        action: ProposedAction,
        risk: RiskResult,
        referral: Optional[Referral] = None,
    ) -> ApprovalDecision:
        """Obtain a decision. MUST NOT return an approving status on failure."""

    def close(self) -> None:  # pragma: no cover - optional hook
        return None


# ---------------------------------------------------------------------------
# CLI gate
# ---------------------------------------------------------------------------

_BOX_W = 74


def _rule(char: str = "=") -> str:
    return char * _BOX_W


def _wrap(text: str, indent: str = "    ", width: int = _BOX_W - 6) -> str:
    """Cheap word wrap. No dependency, no surprises with ANSI width."""
    words = str(text).split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(indent + line for line in lines)


def render_approval_request(
    action: ProposedAction,
    risk: RiskResult,
    referral: Optional[Referral] = None,
) -> str:
    """Render the HITL moment for a terminal. This is the demo's centrepiece."""
    out: list[str] = []
    out.append("")
    out.append(_rule("!"))
    out.append("  HUMAN APPROVAL REQUIRED — the agent has stopped")
    out.append(_rule("!"))

    if referral is not None:
        out.append("")
        out.append(f"  REFERRAL  {referral.referral_id} — resident {referral.resident_ref}")
        out.append(f"            urgency: {referral.urgency}"
                   f" | received: {referral.received_at or 'not recorded'}")
        if referral.source:
            out.append(f"            source: {referral.source}")
        out.append(_wrap(f"requested: {referral.requested_action}",
                         indent="            "))
        if referral.redacted_fields:
            out.append(f"            REDACTED: {', '.join(referral.redacted_fields)} "
                       f"(injection screen)")

    out.append("")
    out.append(f"  ACTION    {action.action_kind}   (proposed by task '{action.task_id}')")
    out.append(_wrap(action.description, indent="            "))

    if action.authority:
        provision = action.authority.get("provision", "")
        authority = action.authority.get("authority", "")
        out.append("")
        out.append(f"  AUTHORITY {authority or 'not determined'}"
                   + (f"   (policy s.{provision})" if provision else ""))
        rationale = action.authority.get("rationale", "")
        if rationale:
            out.append(_wrap(rationale, indent="            "))

    out.append("")
    out.append(f"  WHY GATED {risk.gate_layer.replace('_', ' ').upper()}")
    out.append(_wrap(describe_gate_layer(risk.gate_layer), indent="            "))
    out.append("")
    out.append(f"  RISK      {risk.score:.3f}  (threshold {risk.threshold})"
               + ("   [POLICY BLOCK]" if risk.hard_blocked else ""))
    bar_filled = int(round(min(1.0, max(0.0, risk.score)) * 40))
    out.append(f"            [{'#' * bar_filled}{'.' * (40 - bar_filled)}]")

    if risk.components:
        out.append("            contributions:")
        for name, value in sorted(risk.components.items(), key=lambda kv: -kv[1]):
            if value <= 0:
                continue
            out.append(f"              {name:<34} +{value:.3f}")

    if risk.triggered_signals:
        out.append(f"            signals: {', '.join(risk.triggered_signals)}")

    out.append("")
    out.append("  AGENT REASONING")
    out.append(_wrap(action.reasoning or "(none provided)"))
    out.append(f"    confidence: {action.confidence:.2f}")

    if action.citations:
        out.append("")
        out.append("  POLICY CITATIONS")
        for cite in action.citations:
            if cite.similarity_score is None:
                mark, score_txt = "?", "not verified"
            elif cite.verified:
                mark, score_txt = "OK", f"similarity {cite.similarity_score:.2f}"
            else:
                mark, score_txt = "!!", f"similarity {cite.similarity_score:.2f} — UNSUPPORTED"
            out.append(f"    [{mark}] {cite.section_path or cite.clause_id or cite.chunk_id} "
                       f"({score_txt})")
            if not cite.clause_matched and cite.requested_clause:
                out.append(f"         the planner cited {cite.requested_clause!r}, "
                           f"which was not found in the policy manual")
            if cite.claim:
                out.append(_wrap(f"claim: {cite.claim}", indent="         "))
            if cite.content_snippet:
                snippet = cite.content_snippet.strip().replace("\n", " ")
                out.append(_wrap(f"text: {snippet[:240]}", indent="         "))
    else:
        out.append("")
        out.append("  POLICY CITATIONS  (none — the planner cited no policy)")

    if action.payload:
        out.append("")
        out.append("  EXACT EFFECT IF APPROVED")
        for key, value in action.payload.items():
            text = str(value)
            if "\n" in text or len(text) > 60:
                out.append(f"    {key}:")
                for line in text.splitlines() or [""]:
                    out.append(_wrap(line, indent="      ") or "")
            else:
                out.append(f"    {key}: {text}")

    out.append("")
    out.append(_rule("-"))
    return "\n".join(out)


class CLIGate(ApprovalGate):
    """Terminal approval gate. Blocks on stdin until a human decides."""

    def __init__(self, actor: Optional[str] = None, ask_identity: bool = True):
        self.actor = actor or ""
        self._ask_identity = ask_identity and not actor
        self._identity_resolved = bool(actor)

    def _resolve_identity(self) -> str:
        """Capture who is reviewing, once per session."""
        if self._identity_resolved and self.actor:
            return self.actor
        env_actor = os.environ.get("CASEWORKER_ACTOR", "").strip()
        if env_actor:
            self.actor = f"human:{env_actor}"
        elif self._ask_identity:
            try:
                raw = input("  Reviewer name or ID (for the audit trail): ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            self.actor = f"human:{raw}" if raw else "human:unidentified"
        else:
            self.actor = "human:unidentified"
        self._identity_resolved = True
        log_event(logger, "hitl.reviewer_identified", actor=self.actor)
        return self.actor

    def request(
        self,
        action: ProposedAction,
        risk: RiskResult,
        referral: Optional[Referral] = None,
    ) -> ApprovalDecision:
        actor = self._resolve_identity()
        print(render_approval_request(action, risk, referral))
        log_event(logger, "hitl.approval_requested",
                  action_id=action.id, referral_id=action.referral_id,
                  task_id=action.task_id,
                  gate_layer=risk.gate_layer, risk_score=round(risk.score, 4))

        while True:
            print("  [A]pprove   [R]eject   [E]dit   [S]kip for now   [?] full detail")
            try:
                choice = input("  Decision: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Fail closed. No decision means no execution.
                print("\n  No decision received — action DEFERRED, nothing executed.")
                log_event(logger, "hitl.decision_aborted", level=30,
                          action_id=action.id, actor=actor,
                          detail="stdin closed or interrupted; failing closed")
                return ApprovalDecision(
                    status=ActionStatus.SKIPPED, actor=actor,
                    reason="No decision obtained (input closed or interrupted). "
                           "Failed closed — action was not executed.",
                )

            if choice in ("a", "approve", "y", "yes"):
                reason = self._prompt_reason("Approval note (optional): ")
                return self._finish(action, actor, ActionStatus.APPROVED, reason)

            if choice in ("r", "reject", "n", "no"):
                reason = self._prompt_reason("Reason for rejection (required): ", required=True)
                return self._finish(action, actor, ActionStatus.REJECTED, reason)

            if choice in ("e", "edit"):
                edited = self._prompt_edit(action)
                if edited is None:
                    continue
                reason = self._prompt_reason("Note about the edit (optional): ")
                return self._finish(action, actor, ActionStatus.EDITED, reason,
                                    edited_payload=edited)

            if choice in ("s", "skip", "later", "d", "defer"):
                reason = self._prompt_reason("Why defer? (optional): ")
                return self._finish(action, actor, ActionStatus.SKIPPED,
                                    reason or "Deferred by reviewer.")

            if choice in ("?", "detail", "details"):
                self._print_full_detail(action, risk, referral)
                continue

            print("  Unrecognised choice. Enter A, R, E, S or ?.")

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _prompt_reason(prompt: str, required: bool = False) -> str:
        while True:
            try:
                value = input(f"  {prompt}").strip()
            except (EOFError, KeyboardInterrupt):
                return ""
            if value or not required:
                return value
            print("  A reason is required for this decision.")

    @staticmethod
    def _prompt_edit(action: ProposedAction) -> Optional[dict[str, Any]]:
        """Let the reviewer change the effect before approving it.

        Only the payload is editable. The action kind is not: allowing a
        reviewer to retype the action kind would let them relabel a
        supervisor-reserved act as a permitted one and walk it past both the
        classifier and the effect registry.
        """
        payload = dict(action.payload or {})
        if not payload:
            print("  This action has no editable payload.")
            return None

        keys = list(payload.keys())
        print("\n  Editable fields:")
        for idx, key in enumerate(keys, start=1):
            preview = str(payload[key]).replace("\n", " ")[:60]
            print(f"    {idx}. {key}: {preview}")
        try:
            selection = input("  Field number to edit (blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not selection:
            return None
        if not selection.isdigit() or not (1 <= int(selection) <= len(keys)):
            print("  Not a valid field number.")
            return None

        key = keys[int(selection) - 1]
        print(f"  Current value of {key}:")
        print(_wrap(str(payload[key]), indent="      "))
        print("  New value (single line; blank to cancel):")
        try:
            new_value = input("  > ")
        except (EOFError, KeyboardInterrupt):
            return None
        if not new_value.strip():
            return None

        payload[key] = new_value
        payload["_edited_fields"] = sorted(
            set(payload.get("_edited_fields", []) + [key])
        )
        return payload

    @staticmethod
    def _print_full_detail(action, risk, referral) -> None:
        import json as _json
        print("\n  FULL DECISION RECORD")
        print(_json.dumps(action_review_payload(action, risk, referral),
                          indent=2, default=str))
        print("")

    @staticmethod
    def _finish(
        action: ProposedAction,
        actor: str,
        status: ActionStatus,
        reason: str,
        edited_payload: Optional[dict] = None,
    ) -> ApprovalDecision:
        log_event(logger, "hitl.decision",
                  action_id=action.id, referral_id=action.referral_id,
                  task_id=action.task_id,
                  decision=status.value, actor=actor, reason=reason)
        symbol = {
            ActionStatus.APPROVED: "APPROVED",
            ActionStatus.REJECTED: "REJECTED — nothing was executed",
            ActionStatus.EDITED: "APPROVED WITH EDITS",
            ActionStatus.SKIPPED: "DEFERRED — nothing was executed",
        }.get(status, status.value)
        print(f"  -> {symbol}  (recorded as {actor})\n")
        return ApprovalDecision(status=status, actor=actor, reason=reason,
                                edited_payload=edited_payload)


# ---------------------------------------------------------------------------
# Auto-approve gate (tests and --auto-approve)
# ---------------------------------------------------------------------------

class AutoApproveGate(ApprovalGate):
    """Non-interactive gate.

    Records approvals as AUTO_APPROVED_BYPASS, never as APPROVED, so no
    briefing or audit artifact can claim human oversight that did not happen.

    Two layers are never bypassed:

      AUTHORITY_POLICY  the act is reserved to a supervisor by policy s.3
      UNKNOWN_ACTION    the act matches no provision, so s.6.1 treats it as s.3

    Those are the layers that encode the policy rather than a tuned threshold. A
    flag that switched them off would make the guardrail advisory, and the run
    summary would then be describing a different system than the one that ran.
    Note that `--auto-approve` cannot make a s.3 act *happen* either way — there
    is no effect bound to one. The refusal here is the second lock on a door that
    has no handle.
    """

    BYPASSABLE_LAYERS = frozenset({
        GateLayer.SCORE_THRESHOLD.value,
        GateLayer.FORCED_REVIEW.value,
    })

    #: Layers that stay closed no matter what the caller passes.
    POLICY_LAYERS = frozenset({
        GateLayer.AUTHORITY_POLICY.value,
        GateLayer.UNKNOWN_ACTION.value,
    })

    def __init__(self, actor: str = "system:auto-approve",
                 allow_policy_bypass: bool = False):
        self.actor = actor
        self.allow_policy_bypass = allow_policy_bypass

    def request(
        self,
        action: ProposedAction,
        risk: RiskResult,
        referral: Optional[Referral] = None,
    ) -> ApprovalDecision:
        blocked = risk.hard_blocked or risk.gate_layer in self.POLICY_LAYERS
        if blocked and not self.allow_policy_bypass:
            log_event(logger, "hitl.bypass_refused", level=30,
                      action_id=action.id, referral_id=action.referral_id,
                      gate_layer=risk.gate_layer,
                      detail="policy-gated actions are never auto-approved")
            return ApprovalDecision(
                status=ActionStatus.SKIPPED,
                actor=self.actor,
                reason=(
                    f"--auto-approve does not bypass the {risk.gate_layer} layer. "
                    f"That layer comes from Authority Policy ACA-2026/1, not from a "
                    f"threshold. This action still requires a real supervisor "
                    f"decision and was NOT carried out."
                ),
            )

        log_event(logger, "hitl.auto_approved_bypass", level=30,
                  action_id=action.id, referral_id=action.referral_id,
                  gate_layer=risk.gate_layer, risk_score=round(risk.score, 4))
        return ApprovalDecision(
            status=ActionStatus.AUTO_APPROVED_BYPASS,
            actor=self.actor,
            reason=(
                f"Guardrail bypassed by --auto-approve (gate layer "
                f"{risk.gate_layer}, score {risk.score:.3f}). No human reviewed this."
            ),
        )


class RejectAllGate(ApprovalGate):
    """Rejects everything. Used by tests to prove rejection blocks execution."""

    def __init__(self, actor: str = "test:reject-all", reason: str = "Rejected by test gate."):
        self.actor = actor
        self.reason = reason

    def request(self, action, risk, referral=None) -> ApprovalDecision:
        return ApprovalDecision(status=ActionStatus.REJECTED, actor=self.actor,
                                reason=self.reason)


# ---------------------------------------------------------------------------
# Queue gate (web console)
# ---------------------------------------------------------------------------

@dataclass
class PendingApproval:
    """One action parked in the web console's review queue."""
    action_id: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    decision: Optional[ApprovalDecision] = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "created_at": self.created_at,
            "resolved": self.decision is not None,
            "decision": self.decision.to_dict() if self.decision else None,
            **self.payload,
        }


class QueueGate(ApprovalGate):
    """Approval gate backed by a thread-safe queue, for the web console.

    `request()` runs on the orchestrator's worker thread and blocks. The HTTP
    handler calls `resolve()` from the server thread. A timeout expires closed:
    the action is deferred, not executed.
    """

    def __init__(self, timeout_seconds: Optional[float] = None,
                 actor: str = "human:web-console"):
        self.actor = actor
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._history: list[PendingApproval] = []
        self._waiters: list[threading.Event] = []

    # -- orchestrator side --------------------------------------------------

    def request(
        self,
        action: ProposedAction,
        risk: RiskResult,
        referral: Optional[Referral] = None,
    ) -> ApprovalDecision:
        item = PendingApproval(
            action_id=action.id,
            payload=action_review_payload(action, risk, referral),
        )
        with self._lock:
            self._pending[action.id] = item
        self._notify()
        log_event(logger, "hitl.approval_requested",
                  action_id=action.id, referral_id=action.referral_id,
                  task_id=action.task_id,
                  gate_layer=risk.gate_layer, risk_score=round(risk.score, 4),
                  channel="web")

        signalled = item.event.wait(timeout=self.timeout_seconds)
        with self._lock:
            self._pending.pop(action.id, None)
            self._history.append(item)

        if not signalled or item.decision is None:
            log_event(logger, "hitl.decision_timeout", level=30, action_id=action.id)
            decision = ApprovalDecision(
                status=ActionStatus.SKIPPED, actor="system:timeout",
                reason=(f"No decision within {self.timeout_seconds}s. Failed closed — "
                        f"action was not executed."),
            )
            item.decision = decision
            self._notify()
            return decision

        log_event(logger, "hitl.decision",
                  action_id=action.id, decision=item.decision.status.value,
                  actor=item.decision.actor, reason=item.decision.reason, channel="web")
        self._notify()
        return item.decision

    # -- web side -----------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self._pending.values()]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def resolve(
        self,
        action_id: str,
        status: ActionStatus,
        actor: str = "",
        reason: str = "",
        edited_payload: Optional[dict] = None,
    ) -> bool:
        """Record a decision from the web console. False if nothing was waiting."""
        with self._lock:
            item = self._pending.get(action_id)
            if item is None:
                return False
            item.decision = ApprovalDecision(
                status=status,
                actor=actor or self.actor,
                reason=reason,
                edited_payload=edited_payload,
            )
        item.event.set()
        return True

    def cancel_all(self, reason: str = "Run cancelled.") -> int:
        """Release every waiting action as deferred. Used on shutdown."""
        with self._lock:
            items = list(self._pending.values())
            for item in items:
                item.decision = ApprovalDecision(
                    status=ActionStatus.SKIPPED, actor="system:cancelled", reason=reason,
                )
        for item in items:
            item.event.set()
        return len(items)

    # -- change notification for SSE ---------------------------------------

    def subscribe(self) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._waiters.append(event)
        return event

    def unsubscribe(self, event: threading.Event) -> None:
        with self._lock:
            if event in self._waiters:
                self._waiters.remove(event)

    def _notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for event in waiters:
            event.set()
