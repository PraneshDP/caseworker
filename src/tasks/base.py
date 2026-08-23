"""Core types — the Task contract and the data classes everything else depends on.

Design rules enforced here:

  1. `action_kind` is canonicalised on construction and checked against the
     authority policy, not against a list in this file. An unrecognised kind is
     recorded as `unknown_action` so the classifier can fail CLOSED on it, and
     adding a kind is a change to `data/policy/authority-rules.json` rather than
     to Python.

  2. Risk signals travel with the action and can only ever RAISE risk. There is no
     field here that lowers a score, so a planning model cannot argue its way
     below the gate threshold.

  3. Every ActionResult records WHO acted. An audit trail without an actor is not
     an audit trail.

  4. `Task` HAS NO `execute()`. This is the important one.

     In the version of this system that handled a generic caseload, each Task both
     planned an action and carried it out. That put the effect inside the same
     object as the reasoning, which meant the only thing standing between a plan
     and a side effect was the orchestrator remembering to check a boolean.

     Tasks now propose and nothing more. Effects live in
     `src/effects/registry.py`, which will only bind a callable to an action kind
     the policy marks performable. A task that wanted to terminate an award could
     not do so by writing the code -- there is no method on it that runs, and the
     registry would refuse the binding.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional, Union

from src.domain.referral import Referral, ResidentHistory

#: Canonical sentinel for a kind the policy does not name. Never produced on
#: purpose by a task; produced by `normalise_action_kind` so the classifier has
#: something concrete to fail closed on.
UNKNOWN_ACTION_KIND = "unknown_action"


def normalise_action_kind(raw: Any) -> str:
    """Canonicalise an action-kind string to snake_case.

    Guards against the trivial evasions a raw `in set()` lookup misses: casing,
    surrounding whitespace, hyphen or dot separators, and camelCase. Recognition
    itself is not decided here -- the caller checks the result against
    `AuthorityPolicy.known_action_kinds()`, so the vocabulary lives in the policy
    data and this function only normalises spelling.
    """
    if raw is None:
        return UNKNOWN_ACTION_KIND
    text = str(raw).strip()
    if not text:
        return UNKNOWN_ACTION_KIND

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)   # camelCase -> camel_Case
    text = re.sub(r"[\s\-.]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text or UNKNOWN_ACTION_KIND


def is_recognised_kind(kind: str, known: Iterable[str]) -> bool:
    return bool(kind) and kind != UNKNOWN_ACTION_KIND and kind in set(known)


# ---------------------------------------------------------------------------
# Risk profile (static hints attached to each Task)
# ---------------------------------------------------------------------------

@dataclass
class RiskProfile:
    """Static risk hints for a Task.

    Not the final risk score -- the classifier combines these with the per-action
    RiskSignals below. These three numbers form the deterministic FLOOR of the
    score and never come from model output.
    """
    reversibility: float = 1.0     # 0.0 = fully irreversible, 1.0 = trivially reversible
    scope_of_impact: float = 0.0   # 0.0 = one referral, 1.0 = system-wide
    financial_impact: float = 0.0  # 0.0 = no money moves, 1.0 = large amount
    default_action_kind: str = "categorise_referral"


@dataclass
class RiskSignals:
    """Per-action risk signals supplied by the planning task.

    These make the scored layer live: without them, risk is a property of the task
    *class*, so two very different referrals handled by the same task would score
    identically.

    CONTRACT: signals may only RAISE the score. There is no field here that can
    lower it, and the classifier does not read `confidence` in a direction that
    could.
    """
    irreversible: bool = False
    adverse_to_resident: bool = False        # reduces, suspends or terminates support
    affects_entitlement: bool = False        # touches award amount or eligibility
    authority_restricted: bool = False       # the policy reserves this to a supervisor
    financial_amount: float = 0.0            # absolute amount at stake
    data_incomplete: bool = False            # planned on missing or unverified inputs
    unverified_citation: bool = False        # a policy claim failed citation check
    injection_suspected: bool = False        # untrusted content tripped the screen
    escalation_requested: bool = False       # the planner itself asked for a human
    notes: list[str] = field(default_factory=list)

    _BOOLEAN_NAMES = (
        "irreversible", "adverse_to_resident", "affects_entitlement",
        "authority_restricted", "data_incomplete", "unverified_citation",
        "injection_suspected", "escalation_requested",
    )

    def active(self) -> list[str]:
        """Names of the boolean signals that are set, for audit and UI display."""
        return [n for n in self._BOOLEAN_NAMES if getattr(self, n)]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {n: getattr(self, n) for n in self._BOOLEAN_NAMES}
        out["financial_amount"] = self.financial_amount
        out["notes"] = list(self.notes)
        return out


# ---------------------------------------------------------------------------
# Proposed action (output of Task.plan)
# ---------------------------------------------------------------------------

@dataclass
class Citation:
    """A reference to a specific policy passage that grounds a claim."""
    chunk_id: str
    section_path: str
    clause_id: str
    content_snippet: str
    similarity_score: Optional[float] = None  # None = not verified, not "zero similarity"
    claim: str = ""
    verified: bool = False
    requested_clause: str = ""
    clause_matched: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "section_path": self.section_path,
            "clause_id": self.clause_id,
            "content_snippet": self.content_snippet[:200],
            "similarity_score": self.similarity_score,
            "claim": self.claim,
            "verified": self.verified,
            "requested_clause": self.requested_clause,
            "clause_matched": self.clause_matched,
        }


@dataclass
class ProposedAction:
    """What a task wants to happen. Typed, never free text.

    `action_kind` is canonicalised on construction. Whether it is *recognised* is
    decided by the classifier against the policy, because the policy owns the
    vocabulary -- see `normalise_action_kind`.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_id: str = ""
    referral_id: str = ""
    action_kind: str = ""
    description: str = ""            # plain-language summary for the reviewer
    payload: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.8
    signals: RiskSignals = field(default_factory=RiskSignals)
    #: The authority determination for this action, as a dict. Attached by the
    #: task so the gate and the ledger can show the provision without recomputing.
    authority: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    # Set by __post_init__; not supplied by callers.
    raw_action_kind: str = ""

    def __post_init__(self) -> None:
        self.raw_action_kind = "" if self.action_kind is None else str(self.action_kind)
        self.action_kind = normalise_action_kind(self.action_kind)

        # A confidence outside [0,1] is a planner bug. Clamp it, and treat the
        # out-of-range value as a reason to distrust the action rather than as
        # something to silently fix.
        try:
            conf = float(self.confidence)
        except (TypeError, ValueError):
            conf = 0.0
            self.signals.notes.append("Planner returned a non-numeric confidence.")
        if conf < 0.0 or conf > 1.0:
            self.signals.notes.append(
                f"Planner returned out-of-range confidence {conf!r}; clamped."
            )
            conf = max(0.0, min(1.0, conf))
        self.confidence = conf

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "referral_id": self.referral_id,
            "action_kind": self.action_kind,
            "raw_action_kind": self.raw_action_kind,
            "description": self.description,
            "payload": _jsonable(self.payload),
            "reasoning": self.reasoning,
            "citations": [c.to_dict() for c in self.citations],
            "confidence": self.confidence,
            "signals": self.signals.to_dict(),
            "authority": self.authority,
            "timestamp": self.timestamp,
        }


def _jsonable(value: Any) -> Any:
    """Best-effort JSON projection of a payload.

    Payloads carry live objects between steps (a determination, an assessment, a
    packet). The ledger must hold plain JSON, so anything with `to_dict` is
    projected and anything else unrepresentable becomes its repr rather than
    breaking the write. A ledger entry that fails to serialise is a lost record.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:                              # noqa: BLE001
            return repr(value)
    return repr(value)


# ---------------------------------------------------------------------------
# Risk classification result
# ---------------------------------------------------------------------------

class GateLayer(str, Enum):
    """Which guardrail layer decided this action's fate.

    Recorded in the ledger so "why did this need a human?" is answerable without
    re-deriving the score.
    """
    NONE = "none"                          # below threshold, carried out
    AUTHORITY_POLICY = "authority_policy"  # policy s.3 reserves it to a supervisor
    UNKNOWN_ACTION = "unknown_action"      # kind the policy does not name — fail closed
    FORCED_REVIEW = "forced_review"        # a signal mandates review regardless of score
    SCORE_THRESHOLD = "score_threshold"    # weighted score >= tau


@dataclass
class RiskResult:
    """Output of the Risk Classifier, which is structurally separate from planning.

    The orchestrator asks exactly one question: `requires_approval`. It never
    re-derives the gate decision from `score` at the call site.
    """
    score: float
    hard_blocked: bool
    reason: str
    action_kind: str
    components: dict[str, float] = field(default_factory=dict)
    requires_approval: bool = False
    gate_layer: str = GateLayer.NONE.value
    triggered_signals: list[str] = field(default_factory=list)
    threshold: float = 0.0
    #: Set when the authority layer decided. The provision, so the reason can be
    #: read against the policy without re-running the engine.
    provision: str = ""
    provision_quote: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "hard_blocked": self.hard_blocked,
            "requires_approval": self.requires_approval,
            "gate_layer": self.gate_layer,
            "reason": self.reason,
            "action_kind": self.action_kind,
            "components": self.components,
            "triggered_signals": list(self.triggered_signals),
            "threshold": self.threshold,
            "provision": self.provision,
            "provision_quote": self.provision_quote,
        }


# ---------------------------------------------------------------------------
# Action result
# ---------------------------------------------------------------------------

class ActionStatus(str, Enum):
    AUTO_EXECUTED = "auto_executed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    GATED = "gated_pending"
    #: The policy reserves this action to a supervisor, so it was refused and
    #: escalated under s.4. Distinct from REJECTED, which is a human saying no to
    #: something the assistant was allowed to propose.
    REFUSED = "refused_and_escalated"
    ERROR = "error"
    SKIPPED = "skipped"
    #: A guardrail bypass (--auto-approve). Deliberately NOT `approved`: an audit
    #: log that cannot tell a real human decision from a demo-mode bypass is worse
    #: than no audit log.
    AUTO_APPROVED_BYPASS = "auto_approved_bypass"


#: Statuses under which an effect actually ran.
EXECUTED_STATUSES = frozenset({
    ActionStatus.AUTO_EXECUTED.value,
    ActionStatus.APPROVED.value,
    ActionStatus.EDITED.value,
    ActionStatus.AUTO_APPROVED_BYPASS.value,
})

SYSTEM_ACTOR = "system:orchestrator"


@dataclass
class ActionResult:
    """What actually happened after an action was proposed and (maybe) carried out."""
    action_id: str
    status: ActionStatus
    detail: str = ""
    human_reason: str = ""            # if rejected or edited, why
    actor: str = SYSTEM_ACTOR         # WHO decided — never blank
    executed: bool = False            # did an effect actually run?
    execution_detail: str = ""        # what the effect reported, verbatim
    artifacts: list[str] = field(default_factory=list)
    decided_at: str = field(default_factory=lambda: datetime.now().isoformat())
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def status_value(self) -> str:
        return self.status.value if isinstance(self.status, ActionStatus) else self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "status": self.status_value,
            "detail": self.detail,
            "human_reason": self.human_reason,
            "actor": self.actor,
            "executed": self.executed,
            "execution_detail": self.execution_detail,
            "artifacts": list(self.artifacts),
            "decided_at": self.decided_at,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------

@dataclass
class Skip:
    """A step declining to act on this referral, with its reason.

    Distinct from an error and from a no-op action. The escalation step returns
    this for a referral that stays inside section 2; the flagging step returns it
    for one that needs no human attention. Section 5.1 asks for a record of what was
    done and what was declined, and "this step considered the referral and had
    nothing to do" is part of that record.
    """
    reason: str


@dataclass
class TaskOutcome:
    """A completed (action, risk, result) triple, readable by later tasks.

    This is what closes the observe -> decide -> act -> observe loop. Without it a
    task cannot see what an earlier task concluded, and the note-drafting step has
    no way to use the history the retrieval step pulled.

    `value` is the live object the effect produced — a ResidentHistory, a
    TriageAssessment, a TriageNote. It is kept off `action.payload` so the ledger's
    JSON projection stays a record of what was proposed rather than a mixture of
    proposal and result.
    """
    task_id: str
    referral_id: str
    action: ProposedAction
    risk: RiskResult
    result: ActionResult
    value: Any = None


    @property
    def executed(self) -> bool:
        return self.result.executed

    @property
    def status(self) -> str:
        return self.result.status_value


@dataclass
class RunContext:
    """Shared context for a single morning run. Passed to every Task."""
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    run_date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    referrals: list[Referral] = field(default_factory=list)
    settings: Any = None              # Settings; typed Any to avoid a circular import
    actor: str = SYSTEM_ACTOR

    # referral_id -> task_id -> outcome
    outcomes: dict[str, dict[str, TaskOutcome]] = field(default_factory=dict)
    # referral_id -> resident history retrieved this run
    histories: dict[str, ResidentHistory] = field(default_factory=dict)
    # referral_id -> the authority determination for its requested action
    determinations: dict[str, Any] = field(default_factory=dict)
    # referral_id -> patterns that tripped the injection screen
    quarantined_referrals: dict[str, list[str]] = field(default_factory=dict)
    # referral_id -> notes about missing or degraded inputs
    data_quality: dict[str, list[str]] = field(default_factory=dict)

    def record(self, outcome: TaskOutcome) -> None:
        self.outcomes.setdefault(outcome.referral_id, {})[outcome.task_id] = outcome

    def outcome_for(self, referral_id: str, task_id: str) -> Optional[TaskOutcome]:
        """Read an earlier task's outcome for this referral. None if it has not run."""
        return self.outcomes.get(referral_id, {}).get(task_id)

    def history_for(self, referral_id: str) -> Optional[ResidentHistory]:
        return self.histories.get(referral_id)

    def is_quarantined(self, referral_id: str) -> bool:
        return referral_id in self.quarantined_referrals

    def note_data_quality(self, referral_id: str, note: str) -> None:
        notes = self.data_quality.setdefault(referral_id, [])
        if note not in notes:
            notes.append(note)


# ---------------------------------------------------------------------------
# Task ABC
# ---------------------------------------------------------------------------

class Task(ABC):
    """Abstract base for the steps of a morning run.

    To add a step:
      1. Create a file in src/tasks/
      2. Subclass Task, set `id`, `description`, `risk_profile` and `order`
      3. Implement plan()
      4. Override configure() if it needs shared dependencies

    It auto-registers via `src/tasks/__init__.py` and is sequenced by `order`.
    The orchestrator has no knowledge of any concrete task class.

    NOTE THE ABSENCE OF `execute`. A task proposes; the effect registry performs.
    A task cannot carry out its own proposal even if its author wanted it to,
    because there is no method here that runs one and the registry will not bind a
    callable to an action the policy reserves to a supervisor.
    """

    id: str = ""
    description: str = ""
    risk_profile: RiskProfile = RiskProfile()
    #: Pipeline position; lower runs first. Gaps of 10 mean a new step can be
    #: slotted between two existing ones without renumbering.
    order: int = 100
    #: The policy provision this step operates under, for the trace.
    provision: str = ""

    def configure(self, **deps: Any) -> None:
        """Receive shared run dependencies.

        The orchestrator calls this on every task with the same keyword set
        (policy, registry, history_client, settings, ...). Tasks that need nothing
        inherit this no-op; tasks that need something override and pick it out.
        Keeps the orchestrator free of `isinstance(task, ConcreteClass)` checks.
        """
        return None

    @abstractmethod
    def plan(self, referral: Referral, context: RunContext) -> Union[ProposedAction, "Skip", None]:
        """Propose what should happen for this referral, or Skip if nothing should.

        Returning `Skip(reason)` is a first-class answer, not a failure: the
        escalation step has nothing to do for a referral fully within section 2, and
        a step that says so is more honest than one that proposes a no-op. The
        orchestrator records the reason in the trace, so a skipped step is visible
        rather than a gap.

        May read, may call the model, may consult the policy. Must not cause a side
        effect: that is the registry's job, and the separation is what makes the
        approval gate structural.
        """
        ...


