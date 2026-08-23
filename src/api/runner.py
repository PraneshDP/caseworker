from __future__ import annotations
"""Run manager — owns the background morning run that the web console drives.

The orchestrator is synchronous and blocking on purpose: `QueueGate.request()`
parks the worker thread until a human answers. That is the correct shape for a
human-in-the-loop system — the work genuinely is stopped — but it means the run
cannot live on the event loop. So it lives on a worker thread, and this class is
the thread-safe boundary between it and the HTTP handlers.

Responsibilities, all of them small:
  * build the RAG pipeline once and reuse it across runs (model load is slow)
  * start / cancel exactly one run at a time
  * buffer run events so a reconnecting browser can replay from a sequence number
  * expose the pending approval queue and forward decisions to the QueueGate
  * read historical run ledgers back off disk and verify their hash chains
"""

import json
import os
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.audit.log import verify_chain
from src.config import Settings
from src.domain.referral import load_referrals
from src.hitl.gate import AutoApproveGate, QueueGate
from src.observability.logging_setup import get_logger, log_event
from src.orchestrator import Pipeline, build_pipeline, run_morning
from src.policy.authority import load_policy
from src.risk.classifier import explain_reachability
from src.tasks.base import ActionStatus

logger = get_logger(__name__)

# How many events to keep for replay. 12 cases x 4 tasks emits roughly 150
# events; 5000 covers a long session without unbounded growth.
EVENT_BUFFER_SIZE = 5000

DECISION_MAP = {
    "approve": ActionStatus.APPROVED,
    "approved": ActionStatus.APPROVED,
    "reject": ActionStatus.REJECTED,
    "rejected": ActionStatus.REJECTED,
    "edit": ActionStatus.EDITED,
    "edited": ActionStatus.EDITED,
    "skip": ActionStatus.SKIPPED,
    "skipped": ActionStatus.SKIPPED,
    "defer": ActionStatus.SKIPPED,
}


@dataclass
class RunState:
    """A snapshot of what the worker thread is doing, safe to serialise."""
    status: str = "idle"          # idle | preparing | running | completed | failed | cancelled
    run_id: str = ""
    run_date: str = ""
    actor: str = ""
    auto_approve: bool = False
    started_at: str = ""
    finished_at: str = ""
    case_total: int = 0
    case_index: int = 0
    current_case_id: str = ""
    current_task_id: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    ledger_path: str = ""
    summary_path: str = ""
    error: str = ""
    guardrail_warnings: list[str] = field(default_factory=list)
    case_load_errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


class RunManager:
    """Single-run coordinator shared by every HTTP handler."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._pipeline: Optional[Pipeline] = None
        self._pipeline_error: str = ""
        self._thread: Optional[threading.Thread] = None
        self._gate: Optional[QueueGate] = None
        self._state = RunState()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._version = 0                       # bumped on any change, for polling
        self._change = threading.Event()
        self._actions: dict[str, dict[str, Any]] = {}   # action_id -> latest snapshot
        self._case_view: dict[str, dict[str, Any]] = {}  # case_id -> live case dict

    # -- pipeline ----------------------------------------------------------

    def pipeline_ready(self) -> bool:
        with self._lock:
            return self._pipeline is not None

    def ensure_pipeline(self) -> Pipeline:
        """Build the pipeline on first use. Callers must expect this to be slow."""
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
        pipeline = build_pipeline(self.settings)
        with self._lock:
            self._pipeline = pipeline
            self._pipeline_error = ""
        return pipeline

    # -- events ------------------------------------------------------------

    def _push(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "event": event,
                "ts": datetime.now().isoformat(),
                "payload": payload,
            })
            if len(self._events) > EVENT_BUFFER_SIZE:
                del self._events[: len(self._events) - EVENT_BUFFER_SIZE]
            self._version += 1
        self._change.set()

    def events_since(self, since: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since][:limit]

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def version(self) -> int:
        with self._lock:
            return self._version

    def wait_for_change(self, timeout: float) -> bool:
        flag = self._change.wait(timeout)
        if flag:
            self._change.clear()
        return flag

    # -- run lifecycle -----------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        *,
        auto_approve: bool = False,
        case_limit: Optional[int] = None,
        actor: str = "human:web-console",
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Kick off a run on a worker thread. Refuses if one is already active."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"started": False, "reason": "A run is already in progress."}

            self._gate = QueueGate(timeout_seconds=timeout_seconds, actor=actor)
            self._state = RunState(
                status="preparing",
                actor=actor,
                auto_approve=auto_approve,
                started_at=datetime.now().isoformat(),
            )
            self._actions = {}
            self._case_view = {}
            gate = AutoApproveGate() if auto_approve else self._gate

            thread = threading.Thread(
                target=self._run,
                kwargs={"gate": gate, "auto_approve": auto_approve,
                        "case_limit": case_limit, "actor": actor},
                name="morning-run",
                daemon=True,
            )
            self._thread = thread

        self._push("run_queued", {"auto_approve": auto_approve, "actor": actor})
        thread.start()
        return {"started": True}

    def _run(self, *, gate, auto_approve: bool, case_limit, actor: str) -> None:
        try:
            pipeline = self.ensure_pipeline()
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            log_event(logger, "api.pipeline_build_failed", level=40,
                      error=detail, traceback=traceback.format_exc()[-2000:])
            with self._lock:
                self._state.status = "failed"
                self._state.error = f"Pipeline initialisation failed — {detail}"
                self._state.finished_at = datetime.now().isoformat()
                self._pipeline_error = detail
            self._push("run_failed", {"error": detail})
            return

        try:
            with self._lock:
                self._state.status = "running"
            result = run_morning(
                self.settings,
                gate=gate,
                auto_approve=auto_approve,
                pipeline=pipeline,
                on_event=self._on_event,
                echo=False,
                actor=actor if auto_approve is False else actor,
                referral_limit=case_limit,
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            log_event(logger, "api.run_failed", level=40,
                      error=detail, traceback=traceback.format_exc()[-2000:])
            with self._lock:
                self._state.status = "failed"
                self._state.error = detail
                self._state.finished_at = datetime.now().isoformat()
            self._push("run_failed", {"error": detail})
            return

        with self._lock:
            self._state.status = "completed"
            self._state.finished_at = datetime.now().isoformat()
            self._state.ledger_path = result.ledger_path
            self._state.summary_path = result.summary_path
            self._state.guardrail_warnings = list(result.guardrail_warnings)
            self._state.case_load_errors = list(result.referral_load_errors)

    def _on_event(self, event: str, payload: dict[str, Any]) -> None:
        """Called from the worker thread for every orchestrator event."""
        with self._lock:
            state = self._state
            if event == "run_started":
                state.run_id = payload.get("run_id", "")
                state.run_date = payload.get("run_date", "")
                state.case_total = payload.get("case_count", 0)
                state.guardrail_warnings = list(payload.get("guardrail_warnings", []))
                state.case_load_errors = list(payload.get("case_load_errors", []))
            elif event == "case_started":
                state.case_index = payload.get("index", state.case_index)
                state.current_case_id = payload.get("case", {}).get("id", "")
                self._case_view[state.current_case_id] = payload.get("case", {})
            elif event == "case_completed":
                case = payload.get("case", {})
                if case.get("id"):
                    self._case_view[case["id"]] = case
                state.current_task_id = ""
            elif event == "action_planned":
                state.current_task_id = payload.get("task_id", "")
                action = payload.get("action", {})
                if action.get("id"):
                    self._actions[action["id"]] = {
                        "action": action, "risk": None, "result": None,
                        "case_id": payload.get("case_id"),
                        "task_id": payload.get("task_id"),
                    }
            elif event == "risk_classified":
                entry = self._actions.get(payload.get("action_id", ""))
                if entry is not None:
                    entry["risk"] = payload.get("risk")
            elif event == "action_completed":
                action = payload.get("action", {})
                entry = self._actions.setdefault(action.get("id", ""), {})
                entry.update({
                    "action": action,
                    "risk": payload.get("risk"),
                    "result": payload.get("result"),
                    "case_id": payload.get("case_id"),
                    "task_id": payload.get("task_id"),
                    "duration_ms": payload.get("duration_ms"),
                })
            elif event == "run_completed":
                state.stats = payload.get("stats", {})
                state.ledger_path = payload.get("ledger_path", "")
                state.summary_path = payload.get("summary_path", "")

        self._push(event, payload)

    def cancel(self) -> dict[str, Any]:
        """Release every pending approval as deferred. The run then unwinds."""
        with self._lock:
            gate = self._gate
            running = self._thread is not None and self._thread.is_alive()
        if not running:
            return {"cancelled": False, "reason": "No run is in progress."}
        released = gate.cancel_all("Run cancelled from the web console.") if gate else 0
        with self._lock:
            self._state.status = "cancelled"
        self._push("run_cancelled", {"released": released})
        return {"cancelled": True, "released": released}

    # -- approvals ---------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            gate = self._gate
        return gate.pending() if gate else []

    def resolve(
        self,
        action_id: str,
        decision: str,
        *,
        actor: str = "",
        reason: str = "",
        edited_payload: Optional[dict] = None,
    ) -> dict[str, Any]:
        with self._lock:
            gate = self._gate
        if gate is None:
            return {"ok": False, "error": "No run is waiting for decisions."}

        key = (decision or "").strip().lower()
        status = DECISION_MAP.get(key)
        if status is None:
            return {"ok": False, "error":
                    f"Unknown decision {decision!r}. Expected one of: "
                    f"{sorted(set(DECISION_MAP))}."}

        # A reject or skip must never carry an edit through.
        if status not in (ActionStatus.EDITED,):
            edited_payload = None
        if status is ActionStatus.EDITED and not edited_payload:
            return {"ok": False, "error":
                    "An 'edit' decision must include a non-empty edited_payload."}
        if status in (ActionStatus.REJECTED,) and not reason.strip():
            return {"ok": False, "error":
                    "A rejection requires a reason — the audit trail has to record why."}

        ok = gate.resolve(action_id, status, actor=actor, reason=reason,
                          edited_payload=edited_payload)
        if not ok:
            return {"ok": False, "error":
                    f"Action {action_id} is not waiting for a decision (already "
                    f"resolved, timed out, or never existed)."}
        self._push("approval_resolved", {
            "action_id": action_id, "status": status.value,
            "actor": actor, "reason": reason,
        })
        return {"ok": True, "action_id": action_id, "status": status.value}

    # -- views -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            data = self._state.to_dict()
            data["running"] = self._thread is not None and self._thread.is_alive()
            data["pending_count"] = self._gate.pending_count() if self._gate else 0
            data["pipeline_ready"] = self._pipeline is not None
            data["pipeline_error"] = self._pipeline_error
            data["latest_seq"] = self._seq
            return data

    def actions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._actions.values())

    def cases_live(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._case_view.values())

    # -- static config for the UI ------------------------------------------

    def config_view(self) -> dict[str, Any]:
        with self._lock:
            pipeline = self._pipeline
        tasks: list[dict[str, Any]] = []
        if pipeline is not None:
            for row in pipeline.guardrail_report():
                task = next(t for t in pipeline.tasks if t.id == row["task_id"])
                tasks.append({**row, "description": task.description})
        policy = load_policy(self.settings.policy_rules_path, self.settings.policy_document_path)
        restricted = sorted([r.action_kind for r in policy.restricted])
        return {
            "threshold": self.settings.risk_threshold,
            "hard_blocked_actions": restricted,
            "action_types": sorted([r.action_kind for r in policy.all_rules()]),
            "tasks": tasks,
            "pipeline_ready": pipeline is not None,
            "model": self.settings.gemini_model,
            "llm_configured": bool(self.settings.gemini_api_key),
            "embedding_model": self.settings.embedding_model,
            "runs_dir": self.settings.runs_dir,
            "policy_path": self.settings.policy_document_path,
        }

    def cases_seed(self) -> dict[str, Any]:
        load = load_referrals(self.settings.referral_queue_path)
        return {
            "cases": [c.to_dict() for c in load.referrals],
            "errors": load.errors,
        }

    def reachability(self) -> list[dict[str, Any]]:
        """Guardrail self-audit — usable without building the RAG pipeline."""
        from src.tasks import discover
        policy = load_policy(self.settings.policy_rules_path, self.settings.policy_document_path)
        restricted_kinds = {r.action_kind for r in policy.restricted}
        rows = []
        report = discover()
        for task in report.ordered():
            rows.append({
                "task_id": task.id,
                "description": task.description,
                "order": task.order,
                "default_action_type": task.risk_profile.default_action_kind,
                "hard_blocked_by_default": (
                    task.risk_profile.default_action_kind in restricted_kinds
                ),
                **explain_reachability(task.risk_profile, self.settings),
            })
        return rows

    # -- history / ledgers -------------------------------------------------

    def run_history(self, limit: int = 50) -> list[dict[str, Any]]:
        runs_dir = Path(self.settings.runs_dir)
        if not runs_dir.exists():
            return []
        out = []
        for path in sorted(runs_dir.glob("run_*.json"), reverse=True)[:limit]:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    summary = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                out.append({"summary_path": str(path), "error": str(exc)})
                continue
            out.append({
                "run_id": summary.get("run_id", path.stem),
                "run_date": summary.get("run_date", ""),
                "started_at": summary.get("started_at", ""),
                "finished_at": summary.get("finished_at", ""),
                "actor": summary.get("actor", ""),
                "stats": summary.get("stats", {}),
                "summary_path": str(path),
                "ledger_path": summary.get("ledger_path", ""),
                "entry_count": len(summary.get("entries", [])),
                "final_hash": summary.get("final_hash", ""),
                "hash_chain": summary.get("hash_chain", False),
                "wall_clock_seconds": summary.get("wall_clock_seconds", 0),
                "injection_flags": summary.get("injection_flags", []),
                "invalid_cases": summary.get("invalid_cases", []),
            })
        return out

    def ledger(self, run_id: str) -> dict[str, Any]:
        """Read one run's append-only ledger plus its chain verification."""
        path = self._ledger_path_for(run_id)
        if path is None:
            return {"error": f"No ledger found for run {run_id!r}.", "entries": []}

        entries = []
        with open(path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    entries.append({"record_type": "unparseable",
                                    "line": line_no, "error": str(exc)})
        return {
            "run_id": run_id,
            "ledger_path": str(path),
            "entries": entries,
            "verification": verify_chain(str(path)),
        }

    def verify(self, run_id: str) -> dict[str, Any]:
        path = self._ledger_path_for(run_id)
        if path is None:
            return {"error": f"No ledger found for run {run_id!r}."}
        return {"run_id": run_id, "ledger_path": str(path),
                **verify_chain(str(path))}

    def _ledger_path_for(self, run_id: str) -> Optional[Path]:
        runs_dir = Path(self.settings.runs_dir)
        if not runs_dir.exists():
            return None
        # run files are named run_<date>_<short-id>.jsonl
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        if not safe:
            return None
        matches = sorted(runs_dir.glob(f"*{safe}*.jsonl"), reverse=True)
        if matches:
            return matches[0]
        # fall back to the newest ledger when asked for "latest"
        if run_id in ("latest", "current"):
            all_ledgers = sorted(runs_dir.glob("run_*.jsonl"), reverse=True)
            return all_ledgers[0] if all_ledgers else None
        return None

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        base = Path(self.settings.artifacts_dir)
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        target = base / safe if safe else base
        if not target.exists():
            return []
        out = []
        for path in sorted(target.rglob("*")):
            if path.is_file():
                out.append({
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "kind": path.suffix.lstrip("."),
                })
        return out

    def read_artifact(self, run_id: str, name: str) -> dict[str, Any]:
        base = Path(self.settings.artifacts_dir).resolve()
        safe_run = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        safe_name = os.path.basename(name)
        target = (base / safe_run / safe_name).resolve()
        # Path containment check: never serve a file outside the artifacts dir.
        if not str(target).startswith(str(base) + os.sep):
            return {"error": "Refused: path escapes the artifacts directory."}
        if not target.is_file():
            return {"error": f"No artifact named {safe_name!r} for run {safe_run!r}."}
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"name": safe_name, "path": str(target), "content": text}
