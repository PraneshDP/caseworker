from __future__ import annotations
"""Audit logging — a genuinely append-only, hash-chained ledger.

Two artifacts per run, on purpose:

  run_<date>_<id>.jsonl   The LEDGER. One JSON object per line, opened in
                          append mode and flushed after every write. If the
                          process is killed mid-run — including while a human
                          is staring at an approval prompt — everything
                          decided up to that instant is already on disk.

  run_<date>_<id>.json    The SUMMARY. Written at the end for the web console
                          and for judges: stats, quarantine reports, and the
                          full entry list in one readable object.

The previous implementation accumulated entries in a list and wrote once at the
end, which meant Ctrl-C during an approval destroyed the entire trail. That is
the worst possible moment to lose an audit record, so the ledger now leads and
the summary is derived.

TAMPER EVIDENCE
---------------
Each entry carries `prev_hash` and `entry_hash`, where

    entry_hash = sha256(prev_hash + canonical_json(entry_without_hashes))

Editing, reordering or deleting any line breaks the chain from that point on,
and `verify_chain()` reports the exact index where it broke. This is not a
cryptographic signature — anyone who can write the file can recompute the whole
chain — but it makes silent post-hoc edits detectable, which is the property
that matters for this system's threat model.

EVERY ENTRY ANSWERS
-------------------
  WHO      actor (human identity for decisions, system:orchestrator otherwise)
  WHAT     action_kind + description + payload (the actual note body, the
           actual routing, the actual flag list)
  WHEN     proposed_at, decided_at, logged_at, duration_ms
  WHICH    run_id, referral_id, task_id, action_id
  WHY      reasoning, provision, risk_reason, risk_components, triggered_signals
  GATED?   requires_approval + gate_layer
  RESULT   resolution, executed, execution_detail
  BEFORE-> previous_state / new_state
  AFTER

Section 5.1 of Authority Policy ACA-2026/1 asks for a record of what was done,
in what order, on what information, and what was declined. The last of those is
why `log_step_declined` exists: a step that considered a referral and chose not
to act writes a line saying so. Otherwise a declined step and a step that never
ran are the same absence of evidence.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional

from src.observability.logging_setup import get_logger, log_event
from src.tasks.base import (
    ActionResult,
    ActionStatus,
    EXECUTED_STATUSES,
    GateLayer,
    ProposedAction,
    RiskResult,
    SYSTEM_ACTOR,
)

logger = get_logger(__name__)

GENESIS_HASH = "0" * 64

#: Named in the briefing so a reader knows which policy the numbers were judged
#: against. Kept as a literal rather than read from the loaded policy: the ledger
#: must be writable even when the policy failed to load.
POLICY_REF = "ACA-2026/1"


def _canonical(obj: Any) -> str:
    """Deterministic JSON for hashing. Sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_entry_hash(prev_hash: str, entry: dict) -> str:
    """Hash an entry into the chain, excluding the hash fields themselves."""
    body = {k: v for k, v in entry.items() if k not in ("prev_hash", "entry_hash")}
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()


@dataclass
class BriefingStats:
    """Aggregated stats for the end-of-run briefing.

    `human_approved` counts ONLY decisions a person actually made.
    `auto_approved_bypass` counts --auto-approve guardrail bypasses. Merging
    those two, as the previous version did, means the briefing reports human
    oversight that never happened.

    `authority_gated` and `refused` are the policy's own numbers: how many acts
    the agent declined because section 3 reserves them, and how many escalations
    it filed as a result. They are the numbers a supervisor cares about.
    """
    total_referrals: int = 0
    referrals_skipped_invalid: int = 0
    total_actions: int = 0
    steps_declined: int = 0
    auto_executed: int = 0
    human_approved: int = 0
    human_rejected: int = 0
    human_edited: int = 0
    skipped: int = 0
    auto_approved_bypass: int = 0
    gated_total: int = 0
    hard_blocked: int = 0
    authority_gated: int = 0
    unknown_action_gated: int = 0
    forced_review: int = 0
    risk_gated: int = 0
    escalations_filed: int = 0
    refused: int = 0
    injection_flagged: int = 0
    injection_fields_redacted: int = 0
    errors: int = 0
    total_duration_ms: float = 0.0

    @property
    def human_decisions(self) -> int:
        return self.human_approved + self.human_rejected + self.human_edited + self.skipped


@dataclass
class AuditEntry:
    """A single entry in the audit ledger."""
    # --- WHICH ---
    run_id: str
    referral_id: str
    task_id: str
    action_id: str
    sequence: int
    # --- WHAT ---
    action_kind: str
    raw_action_kind: str
    description: str
    payload: dict[str, Any]
    previous_state: dict[str, Any]
    new_state: dict[str, Any]
    # --- WHY ---
    reasoning: str
    #: The policy provision the action was taken under, and the verbatim quote the
    #: rules engine matched. A reader can check the reason against the policy text
    #: without re-running the engine.
    provision: str
    provision_quote: str
    authority: dict[str, Any]
    citations: list[dict]
    confidence: float
    risk_score: float
    risk_threshold: float
    hard_blocked: bool
    requires_approval: bool
    gate_layer: str
    risk_reason: str
    risk_components: dict[str, float]
    triggered_signals: list[str]
    signals: dict[str, Any]
    # --- WHO / RESULT ---
    resolution: str
    resolution_detail: str
    execution_detail: str
    executed: bool
    actor: str
    human_reason: str
    artifacts: list[str] = field(default_factory=list)
    # --- SECURITY ---
    injection_flagged: bool = False
    injection_patterns: list[str] = field(default_factory=list)
    injection_fields: list[str] = field(default_factory=list)
    # --- WHEN ---
    proposed_at: str = ""
    decided_at: str = ""
    duration_ms: float = 0.0
    logged_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # --- CHAIN ---
    prev_hash: str = ""
    entry_hash: str = ""


class AuditLog:
    """Append-only, hash-chained audit ledger for a single morning run."""

    def __init__(
        self,
        runs_dir: str,
        run_id: str,
        actor: str = SYSTEM_ACTOR,
        hash_chain: bool = True,
        run_date: Optional[str] = None,
    ):
        self.runs_dir = runs_dir
        self.run_id = run_id
        self.run_date = run_date or datetime.now().strftime("%Y-%m-%d")
        self.actor = actor
        self.hash_chain_enabled = hash_chain

        self.entries: list[AuditEntry] = []
        self.stats = BriefingStats()
        self._injection_referrals: list[dict[str, Any]] = []
        self._invalid_referrals: list[dict[str, Any]] = []
        self._declined_steps: list[dict[str, Any]] = []
        self._escalations: list[dict[str, Any]] = []
        self._prev_hash = GENESIS_HASH
        self._sequence = 0
        self._started_at = datetime.now()

        os.makedirs(runs_dir, exist_ok=True)
        self.ledger_path = os.path.join(runs_dir, f"run_{self.run_date}_{run_id}.jsonl")
        self.summary_path = os.path.join(runs_dir, f"run_{self.run_date}_{run_id}.json")

        self._write_line({
            "record_type": "run_started",
            "run_id": run_id,
            "run_date": self.run_date,
            "actor": actor,
            "started_at": self._started_at.isoformat(),
            "hash_chain": hash_chain,
        })
        log_event(logger, "audit.run_started", run_id=run_id,
                  ledger_path=self.ledger_path, actor=actor)

    # ---- low-level ledger writes -----------------------------------------

    def _write_line(self, record: dict) -> dict:
        """Append one record and flush it to disk immediately."""
        if self.hash_chain_enabled:
            record["prev_hash"] = self._prev_hash
            record["entry_hash"] = compute_entry_hash(self._prev_hash, record)
            self._prev_hash = record["entry_hash"]

        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(_canonical(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    # ---- public API -------------------------------------------------------

    def log_action(
        self,
        action: ProposedAction,
        risk: RiskResult,
        result: ActionResult,
        *,
        injection_flagged: bool = False,
        injection_patterns: Optional[list[str]] = None,
        injection_fields: Optional[list[str]] = None,
        previous_state: Optional[dict] = None,
        new_state: Optional[dict] = None,
        duration_ms: float = 0.0,
    ) -> AuditEntry:
        """Log a proposed action and its resolution. Written to disk before returning."""
        self._sequence += 1
        status_val = (
            result.status.value if isinstance(result.status, ActionStatus) else str(result.status)
        )

        entry = AuditEntry(
            run_id=self.run_id,
            referral_id=action.referral_id,
            task_id=action.task_id,
            action_id=action.id,
            sequence=self._sequence,
            action_kind=action.action_kind,
            raw_action_kind=action.raw_action_kind,
            description=action.description,
            # `to_dict` projects live payload objects (a determination, a packet)
            # down to JSON. Writing `action.payload` directly would put reprs in
            # the ledger and make the hash depend on object addresses.
            payload=action.to_dict()["payload"],
            previous_state=previous_state or {},
            new_state=new_state or {},
            reasoning=action.reasoning,
            provision=risk.provision or (action.authority or {}).get("provision", ""),
            provision_quote=risk.provision_quote or (action.authority or {}).get("quote", ""),
            authority=action.authority or {},
            citations=[c.to_dict() for c in action.citations],
            confidence=action.confidence,
            risk_score=risk.score,
            risk_threshold=risk.threshold,
            hard_blocked=risk.hard_blocked,
            requires_approval=risk.requires_approval,
            gate_layer=risk.gate_layer,
            risk_reason=risk.reason,
            risk_components=risk.components,
            triggered_signals=list(risk.triggered_signals),
            signals=action.signals.to_dict(),
            resolution=status_val,
            resolution_detail=result.detail,
            execution_detail=result.execution_detail,
            executed=result.executed,
            actor=result.actor or SYSTEM_ACTOR,
            human_reason=result.human_reason,
            artifacts=list(result.artifacts),
            injection_flagged=injection_flagged,
            injection_patterns=injection_patterns or [],
            injection_fields=injection_fields or [],
            proposed_at=action.timestamp,
            decided_at=result.decided_at,
            duration_ms=round(duration_ms, 2),
        )

        record = {"record_type": "action", **asdict(entry)}
        record.pop("prev_hash", None)
        record.pop("entry_hash", None)
        written = self._write_line(record)
        entry.prev_hash = written.get("prev_hash", "")
        entry.entry_hash = written.get("entry_hash", "")

        self.entries.append(entry)
        self._update_stats(status_val, risk, duration_ms)

        if (
            action.action_kind == "escalate_to_supervisor"
            and status_val in EXECUTED_STATUSES
            and result.executed
        ):
            self.stats.escalations_filed += 1
            self._escalations.append({
                "referral_id": action.referral_id,
                "provisions": (action.authority or {}).get("escalated_provisions", []),
                "artifacts": list(result.artifacts),
                "detail": result.execution_detail or result.detail,
            })

        log_event(
            logger, "audit.action_logged",
            run_id=self.run_id, referral_id=action.referral_id, task_id=action.task_id,
            action_id=action.id, resolution=status_val, gate_layer=risk.gate_layer,
            provision=entry.provision,
            risk_score=round(risk.score, 4), actor=entry.actor, executed=entry.executed,
            duration_ms=round(duration_ms, 2),
        )
        return entry

    def _update_stats(self, status_val: str, risk: RiskResult, duration_ms: float) -> None:
        s = self.stats
        s.total_actions += 1
        s.total_duration_ms += duration_ms

        if status_val == ActionStatus.AUTO_EXECUTED.value:
            s.auto_executed += 1
        elif status_val == ActionStatus.APPROVED.value:
            s.human_approved += 1
        elif status_val == ActionStatus.REJECTED.value:
            s.human_rejected += 1
        elif status_val == ActionStatus.EDITED.value:
            s.human_edited += 1
        elif status_val == ActionStatus.SKIPPED.value:
            s.skipped += 1
        elif status_val == ActionStatus.AUTO_APPROVED_BYPASS.value:
            s.auto_approved_bypass += 1
        elif status_val == ActionStatus.REFUSED.value:
            s.refused += 1
        elif status_val == ActionStatus.ERROR.value:
            s.errors += 1

        # Gate accounting reads the layer the classifier reported. It does not
        # re-derive the decision from a hardcoded threshold, which is how the
        # old `elif risk.score >= 0.4` silently disagreed with a configured tau.
        if risk.requires_approval:
            s.gated_total += 1
        if risk.hard_blocked:
            s.hard_blocked += 1
        if risk.gate_layer == GateLayer.AUTHORITY_POLICY.value:
            s.authority_gated += 1
        elif risk.gate_layer == GateLayer.UNKNOWN_ACTION.value:
            s.unknown_action_gated += 1
        elif risk.gate_layer == GateLayer.FORCED_REVIEW.value:
            s.forced_review += 1
        elif risk.gate_layer == GateLayer.SCORE_THRESHOLD.value:
            s.risk_gated += 1

    def log_step_declined(self, referral_id: str, task_id: str, reason: str) -> None:
        """Record a step that considered this referral and chose not to act.

        Section 5.1 asks for what was declined, and a step returning `Skip` is the
        commonest form of that: the flagging step on a routine referral, the
        escalation step on one that stays inside section 2. Without this line the
        ledger cannot distinguish "considered and declined" from "never ran".
        """
        record = {
            "referral_id": referral_id,
            "task_id": task_id,
            "reason": reason,
            "at": datetime.now().isoformat(),
        }
        self._declined_steps.append(record)
        self.stats.steps_declined += 1
        self._write_line({"record_type": "step_declined", **record})

    def log_injection_flag(
        self,
        referral_id: str,
        patterns: list[str],
        fields: list[str],
        spans: Optional[list[str]] = None,
    ) -> None:
        """Record a quarantine event for the briefing and the security log."""
        record = {
            "referral_id": referral_id,
            "patterns": list(patterns),
            "fields": list(fields),
            # Raw offending text is retained deliberately: without it a reviewer
            # cannot tell a real attack from a false positive.
            "matched_spans": list(spans or []),
            "detected_at": datetime.now().isoformat(),
        }
        self._injection_referrals.append(record)
        self.stats.injection_flagged += 1
        self.stats.injection_fields_redacted += len(fields)
        self._write_line({"record_type": "security_event",
                          "event": "injection_quarantined", **record})

    def log_invalid_referral(self, index: int, reason: str, raw_id: str = "") -> None:
        """Record a referral that failed validation and was skipped."""
        record = {
            "index": index,
            "referral_id": raw_id,
            "reason": reason,
            "at": datetime.now().isoformat(),
        }
        self._invalid_referrals.append(record)
        self.stats.referrals_skipped_invalid += 1
        self._write_line({"record_type": "data_quality_event",
                          "event": "referral_skipped_invalid", **record})
        log_event(logger, "audit.referral_skipped_invalid",
                  run_id=self.run_id, referral_index=index, reason=reason)

    def log_referral_failed(self, referral_id: str, error: str) -> None:
        """Record a referral whose processing raised before it could complete.

        Section 4.3: one referral falling over must not stop the others. This is
        the line that proves the run knew it happened rather than quietly
        producing eleven records where twelve were expected.
        """
        record = {
            "referral_id": referral_id,
            "reason": error,
            "at": datetime.now().isoformat(),
        }
        self.stats.errors += 1
        self._write_line({"record_type": "data_quality_event",
                          "event": "referral_processing_failed", **record})
        log_event(logger, "audit.referral_failed", level=40,
                  run_id=self.run_id, referral_id=referral_id, reason=error[:500])

    def log_bypass_warning(self, count_expected: int) -> None:
        """Record that guardrails were disabled for this run. A security event."""
        self._write_line({
            "record_type": "security_event",
            "event": "guardrails_bypassed",
            "severity": "high",
            "detail": "Run started with --auto-approve. Every gated action was "
                      "executed without human review. Decisions are recorded as "
                      "auto_approved_bypass, NOT as human approvals. Actions "
                      "reserved to a supervisor by policy s.3 were still refused.",
            "at": datetime.now().isoformat(),
        })

    def set_total_referrals(self, count: int) -> None:
        self.stats.total_referrals = count

    # ---- summary ----------------------------------------------------------

    def save(self) -> str:
        """Write the run summary JSON. The ledger is already fully on disk."""
        finished = datetime.now()
        summary = {
            "run_id": self.run_id,
            "run_date": self.run_date,
            "actor": self.actor,
            "started_at": self._started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "wall_clock_seconds": round((finished - self._started_at).total_seconds(), 2),
            "ledger_path": self.ledger_path,
            "hash_chain": self.hash_chain_enabled,
            "final_hash": self._prev_hash,
            "stats": asdict(self.stats),
            "injection_flags": self._injection_referrals,
            "invalid_referrals": self._invalid_referrals,
            "declined_steps": self._declined_steps,
            "escalations": self._escalations,
            "entries": [asdict(e) for e in self.entries],
        }

        self._write_line({
            "record_type": "run_finished",
            "run_id": self.run_id,
            "finished_at": finished.isoformat(),
            "stats": asdict(self.stats),
        })

        with open(self.summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)

        log_event(logger, "audit.run_finished", run_id=self.run_id,
                  summary_path=self.summary_path, **asdict(self.stats))
        return self.summary_path

    # ---- briefing ---------------------------------------------------------

    def print_briefing(self) -> None:
        """Print the end-of-run morning briefing."""
        s = self.stats
        print("\n" + "=" * 72)
        print("MORNING BRIEFING")
        print("=" * 72)
        print(f"\n  Run date : {self.run_date}")
        print(f"  Run ID   : {self.run_id}")
        print(f"  Actor    : {self.actor}")
        print(f"\n  Referral queue")
        print(f"    Referrals triaged ............ {s.total_referrals}")
        if s.referrals_skipped_invalid:
            print(f"    Skipped (invalid data) ....... {s.referrals_skipped_invalid}")
        print(f"    Actions proposed ............. {s.total_actions}")
        if s.steps_declined:
            print(f"    Steps declined (nothing to do) {s.steps_declined}")
        print(f"\n  Authority — policy {POLICY_REF}")
        print(f"    Carried out under section 2 .. {s.auto_executed}")
        print(f"    Refused and escalated (s.3) .. {s.refused}")
        print(f"    Escalation packets filed ..... {s.escalations_filed}")
        print(f"\n  Human-in-the-loop")
        print(f"    Gated for review ............. {s.gated_total}")
        print(f"      - reserved to a supervisor . {s.authority_gated}")
        print(f"      - unknown action (failsafe)  {s.unknown_action_gated}")
        print(f"      - mandatory review signal .. {s.forced_review}")
        print(f"      - risk score >= threshold .. {s.risk_gated}")
        print(f"    Human approved ............... {s.human_approved}")
        print(f"    Human rejected ............... {s.human_rejected}")
        print(f"    Human edited ................. {s.human_edited}")
        print(f"    Deferred / skipped ........... {s.skipped}")
        if s.auto_approved_bypass:
            print(f"    !! GUARDRAIL BYPASS .......... {s.auto_approved_bypass}  "
                  f"(--auto-approve; NOT human approvals)")
        print(f"\n  Security")
        print(f"    Referrals quarantined ........ {s.injection_flagged}")
        print(f"    Fields redacted .............. {s.injection_fields_redacted}")
        print(f"\n  Reliability")
        print(f"    Errors ....................... {s.errors}")
        print(f"    Total task time .............. {s.total_duration_ms / 1000:.1f}s")

        if self._escalations:
            print("\n  ESCALATED TO A SUPERVISOR")
            for esc in self._escalations:
                provisions = ", ".join(f"s.{p}" for p in esc["provisions"]) or "s.3"
                print(f"    {esc['referral_id']}: {provisions}")
                for artifact in esc["artifacts"]:
                    print(f"      -> {artifact}")

        if self._injection_referrals:
            print("\n  INJECTION ALERTS")
            for flag in self._injection_referrals:
                print(f"    Referral {flag['referral_id']}: fields "
                      f"{', '.join(flag['fields'])} matched "
                      f"{', '.join(flag['patterns'])} — content redacted before planning")

        if self._invalid_referrals:
            print("\n  DATA QUALITY")
            for bad in self._invalid_referrals:
                print(f"    Record #{bad['index']} "
                      f"({bad['referral_id'] or 'no id'}): {bad['reason']}")

        awaiting = s.gated_total - s.human_decisions - s.auto_approved_bypass
        print(f"\n  {s.total_referrals} referrals triaged, "
              f"{s.auto_executed} actions carried out under section 2, "
              f"{s.refused} refused and escalated under section 3, "
              f"{s.gated_total} stopped for human review, "
              f"{s.human_decisions} decided by a human"
              + (f", {max(0, awaiting)} still awaiting a decision" if awaiting > 0 else "")
              + f", {s.injection_flagged} quarantined for injected content.")
        print("=" * 72 + "\n")


# ---------------------------------------------------------------------------
# Verification (used by the CLI and the web console)
# ---------------------------------------------------------------------------

def verify_chain(ledger_path: str) -> dict[str, Any]:
    """Recompute the hash chain over a ledger file and report tampering.

    Returns a dict with `valid`, `records`, and — when broken — the exact
    `broken_at` index plus what was expected versus found.
    """
    if not os.path.exists(ledger_path):
        return {"valid": False, "error": f"ledger not found: {ledger_path}", "records": 0}

    prev = GENESIS_HASH
    count = 0
    with open(ledger_path, "r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                return {"valid": False, "records": count, "broken_at": idx,
                        "error": f"line {idx} is not valid JSON: {exc}"}

            stored_prev = record.get("prev_hash")
            stored_hash = record.get("entry_hash")
            if stored_hash is None:
                # Chain disabled for this run; nothing to verify.
                return {"valid": True, "records": count, "chained": False,
                        "detail": "hash chain was disabled for this run"}

            if stored_prev != prev:
                return {"valid": False, "records": count, "broken_at": idx,
                        "error": "prev_hash does not match the previous entry's hash",
                        "expected_prev": prev, "found_prev": stored_prev}

            recomputed = compute_entry_hash(prev, record)
            if recomputed != stored_hash:
                return {"valid": False, "records": count, "broken_at": idx,
                        "error": "entry content does not match its recorded hash "
                                 "— this record was modified after it was written",
                        "expected_hash": recomputed, "found_hash": stored_hash}
            prev = stored_hash

    return {"valid": True, "records": count, "chained": True, "final_hash": prev}
