from __future__ import annotations
"""Risk Classifier — structurally separate from the planning agent.

The planning LLM proposes; this module gates. It is deterministic code, not a
prompt. It makes no LLM call and has no prompt surface to attack.

FOUR layers, in strict precedence order. Each is a plain `if`:

  1. UNKNOWN KIND       -> gate. An action kind the authority rules do not name
                           fails CLOSED, which is section 6.1 in code: where it is
                           unclear whether an action falls within section 3, treat
                           it as though it does.
  2. AUTHORITY POLICY   -> gate. Kinds section 3 reserves to a supervisor. The set
                           comes from `data/policy/authority-rules.json`, not from
                           a literal in this file, so the boundary the Department
                           owns lives in the Department's document.
  3. FORCED REVIEW      -> gate. Specific signals (suspected injection,
                           unverifiable policy citation, planner asked to escalate)
                           mandate a human regardless of score.
  4. WEIGHTED SCORE     -> gate at or above threshold tau.

WHY LAYER 2 IS NOT THE REAL GATE
--------------------------------
It matters that this file is the *third* line of defence for a section 3 action,
not the first. Before the classifier ever runs, `src/effects/registry.py` has
refused to bind a callable to any restricted kind — so even if every branch below
were deleted, there would still be no code that terminates an award. This layer
exists to make the refusal legible in the trace, not to be the thing preventing
the act. `tests/test_effects_registry.py` deletes this layer and confirms the
action still cannot execute.

THE MONOTONICITY GUARANTEE
--------------------------
The deterministic base score (reversibility, scope, financial impact — all taken
from the Task's static RiskProfile, never from model output) is a FLOOR.
Model-influenced inputs are added on top and are clamped at >= 0:

    score = base  +  uncertainty(confidence)  +  signals

`uncertainty` is (1 - confidence) * weight, so a model reporting high confidence
contributes 0. It can never contribute a negative number. There is no code path by
which a planning LLM's own output lowers the score below `base`. That is what
makes "the model cannot grade its own risk down" structurally true rather than
merely intended.
"""

from typing import Optional

from src.config import Settings
from src.observability.logging_setup import get_logger, log_event
from src.policy.authority import AuthorityPolicy, load_policy
from src.tasks.base import (
    UNKNOWN_ACTION_KIND,
    GateLayer,
    ProposedAction,
    RiskProfile,
    RiskResult,
    RiskSignals,
    normalise_action_kind,
)

logger = get_logger(__name__)


# Signals that mandate human review regardless of the computed score.
# These are policy decisions, not arithmetic: a suspected prompt injection or an
# unverifiable policy citation is not something a threshold should be able to wave
# through.
FORCED_REVIEW_SIGNALS: frozenset = frozenset({
    "injection_suspected",
    "unverified_citation",
    "escalation_requested",
})

_SIGNAL_LABELS = {
    "irreversible": "This specific action cannot be undone.",
    "adverse_to_resident": "Action is adverse to the resident "
                           "(reduces, suspends or terminates support).",
    "affects_entitlement": "Action affects an award amount or eligibility status.",
    "authority_restricted": "The authority policy reserves this action to a supervisor.",
    "data_incomplete": "Planned on incomplete or unverified referral data.",
    "unverified_citation": "A policy claim could not be verified against a cited clause.",
    "injection_suspected": "Untrusted referral content tripped the injection screen.",
    "escalation_requested": "The planner itself requested human escalation.",
}


def _signal_weight(name: str, settings: Settings) -> float:
    return {
        "irreversible": settings.signal_weight_irreversible,
        "adverse_to_resident": settings.signal_weight_adverse,
        "affects_entitlement": settings.signal_weight_eligibility,
        "authority_restricted": settings.signal_weight_authority_restricted,
        "data_incomplete": settings.signal_weight_data_incomplete,
        "unverified_citation": settings.signal_weight_unverified_citation,
        "injection_suspected": settings.signal_weight_injection,
        "escalation_requested": settings.signal_weight_escalation,
    }.get(name, 0.0)


def _financial_component(amount: float, settings: Settings) -> tuple[float, Optional[str]]:
    """Tiered contribution for the money actually at stake in THIS action."""
    if amount >= settings.financial_high_amount:
        return settings.signal_weight_financial_high, (
            f"£{amount:,.0f} at stake (>= £{settings.financial_high_amount:,.0f} "
            f"high-value threshold)."
        )
    if amount >= settings.financial_moderate_amount:
        return settings.signal_weight_financial_moderate, (
            f"£{amount:,.0f} at stake (>= £{settings.financial_moderate_amount:,.0f} "
            f"moderate threshold)."
        )
    return 0.0, None


def classify(
    action: ProposedAction,
    risk_profile: RiskProfile,
    settings: Settings,
    *,
    policy: Optional[AuthorityPolicy] = None,
) -> RiskResult:
    """Classify the risk of a proposed action.

    Deterministic. Does not call an LLM. The planning agent has no influence over
    this code path beyond supplying inputs that can only raise the score.

    Args:
        action: the proposed action from the planning agent
        risk_profile: static risk hints from the Task definition
        settings: weights and threshold
        policy: the authority policy; loaded from cache if not supplied

    Returns:
        RiskResult. Callers must branch on `requires_approval` — never re-derive
        the gate decision from `score` at the call site.
    """
    threshold = settings.risk_threshold
    policy = policy or load_policy()

    # ProposedAction normalises on construction, but classify() may be called with
    # a hand-built action in tests or from a future caller. Re-derive so this
    # function is safe standing alone: a guardrail must not depend on someone else
    # having sanitised its input.
    kind = normalise_action_kind(action.raw_action_kind or action.action_kind)
    rule = policy.rule_for_kind(kind)
    signals = action.signals or RiskSignals()

    # ---- Layer 1: kind the policy does not name -> fail CLOSED -----------
    if rule is None or kind == UNKNOWN_ACTION_KIND:
        raw = action.raw_action_kind or action.action_kind
        default = policy.default_rule
        result = RiskResult(
            score=1.0,
            hard_blocked=True,
            requires_approval=True,
            gate_layer=GateLayer.UNKNOWN_ACTION.value,
            reason=(
                f"Action kind {raw!r} is not named in Authority Policy "
                f"{policy.policy_ref}. Under s.6.1, where it is unclear whether an "
                f"action falls within section 3, it is treated as though it does — so "
                f"an unrecognised kind is gated by default. The system will not "
                f"perform an effect it cannot classify."
            ),
            action_kind=kind,
            components={"unknown_action": 1.0},
            triggered_signals=["unknown_action_kind"],
            threshold=threshold,
            provision=default.cited_provision,
            provision_quote=default.quote,
        )
        log_event(
            logger, "risk.gated",
            gate_layer=result.gate_layer, action_id=action.id, task_id=action.task_id,
            referral_id=action.referral_id, raw_action_kind=str(raw), score=1.0,
        )
        return result

    # ---- Layer 2: section 3 reserves this to a supervisor ----------------
    if not rule.performable:
        result = RiskResult(
            score=1.0,
            hard_blocked=True,
            requires_approval=True,
            gate_layer=GateLayer.AUTHORITY_POLICY.value,
            reason=(
                f"{rule.label} engages Authority Policy {policy.policy_ref} "
                f"s.{rule.cited_provision}, which requires a supervisor's approval "
                f"recorded before the action is taken. This is not a score: it is the "
                f"policy. No effect is bound to this kind, so the action cannot "
                f"proceed from here in any form, including the partial or preparatory "
                f"version s.4.1 also forbids."
            ),
            action_kind=kind,
            components={"authority_policy": 1.0},
            triggered_signals=["authority_restricted"],
            threshold=threshold,
            provision=rule.cited_provision,
            provision_quote=rule.quote,
        )
        log_event(
            logger, "risk.gated",
            gate_layer=result.gate_layer, action_id=action.id, task_id=action.task_id,
            referral_id=action.referral_id, action_kind=kind,
            provision=rule.cited_provision, score=1.0,
        )
        return result

    # ---- Layer 4 arithmetic (computed before layer 3 so the score is always
    #      reported, even when a forced-review signal is what decides) -----

    # Deterministic base. Sourced entirely from the Task's static profile — no
    # model output touches these three terms. This is the score FLOOR.
    base_components = {
        "reversibility": (1 - risk_profile.reversibility) * settings.weight_reversibility,
        "scope": risk_profile.scope_of_impact * settings.weight_scope,
        "financial": risk_profile.financial_impact * settings.weight_financial,
    }
    base = sum(base_components.values())

    # Model-influenced uncertainty. Always >= 0: high confidence adds nothing, low
    # confidence adds risk. Never subtracts.
    uncertainty = max(0.0, (1.0 - action.confidence)) * settings.weight_confidence

    # Per-action signals. Every one of these can only add.
    signal_components: dict[str, float] = {}
    triggered: list[str] = []
    reasons: list[str] = []

    for name in _SIGNAL_LABELS:
        if getattr(signals, name, False):
            weight = _signal_weight(name, settings)
            if weight > 0:
                signal_components[f"signal:{name}"] = weight
            triggered.append(name)
            reasons.append(_SIGNAL_LABELS[name])

    fin_weight, fin_reason = _financial_component(signals.financial_amount, settings)
    if fin_weight > 0:
        signal_components["signal:financial_amount"] = fin_weight
        triggered.append("financial_amount")
        if fin_reason:
            reasons.append(fin_reason)

    components = {
        **base_components,
        "confidence_uncertainty": uncertainty,
        **signal_components,
    }
    raw_score = base + uncertainty + sum(signal_components.values())
    score = max(0.0, min(1.0, raw_score))

    # Invariant: the deterministic floor is never undercut. Asserted rather than
    # assumed, because this is the property the whole design rests on.
    assert score >= min(base, 1.0) - 1e-9, (
        f"Monotonicity violated: score {score} < deterministic base {base}"
    )

    # ---- Layer 3: forced-review signals -> gate regardless of score ------
    forced = [s for s in triggered if s in FORCED_REVIEW_SIGNALS]
    if forced:
        detail = " ".join(_SIGNAL_LABELS[s] for s in forced)
        result = RiskResult(
            score=score,
            hard_blocked=False,
            requires_approval=True,
            gate_layer=GateLayer.FORCED_REVIEW.value,
            reason=(
                f"Mandatory review triggered by {', '.join(forced)}. {detail} "
                f"These signals require a human regardless of the computed score "
                f"(score was {score:.3f}, threshold {threshold})."
            ),
            action_kind=kind,
            components=components,
            triggered_signals=triggered,
            threshold=threshold,
            provision=rule.cited_provision,
        )
        log_event(
            logger, "risk.gated",
            gate_layer=result.gate_layer, action_id=action.id, task_id=action.task_id,
            referral_id=action.referral_id, action_kind=kind, score=score,
            forced_signals=forced,
        )
        return result

    # ---- Layer 4: weighted score ----------------------------------------
    gated = score >= threshold
    if gated:
        reason = (
            f"Risk score {score:.3f} >= threshold {threshold}. "
            f"Base (deterministic) {base:.3f} "
            f"[reversibility={(1 - risk_profile.reversibility):.2f}x{settings.weight_reversibility}, "
            f"scope={risk_profile.scope_of_impact:.2f}x{settings.weight_scope}, "
            f"financial={risk_profile.financial_impact:.2f}x{settings.weight_financial}] "
            f"+ uncertainty {uncertainty:.3f} "
            f"+ signals {sum(signal_components.values()):.3f}."
        )
        if reasons:
            reason += " " + " ".join(reasons)
    else:
        reason = (
            f"Risk score {score:.3f} < threshold {threshold} and no mandatory-review "
            f"signal is present. {rule.label} is permitted under "
            f"s.{rule.cited_provision}; proceeding."
        )
        if reasons:
            reason += " Noted: " + " ".join(reasons)

    result = RiskResult(
        score=score,
        hard_blocked=False,
        requires_approval=gated,
        gate_layer=GateLayer.SCORE_THRESHOLD.value if gated else GateLayer.NONE.value,
        reason=reason,
        action_kind=kind,
        components=components,
        triggered_signals=triggered,
        threshold=threshold,
        provision=rule.cited_provision,
    )
    log_event(
        logger, "risk.gated" if gated else "risk.cleared",
        gate_layer=result.gate_layer, action_id=action.id, task_id=action.task_id,
        referral_id=action.referral_id, action_kind=kind, score=score,
        base=round(base, 4), uncertainty=round(uncertainty, 4),
        signals=triggered,
    )
    return result


def explain_reachability(risk_profile: RiskProfile, settings: Settings) -> dict:
    """Compute the achievable score range for a task's static profile.

    Used by `verify-guardrails` to show that every task can in fact reach the gate.
    A task whose maximum achievable score is below tau and which emits no
    gate-worthy signal is a task whose scored guardrail is decorative — this
    surfaces that at build time instead of in a demo.
    """
    base = (
        (1 - risk_profile.reversibility) * settings.weight_reversibility
        + risk_profile.scope_of_impact * settings.weight_scope
        + risk_profile.financial_impact * settings.weight_financial
    )
    max_signals = (
        settings.signal_weight_irreversible
        + settings.signal_weight_adverse
        + settings.signal_weight_eligibility
        + settings.signal_weight_authority_restricted
        + settings.signal_weight_data_incomplete
        + settings.signal_weight_unverified_citation
        + settings.signal_weight_injection
        + settings.signal_weight_escalation
        + settings.signal_weight_financial_high
    )
    return {
        "base": round(base, 4),
        "min_score": round(base, 4),                       # confidence=1.0, no signals
        "max_score_no_signals": round(min(1.0, base + settings.weight_confidence), 4),
        "max_score": round(min(1.0, base + settings.weight_confidence + max_signals), 4),
        "threshold": settings.risk_threshold,
        "can_gate_on_score_alone": (base + settings.weight_confidence) >= settings.risk_threshold,
        "can_gate_with_signals": (
            base + settings.weight_confidence + max_signals
        ) >= settings.risk_threshold,
    }
