from __future__ import annotations
"""The effect registry — the structural approval gate.

WHY THIS FILE EXISTS
--------------------
The brief asks for a hard approval gate on irreversible actions, and is precise
about what "hard" means: the action cannot proceed without approval, not that the
agent is instructed to ask nicely. A guardrail that consists only of an
instruction in a prompt is not a guardrail.

So the gate here is not a rule the agent follows. It is a missing function.

Every act this system can perform is a named `action_kind` with a Python callable
bound to it in this registry. Performing anything means calling
`EffectRegistry.perform(kind, request)`, which is the single code path from a
decision to a side effect -- there is no second one. And the registry will only
accept a binding for a kind that `data/policy/authority-rules.json` marks
`performable: true`, which is exactly the seven acts permitted by section 2 of
Authority Policy ACA-2026/1.

The eight section 3 kinds -- changing entitlement, suspending an award, altering
a payment, changing payment details, sending a communication, disclosing
information outside the Department, recording a finding of fact about conduct,
and any irreversible act -- have no callable bound to them, and `bind()` raises
`EffectBindingError` if anyone tries to bind one. Calling `perform()` on such a
kind raises `ActionNotPerformable`. Not "returns an error the caller may ignore":
raises.

HOW WE KNOW IT HOLDS
--------------------
Three checks, all mechanical:

  * `bind()` consults the policy on every call, so a restricted binding cannot be
    added even by mistake. The failure is at wiring time, not at run time.
  * `verify()` asserts the two-way invariant -- every performable kind has
    exactly one effect, no restricted kind has any -- and is run by
    `python -m src.main verify-guardrails` and by the test suite
    (`tests/test_effects_registry.py`).
  * The tests also delete the gate's *other* layers (risk classifier, HITL gate)
    and confirm a section 3 action still cannot execute, because the structural
    claim must not depend on the layers above it being correct.

The claim is therefore not "the agent was told not to terminate an award". It is
"this process contains no code that terminates an award". Supervisor approval of
an escalation authorises a *person* to act in the Department's systems; it never
unlocks an effect here, because there is nothing to unlock.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.domain.referral import Referral, ResidentHistory
from src.policy.authority import Authority, AuthorityPolicy, load_policy


class EffectError(RuntimeError):
    """Base class for registry failures."""


class EffectBindingError(EffectError):
    """An effect was bound to a kind that must not have one.

    Raised at wiring time -- when the pipeline is built -- so a mistake is a
    startup failure rather than a silent capability.
    """


class ActionNotPerformable(EffectError):
    """This process cannot perform the requested kind of action.

    Carries the policy provision so the message is auditable rather than a bare
    'not implemented'.
    """

    def __init__(self, message: str, *, action_kind: str = "", provision: str = "",
                 quote: str = "") -> None:
        super().__init__(message)
        self.action_kind = action_kind
        self.provision = provision
        self.quote = quote

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "action_not_performable",
            "message": str(self),
            "action_kind": self.action_kind,
            "provision": self.provision,
            "quote": self.quote,
        }


@dataclass
class EffectRequest:
    """Everything an effect is allowed to see.

    Effects receive this and nothing else -- no settings object, no database
    handle, no network client they were not handed. Narrowing the input is part of
    the containment: an effect cannot reach a system it was not given.
    """

    referral: Referral
    action_kind: str
    run_id: str = ""
    actor: str = ""
    history: Optional[ResidentHistory] = None
    payload: dict[str, Any] = field(default_factory=dict)
    workspace: str = "data/artifacts"


@dataclass
class EffectOutcome:
    """What an effect did. `detail` is recorded verbatim in the ledger.

    `value` carries the in-memory result for the next step of the run -- a
    `ResidentHistory`, a `TriageAssessment`, a `TriageNote`. It is deliberately
    kept out of `detail` because the ledger must hold plain JSON, and because a
    ledger entry that embeds live objects tends to drift from what was actually
    written to disk.
    """

    ok: bool
    action_kind: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str = ""
    value: Any = None

    @classmethod
    def failed(cls, action_kind: str, error: str) -> "EffectOutcome":
        return cls(ok=False, action_kind=action_kind,
                   summary=f"{action_kind} failed: {error}", error=error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action_kind": self.action_kind,
            "summary": self.summary,
            "detail": self.detail,
            "artifacts": list(self.artifacts),
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


EffectFn = Callable[[EffectRequest], EffectOutcome]


@dataclass
class RegistryReport:
    """Result of `EffectRegistry.verify()`."""

    ok: bool
    policy_ref: str
    rules_version: str
    bound: list[str] = field(default_factory=list)
    unbound_performable: list[str] = field(default_factory=list)
    restricted_kinds: list[str] = field(default_factory=list)
    illegally_bound: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "policy_ref": self.policy_ref,
            "rules_version": self.rules_version,
            "bound": list(self.bound),
            "unbound_performable": list(self.unbound_performable),
            "restricted_kinds": list(self.restricted_kinds),
            "illegally_bound": list(self.illegally_bound),
            "problems": list(self.problems),
        }

    def render(self) -> str:
        lines = [
            f"Effect registry against policy {self.policy_ref} "
            f"(rules v{self.rules_version})",
            f"  bound effects              : {len(self.bound)}",
        ]
        for kind in self.bound:
            lines.append(f"      + {kind}")
        lines.append(
            f"  restricted, deliberately unbound : {len(self.restricted_kinds)}"
        )
        for kind in self.restricted_kinds:
            lines.append(f"      - {kind}  (no code path exists)")
        if self.unbound_performable:
            lines.append("  PERMITTED BUT UNBOUND (these acts cannot run):")
            lines.extend(f"      ! {k}" for k in self.unbound_performable)
        if self.illegally_bound:
            lines.append("  RESTRICTED BUT BOUND — the gate is broken:")
            lines.extend(f"      !! {k}" for k in self.illegally_bound)
        lines.append(f"  verdict: {'PASS' if self.ok else 'FAIL'}")
        for problem in self.problems:
            lines.append(f"    {problem}")
        return "\n".join(lines)


class EffectRegistry:
    """Maps `action_kind` to the one function permitted to carry it out."""

    def __init__(self, policy: Optional[AuthorityPolicy] = None) -> None:
        self._policy = policy or load_policy()
        self._effects: dict[str, EffectFn] = {}

    @property
    def policy(self) -> AuthorityPolicy:
        return self._policy

    # -- wiring -------------------------------------------------------------

    def bind(self, action_kind: str, fn: EffectFn, *, replace: bool = False) -> None:
        """Bind an effect, or refuse to.

        Refuses when the kind is unknown to the policy (fail closed on typos --
        a misspelled kind would otherwise create an unpoliced capability), when
        the policy marks it restricted, or when something is already bound and
        `replace` was not asked for.
        """
        kind = (action_kind or "").strip()
        if not kind:
            raise EffectBindingError("cannot bind an effect to an empty action_kind")

        rule = self._policy.rule_for_kind(kind)
        if rule is None:
            raise EffectBindingError(
                f"action_kind {kind!r} is not named in the authority rules. "
                f"Effects may only be bound to kinds the policy recognises, so a "
                f"typo cannot create an unpoliced capability. Known kinds: "
                f"{', '.join(sorted(self._policy.known_action_kinds()))}"
            )
        if not rule.performable:
            raise EffectBindingError(
                f"refusing to bind an effect to {kind!r}: Authority Policy "
                f"{self._policy.policy_ref} s.{rule.cited_provision} makes this an "
                f"action requiring recorded supervisor approval before it is taken. "
                f"Policy text: {rule.quote!r} "
                f"This process must be structurally incapable of performing it, so "
                f"there must be no callable here. Escalate under s.4 instead; the "
                f"supervisor acts in the Department's systems, not through this code."
            )
        if kind in self._effects and not replace:
            raise EffectBindingError(
                f"an effect is already bound to {kind!r}; pass replace=True to "
                f"override deliberately"
            )
        self._effects[kind] = fn

    def bind_all(self, effects: dict[str, EffectFn]) -> None:
        for kind, fn in effects.items():
            self.bind(kind, fn)

    # -- queries ------------------------------------------------------------

    def is_bound(self, action_kind: str) -> bool:
        return action_kind in self._effects

    def bound_kinds(self) -> list[str]:
        return sorted(self._effects)

    def authority_for(self, action_kind: str) -> Authority:
        return self._policy.authority_for_kind(action_kind)

    # -- the single path to a side effect -----------------------------------

    def perform(self, request: EffectRequest) -> EffectOutcome:
        """Carry out an action, or raise.

        This is the only function in the system that invokes an effect. Every
        refusal below is a raise, never a returned flag, so a caller that forgets
        to check cannot accidentally proceed.
        """
        kind = (request.action_kind or "").strip()
        rule = self._policy.rule_for_kind(kind)

        if rule is None:
            raise ActionNotPerformable(
                f"unknown action kind {kind!r}: the authority rules do not name it, "
                f"so under Authority Policy {self._policy.policy_ref} s.6.1 it is "
                f"treated as requiring supervisor approval and is not performed here",
                action_kind=kind,
                provision=self._policy.default_rule.cited_provision,
                quote=self._policy.default_rule.quote,
            )

        if not rule.performable:
            raise ActionNotPerformable(
                f"{kind!r} engages Authority Policy {self._policy.policy_ref} "
                f"s.{rule.cited_provision}, which requires a supervisor's approval "
                f"recorded before the action is taken. No effect is bound to this "
                f"kind and none can be, so the action cannot proceed from here — "
                f"not even as the partial or preparatory version s.4.1 also "
                f"forbids. Policy text: {rule.quote!r}",
                action_kind=kind,
                provision=rule.cited_provision,
                quote=rule.quote,
            )

        fn = self._effects.get(kind)
        if fn is None:
            # Permitted by policy but nothing wired it up: a build error, not a
            # policy question. Still fail closed rather than pretend it ran.
            raise ActionNotPerformable(
                f"{kind!r} is permitted under s.{rule.cited_provision} but no effect "
                f"is bound to it in this build, so it cannot be carried out. Run "
                f"'verify-guardrails' to see the registry state.",
                action_kind=kind,
                provision=rule.cited_provision,
                quote=rule.quote,
            )

        started = time.perf_counter()
        try:
            outcome = fn(request)
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            elapsed = (time.perf_counter() - started) * 1000.0
            failure = EffectOutcome.failed(kind, f"{type(exc).__name__}: {exc}")
            failure.duration_ms = elapsed
            return failure

        elapsed = (time.perf_counter() - started) * 1000.0
        if not isinstance(outcome, EffectOutcome):
            return EffectOutcome.failed(
                kind,
                f"effect returned {type(outcome).__name__}, expected EffectOutcome",
            )
        outcome.duration_ms = elapsed
        if not outcome.action_kind:
            outcome.action_kind = kind
        return outcome

    # -- self-check ---------------------------------------------------------

    def verify(self) -> RegistryReport:
        """Assert the two-way invariant that makes the gate structural."""
        performable = set(self._policy.performable_kinds())
        restricted = set(self._policy.restricted_kinds())
        bound = set(self._effects)

        illegally_bound = sorted(bound & restricted)
        unbound_performable = sorted(performable - bound)
        unknown_bound = sorted(bound - performable - restricted)

        problems: list[str] = []
        for kind in illegally_bound:
            rule = self._policy.rule_for_kind(kind)
            provision = rule.cited_provision if rule else "3"
            problems.append(
                f"CRITICAL: an effect is bound to restricted kind {kind!r} "
                f"(s.{provision}). The approval gate is bypassable. Remove it."
            )
        for kind in unbound_performable:
            problems.append(
                f"WARNING: {kind!r} is permitted under section 2 but has no effect "
                f"bound, so that step of the morning cannot run."
            )
        for kind in unknown_bound:
            problems.append(
                f"CRITICAL: an effect is bound to {kind!r}, which the authority "
                f"rules do not name. Unpoliced capability."
            )

        ok = not illegally_bound and not unknown_bound and not unbound_performable
        return RegistryReport(
            ok=ok,
            policy_ref=self._policy.policy_ref,
            rules_version=self._policy.rules_version,
            bound=sorted(bound),
            unbound_performable=unbound_performable,
            restricted_kinds=sorted(restricted),
            illegally_bound=illegally_bound,
            problems=problems,
        )

    def capability_statement(self) -> str:
        """Plain-English statement of what this build can and cannot do.

        Printed by `verify-guardrails` and quoted in DECISIONS.md, generated from
        the policy rather than written by hand so it cannot drift from the code.
        """
        lines = [
            f"This process can perform {len(self._effects)} kinds of action, all of "
            f"them permitted by section 2 of Authority Policy {self._policy.policy_ref}:",
        ]
        for kind in self._sorted_by_provision(self._effects):
            rule = self._policy.rule_for_kind(kind)
            label = rule.label if rule else kind
            provision = rule.cited_provision if rule else "?"
            lines.append(f"  can    s.{provision:<4} {label}")
        lines.append("")
        lines.append(
            "It is structurally incapable of the following, because no callable is "
            "bound to them and the registry refuses to accept one:"
        )
        for kind in self._sorted_by_provision(self._policy.restricted_kinds()):
            rule = self._policy.rule_for_kind(kind)
            label = rule.label if rule else kind
            provision = rule.cited_provision if rule else "3"
            lines.append(f"  cannot s.{provision:<4} {label}")
        lines.append("")
        lines.append(
            "Anything not named above is unknown to the rules and is treated under "
            "s.6.1 as requiring approval, so it is refused too. The boundary is "
            "default-deny."
        )
        return "\n".join(lines)

    def _sorted_by_provision(self, kinds: Any) -> list[str]:
        """Order kinds by the provision they cite, so the statement reads like the policy."""
        def key(kind: str) -> tuple:
            rule = self._policy.rule_for_kind(kind)
            provision = rule.cited_provision if rule else "9"
            parts = tuple(int(p) if p.isdigit() else 0 for p in provision.split("."))
            return (parts, kind)

        return sorted(kinds, key=key)
