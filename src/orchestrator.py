from __future__ import annotations
"""Orchestrator — the morning run loop for Problem 5 (The Caseworker's Morning).

Architecture & Invariants:
1. Authority & Guardrails: The policy boundary is defined in data (AuthorityPolicy)
   and enforced structurally by EffectRegistry. The RiskClassifier gates actions
   deterministically before execution.
2. Safeguarding (ACA-2026/2): Households with children under 18 or unestablished
   household composition are prohibited from receiving automated draft triage notes
   (s.3.9). All established context is preserved and handed to a caseworker (s.3.2).
3. Append-Only Audit: Every action proposed, gated, executed or declined is written
   to a hash-chained audit ledger.
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from src.audit.log import AuditLog, BriefingStats
from src.config import Settings
from src.domain.referral import Referral, ReferralLoadResult, load_referrals
from src.effects.permitted import build_permitted_effects
from src.effects.registry import ActionNotPerformable, EffectOutcome, EffectRegistry, EffectRequest
from src.escalation import EscalationWriter
from src.handoff.packet import CaseworkerHandoffWriter
from src.history.client import ResidentHistoryClient
from src.hitl.gate import (
    ApprovalGate,
    AutoApproveGate,
    CLIGate,
)
from src.observability.logging_setup import (
    SecurityLog,
    bind,
    get_logger,
    log_event,
)
from src.policy.authority import AuthorityPolicy, load_policy
from src.rag.ingest import ingest_policy
from src.rag.retrieve import HybridRetriever
from src.risk.classifier import classify, explain_reachability
from src.security.screen import screen_and_quarantine
from src.tasks import DiscoveryReport, discover
from src.tasks.base import (
    ActionResult,
    ActionStatus,
    ProposedAction,
    RiskResult,
    RunContext,
    SYSTEM_ACTOR,
    Skip,
    Task,
    TaskOutcome,
)

logger = get_logger(__name__)

EventSink = Callable[[str, dict], None]


# ---------------------------------------------------------------------------
# Pipeline assembly
# ---------------------------------------------------------------------------

@dataclass
class Pipeline:
    """Everything a run needs, built once and reusable across runs."""
    policy: AuthorityPolicy
    registry: EffectRegistry
    history_client: ResidentHistoryClient
    escalation_writer: EscalationWriter
    handoff_writer: CaseworkerHandoffWriter
    tasks: list[Task]
    discovery: DiscoveryReport
    settings: Settings
    retriever: Optional[HybridRetriever] = None
    embedding_model: Any = None
    chunks: list = field(default_factory=list)

    def guardrail_report(self) -> list[dict[str, Any]]:
        rows = []
        for task in self.tasks:
            reach = explain_reachability(task.risk_profile, self.settings)
            rows.append({
                "task_id": task.id,
                "order": task.order,
                "default_action_kind": task.risk_profile.default_action_kind,
                **reach,
            })
        return rows


def build_pipeline(settings: Settings, *, strict: bool = True) -> Pipeline:
    """Build authority policy, effects registry, history client, and discover tasks."""
    policy = load_policy(
        rules_path=settings.policy_rules_path,
        source_path=settings.policy_document_path,
    )

    history_client = ResidentHistoryClient(
        base_url=settings.history_api_url,
        timeout=settings.history_timeout_seconds,
        retries=settings.history_retries,
        backoff=settings.history_retry_backoff_seconds,
        snapshot_path=settings.history_snapshot_path,
        allow_snapshot_fallback=settings.history_allow_snapshot_fallback,
    )

    escalation_writer = EscalationWriter(directory=settings.escalations_dir)
    handoff_writer = CaseworkerHandoffWriter(directory=os.path.join(settings.runs_dir, "..", "handoffs"))

    registry = EffectRegistry(policy=policy)
    effects = build_permitted_effects(
        history_client=history_client,
        escalation_writer=escalation_writer,
        handoff_writer=handoff_writer,
        settings=settings,
        artifacts_dir=settings.artifacts_dir,
        triage_record_path=settings.triage_record_path,
        flag_path=settings.flag_path,
    )
    registry.bind_all(effects)
    report = registry.verify()
    if not report.ok:
        raise RuntimeError(f"Effect registry invariant broken: {report.problems}")

    discovery = discover()
    if discovery.failures and strict:
        detail = "; ".join(
            f"{f['module']}({f['stage']}): {f['error']}" for f in discovery.failures
        )
        raise RuntimeError(f"Task discovery failed for {len(discovery.failures)} module(s): {detail}")

    tasks = discovery.ordered()
    for task in tasks:
        task.configure(
            policy=policy,
            registry=registry,
            history_client=history_client,
            settings=settings,
        )

    return Pipeline(
        policy=policy,
        registry=registry,
        history_client=history_client,
        escalation_writer=escalation_writer,
        handoff_writer=handoff_writer,
        tasks=tasks,
        discovery=discovery,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_id: str
    run_date: str
    ledger_path: str
    summary_path: str
    stats: BriefingStats
    context: RunContext
    referral_load_errors: list[dict[str, Any]] = field(default_factory=list)
    guardrail_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {
            "run_id": self.run_id,
            "run_date": self.run_date,
            "ledger_path": self.ledger_path,
            "summary_path": self.summary_path,
            "stats": asdict(self.stats),
            "referral_load_errors": self.referral_load_errors,
            "guardrail_warnings": self.guardrail_warnings,
        }


# ---------------------------------------------------------------------------
# The Morning Run
# ---------------------------------------------------------------------------

def run_morning(
    settings: Settings,
    *,
    gate: Optional[ApprovalGate] = None,
    auto_approve: bool = False,
    pipeline: Optional[Pipeline] = None,
    on_event: Optional[EventSink] = None,
    echo: bool = True,
    actor: str = SYSTEM_ACTOR,
    referral_limit: Optional[int] = None,
) -> RunResult:
    """Execute the full morning routine against the overnight referral queue."""
    emit = _make_emitter(on_event)
    say = _make_printer(echo)

    if gate is None:
        gate = AutoApproveGate() if auto_approve else CLIGate()

    if auto_approve:
        say("")
        say("!" * 74)
        say("  --auto-approve IS SET. Scored guardrails are being bypassed.")
        say("  Gated actions will execute without human review and are recorded as")
        say("  'auto_approved_bypass'. Restricted actions (s.3) are still refused.")
        say("!" * 74)
        say("")

    say("\n🏛️  Calder County — Automated Casework Assistant")
    say("=" * 74)

    # --- Load referrals --------------------------------------------------
    load = load_referrals(settings.referral_queue_path)
    referrals = load.referrals
    if referral_limit is not None:
        referrals = referrals[:referral_limit]
    say(f"Loaded {len(referrals)} referral(s) from overnight queue"
        + (f"; skipped {len(load.errors)} invalid record(s)" if load.errors else ""))

    # --- Build the pipeline ----------------------------------------------
    if pipeline is None:
        say("Initialising Authority Policy and Task Registry...")
        pipeline = build_pipeline(settings)
    tasks = pipeline.tasks
    say(f"Active tasks ({len(tasks)} in order): {[t.id for t in tasks]}")

    # Reset writers for this run
    context = RunContext(referrals=referrals, settings=settings, actor=actor)
    pipeline.escalation_writer.begin_run(context.run_id)
    pipeline.handoff_writer.begin_run(context.run_id)

    # --- Precompute authority determinations -----------------------------
    for referral in referrals:
        context.determinations[referral.referral_id] = pipeline.policy.determine(
            referral.requested_action,
            summary=referral.summary,
            source=referral.source,
        )

    # --- Audit & Security Logs -------------------------------------------
    audit = AuditLog(
        runs_dir=settings.runs_dir,
        run_id=context.run_id,
        actor=actor,
        hash_chain=settings.audit_hash_chain,
        run_date=context.run_date,
    )
    audit.set_total_referrals(len(referrals))
    security_log = SecurityLog(settings.log_dir)
    run_logger = bind(logger, run_id=context.run_id)

    for err in load.errors:
        audit.log_invalid_referral(err["index"], err["reason"], err.get("referral_id", ""))
    if auto_approve:
        audit.log_bypass_warning(count_expected=0)

    # --- Guardrail verification check ------------------------------------
    guardrail_warnings: list[str] = []
    say("=" * 74)
    emit("run_started", {
        "run_id": context.run_id,
        "run_date": context.run_date,
        "actor": actor,
        "referral_count": len(referrals),
        "tasks": [{"id": t.id, "description": t.description, "order": t.order} for t in tasks],
        "threshold": settings.risk_threshold,
        "auto_approve": auto_approve,
        "referral_load_errors": load.errors,
    })

    # --- Process referrals -----------------------------------------------
    for ref_index, referral in enumerate(referrals, start=1):
        ref_logger = bind(logger, run_id=context.run_id, referral_id=referral.referral_id)
        det = context.determinations[referral.referral_id]

        say(f"\n{'-' * 74}")
        say(f"Referral {ref_index}/{len(referrals)}: {referral.referral_id} — {referral.resident_ref}")
        say(f"  Source: {referral.source} | Urgency: {referral.urgency} | Asks: {referral.requested_action}")
        say(f"  Authority: {det.one_line()}")
        say("-" * 74)

        emit("referral_started", {
            "run_id": context.run_id,
            "index": ref_index,
            "total": len(referrals),
            "referral": referral.to_dict(),
            "determination": det.to_dict(),
        })

        # ---- Step 1: Screen & Quarantine untrusted text -----------------
        report = screen_and_quarantine(referral)
        if report.flagged:
            context.quarantined_referrals[referral.referral_id] = list(report.flagged_fields)
            say(f"  ⚠️  INJECTION DETECTED in {', '.join(report.flagged_fields)}")
            say(f"      Patterns: {', '.join(report.patterns)}")
            say("      Content redacted for prompt safety; human review enforced.")
            audit.log_injection_flag(
                referral_id=referral.referral_id,
                patterns=report.patterns,
                fields=report.flagged_fields,
                spans=report.matched_spans,
            )
            security_log.record(
                "injection_quarantined", "high",
                run_id=context.run_id, referral_id=referral.referral_id,
                fields=report.flagged_fields, patterns=report.patterns,
                matched_spans=report.matched_spans,
            )
            emit("injection_detected", {
                "run_id": context.run_id,
                "referral_id": referral.referral_id,
                **report.to_dict(),
            })

        # ---- Step 2: Run Tasks in Pipeline Order -------------------------
        state_before = referral.to_dict()

        for task in tasks:
            started = time.perf_counter()

            try:
                action_or_skip = task.plan(referral, context)

                # If the task chooses not to act, log Skip and continue
                if isinstance(action_or_skip, Skip) or action_or_skip is None:
                    reason = action_or_skip.reason if isinstance(action_or_skip, Skip) else "No action required."
                    audit.log_step_declined(
                        referral_id=referral.referral_id,
                        task_id=task.id,
                        reason=reason,
                    )
                    continue

                action: ProposedAction = action_or_skip
                say(f"\n  Task: {task.id} (s.{task.provision}) — {action.action_kind}")
                say(f"    Proposed: {action.description[:110]}")

                emit("action_planned", {
                    "task_id": task.id,
                    "case_id": referral.referral_id,
                    "action": action.to_dict(),
                })

                risk = classify(action, task.risk_profile, settings, policy=pipeline.policy)
                say(f"    Risk: {risk.score:.3f} (τ={risk.threshold}) -> "
                    f"{'GATED [' + risk.gate_layer + ']' if risk.requires_approval else 'CLEAR'}")

                emit("risk_classified", {
                    "action_id": action.id,
                    "risk": risk.to_dict(),
                })

                # Execute or Gate
                if risk.requires_approval:
                    emit("action_gated", {
                        "action_id": action.id,
                        "referral_id": referral.referral_id,
                        "action_kind": action.action_kind,
                        "description": action.description,
                        "risk_score": risk.score,
                        "gate_layer": risk.gate_layer,
                    })
                    action_result = _handle_gated(
                        pipeline.registry, action, risk, referral, context, gate, say, emit
                    )
                else:
                    action_result = _execute_effect(
                        pipeline.registry, action, referral, context, ActionStatus.AUTO_EXECUTED, SYSTEM_ACTOR, ""
                    )
                    say(f"    Executed: {action_result.detail[:100]}")

                emit("action_executed", {
                    "action_id": action.id,
                    "referral_id": referral.referral_id,
                    "action_kind": action.action_kind,
                    "description": action.description,
                    "status": action_result.status.value,
                    "detail": action_result.detail,
                    "executed": action_result.executed,
                    "risk_score": risk.score,
                })

            except Exception as exc:  # noqa: BLE001
                duration_ms = (time.perf_counter() - started) * 1000
                say(f"    ❌ ERROR in task {task.id}: {type(exc).__name__}: {exc}")
                log_event(ref_logger, "task.failed", level=40,
                          task_id=task.id, error_type=type(exc).__name__,
                          error=str(exc)[:500], duration_ms=round(duration_ms, 2))
                action, risk, action_result = _error_triple(task, referral, exc)
                audit.log_action(
                    action=action, risk=risk, result=action_result,
                    injection_flagged=report.flagged,
                    injection_patterns=report.patterns,
                    injection_fields=report.flagged_fields,
                    previous_state=state_before,
                    new_state=referral.to_dict(),
                    duration_ms=duration_ms,
                )
                continue

            duration_ms = (time.perf_counter() - started) * 1000
            audit.log_action(
                action=action, risk=risk, result=action_result,
                injection_flagged=report.flagged,
                injection_patterns=report.patterns,
                injection_fields=report.flagged_fields,
                previous_state=state_before,
                new_state=referral.to_dict(),
                duration_ms=duration_ms,
            )

            # Record outcome for subsequent tasks in the pipeline
            outcome = TaskOutcome(
                task_id=task.id,
                referral_id=referral.referral_id,
                action=action,
                risk=risk,
                result=action_result,
                value=getattr(action_result, "value", None),
            )
            context.record(outcome)

            # If retrieve_history succeeded, cache history in context
            if task.id == "retrieve_history" and outcome.value is not None:
                context.histories[referral.referral_id] = outcome.value

        emit("referral_completed", {
            "run_id": context.run_id,
            "referral_id": referral.referral_id,
            "index": ref_index,
            "total": len(referrals),
        })

    # --- Write Indexes & Wrap Up -----------------------------------------
    if pipeline.escalation_writer.filed:
        esc_idx = pipeline.escalation_writer.write_index(run_date=context.run_date)
        say(f"\nFiled {len(pipeline.escalation_writer.filed)} Section 4 supervisor escalation(s) -> {esc_idx}")

    if pipeline.handoff_writer.filed:
        handoff_idx = pipeline.handoff_writer.write_index(run_date=context.run_date)
        say(f"Filed {len(pipeline.handoff_writer.filed)} ACA-2026/2 s.3.9 safeguarding hand-off(s) -> {handoff_idx}")

    summary_path = audit.save()
    say(f"\nAudit ledger : {audit.ledger_path}")
    say(f"Run summary  : {summary_path}")

    if echo:
        audit.print_briefing()

    return RunResult(
        run_id=context.run_id,
        run_date=context.run_date,
        ledger_path=audit.ledger_path,
        summary_path=summary_path,
        stats=audit.stats,
        context=context,
        referral_load_errors=load.errors,
        guardrail_warnings=guardrail_warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _handle_gated(
    registry: EffectRegistry,
    action: ProposedAction,
    risk: RiskResult,
    referral: Referral,
    context: RunContext,
    gate: ApprovalGate,
    say: Callable[[str], None],
    emit: EventSink,
) -> ActionResult:
    """Request human decision at the gate, then execute or record refusal."""
    decision = gate.request(action, risk, referral)

    if not decision.approved:
        detail = (
            f"{decision.status.value} by {decision.actor}. Action was not executed."
            + (f" Reason: {decision.reason}" if decision.reason else "")
        )
        say(f"    Decision: {detail}")
        return ActionResult(
            action_id=action.id,
            status=decision.status,
            detail=detail,
            human_reason=decision.reason,
            actor=decision.actor,
            executed=False,
            execution_detail="Not executed — rejected or refused by human review.",
            decided_at=decision.decided_at,
        )

    if decision.edited_payload is not None:
        action.payload = {**(action.payload or {}), **decision.edited_payload}

    return _execute_effect(
        registry, action, referral, context, decision.status, decision.actor,
        decision.reason, decided_at=decision.decided_at
    )


def _execute_effect(
    registry: EffectRegistry,
    action: ProposedAction,
    referral: Referral,
    context: RunContext,
    status: ActionStatus,
    actor: str,
    human_reason: str,
    decided_at: Optional[str] = None,
) -> ActionResult:
    """Perform the permitted effect via EffectRegistry and return ActionResult."""
    history = context.history_for(referral.referral_id)
    request = EffectRequest(
        referral=referral,
        action_kind=action.action_kind,
        run_id=context.run_id,
        actor=actor,
        history=history,
        payload=action.payload,
    )

    try:
        outcome: EffectOutcome = registry.perform(request)
    except ActionNotPerformable as exc:
        return ActionResult(
            action_id=action.id,
            status=ActionStatus.REFUSED,
            detail=str(exc),
            human_reason=human_reason,
            actor=actor,
            executed=False,
            execution_detail=f"Refused by policy: {exc.quote}",
            decided_at=decided_at or datetime.now().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(
            action_id=action.id,
            status=ActionStatus.ERROR,
            detail=f"Execution error: {type(exc).__name__}: {exc}",
            human_reason=human_reason,
            actor=actor,
            executed=False,
            execution_detail=str(exc),
            decided_at=decided_at or datetime.now().isoformat(),
        )

    res = ActionResult(
        action_id=action.id,
        status=status if outcome.ok else ActionStatus.ERROR,
        detail=outcome.summary,
        human_reason=human_reason,
        actor=actor,
        executed=outcome.ok,
        execution_detail=json.dumps(outcome.detail, default=str),
        artifacts=outcome.artifacts,
        decided_at=decided_at or datetime.now().isoformat(),
    )
    # Attach live value for in-memory pipeline sharing
    setattr(res, "value", outcome.value)
    return res


def _error_triple(task: Task, referral: Referral, exc: Exception):
    from src.tasks.base import GateLayer, RiskSignals

    signals = RiskSignals(escalation_requested=True, data_incomplete=True)
    signals.notes.append(f"Task raised {type(exc).__name__}: {exc}")

    action = ProposedAction(
        task_id=task.id,
        referral_id=referral.referral_id,
        action_kind=task.risk_profile.default_action_kind,
        description=f"Task {task.id} failed: {type(exc).__name__}: {exc}",
        reasoning="Task raised an exception before producing an action.",
        confidence=0.0,
        signals=signals,
    )
    risk = RiskResult(
        score=1.0,
        hard_blocked=False,
        requires_approval=True,
        gate_layer=GateLayer.FORCED_REVIEW.value,
        reason=f"Task raised {type(exc).__name__}: {exc}",
        action_kind=task.risk_profile.default_action_kind,
        components={"error": 1.0},
    )
    result = ActionResult(
        action_id=action.id,
        status=ActionStatus.ERROR,
        detail=f"{type(exc).__name__}: {exc}",
        actor=SYSTEM_ACTOR,
        executed=False,
        execution_detail="Failed before execution.",
    )
    return action, risk, result


def _make_emitter(on_event: Optional[EventSink]) -> EventSink:
    if on_event is None:
        return lambda event, payload: None

    def emit(event: str, payload: dict) -> None:
        try:
            on_event(event, payload)
        except Exception:
            pass

    return emit


def _make_printer(echo: bool) -> Callable[[str], None]:
    if not echo:
        return lambda msg: None
    return print
